"""Accepting drafts (spec 10.4): a human turns a ``DRAFTED`` fact into a ``HUMAN`` one.

``accept`` writes ``HUMAN`` facts (``source="accepted:<draft id>"``) to the store; the draft is
kept. ``write_accepted`` copies them into the machine-owned ``accepted.yaml`` overlay and
*returns* the facts whose kind has no overlay form (e.g. ``PII``) instead of dropping them.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from schemaguru._util.timeutil import require_aware, utcnow
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.overlays.writer import append_accepted, facts_to_overlay
from schemaguru.store.protocol import Store

_DRAFT_ONLY_KEYS = ("prompt_hash", "candidate")


def _human(draft: Fact, now: datetime) -> Fact:
    value = {k: v for k, v in draft.value.items() if k not in _DRAFT_ONLY_KEYS}
    verified = draft.status is Status.VERIFIED and draft.evidence is not None
    return Fact.make(
        draft.kind,
        draft.subject,
        value,
        Provenance.HUMAN,
        Status.VERIFIED if verified else Status.UNVERIFIED,
        evidence=draft.evidence if verified else None,
        source=f"accepted:{draft.id}",
        observed_at=draft.observed_at if verified else now,
    )


def accept(store: Store, fact_ids: Iterable[str], *, now: datetime | None = None) -> list[Fact]:
    """Create one ``HUMAN`` fact per drafted id and store them; returns them in id order given.

    Raises ``ValueError`` (a programmer error) for an unknown id, a non-draft fact or a failed
    (``ERROR``) draft; nothing is written in that case.
    """
    at = utcnow() if now is None else require_aware(now)
    ids = list(fact_ids)
    wanted = set(ids)
    found = {f.id: f for f in store.iter_facts() if f.id in wanted}
    out: list[Fact] = []
    for fid in ids:
        f = found.get(fid)
        if f is None:
            raise ValueError(f"no fact with id {fid!r}")
        if f.provenance is not Provenance.DRAFTED:
            raise ValueError(f"{fid} is a {f.provenance.value} fact, not a draft")
        if f.status is Status.ERROR:
            raise ValueError(f"{fid} is a failed draft (ERROR) and cannot be accepted")
        out.append(_human(f, at))
    store.put_facts(out)
    return out


def _has_overlay_form(f: Fact) -> bool:
    return set(facts_to_overlay([f])) != {"version"}


def write_accepted(
    facts: Iterable[Fact], path: str | Path = "schemaguru/accepted.yaml"
) -> list[Fact]:
    """Append ``facts`` to the machine-owned accepted overlay; return those not written."""
    items = list(facts)
    writable = [f for f in items if _has_overlay_form(f)]
    if writable:
        append_accepted(writable, path)
    return [f for f in items if not _has_overlay_form(f)]
