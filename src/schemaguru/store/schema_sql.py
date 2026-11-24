"""Numbered SQL migrations shipped with the package (layer 2)."""

from __future__ import annotations

import re
import sqlite3
from importlib import resources

_NAME = re.compile(r"(\d{4})_[a-z0-9_]+\.sql\Z")

FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS table_docs USING fts5("
    "ref UNINDEXED, name_tokens, columns, text, tokenize='porter unicode61')"
)

VERSION_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_version ("
    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
)


def migrations() -> list[tuple[int, str]]:
    """``(version, sql)`` in ascending order; versions must be 1..N without gaps."""
    found: list[tuple[int, str]] = []
    root = resources.files("schemaguru.store").joinpath("migrations")
    for entry in root.iterdir():
        m = _NAME.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.read_text(encoding="utf-8")))
    found.sort()
    if [v for v, _ in found] != list(range(1, len(found) + 1)):
        raise RuntimeError("migrations must be numbered 0001.. without gaps")
    return found


LATEST_VERSION = len(migrations())


def statements(sql: str) -> list[str]:
    """Split a script into complete statements (comments kept with the next statement)."""
    out: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    tail = "\n".join(
        ln for ln in buf.splitlines() if ln.strip() and not ln.strip().startswith("--")
    )
    if tail:
        out.append(buf.strip())
    return out
