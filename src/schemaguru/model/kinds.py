"""Fact kinds: an open registry of :class:`KindSpec` (spec 4.3, 21)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal


@dataclass(frozen=True)
class KindSpec:
    """``identity_fields`` selects which payload keys define a fact's id.

    ``None`` = the whole value. A tuple (possibly empty) = only those keys, so measured numbers
    stay out of identity and re-measuring yields the same fact id.
    """

    name: str
    subject: Literal["table", "column", "join", "global"]
    meaning: bool
    expectation: bool
    identity_fields: tuple[str, ...] | None = None


_REGISTRY: dict[str, KindSpec] = {}


def register_fact_kind(spec: KindSpec, *, replace: bool = False) -> None:
    if spec.name in _REGISTRY and not replace:
        raise ValueError(f"fact kind already registered: {spec.name}")
    _REGISTRY[spec.name] = spec


def get_kind(name: str) -> KindSpec:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown fact kind: {name}") from None


def _k(
    name: str,
    subject: Literal["table", "column", "join", "global"],
    *,
    meaning: bool = False,
    expectation: bool = False,
    identity: tuple[str, ...] | None = None,
) -> KindSpec:
    spec = KindSpec(name, subject, meaning, expectation, identity)
    register_fact_kind(spec)
    return spec


class FactKind:
    """The built-in kinds (spec 4.3 table)."""

    DESCRIPTION: ClassVar[KindSpec] = _k("DESCRIPTION", "table", meaning=True)
    USE_CASES: ClassVar[KindSpec] = _k("USE_CASES", "table", meaning=True)
    ENTITY_TYPE: ClassVar[KindSpec] = _k("ENTITY_TYPE", "table", meaning=True)
    PRIMARY_KEY: ClassVar[KindSpec] = _k(
        "PRIMARY_KEY", "table", expectation=True, identity=("columns",)
    )
    JOIN: ClassVar[KindSpec] = _k("JOIN", "join", expectation=True, identity=())
    VALUES: ClassVar[KindSpec] = _k(
        "VALUES", "column", expectation=True, identity=("case_sensitive", "split")
    )
    NULL_RATE: ClassVar[KindSpec] = _k("NULL_RATE", "column", identity=())
    ROW_COUNT: ClassVar[KindSpec] = _k("ROW_COUNT", "table", identity=())
    DISTINCT: ClassVar[KindSpec] = _k("DISTINCT", "column", identity=())
    FRESHNESS: ClassVar[KindSpec] = _k("FRESHNESS", "table", identity=())
    TYPE_NOTE: ClassVar[KindSpec] = _k("TYPE_NOTE", "column", identity=("stored_as", "means"))
    SOFT_DELETE: ClassVar[KindSpec] = _k(
        "SOFT_DELETE", "table", expectation=True, identity=("predicate",)
    )
    PII: ClassVar[KindSpec] = _k("PII", "column", identity=("category",))
    PITFALL: ClassVar[KindSpec] = _k("PITFALL", "table", meaning=True)
    GLOSSARY: ClassVar[KindSpec] = _k("GLOSSARY", "global", meaning=True)
    METRIC: ClassVar[KindSpec] = _k("METRIC", "global")
    NEVER_QUERY: ClassVar[KindSpec] = _k("NEVER_QUERY", "table")
    WAYPOINT_RULE: ClassVar[KindSpec] = _k("WAYPOINT_RULE", "global")
