# Warehouse: DuckDB

> The `duckdb` reader crawls the current database of a DuckDB connection with DuckDB's own table functions, and it is the engine every example and most tests in SchemaGuru use.
> `Guru.connect("duckdb:///file.duckdb")` works even without FactLang's `connect()`: the file is opened read-only through the DuckDB driver.
> Validated against the real engine in CI on every push.

Reader: `duckdb` (`schemaguru.catalog.readers.duckdb.DuckDBReader`). Extra:
`pip install "schemaguru[duckdb]"`. The reader works in the connection's current database
(`list_catalogs` yields nothing), so refs carry no catalog: `schema.table`.

## Connecting

```python
guru = sg.Guru.connect("duckdb:///warehouse.duckdb")   # a file, opened read-only
guru = sg.Guru.connect("duckdb:///:memory:")           # an in-memory database
```

When FactLang's `connect()` is installed (FactLang 0.1.1+, the default) it opens the URL and its connector also
serves measurement. Without it, SchemaGuru's built-in fallback opens DuckDB itself: a file is
opened **read-only** (and a path that does not exist is an error, `SG106`, so a typo never
creates a database). You can also wrap an existing connection:

```python
from schemaguru.catalog import DBAPIExecutor, reader_for
guru = sg.Guru(reader=reader_for("duckdb", DBAPIExecutor(conn)), dialect="duckdb")
```

## Metadata sources
`duckdb_schemas()`, `duckdb_tables()`, `duckdb_views()`, `duckdb_columns()`,
`duckdb_constraints()`, filtered to `current_database()`. Schemas `information_schema` and
`pg_catalog` and temporary and internal objects are skipped.

## Permissions and cost
None beyond opening the database; metadata queries are in-process and free.

## What is reported
Comments, a row estimate (`estimated_size`; views have none), column types, nullability and
defaults, and primary, unique and foreign keys (`enforced=True`: DuckDB enforces them).
`bytes`, `last_altered` and incremental listing are not available.

## Quirks
- A database file named like one of its schemas (`shop.duckdb` holding schema `shop`) makes
  `shop.orders` ambiguous to DuckDB itself ("Ambiguous reference to catalog or schema"). Name the
  file differently (the examples use `warehouse.duckdb`).
- DuckDB lets a database file be opened either by one read-write process or by read-only ones,
  so a loader cannot write to the file while a Guru holds it open. Close the Guru
  (`guru.close()`, or use it in a `with` block) before loading new data.

## See also
- [[Catalog-and-Crawl]]
- [[Home]] (the three-line quickstart uses DuckDB)
- [[Contributing]] (the shop fixture is a DuckDB database)
- [[Extending-CatalogReader]]
