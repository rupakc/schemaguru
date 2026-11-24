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


# --- inline mode: a host with no bind parameters (round 2, CRITICAL injection fix) ----------

PAYLOAD = "x\\' OR 1=1 --"  # x\' OR 1=1 --  : ends the literal where \' is an escape


def _probe(template: str, dialect: str, **values: object) -> Any:
    """A rendered probe straight to the connector (no FactLang guard in front of it)."""
    from factlang.ir import Arity
    from factlang.sql import Param, RenderedProbe

    params = tuple(
        Param(k, v, "string" if isinstance(v, str) else "integer") for k, v in values.items()
    )
    return RenderedProbe(template, params, Arity.ROW_SET, None, dialect, "best_effort")


def _escapes(sql: str) -> bool:
    """Would ``OR 1=1`` run as code on a server that reads ``\\'`` as an escape (MySQL,
    Snowflake, Postgres with standard_conforming_strings=off) or on one that does not?"""
    from schemaguru._util.sqltext import Opts, scan

    for backslash in (False, True):
        chars = scan(sql, Opts(backslash=backslash))
        code = "".join(" " if lit else c for c, lit in chars)
        if "OR 1=1" in code:
            return True
    return False


def test_old_backslash_and_ansi_quoters_let_the_payload_escape() -> None:
    """Negative control: the round-1 quoters produce SQL the escape check flags."""

    def old_ansi(t: str) -> str:
        return "'" + t.replace("'", "''") + "'"

    def old_backslashed(t: str) -> str:
        return "'" + t.replace("\\", "\\\\").replace("'", "\\'") + "'"

    # quoted directly: FactLang 0.1.1 refuses a backslash before any host quoter runs
    template = "SELECT 1 AS x WHERE status = "
    assert _escapes(template + old_ansi(PAYLOAD))
    no_backslash_escapes = "' OR 1=1 -- "  # MySQL with NO_BACKSLASH_ESCAPES
    assert _escapes(template + old_backslashed(no_backslash_escapes))
    assert not _escapes("SELECT 1 AS x WHERE status = 'it''s'")


@pytest.mark.parametrize("dialect", sorted({*sg.catalog.dialects(), "sqlite"}))
def test_inline_refuses_the_injection_on_every_dialect(dialect: str) -> None:
    from factlang.connectors import ExecutionError

    got: list[str] = []
    conn = InlineConnector(lambda sql: got.append(sql) or [], dialect)
    with pytest.raises(ExecutionError, match=r"SG109.*backslash"):
        conn.execute(
            _probe("SELECT 1 AS x WHERE status = {{v}}", dialect, v=PAYLOAD), timeout_s=None
        )
    assert got == []  # the host never saw it


def test_inline_injection_end_to_end_through_check_fact(shop_path: Path, tmp_path: Path) -> None:
    """The reviewer's repro: an agent's claim through an execute(sql) host is refused."""
    conn = duckdb.connect(str(shop_path), read_only=True)
    seen: list[str] = []

    def execute(sql: str) -> list[dict[str, Any]]:
        seen.append(sql)
        return _dict_rows(conn, sql)

    guru = sg.Guru(
        connector=execute,
        dialect="duckdb",
        store=str(tmp_path / "s.db"),
        config=sg.Config(agent_checks="allow"),
    )
    guru.crawl()
    before = len(seen)
    claim = "ROWS shop.orders VALUE >=0 WHERE status = '" + PAYLOAD.replace("'", "''") + "'"
    out = guru.handle_tool_call("check_fact", {"claims": claim})
    assert "OR 1=1" not in "".join(seen[before:])
    assert not [s for s in seen[before:] if _escapes(s)]
    assert [e["verdict"] for e in out["evidence"]] == ["error"]  # refused, not judged
    assert len(seen) == before  # before the host ran anything
    guru.close()
    conn.close()


