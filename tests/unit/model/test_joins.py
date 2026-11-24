import pytest

from schemaguru.model.chains import Chain, FilterStep, NoteStep
from schemaguru.model.joins import JOIN_VALUE_KEYS, JoinRef, join_verdict, support
from schemaguru.model.refs import ObjectRef

A = ObjectRef(None, "shop", "orders", "customer_id")
B = ObjectRef(None, "shop", "customers", "id")


def test_join_support_direction() -> None:
    v = {"match_rate": 0.9, "coverage": 0.2}
    assert support(v, reverse=False) == 0.9
    assert support(v, reverse=True) == 0.2
    assert support({"match_rate": None}, reverse=False) is None
    assert support({}, reverse=True) is None


def test_join_verdict_thresholds() -> None:
    assert join_verdict(0.95, 0.95, 0.05) == "OK"
    assert join_verdict(0.05, 0.95, 0.05) == "WEAK"
    assert join_verdict(0.049, 0.95, 0.05) == "BROKEN"
    assert join_verdict(None, 0.95, 0.05) is None


def test_joinref() -> None:
    j = JoinRef((A,), (B,))
    assert j.canonical() == "shop.orders.customer_id -> shop.customers.id"
    assert j.reversed() == JoinRef((B,), (A,))
    assert j.reversed().reversed() == j
    assert JoinRef.from_dict(j.to_dict()) == j
    assert "match_rate" in JOIN_VALUE_KEYS and "verdict" in JOIN_VALUE_KEYS


def test_joinref_validation() -> None:
    with pytest.raises(ValueError):
        JoinRef((), ())
    with pytest.raises(ValueError):
        JoinRef((A,), (B, B))
    with pytest.raises(ValueError):  # one table per side
        JoinRef((A, ObjectRef(None, "shop", "payments", "x")), (B, B))
    with pytest.raises(ValueError):  # columns required
        JoinRef((A.table_ref(),), (B,))


def test_chain_roundtrip() -> None:
    c = Chain(
        "cust",
        (JoinRef((A,), (B,)), FilterStep(B.table_ref(), "deleted_at IS NULL"), NoteStep("n")),
        "because",
    )
    assert Chain.from_dict(c.to_dict()) == c
