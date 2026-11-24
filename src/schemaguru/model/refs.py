"""Object identity (spec 4.1). Names are stored exactly as the catalog returns them."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_BARE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")


def _quote(part: str) -> str:
    return part if _BARE.match(part) else '"' + part.replace('"', '""') + '"'


def _split(text: str) -> list[str]:
    """Split on dots outside double quotes; ``""`` inside quotes is an escaped quote."""
    parts: list[str] = []
    i, n = 0, len(text)
    while True:
        if i < n and text[i] == '"':
            i += 1
            buf: list[str] = []
            while True:
                if i >= n:
                    raise ValueError(f"unterminated quote in {text!r}")
                if text[i] == '"':
                    if i + 1 < n and text[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(text[i])
                i += 1
            part = "".join(buf)
        else:
            j = i
            while j < n and text[j] != ".":
                if text[j] == '"':
                    raise ValueError(f"stray quote in {text!r}")
                j += 1
            part = text[i:j]
            i = j
        if not part:
            raise ValueError(f"empty name part in {text!r}")
        parts.append(part)
        if i >= n:
            return parts
        if text[i] != ".":
            raise ValueError(f"expected '.' at position {i} in {text!r}")
        i += 1


@dataclass(frozen=True)
class ObjectRef:
    catalog: str | None
    schema: str | None
    table: str
    column: str | None = None

    def __post_init__(self) -> None:
        if self.catalog is not None and self.schema is None:
            raise ValueError("a catalog requires a schema")
        for part in (self.catalog, self.schema, self.table, self.column):
            if part is not None and part == "":
                raise ValueError("empty name part")

    def table_ref(self) -> ObjectRef:
        return ObjectRef(self.catalog, self.schema, self.table)

    def canonical(self) -> str:
        parts = (self.catalog, self.schema, self.table, self.column)
        return ".".join(_quote(p) for p in parts if p is not None)

    @classmethod
    def parse(cls, text: str, *, has_column: bool | None = None) -> ObjectRef:
        """Inverse of :meth:`canonical`.

        A bare ``schema.table.column`` is ambiguous with ``catalog.schema.table``; so four parts
        always mean a column, and with fewer parts pass ``has_column=True`` to read the last
        part as the column (default: a table reference).
        """
        parts = _split(text)
        if not 1 <= len(parts) <= 4:
            raise ValueError(f"expected 1-4 name parts, got {len(parts)} in {text!r}")
        if has_column is None:
            has_column = len(parts) == 4
        column = parts.pop() if has_column else None
        if not parts:
            raise ValueError(f"no table part in {text!r}")
        if len(parts) == 3:
            return cls(parts[0], parts[1], parts[2], column)
        if len(parts) == 2:
            return cls(None, parts[0], parts[1], column)
        return cls(None, None, parts[0], column)

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog": self.catalog,
            "schema": self.schema,
            "table": self.table,
            "column": self.column,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ObjectRef:
        return cls(d.get("catalog"), d.get("schema"), d["table"], d.get("column"))


@dataclass(frozen=True)
class WarehouseId:
    name: str
    dialect: str
