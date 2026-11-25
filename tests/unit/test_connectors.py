"""Bring your own connection (spec 2 G2): Guru wraps what a host already holds.

A raw DB-API connection, a SQLAlchemy engine, a BigQuery-shaped client and a plain one-argument
``execute(sql)`` callable are each accepted; anything else is ``ConfigError`` ``SG107``.
"""

from __future__ import annotations

import datetime
import re
import sqlite3
from dataclasses import replace
from decimal import Decimal
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


def _naive_inline(
    template: str, params: tuple[Any, ...], quote: Any = None, style: Any = None
) -> str:
    """What an inliner that trusts a value's own dunders would write (FactLang <= 0.1.1 did
    for ints): ``str()`` of the value, quoted for text, prefixed for dates."""
    from schemaguru.catalog.executor import _ansi

    p = params[0]
    v = p.value
    if isinstance(v, str):
        lit = _ansi(str(v))
    elif isinstance(v, __import__("datetime").date):
        lit = f"DATE '{v.isoformat()}'"
    else:
        lit = str(v) if not isinstance(v, float) else repr(v)
    return template.replace("{{" + p.name + "}}", lit)


# FactLang 0.1.2 spells T-SQL text N'...' and dates as ISO casts
_TSQL_SPELLING = {"'ok'": "N'ok'", "DATE '2026-01-02'": "CAST('2026-01-02' AS DATE)"}


def test_lying_subclasses_inject_without_normalising() -> None:
    """Negative control: spelled from their own dunders, the lying subclasses let OR 1=1
    escape. FactLang 0.1.2 refuses every subclass itself (FL507), so the threat is shown on
    the naive spelling SchemaGuru's own layers (plain_scalar, the literal re-check) guard
    against, not on FactLang."""
    from factlang.sql import RenderError

    from schemaguru.catalog.executor import plain_scalar

    for value, family, _ in LIARS:
        probe = _raw_probe(value, family)
        assert _escapes(_naive_inline(probe.sql_template, probe.params)), value
        with pytest.raises(RenderError, match="FL507"):  # FactLang 0.1.2 refuses it first
            probe.inline()
        base = plain_scalar(value)
        assert type(base) in (str, int, float, __import__("decimal").Decimal,
                              __import__("datetime").date)  # fmt: skip
        assert not _escapes(
            _naive_inline(probe.sql_template, (replace(probe.params[0], value=base),))
        )


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
    want = _TSQL_SPELLING.get(literal, literal) if dialect == "tsql" else literal
    assert got == [f"SELECT 1 AS x WHERE a = {want}"]  # the base content, never the lie
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
    """Belt and braces: with normalising switched off AND an inliner that trusts the value's
    dunders (FactLang 0.1.2 would refuse the subclass itself, so it is replaced here to reach
    SchemaGuru's own layer), the per-literal re-check still refuses every lie (SG109) before
    the host runs."""
    from factlang.connectors import ExecutionError
    from factlang.sql import probe as fl_probe

    from schemaguru.catalog import executor

    monkeypatch.setattr(executor, "plain_scalar", lambda v: v)
    monkeypatch.setattr(fl_probe, "inline_template", _naive_inline)
    got: list[str] = []
    conn = InlineConnector(lambda sql: got.append(sql) or [], "snowflake")
    for value, family, _ in LIARS:
        if isinstance(value, str):
            continue  # LyingStr: SchemaGuru's own backslash refusal answers first
        with pytest.raises(ExecutionError, match=r"SG109.*did not render"):
            conn.execute(_raw_probe(value, family), timeout_s=None)
    assert got == []
    # with both swapped out, the naive inliner alone would have let them through
    assert _escapes(
        _naive_inline("SELECT 1 AS x WHERE a = {{v}}", _raw_probe(LyingInt(1), "integer").params)
    )
    assert executor.literal_matches("'a''b'", "a'b", "snowflake")
    assert not executor.literal_matches("'a\\'b'", "a'b", "snowflake")


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


