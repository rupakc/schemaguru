"""The backend-agnostic ``Store`` contract (spec 7.1).

Every function here is a test that takes a ``store`` fixture. A test module imports them with
``from tests.unit.store.contract import *`` and provides the fixture: ``test_sqlite.py`` for
SQLiteStore, ``tests/integration/store/test_postgres_store.py`` for both backends side by side.
Backend-specific behaviour (FTS5 fallback, read-only files, VACUUM, query plans) stays in the
backend's own module.
"""

from __future__ import annotations

import struct
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


def tref(name: str, schema: str = "shop") -> ObjectRef:
    return ObjectRef(None, schema, name)


def tbl(name: str, comment: str | None = None, schema: str = "shop") -> Table:
    return Table(tref(name, schema), "table", comment=comment)


def col(table: str, name: str, ordinal: int, comment: str | None = None) -> Column:
    return Column(
        ObjectRef(None, "shop", table, name), ordinal, "STRING", "STRING", True, comment, None
    )


def test_upsert_idempotent(store: Any) -> None:
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


def test_withdrawn(store: Any) -> None:
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


def test_naive_withdrawn_rejected(store: Any) -> None:
    with pytest.raises(ValueError):
        store.mark_withdrawn([tref("x")], datetime(2026, 1, 1))


def test_resolve_exact_then_unique_ci(store: Any) -> None:
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


def test_resolve_ambiguous_returns_candidates(store: Any) -> None:
    store.upsert_tables([tbl("Orders"), tbl("ORDERS"), tbl("orders_x")])
    # exact wins even when other case variants exist
    assert store.resolve("shop.Orders") == (tref("Orders"), [])
    got, cands = store.resolve("shop.orders")
    assert got is None
    assert cands == [tref("ORDERS"), tref("Orders")]


def test_resolve_ambiguous_column(store: Any) -> None:
    store.upsert_tables([tbl("orders")])
    store.upsert_columns(tref("orders"), [col("orders", "ID", 1), col("orders", "Id", 2)])
    got, cands = store.resolve("shop.orders.id")
    assert got is None and len(cands) == 2


def test_search_ranks_name_hits_first(store: Any) -> None:
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


def test_search_splits_camel_snake(store: Any) -> None:
    store.upsert_tables([tbl("CustomerOrders"), tbl("misc")])
    store.upsert_columns(tref("misc"), [col("misc", "order_item_id", 1), col("misc", "x", 2)])
    assert store.search("customer", 5)[0][0].table == "CustomerOrders"
    assert store.search("customerOrderId", 5)[0][0].table == "CustomerOrders"
    assert [r.table for r, _ in store.search("item", 5)] == ["misc"]
    assert [r.table for r, _ in store.search("customer_order", 5)][:1] == ["CustomerOrders"]


def test_search_excludes_withdrawn_and_sees_facts(store: Any) -> None:
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


def test_snapshot_and_facts(store: Any) -> None:
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


def test_delete_facts_removes_live_rows_only(store: Any) -> None:
    store.upsert_tables([tbl("orders")])
    desc = Fact.make(FactKind.DESCRIPTION, tref("orders"), {"text": "ledger of refunds"},
                     Provenance.HUMAN, Status.UNVERIFIED, source="overlay:o.yaml:3")  # fmt: skip
    rows = Fact.make(FactKind.ROW_COUNT, tref("orders"), {"rows": 3}, Provenance.CATALOG,
                     Status.OBSERVED)  # fmt: skip
    store.put_facts([desc, rows])
    snap = store.snapshot("before")
    assert store.search("refunds", 5)[0][0] == tref("orders")
    assert store.delete_facts([desc.id, "sha256:not-there"]) == 1
    assert store.facts_for([tref("orders")], None) == [rows]
    assert store.search("refunds", 5) == []  # the search document follows
    assert {f.id for f in store.snapshot_facts(snap)} == {desc.id, rows.id}  # history kept
    assert store.delete_facts([]) == 0


def test_many_subjects_chunked(store: Any) -> None:
    facts = [
        Fact.make(
            FactKind.ROW_COUNT, tref(f"t{i}"), {"rows": i}, Provenance.CATALOG, Status.OBSERVED
        )
        for i in range(1200)
    ]
    store.put_facts(facts)
    assert len(store.facts_for([tref(f"t{i}") for i in range(1200)], None)) == 1200


