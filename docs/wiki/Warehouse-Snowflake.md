# Snowflake

Reader: `snowflake`. Catalog = database, schema = schema. Identifiers are stored exactly as Snowflake returns them: upper-case unless created quoted (`"Order Items"`).

## Metadata sources
`<db>.INFORMATION_SCHEMA.SCHEMATA / TABLES / COLUMNS` (row count, bytes, `LAST_ALTERED`, clustering key, comments) and `SHOW PRIMARY KEYS`, `SHOW UNIQUE KEYS`, `SHOW IMPORTED KEYS` `IN SCHEMA "<db>"."<schema>"` for constraints.

## Permissions
A role with `USAGE` on the database and schemas and any privilege (`SELECT`, `REFERENCES`, ...) on the tables to be described: `INFORMATION_SCHEMA` and `SHOW` list only objects the role can see. `USAGE` on a warehouse for the `INFORMATION_SCHEMA` queries.

## Cost
`INFORMATION_SCHEMA` queries run on the session's **warehouse** and consume compute credits (a small/XS warehouse is enough; auto-suspend keeps it cheap). `SHOW` commands run in cloud services and need no running warehouse. No bytes are billed, so `bytes_billed` stays unknown.

## Quirks
- The driver must use `qmark` binding (`snowflake.connector.paramstyle = "qmark"`).
- Result column names come back upper-case; the reader lower-cases all keys.
- `SHOW ... KEYS` returns the whole schema; the reader filters to the requested tables. Constraints are declared, never enforced (`enforced=False`).
- A clustering key that is an expression (`LINEAR(TO_DATE(TS))`) is left unknown; plain column lists become `clustering`.
- `INFORMATION_SCHEMA.TABLES` shows only the current database (give the reader `database=` or set one on the session). Views report no row count.
- Live test: `SCHEMAGURU_LIVE_SNOWFLAKE=1` with `SCHEMAGURU_SF_{ACCOUNT,USER,PASSWORD,WAREHOUSE,DATABASE,SCHEMA}`.