def test_inline_maps_factlang_refusal_to_sg109() -> None:
    """A value FactLang refuses to inline (here an int in the boolean family: FL507 since
    0.1.2) is SG109 with a clear message, never FactLang's raw code."""
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
    assert msg.startswith("FL700 SG109:")  # the connector code, then SchemaGuru's
    assert not re.search(r"FL5\d\d", msg.split("SG109", 1)[1])
    assert "cannot be inlined" in msg and got == []


# --- FactLang 0.1.2 inline rules: exact types, FL507, lexer-driven quotes, line terminators ----


def _typed_probe(value: object, family: str, dialect: str) -> Any:
    from factlang.ir import Arity
    from factlang.sql import Param, RenderedProbe

    return RenderedProbe(
        "SELECT 1 AS x WHERE a = {{v}}", (Param("v", value, family),), Arity.ROW_SET, None,
        dialect, "best_effort",
    )  # fmt: skip


def test_inline_tsql_text_is_a_national_literal_and_passes_the_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from schemaguru.catalog import executor

    checked: list[tuple[str, str]] = []
    real = executor.literal_matches

    def spy(lit: str, value: object, dialect: str) -> bool:
        checked.append((lit, dialect))
        return real(lit, value, dialect)

    monkeypatch.setattr(executor, "literal_matches", spy)
    got: list[str] = []
    InlineConnector(lambda sql: got.append(sql) or [], "tsql").execute(
        _probe("SELECT 1 AS x WHERE name = {{v}}", "tsql", v="O'Brien"), timeout_s=None
    )
    assert got == ["SELECT 1 AS x WHERE name = N'O''Brien'"]
    assert checked == [("N'O''Brien'", "tsql")]
    assert not _escapes(got[0])


_TS = datetime.datetime(2026, 1, 2, 3, 4, 5, 600, tzinfo=datetime.timezone.utc)
_D = datetime.date(2026, 1, 2)


@pytest.mark.parametrize(
    ("dialect", "value", "family", "literal"),
    [
        ("tsql", True, "boolean", "1"),
        ("tsql", False, "boolean", "0"),
        ("oracle", True, "boolean", "1"),
        ("oracle", False, "boolean", "0"),
        ("duckdb", True, "boolean", "TRUE"),
        ("tsql", _D, "date", "CAST('2026-01-02' AS DATE)"),
        ("oracle", _D, "date", "DATE '2026-01-02'"),
        ("tsql", _TS, "timestamp", "CAST('2026-01-02 03:04:05.000600' AS DATETIME2)"),
        ("oracle", _TS, "timestamp", "TIMESTAMP '2026-01-02 03:04:05.000600'"),
        ("postgres", _TS, "timestamp", "TIMESTAMP '2026-01-02T03:04:05.000600+00:00'"),
        ("postgres", _TS.replace(microsecond=0), "timestamp",
         "TIMESTAMP '2026-01-02T03:04:05+00:00'"),
        ("oracle", "O'Brien", "string", "'O''Brien'"),
        ("snowflake", Decimal("2.50"), "decimal", "2.50"),
        ("mysql", None, "string", "NULL"),
    ],
)  # fmt: skip
def test_inline_dialect_spellings_of_factlang_0_1_4(
    dialect: str, value: object, family: str, literal: str
) -> None:
    got: list[str] = []
    InlineConnector(lambda sql: got.append(sql) or [], dialect).execute(
        _typed_probe(value, family, dialect), timeout_s=None
    )
    assert got == [f"SELECT 1 AS x WHERE a = {literal}"]


