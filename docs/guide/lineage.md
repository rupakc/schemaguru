# Lineage

What a table or column is built from, and what reads it: `analytics.revenue_daily` is made from
`shop.orders` and `shop.refunds`; its `net_revenue` column comes from `orders.amount`. SchemaGuru
reads lineage from three places, stores it as two fact kinds (`LINEAGE` per table, `COLUMN_LINEAGE`
per column), and hands it to agents through the `lineage` tool and one extra line per table in
the view.

Lineage is **declared or parsed, never measured and never verified**: every lineage fact is
`UNVERIFIED`, and no resolver step promotes it. It answers "where does this come from", not "how do
I join these". It is **never a join path**: two tables built from the same source are not thereby
joinable, and `find_join_path` does not read lineage.

## Sources

| Source | Provenance | What it gives |
|---|---|---|
| dbt manifest (`import_dbt`) | `DECLARED`, source `dbt:manifest:<id>` | table lineage from `depends_on.nodes`; column lineage from `compiled_code` |
| View definitions (the crawl) | `CATALOG`, source `view_definition` | table and column lineage parsed from the view's SQL |
| [Overlays](overlays.md#lineage-entries) | `HUMAN`, source `overlay:<file>:<line>` | what a person says, which always beats a parsed link |

**dbt.** `guru.import_dbt("target/manifest.json")` writes a `LINEAGE` fact for every model, snapshot
or seed whose relation is in the store, from `depends_on.nodes` mapped to relations (sources and
refs; tests, macros and metrics are not relations, and an ephemeral model's own inputs are
inherited by its consumers). Column lineage is parsed from each node's `compiled_code` with the
node's adapter dialect. A manifest that was never compiled has no `compiled_code`: only table-level
lineage is imported, with one `SG214` per import (run `dbt compile`). Re-importing retracts the
lineage facts the new manifest no longer produces. Two nodes that write one relation from the same
inputs but with different details are `SG215`; the first by node id is kept.

**View definitions.** A reader that can read definitions reports `view_definitions=True` in its
`ReaderCapabilities`, and the crawl parses every view it writes (or re-reads) into lineage. The
sources, all metadata views and never a data table:

| Warehouse | Where the definition comes from |
|---|---|
| PostgreSQL | `pg_views.definition`, `pg_matviews.definition` |
| Redshift | `pg_views.definition` (the current database only: datashares are not listed) |
| DuckDB | `duckdb_views().sql` |
| Snowflake | `INFORMATION_SCHEMA.VIEWS.VIEW_DEFINITION` |
| BigQuery | `INFORMATION_SCHEMA.VIEWS`, `INFORMATION_SCHEMA.MATERIALIZED_VIEWS` |
| Databricks | `information_schema.views.view_definition` |
| MySQL / MariaDB | `information_schema.VIEWS.VIEW_DEFINITION` |
| ClickHouse | `system.tables.as_select` |
| SQL Server / Synapse | `sys.sql_modules.definition` |
| Oracle | `ALL_VIEWS.TEXT`, `ALL_MVIEWS.QUERY` |
| Trino | `information_schema.views.view_definition` |

A custom [catalog reader](../extending/catalog-reader.md) that does not implement
`view_definitions` keeps working; the crawl never asks it. A view is not parsed again while its
definition is unchanged (same sha256) **and** the relation names it reads still resolve as they did
(same stored relation, same columns). An upstream table entering the crawl's scope, leaving it, or
gaining or losing a column re-parses the view, so `(not in catalog)` and `SELECT *` lineage catch
up. A re-crawled view's lineage is replaced in one transaction, and a dropped view's lineage goes
with it.

## The `lineage` extra

```bash
pip install 'schemaguru[lineage]'     # sqlglot, used to parse SQL
```

Without it, nothing is parsed from SQL: a crawl does not even read the view definitions and
reports `SG212` (once per crawl or import); the dbt import still writes table-level lineage from
`depends_on`, but no column lineage. Overlay lineage needs no parser. So view lineage of any level,
and every column-level link parsed from SQL, needs the extra.

## Try it

```python
import duckdb
import schemaguru as sg

con = duckdb.connect("warehouse.duckdb")
con.execute("CREATE SCHEMA IF NOT EXISTS analytics")
con.execute("""CREATE VIEW analytics.paid_orders AS
               SELECT order_id, customer_id, CAST(order_id AS BIGINT) AS order_no
               FROM shop.orders WHERE status = 'paid'""")
con.execute("""CREATE VIEW analytics.paid_by_customer AS
               SELECT customer_id, count(*) AS paid_orders
               FROM analytics.paid_orders GROUP BY customer_id""")
con.close()

guru = sg.Guru.connect("duckdb:///warehouse.duckdb", crawl=True)
print(guru.handle_tool_call("lineage", {"ref": "analytics.paid_by_customer", "depth": 2})["text"])
print(guru.handle_tool_call("lineage", {"ref": "shop.orders", "direction": "downstream"})["text"])
result = guru.lineage("analytics.paid_orders.order_no")
print([(e.downstream, e.upstream, e.transform) for e in result.edges])
```

```text
Lineage upstream of analytics.paid_by_customer (table level, depth 2) — declared/parsed, unverified; not a join path
  analytics.paid_by_customer <- analytics.paid_orders [view_definition]
  analytics.paid_orders <- shop.orders [view_definition]
Lineage downstream of shop.orders (table level, depth 2) — declared/parsed, unverified; not a join path
  analytics.paid_orders <- shop.orders [view_definition]
  analytics.paid_by_customer <- analytics.paid_orders [view_definition]
[('analytics.paid_orders.order_no', 'shop.orders.order_id', 'cast')]
```

The same from a shell: `schemaguru lineage analytics.paid_by_customer [--downstream] [--depth N]
[--level table|column] [--format text|json]`; over HTTP, `GET /lineage?ref=…&direction=…&depth=…&level=…`
([server mode](server.md)). `guru.lineage(...)` returns a `LineageResult` (it has no `__str__`; use
the tool's `text`, or read its `nodes`, `edges`, `truncated` and `cycles`). `depth` is 1 to 5, a walk
stops at 200 nodes (`truncated`), and a cycle (possible with a mis-declared dbt graph or overlay)
is reported, never followed.

## Overlay syntax

```yaml
lineage:
  - table: analytics.revenue_daily
    upstream: [shop.orders, shop.refunds]
  - column: analytics.revenue_daily.net_revenue
    inputs: [shop.orders.amount, shop.refunds.amount]
    transform: aggregate
  - table: analytics.legacy_view
    upstream: [shop.orders]
    ignore: true          # hides this parsed link; human always wins
```

* A human entry with a list **replaces** every parsed link (dbt and view) of that subject and
  level; table and column levels are independent.
* `ignore: true` **without** a list hides every parsed link of that level for the subject; **with** a
  list it hides only a parsed link whose sorted list is exactly that list. An ignore that hides
  nothing is `SG213`: usually the view was redefined and the new link is the one shown.
* Hidden facts stay in the store (`explain` shows them) and return when the entry is removed.
* Without a human entry, dbt and view links are both effective: identical lists render as one line
  naming both sources, differing lists once each.

`transform` is `copy` (a bare column), `cast`, `expression`, `aggregate`, `window` or `unknown`.
The full rules are on the [overlays page](overlays.md#lineage-entries).

## What agents see

* **`agent` profile.** After the description, `Built from: <ref>, <ref> (+N more) · <source> ·
  unverified`; per column that has lineage, `← <input>[, <input>] (<transform>)`, at most 3
  inputs then `+N more`. An input the store does not hold reads `<name> (not in catalog)`. The
  provenance footer names the lineage source and says unverified.
* **`planner` profile.** The table-level line only. **`router`**: unchanged.
* **`verifier` profile.** Keyed lines, one per distinct link (so one table can carry several
  `LINEAGE` lines): `LINEAGE <table> <- <ref>[, <ref>…]` and
  `COLUMN_LINEAGE <table>.<column> <- <ref>[, <ref>…] <transform>`, an unresolved ref written
  `name(unresolved)` with no space.
* **The `lineage` tool**, in the tool list of Python, the CLI, HTTP, MCP and the framework
  adapters: arguments `ref`, `direction` (`upstream` default or `downstream`), `depth` (1 to 5,
  default 2), `level` (`table` or `column`, default the level of `ref`). It returns JSON with
  `text`, `nodes`, `edges` (each with its `source`s and `transform`), `truncated` and `cycles`. The
  prompt block tells the agent it is declared or parsed, never measured, and never a join path.

Lineage never adds a table to a scoped view by itself: scoping stays question-driven. When a model
and one of its direct upstream tables are both in the view, the model's block shows `Built from`.

## Privacy of view SQL

The raw definition text exists in memory only while it is parsed. The store keeps the lineage facts,
the **sha256** of the definition and the relation names it reads (to skip an unchanged view next
time) and, as evidence, a
**template with every literal replaced by a placeholder**. No raw definition is persisted, logged,
rendered, put in a diagnostic or sent to an LLM; a diagnostic names a fixed failure category,
never SQL text.

## Limits

* A definition over **1,000,000 characters** is skipped before parsing (`SG211 too_large`).
* Parsing runs in a reused worker process (a fresh `python -m schemaguru.lineage.worker` process, like the spawn start method, on every OS) with a hard
  **5 s** cap per request; a definition takes two requests (extract its tables, then the lineage),
  so the cap is per request, not per definition. A timeout kills the worker, replaces it and
  gives that definition `SG211 timeout` and no lineage; a worker that dies is `SG211
  worker_died`. A worker survives a hard-killed parent only until its current parse returns (at
  most the 5 s cap).
* The worker has a **1 GiB** address-space cap on **Linux only**; on other systems only the 5 s cap
  applies.
* `SELECT *` and unqualified columns are expanded with the columns the store holds; a name it does
  not hold is listed as unresolved, never guessed.
* Projection lineage only: a column used only in `WHERE`, `JOIN ON` or `GROUP BY` is not an input.
* An unaliased computed output (a bare `SUM(x)`) gets no column lineage.
* A deleted `JOIN` edge can stay in the cached join graph until the snapshot, generation or date
  changes; lineage itself is not affected.
* Lineage is never a join path and never `VERIFIED`; a dbt or overlay link says what a person or a
  tool declared, and a parsed link says what the SQL text says, not what the data does.

## Diagnostics

| Code | Meaning |
|---|---|
| `SG211` | a definition could not be parsed (`parse_error`, `too_large`, `timeout`, `unsupported_dialect`, `worker_died`) |
| `SG212` | column-level lineage needs the `lineage` extra |
| `SG213` | a lineage `ignore` entry matches no parsed link |
| `SG214` | the dbt manifest has no `compiled_code`: table-level lineage only |
| `SG705` | the store has no lineage index (a custom store): downstream lineage is unavailable |

`SG215` (two dbt nodes write one relation with different details) is listed in the
[diagnostics reference](../reference/diagnostics.md) with the rest.

Lineage at 500,000 tables is measured on the [Scale](scale.md) page.
