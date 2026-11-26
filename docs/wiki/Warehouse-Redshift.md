# Warehouse: Redshift

> The `redshift` reader crawls databases and schemas from the `SVV_ALL_*` views, including Spectrum external schemas (catalog = database).
> Catalog queries run on the leader node and scan no table data; row counts and sizes come from `SVV_TABLE_INFO`.
> Built from documented view shapes and tested against recorded fixtures; not yet validated against a live cluster.

Reader: `redshift`. Catalog = database, schema = schema. Extra: `pip install "schemaguru[redshift]"` (`redshift-connector`).

## Metadata sources
`SVV_ALL_SCHEMAS`, `SVV_ALL_TABLES`, `SVV_ALL_COLUMNS` (cross-database, includes datashares), `SVV_TABLE_INFO` (row estimate, size), `SVV_EXTERNAL_TABLES` / `SVV_EXTERNAL_COLUMNS` (Spectrum external schemas), and `pg_constraint` / `pg_class` / `pg_attribute` for constraints.

View definitions: `pg_views.definition` of the current database (ordinary and late-binding views); a materialized view has no definition column and a datashare database is not visible there, so both are unknown. Metadata only; the text is kept in memory and never logged.

## Permissions
`USAGE` on the schemas and `SELECT` (or any privilege) on the tables to be described; `SVV_*` views show only visible objects. `SVV_TABLE_INFO` is visible only for tables the user can see (superusers see all); where a table is not listed, its row count and size are unknown. For external schemas, `USAGE` on the schema and access to the external catalog (Glue/Hive) through the associated IAM role.

## Cost
Catalog queries run on the leader node and are free of scan charges; Serverless counts a few RPU-seconds each.

## Quirks
- `tbl_rows` (from `SVV_TABLE_INFO`) includes rows deleted but not yet vacuumed; `size` is in 1 MB blocks, so `bytes` is a multiple of 1 MiB. Row counts are joined only for the current database.
- `SVV_TABLE_INFO` and `SVV_EXTERNAL_TABLES` are read once per schema listing, not once per page; a new listing (its first page) reads them again.
- Paging compares names with the database's own collation in both the `>` test and the `ORDER BY`; Redshift has no per-expression collation clause (PostgreSQL's `COLLATE "C"` is not valid there), so none is added.
- External tables are flagged `external`; a table that `SVV_ALL_COLUMNS` does not describe falls back to `SVV_EXTERNAL_COLUMNS` (types are the external type text, e.g. `varchar(65535)`).
- `PRIMARY KEY`, `UNIQUE` and `FOREIGN KEY` are informational and not enforced (`enforced=False`). Composite keys are read per column position with `generate_series` over `conkey` (up to 32 columns), because `unnest ... WITH ORDINALITY` is unavailable on Redshift. This query is exercised by recorded fixtures only, not a live cluster, until a live run is contributed.
- Live test: `SCHEMAGURU_LIVE_REDSHIFT=1` with `SCHEMAGURU_RS_{HOST,DATABASE,USER,PASSWORD,SCHEMA}`.
- **No incremental listing**: every crawl re-lists every table (still metadata only).

## See also
- [[Catalog-and-Crawl]]
- [[Warehouse-PostgreSQL]]
- [[Scale-and-Cost]]
- [[Extending-CatalogReader]]
