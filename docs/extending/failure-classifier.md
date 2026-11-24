# Failure classifier

A failure classifier reads one failed query and its error message and says which table and
column the failure is about, so [runtime feedback](../guide/feedback.md) can prioritise them.

## Protocol

```py
@dataclass(frozen=True)
class FeedbackRecord:
    table: ObjectRef | None
    column: str | None
    failure_class: Literal["type_mismatch", "unknown_column", "unknown_table", "ambiguous_column"]

class FailureClassifier(Protocol):
    def classify(self, sql: str, error: str, dialect: str | None) -> list[FeedbackRecord]: ...
```

Return an empty list when the message is not yours. A classifier that raises contributes
nothing (the intake never raises). Records hold identifiers only; the SQL is never stored.
The built-ins are `schemaguru.feedback.BUILTIN_CLASSIFIERS`.

## Worked example: DuckDB comparison errors

DuckDB's "Cannot compare values of type VARCHAR and type DATE" carries no position, so the
built-in `type_mismatch` classifier records nothing for it. A dialect-specific classifier can
use a narrower rule — here, the one column compared with a `DATE` literal:

```python
import re

import schemaguru as sg
from schemaguru.feedback import BUILTIN_CLASSIFIERS, FeedbackRecord, report_error


class DuckDBCompare:
    _err = re.compile(r"Cannot compare values of type (\w+) and type (\w+)")
    _cmp = re.compile(r"(\w+)\.(\w+)\s*[<>=]+\s*DATE\s*'", re.I)
    _from = re.compile(r"FROM\s+(\w+)\.(\w+)\s+(?:AS\s+)?(\w+)", re.I)

    def classify(self, sql, error, dialect):
        if dialect != "duckdb" or not self._err.search(error):
            return []
        cmp, frm = self._cmp.search(sql), self._from.search(sql)
        if not (cmp and frm and cmp.group(1) == frm.group(3)):
            return []
        table = sg.ObjectRef(None, frm.group(1), frm.group(2))
        return [FeedbackRecord(table, cmp.group(2), "type_mismatch")]


guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
sql = "SELECT count(*) FROM shop.orders o WHERE o.created_at > DATE '2025-06-01'"
err = "Binder Error: Cannot compare values of type VARCHAR and type DATE"
n = report_error(guru.store, sql, err, "duckdb",
                 classifiers=[*BUILTIN_CLASSIFIERS, DuckDBCompare()])
print(n, guru.feedback_summary())
guru.close()
```

Pass the built-ins along with yours: `classifiers=` replaces the default list.
`Guru.report_error` uses the built-ins only; call `schemaguru.feedback.report_error` (as
above) to use your own.

## Test template

```py
def test_classifier():
    c = DuckDBCompare()
    sql = "SELECT 1 FROM shop.orders o WHERE o.created_at > DATE '2025-01-01'"
    err = "Cannot compare values of type VARCHAR and type DATE"
    [rec] = c.classify(sql, err, "duckdb")
    assert (rec.table.table, rec.column, rec.failure_class) == ("orders", "created_at", "type_mismatch")
    assert c.classify(sql, "some other error", "duckdb") == []
    assert c.classify(sql, err, "postgres") == []
```
