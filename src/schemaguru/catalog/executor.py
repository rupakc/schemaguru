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


_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")


def redact_url(url: str) -> str:
    """What may be shown of a connection URL in an error, log or repr: fails CLOSED.

    Only ``scheme://host[:port]``, and only when the URL parses cleanly with at most one ``@``
    and that ``@`` inside the authority; anything else is ``scheme://***`` (or ``***`` without
    a scheme). Userinfo, path, query and fragment are never shown.
    """
    scheme, sep, _ = url.partition("://")
    if not sep or not _SCHEME.fullmatch(scheme):
        return "***"
    closed = f"{scheme}://***"
    if url.count("@") > 1:
        return closed
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return closed
    if "@" in url and "@" not in parts.netloc:
        return closed  # the '@' sits in a path, query or fragment: the authority was misread
    if host is None:
        return closed if parts.netloc else f"{scheme}://"
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", host):
        return closed
    shown = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown}" + (f":{port}" if port is not None else "")


_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")


def redact_text(text: str) -> str:
    """``text`` with every embedded ``scheme://...`` token replaced by :func:`redact_url`.

    For messages that may quote a URL (errors, logs, CLI output). A token runs to the next
    whitespace, so a URL is judged whole; text without a URL is unchanged.
    """
    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), text)


_URL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_TRAILING = "'\")>]},.;:"


def scrub_text(text: str, known: Sequence[str | None] = ()) -> str:
    """``text`` with credentials removed from every URL in it and the rest left intact.

    The exact strings in ``known`` (the ``--url`` value) are replaced by their redacted form
    first; then every URL-shaped token is redacted. Never redacts a whole message.
    """
    for url in sorted({u for u in known if u}, key=len, reverse=True):
        text = text.replace(url, redact_url(url))
    parts = re.split(r"(\s+)", text)  # tokens at even indexes, whitespace at odd ones
    out: list[str] = []
    i = 0
    while i < len(parts):
        tok = parts[i]
        m = _URL_TOKEN.search(tok)
        if m is None:
            out.append(tok)
            i += 1
            continue
        body = m.group(0).rstrip(_TRAILING)
        authority = body.partition("://")[2].split("/", 1)[0]
        if "@" not in body and re.search(r":\D", authority):
            # userinfo with a space in the password: the '@' is in a later token (fail closed)
            j = i + 2
            while j < len(parts) and j <= i + 16 and "://" not in parts[j]:
                if "@" in parts[j]:
                    out.append(tok[: m.start()] + body.split("://")[0] + "://***")
                    i = j + 1
                    break
                j += 2
            else:
                out.append(tok[: m.start()] + redact_url(body) + tok[m.start() + len(body) :])
                i += 1
            continue
        out.append(tok[: m.start()] + redact_url(body) + tok[m.start() + len(body) :])
        i += 1
    return "".join(out)


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


def connect(url: str) -> Connection:
    """Open ``url`` (``duckdb:///shop.duckdb``, ``snowflake://...``) for catalog reads.

    FactLang's ``connect`` (it needs SQLAlchemy for most warehouses) is used when installed; its
    connector also serves measurement. Without it, ``duckdb://`` URLs open through the DuckDB
    driver (``duckdb:///path``, ``duckdb:///:memory:``); anything else raises ``SG106``.
    """
    if "://" not in url:
        raise CatalogError("SG106", f"not a connection URL: {redact_url(url)}")
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
            return Connection(
                dialect, FactLangExecutor(connector), connector, shown_url=redact_url(url)
            )
        except Exception as e:
            failure = f"FactLang could not open {redact_url(url)}: {type(e).__name__}"
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
        return Connection("duckdb", DBAPIExecutor(raw, "qmark"), None, raw, redact_url(url))
    raise CatalogError(
        "SG106",
        failure
        or f"cannot open {redact_url(url)}: the installed FactLang has no connect() and there "
        f"is no built-in driver for {scheme!r} (pass a connector or reader to Guru instead)",
    )
