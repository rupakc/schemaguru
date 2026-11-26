"""Warehouse readers against hand-built fake executors (every SQL passes the metadata guard)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from schemaguru.catalog import ReaderCapabilities, dialects, reader_for
from schemaguru.catalog.executor import check_metadata_sql
from schemaguru.catalog.protocol import Page
from schemaguru.catalog.readers.bigquery import MIN_BILLED_BYTES, BigQueryReader
from schemaguru.catalog.readers.databricks import DatabricksReader
from schemaguru.catalog.readers.oracle import OracleReader
from schemaguru.catalog.readers.redshift import RedshiftReader
from schemaguru.catalog.readers.snowflake import SnowflakeReader
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Constraint, Schema

UTC = timezone.utc


class Fake:
    """Answers the first rule whose substrings all occur in the SQL; records every call."""

    def __init__(self, *rules: tuple[Sequence[str], list[dict[str, Any]]]) -> None:
        self.rules = rules
        self.calls: list[tuple[str, tuple[Any, ...], float | None]] = []

    def run(
        self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
    ) -> list[dict[str, Any]]:
        check_metadata_sql(sql)
        assert sql.count("?") == len(params), sql
        self.calls.append((sql, tuple(params), timeout_s))
        for needles, rows in self.rules:
            if all(n.lower() in sql.lower() for n in needles):
                return rows
        raise AssertionError(f"unexpected SQL: {sql}")


def test_all_registered() -> None:
    assert {"bigquery", "snowflake", "redshift", "databricks", "oracle"} <= set(dialects())
    for d in ("bigquery", "snowflake", "redshift", "databricks", "oracle"):
        assert reader_for(d, Fake()).dialect == d  # type: ignore[arg-type]


# ---- BigQuery ---------------------------------------------------------------------------
def _bq() -> tuple[BigQueryReader, Fake]:
    fake = Fake(
        (["SCHEMATA"], [{"catalog_name": "p1", "schema_name": "shop"}]),
        (
            ["TABLES", "TABLE_STORAGE"],
            [
                {
                    "table_name": "orders",
                    "table_type": "BASE TABLE",
                    "creation_time": datetime(2026, 1, 2, tzinfo=UTC),
                    "description": '"all \\"paid\\" orders"',
                    "total_rows": 10,
                    "total_logical_bytes": 2048,
                    "last_modified": datetime(2026, 2, 1, tzinfo=UTC),
                },
                {
                    "table_name": "v_open",
                    "table_type": "VIEW",
                    "creation_time": None,
                    "description": None,
                    "total_rows": None,
                    "total_logical_bytes": None,
                    "last_modified": None,
                },
            ],
        ),
        (
            ["COLUMNS", "COLUMN_FIELD_PATHS"],
            [
                {
                    "table_name": "orders",
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "INT64",
                    "is_nullable": "NO",
                    "column_default": "NULL",
                    "description": "pk",
                },
                {
                    "table_name": "orders",
                    "column_name": "tags",
                    "ordinal_position": 2,
                    "data_type": "ARRAY<STRING>",
                    "is_nullable": "YES",
                    "column_default": "'x'",
                    "description": None,
                },
            ],
        ),
        (
            ["KEY_COLUMN_USAGE"],
            [
                {
                    "table_name": "items",
                    "constraint_name": "pk",
                    "constraint_type": "PRIMARY KEY",
                    "enforced": "NO",
                    "column_name": "order_id",
                    "ordinal_position": 1,
                    "unique_position": None,
                    "ref_catalog": None,
                    "ref_schema": None,
                    "ref_table": None,
                    "ref_column": None,
                    "ref_position": None,
                },
                {
                    "table_name": "items",
                    "constraint_name": "pk",
                    "constraint_type": "PRIMARY KEY",
                    "enforced": "NO",
                    "column_name": "line_no",
                    "ordinal_position": 2,
                    "unique_position": None,
                    "ref_catalog": None,
                    "ref_schema": None,
                    "ref_table": None,
                    "ref_column": None,
                    "ref_position": None,
                },
                {
                    "table_name": "items",
                    "constraint_name": "fk",
                    "constraint_type": "FOREIGN KEY",
                    "enforced": "NO",
                    "column_name": "order_id",
                    "ordinal_position": 1,
                    "unique_position": 1,
                    "ref_catalog": "p1",
                    "ref_schema": "shop",
                    "ref_table": "orders",
                    "ref_column": "id",
                    "ref_position": 1,
                },
            ],
        ),
    )
    return BigQueryReader(fake, project="p1", region="eu"), fake  # type: ignore[arg-type]


def test_bigquery_reads_and_counts_billed_bytes() -> None:
    r, fake = _bq()
    assert list(r.list_catalogs()) == ["p1"]
    schemas = r.list_schemas(None, page_token=None).items
    assert schemas == [Schema("p1", "shop")]
    assert "`p1`.`region-eu`.INFORMATION_SCHEMA.SCHEMATA" in fake.calls[0][0]
    tables = r.list_tables(schemas[0], page_token=None, changed_since=None).items
    o, v = tables
    assert o.comment == 'all "paid" orders' and o.kind == "table"
    assert (o.row_count, o.bytes) == (10, 2048)
    assert o.last_altered == datetime(2026, 2, 1, tzinfo=UTC)
    assert v.kind == "view" and v.row_count is None and v.created_at is None
    cols = r.columns([ObjectRef("p1", "shop", "orders")])[ObjectRef("p1", "shop", "orders")]
    assert [c.family for c in cols] == ["integer", "array"]
    assert cols[0].comment == "pk" and cols[0].default is None  # BigQuery's "NULL" text
    assert cols[1].default == "'x'" and cols[0].nullable is False
    cons = r.constraints([ObjectRef("p1", "shop", "items")])[ObjectRef("p1", "shop", "items")]
    assert (
        Constraint(
            "primary_key",
            ObjectRef("p1", "shop", "items"),
            ("order_id", "line_no"),
            None,
            None,
            False,
        )
        in cons
    )
    fk = next(c for c in cons if c.kind == "foreign_key")
    assert fk.ref_table == ObjectRef("p1", "shop", "orders") and fk.ref_columns == ("id",)
    assert r.bytes_billed == len(fake.calls) * MIN_BILLED_BYTES
    assert r.bytes_billed_is_lower_bound


def test_bigquery_uses_executor_bytes_when_offered() -> None:
    r, fake = _bq()
    fake.last_bytes_billed = 12_345_678  # type: ignore[attr-defined]
    r.list_schemas(None, page_token=None)
    assert r.bytes_billed == 12_345_678 and not r.bytes_billed_is_lower_bound


def test_bigquery_batches_one_query_per_dataset_and_binds_values() -> None:
    r, fake = _bq()
    refs = [ObjectRef("p1", "shop", n) for n in ("a", "b", "c")] + [ObjectRef("p1", "ops", "x")]
    r.columns(refs)
    assert len(fake.calls) == 2
    assert fake.calls[0][1] == ("a", "b", "c") and "'a'" not in fake.calls[0][0]


def test_bigquery_rejects_unsafe_identifiers() -> None:
    from schemaguru.catalog.protocol import CatalogError

    with pytest.raises(CatalogError):
        BigQueryReader(Fake(), project="p`; DROP")  # type: ignore[arg-type]
    r, _ = _bq()
    with pytest.raises(CatalogError):
        r.columns([ObjectRef("p1", "sh`op", "t")])


def test_bigquery_unresolvable_fk_is_dropped_not_guessed() -> None:
    fake = Fake(
        (
            ["KEY_COLUMN_USAGE"],
            [
                {
                    "table_name": "t",
                    "constraint_name": "fk",
                    "constraint_type": "FOREIGN KEY",
                    "enforced": "NO",
                    "column_name": "x",
                    "ordinal_position": 1,
                    "unique_position": 1,
                    "ref_catalog": "p1",
                    "ref_schema": "other",
                    "ref_table": "u",
                    "ref_column": "id",
                    "ref_position": None,
                }
            ],
        )
    )
    r = BigQueryReader(fake, project="p1")  # type: ignore[arg-type]
    assert r.constraints([ObjectRef("p1", "shop", "t")])[ObjectRef("p1", "shop", "t")] == []


# ---- Snowflake --------------------------------------------------------------------------
def test_snowflake() -> None:
    fake = Fake(
        (["SCHEMATA"], [{"SCHEMA_NAME": "PUBLIC", "COMMENT": None}]),
        (
            ["INFORMATION_SCHEMA.TABLES"],
            [
                {
                    "TABLE_NAME": "ORDERS",
                    "TABLE_TYPE": "BASE TABLE",
                    "ROW_COUNT": 5,
                    "BYTES": 100,
                    "CREATED": datetime(2026, 1, 1, tzinfo=UTC),
                    "LAST_ALTERED": datetime(2026, 1, 3, tzinfo=UTC),
                    "COMMENT": "orders",
                    "CLUSTERING_KEY": "LINEAR(CUSTOMER_ID, CREATED_AT)",
                },
                {
                    "TABLE_NAME": "Mixed Case",
                    "TABLE_TYPE": "VIEW",
                    "ROW_COUNT": None,
                    "BYTES": None,
                    "CREATED": None,
                    "LAST_ALTERED": None,
                    "COMMENT": None,
                    "CLUSTERING_KEY": "LINEAR(TO_DATE(X))",
                },
            ],
        ),
        (
            ["INFORMATION_SCHEMA.COLUMNS"],
            [
                {
                    "TABLE_NAME": "ORDERS",
                    "COLUMN_NAME": "ID",
                    "ORDINAL_POSITION": 1,
                    "DATA_TYPE": "NUMBER",
                    "IS_NULLABLE": "NO",
                    "COLUMN_DEFAULT": None,
                    "COMMENT": "pk",
                    "NUMERIC_PRECISION": 38,
                    "NUMERIC_SCALE": 0,
                    "CHARACTER_MAXIMUM_LENGTH": None,
                },
                {
                    "TABLE_NAME": "ORDERS",
                    "COLUMN_NAME": "NOTE",
                    "ORDINAL_POSITION": 2,
                    "DATA_TYPE": "TEXT",
                    "IS_NULLABLE": "YES",
                    "COLUMN_DEFAULT": None,
                    "COMMENT": None,
                    "NUMERIC_PRECISION": None,
                    "NUMERIC_SCALE": None,
                    "CHARACTER_MAXIMUM_LENGTH": 16777216,
                },
            ],
        ),
        (
            ["SHOW PRIMARY KEYS"],
            [
                {
                    "table_name": "ORDER_ITEMS",
                    "column_name": "LINE_NO",
                    "key_sequence": 2,
                    "constraint_name": "PK1",
                },
                {
                    "table_name": "ORDER_ITEMS",
                    "column_name": "ORDER_ID",
                    "key_sequence": 1,
                    "constraint_name": "PK1",
                },
                {
                    "table_name": "OTHER",
                    "column_name": "X",
                    "key_sequence": 1,
                    "constraint_name": "PK2",
                },
            ],
        ),
        (["SHOW UNIQUE KEYS"], []),
        (
            ["SHOW IMPORTED KEYS"],
            [
                {
                    "pk_database_name": "DB1",
                    "pk_schema_name": "PUBLIC",
                    "pk_table_name": "ORDERS",
                    "pk_column_name": "ID",
                    "fk_table_name": "ORDER_ITEMS",
                    "fk_column_name": "ORDER_ID",
                    "key_sequence": 1,
                    "fk_name": "FK1",
                },
            ],
        ),
    )
    r = SnowflakeReader(fake, database="DB1")  # type: ignore[arg-type]
    assert list(r.list_catalogs()) == ["DB1"]
    [schema] = r.list_schemas(None, page_token=None).items
    assert schema == Schema("DB1", "PUBLIC")
    assert '"DB1".INFORMATION_SCHEMA.SCHEMATA' in fake.calls[0][0]
    t1, t2 = r.list_tables(schema, page_token=None, changed_since=None).items
    assert t1.clustering == ("CUSTOMER_ID", "CREATED_AT") and t2.clustering == ("TO_DATE(X)",)
    assert t2.ref.table == "Mixed Case" and t2.kind == "view" and t2.comment is None
    assert t1.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    cols = r.columns([ObjectRef("DB1", "PUBLIC", "ORDERS")])[ObjectRef("DB1", "PUBLIC", "ORDERS")]
    assert [(c.native_type, c.family) for c in cols] == [
        ("NUMBER(38,0)", "integer"),
        ("TEXT(16777216)", "string"),
    ]
    refs = [ObjectRef("DB1", "PUBLIC", "ORDER_ITEMS")]
    cons = r.constraints(refs)[refs[0]]
    pk = next(c for c in cons if c.kind == "primary_key")
    assert pk.columns == ("ORDER_ID", "LINE_NO") and pk.enforced is False
    fk = next(c for c in cons if c.kind == "foreign_key")
    assert fk.ref_table == ObjectRef("DB1", "PUBLIC", "ORDERS") and fk.ref_columns == ("ID",)
    assert any('SHOW PRIMARY KEYS IN SCHEMA "DB1"."PUBLIC"' in c[0] for c in fake.calls)


def test_snowflake_quotes_hostile_database_name() -> None:
    fake = Fake((["SCHEMATA"], []))
    SnowflakeReader(fake, database='A"B').list_schemas(None, page_token=None)  # type: ignore[arg-type]
    assert '"A""B".INFORMATION_SCHEMA' in fake.calls[0][0]


# ---- Redshift ---------------------------------------------------------------------------
def test_redshift_local_and_external() -> None:
    fake = Fake(
        (
            ["SVV_ALL_SCHEMAS"],
            [
                {"schema_name": "public", "table_count": 3},
                {"schema_name": "spectrum", "table_count": 1},
            ],
        ),
        (["CURRENT_DATABASE"], [{"name": "dev"}]),
        (
            ["FROM SVV_ALL_TABLES"],
            [
                {"table_name": "orders", "table_type": "TABLE", "remarks": "o"},
                {"table_name": "lake_events", "table_type": "EXTERNAL TABLE", "remarks": None},
                {"table_name": "v", "table_type": "VIEW", "remarks": None},
            ],
        ),
        (["FROM SVV_TABLE_INFO"], [{"table_name": "orders", "tbl_rows": 7, "size_mb": 3}]),
        (["FROM SVV_EXTERNAL_TABLES"], [{"table_name": "lake_events"}]),
        (
            ["FROM SVV_ALL_COLUMNS"],
            [
                {
                    "schema_name": "public",
                    "table_name": "orders",
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "is_nullable": "NO",
                    "column_default": None,
                    "remarks": None,
                    "character_maximum_length": None,
                    "numeric_precision": 64,
                    "numeric_scale": 0,
                },
                {
                    "schema_name": "public",
                    "table_name": "orders",
                    "column_name": "payload",
                    "ordinal_position": 2,
                    "data_type": "super",
                    "is_nullable": "YES",
                    "column_default": None,
                    "remarks": "json",
                    "character_maximum_length": None,
                    "numeric_precision": None,
                    "numeric_scale": None,
                },
            ],
        ),
        (
            ["SVV_EXTERNAL_COLUMNS"],
            [
                {
                    "schemaname": "spectrum",
                    "tablename": "lake_events",
                    "columnname": "ts",
                    "columnnum": 1,
                    "external_type": "timestamp",
                    "is_nullable": "true",
                }
            ],
        ),
        (
            ["pg_constraint"],
            [
                {
                    "schema_name": "public",
                    "table_name": "order_items",
                    "constraint_name": "pk",
                    "contype": "p",
                    "pos": 1,
                    "column_name": "order_id",
                    "ref_schema": None,
                    "ref_table": None,
                    "ref_column": None,
                },
                {
                    "schema_name": "public",
                    "table_name": "order_items",
                    "constraint_name": "pk",
                    "contype": "p",
                    "pos": 2,
                    "column_name": "line_no",
                    "ref_schema": None,
                    "ref_table": None,
                    "ref_column": None,
                },
                {
                    "schema_name": "public",
                    "table_name": "order_items",
                    "constraint_name": "fk",
                    "contype": "f",
                    "pos": 1,
                    "column_name": "order_id",
                    "ref_schema": "public",
                    "ref_table": "orders",
                    "ref_column": "id",
                },
            ],
        ),
    )
    r = RedshiftReader(fake)  # type: ignore[arg-type]
    schemas = r.list_schemas(None, page_token=None).items
    assert [s.name for s in schemas] == ["public", "spectrum"] and schemas[0].table_count == 3
    o, ext, v = r.list_tables(schemas[0], page_token=None, changed_since=None).items
    assert (o.row_count, o.bytes, o.comment) == (7, 3 * 1024 * 1024, "o")
    assert ext.kind == "external" and v.kind == "view"
    # leader-node and compute-node views are never joined in one statement
    for sql, _, _ in fake.calls:
        low = sql.lower()
        assert not ("svv_table_info" in low and "svv_all_tables" in low), sql
        assert not ("svv_table_info" in low and "svv_external_tables" in low), sql
    refs = [ObjectRef("dev", "public", "orders"), ObjectRef("dev", "spectrum", "lake_events")]
    cols = r.columns(refs)
    assert [c.family for c in cols[refs[0]]] == ["integer", "json"]
    assert [c.family for c in cols[refs[1]]] == ["timestamp"]  # via SVV_EXTERNAL_COLUMNS
    assert cols[refs[1]][0].nullable is True
    oi = ObjectRef("dev", "public", "order_items")
    cons = r.constraints([oi])[oi]
    assert cons[0].columns == ("order_id", "line_no")
    assert cons[1].ref_table == ObjectRef("dev", "public", "orders")


# ---- Databricks -------------------------------------------------------------------------
def test_databricks() -> None:
    fake = Fake(
        (["catalogs"], [{"catalog_name": "main"}, {"catalog_name": "lake"}]),
        (["schemata"], [{"catalog_name": "main", "schema_name": "shop", "comment": "c"}]),
        (
            ["information_schema.tables"],
            [
                {
                    "table_name": "orders",
                    "table_type": "MANAGED",
                    "comment": None,
                    "created": datetime(2026, 1, 1, tzinfo=UTC),
                    "last_altered": datetime(2026, 1, 2, tzinfo=UTC),
                },
                {
                    "table_name": "raw",
                    "table_type": "EXTERNAL",
                    "comment": "x",
                    "created": None,
                    "last_altered": None,
                },
                {
                    "table_name": "v",
                    "table_type": "VIEW",
                    "comment": None,
                    "created": None,
                    "last_altered": None,
                },
            ],
        ),
        (
            ["information_schema.columns"],
            [
                {
                    "table_catalog": "main",
                    "table_schema": "shop",
                    "table_name": "orders",
                    "column_name": "id",
                    "ordinal_position": 0,
                    "full_data_type": "bigint",
                    "is_nullable": "NO",
                    "column_default": None,
                    "comment": "pk",
                },
                {
                    "table_catalog": "main",
                    "table_schema": "shop",
                    "table_name": "orders",
                    "column_name": "attrs",
                    "ordinal_position": 1,
                    "full_data_type": "map<string,string>",
                    "is_nullable": "YES",
                    "column_default": None,
                    "comment": None,
                },
            ],
        ),
        (
            ["key_column_usage"],
            [
                {
                    "table_catalog": "main",
                    "table_schema": "shop",
                    "table_name": "items",
                    "constraint_name": "fk",
                    "constraint_type": "FOREIGN KEY",
                    "column_name": "order_id",
                    "ordinal_position": 1,
                    "ref_catalog": "main",
                    "ref_schema": "shop",
                    "ref_table": "orders",
                    "ref_column": "id",
                },
            ],
        ),
    )
    r = DatabricksReader(fake)  # type: ignore[arg-type]
    assert list(r.list_catalogs()) == ["main", "lake"]
    [s] = r.list_schemas(None, page_token=None).items
    assert s == Schema("main", "shop", "c")
    t = r.list_tables(s, page_token=None, changed_since=None).items
    assert [x.kind for x in t] == ["table", "external", "view"]
    assert t[0].row_count is None and t[0].last_altered == datetime(2026, 1, 2, tzinfo=UTC)
    ref = ObjectRef("main", "shop", "orders")
    cols = r.columns([ref])[ref]
    assert [c.family for c in cols] == ["integer", "struct"]
    assert [c.ordinal for c in cols] == [1, 2]  # Unity Catalog is 0-based; normalised
    items = ObjectRef("main", "shop", "items")
    [fk] = r.constraints([items])[items]
    assert fk.kind == "foreign_key" and fk.ref_table == ref and fk.enforced is False


# ---- Oracle -----------------------------------------------------------------------------
def test_oracle() -> None:
    fake = Fake(
        (["all_users"], [{"NAME": "SHOP", "TABLE_COUNT": 4}]),
        (
            ["all_tables"],
            [
                {
                    "TABLE_NAME": "ORDERS",
                    "KIND": "TABLE",
                    "NUM_ROWS": 99,
                    "COMMENTS": "o",
                    "PARTITIONING_TYPE": "RANGE",
                },
                {
                    "TABLE_NAME": "Order Items",
                    "KIND": "TABLE",
                    "NUM_ROWS": None,
                    "COMMENTS": None,
                    "PARTITIONING_TYPE": None,
                },
                {
                    "TABLE_NAME": "V_OPEN",
                    "KIND": "VIEW",
                    "NUM_ROWS": None,
                    "COMMENTS": None,
                    "PARTITIONING_TYPE": None,
                },
            ],
        ),
        (
            ["all_tab_columns"],
            [
                {
                    "OWNER": "SHOP",
                    "TABLE_NAME": "ORDERS",
                    "COLUMN_NAME": "ID",
                    "COLUMN_ID": 1,
                    "DATA_TYPE": "NUMBER",
                    "DATA_LENGTH": 22,
                    "CHAR_LENGTH": 0,
                    "DATA_PRECISION": 10,
                    "DATA_SCALE": 0,
                    "NULLABLE": "N",
                    "DATA_DEFAULT": None,
                    "COMMENTS": "pk",
                },
                {
                    "OWNER": "SHOP",
                    "TABLE_NAME": "ORDERS",
                    "COLUMN_NAME": "PLACED",
                    "COLUMN_ID": 2,
                    "DATA_TYPE": "DATE",
                    "DATA_LENGTH": 7,
                    "CHAR_LENGTH": 0,
                    "DATA_PRECISION": None,
                    "DATA_SCALE": None,
                    "NULLABLE": "Y",
                    "DATA_DEFAULT": "SYSDATE ",
                    "COMMENTS": None,
                },
                {
                    "OWNER": "SHOP",
                    "TABLE_NAME": "ORDERS",
                    "COLUMN_NAME": "NOTE",
                    "COLUMN_ID": 3,
                    "DATA_TYPE": "VARCHAR2",
                    "DATA_LENGTH": 400,
                    "CHAR_LENGTH": 100,
                    "DATA_PRECISION": None,
                    "DATA_SCALE": None,
                    "NULLABLE": "Y",
                    "DATA_DEFAULT": None,
                    "COMMENTS": None,
                },
            ],
        ),
        (
            ["all_constraints"],
            [
                {
                    "OWNER": "SHOP",
                    "TABLE_NAME": "Order Items",
                    "CONSTRAINT_NAME": "PK_OI",
                    "CONSTRAINT_TYPE": "P",
                    "STATUS": "ENABLED",
                    "COLUMN_NAME": "ORDER_ID",
                    "POSITION": 1,
                    "REF_OWNER": None,
                    "REF_TABLE": None,
                    "REF_COLUMN": None,
                },
                {
                    "OWNER": "SHOP",
                    "TABLE_NAME": "Order Items",
                    "CONSTRAINT_NAME": "PK_OI",
                    "CONSTRAINT_TYPE": "P",
                    "STATUS": "ENABLED",
                    "COLUMN_NAME": "LINE_NO",
                    "POSITION": 2,
                    "REF_OWNER": None,
                    "REF_TABLE": None,
                    "REF_COLUMN": None,
                },
                {
                    "OWNER": "SHOP",
                    "TABLE_NAME": "Order Items",
                    "CONSTRAINT_NAME": "FK_OI",
                    "CONSTRAINT_TYPE": "R",
                    "STATUS": "DISABLED",
                    "COLUMN_NAME": "ORDER_ID",
                    "POSITION": 1,
                    "REF_OWNER": "SHOP",
                    "REF_TABLE": "ORDERS",
                    "REF_COLUMN": "ID",
                },
            ],
        ),
    )
    r = OracleReader(fake)  # type: ignore[arg-type]
    assert list(r.list_catalogs()) == []
    [s] = r.list_schemas(None, page_token=None).items
    assert s == Schema(None, "SHOP", None, 4)
    o, oi, v = r.list_tables(s, page_token=None, changed_since=None).items
    assert (o.row_count, o.partitioning, o.comment) == (99, "RANGE", "o")
    assert oi.ref.canonical() == 'SHOP."Order Items"' and v.kind == "view"
    assert "FETCH FIRST" in fake.calls[-1][0] and fake.calls[-1][1][-1] == 1001
    ref = ObjectRef(None, "SHOP", "ORDERS")
    cols = r.columns([ref])[ref]
    assert [(c.native_type, c.family, c.nullable) for c in cols] == [
        ("NUMBER(10,0)", "integer", False),
        ("DATE", "timestamp", True),
        ("VARCHAR2(100)", "string", True),
    ]
    assert cols[1].default == "SYSDATE"
    oref = ObjectRef(None, "SHOP", "Order Items")
    cons = r.constraints([oref])[oref]
    assert cons[0].columns == ("ORDER_ID", "LINE_NO") and cons[0].enforced is True
    assert cons[1].ref_table == ref and cons[1].enforced is False


def test_oracle_second_page_binds_token() -> None:
    fake = Fake((["all_tables"], []))
    OracleReader(fake, page_size=5).list_tables(  # type: ignore[arg-type]
        Schema(None, "SHOP"), page_token="M", changed_since=None
    )
    sql, params, _ = fake.calls[0]
    assert "x.table_name > ?" in sql and params[-2:] == ("M", 6)


def test_timeout_is_passed_through() -> None:
    fake = Fake((["all_users"], []))
    OracleReader(fake, timeout_s=7.5).list_schemas(None, page_token=None)  # type: ignore[arg-type]
    assert fake.calls[0][2] == 7.5


def _no_null_binds(fake: Fake) -> None:
    for sql, params, _ in fake.calls:
        assert None not in params, (sql, params)


def test_oracle_columns_do_not_use_hidden_column() -> None:
    fake = Fake((["all_tab_columns"], []))
    OracleReader(fake).columns([ObjectRef(None, "SHOP", "ORDERS")])  # type: ignore[arg-type]
    sql, params, _ = fake.calls[0]
    assert "hidden_column" not in sql.lower() and params == ("SHOP", "ORDERS")


def test_databricks_first_page_binds_no_null() -> None:
    fake = Fake((["tables"], []), (["schemata"], []))
    r = DatabricksReader(fake)  # type: ignore[arg-type]
    r.list_tables(Schema("main", "shop"), page_token=None, changed_since=None)
    r.list_schemas(None, page_token=None)
    _no_null_binds(fake)
    assert "table_name > ?" not in fake.calls[0][0] and "last_altered >=" not in fake.calls[0][0]
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    r.list_tables(Schema("main", "shop"), page_token="m", changed_since=ts)
    sql, params, _ = fake.calls[-1]
    assert "table_name > ?" in sql and "last_altered >= ?" in sql
    assert params == ("main", "shop", "m", ts, 1001)


def test_bigquery_incremental_keeps_views_and_skips_deleted_storage() -> None:
    fake = Fake((["TABLES"], []))
    r = BigQueryReader(fake, project="p1")  # type: ignore[arg-type]
    r.list_tables(Schema("p1", "shop"), page_token=None, changed_since=None)
    sql, params, _ = fake.calls[0]
    assert None not in params and "NOT s.deleted" in sql and "WHERE" not in sql
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    r.list_tables(Schema("p1", "shop"), page_token="a", changed_since=ts)
    sql, params, _ = fake.calls[1]
    assert "s.table_name IS NULL" in sql and "t.creation_time >= ?" in sql
    assert params == ("description", "a", ts, ts, 1001)


def test_redshift_skips_constraints_and_external_columns_for_other_databases() -> None:
    fake = Fake(
        (["CURRENT_DATABASE"], [{"name": "dev"}]),
        (["FROM SVV_ALL_COLUMNS"], []),
    )
    r = RedshiftReader(fake, database="dev")  # type: ignore[arg-type]
    other = ObjectRef("shared_db", "s", "t")
    assert r.constraints([other])[other] == []
    r.columns([other])
    assert all(
        "pg_constraint" not in c[0] and "svv_external" not in c[0].lower() for c in fake.calls
    )


def test_crawl_report_flags_estimated_bytes() -> None:
    from schemaguru.catalog import crawl
    from schemaguru.store.sqlite import SQLiteStore

    fake = Fake(
        (["SCHEMATA"], [{"catalog_name": "p1", "schema_name": "shop"}]),
        (["TABLES"], []),
    )
    store = SQLiteStore(":memory:")
    report = crawl(BigQueryReader(fake), store)  # type: ignore[arg-type]
    assert report.bytes_billed == 2 * MIN_BILLED_BYTES and report.bytes_billed_is_estimate


# ---- incremental capability ---------------------------------------------------------------
def test_redshift_ignores_changed_since_and_says_so() -> None:
    """Redshift's views carry no reliable last-altered time, so list_tables lists everything
    whatever ``changed_since`` says; the reader must therefore not claim to be incremental
    (the crawl would otherwise skip changed tables)."""
    rules = (
        (["CURRENT_DATABASE"], [{"name": "dev"}]),
        (
            ["FROM SVV_ALL_TABLES"],
            [{"table_name": "orders", "table_type": "TABLE", "remarks": None}],
        ),
        (["FROM SVV_TABLE_INFO"], []),
        (["FROM SVV_EXTERNAL_TABLES"], []),
    )
    full, since = Fake(*rules), Fake(*rules)
    schema = Schema("dev", "public")
    a = RedshiftReader(full).list_tables(schema, page_token=None, changed_since=None)  # type: ignore[arg-type]
    ts = datetime(2026, 11, 1, tzinfo=timezone.utc)
    b = RedshiftReader(since).list_tables(schema, page_token=None, changed_since=ts)  # type: ignore[arg-type]
    assert a.items == b.items and [c[:2] for c in full.calls] == [c[:2] for c in since.calls]
    assert RedshiftReader(full).capabilities().incremental is False  # type: ignore[arg-type]


def _uses_changed_since(cls: type) -> bool:
    """Does ``list_tables`` read ``changed_since`` beyond its signature?"""
    import inspect
    import re

    return len(re.findall(r"\bchanged_since\b", inspect.getsource(cls.list_tables))) > 1


def test_incremental_capability_matches_list_tables() -> None:
    """A reader reports ``incremental`` exactly when its list_tables uses ``changed_since``.
    Negative control: a reader claiming incremental while ignoring it is caught."""

    class Nothing:
        def run(self, sql: str, params: Any = (), *, timeout_s: float | None = None) -> list[Any]:
            return []

    for d in dialects():
        r = reader_for(d, Nothing())
        assert r.capabilities().incremental == _uses_changed_since(type(r)), d

    class Liar(RedshiftReader):
        def capabilities(self) -> ReaderCapabilities:
            return replace(super().capabilities(), incremental=True)

    liar = Liar(Nothing())  # type: ignore[arg-type]
    assert liar.capabilities().incremental != _uses_changed_since(Liar)  # the check flags it


@pytest.mark.parametrize("dialect", dialects())
def test_list_schemas_respects_page_token(dialect: str) -> None:
    """Every shipped reader lists schemas in one page: a page token can only ask for a page
    past the last, which is empty and final (and costs no query)."""
    fake = Fake()  # no rules: any query is an error
    reader = reader_for(dialect, fake)  # type: ignore[arg-type]
    assert reader.list_schemas("main", page_token="1") == Page([], None)
    assert reader.list_schemas(None, page_token="") == Page([], None)
    assert fake.calls == []


# ---- 0.2.0 hardening: constraint, paging, billing and dialect edge cases (A4) -------------------
import re  # noqa: E402
import sqlite3  # noqa: E402

from schemaguru.catalog.protocol import CatalogError  # noqa: E402
from schemaguru.catalog.readers.postgres import PostgresReader  # noqa: E402


def _pg_constraint_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema_name": "s",
        "table_name": "items",
        "contype": "f",
        "cols": ["order_id"],
        "validated": True,
        "ref_schema": "s",
        "ref_table": "orders",
        "ref_cols": ["id"],
    }
    row.update(over)
    return row


def test_postgres_not_valid_fk_unenforced() -> None:
    """``NOT VALID`` (``convalidated = false``): existing rows were never checked, so the FK is
    reported unenforced; a validated FK and a primary key stay enforced."""
    fake = Fake(
        (["pg_index"], []),
        (
            ["pg_constraint"],
            [
                _pg_constraint_row(validated=False),
                _pg_constraint_row(contype="f", cols=["x"], ref_cols=["id"], validated=True),
                _pg_constraint_row(contype="p", cols=["id"], validated=True, ref_schema=None),
            ],
        ),
    )
    ref = ObjectRef(None, "s", "items")
    cons = PostgresReader(fake).constraints([ref])[ref]  # type: ignore[arg-type]
    assert [(c.kind, c.columns, c.enforced) for c in cons] == [
        ("foreign_key", ("order_id",), False),
        ("foreign_key", ("x",), True),
        ("primary_key", ("id",), True),
    ]
    assert "con.convalidated" in fake.calls[0][0]


def test_postgres_unique_index_query_failure_keeps_keys() -> None:
    """On PostgreSQL 10 and older ``indnkeyatts`` does not exist: the index query fails, the
    keys already read are returned, and a diagnostic names the cause."""

    class Pg10(Fake):
        def run(
            self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
        ) -> Any:
            if "pg_index" in sql:
                raise RuntimeError("column i.indnkeyatts does not exist")
            return super().run(sql, params, timeout_s=timeout_s)

    fake = Pg10(
        (["pg_constraint"], [_pg_constraint_row(contype="p", cols=["id"], ref_schema=None)])
    )
    reader = PostgresReader(fake)  # type: ignore[arg-type]
    ref = ObjectRef(None, "s", "items")
    cons = reader.constraints([ref])[ref]
    assert [(c.kind, c.columns) for c in cons] == [("primary_key", ("id",))]
    assert len(reader.diagnostics) == 1 and "PostgreSQL 11" in reader.diagnostics[0].message


def test_postgres_unique_index_reported() -> None:
    """A unique index that backs no constraint is still a uniqueness guarantee."""
    fake = Fake(
        (
            ["pg_index"],
            [
                {"schema_name": "s", "table_name": "items", "cols": ["sku", "region"]},
                {"schema_name": "s", "table_name": "other", "cols": ["id"]},
            ],
        ),
        (["pg_constraint"], [_pg_constraint_row(contype="u", cols=["email"], ref_schema=None)]),
    )
    ref = ObjectRef(None, "s", "items")
    cons = PostgresReader(fake).constraints([ref])[ref]  # type: ignore[arg-type]
    assert Constraint("unique", ref, ("email",), None, None, True) in cons
    assert Constraint("unique", ref, ("sku", "region"), None, None, True) in cons
    index_sql = next(c[0] for c in fake.calls if "pg_index" in c[0])
    # only whole-table, valid, constraint-less indexes count
    for needle in ("indisunique", "NOT i.indisprimary", "indisvalid", "indpred IS NULL"):
        assert needle in index_sql
    assert "indexprs IS NULL" in index_sql and "conindid = i.indexrelid" in index_sql
    assert "indnkeyatts" in index_sql  # INCLUDE columns are not part of the key
    assert fake.calls[-1][1] == ("s", "items") or fake.calls[0][1] == ("s", "items")


# -- SQL executed for real: the join semantics, not the shape of the text --------------------------
class _Sqlite:
    """Runs a reader's SQL on in-memory SQLite tables (``rewrite`` maps dialect syntax)."""

    def __init__(self, ddl: Sequence[str], rewrite: Any) -> None:
        self.db = sqlite3.connect(":memory:")
        for stmt in ddl:
            self.db.execute(stmt)
        self.rewrite = rewrite
        self.calls: list[str] = []

    def insert(self, table: str, rows: Sequence[Sequence[Any]]) -> None:
        marks = ", ".join("?" for _ in rows[0])
        self.db.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)

    def run(
        self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
    ) -> list[dict[str, Any]]:
        check_metadata_sql(sql)
        self.calls.append(sql)
        cur = self.db.execute(self.rewrite(sql), tuple(params))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


