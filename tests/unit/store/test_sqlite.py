from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from schemaguru.errors import SchemaGuruError
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table
from schemaguru.store import sqlite as sqlite_mod
from schemaguru.store.sqlite import SQLiteStore

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


def tref(name: str, schema: str = "shop") -> ObjectRef:
    return ObjectRef(None, schema, name)


def tbl(name: str, comment: str | None = None, schema: str = "shop") -> Table:
    return Table(tref(name, schema), "table", comment=comment)


def col(table: str, name: str, ordinal: int, comment: str | None = None) -> Column:
    return Column(
        ObjectRef(None, "shop", table, name), ordinal, "STRING", "STRING", True, comment, None
    )


def test_upsert_idempotent(store: SQLiteStore) -> None:
    t = Table(tref("orders"), "table", comment="c", row_count=5, created_at=NOW)
    cols = [col("orders", "id", 1), col("orders", "customer_id", 2)]
    con = Constraint("primary_key", tref("orders"), ("id",), enforced=False)
    for _ in range(3):
        store.upsert_schemas([Schema(None, "shop")])
        store.upsert_tables([t])
        store.upsert_columns(tref("orders"), cols)
        store.upsert_constraints(tref("orders"), [con])
    assert store.table(tref("orders")) == t
    assert store.columns(tref("orders")) == cols
    assert store.constraints(tref("orders")) == [con]
    assert len(list(store.iter_tables())) == 1
    assert store.table(tref("nope")) is None
    # a re-crawl with fewer columns replaces the list
    store.upsert_columns(tref("orders"), cols[:1])
    assert store.columns(tref("orders")) == cols[:1]


def test_withdrawn(store: SQLiteStore) -> None:
    store.upsert_tables([tbl("orders"), tbl("legacy")])
    store.mark_withdrawn([tref("legacy")], NOW)
    assert [t.ref.table for t in store.iter_tables()] == ["orders"]
    allt = {t.ref.table: t for t in store.iter_tables(include_withdrawn=True)}
    assert allt["legacy"].withdrawn_at == NOW
    # first withdrawal time is kept
    store.mark_withdrawn([tref("legacy")], NOW + timedelta(days=1))
    assert store.table(tref("legacy")).withdrawn_at == NOW  # type: ignore[union-attr]
    # a table seen again is no longer withdrawn
    store.upsert_tables([tbl("legacy")])
    assert store.table(tref("legacy")).withdrawn_at is None  # type: ignore[union-attr]


def test_naive_withdrawn_rejected(store: SQLiteStore) -> None:
    with pytest.raises(ValueError):
        store.mark_withdrawn([tref("x")], datetime(2026, 1, 1))


def test_resolve_exact_then_unique_ci(store: SQLiteStore) -> None:
    store.upsert_tables([tbl("Orders"), tbl("customers")])
    store.upsert_columns(tref("Orders"), [col("Orders", "Id", 1)])
    assert store.resolve("shop.Orders") == (tref("Orders"), [])
    assert store.resolve("SHOP.orders") == (tref("Orders"), [])
    assert store.resolve("shop.customers")[0] == tref("customers")
    assert store.resolve("shop.missing") == (None, [])
    assert store.resolve('???"') == (None, [])
    # table.column forms
    got, cands = store.resolve("shop.Orders.id")
    assert got == ObjectRef(None, "shop", "Orders", "Id") and cands == []
    assert store.resolve("shop.Orders.nope") == (None, [])
    assert store.resolve("shop.Orders.Id")[0] == ObjectRef(None, "shop", "Orders", "Id")


def test_resolve_ambiguous_returns_candidates(store: SQLiteStore) -> None:
    store.upsert_tables([tbl("Orders"), tbl("ORDERS"), tbl("orders_x")])
    # exact wins even when other case variants exist
    assert store.resolve("shop.Orders") == (tref("Orders"), [])
    got, cands = store.resolve("shop.orders")
    assert got is None
    assert cands == [tref("ORDERS"), tref("Orders")]


