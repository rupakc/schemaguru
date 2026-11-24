# Runtime feedback

When an agent's query fails, the failure says something about the warehouse: a column that
is stored as text but compared as a date, a column that does not exist where the agent
thought. Tell SchemaGuru, and the next measurement run looks there first.

```python
import schemaguru as sg

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
sql = "SELECT count(*) FROM shop.orders o WHERE o.created_at > DATE '2025-06-01'"
# a BigQuery-style message: operand types plus the position of the operator
error = (f"No matching signature for operator > for argument types: STRING, DATE "
         f"at [1:{sql.index('>') + 1}]")
print(guru.report_error(sql, error, dialect="bigquery"), "record(s)")
# without a position the failing column cannot be identified: nothing is recorded
print(guru.report_error(sql, "Cannot compare values of type VARCHAR and type DATE"))
guru.report_error("SELECT o.refund_amount FROM shop.orders o",
                  'Binder Error: Table "o" does not have a column named "refund_amount"')
for row in guru.feedback_summary():
    print(row)
guru.close()
```

`report_error(sql, error_message, dialect=None)`:

* **never raises** and **never writes facts**: it only queues feedback (knowledge is never
  changed on the agent's query path);
* stores **identifiers and counts only**, keyed on `(table, column, failure class)` — never
  the SQL text;
* returns the number of records written.

## Failure classes

| Class | Detected from |
|---|---|
| `type_mismatch` | the error's operand types **and position** (BigQuery `at [line:col]`, PostgreSQL `at character N` / `LINE`), resolved to the column at that position in the SQL (aliases resolved, string literals blanked first; numeric-vs-numeric pairs ignored). A message without a position records nothing |
| `unknown_column` | "no such column" errors, resolved through aliases |
| `unknown_table` | "no such table" errors |
| `ambiguous_column` | "ambiguous column" errors |

Each is a plug-in `FailureClassifier`; [add your own](../extending/failure-classifier.md).

## Effect

Feedback raises the table's measurement priority (`guru.measure(all=True)` measures the
hottest tables first) and, for `unknown_*`, triggers a targeted re-crawl of the table at the
next refresh. `guru.feedback_summary()` lists the hot spots, including those no fact covers
yet.

For refusing unsafe SQL *before* it runs, see the read-only guard helper in
[Security](security.md#the-read-only-guard-helper).
