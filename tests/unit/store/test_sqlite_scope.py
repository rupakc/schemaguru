"""Store support for scoping (Task 13): kind push-down, name lookup, embedding side table."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
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
