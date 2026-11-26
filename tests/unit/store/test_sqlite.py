"""SQLiteStore: the shared ``Store`` contract plus what only the SQLite backend does."""

from __future__ import annotations

from pathlib import Path

import pytest

from schemaguru.errors import SchemaGuruError
from schemaguru.model.facts import Provenance
from schemaguru.store import sqlite as sqlite_mod
from schemaguru.store.sqlite import SQLiteStore
from tests.unit.store.contract import *  # noqa: F403  (the contract tests run here)
from tests.unit.store.contract import _lin, col, tbl, tref


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


def test_commit_failure_recovers(store: SQLiteStore) -> None:
    conn = store._conn
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TEMP TABLE parent(id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TEMP TABLE child(pid INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)"
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError), store.transaction():
        store.upsert_tables([tbl("lost")])
        conn.execute("INSERT INTO child(pid) VALUES (1)")  # fails only at COMMIT
    assert not conn.in_transaction and store._depth == 0
    assert store.table(tref("lost")) is None  # rolled back, not left open
    store.upsert_tables([tbl("next")])  # the next transaction works
    with store.transaction():
        store.upsert_tables([tbl("after")])
    assert store.table(tref("next")) is not None and store.table(tref("after")) is not None


def test_backend_switch_rebuilds_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "switch.db"
    with SQLiteStore(p) as s:  # FTS5
        s.upsert_tables([tbl("orders", "customer purchases")])
    with monkeypatch.context() as m:  # LIKE: writes go to the other index only
        m.setattr(sqlite_mod, "_fts5_available", lambda conn: False)
        with SQLiteStore(p) as s:
            s.upsert_tables([tbl("orders", "parcel shipments"), tbl("refunds")])
            assert [r.table for r, _ in s.search("parcel", 5)] == ["orders"]
            assert s.crawl_state_get("_search_backend") == {"backend": "like"}
    with SQLiteStore(p) as s:  # FTS5 again: the stale FTS index is rebuilt
        assert s.crawl_state_get("_search_backend") == {"backend": "fts5"}
        assert [r.table for r, _ in s.search("parcel", 5)] == ["orders"]
        assert s.search("purchases", 5) == []
        assert [r.table for r, _ in s.search("refunds", 5)] == ["refunds"]
    with monkeypatch.context() as m:  # and back to LIKE: the LIKE index is rebuilt too
        m.setattr(sqlite_mod, "_fts5_available", lambda conn: False)
        with SQLiteStore(p) as s:
            assert [r.table for r, _ in s.search("refunds", 5)] == ["refunds"]


def test_readonly_empty_index_warns(tmp_path: Path) -> None:
    p = tmp_path / "ro_empty.db"
    with SQLiteStore(p) as s:
        s.upsert_tables([tbl("orders")])
        s._conn.execute("DELETE FROM table_docs")
    with SQLiteStore(p, readonly=True) as r:
        (d,) = [d for d in r.diagnostics if d.code == "SG706"]
        assert d.severity.value == "WARNING"
        assert d.message == (
            "the read-only store's search index is empty; open it read-write once to rebuild it"
        )
    with SQLiteStore(p) as w:  # read-write rebuilds it
        assert [x.table for x, _ in w.search("orders", 5)] == ["orders"]
    with SQLiteStore(p, readonly=True) as r:
        assert [d.code for d in r.diagnostics] == []
    # negative control: an empty store (no tables) has nothing to index and does not warn
    q = tmp_path / "empty.db"
    SQLiteStore(q).close()
    with SQLiteStore(q, readonly=True) as r:
        assert [d.code for d in r.diagnostics] == []


def test_downstream_query_uses_index(store: SQLiteStore) -> None:
    """The downstream traversal is an index lookup, never a scan of the edge table."""
    store.put_facts([_lin(["shop.orders"], Provenance.DECLARED, "dbt:manifest:x")])
    seen: list[str] = []
    store._conn.set_trace_callback(seen.append)
    try:
        edges = store.lineage_edges(["shop.orders"], direction="downstream", level="table")
    finally:
        store._conn.set_trace_callback(None)
    assert [e.downstream_key for e in edges] == ["shop.v"]
    (query,) = [q for q in seen if "lineage_edges" in q]
    plan = " ".join(str(r[-1]) for r in store._q("EXPLAIN QUERY PLAN " + query))
    assert "lineage_edges_upstream" in plan, plan


def test_lineage_facts_under_query_uses_the_subject_index(store: SQLiteStore) -> None:
    """A crawl replaces each re-parsed view's lineage facts: the read is an index range on the
    view's subject key, never a scan of every lineage fact per view (quadratic at 500k tables:
    the kind index alone was chosen)."""
    store.put_facts([_lin(["shop.orders"], Provenance.CATALOG, "view_definition")])
    # "shop.v$1" sorts inside the range ["shop.v", "shop.v/"): read, then dropped as not v's
    other = _lin(["shop.orders"], Provenance.CATALOG, "view_definition", subject=tref("v$1"))
    store.put_facts([other])
    seen: list[str] = []
    store._conn.set_trace_callback(seen.append)
    try:
        facts = store.lineage_facts_under([tref("v")])
    finally:
        store._conn.set_trace_callback(None)
    assert [f.subject.canonical() for f in facts] == ["shop.v"]
    queries = [q for q in seen if q.lstrip().upper().startswith("SELECT ID, DATA FROM FACTS")]
    assert queries
    for query in queries:
        plan = " ".join(str(r[-1]) for r in store._q("EXPLAIN QUERY PLAN " + query))
        assert "facts_subject_kind" in plan, plan


def test_reindex_reads_meaning_facts_by_the_subject_index(store: SQLiteStore) -> None:
    """Rebuilding a table's search document reads its own meaning facts by an index range on
    its subject key, never every meaning fact in the store (once per crawled table)."""
    from tests.unit.store.contract import _desc

    store.put_facts([_desc(tref("orders"), "the orders")])
    seen: list[str] = []
    store._conn.set_trace_callback(seen.append)
    try:
        store.upsert_tables([tbl("orders")])
    finally:
        store._conn.set_trace_callback(None)
    queries = [q for q in seen if "FROM facts" in q and "subject_key >=" in q]
    assert queries
    for query in queries:
        plan = " ".join(str(r[-1]) for r in store._q("EXPLAIN QUERY PLAN " + query))
        assert "facts_subject_kind" in plan, plan
    assert [r.table for r, _ in store.search("the orders", 1)] == ["orders"]
