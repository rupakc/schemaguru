from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from schemaguru._util.hashing import stable_hash
from schemaguru._util.timeutil import require_aware, utcnow
from schemaguru.diagnostics import CODES, Diagnostic, DiagnosticBag, Severity


def test_diagnostics() -> None:
    d = Diagnostic("SG201", Severity.ERROR, "ambiguous", location="x", hint="h")
    assert d.to_dict() == {
        "code": "SG201", "severity": "ERROR", "message": "ambiguous", "location": "x", "hint": "h"
    }  # fmt: skip
    bag = DiagnosticBag()
    bag.add(d)
    bag.extend([Diagnostic("SG202", Severity.WARNING, "w")])
    assert [x.code for x in bag] == ["SG201", "SG202"]
    assert bag.errors() == [d] and len(bag.to_list()) == 2
    assert dict(CODES) == dict(CODES)


def test_stable_hash() -> None:
    assert stable_hash({"b": 1, "a": (1, 2)}) == stable_hash({"a": [1, 2], "b": 1})
    assert stable_hash(Decimal("1.5")) == stable_hash("1.5")
    t = datetime(2026, 1, 1, 2, tzinfo=timezone(timedelta(hours=2)))
    assert stable_hash(t) == stable_hash("2026-01-01T00:00:00+00:00")
    assert stable_hash(1).startswith("sha256:")
    with pytest.raises(TypeError):
        stable_hash(object())


def test_timeutil() -> None:
    assert utcnow().utcoffset() == timedelta(0)
    with pytest.raises(ValueError):
        require_aware(datetime(2026, 1, 1))
    assert require_aware(utcnow())