def test_queue_priority_order(store: Any) -> None:
    store.queue_push(tref("a"), "stale", 1.0)
    store.queue_push(tref("b"), "new", 5.0)
    store.queue_push(tref("c"), "new", 5.0)
    store.queue_push(tref("a"), "stale", 9.0)  # re-push raises priority, no duplicate
    assert store.queue_pop(2) == [(tref("a"), "stale"), (tref("b"), "new")]
    assert store.queue_pop(10) == [(tref("c"), "new")]
    assert store.queue_pop(1) == []


def test_ledger_totals(store: Any) -> None:
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


def test_crawl_state_and_feedback(store: Any) -> None:
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


def test_transaction_rollback(store: Any) -> None:
    with pytest.raises(RuntimeError), store.transaction():
        store.upsert_tables([tbl("a")])
        raise RuntimeError("boom")
    assert store.table(tref("a")) is None
    with store.transaction():
        store.upsert_tables([tbl("a")])
        with store.transaction():
            store.upsert_tables([tbl("b")])
    assert store.table(tref("b")) is not None


def test_concurrent_writers_serialised(store: Any) -> None:
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(500):
                store.upsert_tables([tbl(f"t{n}_{i}")])
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sum(1 for _ in store.iter_tables()) == 2000


def test_iter_tables_streams_in_pages(store: Any) -> None:
    store.upsert_tables([tbl(f"t{i:04d}") for i in range(2500)])
    it = store.iter_tables()
    first = next(it)
    store.upsert_tables([tbl("zzz")])  # writes interleave with an open iterator
    assert first.ref.table == "t0000"
    assert sum(1 for _ in it) == 2500


def test_snapshot_many_facts(store: Any) -> None:
    facts = [
        Fact.make(
            FactKind.ROW_COUNT, tref(f"t{i}"), {"rows": i}, Provenance.CATALOG, Status.OBSERVED
        )
        for i in range(10_500)
    ]
    store.put_facts(facts)
    sid = store.snapshot("big")
    snap = store.snapshot_facts(sid)
    assert len(snap) == 10_500
    assert all(f.snapshot_id == sid for f in snap)
    by_id = {f.id: f for f in facts}
    assert all(by_id[f.id].value == f.value and by_id[f.id].subject == f.subject for f in snap)


def test_column_description_makes_table_searchable(store: Any) -> None:
    store.upsert_tables([tbl("orders"), tbl("misc")])
    store.upsert_columns(tref("orders"), [col("orders", "status", 1)])
    assert store.search("shipped", 5) == []
    store.put_facts(
        [
            Fact.make(
                FactKind.DESCRIPTION,
                ObjectRef(None, "shop", "orders", "status"),
                {"text": "whether the parcel was shipped"},
                Provenance.HUMAN,
                Status.OBSERVED,
            )
        ]
    )
    assert [r.table for r, _ in store.search("shipped", 5)] == ["orders"]


def test_decimal_and_datetime_values_roundtrip(store: Any, tmp_path: Path) -> None:
    from decimal import Decimal

    f = Fact.make(
        FactKind.ROW_COUNT,
        tref("orders"),
        {"rows": Decimal("12.50"), "at": NOW, "n": 3},
        Provenance.CATALOG,
        Status.OBSERVED,
    )
    store.put_facts([f])
    (got,) = store.facts_for([tref("orders")], None)
    assert got.id == f.id
    # canonical encoding: Decimal -> its exact text, aware datetime -> UTC ISO text
    assert got.value == {"rows": "12.50", "at": NOW.isoformat(), "n": 3}
    store.snapshot()
    from schemaguru.store.export import export_json

    out = tmp_path / "e.json"
    export_json(store, out)
    assert b'"rows":"12.50"' in out.read_bytes()


def _desc(ref: ObjectRef, text: str) -> Fact:
    return Fact.make(FactKind.DESCRIPTION, ref, {"text": text}, Provenance.HUMAN, Status.UNVERIFIED)


def _rows(ref: ObjectRef, n: int) -> Fact:
    return Fact.make(
        FactKind.ROW_COUNT, ref, {"rows": n}, Provenance.MEASURED, Status.OBSERVED, method="exact",
        observed_at=NOW,
    )  # fmt: skip


def test_facts_for_filters_kinds(store: Any) -> None:
    a, b = tref("orders"), tref("customers")
    store.put_facts([_desc(a, "x"), _rows(a, 3), _desc(b, "y")])
    got = store.facts_for([a, b], ["ROW_COUNT"])
    assert [f.kind.name for f in got] == ["ROW_COUNT"]
    assert store.facts_for([a], []) == []
    assert {f.kind.name for f in store.facts_for([a], None)} == {"DESCRIPTION", "ROW_COUNT"}


