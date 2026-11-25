"""Joins (spec 4.5): directed child -> parent."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from schemaguru.model.refs import ObjectRef

JOIN_VALUE_KEYS: tuple[str, ...] = (
    "match_rate",
    "coverage",
    "row_match_rate",
    "distinct_left",
    "distinct_right",
    "matched",
    "left_non_null",
    "fan_in",
    "max_left_per_key",
    "max_right_per_key",
    "cardinality",
    "verdict",
)


@dataclass(frozen=True)
class JoinRef:
    left: tuple[ObjectRef, ...]
    right: tuple[ObjectRef, ...]
    cast_to: str | None = None

    def __post_init__(self) -> None:
        if not self.left or len(self.left) != len(self.right):
            raise ValueError("join sides must be non-empty and of equal length")
        for side in (self.left, self.right):
            if any(c.column is None for c in side):
                raise ValueError("join sides must be column refs")
            if len({c.table_ref() for c in side}) != 1:
                raise ValueError("each join side must be columns of one table")

    def canonical(self) -> str:
        def side(cols: tuple[ObjectRef, ...]) -> str:
            return ", ".join(c.canonical() for c in cols)

        return f"{side(self.left)} -> {side(self.right)}"

    def identity_key(self) -> str:
        """Canonical form plus ``cast_to``: two joins differing only in cast are distinct facts."""
        return (
            self.canonical() if self.cast_to is None else f"{self.canonical()} cast {self.cast_to}"
        )

    def reversed(self) -> JoinRef:
        return JoinRef(self.right, self.left, self.cast_to)

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": [c.to_dict() for c in self.left],
            "right": [c.to_dict() for c in self.right],
            "cast_to": self.cast_to,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> JoinRef:
        return cls(
            tuple(ObjectRef.from_dict(c) for c in d["left"]),
            tuple(ObjectRef.from_dict(c) for c in d["right"]),
            d.get("cast_to"),
        )


def subject_key(subject: ObjectRef | JoinRef) -> str:
    """A fact subject's key: ``JoinRef.identity_key()`` for a join, ``ObjectRef.canonical()``
    otherwise. Part of every fact id; the stores index facts by it."""
    return subject.identity_key() if isinstance(subject, JoinRef) else subject.canonical()


def support(value: Mapping[str, Any], *, reverse: bool) -> float | None:
    """``match_rate`` child -> parent, ``coverage`` parent -> child; unknown is ``None``."""
    v = value.get("coverage" if reverse else "match_rate")
    return None if v is None else float(v)


def join_verdict(
    match_rate: float | None, ok: float, weak: float
) -> Literal["OK", "WEAK", "BROKEN"] | None:
    if match_rate is None:
        return None
    if match_rate >= ok:
        return "OK"
    if match_rate >= weak:
        return "WEAK"
    return "BROKEN"
