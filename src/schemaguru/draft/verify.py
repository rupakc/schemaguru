"""Verification of drafts (spec 10.3): every checkable part becomes a FactLang claim.

* ``SOFT_DELETE`` -> ``ROWS t WHERE <p> VALUE >=1`` (the live rows exist) and the partition
  sanity check ``ROWS t WHERE NOT (<p>) VALUE >=1`` (the marker is actually used). Both PASS ->
  ``VERIFIED``; no live rows -> ``CONTRADICTED``; otherwise it stays ``UNVERIFIED``.
* ``TYPE_NOTE`` with a pattern -> ``PATTERN t.c LIKE '<pattern>' RATE >=99%``.
* ``ENTITY_TYPE`` with a key -> ``UNIQUE t.c [WITH [c2, ...]]``.
* ``JOIN`` candidates -> a measured :class:`~schemaguru.measure.KeyOverlapReq` (exact); verdict
  ``OK`` -> ``VERIFIED`` with the measured numbers, ``BROKEN`` -> ``CONTRADICTED``, ``WEAK`` keeps
  ``UNVERIFIED`` (numbers attached).

A drafted soft-delete predicate is LLM text pasted into a claim, so it is checked first as a
filter expression only (:func:`check_predicate`): no comment or statement markers, no FactLang
attribute keywords, balanced quotes/parentheses, and it must reference one of its
``marker_columns`` (each of which must exist when a store is given). A rejected predicate is
never sent; the draft stays ``UNVERIFIED`` with ``SG406``.

What a PASS proves: ``UNIQUE`` proves the claimed *key* is unique and ``PATTERN`` proves the
values match the *pattern*. Neither proves the prose (the entity name, what the type "means"):
those parts are still the model's words, shown as drafted until a human accepts them.

A claim the backend cannot judge (no FactLang claim capability, an empty or raising ``check``, a
failed probe) leaves the draft ``UNVERIFIED`` and reports ``SG405``; nothing crashes. Prose
parts and non-draft facts are returned unchanged. Ids never change: status, method and evidence
are not part of a draft's identity.
"""

from __future__ import annotations

import re
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
from schemaguru.overlays.schema import check_filter_syntax
from schemaguru.store.protocol import Store

PATTERN_RATE = "99%"
# FactLang claim keywords that would end the filter and start an attribute (or a verb)
_CLAIM_WORDS = re.compile(
    r"\b(?:VALUE|RATE|MATCH|COVERAGE|CARD|CAST|TOLERANCE|ALLOW|SPLIT|WITH|TO|WITHIN|BETWEEN|"
    r"MAX_PER_KEY|ROWS|NULLS|DEAD|UNIQUE|FANOUT|VALUES|RANGE|PATTERN|FRESH|JOIN|COUNT|WHERE)\b",
    re.IGNORECASE,
)
_FORBIDDEN = ("--", ";", "#", "/*", "*/", "\n", "\r", "\x00")
_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`")


def check_predicate(
    predicate: object, markers: Sequence[str], columns: set[str] | None = None
) -> str | None:
    """Why a drafted soft-delete predicate may not be put into a claim, or ``None`` if it may.

    A strict local check (the FactLang-seam ``validate_filter`` does not exist yet): the text
    must be one filter expression. ``columns`` (the table's stored column names), when given,
    must contain every marker column.
    """
    if not isinstance(predicate, str) or not predicate.strip():
        return "the predicate is empty"
    for bad in _FORBIDDEN:
        if bad in predicate:
            return f"the predicate contains {bad!r}"
    syntax = check_filter_syntax(predicate)
    if syntax is not None:
        return syntax
    bare = _QUOTED.sub("''", predicate)
    word = _CLAIM_WORDS.search(bare)
    if word is not None:
        return f"the predicate contains the claim keyword {word.group(0)!r}"
    if not markers:
        return "the draft names no marker column"
    if columns is not None:
        missing = [m for m in markers if m not in columns]
        if missing:
            return f"marker column(s) {missing} do not exist"
    named = {m for m in markers if re.search(rf"(?<![\w$]){re.escape(m)}(?![\w$])", bare)}
    if not named:
        return f"the predicate references none of its marker columns {list(markers)}"
    return None


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
        self,
        backend: MeasureBackend,
        cfg: Config,
        bag: DiagnosticBag | None,
        now: datetime,
        store: Store | None,
    ) -> None:
        self.backend, self.cfg, self.bag, self.now = backend, cfg, bag, now
        self.store = store

    def predicate_problem(self, f: Fact) -> str | None:
        columns: set[str] | None = None
        if self.store is not None and isinstance(f.subject, ObjectRef):
            columns = {
                c.ref.column for c in self.store.columns(f.subject.table_ref()) if c.ref.column
            }
        markers = f.value.get("marker_columns") or []
        return check_predicate(f.value.get("predicate"), [str(m) for m in markers], columns)

    def warn(self, f: Fact, why: str, code: str = "SG405") -> None:
        if self.bag is not None:
            loc = (
                f.subject.identity_key()
                if isinstance(f.subject, JoinRef)
                else f.subject.canonical()
            )
            self.bag.add(
                Diagnostic(code, Severity.WARNING, f"{f.kind.name} draft not checked: {why}", loc)
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
    store: Store | None = None,
) -> list[Fact]:
    """Check every checkable ``DRAFTED`` fact; returns ``facts`` in order, updated.

    Pass ``store`` so soft-delete marker columns are checked against the stored columns.
    """
    at = utcnow() if now is None else require_aware(now)
    v = _Verifier(backend, config or Config(), diagnostics, at, store)
    out: list[Fact] = []
    for f in facts:
        if f.provenance is not Provenance.DRAFTED or f.status is Status.ERROR:
            out.append(f)
        elif f.kind.name == "JOIN" and isinstance(f.subject, JoinRef):
            out.append(v.join(f))
        elif f.kind.name == "SOFT_DELETE" and (problem := v.predicate_problem(f)) is not None:
            v.warn(f, f"predicate rejected: {problem}", "SG406")
            out.append(f)
        else:
            texts = claims_for(f)
            out.append(v.claims(f, texts) if texts else f)
    return out