def test_literal_recheck_accepts_only_the_forms_of_the_dialect() -> None:
    """Fail closed: each dialect accepts its own spelling of a value's type and nothing else
    (a T-SQL N'..' elsewhere, a 1/0 boolean on ANSI, an ANSI timestamp on Oracle, ...)."""
    from schemaguru.catalog.executor import literal_matches as ok

    assert ok("N'O''Brien'", "O'Brien", "tsql") and not ok("'O''Brien'", "O'Brien", "tsql")
    assert ok("'O''Brien'", "O'Brien", "duckdb") and not ok("N'O''Brien'", "O'Brien", "duckdb")
    assert ok("1", True, "tsql") and ok("0", False, "oracle") and not ok("TRUE", True, "tsql")
    assert ok("TRUE", True, "postgres") and not ok("1", True, "postgres")
    assert ok("1", 1, "tsql") and ok("1", 1, "postgres")  # an integer is a number everywhere
    assert ok("CAST('2026-01-02' AS DATE)", _D, "tsql") and not ok("DATE '2026-01-02'", _D, "tsql")
    assert not ok("CAST('2026-01-02' AS DATE)", _D, "postgres")
    assert ok("TIMESTAMP '2026-01-02 03:04:05'", _TS, "oracle")
    assert not ok("TIMESTAMP '2026-01-02T03:04:05+00:00'", _TS, "oracle")
    assert ok("TIMESTAMP '2026-01-02 03:04:05 UTC'", _TS, "trino")
    assert not ok("TIMESTAMP '2026-01-02T03:04:05+00:00'", _TS, "trino")
    assert not ok("CAST('2026-01-02 03:04:05' AS DATETIME2)", _TS, "trino")
    assert not ok("'2026-01-02'", _D, "duckdb")  # a date must not be plain text
    assert not ok("'ok'", 3, "duckdb") and not ok("3", "3", "duckdb")  # type-keyed
    assert not ok("NULL", "x", "duckdb") and ok("NULL", None, "tsql")
    for bad in (
        "'a\nb'",
        "'a\rb'",
        "'a\x85b'",
        "'a\u2028b'",
        "'a\u2029b'",
        "'a\\'b'",
        "'a'b'",
        "'a\x00b'",
    ):
        assert not ok(bad, "x", "duckdb"), repr(bad)
    assert not ok("1 OR 1=1", 1, "duckdb") and not ok("N'a' + N'b'", "ab", "tsql")
    assert not ok("1", object(), "duckdb")


