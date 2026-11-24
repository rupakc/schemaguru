"""A declared soft-delete filter is checked against the catalog and the measured values
(follow-up 6d): a mismatch is reported, never applied silently."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from schemaguru.knowledge.soft_delete import mismatch, observed_values
from schemaguru.model import Fact, FactKind, ObjectRef, Provenance, Status

AT = datetime(2026, 11, 20, tzinfo=timezone.utc)
COLS = {"order_id": "integer", "status": "string", "deleted": "boolean", "Deleted_At": "timestamp"}


def values(col: str, observed: list[Any], *, distinct: int | None = None, **kw: Any) -> Fact:
    n = len(observed) if distinct is None else distinct
    return Fact.make(
        FactKind.VALUES,
        ObjectRef(None, "shop", "orders", col),
        {
            "observed": [{"value": v, "count": 1} for v in observed],
            "distinct": n,
            "distinct_observed": len(observed),
            "case_sensitive": True,
            "split": None,
            "value_policy": "masked",
        },
        Provenance.MEASURED,
        kw.pop("status", Status.OBSERVED),
        method=kw.pop("method", "exact"),
        observed_at=AT,
    )


@pytest.mark.parametrize(
    "pred",
    [
        "deleted IS NULL OR NOT deleted",
        '"deleted" IS NULL',
        "Deleted_At IS NULL",
        "deleted_at IS NULL",  # unquoted: case-folded to the one matching column
        "status <> 'removed' AND coalesce(deleted, FALSE) = FALSE",
    ],
)
def test_matching_filters_pass(pred: str) -> None:
    obs = {"status": ["paid", "removed"], "deleted": [True]}
    assert mismatch(pred, None, COLS, obs) is None


@pytest.mark.parametrize(
    ("pred", "markers", "why"),
    [
        ("is_deleted = 0", None, "names no column"),
        ("\"is_deleted\" IS NULL OR status <> 'x'", None, '"is_deleted" does not exist'),
        ("status <> 'removed'", ["removed_flag"], "'removed_flag' does not exist"),
        ("status <> 'removed'", None, "include none it compares with"),
        ("status = TRUE", None, "TRUE/FALSE"),
    ],
)
def test_mismatches_are_reported(pred: str, markers: list[str] | None, why: str) -> None:
    obs = {"status": ["paid", "refunded"], "deleted": [True]}
    problem = mismatch(pred, markers, COLS, obs)
    assert problem is not None and why in problem


def test_unknown_structure_or_incomplete_values_judge_nothing() -> None:
    assert mismatch("is_deleted = 0", None, {}, {}) is None  # table not crawled
    # status's values are not completely known: the literal cannot be judged
    assert mismatch("status <> 'removed'", None, COLS, {}) is None
    # two columns referenced: a literal could belong to either
    two = "status <> 'removed' AND Deleted_At IS NULL"
    assert mismatch(two, None, COLS, {"status": ["paid"]}) is None
    # an all-NULL marker (nothing deleted yet) is not a mismatch
    assert mismatch("Deleted_At IS NULL", None, COLS, {"Deleted_At": []}) is None


def test_observed_values_keeps_only_complete_exact_lists() -> None:
    facts = [
        values("status", ["paid", "removed"]),
        values("deleted", [True], distinct=3),  # top-k cut: incomplete
        values("order_id", ["1"], method="sampled:keyhash:0.1:7", status=Status.ESTIMATED),
    ]
    assert observed_values(facts) == {"status": ["paid", "removed"]}
