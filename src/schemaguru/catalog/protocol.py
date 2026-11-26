"""Catalog reader protocol (spec 6.1). Every method is metadata-only; unknown is ``None``."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Protocol, TypeVar, runtime_checkable

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
    view_definitions: bool = False


@runtime_checkable
class BillingReader(Protocol):
    """A reader whose metadata queries are billed (BigQuery). The crawl reads ``bytes_billed``
    (the running total) to enforce ``Budget.crawl_bytes`` and reports
    ``bytes_billed_is_lower_bound`` as ``CrawlReport.bytes_billed_is_estimate``. This protocol
    documents and types the pair; the crawl does not gate on it: a reader exposing only a
    non-negative int ``bytes_billed`` is counted, and a missing flag means exact."""

    bytes_billed: int
    bytes_billed_is_lower_bound: bool


@runtime_checkable
class ViewDefinitionReader(Protocol):
    """Optional: a reader that can return the SQL text of its views.

    Batched like ``columns`` and keyed by the refs ``columns`` would use. Holds views and
    materialized views only; a missing, empty or unfetchable definition is absent (unknown is
    never guessed). The text is raw SQL that may carry PII literals: return it in memory only,
    never log it or put it in a diagnostic or exception message.
    """

    def view_definitions(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, str]: ...


def reads_view_definitions(reader: CatalogReader) -> bool:
    """True when the reader declares the capability and implements ``view_definitions``."""
    return bool(reader.capabilities().view_definitions) and callable(
        getattr(reader, "view_definitions", None)
    )


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
