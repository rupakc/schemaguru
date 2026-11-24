"""Catalog reader protocol (spec 6.1). Every method is metadata-only; unknown is ``None``."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Protocol, TypeVar

from schemaguru.errors import SchemaGuruError
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table

T = TypeVar("T")


class CatalogError(SchemaGuruError):
    """A catalog-layer failure carrying its diagnostic ``code`` (``SG1xx``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code} {message}")
        self.code = code


@dataclass(frozen=True)
class Page(Generic[T]):
    """One page of results; ``next_token`` is ``None`` on the last page."""

    items: list[T]
    next_token: str | None = None


@dataclass(frozen=True)
class ReaderCapabilities:
    comments: bool = False
    stats: bool = False
    last_altered: bool = False
    constraints: bool = False
    incremental: bool = False
    cost_estimate: bool = False


class CatalogReader(Protocol):
    """Reads structure from a warehouse's system catalogs.

    A *schema reference* is a :class:`~schemaguru.model.structure.Schema` (``ObjectRef`` needs a
    table part); table references are ``ObjectRef``. ``list_tables`` returns every live table in
    the schema when ``changed_since`` is ``None``; otherwise only those altered since then (only
    meaningful when ``capabilities().incremental``).
    """

    dialect: str

    def list_catalogs(self) -> Iterator[str]:
        """May yield nothing (the reader then works in its single default catalog)."""
        ...

    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]: ...

    def list_tables(
        self,
        schema: Schema,
        *,
        page_token: str | None,
        changed_since: datetime | None,
    ) -> Page[Table]: ...

    def columns(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Column]]:
        """Batched; a table without a result is absent or maps to an empty list."""
        ...

    def constraints(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, list[Constraint]]: ...

    def capabilities(self) -> ReaderCapabilities: ...
