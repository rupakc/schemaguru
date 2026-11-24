# Warehouse: Databricks (Unity Catalog)

> The `databricks` reader crawls every Unity Catalog catalog the principal can see from `system.information_schema` (three-level names: catalog.schema.table).
> Queries run on a SQL warehouse and are metadata-only; Unity Catalog exposes no row counts or sizes, so those stay unknown.
> Built from documented view shapes and tested against recorded fixtures; not yet validated against a live workspace.

Reader: `databricks`. Three-level namespace: catalog.schema.table. Extra: `pip install "schemaguru[databricks]"` (`databricks-sql-connector`).

## Metadata sources
`system.information_schema.{catalogs, schemata, tables, columns, table_constraints, key_column_usage, referential_constraints}`. One `system.information_schema` serves every catalog the principal can see, so one connection reads all of them. The `system` catalog and `information_schema` schemas are skipped.

## Permissions
`USE CATALOG`, `USE SCHEMA` and `SELECT` or `BROWSE` on the objects to describe (Unity Catalog filters `information_schema` to what the principal may see); `SELECT` on `system.information_schema` (granted to all account users by default). A running SQL warehouse (or serverless) executes the queries.

## Cost
Each query runs on the SQL warehouse (DBUs); they are metadata-only and short. No bytes are billed.

## Quirks
- `information_schema` exposes **no row counts or sizes**; `row_count` and `bytes` stay unknown (`DESCRIBE DETAIL` per table would be needed).
- Table types: `MANAGED`, `VIEW`, `MATERIALIZED_VIEW`, `STREAMING_TABLE` map to table/view kinds; `EXTERNAL` and `FOREIGN` become `external`.
- Column native types use `full_data_type` (`decimal(10,2)`, `map<string,string>`); `ordinal_position` is reported as stored (Unity Catalog counts from 0).
- PK/FK constraints are informational (`enforced=False`); foreign-key targets are resolved through `referential_constraints` and the key's `position_in_unique_constraint`.
- Names are lower-case as stored; flexible names (hyphens) are kept verbatim.
- Live test: `SCHEMAGURU_LIVE_DATABRICKS=1` with `SCHEMAGURU_DBX_{HOST,HTTP_PATH,TOKEN,CATALOG,SCHEMA}`.

## See also
- [[Catalog-and-Crawl]]
- [[Scale-and-Cost]]
- [[Concepts]]
- [[Extending-CatalogReader]]
