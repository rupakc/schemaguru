"""Verification of drafts (spec 10.3): every checkable part becomes a FactLang claim.

* ``SOFT_DELETE`` -> ``ROWS t WHERE <p> VALUE >=1`` (the live rows exist) and the partition
  sanity check ``ROWS t WHERE NOT (<p>) VALUE >=1`` (the marker is actually used). Both PASS ->
  ``VERIFIED``; no live rows -> ``CONTRADICTED``; otherwise it stays ``UNVERIFIED``.
* ``TYPE_NOTE`` with a pattern -> ``PATTERN t.c LIKE '<pattern>' RATE >=99%``.
* ``ENTITY_TYPE`` with a key -> ``UNIQUE t.c [WITH [c2, ...]]``.
* ``JOIN`` candidates -> a measured :class:`~schemaguru.measure.KeyOverlapReq` (exact); verdict
  ``OK`` -> ``VERIFIED`` with the measured numbers, ``BROKEN`` -> ``CONTRADICTED``, ``WEAK`` keeps
  ``UNVERIFIED`` (numbers attached).

A claim the backend cannot judge (no FactLang claim capability, an empty or raising ``check``, a
failed probe) leaves the draft ``UNVERIFIED`` and reports ``SG405``; nothing crashes. Prose
parts and non-draft facts are returned unchanged. Ids never change: status, method and evidence
are not part of a draft's identity.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from schemaguru._util.timeutil import require_aware, utcnow
from schemaguru.config import Config
from schemaguru.diagnostics import Diagnostic, DiagnosticBag, Severity
from schemaguru.measure.backend import MeasureBackend
from schemaguru.measure.convert import to_facts
from schemaguru.measure.requests import KeyOverlapReq
from schemaguru.model.evidence import Evidence
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef

PATTERN_RATE = "99%"


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def claims_for(f: Fact) -> list[str]:
    """The FactLang claim texts that check draft ``f`` (empty when nothing is checkable)."""
    v, s, kind = f.value, f.subject, f.kind.name
    if not isinstance(s, ObjectRef):
        return []
    t = s.table_ref().canonical()
    if kind == "SOFT_DELETE" and v.get("predicate"):
        p = v["predicate"]
        return [f"ROWS {t} WHERE {p} VALUE >=1", f"ROWS {t} WHERE NOT ({p}) VALUE >=1"]
    if kind == "TYPE_NOTE" and v.get("pattern") and s.column is not None:
        return [f"PATTERN {s.canonical()} LIKE {_lit(v['pattern'])} RATE >={PATTERN_RATE}"]
    if kind == "ENTITY_TYPE" and v.get("key"):
        key = list(v["key"])
        first = ObjectRef(s.catalog, s.schema, s.table, key[0]).canonical()
        rest = f" WITH [{', '.join(ObjectRef(None, None, c).canonical() for c in key[1:])}]"
        return [f"UNIQUE {first}{rest if len(key) > 1 else ''}"]
    return []


class _Verifier:
    def __init__(
        self, backend: MeasureBackend, cfg: Config, bag: DiagnosticBag | None, now: datetime
    ) -> None:
        self.backend, self.cfg, self.bag, self.now = backend, cfg, bag, now

    def warn(self, f: Fact, why: str) -> None:
        if self.bag is not None:
            loc = (
                f.subject.identity_key()
                if isinstance(f.subject, JoinRef)
                else f.subject.canonical()
            )
            self.bag.add(
                Diagnostic(
                    "SG405", Severity.WARNING, f"{f.kind.name} draft not checked: {why}", loc
                )
            )

    def check(self, claim: str) -> Evidence | None:
        try:
            evs = self.backend.check(claim)
        except Exception:  # an older FactLang or a backend without claims: unverifiable
            return None
        return evs[0] if evs else None

    def claims(self, f: Fact, texts: list[str]) -> Fact:
        evs: list[Evidence] = []
        for text in texts:
            ev = self.check(text)
            if ev is None or ev.verdict not in ("PASS", "FAIL"):
                self.warn(f, "the backend returned no verdict (FactLang claim capability missing?)")
                return f
            evs.append(ev)
            if ev.verdict == "FAIL":
                break
        first = evs[0]
        if first.verdict == "FAIL":
            return self._set(f, Status.CONTRADICTED, first)
        if all(e.verdict == "PASS" for e in evs) and len(evs) == len(texts):
            return self._set(f, Status.VERIFIED, first)
        return f  # e.g. soft delete with no deleted rows yet: plausible, not proven

    def join(self, f: Fact) -> Fact:
        assert isinstance(f.subject, JoinRef)
        try:
            [res] = self.backend.measure([KeyOverlapReq(f.subject)])
        except Exception as e:
            self.warn(f, f"measurement failed: {e}")
            return f
        if res.error is not None or res.payload is None:
            self.warn(f, res.error.message if res.error is not None else "no data")
            return f
        [m] = to_facts(res, observed_at=self.now, snapshot_id="", config=self.cfg)
        value: dict[str, Any] = {**f.value, **{k: m.value.get(k) for k in m.value}}
        value["prompt_hash"] = f.value.get("prompt_hash")
        verdict = m.value.get("verdict")
        status = {"OK": Status.VERIFIED, "BROKEN": Status.CONTRADICTED}.get(
            str(verdict), Status.UNVERIFIED
        )
        if m.status is not Status.OBSERVED:
            status = Status.UNVERIFIED
        return replace(
            f, value=value, status=status, method="exact", evidence=m.evidence, observed_at=self.now
        )

    def _set(self, f: Fact, status: Status, ev: Evidence) -> Fact:
        return replace(f, status=status, method="exact", evidence=ev, observed_at=self.now)


def verify_drafts(
    facts: Sequence[Fact],
    backend: MeasureBackend,
    *,
    config: Config | None = None,
    diagnostics: DiagnosticBag | None = None,
    now: datetime | None = None,
) -> list[Fact]:
    """Check every checkable ``DRAFTED`` fact; returns ``facts`` in order, updated."""
    v = _Verifier(
        backend, config or Config(), diagnostics, utcnow() if now is None else require_aware(now)
    )
    out: list[Fact] = []
    for f in facts:
        if f.provenance is not Provenance.DRAFTED or f.status is Status.ERROR:
            out.append(f)
        elif f.kind.name == "JOIN" and isinstance(f.subject, JoinRef):
            out.append(v.join(f))
        else:
            texts = claims_for(f)
            out.append(v.claims(f, texts) if texts else f)
    return out