_ORA_DDL = (
    "CREATE TABLE all_tables(owner, table_name, num_rows, nested, dropped, iot_name)",
    "CREATE TABLE all_views(owner, view_name)",
    "CREATE TABLE all_mviews(owner, mview_name)",
    "CREATE TABLE all_tab_comments(owner, table_name, comments)",
    "CREATE TABLE all_mview_comments(owner, mview_name, comments)",
    "CREATE TABLE all_part_tables(owner, table_name, partitioning_type)",
)


def _oracle_db() -> _Sqlite:
    db = _Sqlite(_ORA_DDL, lambda sql: sql.replace("FETCH FIRST ? ROWS ONLY", "LIMIT ?"))
    db.insert(
        "all_tables",
        [
            ("SHOP", "ORDERS", 10, "NO", "NO", None),
            ("SHOP", "PLAIN", None, "NO", "NO", None),
            ("SHOP", "MV_SALES", 3, "NO", "NO", None),  # an MV's container table
        ],
    )
    db.insert("all_views", [("SHOP", "V_OPEN")])
    db.insert("all_mviews", [("SHOP", "MV_SALES")])
    # ORDERS is partitioned but has NO row in ALL_TAB_COMMENTS: ``c.owner`` is NULL for it
    db.insert("all_tab_comments", [("SHOP", "PLAIN", "plain table"), ("SHOP", "V_OPEN", "a view")])
    db.insert("all_mview_comments", [("SHOP", "MV_SALES", "sales rollup")])
    db.insert("all_part_tables", [("SHOP", "ORDERS", "RANGE")])
    return db


