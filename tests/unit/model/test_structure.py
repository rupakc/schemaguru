from datetime import datetime, timezone

import pytest

from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Constraint, Schema, Table

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)
T = ObjectRef("c", "shop", "orders")


def test_roundtrips() -> None:
    t = Table(T, "view", comment="x", row_count=0, bytes=5, created_at=NOW, last_altered=NOW,
              partitioning="DAY(created_at)", clustering=("id",), withdrawn_at=NOW)  # fmt: skip
    assert Table.from_dict(t.to_dict()) == t
    c = Column(ObjectRef("c", "shop", "orders", "id"), 1, "INT64", "INTEGER", False, "k", "0")
    assert Column.from_dict(c.to_dict()) == c
    k = Constraint("foreign_key", T, ("cid",), ObjectRef("c", "shop", "customers"), ("id",), False)
    assert Constraint.from_dict(k.to_dict()) == k
    s = Schema("c", "shop", "hello", 3)
    assert Schema.from_dict(s.to_dict()) == s


def test_validation() -> None:
    with pytest.raises(ValueError):
        Table(T, "bogus")
    with pytest.raises(ValueError):
        Table(T, "table", created_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        Column(T, 1, "INT", "INTEGER")  # not a column ref
    with pytest.raises(ValueError):
        Constraint("check", T, ("a",))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Constraint("primary_key", T, ())
