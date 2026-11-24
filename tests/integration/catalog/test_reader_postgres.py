from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from schemaguru.catalog import DBAPIExecutor, crawl
from schemaguru.catalog.readers.postgres import PostgresReader
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema

DSN = os.environ.get("SCHEMAGURU_TEST_POSTGRES_DSN")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="SCHEMAGURU_TEST_POSTGRES_DSN is not set"),
]


@pytest.fixture
def conn() -> Iterator[Any]:
    psycopg = pytest.importorskip("psycopg")
    c = psycopg.connect(DSN, autocommit=True)
    c.execute("DROP SCHEMA IF EXISTS sg_test CASCADE")
    c.execute("CREATE SCHEMA sg_test")
    c.execute("COMMENT ON SCHEMA sg_test IS 'test schema'")
    c.execute(
        "CREATE TABLE sg_test.customers(id integer PRIMARY KEY, email text UNIQUE NOT NULL, "
        "note text DEFAULT 'x', score numeric(10,2), seen timestamptz)"
    )
    c.execute(
        'CREATE TABLE sg_test."Order Items"(id integer, customer_id integer '
        "REFERENCES sg_test.customers(id), PRIMARY KEY (id, customer_id))"
    )
    c.execute("CREATE VIEW sg_test.v_customers AS SELECT id, email FROM sg_test.customers")
    c.execute("COMMENT ON TABLE sg_test.customers IS 'people who buy'")
    c.execute("COMMENT ON COLUMN sg_test.customers.email IS 'login address'")
    c.execute("INSERT INTO sg_test.customers VALUES (1, 'a@x', 'n', 1.5, NULL)")
    c.execute("ANALYZE sg_test.customers")
    yield c
    c.execute("DROP SCHEMA sg_test CASCADE")
    c.close()


def test_reader_postgres(conn: Any) -> None:
    r = PostgresReader(DBAPIExecutor(conn, "format"))
    assert list(r.list_catalogs()) == []
    schemas = {s.name: s for s in r.list_schemas(None, page_token=None).items}
    assert schemas["sg_test"] == Schema(None, "sg_test", "test schema", 3)
    assert "information_schema" not in schemas and "pg_catalog" not in schemas

    page = r.list_tables(Schema(None, "sg_test"), page_token=None, changed_since=None)
    by = {t.ref.table: t for t in page.items}
    assert set(by) == {"customers", "Order Items", "v_customers"}
    assert by["customers"].kind == "table" and by["customers"].comment == "people who buy"
    assert by["customers"].row_count == 1 and (by["customers"].bytes or 0) > 0
    assert by["v_customers"].kind == "view" and by["v_customers"].bytes is None

    cust = by["customers"].ref
    oi = by["Order Items"].ref
    cols = r.columns([cust, oi, by["v_customers"].ref])
    assert cols[cust][0] == Column(
        ObjectRef(None, "sg_test", "customers", "id"), 1, "integer", "integer", False
    )
    assert cols[cust][1].comment == "login address" and cols[cust][1].nullable is False
    assert cols[cust][2].default == "'x'::text" and cols[cust][2].nullable is True
    assert (cols[cust][3].native_type, cols[cust][3].family) == ("numeric(10,2)", "decimal")
    assert cols[cust][4].family == "timestamp"
    assert all(c.nullable is None for c in cols[by["v_customers"].ref])

    cons = r.constraints([cust, oi])
    assert Constraint("primary_key", cust, ("id",), enforced=True) in cons[cust]
    assert Constraint("unique", cust, ("email",), enforced=True) in cons[cust]
    assert Constraint("primary_key", oi, ("id", "customer_id"), enforced=True) in cons[oi]
    assert Constraint("foreign_key", oi, ("customer_id",), cust, ("id",), True) in cons[oi]


def test_postgres_paging_and_crawl(conn: Any, tmp_path: Any) -> None:
    from schemaguru.config import CrawlConfig
    from schemaguru.store.sqlite import SQLiteStore

    for i in range(5):
        conn.execute(f"CREATE TABLE sg_test.p{i}(id integer)")
    r = PostgresReader(DBAPIExecutor(conn, "format"), page_size=2)
    store = SQLiteStore(tmp_path / "pg.db")
    try:
        report = crawl(r, store, config=CrawlConfig(page_size=2, include=("sg_test.*",)))
        assert not report.errors
        assert len(list(store.iter_tables())) == 8
    finally:
        store.close()