def test_oracle_partitioning_null_owner() -> None:
    reader = OracleReader(_oracle_db())  # type: ignore[arg-type]
    page = reader.list_tables(Schema(None, "SHOP"), page_token=None, changed_since=None)
    by = {t.ref.table: t for t in page.items}
    assert by["ORDERS"].partitioning == "RANGE" and by["ORDERS"].comment is None
    assert by["PLAIN"].partitioning is None and by["PLAIN"].comment == "plain table"


def test_oracle_mv_comment() -> None:
    reader = OracleReader(_oracle_db())  # type: ignore[arg-type]
    page = reader.list_tables(Schema(None, "SHOP"), page_token=None, changed_since=None)
    by = {t.ref.table: t for t in page.items}
    assert set(by) == {
        "ORDERS",
        "PLAIN",
        "MV_SALES",
        "V_OPEN",
    }  # the container table is not listed twice
    assert by["MV_SALES"].kind == "materialized_view" and by["MV_SALES"].comment == "sales rollup"
    assert by["V_OPEN"].comment == "a view"
    sql = reader._ex.calls[0]  # type: ignore[attr-defined]
    assert "all_mview_comments" in sql.lower()


# -- Snowflake ----------------------------------------------------------------------------------
def _sf_tables(clustering: str | None) -> Fake:
    return Fake(
        (
            ["INFORMATION_SCHEMA.TABLES"],
            [
                {
                    "TABLE_NAME": "T",
                    "TABLE_TYPE": "BASE TABLE",
                    "ROW_COUNT": 1,
                    "BYTES": 1,
                    "CREATED": None,
                    "LAST_ALTERED": None,
                    "COMMENT": None,
                    "CLUSTERING_KEY": clustering,
                }
            ],
        )
    )


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("LINEAR(A, B)", ("A", "B")),
        ("LINEAR(TO_DATE(TS))", ("TO_DATE(TS)",)),
        ("LINEAR(to_date(ts))", ("to_date(ts)",)),
        ("LINEAR(SUBSTR(A, 1, 2), B)", ("SUBSTR(A, 1, 2)", "B")),
        ('LINEAR("a,b", C)', ('"a,b"', "C")),
        ("LINEAR(F('x,y'), C)", ("F('x,y')", "C")),
        ("LINEAR(A)", ("A",)),
        (None, None),
        ("LINEAR()", None),
        ("LINEAR(A,)", None),
        ("LINEAR(F(A, B)", None),  # unbalanced: unknown, not guessed
        ("something else", None),
    ],
)
def test_snowflake_clustering_expressions(
    key: str | None, expected: tuple[str, ...] | None
) -> None:
    r = SnowflakeReader(_sf_tables(key), database="DB")  # type: ignore[arg-type]
    t = r.list_tables(Schema("DB", "S"), page_token=None, changed_since=None).items[0]
    assert t.clustering == expected


