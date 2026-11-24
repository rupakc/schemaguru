"""Budget fitting: the richest detail level per table that fits (spec 12.1 step 4)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from schemaguru.knowledge.columns import LITE_MAX_COLUMNS, lite_columns
from schemaguru.model.detail import DetailLevel
from schemaguru.model.refs import ObjectRef
from schemaguru.scope.tokens import TokenCounter

__all__ = ["LITE_MAX_COLUMNS", "DetailLevel", "TableRenderer", "fit", "lite_columns"]


class TableRenderer(Protocol):
    """Renders one table block at a detail level (implemented by the render layer)."""

    def render(self, ref: ObjectRef, level: DetailLevel) -> str: ...


def fit(
    tables_ranked: Sequence[ObjectRef],
    renderer: TableRenderer,
    budget_tokens: int,
    counter: TokenCounter,
) -> list[tuple[ObjectRef, DetailLevel]]:
    """Greedy in rank order: each table gets the richest level that leaves room for the rest.

    Being greedy, the first table may take ``FULL`` and push every later table down to
    ``NAME_ONLY``; rank decides who gets detail.

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
