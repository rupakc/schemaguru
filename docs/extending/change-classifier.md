# Change classifier

[Refresh](../guide/refresh.md) classifies every change between two snapshots as `BENIGN`,
`REVIEW`, `PRE_EXISTING` or `NOT_MEASURED`. A change classifier adds your own rules.

## Protocol

```py
class ChangeClassifier(Protocol):
    def classify(self, change: Change, prev_effective: Eff, new_effective: Eff) -> ChangeClass | None:
        """None means no opinion; the next classifier (finally the default) decides."""
```

A `Change` has `kind` (a `KindSpec`), `subject`, `before` and `after` (facts or `None`) and
`change` (`"added"`, `"removed"`, `"value"` or `"status"`). `prev_effective` /
`new_effective` are the resolved knowledge of both snapshots, keyed
`(kind name, subject key)`.

Order of judgement: `NOT_MEASURED` first (a check that did not run is never "removed" or
"fixed" — no classifier can override that), then registered classifiers in registration
order (the first with an opinion wins), then the default rules. Register with
`schemaguru.refresh.register_change_classifier(c)`; `reset_change_classifiers()` removes all.

## Worked example: row counts of reloaded tables are not news

```python
from schemaguru.refresh import ChangeClass, classify_change, register_change_classifier


class RowCountsMove:
    """Tables in `shop` are reloaded nightly: a row count that changes or disappears
    between snapshots is expected, not a finding."""

    def classify(self, change, prev_effective, new_effective):
        if change.kind.name == "ROW_COUNT" and change.subject.schema == "shop":
            return ChangeClass.BENIGN
        return None


register_change_classifier(RowCountsMove())

# what refresh does for one change: a row count that disappeared (by default: REVIEW)
from datetime import datetime, timezone

from schemaguru.knowledge import Change
from schemaguru.model import Fact, FactKind, ObjectRef, Provenance, Status

at = datetime(2026, 11, 24, tzinfo=timezone.utc)
ref = ObjectRef(None, "shop", "orders")
before = Fact.make(FactKind.ROW_COUNT, ref, {"rows": 100}, Provenance.MEASURED,
                   Status.OBSERVED, method="exact", observed_at=at)
change = Change(FactKind.ROW_COUNT, ref, before, None, "removed")
print(classify_change(change, {}, {}))          # BENIGN: our rule had an opinion
```

`classify_change(change, prev_effective, new_effective)` runs the whole chain for one change,
which is what a unit test calls.

## Test template

```py
from datetime import datetime, timezone

from schemaguru.knowledge import Change
from schemaguru.model import Fact, FactKind, ObjectRef, Provenance, Status

AT = datetime(2026, 11, 24, tzinfo=timezone.utc)

def row_count(n):
    return Fact.make(FactKind.ROW_COUNT, ObjectRef(None, "shop", "orders"), {"rows": n},
                     Provenance.MEASURED, Status.OBSERVED, method="exact", observed_at=AT)

def test_removed_row_count_is_benign():
    before = row_count(100)
    change = Change(FactKind.ROW_COUNT, before.subject, before, None, "removed")
    assert classify_change(change, {}, {}) is ChangeClass.BENIGN

def test_other_changes_keep_the_default():
    reset_change_classifiers(); register_change_classifier(RowCountsMove())
    other = Change(FactKind.NULL_RATE, ..., before_fact, None, "removed")
    assert classify_change(other, {}, {}) is ChangeClass.REVIEW
```
