"""Lineage: value builders and the human-wins resolution of LINEAGE / COLUMN_LINEAGE facts
(0.2.0 spec 4.1).

Pure and deterministic: :func:`resolve_lineage` depends only on the *set* of input facts, never on
their order. Lineage is declared (dbt), parsed (view definitions) or written by a human (overlay);
it is never measured and never VERIFIED (``Fact.__post_init__`` enforces that).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from schemaguru._util.hashing import canonical_json
from schemaguru.model.facts import Fact, Provenance
from schemaguru.model.joins import subject_key
from schemaguru.model.kinds import LINEAGE_KINDS

Level = Literal["table", "column"]
Transform = Literal["copy", "cast", "expression", "aggregate", "window", "unknown"]
TRANSFORMS: tuple[str, ...] = ("copy", "cast", "expression", "aggregate", "window", "unknown")

DBT_SOURCE = "dbt:manifest"
VIEW_SOURCE = "view_definition"

# (catalog, schema, name) of an input relation, as written in the SQL or manifest.
RawName = tuple[str | None, str | None, str]


@dataclass(frozen=True)
class ResolvedTable:
    """A relation the store holds: its canonical ``ref`` and its columns in ordinal order."""

    ref: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class LineageEdge:
    """One effective edge, as kept in the store's ``lineage_edges`` index."""

    upstream_key: str
    downstream_key: str
    level: Level
    fact_id: str


@dataclass(frozen=True)
class LineageLink:
    """One effective lineage line of a subject: equal parsed facts merged, sources named."""

    subject: str
    level: Level
    inputs: tuple[str, ...]
    transform: str | None
    unresolved: tuple[str, ...]
    sources: tuple[str, ...]
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class LineageResolution:
    """``links`` are effective; ``hidden`` are parsed facts a human replaced or ignored (kept in
    the store, shown by ``explain``); ``unmatched_ignores`` are HUMAN ignore facts that hid
    nothing (an overlay diagnostic)."""

    links: tuple[LineageLink, ...]
    hidden: tuple[Fact, ...]
    unmatched_ignores: tuple[Fact, ...]


def _names(items: Iterable[str], arg: str) -> list[str]:
    if isinstance(items, (str, bytes)):  # a str is an Iterable[str]: one name, not its letters
        raise TypeError(f"{arg} must be a list of names, not a bare {type(items).__name__}")
    out = sorted(set(items))
    if not all(isinstance(x, str) and x for x in out):
        raise ValueError("lineage names must be non-empty strings")
    return out


def table_lineage_value(
    upstream: Iterable[str] | None,
    unresolved: Iterable[str] = (),
    *,
    ignore: bool = False,
) -> dict[str, Any]:
    """A ``LINEAGE`` payload. ``upstream=None`` (no list) is only meaningful for an ignore."""
    if upstream is None and not ignore:
        raise ValueError("table lineage needs an upstream list unless it is an ignore")
    return {
        "upstream": None if upstream is None else _names(upstream, "upstream"),
        "unresolved": _names(unresolved, "unresolved"),
        "ignore": bool(ignore),
    }


def column_lineage_value(
    inputs: Iterable[str] | None,
    transform: str | None,
    unresolved: Iterable[str] = (),
    *,
    ignore: bool = False,
) -> dict[str, Any]:
    """A ``COLUMN_LINEAGE`` payload. ``transform`` is one of :data:`TRANSFORMS` (``None`` only
    for an ignore); ``inputs=None`` (no list) is only meaningful for an ignore."""
    if inputs is None and not ignore:
        raise ValueError("column lineage needs an inputs list unless it is an ignore")
    if not (transform in TRANSFORMS or (ignore and transform is None)):
        raise ValueError(f"transform must be one of {', '.join(TRANSFORMS)}: {transform!r}")
    return {
        "inputs": None if inputs is None else _names(inputs, "inputs"),
        "transform": transform,
        "unresolved": _names(unresolved, "unresolved"),
        "ignore": bool(ignore),
    }


def source_label(source: str | None) -> str:
    """The source as rendered: a dbt fact's ``dbt:manifest:<uid>`` loses its node id."""
    if not source:
        return "unknown"
    if source.startswith(DBT_SOURCE + ":"):
        return DBT_SOURCE
    return source


