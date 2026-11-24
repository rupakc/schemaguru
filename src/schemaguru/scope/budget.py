"""Budget fitting: the richest detail level per table that fits (spec 12.1 step 4)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from enum import Enum
from typing import Any, Protocol

from schemaguru.model.facts import Fact
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint
from schemaguru.scope.tokens import TokenCounter

LITE_MAX_COLUMNS = 8


class DetailLevel(Enum):
    """Richest first; iteration order is the degradation order."""

    FULL = "full"
    STANDARD = "standard"
    LITE = "lite"
    NAME_ONLY = "name-only"


class TableRenderer(Protocol):
    """Renders one table block at a detail level (implemented by the render layer)."""

    def render(self, ref: ObjectRef, level: DetailLevel) -> str: ...


def fit(
    tables_ranked: Sequence[ObjectRef],
    renderer: TableRenderer,
    budget_tokens: int,
    counter: TokenCounter,
) -> list[tuple[ObjectRef, DetailLevel]]:
    """Give each table, in rank order, the richest level that still leaves room for the rest.

    Detail degrades before tables go: every remaining table is reserved its ``NAME_ONLY``
    cost first, so a table is dropped (from the bottom of the ranking) only when even the
    names of all kept tables would not fit. The sum of the chosen blocks' token counts never
    exceeds ``budget_tokens`` (separators between blocks are the caller's to budget).
    """
    ranked = list(dict.fromkeys(tables_ranked))
    cache: dict[tuple[ObjectRef, DetailLevel], int] = {}

    def cost(ref: ObjectRef, level: DetailLevel) -> int:
        key = (ref, level)
        if key not in cache:
            cache[key] = counter.count(renderer.render(ref, level))
        return cache[key]

    names = [cost(r, DetailLevel.NAME_ONLY) for r in ranked]
    n = len(ranked)
    while n and sum(names[:n]) > budget_tokens:
        n -= 1
    out: list[tuple[ObjectRef, DetailLevel]] = []
    used = 0
    for i, ref in enumerate(ranked[:n]):
        room = budget_tokens - used - sum(names[i + 1 : n])
        for level in DetailLevel:
            c = names[i] if level is DetailLevel.NAME_ONLY else cost(ref, level)
            if c <= room or level is DetailLevel.NAME_ONLY:
                out.append((ref, level))
                used += c
                break
    return out


# ----- LITE column priority --------------------------------------------------------------


def _strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, Mapping):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def _mentions(text: str, name: str) -> bool:
    pat = r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])"
    return re.search(pat, text, re.IGNORECASE) is not None


def _names(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else []


def lite_columns(
    columns: Sequence[Column],
    constraints: Iterable[Constraint],
    facts: Iterable[Fact],
    *,
    limit: int = LITE_MAX_COLUMNS,
) -> list[Column]:
    """The columns a ``LITE`` block keeps, in priority order (spec 12.1 step 4).

    1. primary key (declared constraint or ``PRIMARY_KEY`` fact);
    2. foreign-key / join columns (FK constraints, either side of a ``JOIN`` fact);
    3. columns referenced by pitfalls or soft-delete (marker columns, or the name appearing
       as a whole word in the predicate / pitfall text, case-insensitively);
    4. columns with descriptions or values (comment, column ``DESCRIPTION``, ``VALUES``);
    5. the rest.
    Within a tier, catalog ordinal order. ``facts`` may be any facts touching the table.
    """
    if limit <= 0 or not columns:
        return []
    table = columns[0].ref.table_ref()
    by_name = {c.ref.column: c for c in columns if c.ref.column is not None}
    tier: dict[str, int] = {}

    def mark(name: str, level: int) -> None:
        if name in by_name and tier.get(name, 99) > level:
            tier[name] = level

    for con in constraints:
        if con.kind == "primary_key":
            for n in con.columns:
                mark(n, 0)
        elif con.kind == "foreign_key":
            for n in con.columns:
                mark(n, 1)
    texts: list[str] = []
    for f in facts:
        kind, subj = f.kind.name, f.subject
        if isinstance(subj, JoinRef):
            if kind == "JOIN":
                for side in (*subj.left, *subj.right):
                    if side.table_ref() == table and side.column is not None:
                        mark(side.column, 1)
            continue
        if subj.table_ref() != table:
            continue
        if kind == "PRIMARY_KEY":
            for n in _names(f.value.get("columns")):
                mark(n, 0)
        elif kind == "SOFT_DELETE":
            for n in _names(f.value.get("marker_columns")):
                mark(n, 2)
            texts.append(str(f.value.get("predicate") or ""))
        elif kind == "PITFALL":
            texts.extend(_strings(f.value))
        elif kind in ("DESCRIPTION", "VALUES") and subj.column is not None:
            mark(subj.column, 3)
    for name in by_name:
        if any(_mentions(t, name) for t in texts):
            mark(name, 2)
    for c in columns:
        if c.comment and c.ref.column is not None:
            mark(c.ref.column, 3)
    ordered = sorted(
        columns, key=lambda c: (tier.get(c.ref.column or "", 4), c.ordinal, c.ref.column or "")
    )
    return ordered[:limit]
