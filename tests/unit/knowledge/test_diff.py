from datetime import datetime, timedelta, timezone
from decimal import Decimal

from schemaguru.knowledge.diff import diff
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.refs import ObjectRef

T = datetime(2026, 11, 24, tzinfo=timezone.utc)
COL = ObjectRef(None, "shop", "orders", "status")
TBL = ObjectRef(None, "shop", "orders")


def _nr(rate: float, status: Status = Status.OBSERVED, at: datetime = T) -> Fact:
    return Fact.make(
        FactKind.NULL_RATE,
        COL,
        {"rate": rate},
        Provenance.MEASURED,
        status,
        method="exact",
        observed_at=at,
    )


def test_diff_ignores_timestamps() -> None:
    a, b = _nr(0.1), _nr(0.1, at=T + timedelta(days=3))
    assert a.id == b.id
    assert diff([a], [b]) == []


def test_diff_added_removed_value_status() -> None:
    rc = Fact.make(
        FactKind.ROW_COUNT,
        TBL,
        {"rows": 5},
        Provenance.MEASURED,
        Status.OBSERVED,
        method="exact",
        observed_at=T,
    )
    changes = diff([_nr(0.1), rc], [_nr(0.2, Status.CONTRADICTED)])
    assert [(c.kind.name, c.change) for c in changes] == [
        ("NULL_RATE", "value"),
        ("NULL_RATE", "status"),
        ("ROW_COUNT", "removed"),
    ]
    assert changes[0].before is not None and changes[0].after is not None
    added = diff([], [rc])
    assert [c.change for c in added] == ["added"] and added[0].before is None


def test_diff_value_compare_is_type_insensitive_after_roundtrip() -> None:
    a = Fact.make(
        FactKind.ROW_COUNT,
        TBL,
        {"rows": 5, "d": Decimal("1.5")},
        Provenance.MEASURED,
        Status.OBSERVED,
        method="exact",
        observed_at=T,
    )
    b = Fact.from_dict({**a.to_dict(), "value": {"rows": 5, "d": "1.5"}})
    assert diff([a], [b]) == []


def test_diff_order_deterministic() -> None:
    x = [_nr(0.1), _nr(0.2)]
    assert diff([], x) == diff([], list(reversed(x)))
