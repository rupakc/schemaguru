"""Helpers shared by the warehouse readers (identifier validation, row normalisation)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from schemaguru.catalog.protocol import CatalogError
from schemaguru.model.refs import ObjectRef

Row = dict[str, Any]


def lower_keys(rows: Sequence[Mapping[str, Any]]) -> list[Row]:
    """Warehouses disagree on the case of result column names (Snowflake and Oracle upper-case
    them); readers read every row through lower-case keys."""
    return [{str(k).lower(): v for k, v in r.items()} for r in rows]


def yes_no(value: Any) -> bool | None:
    """``'YES'/'NO'/'Y'/'N'``/bool -> bool; anything else is unknown (``None``)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().upper()
        if v in ("YES", "Y", "TRUE"):
            return True
        if v in ("NO", "N", "FALSE"):
            return False
    return None


def to_utc(value: Any) -> datetime | None:
    """Aware datetime/ISO string -> UTC. A naive value is a documented-UTC column only for the
    readers that call this with ``naive_is_utc``; elsewhere it is unknown (``None``)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_SAFE_ID = re.compile(r"[A-Za-z0-9_\-$]+\Z")


def safe_ident(name: str, what: str) -> str:
    """Identifiers cannot be bound; only names made of ``[A-Za-z0-9_-$]`` may be interpolated."""
    if not _SAFE_ID.match(name):
        raise CatalogError("SG104", f"unsupported characters in {what} {name!r}")
    return name


def quote_ident(name: str) -> str:
    """Double-quoted identifier (Snowflake/Oracle style); embedded quotes are doubled."""
    if "\x00" in name or not name:
        raise CatalogError("SG104", f"invalid identifier {name!r}")
    return '"' + name.replace('"', '""') + '"'


def pairs(
    tables: Sequence[ObjectRef], schema_col: str, table_col: str, default_schema: str = ""
) -> tuple[str, list[str]]:
    clause = " OR ".join(f"({schema_col} = ? AND {table_col} = ?)" for _ in tables)
    params: list[str] = []
    for t in tables:
        params += [t.schema or default_schema, t.table]
    return clause, params


def group_by_scope(tables: Sequence[ObjectRef]) -> dict[tuple[str | None, str | None], list[str]]:
    """``(catalog, schema) -> [table, ...]`` in first-seen order, de-duplicated."""
    out: dict[tuple[str | None, str | None], list[str]] = {}
    for t in tables:
        names = out.setdefault((t.catalog, t.schema), [])
        if t.table not in names:
            names.append(t.table)
    return out


def pairs3(
    tables: Sequence[ObjectRef], catalog_col: str, schema_col: str, table_col: str
) -> tuple[str, list[str]]:
    """``(catalog = ? AND schema = ? AND table = ?) OR ...``; a ref without a catalog or schema
    matches nothing (empty string) rather than guessing a default."""
    clause = " OR ".join(
        f"({catalog_col} = ? AND {schema_col} = ? AND {table_col} = ?)" for _ in tables
    )
    params: list[str] = []
    for t in tables:
        params += [t.catalog or "", t.schema or "", t.table]
    return f"({clause})", params


def definition_text(value: Any) -> str | None:
    """A view definition as text, or ``None`` when absent, blank or not text (unknown is never
    guessed). The value is raw SQL that may hold PII: callers keep it in memory only."""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value