def test_tables_named_case_insensitive_live_only(store: Any) -> None:
    store.upsert_tables(
        [
            Table(tref("Orders"), "table"),
            Table(tref("orders", "archive"), "table"),
            Table(tref("order_items"), "table"),
            Table(tref("orders", "old"), "table"),
        ]
    )
    store.mark_withdrawn([tref("orders", "old")], NOW)
    assert store.tables_named(["orders"], limit=10) == [
        tref("orders", "archive"),
        tref("Orders"),
    ]
    assert store.tables_named(["ORDER_ITEMS", "nope"], limit=10) == [tref("order_items")]
    assert store.tables_named(["orders"], limit=1) == [tref("orders", "archive")]
    assert store.tables_named([], limit=5) == []


def test_embeddings_roundtrip_live_only(store: Any) -> None:
    store.upsert_tables([Table(tref("orders"), "table"), Table(tref("legacy"), "table")])
    store.embeddings_put(
        "m1",
        [(tref("orders"), "h1", [1.0, 0.5]), (tref("legacy"), "h2", [0.0, 1.0])],
    )
    store.embeddings_put("m2", [(tref("orders"), "h3", [2.0])])
    store.mark_withdrawn([tref("legacy")], NOW)
    rows = store.embeddings_get("m1")
    assert [(r, h, d) for r, h, d, _ in rows] == [(tref("orders"), "h1", 2)]
    assert struct.unpack("<2f", rows[0][3]) == (1.0, 0.5)
    # replace by (model, ref)
    store.embeddings_put("m1", [(tref("orders"), "h9", [3.0, 3.0])])
    assert store.embeddings_hashes("m1") == {tref("orders"): "h9", tref("legacy"): "h2"}
    assert [h for _, h, _, _ in store.embeddings_get("m2")] == ["h3"]


def test_embeddings_reject_empty(store: Any) -> None:
    store.upsert_tables([Table(tref("orders"), "table")])
    with pytest.raises(ValueError, match="empty"):
        store.embeddings_put("m", [(tref("orders"), "h", [])])