@pytest.mark.parametrize(
    "value",
    [["a", "b"], float("nan"), float("inf"), -float("inf"), {"a": 1}, b"x", "a\x00b"],
)
def test_inline_refuses_non_scalars_and_non_finite(value: object) -> None:
    from factlang.connectors import ExecutionError
    from factlang.sql import Param

    from schemaguru.catalog.executor import inline_problem

    assert inline_problem(value) is not None
    from factlang.ir import Arity
    from factlang.sql import RenderedProbe

    conn = InlineConnector(lambda sql: [], "duckdb")
    template = "SELECT 1 AS x WHERE c = {{v}}"
    probe = RenderedProbe(
        template, (Param("v", value, "string"),), Arity.ROW_SET, None, "duckdb", "best_effort"
    )
    with pytest.raises(ExecutionError, match="SG109"):
        conn.execute(probe, timeout_s=None)


def test_inline_scalars_ansi_doubling() -> None:
    import datetime
    from decimal import Decimal

    from schemaguru.catalog.executor import inline_problem

    for ok in ("O'Brien", 3, True, 1.5, Decimal("2.50"), datetime.date(2026, 1, 2), None):
        assert inline_problem(ok) is None, ok
    got: list[str] = []
    conn = InlineConnector(lambda sql: got.append(sql) or [], "mysql")
    conn.execute(_probe("SELECT 1 AS x WHERE name = {{v}}", "mysql", v="O'Brien"), timeout_s=None)
    assert got == ["SELECT 1 AS x WHERE name = 'O''Brien'"]
    assert not _escapes(got[0])


@pytest.mark.parametrize(
    "template",
    [
        "SELECT 1 AS x WHERE c = {{v}}; DELETE FROM shop.orders",
        "SELECT 1 AS x WHERE c = {{v}} -- the rest is a comment",
        "DELETE FROM shop.orders WHERE c = {{v}}",
    ],
)
def test_inline_final_sql_goes_through_the_read_only_guard(template: str) -> None:
    from factlang.connectors import ExecutionError

    got: list[str] = []
    conn = InlineConnector(lambda sql: got.append(sql) or [], "duckdb")
    with pytest.raises(ExecutionError, match="SG104"):
        conn.execute(_probe(template, "duckdb", v="ok"), timeout_s=None)
    assert got == []


# --- round 3: subclasses whose __str__ lies -------------------------------------------------


class LyingStr(str):
    def __str__(self) -> str:
        return "x\\' OR 1=1 OR \\'"


class LyingInt(int):
    def __str__(self) -> str:
        return "1 OR 1=1"

    def __repr__(self) -> str:
        return "1 OR 1=1"


class LyingFloat(float):
    def __repr__(self) -> str:
        return "1 OR 1=1"


class LyingDecimal(__import__("decimal").Decimal):
    def __str__(self) -> str:
        return "1 OR 1=1"


class LyingDate(__import__("datetime").date):
    def isoformat(self) -> str:
        return "2026-01-01' OR 1=1 OR '"


class PlainStr(str):  # like numpy.str_: a subclass that keeps its content
    pass


def _raw_probe(value: object, family: str) -> Any:
    from factlang.ir import Arity
    from factlang.sql import Param, RenderedProbe

    p = (Param("v", value, family),)
    return RenderedProbe("SELECT 1 AS x WHERE a = {{v}}", p, Arity.ROW_SET, None, "duckdb", "x")


LIARS = [
    (LyingStr("ok"), "string", "'ok'"),
    (LyingInt(1), "integer", "1"),
    (LyingFloat(1.5), "float", "1.5"),
    (LyingDecimal("2.5"), "decimal", "2.5"),
    (LyingDate(2026, 1, 2), "date", "DATE '2026-01-02'"),
]


def test_lying_subclasses_inject_without_normalising() -> None:
    """Negative control: rendering the raw subclasses (what round 2 sent) lets OR 1=1 escape.
    (FactLang 0.1.1 refuses LyingStr itself, for its backslashes, and formats a Decimal
    without ``str``; a lying int still escapes through FactLang's own inlining.)"""
    from schemaguru.catalog.executor import _ansi

    for value, family, _ in (LIARS[1],):
        probe = _raw_probe(value, family)
        assert _escapes(probe.inline(_ansi)), value