@pytest.mark.parametrize("terminator", ["\r", "\n", "\r\n", "\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize("dialect", ["duckdb", "tsql", "oracle"])
def test_inline_refuses_line_terminators_with_sg109(terminator: str, dialect: str) -> None:
    from factlang.connectors import ExecutionError

    from schemaguru.catalog.executor import inline_problem

    value = f"a{terminator}b"
    assert inline_problem(value) is not None  # SchemaGuru's own layer, whatever FactLang does
    got: list[str] = []
    conn = InlineConnector(lambda sql: got.append(sql) or [], dialect)
    with pytest.raises(ExecutionError, match=r"^FL700 SG109: .*line terminator") as info:
        conn.execute(_probe("SELECT 1 AS x WHERE name = {{v}}", dialect, v=value), timeout_s=None)
    assert "FL5" not in str(info.value)
    assert got == []


def test_inline_maps_factlang_fl507_to_sg109() -> None:
    """A text value in the date family passes SchemaGuru's checks, FactLang refuses it
    (FL507); the caller sees SG109 with FactLang's reason, not FactLang's code."""
    from factlang.connectors import ExecutionError
    from factlang.sql import RenderError

    probe = _typed_probe("2026-01-02", "date", "duckdb")
    with pytest.raises(RenderError, match=r"^FL507"):
        probe.inline()  # negative control: FactLang's own refusal
    got: list[str] = []
    with pytest.raises(ExecutionError, match=r"^FL700 SG109: a value cannot be inlined") as info:
        InlineConnector(lambda sql: got.append(sql) or [], "duckdb").execute(probe, timeout_s=None)
    assert "date family" in str(info.value) and "FL507" not in str(info.value)
    assert info.value.code == "FL700" and got == []


class NumpyLikeFloat(float):  # numpy.float64 subclasses float and keeps its value
    pass


class NumpyLikeStr(str):  # numpy.str_ subclasses str and keeps its content
    pass


@pytest.mark.parametrize(
    ("value", "family", "literal"),
    [(NumpyLikeFloat(1.25), "float", "1.25"), (NumpyLikeStr("O'Brien"), "string", "'O''Brien'")],
)
def test_numpy_like_subclass_is_normalised_before_factlang(
    value: object, family: str, literal: str
) -> None:
    from factlang.sql import RenderError

    raw = _typed_probe(value, family, "postgres")
    with pytest.raises(RenderError, match="FL507"):
        raw.inline()  # negative control: FactLang 0.1.2 alone refuses the subclass
    seen: list[type] = []
    got: list[str] = []

    def host(sql: str) -> list[Any]:
        got.append(sql)
        return []

    import factlang.sql.probe as fl_probe

    real = fl_probe.inline_template

    def watch(template: str, params: tuple[Any, ...], *rest: Any) -> str:
        seen.extend(type(p.value) for p in params)
        return real(template, params, *rest)

    mp = pytest.MonkeyPatch()
    mp.setattr(fl_probe, "inline_template", watch)
    try:
        InlineConnector(host, "postgres").execute(raw, timeout_s=None)
    finally:
        mp.undo()
    assert got == [f"SELECT 1 AS x WHERE a = {literal}"]
    assert seen and all(t in (float, str) for t in seen)  # FactLang only ever saw base types


@pytest.mark.parametrize("dialect", ["bigquery", "databricks", "spark", "spark2", "hive", "sqlite"])
def test_schemaguru_refuses_a_quote_where_it_cannot_rely_on_doubling(dialect: str) -> None:
    """SchemaGuru's own allow-list, independent of FactLang's lexer lookup: a ' is inlined only
    on dialects whose doubled-quote form SchemaGuru knows."""
    from factlang.connectors import ExecutionError

    from schemaguru.catalog.executor import DOUBLED_QUOTE_DIALECTS

    assert dialect not in DOUBLED_QUOTE_DIALECTS
    with pytest.raises(ExecutionError, match=r"^FL700 SG109: .*doubled-quote form"):
        InlineConnector(lambda sql: [], dialect).execute(
            _probe("SELECT 1 AS x WHERE name = {{v}}", dialect, v="O'Brien"), timeout_s=None
        )


_ALIAS_VALUES = [
    ("O'Brien", "string"),
    (True, "boolean"),
    (False, "boolean"),
    (_D, "date"),
    (_TS, "timestamp"),
]


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("mssql", "tsql"),
        ("sqlserver", "tsql"),
        ("sql_server", "tsql"),
        ("synapse", "tsql"),
        ("fabric", "tsql"),
        ("azuresql", "tsql"),
        ("TSQL", "tsql"),
        (" MSSQL ", "tsql"),
        ("postgresql", "postgres"),
        ("mariadb", "mysql"),
        ("Oracle", "oracle"),
    ],
)
def test_inline_dialect_aliases_spell_exactly_as_the_canonical_dialect(
    alias: str, canonical: str
) -> None:
    """An alias is normalised before inlining and before the re-check lookup: FactLang alone
    would spell ``mssql`` in ANSI forms (TRUE, DATE '..', no N prefix)."""
    from schemaguru.catalog.executor import canonical_dialect

    assert canonical_dialect(alias) == canonical
    conn = InlineConnector(lambda sql: [], alias)
    assert conn.dialect == canonical
    for value, family in _ALIAS_VALUES:
        got: list[str] = []
        want: list[str] = []
        InlineConnector(lambda sql, g=got: g.append(sql) or [], alias).execute(
            _typed_probe(value, family, alias), timeout_s=None
        )
        InlineConnector(lambda sql, w=want: w.append(sql) or [], canonical).execute(
            _typed_probe(value, family, canonical), timeout_s=None
        )
        assert got == want, (alias, value)


def test_inline_mssql_gives_the_tsql_forms() -> None:
    from factlang.sql import probe as fl_probe

    assert fl_probe.style_for("mssql").string_prefix == ""  # negative control: FactLang alone
    got: list[str] = []
    conn = InlineConnector(lambda sql: got.append(sql) or [], "mssql")
    for value, family in _ALIAS_VALUES:
        conn.execute(_typed_probe(value, family, "mssql"), timeout_s=None)
    assert [g.removeprefix("SELECT 1 AS x WHERE a = ") for g in got] == [
        "N'O''Brien'",
        "1",
        "0",
        "CAST('2026-01-02' AS DATE)",
        "CAST('2026-01-02 03:04:05.000600' AS DATETIME2)",
    ]


# --- FactLang 0.1.4 literal spellings --------------------------------------------------------

_UTC = datetime.timezone.utc