def test_snowflake_bad_metadata_is_catalog_error() -> None:
    no_db = SnowflakeReader(Fake((["CURRENT_DATABASE"], [])))  # type: ignore[arg-type]
    with pytest.raises(CatalogError) as e:
        list(no_db.list_catalogs())
    assert e.value.code == "SG102"

    bad_seq = Fake(
        (
            ["SHOW PRIMARY KEYS"],
            [{"table_name": "T", "column_name": "A", "key_sequence": "x", "constraint_name": "P"}],
        ),
        (["SHOW UNIQUE KEYS"], []),
        (["SHOW IMPORTED KEYS"], []),
    )
    r = SnowflakeReader(bad_seq, database="DB")  # type: ignore[arg-type]
    with pytest.raises(CatalogError) as e2:
        r.constraints([ObjectRef("DB", "S", "T")])
    assert e2.value.code == "SG102"

    missing = Fake(
        (["SHOW PRIMARY KEYS"], [{"column_name": "A", "key_sequence": 1}]),
        (["SHOW UNIQUE KEYS"], []),
        (["SHOW IMPORTED KEYS"], []),
    )
    with pytest.raises(CatalogError) as e3:
        SnowflakeReader(missing, database="DB").constraints([ObjectRef("DB", "S", "T")])  # type: ignore[arg-type]
    assert e3.value.code == "SG102"