def test_resolve_ambiguous_column(store: SQLiteStore) -> None:
    store.upsert_tables([tbl("orders")])
    store.upsert_columns(tref("orders"), [col("orders", "ID", 1), col("orders", "Id", 2)])
    got, cands = store.resolve("shop.orders.id")
    assert got is None and len(cands) == 2


def test_search_ranks_name_hits_first(store: SQLiteStore) -> None:
    store.upsert_tables(
        [tbl("payments", "payments for each order"), tbl("orders", "customer purchases")]
    )
    store.upsert_columns(tref("payments"), [col("payments", "order_id", 1, "the order paid")])
    store.upsert_columns(tref("orders"), [col("orders", "id", 1)])
    res = store.search("orders", 5)
    assert [r.table for r, _ in res][:2] == ["orders", "payments"]
    assert res[0][1] > res[1][1]
    assert store.search("zzzz", 5) == []
    assert store.search("", 5) == []
    assert len(store.search("order", 1)) == 1


def test_search_splits_camel_snake(store: SQLiteStore) -> None:
    store.upsert_tables([tbl("CustomerOrders"), tbl("misc")])
    store.upsert_columns(tref("misc"), [col("misc", "order_item_id", 1), col("misc", "x", 2)])
    assert store.search("customer", 5)[0][0].table == "CustomerOrders"
    assert store.search("customerOrderId", 5)[0][0].table == "CustomerOrders"
    assert [r.table for r, _ in store.search("item", 5)] == ["misc"]
    assert [r.table for r, _ in store.search("customer_order", 5)][:1] == ["CustomerOrders"]


def test_search_excludes_withdrawn_and_sees_facts(store: SQLiteStore) -> None:
    store.upsert_tables([tbl("orders"), tbl("legacy")])
    store.mark_withdrawn([tref("legacy")], NOW)
    assert store.search("legacy", 5) == []
    store.put_facts(
        [
            Fact.make(
                FactKind.DESCRIPTION,
                tref("orders"),
                {"text": "ledger of refunds"},
                Provenance.HUMAN,
                Status.OBSERVED,
            )
        ]
    )
    assert store.search("refunds", 5)[0][0] == tref("orders")


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


def test_snapshot_and_facts(store: SQLiteStore) -> None:
    j = JoinRef((ObjectRef(None, "shop", "orders", "customer_id"),),
                (ObjectRef(None, "shop", "customers", "id"),))  # fmt: skip
    jc = JoinRef(j.left, j.right, cast_to="STRING")
    f1 = Fact.make(FactKind.JOIN, j, {"match_rate": 0.5}, Provenance.MEASURED, Status.OBSERVED,
                   method="exact")  # fmt: skip
    f2 = Fact.make(FactKind.ROW_COUNT, tref("orders"), {"rows": 3}, Provenance.CATALOG,
                   Status.OBSERVED)  # fmt: skip
    f3 = Fact.make(FactKind.JOIN, jc, {"match_rate": 0.1}, Provenance.MEASURED, Status.OBSERVED,
                   method="exact")  # fmt: skip
    assert store.latest_snapshot() is None
    store.put_facts([f1, f2, f3])
    assert store.facts_for([j], None) == [f1]
    assert store.facts_for([jc], None) == [f3]
    assert store.facts_for([tref("orders")], ["ROW_COUNT"]) == [f2]
    assert store.facts_for([tref("orders")], ["JOIN"]) == []
    assert store.facts_for([], None) == []
    assert {f.id for f in store.facts_for(None, ["JOIN"])} == {f1.id, f3.id}
    assert len(store.facts_for(None, None)) == 3
    # re-measurement: same id, new value -> replaces
    f1b = Fact.make(FactKind.JOIN, j, {"match_rate": 0.9}, Provenance.MEASURED, Status.OBSERVED,
                    method="exact")  # fmt: skip
    assert f1b.id == f1.id
    s1 = store.snapshot("first", checks_run=["join", "rows"])
    store.put_facts([f1b])
    s2 = store.snapshot()
    assert store.latest_snapshot() == s2 and s1 != s2
    old = {f.id: f for f in store.snapshot_facts(s1)}
    assert old[f1.id].value["match_rate"] == 0.5 and old[f1.id].snapshot_id == s1
    assert {f.id: f for f in store.snapshot_facts(s2)}[f1.id].value["match_rate"] == 0.9
    assert len(store.snapshot_facts(s1)) == 3
    assert store.snapshot_checks_run(s1) == frozenset({"join", "rows"})
    assert store.snapshot_checks_run(s2) == frozenset()
    assert store.facts_for([j], None)[0].value["match_rate"] == 0.9
    assert store.snapshot_facts("nope") == []


