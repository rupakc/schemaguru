"""Synthetic fixtures for the warehouse readers (no engine, no account, generic shop names).

Each dialect gets a scripted engine that answers the reader's catalog queries with rows shaped
exactly like the system view's documented columns, then ``record`` writes ``queries.json`` and
``expected.json``. Review ``expected.json`` by eye after a regeneration; it is a contract.

    python -m tests.conformance.record_warehouses [bigquery snowflake ...]
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from schemaguru.catalog import reader_for
from tests.conformance.replay import record

UTC = timezone.utc
T0 = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)
T1 = datetime(2026, 2, 11, 14, 0, tzinfo=UTC)
Rows = list[dict[str, Any]]
Handler = Callable[[str, Sequence[Any]], Rows]


class Scripted:
    def __init__(self, handler: Handler) -> None:
        self._h = handler

    def run(self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None) -> Rows:
        from schemaguru.catalog.executor import check_metadata_sql

        check_metadata_sql(sql)
        return self._h(sql.lower(), params)


def _pairs(params: Sequence[Any]) -> set[tuple[str, str]]:
    return {(params[i], params[i + 1]) for i in range(0, len(params) - 1, 2)}


# ---------------------------------------------------------------------------------- BigQuery
BQ_P = "demo-project"


def _bq(sql: str, params: Sequence[Any]) -> Rows:
    ds = "shop" if "`shop`" in sql else "ops" if "`ops`" in sql else None
    if "schemata" in sql:
        return [
            {"catalog_name": BQ_P, "schema_name": "ops"},
            {"catalog_name": BQ_P, "schema_name": "shop"},
        ]
    if "table_storage" in sql:  # list_tables
        data = {
            "shop": [
                ("customers", "BASE TABLE", '"people who buy"', 1000, 81920, T0, T1),
                ("order-items", "BASE TABLE", None, 25000, 1048576, T0, T1),
                ("orders", "BASE TABLE", '"one row per \\"order\\""', 10000, 524288, T0, T1),
                ("v_open_orders", "VIEW", None, None, None, T0, None),
            ],
            "ops": [
                ("events", "BASE TABLE", None, 500000, 7340032, T0, T1),
                ("sessions", "BASE TABLE", None, 90000, 1310720, T0, T1),
            ],
        }[ds or ""]
        return [
            {
                "table_name": n,
                "table_type": k,
                "creation_time": c,
                "description": d,
                "total_rows": r,
                "total_logical_bytes": b,
                "last_modified": m,
            }
            for n, k, d, r, b, c, m in data
        ]
    if "column_field_paths" in sql:
        cols = {
            ("shop", "customers"): [
                ("customer_id", "INT64", "NO", "surrogate key"),
                ("email", "STRING", "YES", "login address"),
                ("segment", "STRING", "YES", None),
                ("created_at", "TIMESTAMP", "YES", None),
                ("tags", "ARRAY<STRING>", "YES", None),
            ],
            ("shop", "orders"): [
                ("order_id", "INT64", "NO", None),
                ("customer_id", "INT64", "YES", None),
                ("status", "STRING", "YES", None),
                ("total", "NUMERIC(10, 2)", "YES", None),
                ("deleted", "BOOL", "YES", None),
            ],
            ("shop", "order-items"): [
                ("order_id", "INT64", "NO", None),
                ("line_no", "INT64", "NO", None),
                ("product_id", "INT64", "YES", None),
            ],
            ("shop", "v_open_orders"): [
                ("order_id", "INT64", "YES", None),
                ("status", "STRING", "YES", None),
            ],
            ("ops", "events"): [
                ("event_id", "INT64", "NO", None),
                ("session_id", "STRING", "YES", None),
                ("payload", "JSON", "YES", "raw event body"),
                ("context", "STRUCT<ip STRING, agent STRING>", "YES", None),
            ],
            ("ops", "sessions"): [
                ("session_id", "STRING", "NO", None),
                ("started_at", "TIMESTAMP", "YES", None),
            ],
        }
        out: Rows = []
        for (d, t), cs in cols.items():
            if d == ds and t in params:
                for i, (c, ty, nl, desc) in enumerate(cs, 1):
                    out.append(
                        {
                            "table_name": t,
                            "column_name": c,
                            "ordinal_position": i,
                            "data_type": ty,
                            "is_nullable": nl,
                            "column_default": "NULL",
                            "description": desc,
                        }
                    )
        return out
    if "key_column_usage" in sql:

        def row(t: str, cn: str, ty: str, col: str, pos: int, **ref: Any) -> dict[str, Any]:
            base = {
                "table_name": t, "constraint_name": cn, "constraint_type": ty, "enforced": "NO",
                "column_name": col, "ordinal_position": pos, "unique_position": None,
                "ref_catalog": None, "ref_schema": None, "ref_table": None,
                "ref_column": None, "ref_position": None,
            }  # fmt: skip
            base.update(ref)
            return base

        allrows = {
            "shop": [
                row("customers", "customers.pk$", "PRIMARY KEY", "customer_id", 1),
                row("orders", "orders.pk$", "PRIMARY KEY", "order_id", 1),
                row(
                    "orders", "orders.fk_customer", "FOREIGN KEY", "customer_id", 1,
                    unique_position=1, ref_catalog=BQ_P, ref_schema="shop",
                    ref_table="customers", ref_column="customer_id", ref_position=1,
                ),
                row("order-items", "order-items.pk$", "PRIMARY KEY", "order_id", 1),
                row("order-items", "order-items.pk$", "PRIMARY KEY", "line_no", 2),
                row(
                    "order-items", "order-items.fk_order", "FOREIGN KEY", "order_id", 1,
                    unique_position=1, ref_catalog=BQ_P, ref_schema="shop",
                    ref_table="orders", ref_column="order_id", ref_position=1,
                ),
            ],
            "ops": [
                row("events", "events.pk$", "PRIMARY KEY", "event_id", 1),
                row("sessions", "sessions.pk$", "PRIMARY KEY", "session_id", 1),
                row(
                    "events", "events.fk_session", "FOREIGN KEY", "session_id", 1,
                    unique_position=1, ref_catalog=BQ_P, ref_schema="ops",
                    ref_table="sessions", ref_column="session_id", ref_position=1,
                ),
            ],
        }  # fmt: skip
        return [r for r in allrows[ds or ""] if r["table_name"] in params]
    raise AssertionError(sql)


# ---------------------------------------------------------------------------------- Snowflake
SF_DB = "DEMO_DB"


def _sf_tables() -> dict[str, list[tuple[Any, ...]]]:
    return {
        "SHOP": [
            ("CUSTOMERS", "BASE TABLE", 1000, 81920, "people who buy", "LINEAR(SEGMENT)"),
            ("ORDERS", "BASE TABLE", 10000, 524288, None, "LINEAR(CUSTOMER_ID, CREATED_AT)"),
            ("Order Items", "BASE TABLE", 25000, 1048576, None, None),
            ("V_OPEN_ORDERS", "VIEW", None, None, None, None),
        ],
        "OPS": [
            ("EVENTS", "BASE TABLE", 500000, 7340032, None, "LINEAR(TO_DATE(TS))"),
            ("SESSIONS", "BASE TABLE", 90000, 1310720, None, None),
        ],
    }


def _sf(sql: str, params: Sequence[Any]) -> Rows:
    if "current_database()" in sql and "from" not in sql:
        return [{"NAME": SF_DB}]
    if "information_schema.schemata" in sql:
        return [
            {"SCHEMA_NAME": "OPS", "COMMENT": "operational telemetry"},
            {"SCHEMA_NAME": "SHOP", "COMMENT": None},
        ]
    if "information_schema.tables" in sql:
        return [
            {
                "TABLE_NAME": n, "TABLE_TYPE": k, "ROW_COUNT": r, "BYTES": b, "CREATED": T0,
                "LAST_ALTERED": T1, "COMMENT": c, "CLUSTERING_KEY": ck,
            }
            for n, k, r, b, c, ck in _sf_tables()[params[0]]
        ]  # fmt: skip
    if "information_schema.columns" in sql:
        schema, names = params[0], set(params[1:])
        cols: dict[tuple[str, str], list[tuple[Any, ...]]] = {
            ("SHOP", "CUSTOMERS"): [
                ("CUSTOMER_ID", "NUMBER", "NO", "surrogate key", 38, 0, None),
                ("EMAIL", "TEXT", "YES", "login address", None, None, 320),
                ("SEGMENT", "TEXT", "YES", None, None, None, 16777216),
                ("CREATED_AT", "TIMESTAMP_NTZ", "YES", None, None, None, None),
                ("TAGS", "ARRAY", "YES", None, None, None, None),
            ],
            ("SHOP", "ORDERS"): [
                ("ORDER_ID", "NUMBER", "NO", None, 38, 0, None),
                ("CUSTOMER_ID", "NUMBER", "YES", None, 38, 0, None),
                ("TOTAL", "NUMBER", "YES", None, 10, 2, None),
                ("PLACED_AT", "TIMESTAMP_TZ", "YES", None, None, None, None),
            ],
            ("SHOP", "Order Items"): [
                ("ORDER_ID", "NUMBER", "NO", None, 38, 0, None),
                ("LINE_NO", "NUMBER", "NO", None, 38, 0, None),
                ("Unit Price", "FLOAT", "YES", "quoted lower-case column", None, None, None),
            ],
            ("SHOP", "V_OPEN_ORDERS"): [
                ("ORDER_ID", "NUMBER", "YES", None, 38, 0, None),
            ],
            ("OPS", "EVENTS"): [
                ("EVENT_ID", "NUMBER", "NO", None, 38, 0, None),
                ("SESSION_ID", "TEXT", "YES", None, None, None, 36),
                ("PAYLOAD", "VARIANT", "YES", None, None, None, None),
                ("ATTRS", "OBJECT", "YES", None, None, None, None),
            ],
            ("OPS", "SESSIONS"): [
                ("SESSION_ID", "TEXT", "NO", None, None, None, 36),
                ("STARTED_AT", "TIMESTAMP_LTZ", "YES", None, None, None, None),
            ],
        }
        out: Rows = []
        for (s, t), cs in cols.items():
            if s == schema and t in names:
                for i, (c, ty, nl, cm, p, sc, ln) in enumerate(cs, 1):
                    out.append(
                        {
                            "TABLE_NAME": t, "COLUMN_NAME": c, "ORDINAL_POSITION": i,
                            "DATA_TYPE": ty, "IS_NULLABLE": nl, "COLUMN_DEFAULT": None,
                            "COMMENT": cm, "NUMERIC_PRECISION": p, "NUMERIC_SCALE": sc,
                            "CHARACTER_MAXIMUM_LENGTH": ln,
                        }
                    )  # fmt: skip
        return out
    schema = "SHOP" if '"shop"' in sql else "OPS"
    if "show primary keys" in sql:
        pks = {
            "SHOP": [("CUSTOMERS", "CUSTOMER_ID", 1, "PK_CUSTOMERS"),
                     ("ORDERS", "ORDER_ID", 1, "PK_ORDERS"),
                     ("Order Items", "LINE_NO", 2, "PK_ORDER_ITEMS"),
                     ("Order Items", "ORDER_ID", 1, "PK_ORDER_ITEMS")],
            "OPS": [("EVENTS", "EVENT_ID", 1, "PK_EVENTS"),
                    ("SESSIONS", "SESSION_ID", 1, "PK_SESSIONS")],
        }  # fmt: skip
        return [
            {"created_on": T0, "database_name": SF_DB, "schema_name": schema, "table_name": t,
             "column_name": c, "key_sequence": q, "constraint_name": n, "rely": "false",
             "comment": None}
            for t, c, q, n in pks[schema]
        ]  # fmt: skip
    if "show unique keys" in sql:
        if schema == "SHOP":
            return [
                {"created_on": T0, "database_name": SF_DB, "schema_name": "SHOP",
                 "table_name": "CUSTOMERS", "column_name": "EMAIL", "key_sequence": 1,
                 "constraint_name": "UQ_CUSTOMERS_EMAIL", "rely": "false", "comment": None}
            ]  # fmt: skip
        return []
    if "show imported keys" in sql:
        fks = {
            "SHOP": [("ORDERS", "CUSTOMER_ID", "CUSTOMERS", "CUSTOMER_ID", "FK_ORDERS_CUST"),
                     ("Order Items", "ORDER_ID", "ORDERS", "ORDER_ID", "FK_ITEMS_ORDER")],
            "OPS": [("EVENTS", "SESSION_ID", "SESSIONS", "SESSION_ID", "FK_EVENTS_SESSION")],
        }  # fmt: skip
        return [
            {"created_on": T0, "pk_database_name": SF_DB, "pk_schema_name": schema,
             "pk_table_name": pt, "pk_column_name": pc, "fk_database_name": SF_DB,
             "fk_schema_name": schema, "fk_table_name": ft, "fk_column_name": fc,
             "key_sequence": 1, "update_rule": "NO ACTION", "delete_rule": "NO ACTION",
             "fk_name": n, "pk_name": "PK", "deferrability": "NOT DEFERRABLE", "rely": "false",
             "comment": None}
            for ft, fc, pt, pc, n in fks[schema]
        ]  # fmt: skip
    raise AssertionError(sql)


# ---------------------------------------------------------------------------------- Redshift
RS_COLS: dict[tuple[str, str], list[tuple[Any, ...]]] = {
    ("shop", "customers"): [
        ("customer_id", "bigint", "NO", None, None, 64, 0, "surrogate key"),
        ("email", "character varying", "YES", None, 320, None, None, "login address"),
        ("segment", "character varying", "YES", None, 16, None, None, None),
        ("created_at", "timestamp without time zone", "YES", None, None, None, None, None),
    ],
    ("shop", "orders"): [
        ("order_id", "bigint", "NO", None, None, 64, 0, None),
        ("customer_id", "bigint", "YES", None, None, 64, 0, None),
        ("total", "numeric", "YES", None, None, 10, 2, None),
    ],
    ("shop", "Order Items"): [
        ("order_id", "bigint", "NO", None, None, 64, 0, None),
        ("line_no", "integer", "NO", None, None, 32, 0, None),
        ("Unit Price", "double precision", "YES", "0", None, 53, None, None),
    ],
    ("shop", "v_open_orders"): [("order_id", "bigint", "YES", None, None, 64, 0, None)],
    ("ops", "events"): [
        ("event_id", "bigint", "NO", None, None, 64, 0, None),
        ("session_id", "character varying", "YES", None, 36, None, None, None),
        ("payload", "super", "YES", None, None, None, None, "raw event body"),
    ],
    ("ops", "sessions"): [
        ("session_id", "character varying", "NO", None, 36, None, None, None),
        ("started_at", "timestamp with time zone", "YES", None, None, None, None, None),
    ],
}


RS_TABLES: dict[str, list[tuple[Any, ...]]] = {
    "shop": [
        ("Order Items", "TABLE", None, 25000, 12, None),
        ("customers", "TABLE", "people who buy", 1000, 2, None),
        ("orders", "TABLE", None, 10000, 8, None),
        ("v_open_orders", "VIEW", None, None, None, None),
    ],
    "ops": [
        ("events", "TABLE", None, 500000, 64, None),
        ("sessions", "TABLE", None, 90000, 10, None),
    ],
    "spectrum": [("lake_events", "EXTERNAL TABLE", None, None, None, "lake_events")],
}


def _rs(sql: str, params: Sequence[Any]) -> Rows:
    if sql.startswith("select current_database()"):
        return [{"name": "dev"}]
    if "from svv_all_schemas" in sql:
        return [
            {"schema_name": "ops", "table_count": 2},
            {"schema_name": "shop", "table_count": 4},
            {"schema_name": "spectrum", "table_count": 1},
        ]
    if "from svv_all_tables" in sql:
        return [
            {"table_name": n, "table_type": k, "remarks": c}
            for n, k, c, _r, _s, _e in RS_TABLES[params[1]]
        ]
    if "from svv_table_info" in sql:
        return [
            {"table_name": n, "tbl_rows": r, "size_mb": z}
            for n, _k, _c, r, z, _e in RS_TABLES.get(params[0], [])
            if r is not None
        ]
    if "from svv_external_tables" in sql:
        return [{"table_name": n} for n, _k, _c, _r, _z, e in RS_TABLES.get(params[0], []) if e]
    if "from svv_all_columns" in sql:
        wanted = _pairs(params[1:])
        out: Rows = []
        for (s, t), cs in RS_COLS.items():
            if (s, t) in wanted:
                for i, (c, ty, nl, df, ln, pr, sc, rm) in enumerate(cs, 1):
                    out.append(
                        {"schema_name": s, "table_name": t, "column_name": c,
                         "ordinal_position": i, "data_type": ty, "is_nullable": nl,
                         "column_default": df, "remarks": rm, "character_maximum_length": ln,
                         "numeric_precision": pr, "numeric_scale": sc}
                    )  # fmt: skip
        return out
    if "from svv_external_columns" in sql:
        return [
            {"schemaname": "spectrum", "tablename": "lake_events", "columnname": "event_id",
             "columnnum": 1, "external_type": "bigint", "is_nullable": "true"},
            {"schemaname": "spectrum", "tablename": "lake_events", "columnname": "payload",
             "columnnum": 2, "external_type": "varchar(65535)", "is_nullable": "true"},
            {"schemaname": "spectrum", "tablename": "lake_events", "columnname": "event_date",
             "columnnum": 3, "external_type": "date", "is_nullable": "true"},
        ]  # fmt: skip
    if "from pg_constraint" in sql:
        wanted = _pairs(params[3:])
        spec = [
            ("shop", "customers", "customers_pkey", "p", [("customer_id", None)]),
            ("shop", "customers", "customers_email_key", "u", [("email", None)]),
            ("shop", "orders", "orders_pkey", "p", [("order_id", None)]),
            ("shop", "orders", "orders_customer_fk", "f",
             [("customer_id", ("shop", "customers", "customer_id"))]),
            ("shop", "Order Items", "order_items_pkey", "p",
             [("order_id", None), ("line_no", None)]),
            ("shop", "Order Items", "order_items_order_fk", "f",
             [("order_id", ("shop", "orders", "order_id"))]),
            ("ops", "events", "events_pkey", "p", [("event_id", None)]),
            ("ops", "events", "events_session_fk", "f",
             [("session_id", ("ops", "sessions", "session_id"))]),
            ("ops", "sessions", "sessions_pkey", "p", [("session_id", None)]),
        ]  # fmt: skip
        out = []
        for s, t, cn, ty, cols in spec:
            if (s, t) in wanted:
                for i, (c, ref) in enumerate(cols, 1):
                    out.append(
                        {"schema_name": s, "table_name": t, "constraint_name": cn, "contype": ty,
                         "pos": i, "column_name": c,
                         "ref_schema": ref[0] if ref else None,
                         "ref_table": ref[1] if ref else None,
                         "ref_column": ref[2] if ref else None}
                    )  # fmt: skip
        return out
    raise AssertionError(sql)


# ---------------------------------------------------------------------------------- Databricks
def _dbx(sql: str, params: Sequence[Any]) -> Rows:
    if "information_schema.catalogs" in sql:
        return [{"catalog_name": "lake"}, {"catalog_name": "main"}]
    if "information_schema.schemata" in sql:
        schemas = {"main": [("ops", None), ("shop", "retail data")], "lake": [("raw", None)]}
        return [
            {"catalog_name": params[1], "schema_name": s, "comment": c}
            for s, c in schemas[params[1]]
        ]
    if "information_schema.tables" in sql:
        tables = {
            ("main", "shop"): [("customers", "MANAGED", "people who buy"),
                               ("order-items", "MANAGED", None),
                               ("orders", "MANAGED", None),
                               ("v_open_orders", "VIEW", None)],
            ("main", "ops"): [("events", "MANAGED", None), ("sessions", "MANAGED", None)],
            ("lake", "raw"): [("lake_events", "EXTERNAL", "files in object storage")],
        }  # fmt: skip
        return [
            {"table_name": n, "table_type": k, "comment": c, "created": T0, "last_altered": T1}
            for n, k, c in tables[(params[0], params[1])]
        ]
    triples = {(params[i], params[i + 1], params[i + 2]) for i in range(0, len(params), 3)}
    if "information_schema.columns" in sql:
        cols = {
            ("main", "shop", "customers"): [
                ("customer_id", "bigint", "NO", "surrogate key"),
                ("email", "string", "YES", "login address"),
                ("tags", "array<string>", "YES", None),
            ],
            ("main", "shop", "orders"): [
                ("order_id", "bigint", "NO", None),
                ("customer_id", "bigint", "YES", None),
                ("total", "decimal(10,2)", "YES", None),
            ],
            ("main", "shop", "order-items"): [
                ("order_id", "bigint", "NO", None),
                ("line_no", "int", "NO", None),
            ],
            ("main", "shop", "v_open_orders"): [("order_id", "bigint", "YES", None)],
            ("main", "ops", "events"): [
                ("event_id", "bigint", "NO", None),
                ("session_id", "string", "YES", None),
                ("attrs", "map<string,string>", "YES", None),
            ],
            ("main", "ops", "sessions"): [
                ("session_id", "string", "NO", None),
                ("started_at", "timestamp", "YES", None),
            ],
            ("lake", "raw", "lake_events"): [
                ("event_id", "bigint", "YES", None),
                ("event_date", "date", "YES", None),
            ],
        }
        return [
            {"table_catalog": c, "table_schema": s, "table_name": t, "column_name": n,
             "ordinal_position": i, "full_data_type": ty, "is_nullable": nl,
             "column_default": None, "comment": cm}
            for (c, s, t), cs in cols.items()
            if (c, s, t) in triples
            for i, (n, ty, nl, cm) in enumerate(cs)
        ]  # fmt: skip
    if "key_column_usage" in sql:

        def row(s: str, t: str, cn: str, ty: str, col: str, pos: int, ref: Any = None) -> Any:
            return {"table_catalog": "main", "table_schema": s, "table_name": t,
                    "constraint_name": cn, "constraint_type": ty, "column_name": col,
                    "ordinal_position": pos,
                    "ref_catalog": "main" if ref else None, "ref_schema": ref[0] if ref else None,
                    "ref_table": ref[1] if ref else None,
                    "ref_column": ref[2] if ref else None}  # fmt: skip

        allrows = [
            row("shop", "customers", "pk_customers", "PRIMARY KEY", "customer_id", 1),
            row("shop", "orders", "pk_orders", "PRIMARY KEY", "order_id", 1),
            row("shop", "orders", "fk_orders_customer", "FOREIGN KEY", "customer_id", 1,
                ("shop", "customers", "customer_id")),
            row("shop", "order-items", "pk_items", "PRIMARY KEY", "order_id", 1),
            row("shop", "order-items", "pk_items", "PRIMARY KEY", "line_no", 2),
            row("shop", "order-items", "fk_items_order", "FOREIGN KEY", "order_id", 1,
                ("shop", "orders", "order_id")),
            row("ops", "events", "pk_events", "PRIMARY KEY", "event_id", 1),
            row("ops", "sessions", "pk_sessions", "PRIMARY KEY", "session_id", 1),
            row("ops", "events", "fk_events_session", "FOREIGN KEY", "session_id", 1,
                ("ops", "sessions", "session_id")),
        ]  # fmt: skip
        return [
            r for r in allrows
            if (r["table_catalog"], r["table_schema"], r["table_name"]) in triples
        ]  # fmt: skip
    raise AssertionError(sql)


# ---------------------------------------------------------------------------------- Oracle
def _ora(sql: str, params: Sequence[Any]) -> Rows:
    if "from all_users" in sql:
        return [{"NAME": "OPS", "TABLE_COUNT": 2}, {"NAME": "SHOP", "TABLE_COUNT": 4}]
    if "from all_tables t" in sql:
        tables = {
            "SHOP": [("CUSTOMERS", "TABLE", 1000, "people who buy", None),
                     ("ORDERS", "TABLE", 10000, None, "RANGE"),
                     ("Order Items", "TABLE", 25000, None, None),
                     ("V_OPEN_ORDERS", "VIEW", None, None, None)],
            "OPS": [("EVENTS", "TABLE", 500000, None, None),
                    ("SESSIONS", "TABLE", None, None, None)],
        }  # fmt: skip
        return [
            {"TABLE_NAME": n, "KIND": k, "NUM_ROWS": r, "COMMENTS": c, "PARTITIONING_TYPE": p}
            for n, k, r, c, p in tables[params[0]]
        ]
    if "from all_tab_columns" in sql:
        wanted = _pairs(params)
        cols = {
            ("SHOP", "CUSTOMERS"): [
                ("CUSTOMER_ID", "NUMBER", 22, 0, 10, 0, "N", None, "surrogate key"),
                ("EMAIL", "VARCHAR2", 1280, 320, None, None, "Y", None, "login address"),
                ("CREATED_AT", "TIMESTAMP(6)", 11, 0, None, 6, "Y", "SYSTIMESTAMP ", None),
            ],
            ("SHOP", "ORDERS"): [
                ("ORDER_ID", "NUMBER", 22, 0, 10, 0, "N", None, None),
                ("CUSTOMER_ID", "NUMBER", 22, 0, 10, 0, "Y", None, None),
                ("TOTAL", "NUMBER", 22, 0, 10, 2, "Y", None, None),
                ("PLACED", "DATE", 7, 0, None, None, "Y", None, None),
            ],
            ("SHOP", "Order Items"): [
                ("ORDER_ID", "NUMBER", 22, 0, 10, 0, "N", None, None),
                ("LINE_NO", "NUMBER", 22, 0, 5, 0, "N", None, None),
                ("Unit Price", "BINARY_DOUBLE", 8, 0, None, None, "Y", None, None),
            ],
            ("SHOP", "V_OPEN_ORDERS"): [("ORDER_ID", "NUMBER", 22, 0, 10, 0, "Y", None, None)],
            ("OPS", "EVENTS"): [
                ("EVENT_ID", "NUMBER", 22, 0, 18, 0, "N", None, None),
                ("SESSION_ID", "VARCHAR2", 144, 36, None, None, "Y", None, None),
                ("PAYLOAD", "CLOB", 4000, 0, None, None, "Y", None, "raw event body"),
            ],
            ("OPS", "SESSIONS"): [
                ("SESSION_ID", "VARCHAR2", 144, 36, None, None, "N", None, None),
                ("TOKEN", "RAW", 16, 0, None, None, "Y", None, None),
            ],
        }
        out: Rows = []
        for (o, t), cs in cols.items():
            if (o, t) in wanted:
                for i, (c, ty, dl, cl, dp, ds, nl, df, cm) in enumerate(cs, 1):
                    out.append(
                        {"OWNER": o, "TABLE_NAME": t, "COLUMN_NAME": c, "COLUMN_ID": i,
                         "DATA_TYPE": ty, "DATA_LENGTH": dl, "CHAR_LENGTH": cl,
                         "DATA_PRECISION": dp, "DATA_SCALE": ds, "NULLABLE": nl,
                         "DATA_DEFAULT": df, "COMMENTS": cm}
                    )  # fmt: skip
        return out
    if "from all_constraints k" in sql:
        wanted = _pairs(params[3:])
        spec = [
            ("SHOP", "CUSTOMERS", "PK_CUSTOMERS", "P", [("CUSTOMER_ID", None)]),
            ("SHOP", "CUSTOMERS", "UQ_CUSTOMERS_EMAIL", "U", [("EMAIL", None)]),
            ("SHOP", "ORDERS", "PK_ORDERS", "P", [("ORDER_ID", None)]),
            ("SHOP", "ORDERS", "FK_ORDERS_CUSTOMER", "R",
             [("CUSTOMER_ID", ("SHOP", "CUSTOMERS", "CUSTOMER_ID"))]),
            ("SHOP", "Order Items", "PK_ORDER_ITEMS", "P", [("ORDER_ID", None), ("LINE_NO", None)]),
            ("SHOP", "Order Items", "FK_ITEMS_ORDER", "R",
             [("ORDER_ID", ("SHOP", "ORDERS", "ORDER_ID"))]),
            ("OPS", "EVENTS", "PK_EVENTS", "P", [("EVENT_ID", None)]),
            ("OPS", "SESSIONS", "PK_SESSIONS", "P", [("SESSION_ID", None)]),
            ("OPS", "EVENTS", "FK_EVENTS_SESSION", "R",
             [("SESSION_ID", ("OPS", "SESSIONS", "SESSION_ID"))]),
        ]  # fmt: skip
        out = []
        for o, t, cn, ty, cols2 in spec:
            if (o, t) in wanted:
                for i, (c, ref) in enumerate(cols2, 1):
                    out.append(
                        {"OWNER": o, "TABLE_NAME": t, "CONSTRAINT_NAME": cn,
                         "CONSTRAINT_TYPE": ty, "STATUS": "DISABLED" if ty == "R" else "ENABLED",
                         "COLUMN_NAME": c, "POSITION": i,
                         "REF_OWNER": ref[0] if ref else None,
                         "REF_TABLE": ref[1] if ref else None,
                         "REF_COLUMN": ref[2] if ref else None}
                    )  # fmt: skip
        return out
    raise AssertionError(sql)


SCRIPTS: dict[str, Handler] = {
    "bigquery": _bq,
    "snowflake": _sf,
    "redshift": _rs,
    "databricks": _dbx,
    "oracle": _ora,
}


def main(names: Sequence[str]) -> None:
    for name in names or list(SCRIPTS):
        record(lambda ex, n=name: reader_for(n, ex), Scripted(SCRIPTS[name]), name)
        print("recorded", name)


if __name__ == "__main__":
    main(sys.argv[1:])
