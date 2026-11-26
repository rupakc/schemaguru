"""Fixed question, join-path and lineage workloads for the scale benchmark."""

from __future__ import annotations

from benchmarks.scale.generator import (
    ATTRIBUTES,
    ENTITIES,
    ScaleShape,
    _mix,
    _period,
    is_view,
    parent_of,
    table_ref,
    view_level,
)
from schemaguru.model.refs import ObjectRef

_TEMPLATES = (
    "How many {a} were created per {b}?",
    "What is the total {x} of {a} by {b}?",
    "Which {a} have no matching {b}?",
    "Show the latest {x} for each of the {a}",
    "Join {a} with {b} and sum the {x}",
)


def _build() -> tuple[str, ...]:
    out: list[str] = []
    for n in range(200):
        a = ENTITIES[n % len(ENTITIES)]
        b = ENTITIES[(n * 5 + 3) % len(ENTITIES)]
        x = ATTRIBUTES[(n * 7 + 1) % len(ATTRIBUTES)]
        out.append(_TEMPLATES[(n // len(ENTITIES) + n) % len(_TEMPLATES)].format(a=a, b=b, x=x))
    return tuple(dict.fromkeys(out)) + tuple(
        f"Which {ENTITIES[n % len(ENTITIES)]} changed in week {n}?" for n in range(200)
    )


QUESTIONS: tuple[str, ...] = _build()[:200]


def path_pairs(shape: ScaleShape, n: int = 200) -> list[tuple[ObjectRef, ObjectRef]]:
    """Pairs ``(table, ancestor)`` two or three foreign-key hops apart, so a join path exists."""
    pairs: list[tuple[ObjectRef, ObjectRef]] = []
    probe = 0
    while len(pairs) < n and probe < n * 50:
        probe += 1
        start = _mix(shape.seed, probe, 11) % shape.tables
        cur: int | None = start
        for hop in range(2 + probe % 2):
            if cur is None:
                break
            cur = parent_of(cur, hop % max(shape.fks_per_table, 1), shape)
        if cur is not None and cur != start and not is_view(start, shape):
            pairs.append((table_ref(start, shape), table_ref(cur, shape)))
    return pairs


def lineage_refs(shape: ScaleShape, n: int = 200) -> list[ObjectRef]:
    """Views at lineage depth 2 or more, spread over the catalog."""
    p = _period(shape)
    views = [i for i in range(p - 1, shape.tables, p) if view_level(i, shape) >= 2]
    if not views:
        return []
    step = max(1, len(views) // n)
    return [table_ref(i, shape) for i in views[::step][:n]]
