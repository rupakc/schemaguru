"""Effective knowledge: the one place precedence rules live (spec 4.4).

Pure and deterministic: the result depends only on the *set* of input facts, ``now``,
``ttl_days`` and the waivers, never on input order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from schemaguru._util.hashing import canonical_json
from schemaguru._util.timeutil import require_aware
from schemaguru.knowledge.diff import subject_key
from schemaguru.model.facts import Fact, Provenance, Status, Waiver
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import KindSpec
from schemaguru.model.refs import ObjectRef

# Kinds whose truth is structural: CATALOG beats everything (rule 3).
STRUCTURAL_KINDS = frozenset({"TYPE_NOTE", "PRIMARY_KEY"})
_MIN = datetime.min.replace(tzinfo=timezone.utc)

_P = Provenance
_ORDER_MEANING = (_P.HUMAN, _P.DECLARED, _P.CATALOG, _P.MEASURED, _P.DRAFTED)
_ORDER_STRUCTURAL = (_P.CATALOG, _P.HUMAN, _P.DECLARED, _P.MEASURED, _P.DRAFTED)
_ORDER_EXPECTATION = (_P.HUMAN, _P.DECLARED, _P.CATALOG, _P.MEASURED, _P.DRAFTED)
_ORDER_MEASURED = (_P.MEASURED, _P.CATALOG, _P.HUMAN, _P.DECLARED, _P.DRAFTED)


@dataclass(frozen=True)
class Effective:
    """What agents are told for one (kind, subject).

    ``primary`` is the headline fact; ``expectation`` the HUMAN/DECLARED expectation (expectation
    kinds only); ``observed`` the newest MEASURED fact; ``contradictions`` human-readable
    mismatches; ``all`` every retained fact (status-adjusted), in a deterministic order.
    """

    kind: KindSpec
    subject: ObjectRef | JoinRef
    primary: Fact | None
    expectation: Fact | None
    observed: Fact | None
    contradictions: tuple[str, ...]
    all: tuple[Fact, ...]


def _when(f: Fact) -> datetime:
    return f.observed_at if f.observed_at is not None else _MIN


def _newest_first(f: Fact) -> tuple[float, str]:
    return (-_when(f).timestamp(), canonical_json(f.to_dict()))


def _dedupe(facts: Sequence[Fact]) -> list[Fact]:
    """One fact per id: the newest re-measure wins (ties by canonical content)."""
    best: dict[str, Fact] = {}
    for f in sorted(facts, key=_newest_first, reverse=True):
        best[f.id] = f
    return list(best.values())


def _normalise(f: Fact, now: datetime, ttl: timedelta) -> Fact:
    status = f.status
    if f.provenance is Provenance.DRAFTED and status in (
        Status.OBSERVED,
        Status.STALE,
        Status.ESTIMATED,
    ):
        status = Status.UNVERIFIED  # rule 5: a draft is unverified unless its claim VERIFIED
    if (
        status in (Status.OBSERVED, Status.VERIFIED)
        and f.observed_at is not None
        and now - f.observed_at > ttl
    ):
        status = Status.STALE  # unknown age (observed_at None) is never judged stale
    return f if status is f.status else replace(f, status=status)


def _pick(facts: Sequence[Fact], order: tuple[Provenance, ...]) -> Fact | None:
    for prov in order:
        cands = sorted((f for f in facts if f.provenance is prov), key=_newest_first)
        if cands:
            return cands[0]
    return None


def _declared_values(f: Fact) -> list[str] | None:
    v = f.value.get("values")
    return None if not isinstance(v, list) else [str(x) for x in v]


def _contradictions(
    kind: KindSpec, facts: Sequence[Fact], expectation: Fact | None, measured: Sequence[Fact]
) -> list[str]:
    out: list[str] = []
    for f in facts:
        if f.status is Status.CONTRADICTED:
            if f.provenance is Provenance.MEASURED:
                at = "" if f.observed_at is None else f" at {f.observed_at.isoformat()}"
                out.append(f"measured contradiction{at}: {canonical_json(f.value)}")
            else:
                out.append(f"{f.provenance.value} fact failed its check: {canonical_json(f.value)}")
    if kind.name == "VALUES" and expectation is not None:
        declared = _declared_values(expectation)
        obs_fact = next((m for m in measured if isinstance(m.value.get("observed"), list)), None)
        if declared is not None and obs_fact is not None:
            cs = bool(expectation.value.get("case_sensitive", True))

            def norm(s: str) -> str:
                return s if cs else s.casefold()

            known = {norm(d) for d in declared}
            extra: list[tuple[str, int | None]] = []
            for item in obs_fact.value["observed"]:
                if isinstance(item, dict) and norm(str(item.get("value"))) not in known:
                    n = item.get("count")
                    extra.append((str(item.get("value")), n if isinstance(n, int) else None))
            if extra:
                extra.sort(key=lambda t: (-(t[1] or 0), t[0]))
                shown = ", ".join(v if n is None else f"{v} ({n} rows)" for v, n in extra)
                sampled = " [sampled]" if obs_fact.status is Status.ESTIMATED else ""
                decl = ", ".join(sorted(declared))
                out.append(f"declared values {decl} - observed also: {shown}{sampled}")
    if kind.name in STRUCTURAL_KINDS:
        cat = _pick([f for f in facts if f.provenance is Provenance.CATALOG], (_P.CATALOG,))
        if cat is not None:
            field = "stored_as" if kind.name == "TYPE_NOTE" else "columns"
            truth = cat.value.get(field)
            notes = sorted(
                (f for f in facts if f.provenance in (_P.HUMAN, _P.DECLARED)), key=_newest_first
            )
            for n in notes:
                claim = n.value.get(field)
                if claim is None or truth is None:
                    continue
                if _same(claim, truth):
                    continue
                if kind.name == "TYPE_NOTE":
                    out.append(f"note says {claim}; stored as {truth}")
                else:
                    out.append(f"expected primary key {claim}; catalog primary key {truth}")
    return out


def _same(a: object, b: object) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().upper() == b.strip().upper()
    return canonical_json(a) == canonical_json(b)


def resolve(
    facts: Sequence[Fact],
    *,
    now: datetime,
    ttl_days: int,
    waivers: Sequence[Waiver] = (),
) -> dict[tuple[str, str], Effective]:
    """Effective knowledge per ``(kind name, subject key)`` (spec 4.4 rules 1-5, STALE, WAIVED).

    The subject key is ``JoinRef.identity_key()`` for joins and ``ObjectRef.canonical()``
    otherwise. Same-id facts collapse to the newest. Waivers expired at ``now`` are ignored; a
    live waiver turns CONTRADICTED facts of its (kind, subject) into WAIVED and prefixes the
    still-shown contradictions with ``WAIVED until <date> (<reason>): ``.
    """
    require_aware(now)
    ttl = timedelta(days=ttl_days)
    live = sorted((w for w in waivers if w.active(now)), key=lambda w: (w.expires, w.fact_pattern))
    groups: dict[tuple[str, str], list[Fact]] = {}
    for f in _dedupe(facts):
        groups.setdefault((f.kind.name, subject_key(f.subject)), []).append(_normalise(f, now, ttl))

    result: dict[tuple[str, str], Effective] = {}
    for key in sorted(groups):
        fs = groups[key]
        kind, subject = fs[0].kind, fs[0].subject
        measured = sorted((f for f in fs if f.provenance is Provenance.MEASURED), key=_newest_first)
        if kind.name in STRUCTURAL_KINDS:
            order = _ORDER_STRUCTURAL
        elif kind.meaning:
            order = _ORDER_MEANING
        elif kind.expectation:
            order = _ORDER_EXPECTATION
        else:
            order = _ORDER_MEASURED
        exp = _pick([f for f in fs if f.provenance in (_P.HUMAN, _P.DECLARED)], order)
        exp = exp if kind.expectation else None
        msgs = _contradictions(kind, fs, exp, measured)

        waiver = next((w for w in live if w.matches(key[0], key[1])), None)
        if waiver is not None:
            fs = [
                replace(f, status=Status.WAIVED) if f.status is Status.CONTRADICTED else f
                for f in fs
            ]
            msgs = [
                f"WAIVED until {waiver.expires.isoformat()} ({waiver.reason}): {m}" for m in msgs
            ]

        by_id = {f.id: f for f in fs}
        measured = [by_id[m.id] for m in measured]
        primary = _pick(fs, order)
        ordered = sorted(fs, key=lambda f: (order.index(f.provenance), *_newest_first(f)))
        result[key] = Effective(
            kind=kind,
            subject=subject,
            primary=primary,
            expectation=None if exp is None else by_id[exp.id],
            observed=measured[0] if measured else None,
            contradictions=tuple(msgs),
            all=tuple(ordered),
        )
    return result
