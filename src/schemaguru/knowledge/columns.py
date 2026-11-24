"""LITE column priority (spec 12.1 step 4): which columns a short table block keeps.

Shared by scoping (budget fitting) and rendering (the LITE block); both live in layer 6 and may
not import each other, so the rule lives here, once.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from schemaguru.model.facts import Fact
from schemaguru.model.joins import JoinRef
from schemaguru.model.structure import Column, Constraint

LITE_MAX_COLUMNS = 8


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
    Within a tier, catalog ordinal order. Soft-delete columns (markers, or named in the
    predicate) are always kept, past ``limit`` if need be (spec 25.15). ``facts`` may be any
    facts touching the table.
    """
    if not columns:
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
    soft_texts: list[str] = []
    always: set[str] = set()  # soft-delete columns: load-bearing, kept past ``limit``
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
                always.add(n)
            soft_texts.append(str(f.value.get("predicate") or ""))
        elif kind == "PITFALL":
            texts.extend(_strings(f.value))
        elif kind in ("DESCRIPTION", "VALUES") and subj.column is not None:
            mark(subj.column, 3)
    for name in by_name:
        if any(_mentions(t, name) for t in soft_texts):
            always.add(name)
        if any(_mentions(t, name) for t in (*texts, *soft_texts)):
            mark(name, 2)
    for c in columns:
        if c.comment and c.ref.column is not None:
            mark(c.ref.column, 3)
    ordered = sorted(
        columns, key=lambda c: (tier.get(c.ref.column or "", 4), c.ordinal, c.ref.column or "")
    )
    kept = ordered[: max(limit, 0)]
    kept += [c for c in ordered[max(limit, 0) :] if c.ref.column in always]
    return kept
