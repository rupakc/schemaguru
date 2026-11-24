# Warehouse: BigQuery

> The `bigquery` reader crawls a project's datasets from region-level and dataset-level `INFORMATION_SCHEMA` views (catalog = project, schema = dataset).
> Metadata queries are billed (at least 10 MiB each), so the reader batches per dataset and reports the bytes billed to the crawl and its budget.
> Built from documented view shapes and tested against recorded fixtures; not yet validated against a live project (opt-in live test below).

Reader: `bigquery` (`schemaguru.catalog.readers.bigquery.BigQueryReader`). Catalog = project, schema = dataset. Extra: `pip install "schemaguru[bigquery]"` (`google-cloud-bigquery`).

## Metadata sources
Region views `` `region-<r>`.INFORMATION_SCHEMA.SCHEMATA `` and `TABLE_STORAGE` (row count, logical bytes, last modified), and dataset views `TABLES`, `TABLE_OPTIONS` (description), `COLUMNS`, `COLUMN_FIELD_PATHS` (column descriptions), `TABLE_CONSTRAINTS`, `KEY_COLUMN_USAGE`, `CONSTRAINT_COLUMN_USAGE`.

## Permissions
- `bigquery.jobs.create` on the billing project (role `BigQuery Job User`).
- `bigquery.tables.get` / `bigquery.tables.list` / `bigquery.datasets.get` on the datasets to read (role `BigQuery Metadata Viewer`). `TABLE_STORAGE` additionally needs `bigquery.tables.get` on the project.

## Cost
INFORMATION_SCHEMA queries are billed on-demand, **minimum 10 MiB each**. The reader batches columns and constraints **per dataset** (one query per dataset per call) and exposes `bytes_billed`, which the crawl copies into `CrawlReport.bytes_billed` and the budget ledger. If the executor offers `last_bytes_billed` (the job's `total_bytes_billed`) the reader sums it; otherwise it counts the 10 MiB minimum per query and sets `bytes_billed_is_lower_bound`. Use `budget.crawl_bytes` to cap a crawl: it is checked before each listing query, and at the cap the crawl stops between pages with `SG110` and resumes from its checkpoint next time.

## Quirks
- `region` must match the datasets' location (`us`, `eu`, `europe-west3`, ...); a region view only lists datasets in it. Pass `project=` and `region=` to the reader.
- Project, dataset and region are interpolated (they cannot be bound) and are validated against a strict character set; every value is bound with `?`.
- `column_default` is the text `NULL` when there is none; the reader maps it to unknown.
- Key constraints are declared, not enforced (`enforced=False`). A foreign key whose referenced columns cannot be resolved inside the dataset is omitted rather than guessed.
- Partitioning and clustering are not read (they would need `COLUMNS.is_partitioning_column` / `clustering_ordinal_position` per table); both stay unknown.
- Live test: `SCHEMAGURU_LIVE_BIGQUERY=1 SCHEMAGURU_BQ_PROJECT=... SCHEMAGURU_BQ_REGION=... SCHEMAGURU_BQ_DATASET=...` (ADC credentials).
- **Incremental crawls miss schema-only changes.** The incremental listing uses `TABLE_STORAGE.storage_last_modified_time`, which an `ALTER TABLE ... ADD COLUMN` may not move. Run a periodic full crawl: `sg.catalog.crawl(guru.reader, guru.store, config=guru.config.crawl, incremental=False)`.
- Measurement is billed by columns read; key-hash sampling does not reduce bytes for the key columns, so a huge table's value discovery may be over the per-probe budget (`SG301`). See [[Sampling]].

## See also
- [[Catalog-and-Crawl]]
- [[Scale-and-Cost]]
- [[Sampling]]
- [[Extending-CatalogReader]]
