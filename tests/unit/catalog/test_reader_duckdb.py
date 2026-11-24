from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest

from schemaguru.catalog import DBAPIExecutor, crawl, dialects, reader_for
from schemaguru.catalog.readers.duckdb import DuckDBReader
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table
from schemaguru.store.sqlite import SQLiteStore


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    c = duckdb.connect()
    c.execute(
        "CREATE TABLE customers(id INTEGER PRIMARY KEY, email VARCHAR UNIQUE NOT NULL, "
        "note VARCHAR DEFAULT 'x', score DECIMAL(10,2), seen TIMESTAMP)"
    )
    c.execute(
        'CREATE TABLE "Order Items"(id INTEGER, customer_id INTEGER REFERENCES customers(id), '
        "PRIMARY KEY (id, customer_id))"
    )
    c.execute("CREATE TABLE orders_staging_1(id INTEGER)")
    c.execute("CREATE VIEW v_customers AS SELECT id, email FROM customers")
    c.execute("COMMENT ON TABLE customers IS 'people who buy'")
    c.execute("COMMENT ON COLUMN customers.email IS 'login address'")
    c.execute("INSERT INTO customers VALUES (1, 'a@x', 'n', 1.5, NULL), (2, 'b@x', 'n', 2.5, NULL)")
    yield c
    c.close()


@pytest.fixture
def reader(conn: duckdb.DuckDBPyConnection) -> DuckDBReader:
    return DuckDBReader(DBAPIExecutor(conn))


def test_registry_builds_duckdb(conn: duckdb.DuckDBPyConnection) -> None:
    assert "duckdb" in dialects() and "postgres" in dialects()
    assert isinstance(reader_for("duckdb", DBAPIExecutor(conn)), DuckDBReader)


def test_reader_duckdb(reader: DuckDBReader) -> None:
    assert list(reader.list_catalogs()) == []
    schemas = reader.list_schemas(None, page_token=None)
    assert schemas.next_token is None
    assert Schema(None, "main") in schemas.items
    assert all(s.name not in ("information_schema", "pg_catalog") for s in schemas.items)

    page = reader.list_tables(Schema(None, "main"), page_token=None, changed_since=None)
    assert page.next_token is None
    by = {t.ref.table: t for t in page.items}
    assert set(by) == {"customers", "Order Items", "orders_staging_1", "v_customers"}
    assert by["customers"] == Table(
        ObjectRef(None, "main", "customers"), "table", "people who buy", row_count=2
    )
    assert by["v_customers"].kind == "view" and by["v_customers"].row_count is None
    assert by["Order Items"].ref.canonical() == 'main."Order Items"'

    refs = [by["customers"].ref, by["Order Items"].ref]
    cols = reader.columns(refs)
    c = cols[by["customers"].ref]
    assert c[0] == Column(
        ObjectRef(None, "main", "customers", "id"), 1, "INTEGER", "integer", False
    )
    assert c[1] == Column(
        ObjectRef(None, "main", "customers", "email"),
        2,
        "VARCHAR",
        "string",
        False,
        "login address",
    )
    assert c[2].default == "'x'" and c[2].nullable is True
    assert (c[3].native_type, c[3].family) == ("DECIMAL(10,2)", "decimal")
    assert c[4].family == "timestamp"
    oi = cols[by["Order Items"].ref]
    assert [x.ref.column for x in oi] == ["id", "customer_id"]

    cons = reader.constraints(refs)
    cust = by["customers"].ref
    assert Constraint("primary_key", cust, ("id",), enforced=True) in cons[cust]
    assert Constraint("unique", cust, ("email",), enforced=True) in cons[cust]
    oi_ref = by["Order Items"].ref
    assert Constraint("primary_key", oi_ref, ("id", "customer_id"), enforced=True) in cons[oi_ref]
    assert Constraint("foreign_key", oi_ref, ("customer_id",), cust, ("id",), True) in cons[oi_ref]
    assert len(cons[cust]) == 2  # NOT NULL / CHECK are not structural constraints here


def test_paging_by_keyset(conn: duckdb.DuckDBPyConnection) -> None:
    for i in range(5):
        conn.execute(f"CREATE TABLE p{i}(id INTEGER)")
    r = DuckDBReader(DBAPIExecutor(conn), page_size=2)
    seen: list[str] = []
    token = None
    while True:
        page = r.list_tables(Schema(None, "main"), page_token=token, changed_since=None)
        assert len(page.items) <= 2
        seen += [t.ref.table for t in page.items]
        token = page.next_token
        if token is None:
            break
    assert len(seen) == len(set(seen)) == 9
    assert seen == sorted(seen)


def test_crawl_duckdb_end_to_end(tmp_path, reader: DuckDBReader) -> None:  # type: ignore[no-untyped-def]
    store = SQLiteStore(tmp_path / "d.db")
    try:
        report = crawl(reader, store)
        assert not report.errors
        names = {t.ref.table for t in store.iter_tables()}
        assert names == {"customers", "Order Items", "v_customers"}  # staging excluded
        ref, _ = store.resolve('main."Order Items"')
        assert ref is not None and len(store.columns(ref)) == 2
        assert len(store.constraints(ref)) == 2
    finally:
        store.close()
