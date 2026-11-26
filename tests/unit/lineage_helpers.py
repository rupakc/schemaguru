"""Shared lineage test fixtures (Tasks 14, 18, 22): a DuckDB view whose definition carries
literals that must never leave memory, and a two-table Guru tied only by lineage."""

from __future__ import annotations

from pathlib import Path

import duckdb

from schemaguru.catalog import DBAPIExecutor
from schemaguru.catalog.readers.duckdb import DuckDBReader
from schemaguru.guru import Guru
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.lineage import VIEW_SOURCE, table_lineage_value
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Table
from schemaguru.store.sqlite import SQLiteStore

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


def alpha_beta_guru(tmp_path: Path) -> Guru:
    """A Guru over ``shop.alpha`` and ``shop.beta``: no JOIN facts, and ``LINEAGE beta <- alpha``
    (a parsed view link). Lineage is the only thing tying the two tables together."""
    path = tmp_path / "alpha_beta.db"
    store = SQLiteStore(path)
    for name in ("alpha", "beta"):
        ref = ObjectRef(None, "shop", name)
        store.upsert_tables([Table(ref, "table")])
        store.upsert_columns(
            ref, [Column(ObjectRef(None, "shop", name, "id"), 1, "INT", "INT", True, None, None)]
        )
    store.put_facts(
        [
            Fact.make(
                FactKind.LINEAGE,
                ObjectRef(None, "shop", "beta"),
                table_lineage_value(["shop.alpha"]),
                Provenance.CATALOG,
                Status.UNVERIFIED,
                source=VIEW_SOURCE,
            )
        ]
    )
    store.close()
    return Guru(store=path)
