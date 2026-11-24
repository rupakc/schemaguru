"""The ``Guru`` façade (spec 2 G1/G2/G9, 8.2, 12, 19): wiring, caching, laziness, tools."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
import types
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import schemaguru as sg
from schemaguru.catalog import DBAPIExecutor, reader_for
from schemaguru.catalog.executor import connect as connect_url
from schemaguru.config import Budget, Config
from schemaguru.errors import SchemaGuruError
from schemaguru.graph import Refusal
from schemaguru.guru import Guru
from schemaguru.measure import flbridge
from schemaguru.model.facts import Provenance, Status
from schemaguru.model.refs import ObjectRef
from schemaguru.scope.tokens import CharTokenCounter, TiktokenCounter
from tests.fixtures.shop import ShopFacts, build_shop
from tests.unit.draft.conftest import Script
from tests.unit.measure.fake_backend import FakeBackend

duckdb = pytest.importorskip("duckdb")

ORD = ObjectRef(None, "shop", "orders")
CUS = ObjectRef(None, "shop", "customers")
EXPORTS = [
    "Guru",
    "Config",
    "Budget",
    "SamplingConfig",
    "GraphConfig",
    "ScopeConfig",
    "CrawlConfig",
    "ObjectRef",
    "JoinRef",
    "Fact",
    "FactKind",
    "Provenance",
    "Status",
    "ContextView",
    "PathResult",
    "Refusal",
    "RefreshReport",
    "Diagnostic",
    "SchemaGuruError",
    "catalog",
    "measure",
    "draft",
    "render",
    "scope",
    "graph",
    "pii",
    "overlays",
    "guard",
    "__version__",
]


@pytest.fixture(scope="module")
def shop_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The shop warehouse in a DuckDB *file* (a real URL for ``Guru.connect``)."""
    path = tmp_path_factory.mktemp("wh") / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    build_shop(conn, "duckdb")
    conn.close()
    return path


def make_guru(
    tmp_path: Path, conn: Any, *, backend: Any = None, config: Config | None = None, **kw: Any
) -> Guru:
    return Guru(
        reader=reader_for("duckdb", DBAPIExecutor(conn)),
        backend=backend if backend is not None else FakeBackend(conn),
        store=str(tmp_path / "guru.db"),
        config=config,
        dialect="duckdb",
        **kw,
    )


@pytest.fixture
def guru(tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts]) -> Iterator[Guru]:
    g = make_guru(tmp_path, shop_duckdb[0])
    g.crawl()
    yield g
    g.close()


OVERLAY = """\
version: 1
tables:
  shop.orders:
    description: One row per order placed on the storefront.
    use_cases: [refund rate by month, revenue by month]
    soft_delete: "deleted IS NULL OR NOT deleted"
    columns:
      status:
        values: [paid, refunded, cancelled]
chains:
  - name: order to payment
    because: payments are matched via the order reference
    steps: [shop.orders.order_id -> shop.payments.order_id]
glossary:
  - term: refund rate
    definition: refunded orders over all orders
    maps_to: [shop.orders]
"""


# --- public API ------------------------------------------------------------------------------


def test_exports_exact() -> None:
    assert sg.__all__ == EXPORTS
    for name in EXPORTS:
        assert getattr(sg, name) is not None, name
    assert sg.Guru is Guru
    assert sg.__version__ == "0.1.0"


