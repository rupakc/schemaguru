"""Seeded schemas for the container-backed engines (shared by the integration tests and by
``tests.conformance.record``). Every seed builds the same generic shop shape: two schemas, a
table with a comment, a composite primary key and a foreign key (where the engine has them), a
view and an identifier that needs quoting. Seeds drop and recreate their schemas, so they are
re-runnable."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

from schemaguru.catalog import DBAPIExecutor
from schemaguru.catalog.executor import MetadataExecutor


@dataclass(frozen=True)
class Engine:
    name: str  # FactLang dialect name
    driver: str  # module that must import
    connect: Callable[[str], Any]
    seed: Callable[[Any], None]
    executor: Callable[[Any], MetadataExecutor]
    only_catalogs: tuple[str, ...] | None = None  # restrict a federated engine's crawl


def _mysql_connect(dsn: str) -> Any:
    import pymysql

    u = urlparse(dsn)
    return pymysql.connect(
        host=u.hostname or "127.0.0.1",
        port=u.port or 3306,
        user=unquote(u.username or "root"),
        password=unquote(u.password or ""),
        autocommit=True,
    )


def _mysql_seed(conn: Any) -> None:
    cur = conn.cursor()
    for stmt in (
        "DROP DATABASE IF EXISTS sg_shop",
        "DROP DATABASE IF EXISTS sg_ops",
        "CREATE DATABASE sg_shop",
        "CREATE DATABASE sg_ops",
        "CREATE TABLE sg_shop.customers(customer_id INT PRIMARY KEY, "
        "email VARCHAR(255) NOT NULL COMMENT 'login address', note TEXT, "
        "score DECIMAL(10,2) DEFAULT 1.50, seen DATETIME, flags MEDIUMINT UNSIGNED) "
        "COMMENT='people who buy'",
        "CREATE TABLE sg_shop.`Order Items`(order_id INT, line_no INT, customer_id INT, "
        "qty SMALLINT, PRIMARY KEY (order_id, line_no), "
        "CONSTRAINT fk_cust FOREIGN KEY (customer_id) REFERENCES sg_shop.customers(customer_id), "
        "UNIQUE KEY uq_cust_line (customer_id, line_no))",
        "CREATE TABLE sg_shop.products(product_id INT PRIMARY KEY, name VARCHAR(100), "
        "body LONGTEXT, payload JSON)",
        "CREATE VIEW sg_shop.v_customers AS SELECT customer_id, email FROM sg_shop.customers",
        "CREATE TABLE sg_ops.events(event_id BIGINT PRIMARY KEY, at TIMESTAMP NULL, "
        "kind ENUM('a','b'))",
        "CREATE TABLE sg_ops.`odd``name`(id INT)",
        "INSERT INTO sg_shop.customers(customer_id, email) VALUES (1, 'a@x.test'), (2, 'b@x.test')",
        "ANALYZE TABLE sg_shop.customers",
    ):
        cur.execute(stmt)
        cur.fetchall()
    cur.close()


def _clickhouse_args(dsn: str) -> dict[str, Any]:
    u = urlparse(dsn)
    return {
        "host": u.hostname or "127.0.0.1",
        "port": u.port or 8123,
        "username": unquote(u.username or "default"),
        "password": unquote(u.password or ""),
    }


def _clickhouse_connect(dsn: str) -> Any:
    from clickhouse_connect import dbapi

    return dbapi.connect(**_clickhouse_args(dsn))


def _clickhouse_seed(conn: Any) -> None:
    client = conn.client
    for stmt in (
        "DROP DATABASE IF EXISTS sg_shop",
        "DROP DATABASE IF EXISTS sg_ops",
        "CREATE DATABASE sg_shop",
        "CREATE DATABASE sg_ops",
        "CREATE TABLE sg_shop.customers(customer_id UInt32, "
        "email LowCardinality(Nullable(String)) COMMENT 'login address', "
        "note String DEFAULT 'x', score Decimal(10,2), seen Nullable(DateTime64(3)), "
        "tags Array(String), ok Bool) ENGINE = MergeTree PARTITION BY intDiv(customer_id, 1000) "
        "ORDER BY (customer_id, note) COMMENT 'people who buy'",
        "CREATE TABLE sg_shop.`Order Items`(order_id UInt64, line_no UInt32, qty Int16, "
        "meta Map(String, String), alias_col UInt8 ALIAS qty + 1) ENGINE = MergeTree "
        "ORDER BY (order_id, line_no)",
        "CREATE TABLE sg_shop.products(product_id UInt32, name String) ENGINE = MergeTree "
        "ORDER BY product_id",
        "CREATE VIEW sg_shop.v_customers AS SELECT customer_id, email FROM sg_shop.customers",
        "CREATE TABLE sg_ops.events(event_id UInt64, at DateTime, kind Enum8('a' = 1, 'b' = 2)) "
        "ENGINE = MergeTree ORDER BY toDate(at)",
        "CREATE TABLE sg_ops.mem(id Int8) ENGINE = Memory",
        "INSERT INTO sg_shop.customers(customer_id, email) VALUES (1, 'a@x.test'), (2, 'b@x.test')",
    ):
        client.command(stmt)


def _tsql_connect(dsn: str) -> Any:
    """ODBC connection string (CI), or ``pymssql://user:pw@host:port`` for a driver-less laptop."""
    if dsn.startswith("pymssql://"):
        import pymssql

        u = urlparse(dsn)
        return pymssql.connect(
            server=u.hostname,
            port=str(u.port or 1433),
            user=unquote(u.username or "sa"),
            password=unquote(u.password or ""),
            autocommit=True,
        )
    import pyodbc

    return pyodbc.connect(dsn, autocommit=True)


