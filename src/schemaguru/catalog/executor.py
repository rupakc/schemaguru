"""Metadata SQL executors: the FactLang seam for catalog reads (spec 5.2, 6).

Readers write read-only catalog SQL with ``?`` placeholders and bind every value; an executor
runs it and converts the placeholders to its driver's paramstyle. Only this module imports
``factlang`` in the catalog layer.
"""

from __future__ import annotations

import importlib
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from schemaguru._util.sqltext import Opts
from schemaguru._util.sqltext import scan as _scan
from schemaguru.catalog.protocol import CatalogError

_ALLOWED_FIRST = frozenset({"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC"})
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|COPY|CALL|EXECUTE"
    r"|INTO)\b",
    re.IGNORECASE,
)
_Q = "'\""
_METADATA_VARIANTS = (
    Opts(quotes=_Q, comments=False),
    Opts(quotes=_Q, comments=False, backslash=True),
    Opts(quotes=_Q, comments=False, dollar=True),
    Opts(quotes=_Q, comments=False, triple=True),
)
_PARAMSTYLES = ("qmark", "format", "pyformat", "numeric", "named")


class MetadataExecutor(Protocol):
    def run(
        self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
    ) -> list[dict[str, Any]]: ...


def check_metadata_sql(sql: str) -> None:
    """Refuse anything but one read-only metadata statement (SG104); mirrors FactLang's guard.

    Judged under several quote conventions (backslash, $tag$, triple quotes): a violation under
    any one is a refusal. The shared scanner is conservative; shipped readers' SQL must pass.
    """
    for o in _METADATA_VARIANTS:
        _check_metadata(sql, o)


def _check_metadata(sql: str, o: Opts) -> None:
    chars = _scan(sql, o)
    code = "".join(" " if lit else c for c, lit in chars)
    if ";" in code:
        raise CatalogError("SG104", "metadata SQL must be a single statement without ';'")
    if "--" in code or "/*" in code:
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


class FactLangExecutor:
    """Runs metadata SQL through a FactLang connector (``fl.connectors.run_metadata``)."""

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

    def run(
        self, sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None
    ) -> list[dict[str, Any]]:
        result = self._run(self._connector, sql, tuple(params), timeout_s=timeout_s)
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


_SECRET_KEYS = re.compile(
    r"password|passwd|pwd|token|secret|key|private_key|credentials", re.IGNORECASE
)


def _redact(url: str) -> str:
    """``url`` safe for a message: no userinfo, no secret query values, no fragment.

    The userinfo is everything in the authority up to its LAST ``@`` (a password may hold
    ``/`` or ``@``, which ``urlsplit`` would misread), so it is cut by hand: before the query,
    everything up to the last ``@`` becomes ``***@``. Query values whose key names a secret
    (password, pwd, token, secret, key, private_key, credentials, ...) become ``***``.
    """
    parts = urlsplit(url)
    if not parts.scheme or "://" not in url:
        return "***" if "@" in url else url.split("#", 1)[0]
    rest = url.split("://", 1)[1].split("#", 1)[0]
    head, sep, query = rest.partition("?")
    if "@" in head:
        head = "***@" + head.rsplit("@", 1)[1]
    if sep:
        pairs = []
        for item in query.split("&"):
            k, eq, _v = item.partition("=")
            pairs.append(f"{k}=***" if eq and _SECRET_KEYS.search(k) else item)
        query = "&".join(pairs)
    return f"{parts.scheme}://{head}{sep}{query}"


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

    def close(self) -> None:
        for obj in (self.raw, self.connector):
            close = getattr(obj, "close", None)
            if callable(close):
                close()


def connect(url: str) -> Connection:
    """Open ``url`` (``duckdb:///shop.duckdb``, ``snowflake://...``) for catalog reads.

    FactLang's ``connect`` (it needs SQLAlchemy for most warehouses) is used when installed; its
    connector also serves measurement. Without it, ``duckdb://`` URLs open through the DuckDB
    driver (``duckdb:///path``, ``duckdb:///:memory:``); anything else raises ``SG106``.
    """
    if "://" not in url:
        raise CatalogError("SG106", f"not a connection URL: {_redact(url)!r}")
    scheme = url.split("://", 1)[0].split("+", 1)[0].lower()
    dialect = _SCHEME_DIALECTS.get(scheme, scheme)
    fl_connect = None
    try:
        fl_connect = getattr(importlib.import_module("factlang"), "connect", None)
    except ImportError:  # pragma: no cover - factlang is a core dependency
        fl_connect = None
    failure: str | None = None
    if callable(fl_connect):
        try:
            connector = fl_connect(url)
            return Connection(dialect, FactLangExecutor(connector), connector)
        except Exception as e:
            failure = f"FactLang could not open {_redact(url)}: {type(e).__name__}"
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
                raise CatalogError("SG106", f"no DuckDB database at {_redact(url)}")
            try:
                raw = duckdb.connect(path, read_only=True)
            except Exception as e:
                raise CatalogError(
                    "SG106", f"cannot open {_redact(url)}: {type(e).__name__}"
                ) from None
        return Connection("duckdb", DBAPIExecutor(raw, "qmark"), None, raw)
    raise CatalogError(
        "SG106",
        failure
        or f"cannot open {_redact(url)}: the installed FactLang has no connect() and there is no "
        f"built-in driver for {scheme!r} (pass a connector or reader to Guru instead)",
    )