def test_three_line_quickstart(
    tmp_path: Path, shop_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    # --- spec 2 G1, verbatim ---
    guru = sg.Guru.connect(f"duckdb:///{shop_file}")
    text = str(guru.context("monthly revenue by customer segment"))
    # ---
    assert (tmp_path / ".schemaguru" / "default.db").exists()  # the default store path
    assert "[shop.customers]" in text and "[shop.orders]" in text
    assert "unmeasured" in text  # first touch: never blocked, never measured (spec 12.5)
    if "measure" not in flbridge.capabilities():
        report = guru.measure()
        assert [d.code for d in report.errors] == ["SG304"]
        assert report.measured_tables == []
    guru.close()

    # the same store, now with a measurement backend: the queued first-touch tables drain
    conn = duckdb.connect(str(shop_file), read_only=True)  # the fallback opens read-only
    fake = FakeBackend(conn)
    again = sg.Guru.connect(f"duckdb:///{shop_file}", backend=fake)
    report = again.measure_queue()
    assert CUS in report.measured_tables and ORD in report.measured_tables
    assert "1,000 rows (measured" in str(again.context("monthly revenue by customer segment"))
    again.close()
    conn.close()


def test_connect_helper(tmp_path: Path) -> None:
    conn = connect_url("duckdb:///:memory:")
    assert conn.dialect == "duckdb"
    # the DuckDB fallback is a FactLang connector when FactLang ships one (reads + measurement)
    has_connector = hasattr(importlib.import_module("factlang.connectors"), "DBAPIConnector")
    if has_connector:
        assert conn.connector is not None and conn.connector.dialect == "duckdb"
        assert conn.connector.connection is conn.raw
    else:
        assert conn.connector is None
    assert conn.executor.run("SELECT 1 AS one") == [{"one": 1}]
    assert conn.executor.run("SELECT ? AS one", [1]) == [{"one": 1}]
    conn.close()
    with pytest.raises(SchemaGuruError, match="SG106"):
        connect_url("nosuchdb://user:secret@host/db")
    try:
        connect_url("nosuchdb://user:secret@host/db")
    except SchemaGuruError as e:
        assert "secret" not in str(e)  # credentials never reach messages
    with pytest.raises(SchemaGuruError, match="SG106"):
        connect_url("not a url")


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        # parses cleanly, one '@' or none: scheme://host[:port] only
        ("postgresql://alice:secret@db:5432/x?sslmode=require", "postgresql://db:5432"),
        ("snowflake://acct/db?user=a&password=hunter2", "snowflake://acct"),
        ("snowflake://acct/db?a=1?password=hunter2", "snowflake://acct"),
        ("snowflake://acct/db?passphrase=s3cret", "snowflake://acct"),
        ("mssql://host/db;Password=s3cret", "mssql://host"),
        ("duckdb:///shop.duckdb", "duckdb://"),
        ("postgresql://u:pw@[::1]:5432/db", "postgresql://[::1]:5432"),
        # anything else fails closed
        ("postgresql://alice:p/ss@db/x", "postgresql://***"),
        ("mysql://u:pa@ss@h/db", "mysql://***"),
        ("mysql://u:pa#ss@h/db", "mysql://***"),
        ("postgresql://u:p?ss@h/db", "postgresql://***"),
        ("postgresql://u:secret@[::1/db", "postgresql://***"),
        ("not a url with secret", "***"),
    ],
)
def test_redact_fails_closed(url: str, shown: str) -> None:
    from schemaguru.catalog.executor import redact_url

    assert redact_url(url) == shown


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://alice:p/ss@db/x",
        "mysql://u:pa@ss@h/db",
        "mysql://u:pa#ss@h/db",
        "postgresql://u:p?ss@h/db",
        "snowflake://acct/db?a=1?password=hunter2",
        "snowflake://acct/db?passphrase=s3cret",
        "mssql://host/db;Password=s3cret",
        "postgresql://u:secret@[::1/db",
    ],
)
def test_connect_errors_never_echo_secrets(url: str) -> None:
    with pytest.raises(SchemaGuruError, match="SG106") as exc:  # never a raw ValueError
        connect_url(url.replace(url.split("://")[0], "nosuchdb", 1))
    msg = str(exc.value)
    for secret in ("secret", "pa", "ss", "hunter2", "s3cret", "alice", "/x", "/db", "?"):
        assert secret not in msg.split("SG106", 1)[1].replace("pass a connector", ""), msg


def test_guru_repr_hides_the_url(tmp_path: Path, shop_file: Path) -> None:
    g = Guru.connect(f"duckdb:///{shop_file}", store=str(tmp_path / "r.db"))
    text = repr(g)
    assert "duckdb://" in text and str(shop_file) not in text
    g.close()


def test_duckdb_fallback_is_read_only(tmp_path: Path, shop_file: Path) -> None:
    missing = tmp_path / "typo.duckdb"
    with pytest.raises(SchemaGuruError, match="SG106"):
        connect_url(f"duckdb:///{missing}")
    assert not missing.exists()  # a typo never creates a warehouse file
    conn = connect_url(f"duckdb:///{shop_file}")
    with pytest.raises(Exception, match=r"(?i)read.only"):
        conn.raw.execute("CREATE TABLE shop.x(i INTEGER)")
    conn.close()


def test_connect_crawls_an_empty_store_only(tmp_path: Path, shop_file: Path) -> None:
    store = str(tmp_path / "s.db")
    g = Guru.connect(f"duckdb:///{shop_file}", store=store, name="x")
    assert g.store.table(ORD) is not None
    g.close()
    g2 = Guru.connect(f"duckdb:///{shop_file}", store=store, crawl=False)
    assert g2.store.table(ORD) is not None
    g2.close()


def test_guru_needs_a_reader_for_crawl_and_refresh(tmp_path: Path) -> None:
    g = Guru(store=str(tmp_path / "s.db"))
    with pytest.raises(SchemaGuruError, match="reader"):
        g.crawl()
    with pytest.raises(SchemaGuruError, match="reader"):
        g.refresh()
    # no backend: measuring reports SG304, nothing is written
    report = g.measure(all=True)
    assert [d.code for d in report.errors] == ["SG304"]
    assert g.verify("ROWS shop.orders VALUE >=1") == []
    g.close()


