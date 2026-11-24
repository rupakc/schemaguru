"""The ``Guru`` façade (spec 2 G1/G2/G9, 8.2, 12, 19): wiring, caching, laziness, tools."""

from __future__ import annotations

import asyncio
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
    assert conn.dialect == "duckdb" and conn.connector is None
    assert conn.executor.run("SELECT 1 AS one") == [{"one": 1}]
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
    ("url", "secrets"),
    [
        ("postgresql://alice:p/ss@db/x", ["alice", "p/ss"]),
        ("mysql://u:pa@ss@h/db", ["pa@ss", "u:pa", "ss@h"]),
        ("snowflake://acct/db?user=a&password=hunter2", ["hunter2"]),
        (
            "snowflake://acct/db?PWD=s3&Token=t0k&private_key=pk&warehouse=wh#frag",
            ["s3", "t0k", "pk", "frag"],
        ),
    ],
)
def test_redact_removes_credentials(url: str, secrets: list[str]) -> None:
    from schemaguru.catalog.executor import _redact

    out = _redact(url)
    for secret in secrets:
        assert secret not in out, (url, out)
    with pytest.raises(SchemaGuruError) as exc:
        connect_url(url.replace(url.split("://")[0], "nosuchdb", 1))
    for secret in secrets:
        assert secret not in str(exc.value)


def test_redact_keeps_urls_without_credentials() -> None:
    from schemaguru.catalog.executor import _redact

    for url in (
        "duckdb:///shop.duckdb",
        "postgresql://db:5432/x?sslmode=require",
        "duckdb:///:memory:",
    ):
        assert _redact(url) == url
    assert "warehouse=wh" in _redact("snowflake://acct/db?warehouse=wh&password=x")
    assert "h/db" in _redact("mysql://u:pa@ss@h/db")


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
    p = tmp_path / "o.yaml"
    p.write_text(OVERLAY, encoding="utf-8")
    guru.load_overlays(p)
    guru.measure(["shop.orders", "shop.customers"])
    specs = guru.tools()
    names = [s["name"] for s in specs]
    assert names == [
        "schema_context",
        "search_tables",
        "describe_table",
        "find_join_path",
        "column_values",
        "check_fact",
        "explain_term",
    ]
    assert all(s["input_schema"]["type"] == "object" and s["description"] for s in specs)

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        out = guru.handle_tool_call(name, args)
        json.dumps(out)  # JSON-serialisable
        assert isinstance(out["text"], str)
        return out

    assert "[shop.orders]" in call("schema_context", {"question": "refund rate by month"})["text"]
    hits = call("search_tables", {"query": "orders", "k": 3})
    assert hits["tables"][0]["table"] == "shop.orders"
    assert "One row per order placed" in hits["tables"][0]["description"]
    assert "[shop.orders]" in call("describe_table", {"table": "orders"})["text"]
    path = call("find_join_path", {"from": "orders", "to": "customers"})
    assert path["refusal"] is None and "shop.orders" in path["text"]
    vals = call("column_values", {"column": "shop.orders.status"})
    assert "PAID" in vals["text"] and "refunded" in vals["text"]
    assert call("check_fact", {"claim": "ROWS shop.orders VALUE >=1"})["evidence"] == []
    term = call("explain_term", {"term": "Refund Rate"})
    assert "refunded orders over all orders" in term["text"]
    assert call("explain_term", {"term": "nothing"})["entries"] == []
    for name, args in [
        ("nope", {}),
        ("describe_table", {}),
        ("describe_table", {"table": "nowhere"}),
        ("search_tables", {"query": "x", "k": "many"}),
    ]:
        err = call(name, args)
        assert err["error"] and err["code"] == "SG803"


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

    cfg = Config(sampling=SamplingConfig(measurement="force"))
    g = Guru(connector=object(), dialect="duckdb", store=str(tmp_path / "c.db"), config=cfg)
    assert isinstance(g.backend, flbridge.FactLangBackend)
    assert g.backend._sampling is cfg.sampling  # sampled measurement scales by the config
    if "run_metadata" not in flbridge.capabilities():
        assert g.reader is None and any(d.code == "SG105" for d in g.diagnostics)
    g.config = Config()
    assert g.backend._sampling == SamplingConfig()  # an auto-built backend follows the config
    g.close()
