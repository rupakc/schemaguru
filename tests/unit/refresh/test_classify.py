from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from schemaguru.knowledge.diff import Change, diff
from schemaguru.knowledge.resolve import resolve
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JoinRef
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef
from schemaguru.refresh.classify import (
    ChangeClass,
    classify_change,
    register_change_classifier,
    reset_change_classifiers,
)

T = datetime(2026, 11, 24, tzinfo=timezone.utc)
ORD = ObjectRef(None, "shop", "orders")
STATUS = ObjectRef(None, "shop", "orders", "status")
JOIN = JoinRef(
    (ObjectRef(None, "shop", "orders", "customer_id"),),
    (ObjectRef(None, "shop", "customers", "customer_id"),),
)


def values(*vals: str, st: Status = Status.OBSERVED) -> Fact:
    return Fact.make(
        FactKind.VALUES, STATUS,
        {"case_sensitive": True, "split": None,
         "observed": [{"value": v, "count": 1} for v in vals]},
        Provenance.MEASURED, st, method="exact", observed_at=T,
    )  # fmt: skip


def join(verdict: str, rate: float = 0.5, st: Status = Status.OBSERVED) -> Fact:
    return Fact.make(
        FactKind.JOIN, JOIN, {"verdict": verdict, "match_rate": rate},
        Provenance.MEASURED, st, method="exact", observed_at=T,
    )  # fmt: skip


def cls(before: list[Fact], after: list[Fact], **kw: Any) -> list[ChangeClass]:
    pe = resolve(before, now=T, ttl_days=14)
    ne = resolve(after, now=T, ttl_days=14)
    return [classify_change(c, pe, ne, **kw) for c in diff(before, after)]


def test_enum_value_added_benign() -> None:
    assert cls([values("a", "b")], [values("a", "b", "c")]) == [ChangeClass.BENIGN]


def test_enum_value_removed_review() -> None:
    assert cls([values("a", "b")], [values("a")]) == [ChangeClass.REVIEW]


def test_verdict_change_review() -> None:
    assert cls([join("ok", 0.99)], [join("weak", 0.4)]) == [ChangeClass.REVIEW]
    # a measurement that moved but kept its verdict is benign
    assert cls([join("ok", 0.99)], [join("ok", 0.97)]) == [ChangeClass.BENIGN]


def test_preexisting_contradiction_not_regression() -> None:
    bad1 = join("broken", 0.1, Status.CONTRADICTED)
    bad2 = join("broken", 0.05, Status.CONTRADICTED)
    assert cls([bad1], [bad2]) == [ChangeClass.PRE_EXISTING]
    # newly contradicted: was fine before
    got = cls([join("ok", 0.99)], [bad2])
    assert ChangeClass.PRE_EXISTING not in got and ChangeClass.REVIEW in got


def test_narrow_run_reports_not_measured() -> None:
    before = [join("ok"), values("a")]
    pe = resolve(before, now=T, ttl_days=14)
    changes = diff(before, [])
    got = {
        c.kind.name: classify_change(
            c, pe, {}, checks_run={"VALUES", "CATALOG"}, measured_tables=[ORD, STATUS.table_ref()]
        )
        for c in changes
    }
    assert got["JOIN"] is ChangeClass.NOT_MEASURED  # JOIN was not checked: never "removed"
    assert got["VALUES"] is ChangeClass.REVIEW  # checked and gone: a real removal
    # table not measured in this run
    assert (
        classify_change(
            next(c for c in changes if c.kind.name == "VALUES"),
            pe, {}, checks_run={"VALUES"}, measured_tables=[],
        )
        is ChangeClass.NOT_MEASURED
    )  # fmt: skip


def test_new_pii_looking_column_review_and_plain_benign() -> None:
    def nr(col: str) -> Fact:
        return Fact.make(
            FactKind.NULL_RATE, ObjectRef(None, "shop", "customers", col), {"rate": 0.0},
            Provenance.MEASURED, Status.OBSERVED, method="exact", observed_at=T,
        )  # fmt: skip

    assert cls([], [nr("email")]) == [ChangeClass.REVIEW]
    assert cls([], [nr("loyalty_tier")]) == [ChangeClass.BENIGN]


def test_unknown_declared_removal_defaults_review() -> None:
    f = Fact.make(
        FactKind.DESCRIPTION, ORD, {"text": "x"}, Provenance.HUMAN, Status.OBSERVED, observed_at=T
    )
    assert cls([f], [], checks_run=set()) == [ChangeClass.REVIEW]


def test_custom_classifier_wins_then_resets() -> None:
    class AlwaysBenign:
        def classify(self, change: Change, p: Any, n: Any) -> ChangeClass | None:
            return ChangeClass.BENIGN

    register_change_classifier(AlwaysBenign())
    try:
        assert cls([values("a", "b")], [values("a")]) == [ChangeClass.BENIGN]
    finally:
        reset_change_classifiers()
    assert cls([values("a", "b")], [values("a")]) == [ChangeClass.REVIEW]


@pytest.fixture(autouse=True)
def _clean() -> Any:
    reset_change_classifiers()
    yield
    reset_change_classifiers()
