"""A store-backed FactLang ``Catalog`` (spec 5.2): FactLang never queries INFORMATION_SCHEMA.

Implements FactLang's ``Catalog`` protocol (``table_exists``, ``columns``) structurally, without
importing ``factlang`` (only the seam modules may): a FactLang ``TableRef`` is read through its
``catalog``/``schema``/``table`` ``Ident`` parts (``text``, ``quoted``).

Unknown is ``None``, never "absent": a table the crawl has not seen answers ``None`` (FactLang
then reports that it could not check). A table the crawl saw disappear (withdrawn) is ``False``.
Unquoted parts match case-insensitively when the match is unique; quoted parts match exactly.
"""

from __future__ import annotations

from typing import Any

from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Table
from schemaguru.store.protocol import Store


def _part(ident: Any) -> tuple[str | None, bool]:
    if ident is None:
        return None, False
    return str(ident.text), bool(getattr(ident, "quoted", False))


class StoreCatalog:
    def __init__(self, store: Store) -> None:
        self._store = store

    def _find(self, ref: Any) -> Table | None:
        parts = [_part(ref.catalog), _part(ref.schema), _part(ref.table)]
        catalog, schema, table = (p[0] for p in parts)
        if schema is None or table is None:
            return None  # an unqualified name is ambiguous across schemas: unknown
        wanted = ObjectRef(catalog, schema, table)
        hit = self._store.table(wanted)
        if hit is not None:
            return hit
        found, _ambiguous = self._store.resolve(wanted.canonical())
        if found is None:
            return None
        for (text, quoted), got in zip(
            parts, (found.catalog, found.schema, found.table), strict=True
        ):
            if quoted and text != got:
                return None
        return self._store.table(found)

    def table_exists(self, ref: Any) -> bool | None:
        t = self._find(ref)
        if t is None:
            return None
        return t.withdrawn_at is None

    def columns(self, ref: Any) -> dict[str, str] | None:
        t = self._find(ref)
        if t is None or t.withdrawn_at is not None:
            return None
        cols = self._store.columns(t.ref)
        if not cols:
            return None  # crawled table, columns not (yet) read: unknown
        return {c.ref.column or "": c.native_type for c in cols}

    def family(self, column: ObjectRef) -> str | None:
        """The crawled type family of ``column`` (a SchemaGuru ref), or None when unknown."""
        if column.column is None:
            return None
        for c in self._store.columns(column.table_ref()):
            if c.ref.column == column.column:
                return c.family
        return None