def test_many_subjects_chunked(store: SQLiteStore) -> None:
    facts = [
        Fact.make(
            FactKind.ROW_COUNT, tref(f"t{i}"), {"rows": i}, Provenance.CATALOG, Status.OBSERVED
        )
        for i in range(1200)
    ]
    store.put_facts(facts)
    assert len(store.facts_for([tref(f"t{i}") for i in range(1200)], None)) == 1200


def test_queue_priority_order(store: SQLiteStore) -> None:
    store.queue_push(tref("a"), "stale", 1.0)
    store.queue_push(tref("b"), "new", 5.0)
    store.queue_push(tref("c"), "new", 5.0)
    store.queue_push(tref("a"), "stale", 9.0)  # re-push raises priority, no duplicate
    assert store.queue_pop(2) == [(tref("a"), "stale"), (tref("b"), "new")]
    assert store.queue_pop(10) == [(tref("c"), "new")]
    assert store.queue_pop(1) == []


def test_ledger_totals(store: SQLiteStore) -> None:
    store.ledger_add("measure", 100, "bytes", "n1")
    store.ledger_add("measure", 50, "bytes", "n2")
    store.ledger_add("measure", 7, "queries", "n3")
    store.ledger_add("draft", 900, "tokens", "n4")
    assert store.ledger_total("measure", "bytes", None) == 150
    assert store.ledger_total("measure", "queries", None) == 7
    assert store.ledger_total("nothing", "bytes", None) == 0
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert store.ledger_total("measure", "bytes", future) == 0
    assert store.ledger_total("measure", "bytes", NOW) == 150
    with pytest.raises(ValueError):
        store.ledger_total("measure", "bytes", datetime(2026, 1, 1))


def test_crawl_state_and_feedback(store: SQLiteStore) -> None:
    assert store.crawl_state_get("k") is None
    store.crawl_state_set("k", {"page": 2, "done": ["a"]})
    store.crawl_state_set("k", {"page": 3})
    assert store.crawl_state_get("k") == {"page": 3}
    store.feedback_add(tref("orders"), "status", "wrong_value")
    store.feedback_add(tref("orders"), "status", "wrong_value")
    store.feedback_add(tref("orders"), None, "missing_join")
    summary = store.feedback_summary()
    assert {"table": "shop.orders", "column": "status", "failure_class": "wrong_value",
            "count": 2} in summary  # fmt: skip
    assert len(summary) == 2


def test_transaction_rollback(store: SQLiteStore) -> None:
    with pytest.raises(RuntimeError), store.transaction():
        store.upsert_tables([tbl("a")])
        raise RuntimeError("boom")
    assert store.table(tref("a")) is None
    with store.transaction():
        store.upsert_tables([tbl("a")])
        with store.transaction():
            store.upsert_tables([tbl("b")])
    assert store.table(tref("b")) is not None


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


def test_concurrent_writers_serialised(tmp_path: Path) -> None:
    s = SQLiteStore(tmp_path / "c.db")
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(500):
                s.upsert_tables([tbl(f"t{n}_{i}")])
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []
        assert sum(1 for _ in s.iter_tables()) == 2000
    finally:
        s.close()


def test_iter_tables_streams_in_pages(store: SQLiteStore) -> None:
    store.upsert_tables([tbl(f"t{i:04d}") for i in range(2500)])
    it = store.iter_tables()
    first = next(it)
    store.upsert_tables([tbl("zzz")])  # writes interleave with an open iterator
    assert first.ref.table == "t0000"
    assert sum(1 for _ in it) == 2500
