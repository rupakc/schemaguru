# Cost and scale

SchemaGuru is designed for warehouses with hundreds of thousands of tables, where a full
scan is unaffordable and a schema dump does not fit in any prompt.

## Where money is spent

| Activity | Reads | Cost control |
|---|---|---|
| Crawl | system catalog only | paged, incremental; queries counted, BigQuery metadata bytes admitted against `Budget.crawl_bytes` (stops resumably with `SG110`; readers reporting no bytes are uncounted) |
| Measurement | data, through FactLang probes | priced before running; `max_bytes_per_probe` (1 GiB), `max_bytes_total` (10 GiB per run), `max_probes`, `max_seconds`; large tables sampled |
| Agent questions | the store only | never spend by default (`measure_on_demand="queue"`) |
| `check_fact` | data, one claim | off by default; when allowed, admitted through a per-period budget (`period_s`, 1 day) shared with the background drainer |
| Drafting | an LLM | `draft_budget_tokens`; token usage recorded per call |

A probe of unknown cost is charged **pessimistically** (at the per-probe limit) rather than
as free, and a probe whose bytes the warehouse does not report is recorded as unknown spend,
never as 0. Every spend lands in the store's budget ledger.

```python
import schemaguru as sg

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
cheap = sg.Budget(max_bytes_per_probe=256 << 20, max_bytes_total=1 << 30, max_seconds=30)
report = guru.measure(all=True, budget=cheap)
print(report.bytes, "bytes,", len(report.measured_tables), "tables,",
      len(report.over_budget), "over budget,", report.unknown_cost_probes, "of unknown cost")
guru.close()
```

### Warehouse-specific notes

* **BigQuery** bills `INFORMATION_SCHEMA` queries (at least 10 MB each); readers batch per
  dataset. It bills columns, not rows: key-hash sampling still scans key columns in full, and
  top values on a huge table may be over budget (`SG301`).
* **Snowflake** uses warehouse compute for metadata and probes.
* Row counts come from catalog statistics where the reader provides them; otherwise an exact
  `COUNT(*)` within budget.

## Scale

* **Crawl**: metadata-only, paged (1,000 tables per page), columns batched (200 tables per
  query), resumable from a checkpoint, incremental where the warehouse can list changes;
  memory is bounded by the page size.
* **Search**: runs inside the store's full-text index (SQLite FTS5 or Postgres `tsvector`).
* **Graph**: built from join facts once per knowledge change and cached; path search is
  Dijkstra with a hop limit.
* **Views**: an LRU cache (256 views) keyed on snapshot, question and configuration.

Measured on the developer's machine — hardware-dependent, synthetic data:

| Benchmark | Result | Where |
|---|---|---|
| lexical table search, 50,000-table store | p95 ≈ 17 ms | `tests/benchmark/test_search_latency.py` |
| lexical table search, 500,000-table store | p95 ≈ 167 ms (store build 23 s) | `tests/benchmark/test_search_latency.py` (`-m benchmark`) |
| graph expansion, 50,000 joins / 19,800 tables | median ≈ 0.48 s | `benchmarks/bench_expand.py` |

The search benchmark uses a deliberately dense vocabulary (one word matches about a third of
all tables), a worst case. Run them on your own hardware before relying on a number.
