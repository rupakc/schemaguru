# Lineage

> Lineage says what a table or column is built from and what reads it, taken from dbt manifests, view definitions and human overlays.
> It is declared or parsed, never measured and never verified, and it is never a join path.
> Parsing SQL needs the optional `lineage` extra; only a hash and a literal-free template of a view's SQL are ever stored.

## What it is

`analytics.revenue_daily` is a view over `shop.orders` and `shop.refunds`. That fact is lineage.
SchemaGuru stores it as a `LINEAGE` fact on the view (table level) and, per column, as
`COLUMN_LINEAGE` facts (for example `net_revenue` comes from `orders.amount` and
`refunds.amount`, `transform: aggregate`). Both kinds are always `UNVERIFIED`: they state what a
tool or a person declared, or what a SQL text says, not what the data does, and nothing in
SchemaGuru promotes them.

Lineage is **not a join path**. Two tables built from the same source are not thereby joinable;
`find_join_path` never reads lineage, and a lineage answer never names a join key. See
[[Join-Graph]] for how joins are measured.

## Where it comes from

| Source | Provenance | Notes |
|---|---|---|
| dbt manifest | `DECLARED` | `depends_on` gives tables; `compiled_code` gives columns (run `dbt compile`, else `SG214`) |
| View definitions | `CATALOG` | the crawl reads each view's SQL from the engine's metadata views ([[Catalog-and-Crawl]]) |
| Overlays | `HUMAN` | a `lineage:` list; a human entry replaces parsed links, `ignore: true` hides them ([[Overlays]]) |

Every shipped reader implements `view_definitions` (PostgreSQL and Redshift `pg_views`, DuckDB
`duckdb_views()`, Snowflake, BigQuery, Databricks, MySQL and Trino `information_schema`, ClickHouse
`system.tables.as_select`, SQL Server `sys.sql_modules`, Oracle `ALL_VIEWS` and `ALL_MVIEWS`).
A custom reader without it is never asked.

## The `lineage` extra

`pip install 'schemaguru[lineage]'` installs sqlglot. Without it the crawl does not read view
definitions at all (`SG212`, once per crawl or import), dbt import yields table lineage only, and
overlay lineage still works. So view lineage of any level, and every column link parsed from SQL,
needs the extra.

## Parsing is contained

SQL is parsed in a separate worker process, started once per crawl or import. Each parser request
has a hard 5 s cap: on a timeout the worker is killed and replaced, and that definition gets
`SG211 timeout` and no lineage. A definition takes two requests (tables, then lineage). A
definition over 1,000,000 characters is skipped before parsing. On Linux the worker also has a
1 GiB memory cap. A failure of any kind is a diagnostic for that definition; the crawl goes on.

## Privacy

The raw definition text lives in memory only while it is parsed. The store keeps the lineage
facts, a sha256 of the definition plus the relation names it reads (so an unchanged view whose
names still resolve the same way is not parsed again) and a template of it
with every literal replaced by a placeholder (`"..."` too, in dialects where it can be a string:
BigQuery, Databricks/Spark, Hive, MySQL/MariaDB, T-SQL, unknown). Nothing raw is persisted, logged, rendered, put in a
diagnostic or sent to an LLM. See [[PII-and-Sampling-Policy]] and [[Security]].

## What an agent sees

- `agent` view: `Built from: <ref>, <ref> (+N more) · <source> · unverified` under the description,
  and `← <input> (<transform>)` after a column (at most 3 inputs, then `+N more`; an input the
  store does not hold reads `(not in catalog)`).
- `planner` view: the table line only. `verifier` view: keyed lines, one per distinct link:
  `LINEAGE <table> <- <ref>[, <ref>…]` and `COLUMN_LINEAGE <table>.<column> <- <ref>… <transform>`,
  an unresolved ref written `name(unresolved)`. See [[Rendering]].
- The `lineage` tool: `ref`, `direction` (`upstream` or `downstream`), `depth` 1 to 5 (default 2),
  `level` (`table` or `column`). The result has `text`, `nodes`, `edges`, `truncated` (200-node cap)
  and `cycles`. It works from Python (`guru.lineage(...)` returns a `LineageResult`; use
  `guru.handle_tool_call("lineage", {...})["text"]` for text), the CLI (`schemaguru lineage REF`),
  `GET /lineage` ([[Server]]) and MCP ([[Agent-Integration]]).

## Limits

- Never a join path; never verified.
- `SELECT *` and unqualified columns are expanded from the columns the store holds; a name it does
  not hold is listed as unresolved, never guessed.
- Projection lineage only: a column used only in `WHERE`, `JOIN ON` or `GROUP BY` is not an input.
- An unaliased computed output (a bare `SUM(x)`) gets no column lineage.
- A parser worker outlives a hard-killed parent only until its current parse returns (at most the
  5 s cap). A deleted `JOIN` edge can stay in the cached graph until the snapshot, generation or
  date changes.
- A custom store without the lineage index answers downstream queries with `SG705`
  ([[Extending-Store]]).

Diagnostics: `SG211` (a definition could not be parsed), `SG212` (extra missing), `SG213` (an
ignore matches nothing), `SG214` (no `compiled_code`), `SG215` (dbt nodes disagree), `SG705`
([[Diagnostics]]). At 500,000 tables the lineage parse is part of the measured crawl ([[Scale]]).

## See also

- [[Overlays]] - the `lineage:` entries and ignore semantics
- [[Catalog-and-Crawl]] - how view definitions are read
- [[Rendering]] - the lineage lines in each profile
- [[Scale]] - lineage at 500,000 tables
