"""Shared lineage test warehouse (Tasks 14, 18, 22): a DuckDB view whose definition carries
literals that must never leave memory."""

from __future__ import annotations

from pathlib import Path

import duckdb

from schemaguru.catalog import DBAPIExecutor
from schemaguru.catalog.readers.duckdb import DuckDBReader

# The id literal has 11 digits, so it cannot turn up by chance in a sha256 hex string.
LITERALS = ("maria.garcia@example.com", "+1 415 555 0100", "73914286501", "hunter2secret")

VIP_VIEW = (
    "CREATE VIEW shop.v_vip AS SELECT * FROM shop.customers "
    "WHERE email = 'maria.garcia@example.com' AND phone = '+1 415 555 0100' "
    "AND customer_id = 73914286501 AND note <> 'hunter2secret'"
)


def privacy_warehouse(tmp_path: Path) -> tuple[DuckDBReader, tuple[str, ...]]:
    """A DuckDB file warehouse in ``tmp_path`` with ``shop.customers`` and the view
    ``shop.v_vip``; returns its reader and the literals of the view definition."""
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA shop")
    conn.execute(
        "CREATE TABLE shop.customers(customer_id BIGINT, email VARCHAR, phone VARCHAR, "
        "note VARCHAR)"
    )
    conn.execute(VIP_VIEW)
    return DuckDBReader(DBAPIExecutor(conn)), LITERALS
