# Warehouse: SQL Server / Azure SQL / Synapse

> The `tsql` reader crawls the connection's database from the `sys.*` catalog views, with comments from the `MS_Description` extended property.
> Row counts come from `sys.partitions` without scanning data; Synapse's `NOT ENFORCED` keys are reported as not enforced.
> Validated against a real SQL Server 2022 in CI (service container).

Dialect name: `tsql` (extra: `pip install schemaguru[mssql]`, driver `pyodbc`; install the
Microsoft ODBC Driver 18 for SQL Server).

## Metadata sources
`sys.schemas`, `sys.objects` (types `U` and `V`), `sys.partitions`, `sys.columns`, `sys.types`,
`sys.default_constraints`, `sys.key_constraints`, `sys.index_columns`, `sys.foreign_keys`,
`sys.foreign_key_columns`, `sys.extended_properties`. The connection's database is the scope;
refs carry no catalog.

## Permissions needed
`sys.*` shows only objects the login has some permission on, so grant `VIEW DEFINITION` on the
schemas to crawl (or membership in `db_datareader`, which implies visibility). Nothing is written.

## Cost
Metadata queries only; no table data is read. Row counts come from `sys.partitions` (no scan).

## What is reported
Comments from the `MS_Description` extended property (tables, columns, schemas), exact row
counts for tables, defaults, primary keys, unique constraints and foreign keys.

## Quirks
- `sys.objects.create_date` / `modify_date` are server-local with no zone, so `created_at` and
  `last_altered` are left unknown instead of guessed; incremental listing is not offered.
- Sizes are not reported (`bytes` unknown): `sys.allocation_units` is absent on Synapse.
- Synapse dedicated pools declare primary and unique keys `NOT ENFORCED`; the reader detects the
  engine through `SERVERPROPERTY('EngineEdition')` and reports `enforced=False`.
- A disabled foreign key is reported `enforced=False`.
- View column nullability is reported as unknown.
- Type text is rebuilt from `sys.types` and `sys.columns` (`nvarchar(255)`, `decimal(10,2)`,
  `datetime2(3)`); alias types keep their own name but take the family of their base type.
- Names are ordered and paged with a binary collation, so case-insensitive databases page
  correctly.
- A column or table name is returned exactly as stored; use `[ ]` quoting in SQL, the SchemaGuru
  canonical form uses double quotes.

## See also
- [[Catalog-and-Crawl]]
- [[Feedback-and-Guard]] (why `[...]` in agent SQL can be refused)
- [[Concepts]]
- [[Extending-CatalogReader]]
