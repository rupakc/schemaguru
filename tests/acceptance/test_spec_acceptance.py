"""Spec acceptance sweep (spec 2): one test per goal G1-G9 and per anti-goal.

Every test runs on the shop warehouse (``tests/fixtures/shop``, a DuckDB file) through
``Guru.connect`` with the REAL FactLang measurement backend: no fakes of the seam, no skips.
Each anti-goal test carries a negative control, so it is shown able to fail: the same check,
pointed at a case where the guarantee is switched off (raw values, a disabled hub guard, an
exact measurement, a planted write), does flag it.
"""

from __future__ import annotations

import json
import re
import socket
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import duckdb
import factlang
import pytest

import schemaguru as sg
from schemaguru.agent.adapters import anthropic as anthropic_adapter
from schemaguru.agent.mcp import resource_ref
from schemaguru.catalog import DBAPIExecutor, dialects, reader_for
from schemaguru.catalog.executor import check_metadata_sql
from schemaguru.config import Config, CrawlConfig, GraphConfig, SamplingConfig
from schemaguru.model import Provenance, Status
from schemaguru.pii import mask_value
from schemaguru.render.formats import _FORMATS, register_format
from tests.architecture.test_denylist import find_violations, parse_denylist, tracked_files
from tests.fixtures.shop import ShopFacts, build_shop
from tests.unit.draft.conftest import Script, table_reply

TABLES = 14  # every table build_shop creates (big_t is built only by build_big)
QUESTIONS = [
    "monthly revenue by customer segment",
    "refund rate by month",
    "orders per customer",
    "events per account and orders per account",
    "payments matched to orders",
    "products in order items",
    "sessions with events",
]
ROOT = Path(__file__).resolve().parent.parent.parent


# ----- fixtures -------------------------------------------------------------------------------


def url_of(path: Path) -> str:
    """``duckdb:///`` + an absolute path (four slashes on POSIX, ``duckdb:///C:/…`` on Windows)."""
    return f"duckdb:///{path.resolve().as_posix()}"


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, ShopFacts]:
    path = tmp_path_factory.mktemp("acceptance") / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    facts = build_shop(conn, "duckdb")
    conn.close()
    return path, facts


def connect(warehouse: tuple[Path, ShopFacts], store: Path, **kw: Any) -> sg.Guru:
    return sg.Guru.connect(url_of(warehouse[0]), store=str(store), **kw)


