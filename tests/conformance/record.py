"""Record a fixture: ``python -m tests.conformance.record duckdb`` (see the fixtures README)."""

from __future__ import annotations

import sys

from schemaguru.catalog import DBAPIExecutor, reader_for
from tests.conformance.replay import record


def _duckdb() -> None:
    import duckdb

    from tests.fixtures.shop import build_shop

    conn = duckdb.connect(":memory:")
    build_shop(conn, "duckdb")
    # Shapes the shop lacks: a view, comments, a second schema, an odd identifier, a FK.
    conn.execute("CREATE VIEW shop.v_open_orders AS SELECT order_id, status FROM shop.orders")
    conn.execute("COMMENT ON TABLE shop.customers IS 'people who buy'")
    conn.execute("COMMENT ON COLUMN shop.customers.email IS 'login address'")
    conn.execute("CREATE SCHEMA ops")
    conn.execute('CREATE TABLE ops."Order Notes"(note_id INTEGER, body VARCHAR)')
    conn.execute(
        'CREATE TABLE shop."Customer Tags"(tag_id INTEGER, customer_id INTEGER '
        "REFERENCES shop.customers(customer_id), PRIMARY KEY (tag_id, customer_id))"
    )
    record(lambda ex: reader_for("duckdb", ex), DBAPIExecutor(conn), "duckdb")


RECORDERS = {"duckdb": _duckdb}

if __name__ == "__main__":
    RECORDERS[sys.argv[1]]()
