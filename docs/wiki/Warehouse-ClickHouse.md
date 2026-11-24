# Warehouse: ClickHouse

Dialect name: `clickhouse` (extra: `pip install schemaguru[clickhouse]`, driver
`clickhouse-connect`).

## Metadata sources
`system.databases`, `system.tables`, `system.columns`. A database is a schema; refs carry no
catalog.

## Permissions needed
`system.tables` and `system.columns` list only objects the user can see. Grant `SHOW TABLES` /
`SHOW COLUMNS` (or `SHOW DATABASES`) on the databases to crawl. `SELECT` on data is not needed.

## Cost
Metadata queries only. `system.*` reads are in-memory on the server. No data is scanned.

## What is reported
Comments, exact `total_rows` and `total_bytes` for MergeTree-family engines, the partition key
(`partitioning`), the sort key as `clustering` when it is a plain column list, metadata
modification time (`last_altered`, incremental listing supported).

## Quirks
- **No constraints.** ClickHouse has no foreign keys and its `PRIMARY KEY` is a sparse sort index,
  not a uniqueness guarantee, so `constraints()` returns nothing. Never infer a key from it.
- `Nullable(...)` and `LowCardinality(...)` are unwrapped before the type family is chosen; a
  column is nullable only when `Nullable` is present. `native_type` keeps the full declared text.
- `total_rows` / `total_bytes` are `NULL` for views and some engines; they stay unknown.
- A sort key that is an expression (for example `toDate(at)`) is not a column list, so
  `clustering` is unknown.
- `ALIAS`/`MATERIALIZED` columns have no default value; only `DEFAULT` expressions are reported.
- The server does not support correlated subqueries, so the schema table count is computed with a
  join.
- Parameters are interpolated client-side by the driver (the DB-API `format` style); the reader
  still binds every value and builds no SQL from them.
