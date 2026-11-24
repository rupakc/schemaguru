"""Graph edges from effective JOIN knowledge (spec 9.1).

One edge per JOIN subject whose numbers come from a MEASURED fact with verdict ``OK`` or
``WEAK``. ``BROKEN`` joins are refused (no edge), not discounted. A join with no usable
measurement enters only when ``GraphConfig.include_unmeasured`` is set, as an *unmeasured*
candidate with no numbers (it pays ``unmeasured_penalty``). NaN/inf numbers are unknown. A
malformed fact costs exactly one edge and reports ``SG601``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from schemaguru.config import GraphConfig
from schemaguru.diagnostics import Diagnostic, DiagnosticBag, Severity
from schemaguru.knowledge.resolve import Effective
from schemaguru.model.facts import Fact, Provenance, Status, method_class
from schemaguru.model.joins import JoinRef, join_verdict
from schemaguru.model.refs import ObjectRef

Verdict = Literal["OK", "WEAK", "BROKEN"]

# Numbers that are rates in [0, 1] and counts that are >= 0; anything else in them is malformed.
_RATES = ("match_rate", "coverage", "row_match_rate")
_COUNTS = (
    "distinct_left",
    "distinct_right",
    "matched",
    "left_non_null",
    "max_left_per_key",
    "max_right_per_key",
)
_NUMBERS = (*_RATES, *_COUNTS, "fan_in")
# A measurement that produced no numbers is not a measurement of the join.
_UNUSABLE = frozenset({Status.ERROR, Status.UNMEASURABLE})
_EMPTY: Mapping[str, Any] = MappingProxyType({})


class MalformedJoin(ValueError):
    """A JOIN fact whose value cannot be read as join numbers."""


@dataclass(frozen=True, eq=False)
class Edge:
    """A directed child -> parent join usable in path search.

    ``value`` holds the measured numbers (NaN/inf already turned into ``None``); it is empty for
    an unmeasured candidate. ``verdict`` is ``None`` exactly when ``measured`` is false.
    ``estimated`` marks numbers that come from a sample (spec 8.6), never compared with exact.
    """

    join: JoinRef
    value: Mapping[str, Any]
    verdict: Verdict | None
    measured: bool = True
    estimated: bool = False
    fact_id: str | None = field(default=None)

    @property
    def key(self) -> str:
        return self.join.identity_key()

    @property
    def left_table(self) -> ObjectRef:
        return self.join.left[0].table_ref()

    @property
    def right_table(self) -> ObjectRef:
        return self.join.right[0].table_ref()

    def __repr__(self) -> str:
        tag = "unmeasured" if not self.measured else (self.verdict or "?")
        return f"Edge({self.key}, {tag}{', estimated' if self.estimated else ''})"


def _number(name: str, raw: object) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise MalformedJoin(f"{name} is not a number: {raw!r}")
    x = float(raw)
    if not math.isfinite(x):
        return None  # NaN/inf: unknown, never a value
    if name in _RATES and not 0.0 <= x <= 1.0:
        raise MalformedJoin(f"{name} {x} is outside [0, 1]")
    if name in _COUNTS and x < 0:
        raise MalformedJoin(f"{name} {x} is negative")
    if name == "fan_in" and x < 0:
        raise MalformedJoin(f"fan_in {x} is negative")
    return x


def clean_value(value: object) -> dict[str, Any]:
    """Validate and normalise a JOIN value; raises :class:`MalformedJoin`."""
    if not isinstance(value, Mapping):
        raise MalformedJoin(f"value is not a mapping: {type(value).__name__}")
    out = dict(value)
    for k in _NUMBERS:
        if k in out:
            x = _number(k, out[k])
            if x is not None and k in _COUNTS:
                out[k] = int(x) if x.is_integer() else x
            else:
                out[k] = x
    return out


def _pick_measurement(eff: Effective) -> Fact | None:
    """The newest exact usable measurement, else the newest sampled one.

    ``Effective.all`` is ordered by provenance and then newest first. ERROR/UNMEASURABLE facts
    never stand in for a good measurement.
    """
    usable = [
        f for f in eff.all if f.provenance is Provenance.MEASURED and f.status not in _UNUSABLE
    ]
    exact = [
        f for f in usable if f.status is not Status.ESTIMATED and method_class(f.method) == "exact"
    ]
    if exact:
        return exact[0]
    return usable[0] if usable else None


def _sg601(bag: DiagnosticBag | None, where: str, why: str) -> None:
    if bag is not None:
        bag.add(
            Diagnostic(
                "SG601",
                Severity.WARNING,
                f"malformed join fact skipped: {why}",
                location=where,
                hint="re-measure the join; only this edge is lost",
            )
        )


def build_edges(
    effective: Mapping[tuple[str, str], Effective],
    *,
    cfg: GraphConfig | None = None,
    bag: DiagnosticBag | None = None,
) -> list[Edge]:
    """Edges for every effective JOIN (spec 9.1), sorted by join identity.

    The verdict is recomputed from ``match_rate`` with the thresholds in ``cfg`` (read at call
    time), so a threshold change needs no re-measure.
    """
    cfg = cfg if cfg is not None else GraphConfig()
    out: list[Edge] = []
    for key in sorted(effective):
        eff = effective[key]
        if eff.kind.name != "JOIN":
            continue
        join = eff.subject
        if not isinstance(join, JoinRef):
            _sg601(bag, key[1], "JOIN subject is not a join reference")
            continue
        if join.left[0].table_ref() == join.right[0].table_ref():
            continue  # a self-reference never moves a route to another table
        fact = _pick_measurement(eff)
        value: dict[str, Any] | None = None
        if fact is not None:
            try:
                value = clean_value(fact.value)
            except MalformedJoin as exc:
                _sg601(bag, join.identity_key(), str(exc))
                continue
        rate = None if value is None else value.get("match_rate")
        if value is not None and fact is not None and rate is not None:
            verdict = join_verdict(rate, cfg.ok_threshold, cfg.weak_threshold)
            if verdict == "BROKEN":
                continue  # refused, not discounted
            estimated = fact.status is Status.ESTIMATED or method_class(fact.method) == "sampled"
            out.append(Edge(join, MappingProxyType(value), verdict, True, estimated, fact.id))
        elif cfg.include_unmeasured:  # declared/drafted/failed: a candidate with no numbers
            out.append(Edge(join, _EMPTY, None, False, False, None))
    return out


def entity_tables(effective: Mapping[tuple[str, str], Effective]) -> set[ObjectRef]:
    """Tables with an effective ``ENTITY_TYPE`` fact (any provenance, drafts included)."""
    return {
        eff.subject.table_ref()
        for eff in effective.values()
        if eff.kind.name == "ENTITY_TYPE"
        and eff.primary is not None
        and isinstance(eff.subject, ObjectRef)
    }
