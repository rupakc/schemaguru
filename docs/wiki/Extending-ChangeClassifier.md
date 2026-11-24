# Extending: ChangeClassifier

> A `ChangeClassifier` lets you decide how a refresh classifies certain changes, for example "a null rate that moved by less than one point is benign".
> Classifiers registered with `register_change_classifier` are asked before the default rules; the first one with an opinion wins, and returning `None` means "no opinion".
> The `NOT_MEASURED` rule always comes first, so no classifier can turn a check that did not run into a fix.

## The protocol

```python
class ChangeClassifier(Protocol):
    def classify(self, change: Change, prev_effective: Eff, new_effective: Eff) -> ChangeClass | None: ...
```

- `change` (`schemaguru.knowledge.diff.Change`): `kind`, `subject`, `before`, `after` (facts or
  `None`) and `change` (`added`, `removed`, `value` or `status`).
- `prev_effective` / `new_effective`: the resolved knowledge of both snapshots, keyed by
  `(kind name, subject key)`.
- Return `ChangeClass.BENIGN`, `REVIEW`, `PRE_EXISTING` or `NOT_MEASURED`, or `None`.

Order of judgement: `NOT_MEASURED` (built in), then your classifiers in registration order,
then the defaults ([[Refresh-and-Drift]]). `reset_change_classifiers()` clears yours.

## Worked example

```python
class SmallNullRateMoves:
    """A NULL_RATE that moved by less than one percentage point is benign."""

    def classify(self, change, prev_effective, new_effective):
        if change.kind.name != "NULL_RATE" or change.change != "value":
            return None                                  # no opinion: next classifier decides
        before = change.before.value.get("rate") if change.before else None
        after = change.after.value.get("rate") if change.after else None
        if before is None or after is None:              # unknown is not "small"
            return None
        return ChangeClass.BENIGN if abs(after - before) < 0.01 else None


register_change_classifier(SmallNullRateMoves())
```

(With `from schemaguru.refresh import ChangeClass, register_change_classifier`.) Treat unknown as
unknown: a `None` rate returns no opinion rather than "small". Checked against SchemaGuru 0.1.0:
a move from 0.100 to 0.101 is classified `BENIGN`, a move from 0.1 to 0.5 gets no opinion (`None`)
and falls through to the default rules.

## Test template

```python
from schemaguru.knowledge.diff import Change

def test_small_move_is_benign():
    before = null_rate_fact(0.100); after = null_rate_fact(0.101)
    change = Change(before.kind, before.subject, before, after, "value")
    assert SmallNullRateMoves().classify(change, {}, {}) is ChangeClass.BENIGN

def test_negative_control_big_move_has_no_opinion():
    change = Change(KIND, SUBJ, null_rate_fact(0.1), null_rate_fact(0.5), "value")
    assert SmallNullRateMoves().classify(change, {}, {}) is None
```

(`null_rate_fact` is your helper building a `NULL_RATE` fact with `sg.Fact.make`.)

## See also

- [[Refresh-and-Drift]]
- [[Extending-FactKind]]
- [[CLI]]
- [[Design-Decisions]]