def _level(f: Fact) -> Level:
    return "table" if f.kind.name == "LINEAGE" else "column"


def _list(f: Fact) -> tuple[str, ...] | None:
    raw = f.value.get("upstream" if _level(f) == "table" else "inputs")
    return None if raw is None else tuple(sorted(raw))


def _shape(f: Fact) -> tuple[tuple[str, ...], str | None, tuple[str, ...]]:
    transform = None if _level(f) == "table" else f.value.get("transform")
    return (_list(f) or (), transform, tuple(sorted(f.value.get("unresolved") or ())))


def _order(f: Fact) -> tuple[str, str]:
    return (f.id, canonical_json(f.to_dict()))


def _merge(subject: str, level: Level, facts: list[Fact]) -> list[LineageLink]:
    groups: dict[tuple[tuple[str, ...], str | None, tuple[str, ...]], list[Fact]] = {}
    for f in facts:
        groups.setdefault(_shape(f), []).append(f)
    return [
        LineageLink(
            subject=subject,
            level=level,
            inputs=inputs,
            transform=transform,
            unresolved=unresolved,
            sources=tuple(sorted({source_label(f.source) for f in fs})),
            fact_ids=tuple(sorted({f.id for f in fs})),
        )
        for (inputs, transform, unresolved), fs in groups.items()
    ]


_PARSED = (Provenance.DECLARED, Provenance.CATALOG)


def resolve_lineage(facts: Iterable[Fact]) -> LineageResolution:
    """Effective lineage, per ``(subject, level)`` (0.2.0 spec 4.1):

    1. HUMAN facts with a list (not ignores) replace every DECLARED/CATALOG fact of that subject
       and level; each distinct human list is its own link.
    2. A HUMAN ignore without a list hides every parsed fact; one with a list hides only parsed
       facts whose sorted list equals it exactly. An ignore that matches no parsed fact is
       reported in ``unmatched_ignores``.
    3. Otherwise every parsed fact is effective; facts with equal ``(inputs, transform,
       unresolved)`` merge into one link naming each source.

    Non-lineage facts are ignored. Table- and column-level facts are resolved independently.
    """
    unique = {_order(f): f for f in facts if f.kind.name in LINEAGE_KINDS}
    groups: dict[tuple[str, Level], list[Fact]] = {}
    for key in sorted(unique):
        f = unique[key]
        groups.setdefault((subject_key(f.subject), _level(f)), []).append(f)

    links: list[LineageLink] = []
    hidden: list[Fact] = []
    unmatched: list[Fact] = []
    for (subject, level), fs in groups.items():
        parsed = [f for f in fs if f.provenance in _PARSED]
        human = [f for f in fs if f.provenance is Provenance.HUMAN]
        ignores = [f for f in human if f.value.get("ignore")]
        replacing = [f for f in human if not f.value.get("ignore")]

        hidden_ids: set[str] = set()
        for ign in ignores:
            want = _list(ign)
            matched = [p for p in parsed if want is None or _list(p) == want]
            if not matched:
                unmatched.append(ign)
            hidden_ids.update(p.id for p in matched)
        if replacing:
            hidden_ids.update(p.id for p in parsed)
            links.extend(_merge(subject, level, replacing))
        else:
            links.extend(_merge(subject, level, [p for p in parsed if p.id not in hidden_ids]))
        hidden.extend(p for p in parsed if p.id in hidden_ids)

    links.sort(
        key=lambda lk: (
            lk.subject, lk.level, lk.inputs, lk.transform or "", lk.unresolved, lk.sources,
            lk.fact_ids,
        )
    )  # fmt: skip
    return LineageResolution(
        links=tuple(links),
        hidden=tuple(sorted(hidden, key=_order)),
        unmatched_ignores=tuple(sorted(unmatched, key=_order)),
    )


def edges_of(resolution: LineageResolution) -> list[LineageEdge]:
    """One edge per (input, fact id) of every effective link, sorted. Unresolved names are
    never edges: they name no relation the store holds."""
    edges = {
        LineageEdge(upstream, link.subject, link.level, fid)
        for link in resolution.links
        for upstream in link.inputs
        for fid in link.fact_ids
    }
    return sorted(edges, key=lambda e: (e.upstream_key, e.downstream_key, e.level, e.fact_id))
