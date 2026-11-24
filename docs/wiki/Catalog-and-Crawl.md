# Catalog and Crawl

> The crawl reads the warehouse's own metadata (schemas, tables, columns, types, keys, comments, statistics) into the local store, without reading a single data row.
> It is paged, resumable after an interruption, incremental where the warehouse can say what changed, and its memory use does not grow with the number of tables.
> One reader per warehouse dialect turns system views into SchemaGuru objects; unknown values stay unknown.

## What the crawl does

```python
guru = sg.Guru.connect("duckdb:///warehouse.duckdb", crawl=False)
report = guru.crawl(include=["shop.*"])          # include/exclude override the config
print(report)
```

`Guru.connect` crawls automatically when the store holds no table yet (`crawl=None`, the default);
pass `crawl=True` to always crawl or `crawl=False` to never crawl on connect. A crawl is
metadata only, but it is still a set of queries against the warehouse (and billed on BigQuery),
so it is counted in the ledger.

Real output on the shop fixture (generated 2026-11-24 with SchemaGuru 0.1.0, DuckDB reader):

```text
CrawlReport(tables_seen=14, tables_written=14, withdrawn=0, errors=[], queries=6, bytes_billed=None, bytes_billed_is_estimate=False, duration_s=0.018889292143285275)
```

`bytes_billed=None` means "this warehouse does not bill metadata queries by bytes", not zero.

## How it walks the catalog

1. `list_catalogs()` (may yield nothing; most readers work in one default catalog).
2. `list_schemas(catalog)` in pages.
3. `list_tables(schema)` in pages of `crawl.page_size` tables (default 1,000).
4. `columns(tables)` and `constraints(tables)` in batches of `crawl.column_batch` tables
   (default 200), on a pool of `crawl.workers` threads (default 4), each query capped at
   `crawl.timeout_s` (default 60 s).

Each page and its checkpoint are written to the store in **one transaction**. If the crawl is
interrupted (an error, Ctrl-C, a crash), the next crawl resumes from the checkpoint: schemas
before the saved cursor are skipped and the schema in progress restarts at its saved page.

**Memory stays flat.** The names seen in this run live in a throw-away on-disk SQLite file, and
the checkpoint is a small cursor, so memory is bounded by the page size and the number of
schemas, never by the number of tables. The scale benchmark crawled 100,000 synthetic tables at
about 9,600 tables per second with a peak memory of 45 MB (see [[Scale-and-Cost]]).

## Incremental crawls

When a reader reports `incremental=True` in its capabilities, a later crawl re-reads only tables
changed since the last crawl that finished without errors, plus a cheap *name-only* listing per
schema to notice drops and restorations. Incremental mode is ignored when your include/exclude
scope changed since that crawl, because the old crawl's view of "everything" no longer matches.

## Withdrawn tables

A table that a complete listing of its schema no longer shows (or whose schema went out of scope)
is marked `withdrawn_at`. It is a soft delete: its facts and any human overlay about it are kept
and surface as drift in the next [[Refresh-and-Drift]] report. If the table comes back, it is
live again.

## Scope filters

`crawl.include` and `crawl.exclude` are case-sensitive glob patterns matched against
`schema.table` and `catalog.schema.table`. An empty include allows everything; exclude always
wins. The defaults exclude system and scratch objects:

```python
CrawlConfig().exclude
# ('information_schema.*', 'pg_catalog.*', 'sys.*', '*.*_staging*', '*.*tmp*')
```

Excluded objects are never stored. An exclude pattern whose table part is exactly `*`
(`sys.*`) skips the whole schema without listing it.

## Failure isolation

A schema, page or batch that fails is recorded as diagnostic `SG102` with its location and
skipped; the crawl continues, and `CrawlReport.errors` lists every skipped item. A crawl with
errors does not become the baseline for the next incremental crawl.

## Readers

A `CatalogReader` turns one warehouse's system views into `Schema`, `Table`, `Column` and
`Constraint` objects. Shipped readers (`sg.catalog.dialects()`):

```text
['bigquery', 'clickhouse', 'databricks', 'duckdb', 'mysql', 'oracle', 'postgres', 'redshift', 'snowflake', 'trino', 'tsql']
```

Each has a guide with permissions, cost and quirks: [[Warehouse-BigQuery]],
[[Warehouse-Snowflake]], [[Warehouse-Redshift]], [[Warehouse-Databricks]],
[[Warehouse-PostgreSQL]], [[Warehouse-DuckDB]], [[Warehouse-ClickHouse]], [[Warehouse-TSQL]],
[[Warehouse-Oracle]], [[Warehouse-Trino]], [[Warehouse-MySQL]].

How well each reader is tested:

- **Validated against real engines in CI:** PostgreSQL, MySQL, ClickHouse, SQL Server, Trino
  (service containers) and DuckDB.
- **Built from documented system-view shapes, not yet validated against live accounts:**
  BigQuery, Snowflake, Redshift, Databricks, Oracle. They are tested against recorded metadata
  fixtures, and opt-in live tests exist (`tests/live/`). Please report what you find.

Rules every reader follows:

- **Metadata only.** System catalogs, `INFORMATION_SCHEMA`, `SHOW` commands. Never a data row.
- **Bind parameters for every value.** Readers write SQL with `?` placeholders; the executor
  converts them to the driver's style. Identifiers that cannot be bound (a BigQuery project or
  region) are validated against a strict character set first.
- **Read-only, checked.** Every metadata statement passes `check_metadata_sql` first: a single
  `SELECT`/`WITH`/`SHOW`/`DESCRIBE`, no `;`, no comments, no DML/DDL keyword, judged under
  several quoting conventions. A violation is `SG104`.
- **Unknown is `None`.** A row count the warehouse does not report stays `None`; it is never 0.

## Connecting

`Guru.connect(url)` opens the URL through FactLang's `connect()` when it is available (FactLang
0.1.0; most warehouses need SQLAlchemy and the warehouse's driver extra). Without it, `duckdb://`
URLs open through the DuckDB driver directly, and DuckDB *files* open **read-only** (a typo never
creates a new file). Any other URL then fails with `SG106`.

You can also bring your own connection and reader:

```python
from schemaguru.catalog import DBAPIExecutor, reader_for
guru = sg.Guru(reader=reader_for("duckdb", DBAPIExecutor(conn)), dialect="duckdb", store="shop.db")
```

`DBAPIExecutor(connection, paramstyle="qmark")` wraps any DB-API connection;
`FactLangExecutor(connector)` wraps a FactLang connector. Connection URLs are never shown in full
in errors, logs or `repr`: only `scheme://host[:port]` is shown, and anything that does not parse
cleanly becomes `scheme://***`.

## See also

- [[Store]] - where the crawl writes
- [[Extending-CatalogReader]] - add a warehouse
- [[Refresh-and-Drift]] - recrawling and what changed
- [[Scale-and-Cost]] - crawl cost on large warehouses