def test_snowflake_lists_past_show_cap() -> None:
    """``SHOW ... KEYS IN SCHEMA`` returns at most 10,000 rows. A result at the cap is possibly
    truncated, so the reader asks per table instead of trusting it."""
    cap = 10_000
    calls: list[str] = []

    class Engine:
        def run(
            self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
        ) -> list[dict[str, Any]]:
            check_metadata_sql(sql)
            calls.append(sql)
            if sql.startswith("SHOW PRIMARY KEYS IN SCHEMA"):  # full, and none of ours
                return [
                    {"table_name": f"FILL{i}", "column_name": "ID", "key_sequence": 1,
                     "constraint_name": f"PK{i}"}
                    for i in range(cap)
                ]  # fmt: skip
            if sql.startswith("SHOW PRIMARY KEYS IN TABLE"):
                name = sql.rsplit(".", 1)[1].strip('"')
                return [
                    {"table_name": name, "column_name": "ID", "key_sequence": 1,
                     "constraint_name": f"PK_{name}"}
                ]  # fmt: skip
            return []

    r = SnowflakeReader(Engine(), database="DB")  # type: ignore[arg-type]
    refs = [ObjectRef("DB", "S", n) for n in ("T1", "T2")]
    cons = r.constraints(refs)
    assert [c.columns for c in cons[refs[0]]] == [("ID",)] and len(cons[refs[1]]) == 1
    assert 'SHOW PRIMARY KEYS IN TABLE "DB"."S"."T1"' in calls
    assert 'SHOW PRIMARY KEYS IN TABLE "DB"."S"."T2"' in calls
    # below the cap the schema-wide answer is complete: no per-table queries for UNIQUE / FK
    assert not any(c.startswith("SHOW UNIQUE KEYS IN TABLE") for c in calls)


