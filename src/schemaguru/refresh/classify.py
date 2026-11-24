"""Change classification for a refresh (spec 15.4, 15.5).

Order of judgement for each :class:`Change`:

1. ``NOT_MEASURED``: a measured/catalog fact that is missing now but whose check did not run (its
   kind is not in this run's ``run_checks``, or its table was not measured) is never "removed"
   and never "fixed". ``run_checks`` is what the run measured WITHOUT error; kinds merely carried
   from tables that were not due are a separate set (``carried_checks``) and never count as run.
   An added fact with status ERROR is also ``NOT_MEASURED`` (the probe failed). Facts carried
   unchanged on a due table are classified ``NOT_MEASURED`` by ``refresh`` itself.
2. Registered custom classifiers (first one with an opinion wins).
3. The default rules: ``PRE_EXISTING`` (a contradiction the previous snapshot already had),
   then ``BENIGN`` / ``REVIEW`` per spec 15.4. Anything not recognised is ``REVIEW``.

New columns count as PII-looking when their *name* matches a PII hint or a PII fact exists for
the column. Flagging by name is conservative routing to a human, not classification: nothing is
ever recorded as PII because of it.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from enum import Enum
from typing import Any, Protocol

from schemaguru.knowledge.diff import Change, subject_key
from schemaguru.knowledge.resolve import Effective
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import KindSpec
from schemaguru.model.refs import ObjectRef

Eff = Mapping[tuple[str, str], Effective]

# kinds the scheduler produces; only these can be "not measured"
MEASURED_KINDS = frozenset(
    {
        "NULL_RATE", "DISTINCT", "VALUES", "JOIN", "PRIMARY_KEY",
        "FRESHNESS", "TYPE_NOTE", "PII", "ROW_COUNT",
    }
)  # fmt: skip
CATALOG_CHECK = "CATALOG"
# structural change that has no fact behind it (a table the catalog no longer lists)
TABLE_WITHDRAWN = KindSpec("TABLE_WITHDRAWN", "table", False, False)

_VERDICT_KEYS = ("verdict", "unique", "dead", "reason")
_PII_NAME = re.compile(
    r"(e_?mail|phone|mobile|\bssn\b|ssn$|passport|birth|dob$|iban|card_?(no|num)|"
    r"first_?name|last_?name|full_?name|address|street|postcode|zip_?code|salary|"
    r"tax_?id|national_?id|password|secret|token|ip_?addr)",
    re.IGNORECASE,
)


class ChangeClass(Enum):
    BENIGN = "BENIGN"
    REVIEW = "REVIEW"
    PRE_EXISTING = "PRE_EXISTING"
    NOT_MEASURED = "NOT_MEASURED"


class ChangeClassifier(Protocol):
    def classify(
        self, change: Change, prev_effective: Eff, new_effective: Eff
    ) -> ChangeClass | None:
        """``None`` means no opinion; the next classifier (finally the default) decides."""
        ...


_CUSTOM: list[ChangeClassifier] = []


def register_change_classifier(c: ChangeClassifier) -> None:
    _CUSTOM.append(c)


def reset_change_classifiers() -> None:
    _CUSTOM.clear()


def _fact(c: Change) -> Fact:
    f = c.after if c.after is not None else c.before
    assert f is not None
    return f


def _table_of(subject: ObjectRef | JoinRef) -> set[ObjectRef]:
    if isinstance(subject, JoinRef):
        return {s.table_ref() for s in (*subject.left, *subject.right)}
    return {subject.table_ref()}


def _was_contradicted(c: Change, prev: Eff) -> bool:
    if c.before is not None and c.before.status is Status.CONTRADICTED:
        return True
    eff = prev.get((c.kind.name, subject_key(c.subject)))
    return eff is not None and any(f.status is Status.CONTRADICTED for f in eff.all)


def _observed_values(f: Fact | None) -> set[str] | None:
    if f is None:
        return None
    obs = f.value.get("observed")
    if not isinstance(obs, list):
        return None
    return {str(i.get("value")) for i in obs if isinstance(i, dict)}


def _verdict(f: Fact) -> tuple[Any, ...]:
    return tuple(f.value.get(k) for k in _VERDICT_KEYS)


def _pii_looking(subject: ObjectRef, prev: Eff, new: Eff) -> bool:
    key = ("PII", subject.canonical())
    if key in prev or key in new:
        return True
    return bool(subject.column and _PII_NAME.search(subject.column))


def _is_new_column(subject: ObjectRef, prev_subjects: Collection[str]) -> bool:
    return subject.column is not None and subject.canonical() not in prev_subjects


def _default(c: Change, prev: Eff, new: Eff, prev_subjects: Collection[str]) -> ChangeClass:
    f = _fact(c)
    after = c.after
    if after is not None and after.status is Status.CONTRADICTED:
        return ChangeClass.PRE_EXISTING if _was_contradicted(c, prev) else ChangeClass.REVIEW
    if c.change == "removed":
        return ChangeClass.REVIEW
    if c.change == "added":
        if f.status is Status.ERROR:
            return ChangeClass.NOT_MEASURED  # the probe failed: unknown, not a finding
        if f.kind.name == "PII":
            return ChangeClass.REVIEW
        if (
            isinstance(f.subject, ObjectRef)
            and _is_new_column(f.subject, prev_subjects)
            and _pii_looking(f.subject, prev, new)
        ):
            return ChangeClass.REVIEW
        return ChangeClass.BENIGN
    if c.change == "status":
        return ChangeClass.REVIEW
    # value change
    assert c.before is not None and c.after is not None
    if f.kind.name == "VALUES":
        old, cur = _observed_values(c.before), _observed_values(c.after)
        if old is None or cur is None:
            return ChangeClass.REVIEW
        if not old - cur:
            return ChangeClass.BENIGN
        if (c.after.method or "").startswith("sampled:"):
            return ChangeClass.NOT_MEASURED  # a sample never claims a value is absent (spec 8.6)
        return ChangeClass.REVIEW
    if f.kind.name in MEASURED_KINDS and f.provenance is Provenance.MEASURED:
        return ChangeClass.BENIGN if _verdict(c.before) == _verdict(c.after) else ChangeClass.REVIEW
    return ChangeClass.REVIEW


def _not_measured(
    c: Change, checks_run: Collection[str], measured_tables: Collection[ObjectRef]
) -> bool:  # measured_tables should be a set (membership is tested per change)
    b = c.before
    if c.change != "removed" or b is None:
        return False
    if b.provenance is Provenance.CATALOG:
        return CATALOG_CHECK not in checks_run
    if b.provenance is Provenance.MEASURED and b.kind.name in MEASURED_KINDS:
        return b.kind.name not in checks_run or not any(
            t in measured_tables for t in _table_of(b.subject)
        )
    return False


def _classify(
    change: Change,
    prev: Eff,
    new: Eff,
    checks_run: Collection[str] | None,
    measured: Collection[ObjectRef],
    prev_subjects: Collection[str],
) -> ChangeClass:
    if change.kind is TABLE_WITHDRAWN:
        return ChangeClass.REVIEW
    if checks_run is not None and _not_measured(change, checks_run, measured):
        return ChangeClass.NOT_MEASURED
    for c in _CUSTOM:
        got = c.classify(change, prev, new)
        if got is not None:
            return got
    return _default(change, prev, new, prev_subjects)


def classify_change(
    change: Change,
    prev_effective: Eff,
    new_effective: Eff,
    *,
    checks_run: Collection[str] | None = None,
    measured_tables: Collection[ObjectRef] = (),
) -> ChangeClass:
    """Classify one change; ``checks_run=None`` skips the NOT_MEASURED rule."""
    subjects = {k[1] for k in prev_effective}
    return _classify(
        change, prev_effective, new_effective, checks_run, set(measured_tables), subjects
    )


def classify_changes(
    changes: Iterable[Change],
    prev_effective: Eff,
    new_effective: Eff,
    *,
    checks_run: Collection[str] | None = None,
    measured_tables: Collection[ObjectRef] = (),
) -> list[tuple[Change, ChangeClass]]:
    """Classify many changes; the lookup sets are built once, not per change."""
    subjects = {k[1] for k in prev_effective}
    measured = set(measured_tables)
    return [
        (c, _classify(c, prev_effective, new_effective, checks_run, measured, subjects))
        for c in changes
    ]
