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


def test_classify_not_measured_rule_unit() -> None:
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


def test_status_change_review() -> None:
    assert cls([join("ok")], [join("ok", st=Status.UNMEASURABLE)]) == [ChangeClass.REVIEW]


def test_unknown_rule_defaults_review() -> None:
    def pk(note: str) -> Fact:
        return Fact.make(
            FactKind.PRIMARY_KEY, ORD, {"columns": ["order_id"], "note": note},
            Provenance.HUMAN, Status.OBSERVED, observed_at=T,
        )  # fmt: skip

    assert cls([pk("x")], [pk("y")]) == [ChangeClass.REVIEW]


def test_added_pii_fact_review_even_on_known_column() -> None:
    col = ObjectRef(None, "shop", "customers", "loyalty_tier")
    nr = Fact.make(
        FactKind.NULL_RATE, col, {"rate": 0.0},
        Provenance.MEASURED, Status.OBSERVED, method="exact", observed_at=T,
    )  # fmt: skip
    pii = Fact.make(
        FactKind.PII, col, {"category": "name"},
        Provenance.MEASURED, Status.OBSERVED, method="exact", observed_at=T,
    )  # fmt: skip
    got = cls([nr], [nr, pii])
    assert got == [ChangeClass.REVIEW]


def test_sampled_values_missing_is_never_review() -> None:
    def sampled(*vals: str) -> Fact:
        return Fact.make(
            FactKind.VALUES, STATUS,
            {"case_sensitive": True, "split": None,
             "observed": [{"value": v, "count": 1} for v in vals]},
            Provenance.MEASURED, Status.ESTIMATED, method="sampled:keyhash:0.1:7", observed_at=T,
        )  # fmt: skip

    got = cls([sampled("a", "b")], [sampled("a")])
    assert got == [ChangeClass.NOT_MEASURED]  # a sample never claims absence (spec 8.6)


def test_classify_many_is_not_quadratic() -> None:
    import time
    from typing import cast

    from schemaguru.knowledge.resolve import Effective
    from schemaguru.refresh.classify import classify_changes

    n = 50_000
    facts = [
        Fact.make(
            FactKind.NULL_RATE, ObjectRef(None, "shop", "t", f"c{i}"), {"rate": 0.0},
            Provenance.MEASURED, Status.OBSERVED, method="exact", observed_at=T,
        )
        for i in range(n)
    ]  # fmt: skip
    prev = cast(
        "dict[tuple[str, str], Effective]", {("NULL_RATE", f"x{i}"): None for i in range(n)}
    )
    changes = [Change(f.kind, f.subject, None, f, "added") for f in facts]
    gone = [Change(f.kind, f.subject, f, None, "removed") for f in facts]
    t0 = time.perf_counter()
    out = classify_changes(
        changes + gone, prev, {}, checks_run={"NULL_RATE"},
        measured_tables=[ObjectRef(None, "shop", f"m{i}") for i in range(n)],
    )  # fmt: skip
    assert time.perf_counter() - t0 < 2.0
    assert out[0][1] is ChangeClass.BENIGN and out[-1][1] is ChangeClass.NOT_MEASURED
