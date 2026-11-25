"""Diff two fact sets by identity and content, never by timestamps (spec 15)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from schemaguru._util.hashing import canonical_json
from schemaguru.model.facts import Fact, Provenance
from schemaguru.model.joins import JoinRef
from schemaguru.model.joins import subject_key as subject_key  # 0.1.0 import path
from schemaguru.model.kinds import KindSpec
from schemaguru.model.refs import ObjectRef

ChangeKind = Literal["added", "removed", "value", "status"]
_RANK: dict[str, int] = {"added": 0, "removed": 1, "value": 2, "status": 3}
CATALOG_CHECK = "CATALOG"  # the checks_run entry of a crawl (refresh.classify.CATALOG_CHECK)


@dataclass(frozen=True)
class Change:
    kind: KindSpec
    subject: ObjectRef | JoinRef
    before: Fact | None
    after: Fact | None
    change: ChangeKind


_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _when(f: Fact) -> datetime:
    return f.observed_at if f.observed_at is not None else _MIN


def _newest_first(f: Fact) -> tuple[float, str]:
    """Sort key: newest ``observed_at`` first (unknown last), ties by canonical content. The one
    tie-break of :func:`diff` and :func:`~schemaguru.knowledge.resolve.resolve`."""
    return (-_when(f).timestamp(), canonical_json(f.to_dict()))


def _by_id(facts: Sequence[Fact]) -> dict[str, Fact]:
    """One fact per id: the newest re-measure wins (ties by canonical content), as in
    ``resolve``, so both choose the same fact from one set."""
    out: dict[str, Fact] = {}
    for f in sorted(facts, key=_newest_first, reverse=True):
        out[f.id] = f
    return dict(sorted(out.items()))


def _check_of(f: Fact) -> str | None:
    """The check whose run would have re-observed ``f``: ``CATALOG`` for catalog facts, the
    kind name for measured ones (the names a snapshot's ``checks_run`` records); ``None`` for
    facts no check produces (HUMAN, DECLARED, DRAFTED)."""
    if f.provenance is Provenance.CATALOG:
        return CATALOG_CHECK
    if f.provenance is Provenance.MEASURED:
        return f.kind.name
    return None


def diff(
    before: Sequence[Fact],
    after: Sequence[Fact],
    *,
    checks_run: frozenset[str] | None = None,
) -> list[Change]:
    """Changes between two fact sets, matched by fact id.

    ``checks_run`` (a snapshot's ``snapshot_checks_run``) names the checks that ran between the
    two sets: a fact that vanished while its check did not run is not "removed" (it was not
    looked at), so it is left out. A ``carried:`` entry is not a run. ``None`` (the default)
    reports every vanished fact.

    Value comparison is on canonical JSON (stable across store round-trips, where Decimal and
    datetime come back as strings). ``observed_at`` and ``snapshot_id`` never count. A fact whose
    value and status both changed yields two changes (``value`` then ``status``). Output is sorted
    by (kind, subject, fact id, change kind).
    """
    b, a = _by_id(before), _by_id(after)
    out: list[Change] = []
    for fid in b.keys() | a.keys():
        old, new = b.get(fid), a.get(fid)
        if old is None and new is not None:
            out.append(Change(new.kind, new.subject, None, new, "added"))
        elif new is None and old is not None:
            check = _check_of(old)
            if checks_run is None or check is None or check in checks_run:
                out.append(Change(old.kind, old.subject, old, None, "removed"))
        elif old is not None and new is not None:
            if canonical_json(old.value) != canonical_json(new.value):
                out.append(Change(new.kind, new.subject, old, new, "value"))
            if old.status is not new.status:
                out.append(Change(new.kind, new.subject, old, new, "status"))

    def key(c: Change) -> tuple[str, str, str, int]:
        f = c.after if c.after is not None else c.before
        assert f is not None
        return (c.kind.name, subject_key(c.subject), f.id, _RANK[c.change])

    return sorted(out, key=key)
