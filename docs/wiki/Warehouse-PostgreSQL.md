# Warehouse: PostgreSQL

> The `postgres` reader crawls one PostgreSQL database through `pg_catalog`, giving tables, views, materialized views, foreign tables, columns, comments, row estimates, sizes and keys.
> It reads only system catalogs; no table data is scanned, and measurement probes run through FactLang like on every warehouse.
> Validated against a real PostgreSQL 16 server in CI (service container).

Reader: `postgres` (`schemaguru.catalog.readers.postgres.PostgresReader`). Extra:
`pip install "schemaguru[postgres]"` (driver `psycopg` 3). URLs `postgresql://...` and
`postgres://...` both map to this dialect. One database per connection, so refs carry no catalog:
`schema.table`.

## Metadata sources
`pg_namespace`, `pg_class`, `pg_attribute`, `pg_attrdef`, `pg_constraint`, `pg_description`
(comments via `obj_description`), `pg_total_relation_size` (bytes) and `pg_get_partkeydef`
(partitioning). Not `information_schema`, which hides objects the role cannot use and is slow on
large catalogs.

## Permissions
A role that can connect to the database. `pg_catalog` is readable by every role; nothing is
written. Measurement probes need `SELECT` on the tables you measure, so give SchemaGuru a
read-only role (for example one granted `pg_read_all_data` on PostgreSQL 14+, or `SELECT` on the
schemas you care about).

## Cost
Catalog queries only, one per page of tables and one per batch of up to 200 tables for columns
and constraints. They are cheap even on catalogs with many thousands of relations.

## What is reported
- Kinds: ordinary and partitioned tables (`table`), views, materialized views, foreign tables
  (`external`). Partitions themselves (`relispartition`) are not listed separately; the parent
  carries the partition key as `partitioning`.
- Comments on schemas, tables and columns.
- `row_count` from `pg_class.reltuples`, an estimate maintained by `VACUUM`/`ANALYZE`. A table
  that was never analysed (`reltuples = -1`, or no pages and no tuples) has an **unknown** row
  count, never 0.
- `bytes` from `pg_total_relation_size` (table, indexes and TOAST).
- Primary, unique and foreign keys (`enforced=True`), composite keys in full, in key order.
- Column defaults as expressions (`pg_get_expr`); view column nullability is unknown.

## Quirks
- **No incremental listing** and no `last_altered`: PostgreSQL keeps no per-table modification
  time in the catalog, so every crawl re-lists every table (still metadata only).
- Schemas `information_schema` and `pg_*` are skipped; the crawl's default excludes add
  `pg_catalog.*`.
- Run `ANALYZE` before crawling if you want row estimates for freshly loaded tables.
- Names are returned exactly as stored (lower-case unless created quoted).

## See also
- [[Catalog-and-Crawl]]
- [[Warehouse-Redshift]] (a PostgreSQL-derived warehouse with its own reader)
- [[Store]] (the Postgres-backed *store* is a different thing: where SchemaGuru keeps its knowledge)
- [[Extending-CatalogReader]]
