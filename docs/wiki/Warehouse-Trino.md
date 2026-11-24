# Warehouse: Trino

Dialect name: `trino` (extra: `pip install schemaguru[trino]`, driver `trino`).

## Metadata sources
`SHOW CATALOGS`, then per catalog `<catalog>.information_schema.{schemata, tables, columns}` and
`system.metadata.table_comments` for table comments. Trino is federated: every connector catalog
is a SchemaGuru catalog and refs carry it (`system` is skipped).

## Permissions needed
The Trino user needs access to the catalogs to crawl; `information_schema` shows only what the
user may see. Table comments need read access to `system.metadata.table_comments`. Nothing is
written.

## Cost
Metadata queries only, but they are executed by the connector behind each catalog, so a slow
federated source (a remote database, a Hive metastore) makes a crawl slow. Use scope filters to
exclude catalogs you do not need.

## What is reported
Schemas, tables, views, column types, nullability, defaults, table comments and column comments.

## Quirks
- **No constraints, row counts or sizes.** Trino exposes none in metadata (`SHOW STATS` is
  per-table and connector-dependent), so those are unknown; `constraints()` returns nothing.
- Catalog names cannot be bind parameters; the reader quotes them as identifiers (`"` doubled).
  Every value is bound.
- Comment support depends on the connector (the memory connector supports table and column
  comments; others may return `NULL`).
- View column nullability is reported as unknown.
- Type text is Trino's (`varchar(100)`, `timestamp(3) with time zone`, `array(varchar)`,
  `row(...)`, `map(...)`).
- Tables are ordered and paged by name.
