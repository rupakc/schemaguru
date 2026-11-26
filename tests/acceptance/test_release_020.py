"""Release 0.2.0 acceptance sweep: one test per 0.2.0 goal (L1-L4, H1, S1) and anti-goal.

Lineage tests need the ``lineage`` extra (sqlglot); under ``SCHEMAGURU_FORBID_SKIPS=1`` a skip
fails, so a job that claims the extra cannot silently lose them. Nothing here runs the guard or
executor suites (the gate does): the guard test only checks they exist and are collected.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import pytest

import schemaguru as sg
from schemaguru.catalog import DBAPIExecutor
from schemaguru.catalog.crawl import crawl
from schemaguru.catalog.readers.duckdb import DuckDBReader
from schemaguru.graph.search import Refusal
from schemaguru.guru import Guru
from schemaguru.lineage import lineage_available
from schemaguru.lineage.worker import ParserPool
from schemaguru.model.evidence import Evidence
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.lineage import table_lineage_value
from schemaguru.model.refs import ObjectRef
from schemaguru.store.sqlite import SQLiteStore
from tests.conformance.test_hardening_ledger import ITEM_IDS, parse_ledger
from tests.unit.catalog.fake_reader import ViewFakeReader
from tests.unit.lineage_helpers import alpha_beta_guru, privacy_warehouse

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
LEDGER = ROOT / "docs" / "specs" / "2026-11-25-schemaguru-0.2.0-hardening-ledger.md"
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
needs_parser = pytest.mark.skipif(not lineage_available(), reason="needs the lineage extra")


def _changelog_section(heading: str) -> str:
    m = re.search(r"^## \[0\.2\.0\][^\n]*\n(.*?)(?=^## \[|\Z)", CHANGELOG, re.M | re.S)
    assert m is not None
    sub = re.search(rf"^### {heading}\n(.*?)(?=^### |\Z)", m.group(1), re.M | re.S)
    assert sub is not None, heading
    return sub.group(1)


def _lineage_warehouse(tmp_path: Path) -> Guru:
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    conn.execute("CREATE SCHEMA shop")
    conn.execute("CREATE SCHEMA analytics")
    cols = {
        "orders": "order_id BIGINT, customer_id BIGINT, amount DOUBLE",
        "customers": "customer_id BIGINT",
        "payments": "payment_id BIGINT",
        "events": "event_id BIGINT",
        "sessions": "session_id BIGINT",
        "refunds": "order_id BIGINT, refund_amount DOUBLE, amount DOUBLE",
        "revenue": "customer_id BIGINT, net_revenue DOUBLE",
    }
    for name, ddl in cols.items():
        conn.execute(f"CREATE TABLE shop.{name}({ddl})")
    conn.execute("CREATE TABLE analytics.revenue_daily(day DATE, net_revenue DOUBLE)")
    conn.execute("CREATE TABLE analytics.legacy_view(order_id BIGINT)")
    conn.execute("CREATE VIEW shop.v_orders AS SELECT order_id, amount FROM shop.orders")
    guru = Guru(
        reader=DuckDBReader(DBAPIExecutor(conn)), store=str(tmp_path / "g.db"), dialect="duckdb"
    )
    guru.crawl()
    guru.import_dbt(FIXTURES / "dbt" / "manifest_lineage.json")
    guru.load_overlays(FIXTURES / "overlays" / "lineage.yaml")
    return guru


def _lineage_facts(guru: Guru) -> list[Fact]:
    kinds = ["LINEAGE", "COLUMN_LINEAGE"]
    subjects: list[ObjectRef] = []
    for t in guru.store.iter_tables():
        subjects.append(t.ref)
        subjects.extend(c.ref for c in guru.store.columns(t.ref))
    return guru.store.facts_for(subjects, kinds)


def _source_kinds(facts: list[Fact]) -> set[tuple[Provenance, str]]:
    return {(f.provenance, (f.source or "").split(":")[0]) for f in facts}


# ----- release ---------------------------------------------------------------------------------


def test_version_is_020() -> None:
    assert sg.__version__ == "0.2.0"
    assert re.search(r"^## \[0\.2\.0\] - \d{4}-\d{2}-\d{2}$", CHANGELOG, re.M)
    assert "UNRELEASED" not in CHANGELOG


# ----- goals -----------------------------------------------------------------------------------


@needs_parser
def test_l1_sources(tmp_path: Path) -> None:
    with _lineage_warehouse(tmp_path) as guru:
        facts = _lineage_facts(guru)
        assert {f.kind.name for f in facts} == {"LINEAGE", "COLUMN_LINEAGE"}
        assert _source_kinds(facts) >= {
            (Provenance.DECLARED, "dbt"),
            (Provenance.CATALOG, "view_definition"),
            (Provenance.HUMAN, "overlay"),
        }
        assert any((f.source or "").startswith("dbt:manifest:") for f in facts)
        assert any((f.source or "").startswith("overlay:") for f in facts)


@needs_parser
def test_l2_truthful(tmp_path: Path) -> None:
    with _lineage_warehouse(tmp_path) as guru:
        facts = _lineage_facts(guru)
        assert facts and {f.status for f in facts} == {Status.UNVERIFIED}
        texts = [guru.context("revenue by customer", profile=p).text for p in ("agent", "planner")]
        texts.append(guru.handle_tool_call("lineage", {"ref": "analytics.revenue_daily"})["text"])
        texts.append(guru.handle_tool_call("lineage", {"ref": "shop.v_orders"})["text"])
        for text in texts:
            for line in (ln for ln in text.splitlines() if "lineage" in ln.lower()
                         or "built from" in ln.lower()):  # fmt: skip
                assert "measured" not in line.lower() and "VERIFIED" not in line.replace(
                    "UNVERIFIED", ""
                ), line
        assert "unverified" in texts[-1].lower()
        assert any("Built from" in t and "unverified" in t for t in texts[:2])


@needs_parser
def test_l3_agent_surfaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from schemaguru.cli import main

    # the CI skip switch shares the SCHEMAGURU_ prefix; the CLI must not read it as config
    monkeypatch.setenv("SCHEMAGURU_FORBID_SKIPS", "1")
    with _lineage_warehouse(tmp_path) as guru:
        db = tmp_path / "g.db"
        assert guru.lineage("shop.v_orders").edges
        out = guru.handle_tool_call("lineage", {"ref": "shop.v_orders"})
        assert "error" not in out and "shop.orders" in out["text"]
        assert "lineage" in [t["name"] for t in guru.tools()]
        if importlib.util.find_spec("starlette") and importlib.util.find_spec("httpx"):
            from starlette.testclient import TestClient

            from schemaguru.server import BearerTokens, create_app

            tok = "reader-token-0123456789"
            client = TestClient(create_app(guru, authenticator=BearerTokens({tok: "reader"})))
            r = client.get("/lineage?ref=shop.v_orders", headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200 and "shop.orders" in r.text
        else:
            pytest.skip("needs starlette and httpx")
    assert main(["lineage", "shop.v_orders", "--store", str(db), "--format", "json"]) == 0


def test_l3_mcp_and_adapters(tmp_path: Path) -> None:
    import asyncio

    pytest.importorskip("mcp")
    from mcp import Client

    from schemaguru.agent.adapters import anthropic as anth
    from schemaguru.agent.mcp import build_server

    with alpha_beta_guru(tmp_path) as guru:

        async def names() -> list[str]:
            async with Client(build_server(guru)) as client:
                return [t.name for t in (await client.list_tools()).tools]

        assert "lineage" in asyncio.run(names())
        assert "lineage" in [t["name"] for t in anth.tools(guru)]
        for mod, fn in (
            ("langchain_core", "langchain"),
            ("llama_index.core", "llamaindex"),
            ("agents", "openai_agents"),
        ):
            pytest.importorskip(mod)
            adapter = importlib.import_module(f"schemaguru.agent.adapters.{fn}")
            tools = (
                adapter.tools(guru) if fn == "langchain"
                else adapter.tool_spec(guru) if fn == "llamaindex"
                else adapter.function_tools(guru)
            )  # fmt: skip
            got = [
                getattr(getattr(t, "metadata", None), "name", None) or getattr(t, "name", None)
                for t in tools
            ]
            assert "lineage" in got, (fn, got)


@needs_parser
def test_l4_optional_parser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from schemaguru.diagnostics import DiagnosticBag
    from schemaguru.model.structure import Column, Table
    from schemaguru.overlays import apply_dbt

    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")  # no tomllib on Python 3.10
    core = re.search(r"^dependencies = (\[.*?\])", text, re.M | re.S)
    lineage = re.search(r"^lineage = (\[.*?\])", text, re.M | re.S)
    assert core is not None and lineage is not None
    assert json.loads(core[1]) == ["factlang>=0.1.4,<0.2", "PyYAML>=6"]
    assert json.loads(lineage[1]) == ["sqlglot>=30,<31"]
    monkeypatch.setattr("schemaguru.lineage.parse.lineage_available", lambda: False)
    monkeypatch.setitem(sys.modules, "sqlglot", None)
    store = SQLiteStore(tmp_path / "t.db")
    try:
        for t, cs in {
            "orders": ["order_id", "customer_id", "amount"], "customers": ["customer_id"],
            "payments": ["payment_id"], "events": ["event_id"], "sessions": ["session_id"],
            "refunds": ["order_id", "refund_amount"], "revenue": ["customer_id", "net_revenue"],
        }.items():  # fmt: skip
            ref = ObjectRef(None, "shop", t)
            store.upsert_tables([Table(ref, "table")])
            store.upsert_columns(
                ref,
                [Column(ObjectRef(None, "shop", t, c), i, "STRING", "STRING", True, None, None)
                 for i, c in enumerate(cs, 1)],
            )  # fmt: skip
        bag = DiagnosticBag()
        kinds = {f.kind.name for f in apply_dbt(
            FIXTURES / "dbt" / "manifest_lineage.json", store, diagnostics=bag)}  # fmt: skip
        assert "LINEAGE" in kinds and "COLUMN_LINEAGE" not in kinds
        assert [d.code for d in bag] == ["SG212"]
    finally:
        store.close()


def test_h1_ledger_closed() -> None:
    rows = parse_ledger(LEDGER.read_text(encoding="utf-8"))
    assert set(rows) == set(ITEM_IDS)
    states = {i: rows[i][1] for i in ITEM_IDS}
    assert set(states.values()) <= {"fixed", "already fixed", "ruled out"}, {
        i: s for i, s in states.items() if s not in ("fixed", "already fixed", "ruled out")
    }


def test_s1_results_published() -> None:
    from benchmarks.scale.compare import HARD_TARGETS, known_limit_metrics

    result: dict[str, Any] = json.loads(
        (ROOT / "benchmarks" / "results" / "0.2.0.json").read_text(encoding="utf-8")
    )
    assert result["size"]["tables"] == 500_000 and result["size"]["columns"] == 25_000_000
    named = known_limit_metrics(result)
    changelog_limits = _changelog_section("Known limits")
    for name, bound in HARD_TARGETS.items():
        value = result["metrics"].get(name)
        assert value is not None, f"{name} is missing from the published result"
        if value > bound:  # a miss is allowed only when reported, never hidden
            assert name in named, f"{name} = {value} misses {bound} and is not in known_limits"
            assert re.search(rf"\b{name}\b", changelog_limits), f"{name} not in CHANGELOG limits"
    assert (ROOT / ".github" / "workflows" / "bench.yml").is_file()


# ----- anti-goals ------------------------------------------------------------------------------


def test_antigoal_no_verified_lineage() -> None:
    beta = ObjectRef(None, "shop", "beta")
    ev = Evidence(verdict="PASS", claim_text="x", sql="SELECT 1")
    value = table_lineage_value(["shop.alpha"])
    for prov in (Provenance.HUMAN, Provenance.DECLARED, Provenance.CATALOG):
        with pytest.raises(ValueError):
            Fact.make(FactKind.LINEAGE, beta, value, prov, Status.VERIFIED, evidence=ev,
                      method="exact")  # fmt: skip
    ok = Fact.make(FactKind.LINEAGE, beta, value, Provenance.CATALOG, Status.UNVERIFIED)
    assert ok.status is Status.UNVERIFIED  # the control: the same fact, unverified, is accepted


def test_antigoal_no_lineage_join_edge(tmp_path: Path) -> None:
    with alpha_beta_guru(tmp_path) as guru:
        assert guru.lineage("shop.beta").edges  # the control: lineage does tie the pair
        res = guru.find_path("shop.alpha", "shop.beta")
        assert res.paths == () and isinstance(res.refusal, Refusal)


@needs_parser
def test_antigoal_no_raw_definition_anywhere(tmp_path: Path) -> None:
    reader, literals = privacy_warehouse(tmp_path)
    db = tmp_path / "store.db"
    store = SQLiteStore(db)
    report = crawl(reader, store)
    assert report.views_parsed == 1  # the control: the definition was parsed and stored
    store.close()
    blob = b"".join(
        p.read_bytes() for s in ("", "-wal", "-shm") if (p := Path(f"{db}{s}")).exists()
    )
    for literal in literals:
        assert literal.encode() not in blob, literal


def test_antigoal_hostile_sql_cannot_hang(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    reader = ViewFakeReader({"shop.orders": ["id"], "shop.v_bad": ["id"]}, {"shop.v_bad": "HANG"})
    store = SQLiteStore(tmp_path / "t.db")
    timeout_s = 1.0
    cmd = [sys.executable, "-m", "tests.unit.lineage.hostile_worker"]
    try:
        with ParserPool(timeout_s=timeout_s, command=cmd) as pool:
            start = datetime.now(timezone.utc)
            report = crawl(reader, store, parser_pool=pool)
            took = datetime.now(timezone.utc) - start
        assert took < timedelta(seconds=timeout_s + 10)
        assert [(d.code, d.location) for d in report.lineage_diagnostics] == [
            ("SG211", "shop.v_bad")
        ]
        assert "timeout" in report.lineage_diagnostics[0].message
    finally:
        store.close()


def test_antigoal_guards_only_tightened() -> None:
    rows = parse_ledger(LEDGER.read_text(encoding="utf-8"))
    guard_files = ("tests/unit/test_guard.py", "tests/unit/catalog/test_executor.py")
    named: list[tuple[str, str]] = []
    for item_id, (_, state, evidence) in rows.items():
        if item_id.startswith(("A1.", "A2.")) and state == "fixed":
            m = re.search(r"test (?P<path>[\w./-]+\.py)::(?P<name>\w+)", evidence)
            assert m, item_id
            path = ROOT / m["path"]
            assert path.is_file() and f"def {m['name']}" in path.read_text(encoding="utf-8")
            named.append((m["path"], m["name"]))
    assert named
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         *guard_files],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-1000:]
    for path, name in named:
        if path in guard_files:
            assert f"{path}::{name}" in out.stdout or f"::{name}" in out.stdout, (path, name)