# -- BigQuery -----------------------------------------------------------------------------------
def test_bigquery_pseudo_columns_hidden() -> None:
    fake = Fake(
        (
            ["COLUMNS", "COLUMN_FIELD_PATHS"],
            [
                {
                    "table_name": "events",
                    "column_name": name,
                    "ordinal_position": i,
                    "data_type": ty,
                    "is_nullable": "YES",
                    "column_default": "NULL",
                    "description": None,
                }
                for i, (name, ty) in enumerate(
                    [
                        ("id", "INT64"),
                        ("_PARTITIONTIME", "TIMESTAMP"),
                        ("_PARTITIONDATE", "DATE"),
                        ("_partition_user", "STRING"),  # reserved prefix too, any case
                        ("partitioned_by", "STRING"),  # an ordinary column that merely resembles
                    ],
                    1,
                )
            ],
        )
    )
    ref = ObjectRef("p1", "shop", "events")
    cols = BigQueryReader(fake, project="p1").columns([ref])[ref]  # type: ignore[arg-type]
    assert [c.ref.column for c in cols] == ["id", "partitioned_by"]


def test_bigquery_cross_project_storage() -> None:
    fake = Fake((["TABLES"], []), (["SCHEMATA"], []))
    r = BigQueryReader(fake, project="billing", region="eu")  # type: ignore[arg-type]
    r.list_tables(Schema("data-proj", "shop"), page_token=None, changed_since=None)
    sql = fake.calls[0][0]
    assert "`data-proj`.`region-eu`.INFORMATION_SCHEMA.TABLE_STORAGE" in sql
    assert "`billing`.`region-eu`" not in sql
    assert "`data-proj`.`shop`.INFORMATION_SCHEMA.TABLES" in sql
    r.list_schemas("data-proj", page_token=None)
    assert "`data-proj`.`region-eu`.INFORMATION_SCHEMA.SCHEMATA" in fake.calls[1][0]
    assert fake.calls[1][1] == ("data-proj",)
    # no reader project: the dataset's own project still qualifies the region view
    bare = BigQueryReader(fake, region="eu")  # type: ignore[arg-type]
    bare.list_tables(Schema("data-proj", "shop"), page_token=None, changed_since=None)
    assert "`data-proj`.`region-eu`.INFORMATION_SCHEMA.TABLE_STORAGE" in fake.calls[2][0]
    with pytest.raises(CatalogError):
        r.list_tables(Schema("bad`proj", "shop"), page_token=None, changed_since=None)