def test_store_argument_forms(tmp_path: Path) -> None:
    from schemaguru.store.sqlite import SQLiteStore

    own = SQLiteStore(tmp_path / "own.db")
    g = Guru(store=own)
    assert g.store is own
    g.close()
    own.table(ORD)  # a store the caller passed is not closed by the guru
    own.close()
    g2 = Guru(config=Config(store_path=str(tmp_path / "cfg.db")))
    assert Path(g2.store.path).name == "cfg.db"  # type: ignore[attr-defined]
    g2.close()


# --- laziness (spec 8.2, 12.5) ----------------------------------------------------------------


def test_context_never_spends_by_default(
    tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts]
) -> None:
    fake = FakeBackend(shop_duckdb[0])
    g = make_guru(tmp_path, shop_duckdb[0], backend=fake)
    g.crawl()
    view = g.context("orders per customer")
    assert fake.measured == [] and fake.estimated == []
    assert ORD in view.tables
    queued = {ref for ref, _ in g.store.queue_pop(100)}
    assert set(view.tables) <= queued  # first touch: every shown, unmeasured table is queued
    assert not any(f.provenance is Provenance.MEASURED for f in g.store.iter_facts())
    g.close()


def test_background_mode_drains(tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts]) -> None:
    conn = shop_duckdb[0].cursor()  # the drainer thread gets its own DuckDB connection
    fake = FakeBackend(conn)
    cfg = Config(measure_on_demand="background")
    g = make_guru(tmp_path, shop_duckdb[0], backend=fake, config=cfg, drain_interval_s=0.05)
    g.crawl()
    first = g.context("orders per customer")
    assert "unmeasured" in first.text
    assert g.context("orders per customer") is first  # cached until the drainer writes
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        # the SAME Guru: the drainer's writes invalidate its cached view
        if "10,000 rows (measured" in g.context("orders per customer").text:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the background drainer's measurements never reached this Guru's view")
    drainer = g.drainer
    g.close()  # stops the drainer and waits for it
    assert drainer is not None and not drainer.is_alive()
    conn.close()


def test_config_setter_restarts_the_drainer_on_the_one_scheduler(
    tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts]
) -> None:
    conn = shop_duckdb[0].cursor()
    fake = FakeBackend(conn)
    bg = Config(measure_on_demand="background")
    g = make_guru(tmp_path, shop_duckdb[0], backend=fake, config=bg, drain_interval_s=0.05)
    g.crawl()
    sched = g._scheduler
    g.context("orders per customer")
    old = g.drainer
    assert old is not None and old.is_alive() and old.scheduler is sched
    g.config = replace(bg, enum_max_distinct=5)  # still background: restarted
    assert not old.is_alive()
    assert g._scheduler is sched and sched.config.enum_max_distinct == 5
    new = g.drainer
    assert new is not None and new is not old and new.is_alive() and new.scheduler is sched
    g.config = Config()  # queue mode: stopped, not restarted
    assert not new.is_alive() and g.drainer is None
    assert g._scheduler is sched
    # a budget override is a run-level guard on the same scheduler (one lock, no second runner)
    report = g.measure(CUS, budget=Budget(max_bytes_total=1, max_bytes_per_probe=None))
    assert g._scheduler is sched
    assert report.bytes == 0 and {d.code for d in report.errors} & {"SG301", "SG302"}
    g.close()
    conn.close()


def test_block_mode_measures_outside_the_lock_with_a_time_cap(
    tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts]
) -> None:
    held: list[bool] = []
    fake = FakeBackend(shop_duckdb[0])
    real = fake.measure

    def measure(reqs: Any) -> Any:
        held.append(g._lock._is_owned())  # type: ignore[attr-defined]
        return real(reqs)

    fake.measure = measure  # type: ignore[method-assign]
    g = make_guru(tmp_path, shop_duckdb[0], backend=fake, config=Config(measure_on_demand="block"))
    g.crawl()
    assert "10,000 rows (measured" in g.context("orders per customer").text
    assert held and not any(held)  # warehouse calls never run under the façade lock
    g.close()
    assert Config().block_timeout_s == 10.0
    capped = Config(measure_on_demand="block", block_timeout_s=0.0)
    g = make_guru(tmp_path / "capped", shop_duckdb[0], config=capped)
    g.crawl()
    view = g.context("orders per customer")  # the cap stops the run; the view still renders
    assert "unmeasured" in view.text
    assert {r for r, _ in g.store.queue_pop(100)} & set(view.tables)  # what was left is queued
    g.close()


def test_one_retrieval_per_render(guru: Guru) -> None:
    calls: list[int] = []
    real = guru.retriever.search

    def search(q: str, k: int) -> Any:
        calls.append(k)
        return real(q, k)

    guru.retriever.search = search  # type: ignore[method-assign]
    guru.context("orders per customer")
    assert len(calls) == 1


