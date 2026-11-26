# Scale and Cost

> SchemaGuru is built for warehouses with hundreds of thousands of tables: the crawl is metadata-only, paged and constant-memory, search runs inside the store's index, and measurement is lazy and budgeted.
> Warehouse money is spent only by crawling (metadata queries) and measuring (read-only probes), both counted in a ledger, and measurement only when you ask for it.
> This page gives the numbers we have, the knobs that matter, and a plan for a very large warehouse.

## What costs money

| Activity | What it reads | Who triggers it | How it is capped |
|---|---|---|---|
| crawl | system views only (no data) | `crawl()`, `connect()` on an empty store, `refresh()` | `crawl.include/exclude`, `budget.crawl_bytes` (BigQuery) |
| measurement | the measured columns of the measured tables | you: `measure()`, `refresh()`, `worker`, or an opt-in background/block mode | `Budget` per probe, per run, per period; [[Sampling]] for large tables |
| `check_fact` agent tool | what the claim needs | an agent, only with `agent_checks="allow"` (default `deny`) | the per-period budget shared with the background drainer |
| drafting | nothing in the warehouse; LLM tokens | you: `draft()` | `draft_budget_tokens` |
| `context()`, search, paths, the other tools | the local store | anyone | free |

**An agent question never spends warehouse money by itself** (`measure_on_demand="queue"`, the
default). Every spend is written to the store's ledger; `schemaguru report` sums it.

### Per-warehouse notes

- **BigQuery** bills `INFORMATION_SCHEMA` queries at a minimum of 10 MiB each. The reader batches
  per dataset; the crawl reports `bytes_billed` (a lower bound when the job's real bytes are not
  available). Measurement is billed by columns read: sampling of the *rows* of a column does not
  reduce bytes for key-hash samples (see [[Sampling]]).
- **Snowflake, Databricks**: metadata queries use warehouse compute; measurement is compute time.
- **Self-hosted engines** (PostgreSQL, MySQL, ClickHouse, SQL Server, Trino, DuckDB): no per-byte
  bill, but probes still use I/O; run measurement off-peak or against a replica.

The `Warehouse-*` pages have the details for each.

## Measured numbers

These are from single local runs on a developer laptop (macOS, Python 3.12) on
2026-11-24 while other test suites were running, so read them as orders of magnitude, not as a
benchmark result:

| Benchmark | Result |
|---|---|
| `benchmarks/bench_crawl.py 100000` (synthetic reader, 10,000 tables per schema, into SQLite) | 100,000 tables in 10.4 s, about 9,600 tables/s, **peak memory 45 MB**, 1,102 metadata queries |
| `benchmarks/bench_expand.py` (graph expansion, `seed_k=8`, 50,000 edges, ~20,000 tables) | graph build 4.5 s; expansion median 618 ms, first call 1,056 ms |
| `tests/benchmark/test_search_latency.py` (lexical search over a 50,000-table synthetic catalog) | p95 under 200 ms (asserted; runs with the unit suite) |

The full-size 500,000-table / 25,000,000-column run (crawl, incremental crawl, `context`, join
paths and lineage), its targets with pass or miss, the nightly regression workflow and the known
limits are on the [[Scale]] page. The crawl's memory is bounded by page size and the number of
schemas, not by table count, which is why the peak above stays flat as the catalog grows.

## The knobs that matter at scale

| Setting | Why |
|---|---|
| `crawl.include` / `crawl.exclude` | do not index what no agent should see; excluded objects are never stored |
| `crawl.page_size`, `column_batch`, `workers`, `timeout_s` | throughput versus load on the catalog |
| `budget.max_bytes_per_probe`, `max_bytes_total`, `period_s` | the hard ceiling on measurement spend |
| `sampling.sample_threshold_bytes` / `_rows` | when a table counts as large |
| `measure_on_demand` | `queue` (spend only when you drain), `background` (bounded by `period_s`), `block` (bounded by `block_timeout_s`) |
| `enum_max_distinct` | how many columns get a second, value-discovery probe |
| `scope.seed_k`, `max_expansion_tables` | how much of the graph a question pulls in |

## A plan for a very large warehouse

1. **Crawl with a scope.** `guru.crawl(include=["analytics.*", "sales.*"])`. On BigQuery, set
   `budget.crawl_bytes` and check `CrawlReport.bytes_billed`. Later crawls are incremental where
   the reader supports it.
2. **Write the first overlays** for the tables agents use most: descriptions, use cases,
   soft-delete rules ([[Overlays]]). Retrieval depends on them.
3. **Measure the hot set within a budget.** `guru.measure(all=True, budget=sg.Budget(max_bytes_total=...))`
   ranks by feedback, declared foreign keys and size, and stops cleanly when the budget is spent;
   the remainder stays queued. Large tables are sampled automatically.
4. **Let usage drive the rest.** With the default queue mode, every table an agent's question
   touches is queued; run `schemaguru worker` (or a scheduled `schemaguru measure`) to drain it
   under the budget, or opt into `measure_on_demand="background"` with a `period_s` budget.
5. **Refresh nightly** and fail CI on review-class drift ([[Refresh-and-Drift]]).

## See also

- [[Scale]] - the published 500,000-table results and how to reproduce them
- [[Measurement]] - budgets in detail
- [[Sampling]] - large-table measurement
- [[Catalog-and-Crawl]] - crawl mechanics
- [[Configuration]]
