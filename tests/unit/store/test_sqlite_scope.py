"""Store support for scoping (Task 13): kind push-down, name lookup, embedding side table."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Table
from schemaguru.store import schema_sql
from schemaguru.store.sqlite import SQLiteStore

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


def tref(name: str, schema: str = "shop") -> ObjectRef:
    return ObjectRef(None, schema, name)


def _desc(ref: ObjectRef, text: str) -> Fact:
    return Fact.make(FactKind.DESCRIPTION, ref, {"text": text}, Provenance.HUMAN, Status.UNVERIFIED)


def _rows(ref: ObjectRef, n: int) -> Fact:
    return Fact.make(
        FactKind.ROW_COUNT, ref, {"rows": n}, Provenance.MEASURED, Status.OBSERVED, method="exact",
        observed_at=NOW,
    )  # fmt: skip


def test_facts_for_filters_kinds_in_sql(store: SQLiteStore) -> None:
    a, b = tref("orders"), tref("customers")
    store.put_facts([_desc(a, "x"), _rows(a, 3), _desc(b, "y")])
    got = store.facts_for([a, b], ["ROW_COUNT"])
    assert [f.kind.name for f in got] == ["ROW_COUNT"]
    assert store.facts_for([a], []) == []
    assert {f.kind.name for f in store.facts_for([a], None)} == {"DESCRIPTION", "ROW_COUNT"}
    # the query the store runs is served by the (subject_key, kind) index
    plan = store._q(
        "EXPLAIN QUERY PLAN SELECT id, data FROM facts WHERE subject_key IN (?) AND kind IN (?)",
        ("a", "b"),
    )
    assert any("facts_subject_kind" in str(row) for row in plan)


def test_tables_named_case_insensitive_live_only(store: SQLiteStore) -> None:
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


def test_tables_named_backfilled_by_migration(tmp_path: Path) -> None:
    """A version-1 store gains the name index on open, for tables written before it existed."""
    p = tmp_path / "v1.db"
    con = sqlite3.connect(p)
    con.execute(schema_sql.VERSION_DDL)
    for stmt in schema_sql.statements(schema_sql.migrations()[0][1]):
        con.execute(stmt)
    con.execute("INSERT INTO schema_version VALUES (1, '2026-11-24T00:00:00.000000Z')")
    data = '{"ref":{"catalog":null,"column":null,"schema":"shop","table":"Orders"},"kind":"table"}'
    con.execute(
        "INSERT INTO tables(ref, ref_lower, data) VALUES ('shop.Orders', 'shop.orders', ?)",
        (data,),
    )
    con.commit()
    con.close()
    with SQLiteStore(p) as s:
        assert s.tables_named(["orders"], limit=5) == [tref("Orders")]


def test_embeddings_roundtrip_live_only(store: SQLiteStore) -> None:
    store.upsert_tables([Table(tref("orders"), "table"), Table(tref("legacy"), "table")])
    store.embeddings_put(
        "m1",
        [(tref("orders"), "h1", [1.0, 0.5]), (tref("legacy"), "h2", [0.0, 1.0])],
    )
    store.embeddings_put("m2", [(tref("orders"), "h3", [2.0])])
    store.mark_withdrawn([tref("legacy")], NOW)
    rows = store.embeddings_get("m1")
    assert [(r, h, d) for r, h, d, _ in rows] == [(tref("orders"), "h1", 2)]
    import struct

    assert struct.unpack("<2f", rows[0][3]) == (1.0, 0.5)
    # replace by (model, ref)
    store.embeddings_put("m1", [(tref("orders"), "h9", [3.0, 3.0])])
    assert store.embeddings_hashes("m1") == {tref("orders"): "h9", tref("legacy"): "h2"}
    assert [h for _, h, _, _ in store.embeddings_get("m2")] == ["h3"]


def test_embeddings_reject_ragged(store: SQLiteStore) -> None:
    store.upsert_tables([Table(tref("orders"), "table")])
    with pytest.raises(ValueError, match="empty"):
        store.embeddings_put("m", [(tref("orders"), "h", [])])