def test_block_mode_measures_scoped_tables(
    tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts]
) -> None:
    fake = FakeBackend(shop_duckdb[0])
    g = make_guru(tmp_path, shop_duckdb[0], backend=fake, config=Config(measure_on_demand="block"))
    g.crawl()
    view = g.context("orders per customer")
    assert fake.measured  # block mode is the user's opt-in to spend
    assert "10,000 rows (measured" in view.text
    assert g.store.queue_pop(100) == []  # measured, not queued
    g.close()


def test_background_mode_without_backend_never_starts(tmp_path: Path) -> None:
    g = Guru(store=str(tmp_path / "s.db"), config=Config(measure_on_demand="background"))
    g.context("anything")
    assert g.drainer is None
    g.close()


# --- caching (spec 12.2) ----------------------------------------------------------------------


def test_view_cache_hit_and_invalidation(guru: Guru, tmp_path: Path) -> None:
    a = guru.context("refund rate by month")
    assert guru.context("refund rate by month") is a  # cached
    assert guru.context("refund rate by month", budget_tokens=3000) is not a  # budget in key
    assert guru.context("refund rate by month", profile="planner") is not a
    md = guru.context("refund rate by month", format="markdown")
    assert str(md).startswith("# Context:") and md.text == a.text
    with pytest.raises(ValueError, match="format"):
        guru.context("refund rate by month", format="nope")
    overlay = tmp_path / "o.yaml"
    overlay.write_text(OVERLAY, encoding="utf-8")
    guru.load_overlays(overlay)
    b = guru.context("refund rate by month")
    assert b is not a and "One row per order placed" in b.text
    guru.measure(ORD)
    c = guru.context("refund rate by month")
    assert c is not b and "10,000 rows (measured" in c.text


def test_cache_disabled_and_bounded(tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts]) -> None:
    from schemaguru.config import ScopeConfig

    g = make_guru(tmp_path, shop_duckdb[0], config=Config(scope=ScopeConfig(cache_entries=0)))
    g.crawl()
    assert g.context("orders") is not g.context("orders")
    g.close()
    g = make_guru(tmp_path, shop_duckdb[0], config=Config(scope=ScopeConfig(cache_entries=1)))
    a = g.context("orders")
    g.context("customers")
    assert g.context("orders") is not a  # evicted (LRU of one)
    g.close()


def test_views_are_deterministic(guru: Guru) -> None:
    a = guru.context("revenue by customer segment")
    guru.crawl()  # a write: invalidates, re-renders
    b = guru.context("revenue by customer segment")
    assert a is not b and a.text == b.text and a.fingerprint == b.fingerprint
    fps = guru.fingerprint_views(["revenue by customer segment"])
    assert fps == {"revenue by customer segment": a.fingerprint}


# --- names, paths, overlays -------------------------------------------------------------------


def test_bare_and_ambiguous_names(tmp_path: Path) -> None:
    conn = duckdb.connect()
    conn.execute("CREATE SCHEMA a; CREATE SCHEMA b")
    conn.execute("CREATE TABLE a.t(id INTEGER PRIMARY KEY); CREATE TABLE b.t(id INTEGER)")
    conn.execute("CREATE TABLE a.only_here(id INTEGER)")
    g = make_guru(tmp_path, conn)
    g.crawl()
    assert g.table_ref("only_here") == ObjectRef(None, "a", "only_here")
    assert g.table_ref("a.t") == ObjectRef(None, "a", "t")
    assert g.table_ref(ObjectRef(None, "a", "t", "id")) == ObjectRef(None, "a", "t")
    with pytest.raises(SchemaGuruError, match=r"ambiguous.*a\.t.*b\.t"):
        g.table_ref("t")
    with pytest.raises(SchemaGuruError, match="unknown table"):
        g.table_ref("nowhere")
    with pytest.raises(SchemaGuruError, match="unknown table"):
        g.table_ref("x..y")
    assert g.column_ref("a.t.id") == ObjectRef(None, "a", "t", "id")
    with pytest.raises(SchemaGuruError, match="unknown column"):
        g.column_ref("a.t.nope")
    with pytest.raises(SchemaGuruError, match="column"):
        g.column_ref("only_here")
    g.close()
    conn.close()


