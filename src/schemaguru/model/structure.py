"""Structural objects read from the catalog (spec 4.2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, get_args

from schemaguru._util.timeutil import require_aware
from schemaguru.model.refs import ObjectRef

TableKind = Literal["table", "view", "materialized_view", "external", "other"]
ConstraintKind = Literal["primary_key", "unique", "foreign_key"]


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat()


def _dt(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


@dataclass(frozen=True)
class Schema:
    """A schema (dataset / namespace). Its identity is ``(catalog?, name)``.

    ``ObjectRef`` requires a table part, so a schema carries ``catalog`` and ``name`` instead of
    a ``ref``; :meth:`key` is its canonical string.
    """

    catalog: str | None
    name: str
    comment: str | None = None
    table_count: int | None = None

    def __post_init__(self) -> None:
        if not self.name or self.catalog == "":
            raise ValueError("empty name part")

    def key(self) -> str:
        """Canonical string, ``catalog.name`` with each part quoted where needed."""
        parts = [p for p in (self.catalog, self.name) if p is not None]
        return ".".join(ObjectRef(None, None, p).canonical() for p in parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog": self.catalog,
            "name": self.name,
            "comment": self.comment,
            "table_count": self.table_count,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Schema:
        return cls(d.get("catalog"), d["name"], d.get("comment"), d.get("table_count"))


@dataclass(frozen=True)
class Table:
    ref: ObjectRef
    kind: TableKind
    comment: str | None = None
    row_count: int | None = None
    bytes: int | None = None
    created_at: datetime | None = None
    last_altered: datetime | None = None
    partitioning: str | None = None
    clustering: tuple[str, ...] | None = None
    withdrawn_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind not in get_args(TableKind):
            raise ValueError(f"unknown table kind: {self.kind!r}")
        if self.ref.column is not None:
            raise ValueError("a table ref must not carry a column")
        for dt in (self.created_at, self.last_altered, self.withdrawn_at):
            if dt is not None:
                require_aware(dt)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref.to_dict(),
            "kind": self.kind,
            "comment": self.comment,
            "row_count": self.row_count,
            "bytes": self.bytes,
            "created_at": _iso(self.created_at),
            "last_altered": _iso(self.last_altered),
            "partitioning": self.partitioning,
            "clustering": None if self.clustering is None else list(self.clustering),
            "withdrawn_at": _iso(self.withdrawn_at),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Table:
        clustering = d.get("clustering")
        return cls(
            ObjectRef.from_dict(d["ref"]),
            d["kind"],
            d.get("comment"),
            d.get("row_count"),
            d.get("bytes"),
            _dt(d.get("created_at")),
            _dt(d.get("last_altered")),
            d.get("partitioning"),
            None if clustering is None else tuple(clustering),
            _dt(d.get("withdrawn_at")),
        )


@dataclass(frozen=True)
class Column:
    """``nullable`` unknown is ``None`` (never defaulted); ``family`` is a FactLang type family."""

    ref: ObjectRef
    ordinal: int
    native_type: str
    family: str
    nullable: bool | None = None
    comment: str | None = None
    default: str | None = None

    def __post_init__(self) -> None:
        if self.ref.column is None:
            raise ValueError("a column ref must carry a column")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref.to_dict(),
            "ordinal": self.ordinal,
            "native_type": self.native_type,
            "family": self.family,
            "nullable": self.nullable,
            "comment": self.comment,
            "default": self.default,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Column:
        return cls(
            ObjectRef.from_dict(d["ref"]),
            d["ordinal"],
            d["native_type"],
            d["family"],
            d.get("nullable"),
            d.get("comment"),
            d.get("default"),
        )


@dataclass(frozen=True)
class Constraint:
    """Many warehouses declare but do not enforce; ``enforced`` unknown is ``None``."""

    kind: ConstraintKind
    table: ObjectRef
    columns: tuple[str, ...]
    ref_table: ObjectRef | None = None
    ref_columns: tuple[str, ...] | None = None
    enforced: bool | None = None

    def __post_init__(self) -> None:
        if self.kind not in get_args(ConstraintKind):
            raise ValueError(f"unknown constraint kind: {self.kind!r}")
        if not self.columns:
            raise ValueError("a constraint needs at least one column")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "table": self.table.to_dict(),
            "columns": list(self.columns),
            "ref_table": None if self.ref_table is None else self.ref_table.to_dict(),
            "ref_columns": None if self.ref_columns is None else list(self.ref_columns),
            "enforced": self.enforced,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Constraint:
        rt = d.get("ref_table")
        rc = d.get("ref_columns")
        return cls(
            d["kind"],
            ObjectRef.from_dict(d["table"]),
            tuple(d["columns"]),
            None if rt is None else ObjectRef.from_dict(rt),
            None if rc is None else tuple(rc),
            d.get("enforced"),
        )
