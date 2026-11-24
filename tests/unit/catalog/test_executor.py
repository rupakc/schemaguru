from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from schemaguru.catalog import CatalogError, DBAPIExecutor, FactLangExecutor, reader_for
from schemaguru.catalog.executor import _convert, check_metadata_sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "  select * from t where a = ';'",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SHOW TABLES",
        "DESCRIBE t",
        "DESC t",
        'SELECT "a;b" FROM t',
        "SELECT 'DELETE FROM t' AS q, 'insert into x' AS r FROM t",
        'SELECT "drop", "into" FROM t WHERE created_at > updated_at',
        "SELECT executed, creator FROM t WHERE note = 'a INTO b'",
    ],
)
def test_guard_allows(sql: str) -> None:
    check_metadata_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE t",
        "DELETE FROM t",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "CREATE TABLE t(a int)",
        "SELECT 1; DROP TABLE t",
        "SELECT 1;",
        "SELECT 1 -- hi",
        "SELECT /* x */ 1",
        "",
        "COPY t TO 'x'",
        "PRAGMA writable_schema=1",
        "WITH d AS (DELETE FROM t RETURNING 1) SELECT 1",
        "SELECT 1 INTO newt",
        "SELECT * FROM t; ",
        "with x as (insert into t values (1) returning 1) select * from x",
        "CALL do_things()",
        "SELECT 1 FROM t WHERE a IN (SELECT 1) UNION SELECT 1 INTO x",
    ],
)
def test_guard_refuses_with_sg104(sql: str) -> None:
    with pytest.raises(CatalogError) as ei:
        check_metadata_sql(sql)
    assert ei.value.code == "SG104" and str(ei.value).startswith("SG104 ")


def test_paramstyle_conversion_ignores_literals() -> None:
    sql = "SELECT '?' AS q, a FROM t WHERE a = ? AND b LIKE 'x%' AND c = ?"
    assert _convert(sql, "qmark", 2) == sql
    assert _convert(sql, "format", 2) == (
        "SELECT '?' AS q, a FROM t WHERE a = %s AND b LIKE 'x%%' AND c = %s"
    )
    assert _convert(sql, "numeric", 2).endswith("a = :1 AND b LIKE 'x%' AND c = :2")
    assert _convert(sql, "named", 2).endswith("c = :p2")
    with pytest.raises(CatalogError):
        _convert(sql, "format", 1)
    with pytest.raises(CatalogError):
        DBAPIExecutor(object(), "bogus")


def test_dbapi_executor_binds_and_returns_dicts() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(a INTEGER, b TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(1, "x"), (2, "y'; DROP TABLE t; --")])
    ex = DBAPIExecutor(conn)
    assert ex.run("SELECT a, b FROM t WHERE b = ?", ["y'; DROP TABLE t; --"]) == [
        {"a": 2, "b": "y'; DROP TABLE t; --"}
    ]
    assert ex.run("SELECT count(*) AS n FROM t") == [{"n": 2}]
    named = DBAPIExecutor(conn, "named")
    assert named.run("SELECT a FROM t WHERE a = ?", (1,)) == [{"a": 1}]
    with pytest.raises(CatalogError):
        ex.run("DELETE FROM t")
    assert ex.run("SELECT count(*) AS n FROM t") == [{"n": 2}]


def test_unknown_dialect_sg101_lists_dialects() -> None:
    with pytest.raises(CatalogError) as ei:
        reader_for("nope", DBAPIExecutor(sqlite3.connect(":memory:")))
    assert ei.value.code == "SG101" and "duckdb" in str(ei.value) and "postgres" in str(ei.value)


def _has_run_metadata() -> bool:
    try:
        FactLangExecutor(object())
    except CatalogError:
        return False
    return True


def test_factlang_executor_reports_missing_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    real = importlib.import_module

    def fake(name: str, *a: Any, **k: Any) -> Any:
        if name == "factlang.connectors":
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake)
    with pytest.raises(CatalogError) as ei:
        FactLangExecutor(object())
    assert ei.value.code == "SG105"


