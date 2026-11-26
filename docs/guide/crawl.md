# Crawl the catalog

A crawl reads the warehouse's **system catalog only** — schemas, tables, columns, types,
declared keys, comments and statistics — and writes it to the store. It never reads a data
row.

```python
import schemaguru as sg

guru = sg.Guru.connect("duckdb:///warehouse.duckdb", crawl=False)
report = guru.crawl(include=["shop.*"], exclude=["shop.*_t"])
print(report.tables_seen, "seen,", report.tables_written, "written,", report.withdrawn, "withdrawn")
print(report.queries, "metadata queries in", round(report.duration_s, 2), "s")
print(sorted(t.ref.canonical() for t in guru.store.iter_tables()))
for d in report.errors:
    print(d.code, d.message)
guru.close()
```

## How it scales

* **Paged and streaming.** Schemas, then tables in pages (`CrawlConfig.page_size`, default
  1,000); columns and constraints are fetched in batches (`column_batch`, default 200 tables
  per query) by up to `workers` threads (default 4), each query with a timeout
  (`timeout_s`, default 60). Each page is written before the next is read, so memory is
  bounded by the page size, not the warehouse.
* **Resumable.** Progress is checkpointed after every page; an interrupted crawl continues
  from its checkpoint. Schemas listed before the checkpoint are skipped unless the store has
  never seen them (created since the interruption); a checkpoint saved under a different
  include/exclude scope is discarded and the crawl starts from the top.
* **Incremental.** When the reader can list "changed since" (`last_altered`), a later crawl
  re-reads only changed tables, plus a name-only listing per schema to detect drops. A
  change of the include/exclude scope makes the next crawl a full one.
* **Withdrawn, not deleted.** A table missing from a complete listing is marked withdrawn;
  its facts are kept for one refresh cycle and reported as drift. Human overlays on it are
  never deleted.
* **Failure isolation.** A schema, page or batch that fails is skipped and reported
  (`SG102`); the crawl continues.

## Scope filters

`include` and `exclude` are case-sensitive globs matched against `schema.table` and
`catalog.schema.table`. An empty include allows everything; exclude wins. The defaults
exclude `information_schema.*`, `pg_catalog.*`, `sys.*`, `*.*_staging*` and `*.*tmp*`.
Excluded objects are never stored. Set them for every crawl in `Config(crawl=...)`:

```python
cfg = sg.Config(crawl=sg.CrawlConfig(include=("shop.*",), page_size=500))
print(cfg.crawl.exclude)
```

## Readers

One reader per dialect, each reading only that warehouse's system views with bind
parameters for every value; the metadata SQL is the only SQL SchemaGuru writes.

| Dialect | Reads | Validated |
|---|---|---|
| DuckDB | `duckdb_tables()`, `duckdb_columns()`, `duckdb_constraints()` | real engine, CI |
| PostgreSQL | `pg_catalog` (row estimates from `reltuples`, comments from `pg_description`) | real engine, CI |
| MySQL / MariaDB | `information_schema` | real engine, CI |
| ClickHouse | `system.tables`, `system.columns` (no foreign keys) | real engine, CI |
| SQL Server / Synapse | `sys.*` views, `MS_Description` comments | real engine, CI |
| Trino | `<catalog>.information_schema.*` (constraints usually absent) | real engine, CI |
| BigQuery | `region-*.INFORMATION_SCHEMA.*` (billed: admitted against `Budget.crawl_bytes`) | recorded metadata only |
| Snowflake | `<db>.INFORMATION_SCHEMA.*`, `SHOW … KEYS` | recorded metadata only |
| Redshift | `SVV_*` views, `pg_constraint` | recorded metadata only |
| Databricks | Unity Catalog `system.information_schema.*` | recorded metadata only |
| Oracle | `ALL_*` dictionary views | recorded metadata only |

Known limits: the BigQuery incremental listing misses schema-only changes (a column added
without the table's `last_altered` moving), so run a periodic full crawl
(`schemaguru.catalog.crawl(guru.reader, guru.store, incremental=False)`); Redshift has no
incremental listing.
Every shipped reader also implements `view_definitions` (`ReaderCapabilities.view_definitions`):
the crawl reads each view's SQL from the engine's metadata views and parses it into
[lineage](lineage.md) (needs the `lineage` extra; `CrawlReport.views_parsed` counts them and
`lineage_diagnostics` carries `SG211`/`SG212`). A custom reader that lacks the method is never
asked. Per-warehouse permissions and cost notes are in the project wiki's warehouse guides.

A warehouse that is not listed needs a [custom reader](../extending/catalog-reader.md).

## Cost

A full crawl from Python:

```python
from schemaguru.catalog import crawl

guru = sg.Guru.connect("duckdb:///warehouse.duckdb", crawl=False)
print(crawl(guru.reader, guru.store, incremental=False).tables_seen)
guru.close()
```

Every crawl reports the metadata queries it ran and, where the warehouse bills metadata
(BigQuery), the bytes billed (`report.bytes_billed`; `bytes_billed_is_estimate` says when it
is a lower bound). `Budget.crawl_bytes` caps those bytes: before each listing query the bytes
billed so far in this crawl are compared with it, and once it is reached the crawl stops
between pages with warning `SG110`, keeps its checkpoint, and the next `crawl()` (or
`schemaguru crawl`) resumes where it stopped. Readers that report no bytes (every warehouse
but BigQuery today) are not counted, so the cap never stops them.
