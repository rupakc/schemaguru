"""Scope filters (spec 6.3): glob include/exclude on ``catalog.schema.table`` / ``schema.table``."""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase

from schemaguru.model.refs import ObjectRef

_PROBE = "\x00probe"


def _names(ref: ObjectRef) -> list[str]:
    out: list[str] = []
    if ref.schema is not None:
        out.append(f"{ref.schema}.{ref.table}")
        if ref.catalog is not None:
            out.append(f"{ref.catalog}.{ref.schema}.{ref.table}")
    else:
        out.append(ref.table)
    return out


class ScopeFilter:
    """Case-sensitive ``fnmatch`` globs. Empty ``include`` allows everything; ``exclude`` wins."""

    def __init__(self, include: Sequence[str] = (), exclude: Sequence[str] = ()) -> None:
        self.include = tuple(include)
        self.exclude = tuple(exclude)

    def _matches(self, patterns: Sequence[str], names: Sequence[str]) -> bool:
        return any(fnmatchcase(n, p) for p in patterns for n in names)

    def allows(self, ref: ObjectRef) -> bool:
        names = _names(ref.table_ref())
        if self.include and not self._matches(self.include, names):
            return False
        return not self._matches(self.exclude, names)

    def excludes_schema(self, catalog: str | None, schema: str) -> bool:
        """True when an exclude pattern removes *every* table of the schema (safe to skip it)."""
        return self._matches(self.exclude, _names(ObjectRef(catalog, schema, _PROBE)))