def _raw(store: Any, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Raw rows through the backend's own query helper (``?`` placeholders on both)."""
    if type(store).__name__ == "PostgresStore":
        sql = sql.replace("?", "%s")
    return [tuple(r) for r in store._q(sql, params)]


def test_nested_transaction_savepoint(store: Any) -> None:
    with store.transaction():
        store.upsert_tables([tbl("outer_before")])
        with pytest.raises(RuntimeError), store.transaction():
            store.upsert_tables([tbl("inner")])
            store.put_facts([_desc(tref("outer_before"), "inner words")])
            raise RuntimeError("inner block fails")
        store.upsert_tables([tbl("outer_after")])
    assert store.table(tref("outer_before")) is not None
    assert store.table(tref("outer_after")) is not None
    assert store.table(tref("inner")) is None  # only the inner block's writes rolled back
    assert store.facts_for([tref("outer_before")], None) == []
    assert store.search("inner", 5) == []
    # three levels: the middle one fails, the innermost (already released) goes with it
    with store.transaction():
        with pytest.raises(ValueError), store.transaction():
            with store.transaction():
                store.upsert_tables([tbl("deepest")])
            raise ValueError("middle fails")
        store.upsert_tables([tbl("top")])
    assert store.table(tref("deepest")) is None and store.table(tref("top")) is not None


def test_reindex_on_subject_change(store: Any) -> None:
    # a column of shop.orders and the catalog-qualified table shop.orders.status share one
    # canonical key, so the same fact id can move between the two tables
    column = ObjectRef(None, "shop", "orders", "status")
    table = ObjectRef("shop", "orders", "status")
    store.upsert_tables([tbl("orders"), Table(table, "table")])
    first = _desc(column, "zebra crossing")
    moved = _desc(table, "zebra crossing")
    assert first.id == moved.id and first.subject != moved.subject
    store.put_facts([first])
    assert [r for r, _ in store.search("zebra", 5)] == [tref("orders")]
    store.put_facts([moved])
    assert [r for r, _ in store.search("zebra", 5)] == [table]  # the old table's doc follows


def test_iter_schemas_streams(store: Any) -> None:
    store.upsert_schemas([Schema(None, f"s{i:04d}") for i in range(2500)])
    sizes: list[int] = []
    real = store._q

    def spy(*a: Any, **kw: Any) -> Any:
        rows = real(*a, **kw)
        sizes.append(len(rows))
        return rows

    store._q = spy
    try:
        it = store.iter_schemas()
        first = next(it)
        store.upsert_schemas([Schema(None, "zzz")])  # writes interleave with an open iterator
        names = [first.name, *(s.name for s in it)]
    finally:
        del store._q
    assert names[:2] == ["s0000", "s0001"] and len(names) >= 2500
    assert names == sorted(names) and len(set(names)) == len(names)
    assert sizes and max(sizes) < 2500  # read in pages, never one fetch of every row


def test_glossary_terms_searchable(store: Any) -> None:
    store.upsert_tables([tbl("orders"), tbl("payments"), tbl("misc")])
    glob = ObjectRef(None, None, "_global")
    term = Fact.make(
        FactKind.GLOSSARY, glob,
        {"term": "net revenue", "definition": "paid minus refunded",
         "maps_to": [tref("orders").to_dict(), tref("payments").to_dict()]},
        Provenance.HUMAN, Status.UNVERIFIED,
    )  # fmt: skip
    assert store.search("net revenue", 5) == []
    store.put_facts([term])
    assert sorted(r.table for r, _ in store.search("net revenue", 5)) == ["orders", "payments"]
    assert store.search("revenue", 5)[0][0] in (tref("orders"), tref("payments"))
    # the term stays searchable when the table is re-crawled, and goes when the term goes
    store.upsert_columns(tref("orders"), [col("orders", "id", 1)])
    assert "orders" in [r.table for r, _ in store.search("revenue", 5)]
    store.delete_facts([term.id])
    assert store.search("revenue", 5) == []


def test_snapshot_bytes_deterministic(store: Any) -> None:
    from decimal import Decimal

    from schemaguru._util.hashing import canonical_json

    facts = [
        Fact.make(FactKind.ROW_COUNT, tref(f"t{i}"), {"rows": i, "n": Decimal("1.50"),
                  "note": "café ✓", "nested": {"b": 1, "a": [2.5, None]}},
                  Provenance.CATALOG, Status.OBSERVED, observed_at=NOW)
        for i in range(30)
    ] + [_desc(tref("orders"), "Bestellungen für Kunden")]  # fmt: skip
    rows: list[list[tuple[Any, ...]]] = []
    for order in (facts, list(reversed(facts)), facts[7:] + facts[:7]):
        store.delete_facts([f.id for f in facts])
        for f in order:
            store.put_facts([f])
        sid = store.snapshot()
        got = _raw(store, "SELECT fact_id, data FROM snapshot_facts WHERE snapshot_id=? "
                   "ORDER BY fact_id", (sid,))  # fmt: skip
        assert {f.snapshot_id for f in store.snapshot_facts(sid)} == {sid}
        rows.append(got)
    assert rows[0] == rows[1] == rows[2]  # the same facts give the same bytes, whatever order
    by_id = {f.id: f for f in facts}
    assert [d for _, d in rows[0]] == [canonical_json(by_id[i].to_dict()) for i, _ in rows[0]]


# ----- lineage index (0.2.0 spec 4.5) -----------------------------------------------------------

_VIEW = ObjectRef(None, "shop", "v")


def _lin(
    upstream: list[str] | None,
    prov: Provenance,
    source: str,
    *,
    ignore: bool = False,
    subject: ObjectRef = _VIEW,
) -> Fact:
    from schemaguru.model.lineage import table_lineage_value

    value = table_lineage_value(upstream, ignore=ignore)
    return Fact.make(FactKind.LINEAGE, subject, value, prov, Status.UNVERIFIED, source=source)


def _edge_ids(store: Any, keys: list[str], direction: str = "upstream") -> list[str]:
    return [e.fact_id for e in store.lineage_edges(keys, direction=direction, level="table")]


def test_lineage_edges_follow_facts(store: Any) -> None:
    from schemaguru.model.lineage import LineageEdge
    from schemaguru.store.protocol import lineage_index

    assert lineage_index(store) is store
    dbt = _lin(["shop.orders"], Provenance.DECLARED, "dbt:manifest:model.shop.v")
    view = _lin(["shop.orders", "shop.refunds"], Provenance.CATALOG, "view_definition")
    assert store.lineage_edges(["shop.v"], direction="upstream", level="table") == []
    store.put_facts([dbt, view])
    edges = store.lineage_edges(["shop.v"], direction="upstream", level="table")
    assert {e.fact_id for e in edges} == {dbt.id, view.id}
    assert edges == sorted(edges, key=lambda e: (e.downstream_key, e.upstream_key, e.fact_id))
    assert LineageEdge("shop.refunds", "shop.v", "table", view.id) in edges
    assert _edge_ids(store, ["shop.refunds"], "downstream") == [view.id]
    assert store.lineage_edges(["shop.v"], direction="upstream", level="column") == []
    assert store.lineage_edges([], direction="downstream", level="table") == []
    # deleting a fact drops its edges
    store.delete_facts([view.id])
    assert _edge_ids(store, ["shop.v"]) == [dbt.id]


def test_lineage_edges_effective_only(store: Any) -> None:
    dbt = _lin(["shop.orders"], Provenance.DECLARED, "dbt:manifest:model.shop.v")
    store.put_facts([dbt])
    ignore = _lin(None, Provenance.HUMAN, "overlay:lineage.yml:1", ignore=True)
    store.put_facts([ignore])
    assert _edge_ids(store, ["shop.v"]) == []
    assert _edge_ids(store, ["shop.orders"], "downstream") == []
    store.delete_facts([ignore.id])
    assert _edge_ids(store, ["shop.v"]) == [dbt.id]
    # a human list replaces the parsed one; its own edges are the effective ones
    human = _lin(["shop.payments"], Provenance.HUMAN, "overlay:lineage.yml:2")
    store.put_facts([human])
    assert _edge_ids(store, ["shop.v"]) == [human.id]
    assert _edge_ids(store, ["shop.orders"], "downstream") == []


def test_lineage_edges_same_transaction(store: Any) -> None:
    dbt = _lin(["shop.orders"], Provenance.DECLARED, "dbt:manifest:model.shop.v")
    with pytest.raises(RuntimeError), store.transaction():
        store.put_facts([dbt])
        assert _edge_ids(store, ["shop.v"]) == [dbt.id]  # visible inside the transaction
        raise RuntimeError("boom")
    assert _edge_ids(store, ["shop.v"]) == []
    assert store.facts_for([_VIEW], None) == []


def test_lineage_edges_many_keys_chunked(store: Any) -> None:
    facts = [
        _lin(["shop.orders"], Provenance.DECLARED, "dbt:manifest:x", subject=tref(f"v{i:04d}"))
        for i in range(1200)
    ]
    store.put_facts(facts)
    keys = [f"shop.v{i:04d}" for i in range(1200)]
    assert len(store.lineage_edges(keys, direction="upstream", level="table")) == 1200
    assert len(_edge_ids(store, ["shop.orders"], "downstream")) == 1200


def test_lineage_state_roundtrip(store: Any) -> None:
    a, b = tref("a"), tref("b")
    assert store.lineage_state_get([a, b]) == {}
    store.lineage_state_set({a: "1" * 64, b: "2" * 64})
    assert store.lineage_state_get([a, b, tref("c")]) == {a: "1" * 64, b: "2" * 64}
    store.lineage_state_set({a: None, b: "3" * 64})
    assert store.lineage_state_get([a, b]) == {b: "3" * 64}
    assert store.lineage_state_get([]) == {}


def test_lineage_facts_under_tables(store: Any) -> None:
    """The lineage facts of a table and of every column under it, whether or not the column
    is still stored; never those of another table that only shares the key prefix."""
    from schemaguru.model.lineage import column_lineage_value

    def col_lin(ref: ObjectRef) -> Fact:
        value = column_lineage_value(["shop.orders.id"], "copy")
        return Fact.make(
            FactKind.COLUMN_LINEAGE, ref, value, Provenance.CATALOG, Status.UNVERIFIED,
            source="view_definition",
        )  # fmt: skip

    table = _lin(["shop.orders"], Provenance.CATALOG, "view_definition")
    gone = col_lin(ObjectRef(None, "shop", "v", "dropped"))
    quoted = col_lin(ObjectRef(None, "shop", "v", "Odd Name"))
    nested = _lin(["shop.orders"], Provenance.CATALOG, "view_definition",
                  subject=ObjectRef("shop", "v", "x"))  # fmt: skip
    sibling = col_lin(ObjectRef(None, "shop", "v2", "a"))
    other = _desc(_VIEW, "not lineage")
    store.put_facts([table, gone, quoted, nested, sibling, other])
    got = store.lineage_facts_under([_VIEW])
    assert [f.id for f in got] == sorted([table.id, gone.id, quoted.id])
    assert store.lineage_facts_under([]) == []