_BQ_DDL = (
    "CREATE TABLE bq_TABLE_CONSTRAINTS(constraint_catalog, constraint_schema, constraint_name, "
    "table_catalog, table_schema, table_name, constraint_type, enforced)",
    "CREATE TABLE bq_KEY_COLUMN_USAGE(constraint_catalog, constraint_schema, constraint_name, "
    "table_catalog, table_schema, table_name, column_name, ordinal_position, "
    "position_in_unique_constraint)",
    "CREATE TABLE bq_CONSTRAINT_COLUMN_USAGE(table_catalog, table_schema, table_name, "
    "column_name, constraint_catalog, constraint_schema, constraint_name)",
)


def _bq_rewrite(sql: str) -> str:
    """Every ``project.dataset.INFORMATION_SCHEMA.X`` is one table: the worst case, where a view
    holds constraints of several datasets side by side."""
    return re.sub(r"`[^`]+`\.`[^`]+`\.INFORMATION_SCHEMA\.(\w+)", r"bq_\1", sql)


def test_bigquery_fk_join_full_key() -> None:
    """Same-named constraints in two datasets (``fk`` and ``pk`` exist in both) never cross: the
    join is on (catalog, schema, name) plus the referenced column's position."""
    db = _Sqlite(_BQ_DDL, _bq_rewrite)
    for ds, child in (("shop", "items"), ("ops", "lines")):
        db.insert(
            "bq_TABLE_CONSTRAINTS",
            [
                ("p1", ds, "pk", "p1", ds, "orders", "PRIMARY KEY", "NO"),
                ("p1", ds, "fk", "p1", ds, child, "FOREIGN KEY", "NO"),
            ],
        )
        db.insert(
            "bq_KEY_COLUMN_USAGE",
            [
                ("p1", ds, "pk", "p1", ds, "orders", "id", 1, None),
                ("p1", ds, "fk", "p1", ds, child, "order_id", 1, 1),
            ],
        )
        db.insert(
            "bq_CONSTRAINT_COLUMN_USAGE",
            [
                ("p1", ds, "orders", "id", "p1", ds, "pk"),
                ("p1", ds, "orders", "id", "p1", ds, "fk"),
            ],
        )
    ref = ObjectRef("p1", "shop", "items")
    cons = BigQueryReader(db, project="p1").constraints([ref])[ref]  # type: ignore[arg-type]
    fks = [c for c in cons if c.kind == "foreign_key"]
    assert len(fks) == 1
    assert fks[0].ref_table == ObjectRef("p1", "shop", "orders") and fks[0].ref_columns == ("id",)
    sql = db.calls[0]
    assert "constraint_schema" in sql and "constraint_catalog" in sql


