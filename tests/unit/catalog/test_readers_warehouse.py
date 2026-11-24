"""Warehouse readers against hand-built fake executors (every SQL passes the metadata guard)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import pytest

from schemaguru.catalog import dialects, reader_for
from schemaguru.catalog.executor import check_metadata_sql
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
    assert t1.clustering == ("CUSTOMER_ID", "CREATED_AT") and t2.clustering is None
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