@pytest.fixture(scope="module")
def measured(
    warehouse: tuple[Path, ShopFacts], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[sg.Guru]:
    """One Guru over the whole shop, measured exactly by the real FactLang (read-only use)."""
    guru = connect(warehouse, tmp_path_factory.mktemp("measured") / "store.db")
    report = guru.measure(all=True)
    assert not report.errors and len(report.measured_tables) == TABLES
    assert not report.sampled_tables  # small tables are measured exactly (spec 8.6)
    yield guru
    guru.close()


def blocks(text: str) -> dict[str, str]:
    """Table blocks of a rendered view, keyed by ``schema.table``."""
    out: dict[str, str] = {}
    for part in text.split("\n\n"):
        m = re.match(r"\[([^\]]+)\]", part)
        if m:
            out[m.group(1)] = part
    return out


_LABEL = re.compile(
    r"(measured|verified) \d{4}-\d\d-\d\d|catalog stats|\((declared|human)|\(draft\)|sampled"
)


# a figure: digits standing alone (``1,000``, ``5%``, ``0.98``), not part of a name (``ISO-8601``)
_FIGURE = re.compile(r"(?<![\w-])\d[\d,.]*%?(?![\w-])")


def unlabelled_numbers(block: str) -> list[str]:
    """Clauses of a table block that state a number without saying how it is known (G6)."""
    return [c for c in figure_clauses(block) if not _LABEL.search(c)]


def figure_clauses(block: str) -> list[str]:
    """Clauses of a table block (row count, column notes) that state a figure."""
    out: list[str] = []
    in_columns = False
    for line in block.splitlines()[1:]:
        s = line.strip()
        if s == "Columns:":
            in_columns = True
            continue
        if s.startswith("Provenance:"):
            in_columns = False
            continue
        if in_columns:
            parts = re.split(r"\s{2,}", s, maxsplit=2)  # name, type, what is known
            clauses = parts[2].split("; ") if len(parts) == 3 else []
        elif re.match(r"[\d,]+ rows", s):
            clauses = [s]
        else:
            continue
        out += [c for c in clauses if _FIGURE.search(c)]
    return out


# ----- goals ----------------------------------------------------------------------------------


def test_g1_three_lines_from_install_to_context(
    warehouse: tuple[Path, ShopFacts], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G1: the quickstart, verbatim in shape, with no config file and no store argument."""
    monkeypatch.chdir(tmp_path)
    assert not list(tmp_path.iterdir())  # no schemaguru.yaml, nothing prepared
    guru = sg.Guru.connect(url_of(warehouse[0]))
    view = guru.context("monthly revenue by customer segment")
    guru.close()
    assert str(view) == view.text
    assert "[shop.customers]" in view.text and "segment" in view.text
    assert (tmp_path / ".schemaguru" / "default.db").is_file()  # the default store


def test_g2_integrates_existing_connections_and_emits_every_shape(
    warehouse: tuple[Path, ShopFacts], tmp_path: Path
) -> None:
    """G2: an existing DB-API connection or a plain execute callable (wrapped in FactLang's
    connectors, spec 19), and output as text, JSON, markdown, tool specs and MCP resources."""
    conn = duckdb.connect(str(warehouse[0]), read_only=True)

    def execute(sql: str, params: Any) -> list[dict[str, Any]]:
        cur = conn.cursor()  # FactLang may run probes in parallel: one cursor per call
        cur.execute(sql, list(params) if params else None)
        names = [d[0] for d in cur.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    connectors = {
        "dbapi": factlang.DBAPIConnector(conn, "duckdb"),
        "callable": factlang.CallableConnector(execute, "duckdb"),
    }
    try:
        for name, connector in connectors.items():
            guru = sg.Guru(connector=connector, store=str(tmp_path / f"{name}.db"))
            assert guru.crawl().tables_seen == TABLES, name
            assert not guru.measure("shop.orders").errors, name
            text = guru.context("orders by status").text
            assert "10,000 rows (measured" in blocks(text)["shop.orders"], name
            guru.close()
    finally:
        conn.close()

    guru = sg.Guru(connector=connectors["dbapi"], store=str(tmp_path / "dbapi.db"))
    view = guru.context("orders by status")
    as_json = json.loads(str(guru.context("orders by status", format="json")))
    assert as_json and "shop.orders" in json.dumps(as_json)
    assert "shop.orders" in str(guru.context("orders by status", format="markdown"))
    specs = guru.tools()
    assert all({"name", "description", "input_schema"} <= set(s) for s in specs)
    result = guru.handle_tool_call("schema_context", {"question": "orders by status"})
    assert json.loads(json.dumps(result))["text"] == view.text
    assert resource_ref("schema://tables/shop.orders") == "shop.orders"  # MCP resource URIs
    guru.close()


def test_g3_extension_points_plug_in(
    warehouse: tuple[Path, ShopFacts], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3: a new warehouse reader, a retriever, a token counter and an output format are
    plugged in through their documented points, and the pipeline uses each of them."""
    from schemaguru.catalog import registry

    monkeypatch.setattr(registry, "_FACTORIES", dict(registry._FACTORIES))
    monkeypatch.setattr("schemaguru.render.formats._FORMATS", dict(_FORMATS))
    built: list[str] = []

    def toy_reader(executor: Any) -> Any:
        built.append("toy")
        return registry._FACTORIES["duckdb"](executor)

    sg.catalog.register_reader("toyshop", toy_reader)

    class Fixed:
        def __init__(self) -> None:
            self.questions: list[str] = []

        def search(self, question: str, k: int) -> list[tuple[sg.ObjectRef, float]]:
            self.questions.append(question)
            return [(sg.ObjectRef(None, "shop", "payments"), 1.0)]

    class Words:
        def count(self, text: str) -> int:
            return len(text.split())

    register_format("shout", lambda v: v.text.upper())
    conn = duckdb.connect(str(warehouse[0]), read_only=True)
    retriever = Fixed()
    guru = sg.Guru(
        reader=reader_for("toyshop", DBAPIExecutor(conn)),
        store=str(tmp_path / "ext.db"),
        dialect="duckdb",
        retriever=retriever,
        token_counter=Words(),
    )
    assert built == ["toy"] and guru.crawl().tables_seen == TABLES
    view = guru.context("anything at all")
    assert retriever.questions == ["anything at all"]
    assert list(blocks(view.text)) == ["shop.payments"]
    assert view.tokens == len(view.text.split())  # the plugged-in counter
    assert str(guru.context("anything at all", format="shout")) == view.text.upper()
    guru.close()
    conn.close()


def test_g4_every_listed_warehouse_has_a_reader() -> None:
    """G4: the owner-approved warehouses plus MySQL/MariaDB each have a registered reader."""
    listed = {
        "bigquery", "snowflake", "redshift", "databricks", "postgres", "duckdb",
        "clickhouse", "tsql", "oracle", "trino", "mysql",
    }  # fmt: skip
    assert listed <= set(dialects())

    class Nothing:
        def run(self, sql: str, params: Any = (), *, timeout_s: float | None = None) -> list[Any]:
            return []

    for d in sorted(listed):
        reader = reader_for(d, Nothing())
        assert isinstance(reader.capabilities(), sg.catalog.ReaderCapabilities), d


def test_g5_crawl_is_metadata_only_paged_resumable_and_context_is_offline(
    warehouse: tuple[Path, ShopFacts], tmp_path: Path
) -> None:
    """G5: the crawl reads system catalogs only, in pages, and resumes from its checkpoint
    after an interruption; serving a view touches the store alone, never the warehouse.
    (The 500k-table latency target is the ``benchmark``-marked search test.)"""
    conn = duckdb.connect(str(warehouse[0]), read_only=True)

    class Recorder:
        def __init__(self) -> None:
            self.inner = DBAPIExecutor(conn)
            self.calls: list[tuple[str, tuple[Any, ...]]] = []
            self.interrupt_on_page: int | None = None

        def run(self, sql: str, params: Any = (), *, timeout_s: float | None = None) -> Any:
            self.calls.append((sql, tuple(params)))
            pages = sum(1 for s, p in self.calls if "estimated_size" in s and "shop" in p)
            if self.interrupt_on_page is not None and pages == self.interrupt_on_page:
                raise KeyboardInterrupt  # the process dies mid-crawl
            return self.inner.run(sql, params, timeout_s=timeout_s)

    rec = Recorder()
    rec.interrupt_on_page = 3
    cfg = Config(crawl=CrawlConfig(page_size=4, workers=1))
    guru = sg.Guru(
        reader=reader_for("duckdb", rec),
        store=str(tmp_path / "g5.db"),
        dialect="duckdb",
        config=cfg,
    )
    with pytest.raises(KeyboardInterrupt):
        guru.crawl()
    checkpointed = len(list(guru.store.iter_tables()))
    assert 0 < checkpointed < TABLES  # pages before the interruption were written
    rec.interrupt_on_page = None
    rec.calls.clear()
    assert guru.crawl().tables_seen == TABLES - checkpointed  # resumed, not restarted
    assert len(list(guru.store.iter_tables())) == TABLES
    first_page = next(p for s, p in rec.calls if "estimated_size" in s)
    assert any(isinstance(x, str) and x.startswith(("o", "p", "r", "s")) for x in first_page)
    for sql, _ in rec.calls:
        check_metadata_sql(sql)  # a single catalog SELECT
        assert not re.search(r'\bshop"?\s*\.\s*"?\w', sql), sql  # never a data table
    rec.calls.clear()
    for q in QUESTIONS:
        guru.context(q)
    assert rec.calls == []  # the query path never reaches the warehouse
    guru.close()
    conn.close()


def test_g6_every_shown_fact_states_its_provenance(
    measured: sg.Guru, warehouse: tuple[Path, ShopFacts], tmp_path: Path
) -> None:
    """G6: every number shown says how it is known (measured + date, catalog, declared, human,
    draft, sampled); drafts say draft. Negative control: an unlabelled number is flagged."""
    for q in QUESTIONS:
        for table, block in blocks(measured.context(q).text).items():
            assert unlabelled_numbers(block) == [], (q, table)
            assert "Provenance:" in block, (q, table)
    assert unlabelled_numbers("[t]\n  Columns:\n    c  INTEGER  null 10%") == ["null 10%"]
    assert unlabelled_numbers("[t]\n  1,000 rows") == ["1,000 rows"]

    script = Script(
        lambda tables, _: {
            "tables": [table_reply(t["table"], description="Holds one thing.") for t in tables]
        }
    )
    guru = connect(warehouse, tmp_path / "drafted.db")
    guru.measure("shop.products")
    assert guru.draft(["shop.products"], provider=script.provider()).drafted
    text = blocks(guru.context("products in order items").text)["shop.products"]
    line = next(ln for ln in text.splitlines() if "Holds one thing." in ln)
    assert "(draft)" in line and "verified" not in line
    guru.close()


def test_g7_one_bad_item_costs_only_that_item(
    warehouse: tuple[Path, ShopFacts], tmp_path: Path
) -> None:
    """G7: a failing crawl page, a failing probe, a broken overlay file, a malformed overlay
    entry and an invalid LLM draft each cost that item; nothing raises, the rest goes on."""
    conn = duckdb.connect(str(warehouse[0]), read_only=True)

    class FlakyPages:
        def __init__(self) -> None:
            self.inner = DBAPIExecutor(conn)
            self.pages = 0

        def run(self, sql: str, params: Any = (), *, timeout_s: float | None = None) -> Any:
            if "estimated_size" in sql and "shop" in tuple(params):
                self.pages += 1
                if self.pages == 2:
                    raise RuntimeError("page lost")
            return self.inner.run(sql, params, timeout_s=timeout_s)

    cfg = Config(crawl=CrawlConfig(page_size=4, workers=1))
    store = tmp_path / "g7.db"
    guru = sg.Guru(reader=reader_for("duckdb", FlakyPages()), store=str(store), config=cfg)
    report = guru.crawl()
    assert [d.code for d in report.errors] == ["SG102"]
    assert 0 < report.tables_seen < TABLES  # the other pages were crawled
    guru.close()
    conn.close()

    guru = connect(warehouse, store, crawl=True)  # a clean crawl fills the gap
    assert len(list(guru.store.iter_tables())) == TABLES
    inner = guru.backend

    class OneProbeFails:
        def capabilities(self) -> frozenset[str]:
            return inner.capabilities()

        def measure(self, reqs: Any) -> Any:
            if any("payments" in repr(r) for r in reqs):
                raise RuntimeError("probe failed")
            return inner.measure(reqs)

        def estimate(self, reqs: Any) -> Any:
            return inner.estimate(reqs)

        def check(self, claims: str) -> Any:
            return inner.check(claims)

    guru.backend = OneProbeFails()
    guru._scheduler.reconfigure(guru.config, guru.backend)
    measured = guru.measure(["shop.payments", "shop.customers"])
    assert {d.code for d in measured.errors} == {"SG303"}
    assert sg.ObjectRef(None, "shop", "customers") in measured.measured_tables
    assert "1,000 rows (measured" in blocks(guru.context("customers").text)["shop.customers"]

    good = tmp_path / "good.yaml"
    good.write_text(
        "version: 1\ntables:\n  shop.customers:\n    description: One row per shopper.\n"
        "  shop.nowhere:\n    description: x\n  shop.orders:\n    colour: blue\n",
        encoding="utf-8",
    )
    broken = tmp_path / "broken.yaml"
    broken.write_text("version: 1\ntables: [unclosed\n", encoding="utf-8")
    diags = guru.load_overlays(good, broken)
    codes = {d.code for d in diags}
    assert {"SG203", "SG202", "SG208"} <= codes, codes
    assert "One row per shopper." in guru.context("customers").text

    def reply(tables: list[dict[str, Any]], _: int) -> dict[str, Any]:
        out = [table_reply(t["table"]) for t in tables]
        for r in out:
            if r["table"].endswith("products"):
                r["columns"] = [{"name": "no_such_column", "description": "x"}]
        return {"tables": out}

    drafted = guru.draft(["shop.products", "shop.accounts"], provider=Script(reply).provider())
    assert {d.code for d in drafted.diagnostics} >= {"SG401"}
    assert [f.subject.table for f in drafted.errors] == ["products"]
    assert {f.subject.table_ref().table for f in drafted.drafted} == {"accounts"}
    guru.close()


def test_g8_agent_native_surfaces(measured: sg.Guru) -> None:
    """G8: Python API, tool definitions and dispatch, the prompt block, the MCP resource URI
    scheme and the Anthropic adapter (the SDK-free parts; the other adapters and the MCP SDK
    session are covered in tests/unit/agent, where their extras are installed)."""
    names = [t["name"] for t in measured.tools()]
    assert names == [
        "schema_context", "search_tables", "describe_table", "find_join_path",
        "column_values", "explain_term",
    ]  # fmt: skip
    calls = {
        "schema_context": {"question": "orders per customer"},
        "search_tables": {"query": "payments"},
        "describe_table": {"table": "shop.orders"},
        "find_join_path": {"from": "shop.orders", "to": "shop.customers"},
        "column_values": {"column": "shop.customers.segment"},
        "explain_term": {"term": "refund rate"},
    }
    for name, args in calls.items():
        result = measured.handle_tool_call(name, args)
        assert isinstance(result["text"], str) and json.loads(json.dumps(result)) == result, name
    assert "error" not in measured.handle_tool_call("describe_table", calls["describe_table"])
    refused = measured.handle_tool_call("check_fact", {"claims": "ROWS shop.orders VALUE >=1"})
    assert refused["error"]["code"] == "SG803"  # agent_checks defaults to "deny"
    block = measured.prompt_block("orders per customer")
    assert "Absence of a warning is not verification." in block and "[shop.orders]" in block
    assert anthropic_adapter.tools(measured) == measured.tools()
    use = {"type": "tool_use", "id": "t1", "name": "describe_table", "input": {"table": "orders"}}
    result = anthropic_adapter.run_tool_use(measured, use)
    assert result["tool_use_id"] == "t1" and not result["is_error"]
    assert "[shop.orders]" in result["content"]
    assert resource_ref("schema://tables/shop.orders?x=1") == "shop.orders"


def test_g9_everything_but_drafting_runs_with_zero_llm_calls(
    warehouse: tuple[Path, ShopFacts], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G9: discovery, measurement, overlays, graph, scoping, rendering, refresh and the tool
    surface with no LLM SDK importable and the drafting entry point booby-trapped.
    Negative control: drafting is caught by the trap."""
    import schemaguru.guru as guru_module

    for sdk in ("anthropic", "openai"):
        monkeypatch.setitem(sys.modules, sdk, None)  # importing it now raises ImportError
    llm_calls: list[object] = []
    real_draft = guru_module.draft_tables

    def trap(*args: Any, **kw: Any) -> Any:
        llm_calls.append(args)
        return real_draft(*args, **kw)

    monkeypatch.setattr(guru_module, "draft_tables", trap)
    guru = connect(warehouse, tmp_path / "g9.db")
    assert not guru.measure(all=True).errors
    guru.load_overlays(ROOT / "tests" / "fixtures" / "overlays" / "spec_example.yaml")
    assert guru.find_path("shop.payments", "shop.customers").paths
    view = guru.context("refund rate by month for orders")
    assert "shop.orders" in blocks(view.text)
    assert not [b for b in blocks(view.text).values() if "(draft)" in b]
    assert guru.refresh().snapshot_id
    for q in QUESTIONS:
        guru.handle_tool_call("schema_context", {"question": q})
    assert llm_calls == []
    guru.draft(["shop.accounts"], provider=Script().provider())  # the control
    assert len(llm_calls) == 1
    guru.close()


# ----- anti-goals -----------------------------------------------------------------------------


def test_no_unmeasured_number_shown_as_measured_and_no_draft_as_verified(
    warehouse: tuple[Path, ShopFacts], tmp_path: Path
) -> None:
    """Anti-goal 1: before measuring, nothing claims to be measured; a draft is never shown as
    verified. Negative control: after measuring, the same check sees "measured"."""
    guru = connect(warehouse, tmp_path / "a1.db")
    text = guru.context("orders per customer").text
    assert "[shop.orders]" in text
    assert "unmeasured: nothing here has been measured yet" in text
    assert re.search(r"(measured|verified) \d{4}-", text) is None
    report = guru.draft(["shop.orders"], provider=Script().provider())
    assert report.drafted and all(f.status is not Status.VERIFIED for f in report.drafted)
    drafted = guru.context("orders per customer").text
    assert "(draft)" in drafted and "verified" not in drafted.replace("never verified", "")
    guru.measure("shop.orders")
    assert re.search(r"measured \d{4}-", guru.context("orders per customer").text)  # control
    guru.close()


def test_row_multiplying_paths_always_carry_a_warning(
    measured: sg.Guru, warehouse: tuple[Path, ShopFacts], tmp_path: Path
) -> None:
    """Anti-goal 2: a route through the accounts hub (two attribution edges: a cartesian
    blow-up) is refused with a warning; every hop that is shown states its cardinality.
    Negative control: with the hub guard switched off the same route is returned."""
    res = measured.find_path("shop.orders", "shop.events")
    assert not res.paths and res.refusal is sg.Refusal.HUB_PIVOT
    assert "cartesian hazard" in res.explanation
    text = measured.context("events per account and orders per account").text
    assert "⚠ no safe route" in text and "cartesian hazard" in text
    hops = [ln for ln in text.splitlines() if re.match(r"\s{4}\w+\.\w+ = \w+\.\w+", ln)]
    assert hops
    for line in hops:
        assert re.search(r"\((1:1|N:1|1:N|N:M)\)", line), line  # cardinality stated

    off = connect(
        warehouse, tmp_path / "noguard.db", config=Config(graph=GraphConfig(attribution_fanin=1e9))
    )
    off.measure(all=True)
    route = off.find_path("shop.orders", "shop.events")
    assert route.paths and sg.ObjectRef(None, "shop", "accounts") in route.paths[0].tables
    off.close()


def test_no_unmasked_value_reaches_llm_by_default(
    warehouse: tuple[Path, ShopFacts], tmp_path: Path
) -> None:
    """Anti-goal 3: the drafting payload carries personal-looking values only as shape tokens
    by default (masked mode masks values that look personal). Negative control: with the
    explicit raw opt-in, the same check finds the raw values."""
    with duckdb.connect(str(warehouse[0]), read_only=True) as conn:
        rows = conn.execute("SELECT DISTINCT kind FROM shop.anon_enum_t").fetchall()
    planted = [r[0] for r in rows]
    assert planted and all(mask_value(v)[1] is not None for v in planted)  # email-shaped

    def payload(cfg: Config, name: str) -> str:
        guru = connect(warehouse, tmp_path / f"{name}.db", config=cfg)
        guru.measure("shop.anon_enum_t")
        script = Script()
        guru.draft(["shop.anon_enum_t"], provider=script.provider())
        guru.close()
        return "\n".join(c["system"] + c["user"] for c in script.calls)

    default = payload(Config(), "masked")
    assert "<email>" in default
    assert not [v for v in planted if v in default]
    raw = SamplingConfig(llm_values="raw", store_raw_samples=True)
    opted = payload(Config(sampling=raw), "raw")
    assert [v for v in planted if v in opted]  # the check can fail


def test_no_writes_on_query_path(measured: sg.Guru, monkeypatch: pytest.MonkeyPatch) -> None:
    """Anti-goal 4: 100 context() calls (plus every read-only tool and the prompt block) leave
    the store's write counter at zero. Negative control: a feedback report is counted."""
    writes: list[str] = []
    store = measured.store
    for name in (
        "upsert_schemas", "upsert_tables", "upsert_columns", "upsert_constraints",
        "mark_withdrawn", "put_facts", "snapshot", "crawl_state_set", "ledger_add",
        "queue_push", "feedback_add", "delete_facts",
    ):  # fmt: skip
        method = getattr(store, name, None)
        if method is None:
            continue

        def counted(*a: Any, _n: str = name, _m: Callable[..., Any] = method, **k: Any) -> Any:
            writes.append(_n)
            return _m(*a, **k)

        monkeypatch.setattr(store, name, counted)
    before = sorted(f.id for f in store.iter_facts())
    for i in range(100):
        measured.context(f"{QUESTIONS[i % len(QUESTIONS)]} #{i}")  # distinct: no view cache
    for name, args in {
        "search_tables": {"query": "orders"},
        "describe_table": {"table": "shop.orders"},
        "find_join_path": {"from": "shop.payments", "to": "shop.customers"},
        "column_values": {"column": "shop.orders.status"},
        "explain_term": {"term": "refund"},
    }.items():
        measured.handle_tool_call(name, args)
    measured.prompt_block("orders per customer")
    measured.find_path("shop.orders", "shop.customers")
    assert writes == []
    assert sorted(f.id for f in store.iter_facts()) == before
    measured.report_error("SELECT nope FROM shop.orders", 'column "nope" does not exist')
    assert "feedback_add" in writes  # the control: a write is seen


def test_no_full_dump_by_default(measured: sg.Guru) -> None:
    """Anti-goal 5: a question gets its scoped slice, never every table; no question, no
    tables. Negative control: the counter sees every table in an explicit full listing."""
    every = {t.ref.canonical() for t in measured.store.iter_tables()}
    assert len(every) == TABLES
    for q in QUESTIONS:
        shown = set(blocks(measured.context(q).text))
        assert shown < every and len(shown) <= 8, (q, shown)
    assert blocks(measured.context("orders per customer").text)
    assert blocks(measured.prompt_block()) == {}
    assert blocks(measured.context("weather forecast for the weekend").text) == {}
    dump = "\n\n".join(
        measured.handle_tool_call("describe_table", {"table": t})["text"] for t in sorted(every)
    )
    assert set(blocks(dump)) == every  # the control


def test_no_running_service_required(
    warehouse: tuple[Path, ShopFacts], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anti-goal 6: connect, crawl, measure, overlays, context, paths and tools work with every
    network connection refused (in-process engine, file store). Negative control: the block
    does refuse a connection attempt."""
    attempts: list[object] = []

    def refuse(*args: Any, **kw: Any) -> Any:
        attempts.append(args)
        raise ConnectionRefusedError("network disabled by the acceptance test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    guru = connect(warehouse, tmp_path / "offline.db")
    assert not guru.measure(all=True).errors
    guru.load_overlays(ROOT / "tests" / "fixtures" / "overlays" / "spec_example.yaml")
    assert "[shop.orders]" in guru.context("refund rate by month for orders").text
    assert guru.find_path("shop.payments", "shop.customers").paths
    assert "error" not in guru.handle_tool_call("describe_table", {"table": "shop.orders"})
    guru.close()
    assert attempts == []
    with pytest.raises(ConnectionRefusedError):  # the control
        socket.create_connection(("127.0.0.1", 9))
    assert len(attempts) == 1


def test_no_originating_names_anywhere(tmp_path: Path) -> None:
    """Anti-goal 7: the repository holds no denylisted name. The list itself is never committed
    (spec 22): it comes from SCHEMAGURU_DENYLIST (CI secret) or SCHEMAGURU_DENYLIST_FILE.
    Negative control: a planted term in a scanned file is found, in any spelling."""
    import os

    raw = os.environ.get("SCHEMAGURU_DENYLIST", "")
    file = os.environ.get("SCHEMAGURU_DENYLIST_FILE")
    if file:
        raw += "\n" + Path(file).read_text(encoding="utf-8")
    terms = parse_denylist(raw)
    assert find_violations(tracked_files(ROOT), terms, ROOT) == []
    planted = tmp_path / "planted.txt"
    planted.write_text("see the QuuxZorb_Analytics notes\n", encoding="utf-8")
    assert find_violations([planted], ["quuxzorb analytics"], tmp_path)  # the control


def test_sampled_marked_everywhere(warehouse: tuple[Path, ShopFacts], tmp_path: Path) -> None:
    """A sampled measurement is never VERIFIED and is marked sampled in facts, views, tool
    results, paths and the export. Negative control: exact measurements fail the view check."""
    cfg = Config(sampling=SamplingConfig(measurement="force"))
    guru = connect(warehouse, tmp_path / "sampled.db", config=cfg)
    report = guru.measure(all=True)
    assert len(report.sampled_tables) == TABLES and not report.errors
    facts = [f for f in guru.store.iter_facts() if f.provenance is Provenance.MEASURED]
    sampled = [f for f in facts if (f.method or "").startswith("sampled:")]
    assert {f.subject.table for f in sampled if isinstance(f.subject, sg.ObjectRef)} >= {
        "orders", "customers", "order_items", "payments"
    }  # fmt: skip
    for f in facts:
        assert f.status is not Status.VERIFIED, f
        if f in sampled:  # an estimate, whatever its kind
            assert f.status not in (Status.OBSERVED, Status.VERIFIED), f
        else:  # exact where no key-hash sample applies (a table without a key), said so
            assert f.method == "exact" or f.status is Status.ERROR, f

    def unmarked(text: str) -> list[str]:
        out: list[str] = []
        for table, block in blocks(text).items():
            measured_here = "Provenance: catalog only" not in block  # a 0-row sample: nothing
            header = "sampled table: ≈ and ≥ figures are estimates from a sample"
            if measured_here and header not in block:
                out.append(f"{table}: no sampled header")
            out += [
                f"{table}: {c}"
                for c in figure_clauses(block)
                if not re.search(r"sampled|catalog stats|\(declared|\(human", c)
            ]
        return out

    for q in QUESTIONS:
        text = guru.context(q).text
        assert unmarked(text) == [], q
        for line in text.splitlines():
            if line.startswith("  shop.") and "· support" in line:
                assert "· sampled" in line, line
    described = guru.handle_tool_call("describe_table", {"table": "shop.orders"})["text"]
    assert unmarked(described) == []
    values = guru.handle_tool_call("column_values", {"column": "shop.orders.status"})
    assert "sampled" in values["text"]
    assert all(e["status"] == "ESTIMATED" for e in values["entries"])
    assert all(e["method"].startswith("sampled:") for e in values["entries"])
    path = guru.find_path("shop.order_items", "shop.orders")
    assert path.paths and all(p.estimated for p in path.paths)
    guru.export(tmp_path / "export.json")
    exported = json.loads((tmp_path / "export.json").read_text(encoding="utf-8"))
    assert "sampled:" in json.dumps(exported)
    guru.close()

    exact = connect(warehouse, tmp_path / "exact.db")
    exact.measure(all=True)
    assert unmarked(exact.context("orders per customer").text)  # the control
    exact.close()