def test_factlang_executor_adapts_result(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    calls: list[tuple[Any, ...]] = []

    class Result:
        columns = ("a", "b")
        rows = ((1, "x"), (2, "y"))

    def run_metadata(conn: Any, sql: str, params: Any = (), *, timeout_s: Any = None) -> Result:
        calls.append((conn, sql, params, timeout_s))
        return Result()

    mod = types.ModuleType("factlang.connectors")
    mod.run_metadata = run_metadata  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "factlang.connectors", mod)
    ex = FactLangExecutor("CONN")
    got = ex.run("SELECT a, b FROM t WHERE c = ? AND d = '?'", [5], timeout_s=3.0)
    assert got == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    # FactLang's run_metadata binds neutral {{name}} tokens from a mapping (spec 9.1)
    assert calls == [("CONN", "SELECT a, b FROM t WHERE c = {{p1}} AND d = '?'", {"p1": 5}, 3.0)]
    with pytest.raises(CatalogError) as e:  # SchemaGuru's own guard still runs first
        ex.run("SELECT 1; DROP TABLE t")
    assert e.value.code == "SG104" and len(calls) == 1
    with pytest.raises(CatalogError):
        ex.run("SELECT ?", [])


@pytest.mark.factlang
def test_factlang_run_metadata_real() -> None:
    if not _has_run_metadata():
        pytest.skip("factlang capability run_metadata not yet available")
    duckdb = pytest.importorskip("duckdb")
    import factlang.connectors as flc

    conn = duckdb.connect(":memory:")
    try:
        conn.execute("CREATE SCHEMA shop")
        conn.execute("CREATE TABLE shop.orders (order_id INTEGER, note VARCHAR)")
        ex = FactLangExecutor(flc.DBAPIConnector(conn, "duckdb"))
        rows = ex.run(
            "SELECT table_name, ? AS tag FROM information_schema.tables WHERE table_schema = ?",
            ["it's", "shop"],
        )
        assert rows == [{"table_name": "orders", "tag": "it's"}]
        assert ex.run("SHOW TABLES FROM shop") == [{"name": "orders"}]
        # FactLang's guard admits EXPLAIN (FL703 ruling); SchemaGuru's readers never need it,
        # so SchemaGuru's stricter guard (run first) keeps refusing it
        for bad in ("EXPLAIN SELECT 1", "SELECT 1; SELECT 2", "SELECT * FROM t; DROP TABLE t"):
            with pytest.raises(CatalogError):
                ex.run(bad)
    finally:
        conn.close()


@pytest.mark.factlang
def test_every_recorded_reader_query_passes_factlangs_guard() -> None:
    """Every metadata query a shipped reader ran against a real engine (the recorded conformance
    fixtures) passes FactLang's FL703 guard once its ``?`` markers are FactLang tokens."""
    import json
    from pathlib import Path

    try:
        from factlang.connectors import check_metadata_sql as fl_check
    except ImportError:
        pytest.skip("factlang capability run_metadata not yet available")
    from schemaguru.catalog.executor import _factlang_sql

    root = Path(__file__).resolve().parents[2] / "fixtures" / "catalog"
    seen = 0
    refused: dict[str, list[str]] = {}
    for path in sorted(root.glob("*/queries.json")):
        for q in json.loads(path.read_text(encoding="utf-8"))["queries"]:
            sql, bound = _factlang_sql(q["sql"], len(q.get("params") or ()))
            assert len(bound) == len(q.get("params") or ())
            try:
                fl_check(sql)
            except Exception as e:
                refused.setdefault(path.parent.name, []).append(str(e))
            seen += 1
    assert seen >= 20
    # Known FactLang false positive (reported upstream, FactLang is not changed from here): its
    # FL703 guard refuses the keyword FETCH, so the Oracle reader's read-only keyset page
    # ``... FETCH FIRST ? ROWS ONLY`` cannot run through run_metadata. Pinned exactly: any other
    # refusal fails, and this one must still be the only cause (re-open when FactLang admits it).
    assert set(refused) <= {"oracle"}, refused
    assert all("FETCH" in m for m in refused.get("oracle", [])), refused


def test_metadata_guard_backslash_escape_cannot_hide_a_statement() -> None:
    with pytest.raises(CatalogError):
        check_metadata_sql("SELECT 'a\\'' ; DROP TABLE x --")
    check_metadata_sql("SELECT 'it''s'")


def test_metadata_guard_dollar_and_triple_quotes_cannot_hide_a_statement() -> None:
    for bad in ("SELECT $$'$$; DROP TABLE x; -- '", "SELECT '''it's'''; DROP TABLE t; -- '"):
        with pytest.raises(CatalogError):
            check_metadata_sql(bad)