# -- Redshift -----------------------------------------------------------------------------------
class _RsPages(Fake):
    """Keyset-paged ``SVV_ALL_TABLES`` over a static rule set."""

    def run(
        self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
    ) -> list[dict[str, Any]]:
        rows = super().run(sql, params, timeout_s=timeout_s)
        if "from svv_all_tables" in sql.lower():
            rows = sorted(rows, key=lambda r: r["table_name"])
            if "table_name > ?" in sql:
                rows = [r for r in rows if r["table_name"] > params[2]]
            rows = rows[: params[-1]]
        return rows


def test_redshift_schema_info_once() -> None:
    fake = _RsPages(
        (["CURRENT_DATABASE"], [{"name": "dev"}]),
        (
            ["FROM SVV_ALL_TABLES"],
            [
                {"table_name": n, "table_type": "TABLE", "remarks": None}
                for n in ("a", "b", "c", "d", "e")
            ],
        ),
        (["FROM SVV_TABLE_INFO"], [{"table_name": "a", "tbl_rows": 7, "size_mb": 3}]),
        (["FROM SVV_EXTERNAL_TABLES"], [{"table_name": "e"}]),
    )
    r = RedshiftReader(fake, database="dev", page_size=2)  # type: ignore[arg-type]
    schema = Schema("dev", "public")

    def count(needle: str) -> int:
        return sum(needle in c[0].lower() for c in fake.calls)

    items, token = [], None
    pages = 0
    while True:
        page = r.list_tables(schema, page_token=token, changed_since=None)
        items += page.items
        pages += 1
        token = page.next_token
        if token is None:
            break
    assert pages == 3 and [t.ref.table for t in items] == ["a", "b", "c", "d", "e"]
    assert count("from svv_table_info") == 1 and count("from svv_external_tables") == 1
    assert items[0].row_count == 7 and items[4].kind == "external"  # merged on every page
    # a new listing (first page) re-reads: stats are not carried across crawls
    r.list_tables(schema, page_token=None, changed_since=None)
    assert count("from svv_table_info") == 2


# -- paging collation ---------------------------------------------------------------------------
def _keyset_walk(names: list[str], order: Any, compare: Any, size: int = 2) -> list[str]:
    """Page through ``names`` like the readers do: ORDER BY ``order``, then ``> last`` under
    ``compare``. Returns what the walk listed."""
    listed: list[str] = []
    token: str | None = None
    for _ in range(50):
        rest = [n for n in names if token is None or compare(n) > compare(token)]
        page = sorted(rest, key=order)[: size + 1]
        listed += page[:size]
        if len(page) <= size:
            return listed
        token = page[size - 1]
    raise AssertionError("keyset walk did not end")


_NAMES = ["a", "B", "b", "ä"]
_BINARY = lambda s: s.encode("utf-8")  # noqa: E731
_LOCALE = lambda s: (s.casefold().replace("ä", "a"), s)  # noqa: E731


def test_keyset_walk_needs_one_collation() -> None:
    """Why paging pins a collation: the same walk is exact under one ordering and loses or
    repeats names when ORDER BY and ``>`` disagree (the negative control)."""
    assert _keyset_walk(_NAMES, _BINARY, _BINARY) == sorted(_NAMES, key=_BINARY)
    assert _keyset_walk(_NAMES, _LOCALE, _LOCALE) == sorted(_NAMES, key=_LOCALE)
    mixed = _keyset_walk(_NAMES, _LOCALE, _BINARY)
    assert sorted(mixed) != sorted(_NAMES) or len(mixed) != len(set(mixed))


# dialect -> the explicit binary-collation pin its keyset SQL carries; ``None`` means the engine's
# default comparison of strings is already binary and the SQL must not mix in a collation clause
_PIN: dict[str, str | None] = {
    "postgres": 'COLLATE "C"',
    "mysql": "BINARY",
    "tsql": "COLLATE LATIN1_GENERAL_BIN2",
    "redshift": None,
    "bigquery": None,
    "snowflake": None,
    "databricks": None,
    "oracle": None,
    "clickhouse": None,
    "trino": None,
    "duckdb": None,
}


class _Capture:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def run(
        self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
    ) -> list[dict[str, Any]]:
        check_metadata_sql(sql)
        assert sql.count("?") == len(params), sql
        self.sql.append(sql)
        return []


def test_every_dialect_has_a_pin_decision() -> None:
    assert set(_PIN) == set(dialects())


@pytest.mark.parametrize("dialect", sorted(_PIN))
def test_paging_collation_stable(dialect: str) -> None:
    """The keyset comparison and the ORDER BY of ``list_tables`` use one collation: an explicit
    binary pin on BOTH (``COLLATE "C"`` PostgreSQL, ``BINARY`` MySQL, ``Latin1_General_BIN2``
    T-SQL), or no collation clause on either where the engine's default is binary."""
    cap = _Capture()
    reader = reader_for(dialect, cap)  # type: ignore[arg-type]
    reader.list_tables(Schema("main", "shop"), page_token="m", changed_since=None)
    paged = next(s for s in cap.sql if re.search(r"(?i)\border by\b", s))
    upper = re.sub(r"\s+", " ", paged).upper()
    order, before = upper[upper.rindex("ORDER BY") :], upper[: upper.rindex("ORDER BY")]
    pin = _PIN[dialect]
    if pin is None:
        assert "COLLATE" not in upper and "BINARY" not in upper, paged
    else:  # both halves carry it: the comparison (before ORDER BY) and the ordering
        assert pin in order and pin in before, paged


def test_crawl_surfaces_reader_diagnostics_once() -> None:
    """The PostgreSQL-10 unique-index warning reaches ``CrawlReport.errors`` exactly once."""
    from schemaguru.catalog import crawl
    from schemaguru.store.sqlite import SQLiteStore

    class Pg10(Fake):
        def run(
            self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
        ) -> Any:
            if "pg_index" in sql:
                raise RuntimeError("column i.indnkeyatts does not exist")
            return super().run(sql, params, timeout_s=timeout_s)

    fake = Pg10(
        (["obj_description"], [{"name": "s", "comment": None, "table_count": 1}]),
        (
            ["reltuples"],
            [
                {
                    "name": "items",
                    "relkind": "r",
                    "reltuples": 1,
                    "size_bytes": 1,
                    "partitioning": None,
                    "comment": None,
                }
            ],
        ),
        (["FROM pg_attribute a"], []),
        (["pg_constraint con"], [_pg_constraint_row(contype="p", cols=["id"], ref_schema=None)]),
    )
    reader = PostgresReader(fake)  # type: ignore[arg-type]
    store = SQLiteStore(":memory:")
    report = crawl(reader, store)
    again = crawl(reader, store)
    store.close()
    assert [d.code for d in report.errors] == ["SG102"]
    assert "PostgreSQL 11" in report.errors[0].message
    assert not reader.diagnostics  # consumed: not repeated
    assert [d.code for d in again.errors] == ["SG102"]  # a new crawl reports its own, once
