# Oracle

Reader: `oracle`. Schema = owner; refs carry no catalog. Requires Oracle Database 12.1 or later (`FETCH FIRST`, `ALL_USERS.ORACLE_MAINTAINED`).

## Metadata sources
`ALL_USERS`, `ALL_TABLES`, `ALL_VIEWS`, `ALL_MVIEWS`, `ALL_TAB_COMMENTS`, `ALL_PART_TABLES`, `ALL_TAB_COLUMNS`, `ALL_COL_COMMENTS`, `ALL_CONSTRAINTS`, `ALL_CONS_COLUMNS`.

## Permissions
Any user: `ALL_*` shows objects the user owns or has been granted access to. To describe another schema, grant `SELECT` (or `READ`) on its objects, or `SELECT ANY DICTIONARY` / the `SELECT_CATALOG_ROLE` role for the dictionary views to show everything.

## Cost
Dictionary queries only; no extra licensing beyond the base database.

## Quirks
- Names are upper-case unless created quoted (`"Order Items"`); the reader keeps them as returned.
- `NUM_ROWS` is the optimizer-statistics estimate and is unknown until statistics are gathered. Table bytes are not read (they would need `DBA_SEGMENTS`).
- `DATE` includes a time part, so it maps to the `timestamp` family. Dictionary timestamps are zone-less, so `created_at` / `last_altered` stay unknown and `incremental` is not offered.
- Materialized view container tables, nested tables, IOT overflow segments and recycle-bin (`DROPPED = 'YES'`) tables are excluded.
- `ENABLED` / `DISABLED` constraint status is reported as `enforced`.
- Bind style: `oracledb` numeric (`:1`); the executor converts `?`.
- Live test: `SCHEMAGURU_LIVE_ORACLE=1` with `SCHEMAGURU_ORA_{DSN,USER,PASSWORD,SCHEMA}`.
