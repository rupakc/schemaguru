"""Diff two fact sets by identity and content, never by timestamps (spec 15)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from schemaguru._util.hashing import canonical_json
from schemaguru.model.facts import Fact
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import KindSpec
from schemaguru.model.refs import ObjectRef

ChangeKind = Literal["added", "removed", "value", "status"]
_RANK: dict[str, int] = {"added": 0, "removed": 1, "value": 2, "status": 3}


@dataclass(frozen=True)
class Change:
    kind: KindSpec
    subject: ObjectRef | JoinRef
    before: Fact | None
    after: Fact | None
    change: ChangeKind


def subject_key(subject: ObjectRef | JoinRef) -> str:
    return subject.identity_key() if isinstance(subject, JoinRef) else subject.canonical()


def _by_id(facts: Sequence[Fact]) -> dict[str, Fact]:
    """One fact per id; duplicates resolve deterministically (greatest canonical content)."""
    out: dict[str, Fact] = {}
    for f in sorted(facts, key=lambda f: canonical_json(f.to_dict())):
        out[f.id] = f
    return out


def diff(before: Sequence[Fact], after: Sequence[Fact]) -> list[Change]:
    """Changes between two fact sets, matched by fact id.

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