def test_find_path_and_rules(guru: Guru, tmp_path: Path) -> None:
    guru.measure(["shop.orders", "shop.customers", "shop.payments"])
    res = guru.find_path("orders", "customers")
    assert res.refusal is None and res.paths[0].tables == (ORD, CUS)
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "version: 1\nwaypoints:\n  - pair: [shop.orders, shop.customers]\n"
        '    require: "edge(shop.orders.customer_id->shop.customers.customer_id)"\n'
        '    because: ""\n'
        "  - pair: [shop.orders, shop.payments]\n"
        '    require: "node(shop.nowhere)"\n'
        "    because: dead requirement\n",
        encoding="utf-8",
    )
    diags = guru.load_overlays(bad)
    codes = {d.code for d in diags}
    assert "SG602" in codes and "SG604" in codes
    assert {d.code for d in guru.diagnostics} >= {"SG602", "SG604"}
    # fail closed: the malformed rule still governs its pair
    res = guru.find_path("shop.orders", "shop.customers")
    assert res.refusal is Refusal.WAYPOINT_UNSATISFIABLE
    assert guru.find_path("orders", "orders").refusal is Refusal.SAME_TABLE
    assert asyncio.run(guru.afind_path("orders", "customers")).refusal is (
        Refusal.WAYPOINT_UNSATISFIABLE
    )


def test_overlay_chain_compiles_and_reports(guru: Guru, tmp_path: Path) -> None:
    guru.measure(["shop.orders", "shop.payments", "shop.customers"])
    ok = tmp_path / "ok.yaml"
    ok.write_text(OVERLAY, encoding="utf-8")
    assert not [d for d in guru.load_overlays(ok) if d.code == "SG605"]
    res = guru.find_path("orders", "payments")
    assert res.refusal is None
    broken = tmp_path / "chain.yaml"
    broken.write_text(
        "version: 1\nchains:\n  - name: impossible\n    because: test\n"
        "    steps: [shop.orders.status -> shop.customers.segment]\n",
        encoding="utf-8",
    )
    diags = guru.load_overlays(broken)
    assert any(d.code == "SG605" for d in diags)
    assert any(d.code == "SG605" for d in guru.diagnostics)


