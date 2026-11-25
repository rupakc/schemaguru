"""SQLiteStore: the shared ``Store`` contract plus what only the SQLite backend does."""

from __future__ import annotations

from pathlib import Path

import pytest

from schemaguru.errors import SchemaGuruError
from schemaguru.store import sqlite as sqlite_mod
from schemaguru.store.sqlite import SQLiteStore
from tests.unit.store.contract import *  # noqa: F403  (the contract tests run here)
from tests.unit.store.contract import col, tbl, tref


def test_fts5_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite_mod, "_fts5_available", lambda conn: False)
    s = SQLiteStore(tmp_path / "fb.db")
    try:
        assert [d.code for d in s.diagnostics] == ["SG701"]
        s.upsert_tables([tbl("payments", "payments for each order"), tbl("orders")])
        s.upsert_columns(tref("payments"), [col("payments", "order_id", 1)])
        res = s.search("order", 5)
        assert [r.table for r, _ in res] == ["orders", "payments"]
        assert s.search("100%_", 5) == []
        assert "payments" in [r.table for r, _ in s.search("customerOrderId", 5)]
    finally:
        s.close()


def test_readonly(tmp_path: Path) -> None:
    p = tmp_path / "ro.db"
    w = SQLiteStore(p)
    w.upsert_tables([tbl("orders")])
    w.close()
    r = SQLiteStore(p, readonly=True)
    try:
        assert r.table(tref("orders")) is not None
        with pytest.raises(SchemaGuruError):
            r.upsert_tables([tbl("x")])
    finally:
        r.close()


def _build_without_fts(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as m:
        m.setattr(sqlite_mod, "_fts5_available", lambda conn: False)
        s = SQLiteStore(path)
        s.upsert_tables([tbl("orders", "customer purchases"), tbl("payments")])
        s.upsert_columns(tref("orders"), [col("orders", "id", 1)])
        s.close()


def test_store_built_without_fts_reindexed_when_fts_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "x.db"
    _build_without_fts(p, monkeypatch)
    s = SQLiteStore(p)
    try:
        assert list(s.diagnostics) == []
        assert [r.table for r, _ in s.search("orders", 5)] == ["orders"]
    finally:
        s.close()


def test_store_built_with_fts_usable_without(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "y.db"
    SQLiteStore(p).close()
    s = SQLiteStore(p)
    s.upsert_tables([tbl("orders")])
    s.close()
    with monkeypatch.context() as m:
        m.setattr(sqlite_mod, "_fts5_available", lambda conn: False)
        s = SQLiteStore(p)
        try:
            assert [r.table for r, _ in s.search("orders", 5)] == ["orders"]
        finally:
            s.close()


def test_readonly_without_fts_index_warns_and_uses_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "z.db"
    _build_without_fts(p, monkeypatch)
    r = SQLiteStore(p, readonly=True)
    try:
        assert [d.code for d in r.diagnostics] == ["SG701"]
        assert [x.table for x, _ in r.search("orders", 5)] == ["orders"]
    finally:
        r.close()


def test_search_survives_vacuum(store: SQLiteStore) -> None:
    store.upsert_tables([tbl(f"filler{i}") for i in range(20)] + [tbl("orders")])
    store.upsert_columns(tref("orders"), [col("orders", "id", 1)])
    for i in range(10):
        store._conn.execute("DELETE FROM tables WHERE ref=?", (f"shop.filler{i}",))
    store._conn.execute("VACUUM")
    assert [r.table for r, _ in store.search("orders", 3)] == ["orders"]


def test_facts_for_filters_kinds_in_sql(store: SQLiteStore) -> None:
    """The kind filter runs in SQL on the ``(subject_key, kind)`` index, not in Python after
    loading every fact of the subjects."""
    from tests.unit.store.contract import _desc, _rows

    a = tref("orders")
    store.put_facts([_desc(a, "x"), _rows(a, 3)])
    seen: list[str] = []
    store._conn.set_trace_callback(seen.append)
    try:
        assert [f.kind.name for f in store.facts_for([a], ["ROW_COUNT"])] == ["ROW_COUNT"]
    finally:
        store._conn.set_trace_callback(None)
    (sql,) = [s for s in seen if s.lstrip().upper().startswith("SELECT ID, DATA FROM FACTS")]
    assert " AND kind IN (" in sql, sql
    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT id, data FROM facts WHERE subject_key IN (?) AND kind IN (?)",
        ["k", "ROW_COUNT"],
    ).fetchall()
    assert any("facts_subject_kind" in str(row) for row in plan), plan
