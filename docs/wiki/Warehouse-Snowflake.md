# Warehouse: Snowflake

> The `snowflake` reader crawls a database's schemas from `INFORMATION_SCHEMA` and takes keys from `SHOW ... KEYS` (catalog = database).
> Its queries are metadata-only but run on a virtual warehouse, so they cost a little compute; identifiers are kept upper-case as Snowflake stores them.
> Built from documented view shapes and tested against recorded fixtures; not yet validated against a live account.

Reader: `snowflake`. Catalog = database, schema = schema. Identifiers are stored exactly as Snowflake returns them: upper-case unless created quoted (`"Order Items"`). Extra: `pip install "schemaguru[snowflake]"` (`snowflake-connector-python`).

## Metadata sources
`<db>.INFORMATION_SCHEMA.SCHEMATA / TABLES / COLUMNS` (row count, bytes, `LAST_ALTERED`, clustering key, comments) and `SHOW PRIMARY KEYS`, `SHOW UNIQUE KEYS`, `SHOW IMPORTED KEYS` `IN SCHEMA "<db>"."<schema>"` for constraints.

## Permissions
A role with `USAGE` on the database and schemas and any privilege (`SELECT`, `REFERENCES`, ...) on the tables to be described: `INFORMATION_SCHEMA` and `SHOW` list only objects the role can see. `USAGE` on a warehouse for the `INFORMATION_SCHEMA` queries.

## Cost
`INFORMATION_SCHEMA` queries run on the session's **warehouse** and consume compute credits (a small/XS warehouse is enough; auto-suspend keeps it cheap). `SHOW` commands run in cloud services and need no running warehouse. No bytes are billed, so `bytes_billed` stays unknown.

## Quirks
- The driver must use `qmark` binding (`snowflake.connector.paramstyle = "qmark"`).
- Result column names come back upper-case; the reader lower-cases all keys.
- `SHOW ... KEYS` returns the whole schema; the reader filters to the requested tables. `SHOW` returns at most 10,000 rows, so an answer that reaches that count may be cut off: the reader then asks per table (`SHOW ... KEYS IN TABLE`) instead of trusting it. Constraints are declared, never enforced (`enforced=False`).
- `clustering` holds each clustering key expression verbatim: `LINEAR(A, B)` gives `("A", "B")`, `LINEAR(TO_DATE(TS))` gives `("TO_DATE(TS)",)`. A key that cannot be split unambiguously (unbalanced parentheses or quotes) is left unknown.
- Metadata the reader cannot interpret (no database, a `SHOW` row without its key columns or sequence) raises `SG102`; the crawl records it and continues.
- `INFORMATION_SCHEMA.TABLES` shows only the current database (give the reader `database=` or set one on the session). Views report no row count.
- Live test: `SCHEMAGURU_LIVE_SNOWFLAKE=1` with `SCHEMAGURU_SF_{ACCOUNT,USER,PASSWORD,WAREHOUSE,DATABASE,SCHEMA}`.

## See also
- [[Catalog-and-Crawl]]
- [[Scale-and-Cost]]
- [[Concepts]] (names are never case-folded)
- [[Extending-CatalogReader]]
