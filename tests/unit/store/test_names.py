from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from schemaguru.model.lineage import ResolvedTable
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Table
from schemaguru.store.names import resolve_relation_names
from schemaguru.store.sqlite import SQLiteStore

VIEW = ObjectRef(None, "shop", "v")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    s = SQLiteStore(tmp_path / "n.db")
    for schema, name in [
        ("shop", "orders"),
        ("shop", "Order Items"),
        ("ops", "Orders"),
        ("ops", "orders"),
        ("shop", "gone"),
    ]:
        ref = ObjectRef(None, schema, name)
        s.upsert_tables([Table(ref, "table")])
        s.upsert_columns(
            ref,
            [
                Column(ObjectRef(None, schema, name, "id"), 1, "INT", "integer"),
                Column(ObjectRef(None, schema, name, "b"), 2, "INT", "integer"),
            ],
        )
    s.mark_withdrawn([ObjectRef(None, "shop", "gone")], datetime(2026, 1, 1, tzinfo=timezone.utc))
    yield s
    s.close()


def test_relation_names_case_and_quoting(store: SQLiteStore) -> None:
    upper = (None, "SHOP", "ORDERS")
    spaced = (None, "shop", "Order Items")
    ambiguous = (None, "ops", "ORDERS")
    out = resolve_relation_names(store, [upper, spaced, ambiguous], VIEW)
    assert out[upper] == ResolvedTable("shop.orders", ("id", "b"))
    assert out[spaced] == ResolvedTable('shop."Order Items"', ("id", "b"))
    assert ambiguous not in out


def test_exact_wins_over_case_insensitive(store: SQLiteStore) -> None:
    out = resolve_relation_names(store, [(None, "ops", "Orders")], VIEW)
    assert out[(None, "ops", "Orders")].ref == "ops.Orders"


def test_bare_name_uses_the_default_schema(store: SQLiteStore) -> None:
    assert (
        resolve_relation_names(store, [(None, None, "orders")], VIEW)[(None, None, "orders")].ref
        == "shop.orders"
    )
    other = ObjectRef(None, "ops", "v")
    assert (None, None, "ORDERS") not in resolve_relation_names(
        store, [(None, None, "ORDERS")], other
    )


def test_unknown_withdrawn_and_three_part_are_unresolved(store: SQLiteStore) -> None:
    names = [(None, "staging_raw", "orders"), (None, "shop", "gone"), ("db", "shop", "orders")]
    assert resolve_relation_names(store, names, VIEW) == {}


def test_two_part_name_is_not_read_as_table_dot_column(store: SQLiteStore) -> None:
    # "shop.id" must not resolve to the column id of a table called shop
    store.upsert_tables([Table(ObjectRef(None, None, "shop"), "table")])
    store.upsert_columns(
        ObjectRef(None, None, "shop"),
        [Column(ObjectRef(None, None, "shop", "id"), 1, "INT", "integer")],
    )
    assert resolve_relation_names(store, [(None, "shop", "id")], VIEW) == {}
