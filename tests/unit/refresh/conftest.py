from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from schemaguru.store.sqlite import SQLiteStore

duckdb = pytest.importorskip("duckdb")


@pytest.fixture
def conn() -> Iterator[Any]:
    c = duckdb.connect(":memory:")
    c.execute("CREATE SCHEMA shop")
    c.execute("CREATE TABLE shop.customers(customer_id INTEGER PRIMARY KEY, tier VARCHAR)")
    c.execute("CREATE TABLE shop.orders(order_id INTEGER PRIMARY KEY, customer_id INTEGER)")
    c.execute("INSERT INTO shop.customers VALUES (1,'a'),(2,'b')")
    c.execute("INSERT INTO shop.orders VALUES (10,1),(11,2),(12,1)")
    yield c
    c.close()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    with SQLiteStore(tmp_path / "r.db") as s:
        yield s