def _tsql_executor(conn: Any) -> MetadataExecutor:
    style = "format" if type(conn).__module__.startswith("pymssql") else "qmark"
    return DBAPIExecutor(conn, style)


def _tsql_seed(conn: Any) -> None:
    cur = conn.cursor()
    for stmt in (
        "IF DB_ID('sg_test') IS NOT NULL BEGIN ALTER DATABASE sg_test SET SINGLE_USER "
        "WITH ROLLBACK IMMEDIATE; DROP DATABASE sg_test END",
        "CREATE DATABASE sg_test",
        "USE sg_test",
        "EXEC('CREATE SCHEMA sg_shop')",
        "EXEC('CREATE SCHEMA sg_ops')",
        "CREATE TABLE sg_shop.customers(customer_id int NOT NULL PRIMARY KEY, "
        "email nvarchar(255) NOT NULL, note nvarchar(max) NULL, "
        "score decimal(10,2) NULL DEFAULT 1.50, seen datetime2(3) NULL, ok bit NULL)",
        "CREATE TABLE sg_shop.[Order Items](order_id int NOT NULL, line_no int NOT NULL, "
        "customer_id int NULL, qty smallint NULL, PRIMARY KEY (order_id, line_no), "
        "CONSTRAINT fk_cust FOREIGN KEY (customer_id) REFERENCES sg_shop.customers(customer_id), "
        "CONSTRAINT uq_cust_line UNIQUE (customer_id, line_no))",
        "CREATE TABLE sg_shop.products(product_id int NOT NULL PRIMARY KEY, name varchar(100), "
        "payload varbinary(max), token uniqueidentifier)",
        "EXEC('CREATE VIEW sg_shop.v_customers AS "
        "SELECT customer_id, email FROM sg_shop.customers')",
        "CREATE TABLE sg_ops.events(event_id bigint NOT NULL PRIMARY KEY, at datetimeoffset NULL, "
        "kind char(1) NULL)",
        "CREATE TABLE sg_ops.[odd]]name](id int)",
        "INSERT INTO sg_shop.customers(customer_id, email) VALUES (1, 'a@x.test'), (2, 'b@x.test')",
        "EXEC sys.sp_addextendedproperty @name = N'MS_Description', @value = N'people who buy', "
        "@level0type = N'SCHEMA', @level0name = N'sg_shop', @level1type = N'TABLE', "
        "@level1name = N'customers'",
        "EXEC sys.sp_addextendedproperty @name = N'MS_Description', @value = N'login address', "
        "@level0type = N'SCHEMA', @level0name = N'sg_shop', @level1type = N'TABLE', "
        "@level1name = N'customers', @level2type = N'COLUMN', @level2name = N'email'",
        "EXEC sys.sp_addextendedproperty @name = N'MS_Description', @value = N'storefront', "
        "@level0type = N'SCHEMA', @level0name = N'sg_shop'",
    ):
        cur.execute(stmt)
        while cur.nextset() if hasattr(cur, "nextset") else False:
            pass
    cur.close()