def test_overlay_filters_validated_with_render_seam(
    guru: Guru, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from schemaguru import guru as guru_mod
    from schemaguru.diagnostics import Diagnostic, DiagnosticBag, Severity

    seen: list[tuple[str, str]] = []

    def strict(text: str, dialect: str) -> DiagnosticBag:
        seen.append((text, dialect))
        bag = DiagnosticBag()
        if "deleted" in text:
            bag.add(Diagnostic("SG502", Severity.ERROR, f"invalid filter {text!r}"))
        return bag

    monkeypatch.setattr(guru_mod, "validate_filter", strict)
    p = tmp_path / "o.yaml"
    p.write_text(
        OVERLAY.replace(
            "    steps: [shop.orders.order_id -> shop.payments.order_id]",
            "    steps:\n      - shop.orders.order_id -> shop.payments.order_id\n"
            '      - {filter: "deleted IS NULL", table: shop.orders}',
        ),
        encoding="utf-8",
    )
    diags = guru.load_overlays(p)
    assert ("deleted IS NULL OR NOT deleted", "duckdb") in seen
    sg502 = [d for d in diags if d.code == "SG502"]
    assert len(sg502) == 2 and all(d.location for d in sg502)
    assert guru.store.facts_for([ORD], ["SOFT_DELETE"]) == []  # the rejected fact is dropped
    assert guru.store.facts_for([ORD], ["DESCRIPTION"])  # the rest of the file still loads


def _soft_delete_overlay(tmp_path: Path, predicate: str) -> Path:
    p = tmp_path / "soft.yaml"
    p.write_text(
        f"version: 1\ntables:\n  shop.orders:\n    soft_delete: {json.dumps(predicate)}\n",
        encoding="utf-8",
    )
    return p


def test_declared_soft_delete_checked_against_the_data(guru: Guru, tmp_path: Path) -> None:
    """6d: a human soft-delete filter that fits the measured data stays a (human, unchecked)
    filter; one naming a missing column, or values the column never holds, gets SG210, a
    warning in the view and no place in a SQL snippet. The human text itself is kept."""
    guru.measure(["shop.orders", "shop.customers"])
    good = guru.load_overlays(_soft_delete_overlay(tmp_path, "deleted IS NULL OR NOT deleted"))
    assert not [d for d in good if d.code == "SG210"]
    text = guru.context("orders per customer").text
    assert "live rows: (deleted IS NULL OR NOT deleted) (human, unchecked)" in text
    assert "WHERE" in text  # the fitting filter goes into the join snippet

    for predicate, why in [
        ("is_deleted = 0", "names no column"),
        ("status != 'removed'", "include none it compares with"),
    ]:
        diags = guru.load_overlays(_soft_delete_overlay(tmp_path, predicate))
        sg210 = [d for d in diags if d.code == "SG210"]
        assert len(sg210) == 1 and why in sg210[0].message, diags
        assert sg210[0].location and "soft.yaml" in sg210[0].location
        text = guru.context("orders per customer").text
        warning = next(ln for ln in text.splitlines() if "live-rows filter" in ln)
        assert "⚠" in warning and "does not match the data" in warning and why in warning
        assert "WHERE" not in text  # never a silent filter
        stored = guru.store.facts_for([ORD], ["SOFT_DELETE"])
        assert [f.value["predicate"] for f in stored if f.provenance is Provenance.HUMAN] == [
            predicate
        ]


def test_waived_soft_delete_mismatch_is_not_reported(guru: Guru, tmp_path: Path) -> None:
    """A live waiver on the table's SOFT_DELETE silences SG210, as it turns the view's warning
    into WAIVED (render/knowledge.py); the filter still stays out of SQL snippets."""
    guru.measure(["shop.orders", "shop.customers"])
    p = tmp_path / "w.yaml"
    p.write_text(
        'version: 1\ntables:\n  shop.orders:\n    soft_delete: "is_deleted = 0"\n'
        'waivers:\n  - fact: "SOFT_DELETE shop.orders"\n    reason: column being added\n'
        "    expires: 2999-01-01\n",
        encoding="utf-8",
    )
    assert not [d for d in guru.load_overlays(p) if d.code == "SG210"]
    text = guru.context("orders per customer").text
    assert "WAIVED until 2999-01-01" in text and "WHERE" not in text
    p.write_text(p.read_text(encoding="utf-8").replace("2999-01-01", "2001-01-01"), "utf-8")
    assert [d for d in guru.load_overlays(p) if d.code == "SG210"]  # expired: reported


def test_malformed_entry_retracts_its_previous_fact(guru: Guru, tmp_path: Path) -> None:
    """An entry-level error (SG208 on one entry) costs that entry: its earlier fact goes;
    the rest of the file still loads. Only a file that fails as a whole keeps its facts."""
    p = tmp_path / "e.yaml"
    p.write_text(OVERLAY, encoding="utf-8")
    guru.load_overlays(p)
    status = ObjectRef(None, "shop", "orders", "status")
    assert guru.store.facts_for([status], ["VALUES"])
    p.write_text(
        OVERLAY.replace("        values: [paid, refunded, cancelled]", "        values: {paid: 1}"),
        encoding="utf-8",
    )
    diags = guru.load_overlays(p)
    assert any(d.code == "SG208" for d in diags)
    human = [f for f in guru.store.facts_for([status], ["VALUES"]) if f.provenance.name == "HUMAN"]
    assert human == []
    assert guru.store.facts_for([ORD], ["DESCRIPTION"])  # the other entries stay


def test_removed_overlay_entries_are_retracted(guru: Guru, tmp_path: Path) -> None:
    """17b: load, remove an entry, reload: the HUMAN facts it produced leave the store and the
    view. A file that fails to load keeps its previous facts until it loads again; facts from
    other sources (accepted drafts, measurements) are never touched."""
    guru.measure(["shop.orders", "shop.customers"])
    p = tmp_path / "o.yaml"
    p.write_text(OVERLAY, encoding="utf-8")
    guru.load_overlays(p)
    assert "One row per order placed on the storefront." in guru.context("refund rate").text
    measured = {f.id for f in guru.store.iter_facts() if f.provenance is Provenance.MEASURED}

    p.write_text("version: 1\ntables: [unclosed\n", encoding="utf-8")  # broken: keep all
    assert any(d.code == "SG208" for d in guru.load_overlays(p))
    assert "One row per order placed on the storefront." in guru.context("refund rate").text

    p.write_text(OVERLAY.replace("    description: One row per order placed on the storefront.\n",
                                 ""), encoding="utf-8")  # fmt: skip
    guru.load_overlays(p)
    text = guru.context("refund rate").text
    assert "One row per order placed on the storefront." not in text
    assert "live rows: (deleted IS NULL OR NOT deleted)" in text  # the kept entry stays
    assert not guru.store.facts_for([ORD], ["DESCRIPTION"])
    assert measured <= {f.id for f in guru.store.iter_facts()}

    p.unlink()
    guru.load_overlays(tmp_path / "nothing.yaml")  # every overlay gone: every overlay fact goes
    left = [f for f in guru.store.iter_facts() if (f.source or "").startswith("overlay:")]
    assert left == [] and measured <= {f.id for f in guru.store.iter_facts()}


# --- measurement, drafting, verification -------------------------------------------------------


def test_measure_targets_globs_and_all(guru: Guru) -> None:
    fake = guru.backend
    assert isinstance(fake, FakeBackend)
    r = guru.measure("shop.cust*")
    assert r.measured_tables == [CUS]
    with pytest.raises(SchemaGuruError, match="matches no table"):
        guru.measure("shop.zz*")
    r = guru.measure(all=True, budget=Budget(max_bytes_total=None, max_bytes_per_probe=None))
    assert set(r.measured_tables) >= {ORD, CUS}
    # eager order: the hot table (feedback) first
    order = guru._eager_order()
    assert order.index(ORD) < order.index(CUS)  # log(row count): 10,000 before 1,000
    products = ObjectRef(None, "shop", "products")
    for _ in range(6):  # query frequency from feedback outweighs size
        guru.report_error("SELECT x FROM shop.products", 'Referenced column "x" not found')
    assert guru._eager_order()[0] == products
    r = asyncio.run(guru.ameasure(CUS))
    assert r.measured_tables == [CUS]


def test_report_error_and_feedback_summary(guru: Guru) -> None:
    n = guru.report_error("SELECT nope FROM shop.orders", 'Referenced column "nope" not found')
    assert n >= 1
    assert guru.feedback_summary()[0]["table"] == "shop.orders"
    assert guru.report_error("", "") == 0  # never raises


def test_draft_verify_accept(guru: Guru) -> None:
    script = Script()
    report = guru.draft(["shop.orders"], provider=script.provider())
    assert report.drafted and all(f.provenance is Provenance.DRAFTED for f in report.drafted)
    assert asyncio.run(guru.adraft("shop.customers", provider=script.provider(), verify=False))
    desc = next(f for f in report.drafted if f.kind.name == "DESCRIPTION")
    accepted = guru.accept([desc.id])
    assert accepted[0].provenance is Provenance.HUMAN
    assert "One row per shop.orders record." in guru.context("orders").text


def test_verify_claims_and_exact(guru: Guru, monkeypatch: pytest.MonkeyPatch) -> None:
    from schemaguru.config import SamplingConfig
    from schemaguru.model.evidence import Evidence

    fake = guru.backend
    assert isinstance(fake, FakeBackend)
    monkeypatch.setattr(fake, "check", lambda claims: [Evidence(claim_text=claims, verdict="PASS")])
    ev = guru.verify("ROWS shop.orders VALUE >=1")
    assert ev[0].verdict == "PASS"
    guru.config = Config(sampling=SamplingConfig(measurement="force"))
    guru.measure(CUS)
    est = [
        f
        for f in guru.store.facts_for(None, ["NULL_RATE", "PRIMARY_KEY"])
        if isinstance(f.subject, ObjectRef)
        and f.subject.table_ref() == CUS
        and f.status is Status.ESTIMATED
    ]
    assert est
    out = guru.verify(est, exact=True)
    assert any(f.method == "exact" for f in out)
    assert guru.verify(est) == est  # not exact: measured facts come back unchanged


def test_refresh_and_export(guru: Guru, tmp_path: Path) -> None:
    report = asyncio.run(guru.arefresh())
    assert report.exit_code in (0, 1, 3)
    out = tmp_path / "export.json"
    guru.export(out)
    assert json.loads(out.read_text(encoding="utf-8"))
    assert asyncio.run(guru.acrawl()).tables_seen > 0


# --- agent surface (spec 18) ------------------------------------------------------------------


def test_tools_and_handle_tool_call(guru: Guru, tmp_path: Path) -> None:
    """The façade delegates to schemaguru.agent (tested in depth in tests/unit/agent)."""
    from schemaguru.agent import handle_tool_call, tool_definitions

    p = tmp_path / "o.yaml"
    p.write_text(OVERLAY, encoding="utf-8")
    guru.load_overlays(p)
    guru.measure(["shop.orders", "shop.customers"])
    assert guru.tools() == tool_definitions(guru)
    assert all(s["input_schema"]["type"] == "object" and s["description"] for s in guru.tools())
    out = guru.handle_tool_call("describe_table", {"table": "orders"})
    assert out == handle_tool_call(guru, "describe_table", {"table": "orders"})
    assert "[shop.orders]" in out["text"]
    vals = guru.handle_tool_call("column_values", {"column": "shop.orders.status"})
    assert "PAID" in vals["text"] and "refunded" in vals["text"]
    err = guru.handle_tool_call("nope", {})
    assert err["error"]["code"] == "SG803" and json.loads(json.dumps(err)) == err


def test_prompt_block(guru: Guru) -> None:
    block = guru.prompt_block()
    assert "search_tables" in block and "Absence of a warning is not verification." in block
    with_q = guru.prompt_block("orders per customer")
    assert "[shop.orders]" in with_q and with_q.startswith(block)


def test_async_context_matches_sync(guru: Guru) -> None:
    sync = guru.context("orders per customer").text
    assert asyncio.run(guru.acontext("orders per customer")).text == sync


# --- token counters --------------------------------------------------------------------------


def test_token_counters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert CharTokenCounter().count("abcdefgh") == 2
    fake = types.ModuleType("tiktoken")

    class _Enc:
        def encode(self, text: str, **kw: Any) -> list[int]:
            return list(range(len(text.split())))

    fake.get_encoding = lambda name: _Enc()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tiktoken", fake)
    assert TiktokenCounter().count("a b c") == 3
    g = Guru(store=str(tmp_path / "t.db"), config=Config(token_counter="tiktoken"))
    assert isinstance(g.counter, TiktokenCounter)
    g.close()
    monkeypatch.setitem(sys.modules, "tiktoken", None)
    with pytest.raises(ImportError, match="schemaguru\\[tiktoken\\]"):
        TiktokenCounter()


def test_connector_wiring(tmp_path: Path) -> None:
    from schemaguru.config import SamplingConfig

    class Stub:  # a FactLang connector shape: used as is
        dialect = "duckdb"
        paramstyle = "qmark"

        def execute(self, probe: Any, *, timeout_s: float | None) -> Any:
            raise AssertionError("not run")

    cfg = Config(sampling=SamplingConfig(measurement="force"))
    g = Guru(connector=Stub(), dialect="duckdb", store=str(tmp_path / "c.db"), config=cfg)
    assert isinstance(g.backend, flbridge.FactLangBackend)
    assert g.backend._sampling is cfg.sampling  # sampled measurement scales by the config
    if "run_metadata" not in flbridge.capabilities():
        assert g.reader is None and any(d.code == "SG105" for d in g.diagnostics)
    g.config = Config()
    assert g.backend._sampling == SamplingConfig()  # an auto-built backend follows the config
    g.close()


def _in_thread(fn: Any) -> list[BaseException]:
    """Run ``fn`` on another thread to completion; return what it raised."""
    import threading

    errors: list[BaseException] = []

    def body() -> None:
        try:
            fn()
        except BaseException as e:
            errors.append(e)

    t = threading.Thread(target=body)
    t.start()
    t.join(30)
    assert not t.is_alive()
    return errors


@pytest.mark.parametrize("when", ["before_reconfigure", "after_reconfigure"])
def test_config_switch_races_never_leave_a_drainer(
    tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts], when: str
) -> None:
    """Injected interleavings: a context() on another thread during a switch to queue mode."""
    conn = shop_duckdb[0].cursor()
    bg = Config(measure_on_demand="background")
    g = make_guru(
        tmp_path, shop_duckdb[0], backend=FakeBackend(conn), config=bg, drain_interval_s=5
    )
    g.crawl()
    g.context("orders per customer")
    assert g.drainer is not None and g.drainer.is_alive()
    sched = g._scheduler
    real = sched.reconfigure
    seen: list[list[BaseException]] = []

    def racing_reconfigure(config: Config, backend: Any = None) -> None:
        if when == "after_reconfigure":
            real(config, backend)
        # (a) a context() elsewhere queues a table and asks for a drainer;
        # (b) after the reconfigure, BackgroundDrainer would have raised ValueError
        seen.append(_in_thread(lambda: g._enqueue([ORD])))
        seen.append(_in_thread(g._ensure_drainer))
        if when == "before_reconfigure":
            real(config, backend)

    sched.reconfigure = racing_reconfigure  # type: ignore[method-assign]
    g.config = Config()  # queue mode
    assert seen == [[], []]  # nothing raised on the racing thread
    assert g.drainer is None  # no drainer survives the switch
    g._ensure_drainer()  # and none starts afterwards in queue mode
    assert g.drainer is None
    # back to background: a drainer may start again
    sched.reconfigure = real  # type: ignore[method-assign]
    g.config = bg
    g._enqueue([ORD])
    assert g.drainer is not None and g.drainer.is_alive()
    g.close()
    conn.close()