@pytest.mark.parametrize("dialect", sorted({*sg.catalog.dialects(), "sqlite"}))
@pytest.mark.parametrize(("value", "family", "literal"), LIARS)
def test_subclasses_are_normalised_to_their_base_content(
    dialect: str, value: object, family: str, literal: str
) -> None:
    got: list[str] = []
    conn = InlineConnector(lambda sql: got.append(sql) or [], dialect)
    probe = _raw_probe(value, family)
    conn.execute(type(probe)(probe.sql_template, probe.params, probe.arity, None, dialect, "x"),
                 timeout_s=None)  # fmt: skip
    assert got == [f"SELECT 1 AS x WHERE a = {literal}"]  # the base content, never the lie
    assert not _escapes(got[0])


def test_plain_str_subclass_keeps_its_content() -> None:
    got: list[str] = []
    InlineConnector(lambda sql: got.append(sql) or [], "snowflake").execute(
        _raw_probe(PlainStr("O'Brien"), "string"), timeout_s=None
    )
    assert got == ["SELECT 1 AS x WHERE a = 'O''Brien'"]


def test_literal_recheck_catches_what_normalising_would_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt and braces: with normalising switched off, the per-literal re-check still refuses
    the lying subclasses (SG109) before the host runs."""
    from factlang.connectors import ExecutionError

    from schemaguru.catalog import executor

    monkeypatch.setattr(executor, "plain_scalar", lambda v: v)
    got: list[str] = []
    conn = InlineConnector(lambda sql: got.append(sql) or [], "snowflake")
    for value, family in ((LyingStr("ok"), "string"), (LyingInt(1), "integer")):
        with pytest.raises(ExecutionError, match="SG109"):
            conn.execute(_raw_probe(value, family), timeout_s=None)
    assert got == []
    assert executor._LITERAL.fullmatch("'a''b'") and not executor._LITERAL.fullmatch("'a\\'b'")


# --- FactLang 0.1.1 inline rules: no doubled quote on BigQuery/Databricks; FL500 -> SG109 ------


@pytest.mark.parametrize("dialect", ["bigquery", "databricks"])
def test_inline_refuses_a_quote_where_doubling_is_not_an_escape(dialect: str) -> None:
    """On BigQuery ``'O''Brien'`` is a syntax error and Databricks concatenates adjacent
    literals (``OBrien``: a silently wrong value), so a ``'`` cannot be inlined there."""
    from factlang.connectors import ExecutionError

    got: list[str] = []
    conn = InlineConnector(lambda sql: got.append(sql) or [], dialect)
    with pytest.raises(ExecutionError, match=r"SG109.*'") as info:
        conn.execute(
            _probe("SELECT 1 AS x WHERE name = {{v}}", dialect, v="O'Brien"), timeout_s=None
        )
    assert "FL500" not in str(info.value).split("SG109", 1)[1]
    assert got == []
    # the same dialect inlines text without a quote (negative control)
    conn.execute(_probe("SELECT 1 AS x WHERE name = {{v}}", dialect, v="Brien"), timeout_s=None)
    assert got == ["SELECT 1 AS x WHERE name = 'Brien'"]


def test_inline_maps_factlang_fl500_to_sg109() -> None:
    """A value FactLang refuses to inline (here an int in the boolean family) is SG109 with a
    clear message, never FactLang's raw FL500."""
    from factlang.connectors import ExecutionError
    from factlang.ir import Arity
    from factlang.sql import Param, RenderedProbe

    got: list[str] = []
    conn = InlineConnector(lambda sql: got.append(sql) or [], "duckdb")
    probe = RenderedProbe(
        "SELECT 1 AS x WHERE c = {{v}}",
        (Param("v", 1, "boolean"),),
        Arity.ROW_SET,
        None,
        "duckdb",
        "best_effort",
    )
    with pytest.raises(ExecutionError, match="SG109") as info:
        conn.execute(probe, timeout_s=None)
    msg = str(info.value)
    assert not msg.startswith("FL500") and "FL500" not in msg.split("SG109", 1)[1]
    assert "cannot be inlined" in msg and got == []
