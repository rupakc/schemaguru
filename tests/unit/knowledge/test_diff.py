from datetime import datetime, timedelta, timezone
from decimal import Decimal

from schemaguru.knowledge.diff import diff, diff_split
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


def test_unrun_checks_not_removed() -> None:
    nr = _nr(0.1)
    rows = Fact.make(FactKind.ROW_COUNT, TBL, {"rows": 3}, Provenance.CATALOG, Status.OBSERVED)
    desc = Fact.make(
        FactKind.DESCRIPTION, TBL, {"text": "orders"}, Provenance.HUMAN, Status.UNVERIFIED
    )
    before = [nr, rows, desc]
    # without checks_run every vanished fact is a removal (0.1.0 behaviour)
    assert {c.before.id for c in diff(before, []) if c.before} == {nr.id, rows.id, desc.id}
    # a check that did not run says nothing about its facts: never "removed"
    got = diff(before, [], checks_run=frozenset())
    assert [(c.kind.name, c.change) for c in got] == [("DESCRIPTION", "removed")]
    got = diff(before, [], checks_run=frozenset({"NULL_RATE", "carried:ROW_COUNT"}))
    assert {c.kind.name for c in got} == {"DESCRIPTION", "NULL_RATE"}
    got = diff(before, [], checks_run=frozenset({"CATALOG"}))
    assert {c.kind.name for c in got} == {"DESCRIPTION", "ROW_COUNT"}
    # additions and value changes are reported whatever ran
    nr2 = _nr(0.4)
    got = diff([nr], [nr2, rows], checks_run=frozenset())
    assert [(c.kind.name, c.change) for c in got] == [
        ("NULL_RATE", "value"),
        ("ROW_COUNT", "added"),
    ]


def test_diff_split_returns_the_left_out_removals() -> None:
    """``diff_split``'s second list is exactly the removals ``diff`` reports without
    ``checks_run`` but leaves out with it (one pass instead of two full diffs)."""
    nr = _nr(0.1)
    rows = Fact.make(FactKind.ROW_COUNT, TBL, {"rows": 3}, Provenance.CATALOG, Status.OBSERVED)
    desc = Fact.make(
        FactKind.DESCRIPTION, TBL, {"text": "orders"}, Provenance.HUMAN, Status.UNVERIFIED
    )
    before = [nr, rows, desc]
    for ran in (frozenset(), frozenset({"CATALOG"}), frozenset({"NULL_RATE", "CATALOG"})):
        changes, unlooked = diff_split(before, [rows], checks_run=ran)
        assert changes == diff(before, [rows], checks_run=ran)
        kept = {c.before.id for c in changes if c.change == "removed" and c.before}
        every = [c for c in diff(before, [rows]) if c.change == "removed"]
        assert unlooked == [c for c in every if c.before and c.before.id not in kept]
    assert diff_split(before, [], checks_run=None)[1] == []
