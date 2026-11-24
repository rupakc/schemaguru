# Extending: FailureClassifier

> A `FailureClassifier` turns a failed agent query and its error message into identifier-only feedback records, such as "unknown column `amount` in `shop.orders`".
> Built-ins cover common messages for type mismatches, unknown columns, unknown tables and ambiguous columns; add your own for messages they do not recognise.
> A classifier never sees the store and never keeps SQL text; when unsure it should return nothing.

## The protocol

```python
class FailureClassifier(Protocol):
    def classify(self, sql: str, error: str, dialect: str | None) -> list[FeedbackRecord]: ...

@dataclass(frozen=True)
class FeedbackRecord:
    table: ObjectRef | None      # as the SQL wrote it; intake resolves it to a stored table
    column: str | None
    failure_class: Literal["type_mismatch", "unknown_column", "unknown_table", "ambiguous_column"]
```

Intake resolves each record's table against the store (a unique match, schema optional) and
skips records it cannot attribute. Blank string literals before reading tokens from the SQL;
the built-ins do.

## Worked example

```python
class UnrecognizedName:
    """BigQuery-style 'Unrecognized name: amount' -> unknown_column on the only table in the SQL."""

    _err = re.compile(r"Unrecognized name: (\w+)")
    _from = re.compile(r"\b(?:FROM|JOIN)\s+([\w.]+)", re.IGNORECASE)

    def classify(self, sql, error, dialect):
        m, t = self._err.search(error), self._from.findall(sql)
        if not m or len(t) != 1:
            return []                                    # not sure: record nothing
        table = ObjectRef.parse(t[0])
        return [FeedbackRecord(table, m.group(1), "unknown_column")]


MY_CLASSIFIERS = (*BUILTIN_CLASSIFIERS, UnrecognizedName())


MY_CLASSIFIERS = (*BUILTIN_CLASSIFIERS, UnrecognizedName())
```

(With `from schemaguru.feedback import BUILTIN_CLASSIFIERS, FeedbackRecord` and
`from schemaguru.model.refs import ObjectRef`.) `Guru.report_error` uses the built-ins; to use
yours, call the intake function directly:

```python
from schemaguru.feedback import report_error
report_error(guru.store, "SELECT amount FROM shop.orders",
             "Unrecognized name: amount at [1:8]", "bigquery", classifiers=MY_CLASSIFIERS)
# 1   (real result on the shop fixture: one record written)
```

There is no registry for failure classifiers in 0.1.0 (and no entry-point discovery yet):
pass them explicitly as above.

## Test template

```python
def test_unrecognized_name():
    recs = UnrecognizedName().classify("SELECT amount FROM shop.orders",
                                       "Unrecognized name: amount at [1:8]", "bigquery")
    assert [(r.table.canonical(), r.column) for r in recs] == [("shop.orders", "amount")]

def test_negative_control_two_tables_is_unsure():
    sql = "SELECT amount FROM shop.orders JOIN shop.payments USING (order_id)"
    assert UnrecognizedName().classify(sql, "Unrecognized name: amount", None) == []
```

## See also

- [[Feedback-and-Guard]]
- [[Measurement]]
- [[Diagnostics]]
- [[Contributing]]
