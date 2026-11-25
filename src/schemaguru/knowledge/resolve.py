"""Effective knowledge: the one place precedence rules live (spec 4.4).

Pure and deterministic: the result depends only on the *set* of input facts, ``now``,
``ttl_days`` and the waivers, never on input order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from schemaguru._util.hashing import canonical_json
from schemaguru._util.timeutil import require_aware
from schemaguru.knowledge.diff import _by_id, _newest_first, _when
from schemaguru.model.facts import Fact, Provenance, Status, Waiver
from schemaguru.model.joins import JoinRef, subject_key
from schemaguru.model.kinds import KindSpec
from schemaguru.model.refs import ObjectRef
from schemaguru.pii import mask_value

# Kinds whose truth is structural: CATALOG beats everything (rule 3: existence and type only).
STRUCTURAL_KINDS = frozenset({"TYPE_NOTE"})
# Kinds whose HUMAN/DECLARED claim is compared with the catalog's fact (contradiction shown).
CATALOG_CHECKED_KINDS = frozenset({"TYPE_NOTE", "PRIMARY_KEY"})
# Non-meaning kinds that still order like meaning kinds (a human classification wins).
MEANING_ORDER_KINDS = frozenset({"PII"})

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


def _dedupe(facts: Sequence[Fact]) -> list[Fact]:
    """One fact per id: the newest re-measure wins (ties by canonical content; shared with
    ``diff``)."""
    return list(_by_id(facts).values())


def _normalise(f: Fact, now: datetime, ttl: timedelta) -> Fact:
    status = f.status
    if f.provenance is Provenance.DRAFTED and status in (
        Status.OBSERVED,
        Status.STALE,
        Status.ESTIMATED,
    ):
        status = Status.UNVERIFIED  # rule 5: a draft is unverified unless its claim VERIFIED
    if (
        status in (Status.OBSERVED, Status.VERIFIED, Status.ESTIMATED)
        and f.observed_at is not None
        and now - f.observed_at > ttl
    ):
        # an old estimate ages like any measurement; its sampled method still marks it as one.
        # Unknown age (observed_at None) is never judged stale (base spec 4.4).
        status = Status.STALE
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


def _is_sample(f: Fact) -> bool:
    return f.status is Status.ESTIMATED or (f.method or "").startswith("sampled:")


def _supersession(measured: Sequence[Fact]) -> tuple[list[Fact], list[Fact]]:
    """Split newest-first measurements into (warning, superseded).

    The pivot is the newest NON-sampled measurement: a sample can refute but never confirm
    (spec 8.6), so only an exact measurement supersedes older ones. Samples newer than the pivot
    keep warning alongside it. With no exact measurement nothing is superseded.
    """
    ok = [m for m in measured if m.status is not Status.ERROR]
    pivot = next((i for i, m in enumerate(ok) if not _is_sample(m)), None)
    if pivot is None:
        return list(measured), []
    newest = _when(ok[pivot])
    errors = [m for m in measured if m.status is Status.ERROR]
    # a failed probe never pivots or supersedes; one older than the pivot no longer warns
    warn = [*ok[: pivot + 1], *(e for e in errors if _when(e) > newest)]
    gone = [*ok[pivot + 1 :], *(e for e in errors if _when(e) <= newest)]
    return sorted(warn, key=_newest_first), sorted(gone, key=_newest_first)


def _identity_key(f: Fact) -> str:
    """The payload part of a fact's identity (``KindSpec.identity_fields``; whole value if None)."""
    names = f.kind.identity_fields
    ident = dict(f.value) if names is None else {n: f.value.get(n) for n in names}
    return canonical_json(ident)


def _supersession_by_identity(measured: Sequence[Fact]) -> tuple[list[Fact], list[Fact]]:
    """``_supersession`` applied per identity: a measurement of another identity (a UNIQUE check
    on candidate key ``email`` vs the declared key ``order_id``) never supersedes this one."""
    by_ident: dict[str, list[Fact]] = {}
    for m in measured:
        by_ident.setdefault(_identity_key(m), []).append(m)
    warn: list[Fact] = []
    gone: list[Fact] = []
    for ms in by_ident.values():
        w, g = _supersession(ms)
        warn += w
        gone += g
    return sorted(warn, key=_newest_first), sorted(gone, key=_newest_first)


