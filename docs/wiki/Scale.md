# Scale

> The published 500,000-table / 25,000,000-column benchmark: crawl, incremental crawl, `context`, join paths and lineage, measured on one named machine.
> Each number is held to its target (pass or miss), and the nightly workflow gates regressions against a committed baseline.
> The `context` latency target is missed at this size; the reason, the measured numbers and what was fixed are below.

## The catalog

A deterministic synthetic catalog (`benchmarks/scale/generator.py`, seed 20261125), served page
by page by a fake `CatalogReader`, so nothing ever holds the whole catalog:

* **500,000 tables, 25,000,000 columns** (50 per table) in 100 schemas of 5,000 tables;
* **50,000 views** (every 10th table) with SQL definitions; a view selects from the two tables
  before it or from the previous view, so lineage chains are up to 6 views deep;
* two declared foreign keys per non-view table, to lower-numbered tables, and a comment on half
  of the tables;
* a `MEASURED` join fact for every foreign-key pair. These are **synthetic**: their match rates
  are a hash of the pair (`source: benchmark:synthetic`), not measured on any data. Every table
  ends up in one join component of about 450,000 tables.

The query phases use 200 fixed questions (`benchmarks/scale/questions.py`), 200 table pairs for
join paths and 200 view references for lineage.

## The machine

The committed result (`benchmarks/results/0.2.0.json`) was run on 2026-11-26 on an
**Apple M2 Pro, 10 cores, 32 GiB RAM**, macOS, Python 3.12.2, runner class
`local-apple-m2-pro-32g`, with nothing else running. The whole harness took 54.6 minutes of
wall time (3,273 s); the query process peaked at 5.1 GB RSS (the join graph of 900,000 edges).

## Results

| Metric | What | Result | Target |
|---|---|---|---|
| `crawl_full_s` | Full crawl into SQLite, wall time (lineage parse included) | 566.4 s | reported; regression-gated (25%) |
| `crawl_peak_rss_bytes` | Peak RSS of the crawl process | 63 MiB | ≤ 2 GiB: **pass** |
| `parser_peak_rss_bytes` | Peak RSS of the lineage parser worker | 53 MiB | reported |
| `crawl_incremental_s` | Incremental crawl, 1% of tables changed | 35.6 s | reported |
| `incremental_ratio` | Incremental / full crawl time | 0.063 | ≤ 0.10: **pass** |
| `lineage_parse_s` | View-definition lineage parse (part of the full crawl) | 123.8 s | reported; regression-gated (25%) |
| `graph_build_s` | Join graph build | 129.3 s | reported; regression-gated (25%) |
| `context_p50_ms` | `guru.context`, 200 questions, p50 | 9,327.7 ms | reported |
| `context_p95_ms` | `guru.context`, 200 questions, p95 | 28,575.6 ms | < 200 ms: **miss** |
| `path_p95_ms` | Join-path search, 200 pairs, p95 | 1,222.2 ms | reported; regression-gated (25%) |
| `lineage_upstream_p95_ms` | `guru.lineage(depth=2)` upstream, 200 refs, p95 | 1.0 ms | < 50 ms: **pass** |
| `lineage_downstream_p95_ms` | `guru.lineage(depth=2)` downstream, 200 refs, p95 | 1.2 ms | < 50 ms: **pass** |
| `store_bytes` | Store size on disk | 8.83 GiB | reported |

Four of the five hard targets pass. The crawl streams: its peak memory (63 MiB) does not grow
with the catalog. **`guru.context` p95 misses its target by two orders of magnitude** (below).
The `checks` block of the result confirms the run did the work: 500,000 tables written, 50,000
views parsed, 5,000 tables re-read incrementally, and a non-empty answer for all 200 questions,
paths and lineage references.

## Known limits

* **`context` latency at 500,000 tables (target missed).** p50 9.3 s, p95 28.6 s against a
  200 ms target. Profiled at this size, 87% of a call is the join-path search's exact A* lower
  bound: a reverse Dijkstra over every table within the 6-hop limit of the target, about one
  second each and about 18 per question (graph expansion's multi-seed searches take 62% of the
  time, the hazard scan over every pair of candidate tables 34%). In this catalog one join
  component holds about 450,000 tables, so the 6-hop neighbourhood is a large share of it. The
  fixes that fit the architecture are in 0.2.0 (below); meeting the target needs an
  architecture change, such as precomputed distance bounds (landmarks) or a hazard scan that
  does not search every candidate pair. At 2,000 tables p95 is 119 ms.
* **Join-path search p95 1.2 s** (no fixed target; regression-gated): the same per-target bound.
* The join facts are synthetic (hash-derived rates); a view's declared columns are not derived
  from its definition; `context` runs against a store with no column-level facts beyond the
  crawl. A real catalog with a sparser join graph does less path-search work.

### Fixed in 0.2.0 by this benchmark

* `context` read and re-verified every `JOIN` fact in the store on every call; it now reads a
  table's joins by subject through an index built with the join graph (every fact read is
  still id-verified). At 2,000 tables p95 went from 259 ms to 119 ms.
* The path search's lower bound walked the whole join component per target; it now stops at the
  hop limit, with identical routes. With the join read above, `context` at 20,000 tables went
  from p50 3.9 s / p95 5.3 s to p50 1.0 s / p95 1.5 s.
* The crawl re-read every lineage fact for every re-parsed view (an index the query planner
  did not use): quadratic in views, 105 s of lineage at 100,000 tables. It is now linear
  (124 s at 500,000).
* `refresh` compared the two snapshots twice; the Postgres store read every glossary term for
  every crawled page.

## Nightly regression workflow

`.github/workflows/bench.yml` runs at 03:17 UTC and on `workflow_dispatch`, never on a push or
pull request, and is not a required check. It runs the harness at **500,000 tables** on
`ubuntu-latest` (runner class `github-ubuntu-latest`, 350-minute budget), compares the result,
and uploads `bench.json` whatever the outcome.

Why 500,000: projected from the run above with 30% headroom, the store needs 9.48 GB on disk
(12.3 GB of the runner's ~14 GB, the tightest limit), the query process 5.1 GB of RAM (6.7 of
16 GiB), and 54.6 minutes here becomes about 109 minutes on a runner core assumed half as fast
(142 of 350 with headroom). This is a projection; the first dispatched run confirms it.

The comparator fails the job when a hard target is missed or a regression-gated metric is more
than 25% worse than the committed baseline for the same runner class and size. Until a GitHub
baseline (`benchmarks/results/baseline-github-ubuntu-latest.json`) is committed it exits 2
("gate inactive"), which the workflow reports as a warning, not a failure. While the `context`
target is missed, the nightly fails on that hard target: the miss stays visible.

## Reproduce

```bash
pip install -e ".[dev]"
python3 -m benchmarks.scale.harness --tables 500000 --out bench.json \
    --runner-class local-<your-machine> --workdir <a directory with 15 GB free>
python3 -m benchmarks.scale.compare bench.json --baseline-dir benchmarks/results
```

`--tables 20000` takes a few minutes. The comparator exits 0 (pass), 1 (a missed target or a
regression), 2 (no baseline for this runner class and size: hard targets checked, regressions
not) or 3 (not a result file). `pytest -m benchmark tests/unit/benchmarks` runs a 2,000-table
version.

## See also

- [[Scale-and-Cost]] - cost model, knobs and a plan for a very large warehouse
- [[Path-Search-and-Hub-Guard]] - the search whose lower bound dominates `context` at this size
- [[Scoping]] - how a question's candidate tables are chosen