def _trino_connect(dsn: str) -> Any:
    import trino

    u = urlparse(dsn)
    return trino.dbapi.connect(
        host=u.hostname or "127.0.0.1",
        port=u.port or 8080,
        user=unquote(u.username or "schemaguru"),
        catalog=(u.path or "/memory").lstrip("/") or "memory",
    )


def _trino_seed(conn: Any) -> None:
    cur = conn.cursor()
    for stmt in (
        "DROP VIEW IF EXISTS memory.sg_shop.v_customers",
        "DROP TABLE IF EXISTS memory.sg_shop.customers",
        'DROP TABLE IF EXISTS memory.sg_shop."Order Items"',
        "DROP TABLE IF EXISTS memory.sg_shop.products",
        "DROP TABLE IF EXISTS memory.sg_ops.events",
        'DROP TABLE IF EXISTS memory.sg_ops."odd name"',
        "DROP SCHEMA IF EXISTS memory.sg_shop",
        "DROP SCHEMA IF EXISTS memory.sg_ops",
        "CREATE SCHEMA memory.sg_shop",
        "CREATE SCHEMA memory.sg_ops",
        "CREATE TABLE memory.sg_shop.customers(customer_id integer, "
        "email varchar COMMENT 'login address', note varchar, score decimal(10,2), "
        "seen timestamp(3) with time zone, tags array(varchar), "
        "addr row(city varchar, zip varchar)) COMMENT 'people who buy'",
        'CREATE TABLE memory.sg_shop."Order Items"(order_id bigint, line_no integer, '
        "customer_id integer, qty smallint, meta map(varchar, varchar))",
        "CREATE TABLE memory.sg_shop.products(product_id integer, name varchar(100), "
        "payload varbinary, ok boolean)",
        "CREATE VIEW memory.sg_shop.v_customers AS SELECT customer_id, email "
        "FROM memory.sg_shop.customers",
        "CREATE TABLE memory.sg_ops.events(event_id bigint, at date, kind char(1))",
        'CREATE TABLE memory.sg_ops."odd name"(id integer)',
    ):
        cur.execute(stmt)
        cur.fetchall()
    cur.close()


def _dbapi(style: str) -> Callable[[Any], MetadataExecutor]:
    return lambda conn: DBAPIExecutor(conn, style)


ENGINES: dict[str, Engine] = {
    "mysql": Engine("mysql", "pymysql", _mysql_connect, _mysql_seed, _dbapi("format")),
    "clickhouse": Engine(
        "clickhouse", "clickhouse_connect", _clickhouse_connect, _clickhouse_seed, _dbapi("format")
    ),
    "tsql": Engine("tsql", "pyodbc", _tsql_connect, _tsql_seed, _tsql_executor),
    "trino": Engine(
        "trino", "trino", _trino_connect, _trino_seed, _dbapi("qmark"), only_catalogs=("memory",)
    ),
}


def import_driver(engine: Engine) -> None:
    importlib.import_module(engine.driver)