def test_close_waits_for_an_in_flight_block_run(
    tmp_path: Path, shop_duckdb: tuple[Any, ShopFacts]
) -> None:
    import threading

    entered, release = threading.Event(), threading.Event()
    fake = FakeBackend(shop_duckdb[0])
    real = fake.measure

    def slow(reqs: Any) -> Any:
        entered.set()
        assert release.wait(30)
        return real(reqs)

    g = make_guru(tmp_path, shop_duckdb[0], backend=fake, config=Config(measure_on_demand="block"))
    g.crawl()
    fake.measure = slow  # type: ignore[method-assign]
    views: list[Any] = []
    errors: list[BaseException] = []

    def ask() -> None:
        try:
            views.append(g.context("orders per customer"))
        except BaseException as e:
            errors.append(e)

    asker = threading.Thread(target=ask)
    asker.start()
    assert entered.wait(30)
    closer = threading.Thread(target=g.close)
    closer.start()
    closer.join(0.3)
    assert closer.is_alive()  # close() waits for the block run
    release.set()
    asker.join(30)
    closer.join(30)
    assert not closer.is_alive() and errors == [] and len(views) == 1
    assert "10,000 rows (measured" in views[0].text


def test_redact_text_judges_every_embedded_url() -> None:
    from schemaguru.catalog.executor import redact_text

    msg = "failed: mysql://u:pa@ss@h/db and postgresql://a:b@db:5432/x?password=q (retry)"
    assert redact_text(msg) == "failed: mysql://*** and postgresql://db:5432 (retry)"
    assert redact_text("no url here") == "no url here"
