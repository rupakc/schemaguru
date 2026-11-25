"""Metadata SQL executors: the FactLang seam for catalog reads (spec 5.2, 6).

Readers write read-only catalog SQL with ``?`` placeholders and bind every value; an executor
runs it and converts the placeholders to its driver's paramstyle. Only this module imports
``factlang`` in the catalog layer.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib
import math
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Protocol

from schemaguru._util.redact import redact_text, redact_url
from schemaguru._util.redact import scrub_text as scrub_text  # re-exported (CLI, server)
from schemaguru._util.sqltext import Opts
from schemaguru._util.sqltext import lex as _lex
from schemaguru._util.sqltext import scan as _scan
from schemaguru.catalog.protocol import CatalogError

_ALLOWED_FIRST = frozenset({"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC"})
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|COPY|CALL|EXECUTE"
    r"|INTO)\b",
    re.IGNORECASE,
)
_Q = "'\""
_QB = _Q + "`"  # MySQL, BigQuery, Spark, ClickHouse read backticks as identifier quotes
_METADATA_VARIANTS = (
    Opts(quotes=_Q, comments=False),
    Opts(quotes=_Q, comments=False, backslash=True),
    Opts(quotes=_Q, comments=False, dollar=True),
    Opts(quotes=_Q, comments=False, triple=True),
    Opts(quotes=_QB, comments=False),
    Opts(quotes=_QB, comments=False, backslash=True),
    Opts(quotes=_QB, comments=False, backslash=True, triple=True),
    Opts(quotes=_Q, comments=False, bracket=True),  # T-SQL [ident]
    Opts(quotes=_Q, comments=False, qquote=True),  # Oracle q'[...]'
)
_PARAMSTYLES = ("qmark", "format", "pyformat", "numeric", "named")


class MetadataExecutor(Protocol):
    def run(
        self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
    ) -> list[dict[str, Any]]: ...


def check_metadata_sql(sql: str) -> None:
    """Refuse anything but one read-only metadata statement (SG104); mirrors FactLang's guard.

    Judged under several quote conventions (backslash escapes, ``$tag$`` strings, triple quotes,
    backtick identifiers, T-SQL ``[ident]``, Oracle ``q'[...]'``): a violation under any one is a
    refusal, so a quote inside one of those forms (```it's```, ``[it's]``, ``q'[x']'``) cannot
    hide a ``;`` or a comment, and a ``;`` inside one (```a;b```) is refused like FactLang's
    guard refuses it. Comment markers (``--``, ``/*``, ``#``, ``//``) are refused outside
    literals, since some dialect reads each as a comment. SQL that leaves a quote open under
    every convention is refused too (no reading of it is a complete statement). The shared
    scanner is conservative; shipped readers' SQL must pass.
    """
    for o in _METADATA_VARIANTS:
        _check_metadata(sql, o)
    if not any(_lex(sql, o).terminated for o in _METADATA_VARIANTS):
        raise CatalogError("SG104", "metadata SQL has a quote that no quoting convention closes")


def _check_metadata(sql: str, o: Opts) -> None:
    chars = _scan(sql, o)
    code = "".join(" " if lit else c for c, lit in chars)
    if ";" in code:
        raise CatalogError("SG104", "metadata SQL must be a single statement without ';'")
    if any(marker in code for marker in ("--", "/*", "#", "//")):  # '#' MySQL, '//' Snowflake
        raise CatalogError("SG104", "comments are not allowed in metadata SQL")
    bad = _FORBIDDEN.search(code)
    if bad:
        raise CatalogError("SG104", f"metadata SQL must be read-only; found {bad.group(0).upper()}")
    words = code.split()
    first = words[0].upper() if words else ""
    if first not in _ALLOWED_FIRST:
        raise CatalogError(
            "SG104", f"metadata SQL must start with one of {sorted(_ALLOWED_FIRST)}, got {first!r}"
        )


def _convert(sql: str, paramstyle: str, nparams: int) -> str:
    if nparams == 0 or paramstyle == "qmark":
        # Without parameters drivers do no interpolation, so ``%`` must stay as written.
        if any(ch == "?" and not lit for ch, lit in _scan(sql)) and nparams == 0:
            raise CatalogError("SG104", "SQL has placeholders but no parameters were given")
        return sql
    out: list[str] = []
    k = 0
    for ch, lit in _scan(sql):
        if not lit and ch == "?":
            k += 1
            out.append(
                {
                    "format": "%s",
                    "pyformat": "%s",
                    "numeric": f":{k}",
                    "named": f":p{k}",
                }[paramstyle]
            )
        elif paramstyle in ("format", "pyformat") and ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    if k != nparams:
        raise CatalogError("SG104", f"SQL has {k} placeholders but {nparams} parameters were given")
    return "".join(out)


class DBAPIExecutor:
    """Runs metadata SQL on a DB-API 2.0 connection (for hosts without FactLang connectors).

    One lock serialises use of the connection, so a crawl's worker threads share it safely.
    ``timeout_s`` is advisory: DB-API has no portable statement timeout.
    """

    def __init__(self, connection: Any, paramstyle: str = "qmark") -> None:
        if paramstyle not in _PARAMSTYLES:
            raise CatalogError("SG104", f"unsupported paramstyle {paramstyle!r}")
        self._conn = connection
        self._style = paramstyle
        self._lock = threading.Lock()

    def run(
        self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
    ) -> list[dict[str, Any]]:
        check_metadata_sql(sql)
        text = _convert(sql, self._style, len(params))
        bound: Any
        if self._style == "named":
            bound = {f"p{i + 1}": v for i, v in enumerate(params)}
        else:
            bound = list(params)
        with self._lock:
            cur = self._conn.cursor()
            try:
                if params:
                    cur.execute(text, bound)
                else:
                    cur.execute(text)
                names = [d[0] for d in cur.description or ()]
                return [dict(zip(names, row, strict=False)) for row in cur.fetchall()]
            finally:
                cur.close()


def _factlang_sql(sql: str, nparams: int) -> tuple[str, list[str]]:
    """``?`` markers (outside literals) -> FactLang's neutral ``{{p<n>}}`` tokens, plus the
    token names in order. Raises ``SG104`` when the counts disagree."""
    out: list[str] = []
    names: list[str] = []
    for ch, lit in _scan(sql):
        if not lit and ch == "?":
            names.append(f"p{len(names) + 1}")
            out.append("{{" + names[-1] + "}}")
        else:
            out.append(ch)
    if len(names) != nparams:
        raise CatalogError(
            "SG104", f"SQL has {len(names)} placeholders but {nparams} parameters were given"
        )
    return "".join(out), names


class FactLangExecutor:
    """Runs metadata SQL through a FactLang connector (``fl.connectors.run_metadata``).

    SchemaGuru's readers mark parameters with ``?``; FactLang's ``run_metadata`` takes neutral
    ``{{name}}`` tokens and a ``{name: value}`` mapping, binding them in the connector's own
    paramstyle. SchemaGuru's guard (SG104) runs before FactLang's (FL703). One lock serialises
    use of the connector, as for :class:`DBAPIExecutor`.
    """

    def __init__(self, connector: Any) -> None:
        try:
            mod = importlib.import_module("factlang.connectors")
            self._run = mod.run_metadata
        except (ImportError, AttributeError):
            raise CatalogError(
                "SG105",
                "the installed FactLang does not provide connectors.run_metadata "
                "(needs the FactLang 0.1 metadata capability)",
            ) from None
        self._connector = connector
        self._lock = threading.Lock()

    def run(
        self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
    ) -> list[dict[str, Any]]:
        check_metadata_sql(sql)
        text, names = _factlang_sql(sql, len(params))
        bound = dict(zip(names, params, strict=True))
        try:
            with self._lock:
                result = self._run(self._connector, text, bound, timeout_s=timeout_s)
        except Exception as e:
            if str(getattr(e, "code", "") or str(e)[:5]).startswith("FL703"):
                raise CatalogError("SG104", f"FactLang refused the metadata SQL: {e}") from None
            raise
        columns = list(getattr(result, "columns", ()))
        rows = getattr(result, "rows", result)
        out: list[dict[str, Any]] = []
        for row in rows:
            pairs = row if isinstance(row, dict) else dict(zip(columns, row, strict=False))
            out.append(dict(pairs))
        return out


# --- opening a warehouse from a URL (spec 19: ``Guru.connect(url)``) ------------------------------

# URL scheme (before any ``+driver``) -> catalog reader dialect
_SCHEME_DIALECTS = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "mssql": "tsql",
    "sqlserver": "tsql",
    "mariadb": "mysql",
}


_redact = redact_text  # message text (the CLI); connect() itself uses redact_url


@dataclass
class Connection:
    """What :func:`connect` opened: a reader dialect, a metadata executor, and the connector.

    ``connector`` is the FactLang connector (``None`` when the built-in fallback opened the URL,
    so measurement reports ``SG304`` until FactLang can connect); ``raw`` is the DB-API
    connection the fallback opened, closed by :meth:`close`.
    """

    dialect: str
    executor: MetadataExecutor
    connector: Any | None = None
    raw: Any | None = None
    # the only form of the URL ever shown (redact_url): never userinfo, path or query
    shown_url: str = ""

    def close(self) -> None:
        for obj in (self.raw, self.connector):
            close = getattr(obj, "close", None)
            if callable(close):
                close()


def _factlang_connect() -> Any | None:
    """FactLang's URL connector factory: ``factlang.connect`` (spec 10) or, where FactLang ships
    it, ``factlang.connectors.connect`` (SQLAlchemy URLs); None when neither is installed."""
    for module in ("factlang", "factlang.connectors"):
        try:
            fn = getattr(importlib.import_module(module), "connect", None)
        except ImportError:  # pragma: no cover - factlang is a core dependency
            fn = None
        if callable(fn):
            return fn
    return None


def _duckdb_connector(raw: Any) -> Any | None:
    """A FactLang ``DBAPIConnector`` over an opened DuckDB connection (measurement and catalog
    reads go through FactLang), or None when the installed FactLang has none."""
    try:
        cls = getattr(importlib.import_module("factlang.connectors"), "DBAPIConnector", None)
    except ImportError:  # pragma: no cover - factlang is a core dependency
        return None
    return cls(raw, "duckdb") if callable(cls) else None


def connect(url: str) -> Connection:
    """Open ``url`` (``duckdb:///shop.duckdb``, ``snowflake://...``) for catalog reads.

    ``duckdb://`` URLs open read-only through the DuckDB driver (a typo must not create a file)
    and are wrapped in FactLang's ``DBAPIConnector`` when installed, so catalog reads
    (``run_metadata``) and measurement go through FactLang. Other URLs open through FactLang's
    ``connect`` (it needs SQLAlchemy for most warehouses); without it they raise ``SG106``.
    """
    if "://" not in url:
        raise CatalogError("SG106", f"not a connection URL: {redact_url(url)}")
    scheme = url.split("://", 1)[0].split("+", 1)[0].lower()
    dialect = _SCHEME_DIALECTS.get(scheme, scheme)
    if scheme == "duckdb":
        try:
            duckdb = importlib.import_module("duckdb")
        except ImportError:
            raise CatalogError(
                "SG106", "duckdb:// needs the DuckDB driver: pip install 'schemaguru[duckdb]'"
            ) from None
        path = url.split("://", 1)[1].split("?", 1)[0]
        path = (path[1:] if path.startswith("/") else path) or ":memory:"
        if path == ":memory:":
            raw = duckdb.connect(path)
        else:  # read-only: SchemaGuru never writes, and a typo must not create a file
            if not os.path.exists(path):
                raise CatalogError("SG106", f"no DuckDB database file at {redact_url(url)}")
            try:
                raw = duckdb.connect(path, read_only=True)
            except Exception as e:
                raise CatalogError(
                    "SG106", f"cannot open {redact_url(url)}: {type(e).__name__}"
                ) from None
        connector = _duckdb_connector(raw)
        executor: MetadataExecutor = DBAPIExecutor(raw, "qmark")
        if connector is not None:
            with contextlib.suppress(CatalogError):  # an older FactLang without run_metadata
                executor = FactLangExecutor(connector)
        return Connection("duckdb", executor, connector, raw, redact_url(url))
    fl_connect = _factlang_connect()
    failure: str | None = None
    if fl_connect is not None:
        try:
            connector = fl_connect(url)
            return Connection(
                dialect, FactLangExecutor(connector), connector, shown_url=redact_url(url)
            )
        except Exception as e:
            failure = f"FactLang could not open {redact_url(url)}: {type(e).__name__}"
    raise CatalogError(
        "SG106",
        failure
        or f"cannot open {redact_url(url)}: the installed FactLang has no connect() and there "
        f"is no built-in driver for {scheme!r} (pass a connector or reader to Guru instead)",
    )


# --- bring your own connection (spec 2 G2): wrap what a host already holds ---------------------

# root module of a DB-API driver -> FactLang/SchemaGuru dialect
_DRIVER_DIALECTS = {
    "duckdb": "duckdb",
    "sqlite3": "sqlite",
    "psycopg": "postgres",
    "psycopg2": "postgres",
    "pg8000": "postgres",
    "pymysql": "mysql",
    "MySQLdb": "mysql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "snowflake": "snowflake",
    "redshift_connector": "redshift",
    "databricks": "databricks",
    "oracledb": "oracle",
    "cx_Oracle": "oracle",
    "trino": "trino",
    "clickhouse_connect": "clickhouse",
}
ACCEPTED_CONNECTORS = (
    "a FactLang connector, a DB-API (PEP 249) connection, a SQLAlchemy engine, a BigQuery "
    "client, or a callable execute(sql) / execute(sql, params) returning rows as mappings"
)


# what a host without bind parameters may receive as a literal (spec 2 G2): plain scalars only
_INLINE_TYPES = (str, int, float, Decimal, datetime.date, type(None))
# CR, LF, NEL, LINE SEPARATOR, PARAGRAPH SEPARATOR: never inside an inlined literal
LINE_TERMINATORS = ("\r", "\n", "\x85", "\u2028", "\u2029")
# characters a non-Unicode client code page (Windows ODBC "best fit") may map to ``'``: curly
# and low-9 quotes, prime, modifier apostrophe, fullwidth apostrophe; plus the ASCII backtick,
# refused fail-safe on its own ground (some dialects read it as an identifier quote)
QUOTE_LOOKALIKES = ("\u2018", "\u2019", "\u201a", "\u201b", "\u2032", "\u02bc", "\uff07", "`")


def inline_problem(value: object) -> str | None:
    """Why ``value`` may not be inlined into SQL for a host with no bind parameters, or None.

    Strings may not hold a backslash or NUL: whether ``\\'`` ends a literal depends on the
    server's mode (MySQL, Snowflake, Postgres with ``standard_conforming_strings=off``), so
    such a value has no spelling that is safe everywhere. Without them, doubling ``'`` is
    unambiguous on every engine that has the doubled form. Nor may they hold a line terminator
    (CR, LF, U+0085, U+2028, U+2029): console tools may end a statement at a line break
    (SQL*Plus at a blank line). FactLang 0.1.4 refuses the same set; SchemaGuru refuses it on
    its own so the behaviour does not depend on the FactLang version. Nor may they hold a quote
    look-alike (U+2018, U+2019, U+201A, U+201B, U+2032, U+02BC, U+FF07): a driver that converts
    the text to a non-Unicode code page may map one to ``'``, which would end the literal on the
    server. Nor a backtick (U+0060), refused fail-safe because some dialects read it as an
    identifier quote. Only plain scalars are allowed, and floats must be finite.
    """
    if not isinstance(value, _INLINE_TYPES):  # bool is an int; datetime is a date
        return f"a {type(value).__name__} value cannot be inlined (scalars only)"
    if isinstance(value, float) and not math.isfinite(value):
        return f"a non-finite number ({value!r}) cannot be inlined"
    if isinstance(value, Decimal) and not value.is_finite():
        return f"a non-finite number ({value}) cannot be inlined"
    if isinstance(value, str) and ("\\" in value or "\x00" in value):
        return "a text value holding a backslash or NUL cannot be inlined safely"
    if isinstance(value, str) and any(ch in value for ch in LINE_TERMINATORS):
        return (
            "a text value holding a line terminator (CR, LF, U+0085, U+2028 or U+2029) cannot "
            "be inlined safely"
        )
    if isinstance(value, str) and "`" in value:
        return (
            "a text value holding a backtick cannot be inlined (refused fail-safe: some dialects "
            "read a backtick as an identifier quote)"
        )
    if isinstance(value, str) and any(ch in value for ch in QUOTE_LOOKALIKES):
        return (
            "a text value holding a quote look-alike (U+2018, U+2019, U+201A, U+201B, U+2032, "
            "U+02BC or U+FF07) cannot be inlined safely: a non-Unicode code page may read it "
            "as '"
        )
    return None


def plain_scalar(value: object) -> object:
    """``value`` as its exact base type, converted by the BASE class's own methods, so a
    subclass's ``__str__``/``__repr__``/``__index__`` overrides are never consulted.
    ``TypeError`` for anything outside the inline whitelist."""
    if value is None or type(value) is bool:
        return value
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, bool):  # pragma: no cover - bool cannot be subclassed
        return bool(int.__index__(value))
    if isinstance(value, int):
        return int.__index__(value)
    if isinstance(value, float):
        return float.__float__(value)
    if isinstance(value, Decimal):
        return Decimal(Decimal.__str__(value))
    if isinstance(value, datetime.datetime):
        try:  # isoformat asks the tzinfo for its offset, which host code may make raise
            text = datetime.datetime.isoformat(value)
        except Exception as e:
            raise TypeError(
                f"a datetime whose time zone cannot give its offset cannot be inlined "
                f"({type(e).__name__})"
            ) from None
        return datetime.datetime.fromisoformat(text)
    if isinstance(value, datetime.date):
        return datetime.date.fromisoformat(datetime.date.isoformat(value))
    raise TypeError(f"a {type(value).__name__} value cannot be inlined (scalars only)")


# the inlined literal forms FactLang 0.1.4 emits, by value type, nothing else. Quoted text holds
# only doubled quotes, never a backslash, NUL or line terminator.
_QUOTED = r"'(?:[^'\\\x00\r\n\x85\u2028\u2029]|'')*'"
_NUMBER = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_DATE = r"\d{4}-\d{2}-\d{2}"
_TIME = r"\d{2}:\d{2}:\d{2}(?:\.\d{6})?"


@dataclass(frozen=True)
class _Forms:
    """One dialect's literal spellings (each a full-match regex)."""

    text: str = _QUOTED
    boolean: str = "TRUE|FALSE"
    date: str = f"DATE '{_DATE}'"
    timestamp: str = rf"TIMESTAMP '{_DATE}T{_TIME}\+00:00'"
    # a form FactLang spells without a time zone: refused with ``zoneless_reason`` (the engine
    # would read it in the session time zone, naming a different instant)
    zoneless: str | None = None
    zoneless_reason: str = ""


_ANSI_FORMS = _Forms()
_DIALECT_FORMS = {
    # N'…' national text, BIT 1/0, ISO casts that ignore DATEFORMAT/LANGUAGE
    "tsql": _Forms(
        text="N" + _QUOTED,
        boolean="1|0",
        date=rf"CAST\('{_DATE}' AS DATE\)",
        timestamp=rf"CAST\('{_DATE} {_TIME}' AS DATETIME2\)",
    ),
    # TRUE is an identifier before 23ai; an ANSI TIMESTAMP takes no 'T' and no offset
    "oracle": _Forms(boolean="1|0", timestamp=f"TIMESTAMP '{_DATE} {_TIME}'"),
    # 0.1.4 spells a UTC instant with an explicit zone name
    "trino": _Forms(timestamp=f"TIMESTAMP '{_DATE} {_TIME} UTC'"),
    # 0.1.4 spells a UTC instant zone-explicitly (``TIMESTAMP '… UTC'``). 0.1.3 rendered it
    # zone-less, which Spark reads in spark.sql.session.timeZone: that form stays refused (with
    # the reason), so a regression in a future FactLang fails closed
    "databricks": _Forms(
        timestamp=rf"TIMESTAMP '{_DATE}(?: {_TIME} UTC|T{_TIME}\+00:00)'",
        zoneless=f"TIMESTAMP '{_DATE} {_TIME}'",
        zoneless_reason=(
            "a timestamp cannot be inlined for databricks: FactLang renders it without a time "
            "zone, which Databricks reads in the session time zone"
        ),
    ),
}


def literal_refusal(lit: str, value: object, dialect: str) -> str | None:
    """A specific reason ``lit`` is refused for ``value`` on ``dialect``, or None when it has
    no more specific reason than "not the exact form"."""
    forms = _DIALECT_FORMS.get(canonical_dialect(dialect), _ANSI_FORMS)
    if (
        forms.zoneless is not None
        and isinstance(value, datetime.datetime)
        and re.fullmatch(forms.zoneless, lit)
    ):
        return forms.zoneless_reason
    return None


def literal_matches(lit: str, value: object, dialect: str) -> bool:
    """Is ``lit`` exactly the literal FactLang 0.1.4 spells for ``value``'s type on
    ``dialect``? Fail closed: an unknown type, or any other spelling, is ``False``."""
    forms = _DIALECT_FORMS.get(canonical_dialect(dialect), _ANSI_FORMS)
    if value is None:
        pattern = "NULL"
    elif isinstance(value, bool):
        pattern = forms.boolean
    elif isinstance(value, (int, float, Decimal)):
        pattern = _NUMBER
    elif isinstance(value, str):
        pattern = forms.text
    elif isinstance(value, datetime.datetime):
        pattern = forms.timestamp
    elif isinstance(value, datetime.date):
        pattern = forms.date
    else:
        return False
    return re.fullmatch(pattern, lit) is not None


def _ansi(text: str) -> str:
    """An ANSI string literal: quotes doubled (no backslash reaches here)."""
    return "'" + text.replace("'", "''") + "'"


def _as_rows(result: Any) -> list[dict[str, Any]]:
    """Rows as mappings, from a list of mappings or of row objects with ``keys()``/``items()``."""
    out: list[dict[str, Any]] = []
    for row in result:
        if isinstance(row, dict):
            out.append(dict(row))
        elif hasattr(row, "items"):
            out.append(dict(row.items()))
        elif hasattr(row, "keys"):
            out.append({k: row[k] for k in row.keys()})  # noqa: SIM118 - not always a dict
        else:
            raise TypeError(f"a row must be a mapping, got {type(row).__name__}")
    return out


# dialects whose lexers read a doubled ``''`` as a quote inside a string literal (FactLang
# 0.1.4's built-in profiles that set ``doubled_quote_escape``, conformance-checked there). On
# every other dialect text holding ``'`` is refused: BigQuery rejects ``''`` (syntax error),
# Databricks/Spark/Hive read adjacent literals as one (``'O''Brien'`` is ``OBrien``, a silently
# wrong value), and a dialect SchemaGuru does not know gets no benefit of the doubt.
# labels hosts use for a dialect -> its canonical (FactLang profile) name. The SQL Server set is
# FactLang's ``TSQL_ALIASES``; ``postgresql``/``mariadb`` are SQLAlchemy's names.
DIALECT_ALIASES = {
    **dict.fromkeys(("mssql", "sqlserver", "sql_server", "synapse", "fabric", "azuresql"), "tsql"),
    "postgresql": "postgres",
    "mariadb": "mysql",
}


def canonical_dialect(dialect: str) -> str:
    """The canonical dialect name for a host's label (case- and whitespace-insensitive): an
    alias becomes its dialect (``mssql`` -> ``tsql``), any other label is lower-cased."""
    name = dialect.strip().lower()
    return DIALECT_ALIASES.get(name, name)


DOUBLED_QUOTE_DIALECTS = frozenset(
    {"clickhouse", "duckdb", "mysql", "oracle", "postgres", "redshift", "snowflake", "trino",
     "tsql"}
)  # fmt: skip


class InlineConnector:
    """A FactLang connector over a host that only runs SQL text: ``fn(sql) -> rows``.

    The host has no driver binding, so values are inlined as literals, on every dialect the same
    way (``SG109`` refuses anything else):

    * only plain scalars (text, integers, booleans, finite numbers, decimals, dates,
      timestamps, NULL), normalised to their exact base type first (a ``numpy.float64`` is
      inlined as a ``float``); a list, NaN or infinity is refused;
    * text holding a backslash, NUL, line terminator (CR, LF, U+0085, U+2028, U+2029) or a
      quote look-alike or backtick (``QUOTE_LOOKALIKES``; a non-Unicode code page may read a
      look-alike as ``'``) is refused, as is a timestamp whose time zone cannot give its
      offset; all other text is quoted with ``'`` doubled, which no server mode can read
      differently -- on ``DOUBLED_QUOTE_DIALECTS`` only: elsewhere (BigQuery, Databricks,
      Spark, Hive, ...) text holding ``'`` is refused;
    * anything FactLang itself refuses to inline (``FL507``; ``FL500``) is reported as
      ``SG109`` too;
    * every literal must be exactly the form FactLang 0.1.4 spells for the value's type on
      this dialect (``N'…'`` text, ``1``/``0`` booleans and ISO casts on T-SQL; ``1``/``0``
      and ``TIMESTAMP 'YYYY-MM-DD HH:MM:SS'`` on Oracle; ``TIMESTAMP 'YYYY-MM-DD HH:MM:SS UTC'``
      on Trino and Databricks/Spark, a zone-less Databricks timestamp being refused with its
      reason; ANSI elsewhere), else ``SG109``;
    * the final SQL must pass the read-only guard (``SG104``) right before the host runs it.

    Rows are mappings (or row objects with ``keys``/``items``). Only FactLang's public API is
    used: ``fl.connectors.ExecutionError``/``QueryResult``, ``fl.sql.RenderError`` and the
    rendered probe's own ``inline()``.
    """

    paramstyle = "qmark"  # nominal: every value is inlined

    def __init__(self, fn: Any, dialect: str) -> None:
        self.fn = fn
        # canonical before any inlining or re-check: FactLang spells an alias it does not know
        # (``mssql``) in ANSI forms, not T-SQL's
        self.dialect = canonical_dialect(dialect)

    def execute(self, probe: Any, *, timeout_s: float | None) -> Any:
        conns = importlib.import_module("factlang.connectors")
        render_error = importlib.import_module("factlang.sql").RenderError

        def refuse(why: str) -> Any:
            return conns.ExecutionError(
                f"SG109: {why}; this host takes SQL text only (pass a DB-API connection or "
                "an engine for bound parameters)",
                code="FL700",
            )

        dialect = self.dialect
        no_doubling = dialect not in DOUBLED_QUOTE_DIALECTS
        params = []
        for p in probe.params:
            try:  # the exact base type, read through the base class (a subclass cannot lie)
                value = plain_scalar(p.value)
            except TypeError as e:
                raise refuse(str(e)) from None
            why = inline_problem(value)
            if why is None and no_doubling and isinstance(value, str) and "'" in value:
                why = (
                    f"a text value holding ' cannot be inlined for {self.dialect}: its string "
                    "literals have no doubled-quote form SchemaGuru can rely on"
                )
            if why is not None:
                raise refuse(why)
            params.append(replace(p, value=value))

        def inline(template: str, ps: tuple[Any, ...]) -> str:
            # spelled for the host's dialect, which is what runs the text
            target = replace(probe, sql_template=template, params=ps, dialect=dialect)
            try:
                return str(target.inline(_ansi))
            except render_error as e:  # FL507 (a value it will not inline) or FL500
                detail = re.sub(r"^FL\d{3}\s*", "", str(e))
                raise refuse(f"a value cannot be inlined ({detail})") from None

        for p in params:  # belt and braces: every rendered literal is exactly its type's form
            lit = inline("{{" + p.name + "}}", (p,))
            if not literal_matches(lit, p.value, dialect):
                specific = literal_refusal(lit, p.value, dialect)
                if specific is not None:
                    raise refuse(specific)
                raise refuse(f"parameter {p.name!r} did not render as a single safe literal")
        sql = inline(probe.sql_template, tuple(params))
        try:
            check_metadata_sql(sql)  # read-only, one statement, no comment, on the final text
        except CatalogError as e:
            raise conns.ExecutionError(f"SG104: inlined SQL refused: {e}", code="FL703") from None
        try:
            rows = _as_rows(self.fn(sql))
        except conns.ExecutionError:
            raise
        except Exception as exc:
            raise _execution_error(conns, exc, timeout_s) from exc
        if getattr(probe, "plan", None) is not None:
            # FactLang's result labels are a lower-case wire format; some warehouses upper-case
            # unquoted labels (host metadata SQL, plan None, keeps the warehouse's labels)
            rows = [
                {(k.lower() if isinstance(k, str) else k): v for k, v in r.items()} for r in rows
            ]
        return conns.QueryResult(rows)


_TIMEOUT_TEXT = re.compile(r"(?i)\b(timed? ?out|timeout|cancell?ed|statement canceled)\b")


def _execution_error(conns: Any, exc: Exception, timeout_s: float | None) -> Any:
    """FactLang's ``ExecutionError`` for a host's exception: ``FL702`` for a query timeout,
    else ``FL700`` (FactLang's own mapping is internal, so this is SchemaGuru's)."""
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, TimeoutError) or _TIMEOUT_TEXT.search(str(exc)):
        limit = f" after {timeout_s:g}s" if timeout_s is not None else ""
        return conns.ExecutionError(f"query timed out{limit} ({detail})", code="FL702")
    return conns.ExecutionError(f"query failed ({detail})", code="FL700")


def _query_client(client: Any) -> Any:
    """``fn(sql) -> rows`` over a client exposing ``query(sql).result()`` (BigQuery's shape)."""

    def run(sql: str) -> list[dict[str, Any]]:
        return _as_rows(client.query(sql).result())

    return run


def _positional_arity(fn: Any) -> int | None:
    import inspect

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    required = [
        p
        for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
    ]
    return len(required)


def as_connector(obj: Any, dialect: str | None = None) -> Any:
    """A FactLang connector for what the host holds (spec 2 G2), or ``ConfigError`` ``SG107``.

    Accepted: a FactLang connector (returned as is); a SQLAlchemy engine (FactLang's
    ``SQLAlchemyConnector``); a BigQuery client (FactLang's ``BigQueryConnector`` when
    ``google-cloud-bigquery`` is installed, else any object with ``query(sql).result()``,
    inlined); a DB-API connection (FactLang's ``DBAPIConnector``, dialect from the driver
    module unless given); a callable ``execute(sql, params)`` (FactLang's ``CallableConnector``)
    or ``execute(sql)`` (values inlined). A callable or an unknown driver needs ``dialect``.
    """
    from schemaguru.errors import ConfigError

    conns = importlib.import_module("factlang.connectors")
    if isinstance(getattr(obj, "dialect", None), str) and callable(getattr(obj, "execute", None)):
        return obj  # already a FactLang connector
    module = type(obj).__module__ or ""
    root = module.split(".")[0].lstrip("_")
    if root == "sqlalchemy" or (
        hasattr(getattr(obj, "dialect", None), "name") and callable(getattr(obj, "connect", None))
    ):
        return conns.SQLAlchemyConnector(obj)
    if module.startswith("google.cloud.bigquery"):
        return conns.BigQueryConnector(obj)
    if callable(getattr(obj, "query", None)) and not callable(getattr(obj, "cursor", None)):
        return InlineConnector(_query_client(obj), dialect or "bigquery")
    if callable(getattr(obj, "cursor", None)):
        name = dialect or _DRIVER_DIALECTS.get(root) or _DRIVER_DIALECTS.get(module.split(".")[0])
        if name is None:
            raise ConfigError(
                f"SG107: cannot tell the dialect of a {module}.{type(obj).__name__} connection; "
                "pass dialect=..."
            )
        return conns.DBAPIConnector(obj, name)
    if callable(obj):
        if dialect is None:
            raise ConfigError("SG107: a callable connector needs dialect=... (the SQL it runs)")
        if _positional_arity(obj) == 1:
            return InlineConnector(obj, dialect)
        return conns.CallableConnector(obj, dialect)
    raise ConfigError(
        f"SG107: cannot use a {module}.{type(obj).__name__} as a connector; expected "
        f"{ACCEPTED_CONNECTORS}"
    )
