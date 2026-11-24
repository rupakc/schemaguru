"""Edge cases of the MySQL, ClickHouse, SQL Server and Trino readers that a recorded fixture
does not pin."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from schemaguru.catalog.readers.clickhouse import _family, _kind, _sorting_columns, _unwrap
from schemaguru.catalog.readers.mysql import MySQLReader, _default
from schemaguru.catalog.readers.trino import TrinoReader, _q
from schemaguru.catalog.readers.tsql import _native_type
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Schema


class Recorder:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.rows = rows or []

    def run(
        self, sql: str, params: Any = (), *, timeout_s: float | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, tuple(params)))
        return self.rows


def test_clickhouse_unwrap_and_family() -> None:
    assert _unwrap("LowCardinality(Nullable(String))") == ("String", True)
    assert _unwrap("Array(Nullable(String))") == ("Array(Nullable(String))", False)
    assert _family("Bool") == "boolean" and _family("UInt64") == "integer"
    assert _family("Decimal(10, 2)") == "decimal" and _family("SimpleAggregateFunction") == "other"


def test_clickhouse_kind_and_sorting_key() -> None:
    assert _kind("MergeTree") == "table" and _kind("ReplicatedReplacingMergeTree") == "table"
    assert _kind("View") == "view" and _kind("MaterializedView") == "materialized_view"
    assert _kind("S3") == "external" and _kind("SomethingNew") == "other"
    assert _sorting_columns("a, b") == ("a", "b")
    assert _sorting_columns("toDate(a)") is None and _sorting_columns("") is None


def test_clickhouse_constraints_are_never_invented() -> None:
    from schemaguru.catalog.readers.clickhouse import ClickHouseReader

    ref = ObjectRef(None, "d", "t")
    assert ClickHouseReader(Recorder()).constraints([ref]) == {ref: []}


def test_mariadb_default_normalised_only_for_mariadb() -> None:
    assert _default("NULL", True) is None
    assert _default("'it''s'", True) == "it's"
    assert _default("current_timestamp()", True) == "current_timestamp()"
    assert _default("NULL", False) == "NULL"  # in MySQL that is a real string default


def test_mysql_incremental_binds_utc_naive_and_never_formats_values() -> None:
    ex = Recorder()
    since = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    MySQLReader(ex).list_tables(Schema(None, "x'; DROP"), page_token=None, changed_since=since)
    sql, params = ex.calls[0]
    assert "DROP" not in sql and params[0] == "x'; DROP"
    assert params[4] == datetime(2026, 1, 2, 3, 4, 5) and params[4].tzinfo is None


def test_tsql_native_type_text() -> None:
    assert _native_type("nvarchar", 510, 0, 0) == "nvarchar(255)"
    assert _native_type("nvarchar", -1, 0, 0) == "nvarchar(max)"
    assert _native_type("varchar", 100, 0, 0) == "varchar(100)"
    assert _native_type("decimal", 9, 10, 2) == "decimal(10,2)"
    assert _native_type("datetime2", 8, 27, 3) == "datetime2(3)"
    assert _native_type("int", 4, 10, 0) == "int"


def test_trino_catalog_is_quoted_and_values_are_bound() -> None:
    assert _q('we"ird') == '"we""ird"'
    ex = Recorder()
    TrinoReader(ex).list_schemas('c"x', page_token=None)
    sql, params = ex.calls[0]
    assert sql.startswith('SELECT schema_name FROM "c""x".information_schema.schemata')
    assert params == ("information_schema",)


def test_trino_without_a_catalog_reads_nothing() -> None:
    ex = Recorder()
    r = TrinoReader(ex)
    assert r.list_schemas(None, page_token=None).items == []
    assert r.list_tables(Schema(None, "s"), page_token=None, changed_since=None).items == []
    assert ex.calls == []


def test_trino_skips_the_system_catalog() -> None:
    ex = Recorder([{"Catalog": "system"}, {"Catalog": "hive"}, {"Catalog": "memory"}])
    assert list(TrinoReader(ex).list_catalogs()) == ["hive", "memory"]


@pytest.mark.parametrize("name", ["mysql", "clickhouse", "tsql", "trino"])
def test_registered_under_the_factlang_dialect_name(name: str) -> None:
    from schemaguru.catalog import dialects

    assert name in dialects()
