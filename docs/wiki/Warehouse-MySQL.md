# Warehouse: MySQL / MariaDB

Dialect name: `mysql` (extra: `pip install schemaguru[mysql]`, driver `pymysql`).

## Metadata sources
`information_schema.{schemata, tables, columns, table_constraints, key_column_usage}`. A MySQL
*schema* is a database; refs carry no catalog (`table_catalog` is always `def`).

MySQL 8 accounts default to `caching_sha2_password`; `pymysql` needs the `cryptography` package
(`pip install cryptography`) to log in with one over a non-TLS connection.

## Permissions needed
`information_schema` shows only what the account may access, so grant `SELECT` (or any privilege)
on the schemas to crawl. No access to `mysql.*` or `performance_schema` is needed. Nothing is
written.

## Cost
Metadata queries only; no table data is read. Each page and each batch of up to 200 tables is
one query against `information_schema`. On servers with many thousands of tables MySQL 8 may
serve statistics from a cache (see below), which keeps the queries cheap.

## What is reported
Comments (table and column), row estimate and size, creation and last-alteration time, primary
keys, unique keys and foreign keys (`enforced=True`; InnoDB enforces them). Incremental listing
(`changed_since`) is supported.

## Quirks
- `TABLE_ROWS` is an estimate for InnoDB and may be cached for `information_schema_stats_expiry`
  seconds (default 24 h in MySQL 8), so `row_count`, `bytes` and `last_altered` can lag. It is
  unknown (`None`) for views.
- `update_time` is `NULL` for tables never written since the server started; such a table is
  always included by an incremental listing rather than assumed unchanged. Timestamps are
  converted from the session time zone to UTC in SQL.
- Column names are aliased everywhere because MySQL 8 upper-cases `information_schema` column
  names.
- MariaDB reports a column default of `NULL` as the word `NULL` and quotes string literals; the
  reader normalises both to MySQL's form. Some MariaDB versions return no `create_time` for views.
- View column nullability is reported as unknown (`None`).
- Tables are ordered and paged by binary name, so tables whose names differ only in case page
  correctly.
