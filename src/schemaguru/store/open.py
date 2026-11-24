"""Open a store from what a user passes as ``store=`` / ``Config.store_path`` (layer 2).

A ``postgres://`` or ``postgresql://`` DSN opens :class:`~schemaguru.store.postgres.PostgresStore`
(extra ``postgres-store``); any other URL-shaped string is a ``ConfigError`` (it is never turned
into a SQLite file named after the URL, which would write a password into a file name); anything
else is a SQLite file path. Messages never show a DSN's credentials.
"""

from __future__ import annotations

import os
import re

from schemaguru._util.redact import redact_url
from schemaguru.errors import ConfigError
from schemaguru.store.protocol import Store
from schemaguru.store.sqlite import SQLiteStore

PG_SCHEMES = ("postgres://", "postgresql://")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


def is_postgres_dsn(spec: object) -> bool:
    return isinstance(spec, str) and spec.lower().startswith(PG_SCHEMES)


def open_store(spec: str | os.PathLike[str]) -> Store:
    """The store ``spec`` names: a Postgres DSN, else a SQLite path (URLs are refused)."""
    text = os.fspath(spec)
    if is_postgres_dsn(text):
        try:
            from schemaguru.store.postgres import PostgresStore
        except ImportError:  # pragma: no cover - the module imports psycopg lazily
            raise ConfigError(
                "a Postgres store needs the postgres-store extra: "
                "pip install 'schemaguru[postgres-store]'"
            ) from None
        return PostgresStore(text)
    if _URL.match(text):
        raise ConfigError(
            f"store {redact_url(text)!r} is a URL SchemaGuru cannot open as a store: pass a "
            "file path (SQLite) or a postgres:// / postgresql:// DSN"
        )
    return SQLiteStore(text)


__all__ = ["PG_SCHEMES", "is_postgres_dsn", "open_store"]
