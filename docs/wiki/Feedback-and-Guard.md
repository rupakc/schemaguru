# Feedback and Guard

> When an agent's query fails, the host can report it with `guru.report_error(sql, error)`; SchemaGuru keeps only identifiers and counts and raises the affected table's measurement priority.
> Nothing is written to the knowledge on the agent's path: feedback is queued and acted on at the next measurement run or refresh.
> The optional guard helper `sg.guard.check_sql` judges agent SQL as text (single read-only statement, no never-query tables) and never executes it; it is a deny-list helper, not a sandbox.

## Reporting failed queries

```python
try:
    rows = run_agent_sql(sql)                   # your code runs the agent's SQL
except Exception as e:
    guru.report_error(sql, str(e))              # never raises; returns records written
```

Real output on the shop fixture (2026-11-24), one DuckDB error SchemaGuru understands and one it
does not attribute:

```python
guru.report_error("SELECT o.amount FROM shop.orders o",
                  'Binder Error: Referenced column "amount" not found in FROM clause!')
# 1
guru.report_error("SELECT * FROM shop.orders WHERE created_at > DATE '2026-01-01'",
                  "Binder Error: Cannot compare values of type VARCHAR and type DATE in expression created_at > DATE '2026-01-01'")
# 0
guru.feedback_summary()
# [{'table': 'shop.orders', 'column': 'amount', 'failure_class': 'unknown_column', 'count': 1}]
```

The second call returned 0: no built-in classifier recognised that message's shape, so nothing
was recorded. That is the safe outcome (no guess), and it is why classifiers are plug-ins.

### What is stored

Records are keyed on `(table, column, failure_class)` and hold a count. **The SQL text is never
stored**, and string literals are blanked before any token is read. A record whose table cannot
be resolved to a stored table is skipped.

### Failure classes

| Class | Recognised from |
|---|---|
| `type_mismatch` | the error's position or operand types; the token at that position in the SQL, aliases resolved, numeric pairs ignored |
| `unknown_column` | "column not found"-style messages |
| `unknown_table` | "table does not exist"-style messages |
| `ambiguous_column` | "ambiguous column reference"-style messages |

Each is a `FailureClassifier` plug-in ([[Extending-FailureClassifier]]).

### What feedback does

- It pushes the table onto the measurement queue (reason `feedback:<class>`, priority = the
  record's count), so the next `guru.measure()`, `schemaguru worker` run or background drain
  re-measures it.
- It raises the table's rank for eager measurement (`guru.measure(all=True)` measures hot tables
  first).
- `guru.feedback_summary()` (and `schemaguru report`) list the hot spots.
- In 0.1.0 an `unknown_table`/`unknown_column` record does not yet trigger a targeted re-crawl of
  its own; the next refresh's incremental crawl is what picks up a renamed or new object.

It never edits a fact. If agents keep tripping over a column, the fix is a measured fact, a type
note or a pitfall, which a person or the next refresh adds.

## The guard helper

```python
from schemaguru.guard import check_sql
verdict = check_sql(sql, never_query=[sg.ObjectRef(None, "shop", "orders_backup_2019")])
if not verdict.ok:
    refuse(verdict.reasons)
```

Real verdicts (SchemaGuru 0.1.0, 2026-11-24):

```text
>>> check_sql("SELECT o.order_id FROM shop.orders o WHERE o.status = 'paid'", never_query=banned)
GuardVerdict(ok=True, reasons=())
>>> check_sql("SELECT * FROM shop.customers WHERE email LIKE '%@example.com'", never_query=banned)
GuardVerdict(ok=True, reasons=())
>>> check_sql('DELETE FROM shop.orders', never_query=banned)
GuardVerdict(ok=False, reasons=('SG801: not read-only; found DELETE', "SG801: must start with SELECT or WITH, got 'DELETE'"))
>>> check_sql('SELECT * FROM shop.orders_backup_2019', never_query=banned)
GuardVerdict(ok=False, reasons=('SG802: reads never-query table shop.orders_backup_2019',))
>>> check_sql('SELECT 1; SELECT 2', never_query=banned)
GuardVerdict(ok=False, reasons=('SG801: more than one statement',))
>>> check_sql("SELECT * FROM read_csv('/etc/passwd')", never_query=banned)
GuardVerdict(ok=False, reasons=('SG801: read_csv() runs SQL or reads a table or file named by text',))
>>> check_sql("SELECT ARRAY['a','b']", never_query=banned)
GuardVerdict(ok=False, reasons=('SG801: ambiguous across SQL dialects',))
```

What it checks: one statement; it must start with `SELECT` or `WITH`; no DML/DDL keyword
(checked after removing comments and string literals, so `LIKE '%@example.com'` is fine); at
most 100,000 characters; no never-query table; no function that runs SQL from text or reads
files, URLs or other databases (`read_*`, `query_*`, `dblink*`, `*_scan`, `pg_read_file`,
`getenv`, ...); no file or URL used as a table (`FROM '/etc/passwd'`, a path-like quoted
identifier).

### What it is not

- **Not a sandbox.** It is a best-effort deny-list over text. It does not stop expensive or
  side-effecting read-only constructs (`pg_sleep`, huge scans), and an unlisted vendor function
  passes. **Run agent SQL under a read-only warehouse role with resource limits**; the guard is an
  extra layer in front of that, not a replacement.
- **Not dialect-specific.** SQL is lexed under several dialect conventions at once (`#` and `//`
  comments, `$tag$` strings, triple quotes, `[ident]`, backslash escapes...), and anything whose
  string/comment/identifier structure differs between them is refused as "ambiguous across SQL
  dialects". So some valid dialect-specific SQL is refused by design: `#` comments, `$$` strings,
  triple quotes, `x['key']`. Known fail-safe refusals: `substring(x FROM 'regex')`,
  `ARRAY['a','b']` (T-SQL reads `[...]` as an identifier; use `ARRAY('a','b')` or a parameter).
- **`never_query` is best-effort** against a *cooperative* agent. It matches the table's last name
  part anywhere outside strings and comments, and inside string literals (dynamic SQL), but real
  isolation from an adversarial agent needs warehouse permissions: a role that cannot see the
  table.

The `dialect` argument is reserved for future use.

## See also

- [[Security]] - the overall safety model
- [[Agent-Integration]] - where agent SQL comes from
- [[Measurement]] - what raised priority leads to
- [[Extending-FailureClassifier]]
