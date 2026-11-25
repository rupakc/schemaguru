# Security

What SchemaGuru touches, what it stores, what it sends, and where its protections end.

## Read-only by construction

* SchemaGuru **never writes to a warehouse**. Catalog readers run metadata `SELECT`/`SHOW`
  statements on system views, with a bind parameter for every value; that metadata SQL is
  the only SQL SchemaGuru authors. Measurement SQL is rendered and run by FactLang.
* DuckDB files are opened read-only; a mistyped path is an error, not a new file.
* Give SchemaGuru a warehouse role that can read the system catalog and the data it should
  measure, and nothing more. Measurement cost is bounded by the [budget](cost-and-scale.md).

## What the store holds

The store (one SQLite file by default, or Postgres) holds structure, facts and a budget
ledger. In the default `masked` mode, observed values are stored with personal data replaced
by its shape; raw values are stored only with `llm_values="raw"` **and**
`store_raw_samples=True`. Evidence keeps SQL as a template with every literal blanked.
Runtime feedback stores identifiers and counts, never SQL text. Treat the store like the
catalog it mirrors: it reveals table and column names, statistics and low-cardinality values.

## What leaves

* **To an agent**: views and tool results, after the value policy, a final shape sweep and
  free-text masking ([PII](pii.md)).
* **To an LLM** (only when you run drafting): table and column metadata, measured statistics
  and values under the same policy; every payload is swept in masked mode. Use `none` for
  tables whose values must never leave.
* **In errors and logs**: connection URLs are shown as `scheme://host` only — never user,
  password, path or query; a URL that does not parse cleanly is shown as `scheme://***`.
  Driver error messages are scrubbed of embedded URLs.

## Spending on an agent's behalf

An agent's question never spends warehouse money by itself (`measure_on_demand="queue"`).
`check_fact`, the one agent tool that runs on the warehouse, is **off by default**
(`agent_checks="deny"`); with `"allow"` each call is priced and admitted through the budget.

## The read-only guard helper

SchemaGuru does not execute agent SQL. If your host does, `sg.guard.check_sql` is a
best-effort pre-check — a deny-list helper, **not a sandbox**:

```python
import schemaguru as sg

never = [sg.ObjectRef(None, "shop", "orders_backup_2019")]
for sql in [
    "SELECT count(*) FROM shop.orders WHERE status = 'paid'",
    "SELECT email FROM shop.customers WHERE email LIKE '%@example.com'",
    "DELETE FROM shop.orders",
    "SELECT 1; DROP TABLE shop.orders",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM shop.orders_backup_2019",
]:
    verdict = sg.guard.check_sql(sql, "duckdb", never_query=never)
    print(verdict.ok, verdict.reasons)
```

It requires a single statement starting with `SELECT` or `WITH`, applies a DML/DDL keyword
deny-list after removing comments and string literals (so `'%@x.y'` does not trigger it),
caps the length, and refuses never-query tables. It also refuses, fail-safe:

* SQL whose string or comment structure differs between dialects — so `#` comments, `$$`
  strings, bracketed identifiers containing quotes, and triple quotes may be refused;
* functions that run SQL from text or read files, file-as-table string literals and
  path-like quoted identifiers (`/`, or a file suffix such as `.csv`, `.parquet`, `.avro`,
  `.orc`, `.gz`, `.bz2`) in any FROM/JOIN position, on any part of a qualified name
  (`s."x.csv"`);
* a few legitimate forms it cannot tell apart from the above: `substring(x FROM 'regex')`,
  `ARRAY['a', 'b']`, `x['key']`.

**Run agent SQL under a read-only warehouse role.** The guard catches mistakes; permissions
stop adversaries.

## Never-query tables

`never_query` in an overlay lists tables agents must not use; every view states the list and
the guard refuses SQL reading them when you pass it. This works against a *cooperative*
agent. Isolation from an adversarial one is the warehouse's job: do not grant the role access
to those tables.

## Known limits

- The metadata guard refuses a `;` inside a backtick identifier (`` `a;b` ``), as FactLang's
  own metadata guard does: where backticks are not identifier quotes (Postgres, the ANSI
  default) that `;` separates statements. Plain backtick identifiers (`` `table_name` ``) pass.
- `literal_refusal` in `schemaguru.catalog.executor` is a public name since 0.1.1 and stays
  under that name: renaming it would remove a released name. It returns a specific reason a
  rendered literal is refused (today the zone-less Databricks timestamp), or `None`.

## Server mode

Bearer tokens of at least 16 characters, compared in constant time, never logged or echoed;
roles `reader` and `admin`; localhost binding by default; internal errors are logged, not
returned. See [Server mode](server.md).

## Reporting a vulnerability

See `SECURITY.md` in the repository.
