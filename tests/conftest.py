"""Shared fixtures: the shop warehouse (DuckDB) and a store mirroring its DDL."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.shop import ShopFacts, build_big, build_shop, shop_catalog


@pytest.fixture(scope="session")
def shop_duckdb() -> Iterator[tuple[Any, ShopFacts]]:
    """Session-scoped in-memory DuckDB with schema ``shop`` (no ``big_t``); read-only use."""
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(":memory:")
    facts = build_shop(conn, "duckdb")
    yield conn, facts
    conn.close()


@pytest.fixture(scope="session")
def shop_duckdb_big() -> Iterator[Any]:
    """Separate connection holding only ``shop.big_t`` (2,000,000 rows), built lazily."""
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(":memory:")
    build_big(conn, "duckdb")
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def shop_facts(shop_duckdb: tuple[Any, ShopFacts]) -> ShopFacts:
    return shop_duckdb[1]


@pytest.fixture
def shop_store(tmp_path: Path, shop_facts: ShopFacts) -> Iterator[Any]:
    """A SQLiteStore holding Schema/Table/Column/Constraint objects mirroring the shop DDL."""
    from schemaguru.store.sqlite import SQLiteStore

    schemas, tables, columns, cons = shop_catalog(shop_facts)
    with SQLiteStore(tmp_path / "shop.db") as store:
        store.upsert_schemas(schemas)
        store.upsert_tables(tables)
        for ref, cols in columns.items():
            store.upsert_columns(ref, cols)
            store.upsert_constraints(ref, cons[ref])
        yield store