def _contradictions(
    kind: KindSpec, facts: Sequence[Fact], expectations: Sequence[Fact], measured: Sequence[Fact]
) -> list[str]:
    out: list[str] = []
    warn_ids = {m.id for m in measured}
    for m in measured:
        if m.status is Status.ERROR:
            at = "" if m.observed_at is None else f" at {m.observed_at.isoformat()}"
            reason = m.value.get("reason") or "probe_error"
            out.append(f"latest measurement failed{at}: {reason}")
    for f in facts:
        if f.status is Status.CONTRADICTED:
            if f.provenance is Provenance.MEASURED:
                if f.id not in warn_ids:
                    continue  # a superseded measurement no longer warns
                at = "" if f.observed_at is None else f" at {f.observed_at.isoformat()}"
                tag = " [sampled]" if _is_sample(f) else ""
                out.append(f"measured contradiction{at}{tag}: {canonical_json(f.value)}")
            else:
                out.append(f"{f.provenance.value} fact failed its check: {canonical_json(f.value)}")
    for expectation in expectations if kind.name == "VALUES" else ():
        declared = _declared_values(expectation)
        obs_facts = [m for m in measured if isinstance(m.value.get("observed"), list)]
        if declared is not None and not obs_facts:
            if any(m.value.get("value_policy") == "none" for m in measured):
                out.append("declared values not compared (value policy: none)")
            continue
        if declared is not None and obs_facts:
            # observed values stored masked: compare and render both sides as shape tokens, so a
            # PII-shaped declared list neither false-contradicts nor echoes its raw values
            masked = any(of.value.get("value_policy") != "raw" for of in obs_facts)
            norm = _masked if masked else str
            declared = sorted({norm(d) for d in declared})
            cs = bool(expectation.value.get("case_sensitive", True))
            known = {_fold(d, cs) for d in declared}
            # value -> (count, seen by an exact measurement); union over pivot and newer samples
            extra: dict[str, tuple[int | None, bool]] = {}
            for of in obs_facts:
                for item in of.value["observed"]:
                    if not isinstance(item, dict):
                        continue
                    v = norm(str(item.get("value")))
                    if _fold(v, cs) in known:
                        continue
                    n = item.get("count")
                    n = n if isinstance(n, int) else None
                    old = extra.get(v)
                    exact = (old[1] if old else False) or not _is_sample(of)
                    cnt = max(
                        (c for c in (old[0] if old else None, n) if c is not None), default=None
                    )
                    extra[v] = (cnt, exact)
            if extra:
                rows = sorted(extra.items(), key=lambda t: (-(t[1][0] or 0), t[0]))
                shown = ", ".join(
                    (v if n is None else f"{v} ({n} rows)") + ("" if ex else " [sampled]")
                    for v, (n, ex) in rows
                )
                decl = ", ".join(sorted(declared))
                out.append(f"declared values {decl} - observed also: {shown}")
    if kind.name in CATALOG_CHECKED_KINDS:
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
    return list(dict.fromkeys(out))


def _masked(v: str) -> str:
    """A value as it is stored under the masked policy (its shape token, or itself)."""
    text, cat = mask_value(v)
    return text if cat is not None else v


def _fold(s: str, case_sensitive: bool) -> str:
    return s if case_sensitive else s.casefold()


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
        measured_warn, superseded = _supersession_by_identity(measured)
        if superseded:  # kept in `all`, but no longer warning
            older = {m.id for m in superseded}
            fs = [
                replace(f, status=Status.STALE)
                if f.id in older
                and f.status
                in (Status.OBSERVED, Status.VERIFIED, Status.CONTRADICTED, Status.ESTIMATED)
                else f
                for f in fs
            ]
        if kind.name in STRUCTURAL_KINDS:
            order = _ORDER_STRUCTURAL
        elif kind.meaning or kind.name in MEANING_ORDER_KINDS:
            order = _ORDER_MEANING
        elif kind.expectation:
            order = _ORDER_EXPECTATION
        else:
            order = _ORDER_MEASURED
        exps = (
            [
                e
                for e in (
                    _pick([f for f in fs if f.provenance is prov], (prov,))
                    for prov in (_P.HUMAN, _P.DECLARED)
                )
                if e is not None
            ]
            if kind.expectation
            else []
        )
        exp = exps[0] if exps else None  # HUMAN-first pick; every expectation is still checked
        msgs = _contradictions(kind, fs, exps, measured_warn)

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
        # a failed probe is a warning only: never the headline while anything else is known
        measured = [m for m in measured if m.status is not Status.ERROR] or measured
        primary = _pick([f for f in fs if f.status is not Status.ERROR], order) or _pick(fs, order)
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
