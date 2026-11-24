"""Bring your own connection (spec 2 G2): Guru wraps what a host already holds.

A raw DB-API connection, a SQLAlchemy engine, a BigQuery-shaped client and a plain one-argument
``execute(sql)`` callable are each accepted; anything else is ``ConfigError`` ``SG107``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

import schemaguru as sg
from schemaguru.catalog import FactLangExecutor
from schemaguru.catalog.executor import ACCEPTED_CONNECTORS, InlineConnector, as_connector
from schemaguru.errors import ConfigError
from tests.fixtures.shop import build_shop

duckdb = pytest.importorskip("duckdb")
TABLES = 14


@pytest.fixture(scope="module")
def shop_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("byo") / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    build_shop(conn, "duckdb")
    conn.close()
    return path


def _crawl_and_measure(guru: sg.Guru) -> str:
    assert guru.crawl().tables_seen == TABLES
    report = guru.measure("shop.orders")
    assert not report.errors, report.errors
    return guru.context("orders by status").text


def test_raw_duckdb_connection(shop_path: Path, tmp_path: Path) -> None:
    conn = duckdb.connect(str(shop_path), read_only=True)
    guru = sg.Guru(connector=conn, store=str(tmp_path / "s.db"))  # dialect from the driver
    assert guru.dialect == "duckdb"
    assert "10,000 rows (measured" in _crawl_and_measure(guru)
    guru.close()
    conn.close()


def _dict_rows(conn: Any, sql: str) -> list[dict[str, Any]]:
    cur = conn.cursor()  # FactLang may run probes in parallel: one cursor per call
    cur.execute(sql)
    names = [d[0] for d in cur.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def test_one_argument_execute_callable(shop_path: Path, tmp_path: Path) -> None:
    conn = duckdb.connect(str(shop_path), read_only=True)
    seen: list[str] = []

    def execute(sql: str) -> list[dict[str, Any]]:
        seen.append(sql)
        return _dict_rows(conn, sql)

    guru = sg.Guru(connector=execute, dialect="duckdb", store=str(tmp_path / "s.db"))
    assert isinstance(guru.connector, InlineConnector)
    assert "10,000 rows (measured" in _crawl_and_measure(guru)
    assert seen and not any("?" in s or "{{" in s for s in seen)  # values inlined
    guru.close()
    conn.close()


def test_bigquery_shaped_client(shop_path: Path, tmp_path: Path) -> None:
    """Anything with ``query(sql).result()`` (google-cloud-bigquery's Client shape)."""
    conn = duckdb.connect(str(shop_path), read_only=True)

    class Job:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self.rows = rows

        def result(self) -> list[dict[str, Any]]:
            return self.rows

    class Client:
        def query(self, sql: str) -> Job:
            return Job(_dict_rows(conn, sql))

    # the fake runs DuckDB SQL, so it says so; a real client defaults to dialect "bigquery"
    guru = sg.Guru(connector=Client(), dialect="duckdb", store=str(tmp_path / "s.db"))
    assert "10,000 rows (measured" in _crawl_and_measure(guru)
    assert as_connector(Client()).dialect == "bigquery"
    guru.close()
    conn.close()


def test_sqlite3_dbapi_connection(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "w.sqlite"), check_same_thread=False)
    conn.execute("CREATE TABLE orders (order_id INTEGER PRIMARY KEY)")
    conn.commit()
    guru = sg.Guru(connector=conn, store=str(tmp_path / "s.db"))
    assert guru.dialect == "sqlite" and guru.connector.dialect == "sqlite"
    rows = FactLangExecutor(guru.connector).run(
        "SELECT name FROM sqlite_master WHERE type = ?", ("table",)
    )
    assert rows == [{"name": "orders"}]  # metadata SQL runs through the wrapped connection
    assert guru.reader is None and any(d.code == "SG101" for d in guru.diagnostics)  # no reader
    guru.close()
    conn.close()


def test_sqlalchemy_engine(tmp_path: Path) -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'e.sqlite'}")
    with engine.begin() as c:
        c.execute(sqlalchemy.text("CREATE TABLE payments (payment_id INTEGER PRIMARY KEY)"))
    guru = sg.Guru(connector=engine, store=str(tmp_path / "s.db"))
    assert type(guru.connector).__name__ == "SQLAlchemyConnector"
    assert guru.dialect == "sqlite"
    rows = FactLangExecutor(guru.connector).run(
        "SELECT name FROM sqlite_master WHERE type = ?", ("table",)
    )
    assert rows == [{"name": "payments"}]
    guru.close()
    engine.dispose()


@pytest.mark.parametrize("thing", [object(), 42, "duckdb:///x.duckdb"])
def test_unknown_object_is_a_config_error(thing: Any, tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        sg.Guru(connector=thing, store=str(tmp_path / "s.db"))
    msg = str(info.value)
    assert msg.startswith("SG107") and ACCEPTED_CONNECTORS in msg


def test_callable_and_unknown_driver_need_a_dialect(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"SG107.*dialect"):
        as_connector(lambda sql: [])

    class Conn:  # a DB-API connection from a driver SchemaGuru does not know
        def cursor(self) -> Any:
            raise AssertionError

    with pytest.raises(ConfigError, match=r"SG107.*dialect"):
        as_connector(Conn())
    assert as_connector(Conn(), "postgres").dialect == "postgres"


def test_inline_literals_cannot_escape() -> None:
    """Values the host cannot bind are inlined escaped for the dialect; NUL is refused."""
    from factlang.connectors.base import ExecutionError
    from factlang.connectors.base import run_metadata as fl_run

    got: list[str] = []
    ansi = InlineConnector(lambda sql: got.append(sql) or [], "duckdb")
    fl_run(ansi, "SELECT 1 AS x WHERE {{v}} = 'a'", {"v": "it's"})
    assert "'it''s'" in got[-1]
    bq = InlineConnector(lambda sql: got.append(sql) or [], "bigquery")
    fl_run(bq, "SELECT 1 AS x WHERE {{v}} = 'a'", {"v": "a\\' OR 1=1 --"})
    assert "'a\\\\\\' OR 1=1 --'" in got[-1]
    with pytest.raises(ExecutionError):
        fl_run(ansi, "SELECT 1 AS x WHERE {{v}} = 'a'", {"v": "a\x00b"})