def _ts_probe(dialect: str, value: datetime.datetime) -> Any:
    from factlang.ir import Arity
    from factlang.sql import Param, RenderedProbe

    return RenderedProbe(
        "SELECT 1 AS x WHERE t = {{v}}",
        (Param("v", value, "timestamp"),),
        Arity.ROW_SET,
        None,
        dialect,
        "x",
    )


@pytest.mark.parametrize("dialect", ["trino", "Trino "])
@pytest.mark.parametrize(
    ("value", "text"),
    [
        (datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=_UTC), "2026-01-02 03:04:05 UTC"),
        (
            datetime.datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=_UTC),
            "2026-01-02 03:04:05.123456 UTC",
        ),
    ],
)
def test_trino_timestamp_inlined_end_to_end(
    dialect: str, value: datetime.datetime, text: str
) -> None:
    got: list[str] = []
    conn = InlineConnector(lambda sql: got.append(sql) or [], dialect)
    conn.execute(_ts_probe("trino", value), timeout_s=None)
    assert got == [f"SELECT 1 AS x WHERE t = TIMESTAMP '{text}'"]


def test_trino_rejects_the_old_timestamp_form() -> None:
    from schemaguru.catalog.executor import literal_matches

    v = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=_UTC)
    assert not literal_matches("TIMESTAMP '2026-01-02T03:04:05+00:00'", v, "trino")
    assert not literal_matches("TIMESTAMP '2026-01-02 03:04:05'", v, "trino")
    assert literal_matches("TIMESTAMP '2026-01-02 03:04:05 UTC'", v, "trino")


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=_UTC), "2026-01-02 03:04:05 UTC"),
        (
            datetime.datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=_UTC),
            "2026-01-02 03:04:05.123456 UTC",
        ),
    ],
)
def test_databricks_timestamp_inlined_end_to_end(value: datetime.datetime, text: str) -> None:
    got: list[str] = []
    conn = InlineConnector(lambda sql: got.append(sql) or [], "databricks")
    conn.execute(_ts_probe("databricks", value), timeout_s=None)
    assert got == [f"SELECT 1 AS x WHERE t = TIMESTAMP '{text}'"]


def test_databricks_zone_less_timestamp_literal_still_refused_with_the_reason() -> None:
    """A future FactLang that spells it zone-less again fails closed, with the honest reason."""
    from schemaguru.catalog.executor import literal_matches, literal_refusal

    v = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=_UTC)
    lit = "TIMESTAMP '2026-01-02 03:04:05'"
    assert not literal_matches(lit, v, "databricks")
    reason = literal_refusal(lit, v, "databricks")
    assert reason is not None
    assert "timestamp cannot be inlined for databricks" in reason
    assert "session time zone" in reason


def test_inline_connector_docstring_names_the_zone_explicit_forms() -> None:
    doc = InlineConnector.__doc__ or ""
    assert "FactLang 0.1.4" in doc
    assert "YYYY-MM-DD HH:MM:SS UTC" in doc
    assert "Trino" in doc
    assert "Databricks" in doc
    assert "zone-less" in doc


def test_databricks_zone_explicit_timestamp_forms_accepted() -> None:
    from schemaguru.catalog.executor import literal_matches

    v = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=_UTC)
    assert literal_matches("TIMESTAMP '2026-01-02 03:04:05 UTC'", v, "databricks")
    assert literal_matches("TIMESTAMP '2026-01-02T03:04:05.123456+00:00'", v, "databricks")
    assert not literal_matches("TIMESTAMP '2026-01-02 03:04:05'", v, "databricks")


@pytest.mark.parametrize("value", [Decimal("1" + "0" * 38), 2**63, -(2**63) - 1])
def test_inline_refuses_values_factlang_0_1_4_will_not_spell(value: object) -> None:
    from factlang.connectors import ExecutionError
    from factlang.ir import Arity
    from factlang.sql import Param, RenderedProbe

    family = "integer" if isinstance(value, int) else "decimal"
    probe = RenderedProbe(
        "SELECT 1 AS x WHERE c = {{v}}",
        (Param("v", value, family),),
        Arity.ROW_SET,
        None,
        "duckdb",
        "x",
    )
    with pytest.raises(ExecutionError, match="SG109"):
        InlineConnector(lambda sql: [], "duckdb").execute(probe, timeout_s=None)
