# Configuration

> Everything is configured through one frozen object, `sg.Config`, with sensible defaults; no config file is required.
> It can be built in Python, from a YAML file (`Config.from_yaml`) or from `SCHEMAGURU_*` environment variables (`Config.from_env`), and unknown keys are an error, not a silent typo.
> This page lists every setting, its default and what it changes.

## Three ways to configure

```python
import schemaguru as sg

cfg = sg.Config(
    budget=sg.Budget(max_bytes_total=5 << 30),
    sampling=sg.SamplingConfig(llm_values="masked", per_glob={"shop.products": "raw"}),
    graph=sg.GraphConfig(hop_limit=4),
)
guru = sg.Guru.connect("duckdb:///warehouse.duckdb", config=cfg)

cfg = sg.Config.from_yaml("schemaguru.config.yaml")
cfg = sg.Config.from_env()            # SCHEMAGURU_FIELD, SCHEMAGURU_SECTION__FIELD
```

**Environment variables**: `SCHEMAGURU_<FIELD>` sets a top-level field and
`SCHEMAGURU_<SECTION>__<FIELD>` (double underscore) a nested one. Values are parsed as YAML
scalars, and tuple fields take comma-separated text:

```bash
export SCHEMAGURU_MEASURE_ON_DEMAND=background
export SCHEMAGURU_BUDGET__MAX_BYTES_TOTAL=5368709120
export SCHEMAGURU_SCOPE__SEED_K=8
export SCHEMAGURU_CRAWL__INCLUDE="shop.*,sales.*"
```

`SCHEMAGURU_URL` is the CLI's warehouse URL and is not a config field. An unknown key, a bad
value or a value outside the allowed set raises `ConfigError` (the CLI exits 2). The CLI reads
`--config FILE` if given, else the environment.

**YAML**: `schemaguru init` writes a commented `schemaguru.config.yaml` with the defaults. The
structure mirrors the dataclasses:

```yaml
budget:
  max_bytes_total: 5368709120
sampling:
  llm_values: masked
  per_glob:
    shop.products: raw
crawl:
  include: ["shop.*"]
measure_on_demand: queue
```

Changing `guru.config = new_cfg` on a live Guru is supported: the background drainer is stopped,
the scheduler is reconfigured between runs, cached views are dropped.

## `Config` (top level)

| Field | Default | Meaning |
|---|---|---|
| `budget` | `Budget()` | measurement and drafting spend caps |
| `sampling` | `SamplingConfig()` | value policy and measurement sampling |
| `graph` | `GraphConfig()` | join graph and path search |
| `scope` | `ScopeConfig()` | retrieval and views |
| `crawl` | `CrawlConfig()` | catalog crawl |
| `enum_max_distinct` | `20` | columns with at most this many distinct values get their values discovered |
| `fact_ttl_days` | `14` | measured facts older than this are `STALE` and re-measured by refresh |
| `measure_on_demand` | `"queue"` | `queue`: questions only enqueue; `background`: a daemon thread drains the queue under the budget; `block`: `context()` measures its unmeasured tables first |
| `block_timeout_s` | `10.0` | the most a `block`-mode `context()` call may spend measuring |
| `store_path` | `None` | default store file (else `.schemaguru/<name>.db`) |
| `llm_model` | `"claude-sonnet-5-5"` | default model for `schemaguru draft` |
| `token_counter` | `"chars"` | `chars` (len/4 heuristic) or `tiktoken` (extra) |
| `check_max_claims` | `200` | the most claims one `check_fact` call may carry; more is refused with `too_many_claims` before any warehouse spend (`None` disables the cap) |
| `agent_checks` | `"deny"` | `allow` offers the spending `check_fact` agent tool, admitted through the per-period budget it shares with the background drainer |

## `Budget`

| Field | Default | Meaning |
|---|---|---|
| `max_bytes_per_probe` | `1 << 30` (1 GiB) | a probe estimated above this is not run (`SG301`) |
| `max_bytes_total` | `10 << 30` (10 GiB) | per run; when spent the run stops (`SG302`) |
| `max_probes` | `None` | probe count cap |
| `max_seconds` | `None` | wall-clock cap |
| `crawl_bytes` | `None` | cap on billed metadata bytes during a crawl (where the reader reports them, BigQuery); at the cap the crawl stops resumably with `SG110` |
| `draft_budget_tokens` | `None` | token cap for a drafting run |
| `period_s` | `86400.0` | a guard shared across runs (background drainer, agent checks) refills its byte total once per period; `None` = never |

`None` means unlimited. `schemaguru doctor` warns when a byte cap is unlimited.

## `SamplingConfig`

| Field | Default | Meaning |
|---|---|---|
| `llm_values` | `"masked"` | value policy: `masked`, `raw` (opt-in) or `none` ([[PII-and-Sampling-Policy]]) |
| `per_glob` | `{}` | glob → mode overrides; narrowest glob wins, ties go to the most restrictive |
| `sample_values_per_column` | `5` | example values per column offered to drafting |
| `store_raw_samples` | `False` | in `raw` mode, whether raw values may be stored |
| `measurement` | `"auto"` | `auto` (sample large tables), `off`, `force` ([[Sampling]]) |
| `sample_threshold_bytes` | `50 << 30` | a table this large (catalog bytes) is sampled |
| `sample_threshold_rows` | `500_000_000` | used when bytes are unknown |
| `exact_key_limit` | `5_000_000` | key-hash sampling only above this many distinct keys |
| `block_fraction_floor` | `0.001` | smallest block-sample fraction |
| `block_fraction_ceiling` | `0.10` | largest block-sample fraction |
| `design_effect` | `2.0` | widening of block-sample intervals |

## `GraphConfig`

| Field | Default | Meaning |
|---|---|---|
| `w_support`, `w_fanout`, `w_hub` | `1.0`, `0.35`, `0.25` | hop-cost weights ([[Join-Graph]]) |
| `unmeasured_penalty` | `1.0` | cost instead of the support term when support is unknown |
| `hop_limit` | `6` | longest route |
| `thin_support` | `0.05` | pass-1 pruning threshold; thinner routes come back `provisional` |
| `attribution_fanin` | `10.0` | fan-in above which an edge into a hub is an attribution edge |
| `k_paths` | `3` | routes returned |
| `include_unmeasured` | `False` | let unmeasured candidates into the graph |
| `ok_threshold`, `weak_threshold` | `0.95`, `0.05` | join verdicts OK / WEAK / BROKEN |

## `ScopeConfig`

| Field | Default | Meaning |
|---|---|---|
| `seed_k` | `8` | tables retrieved for a question |
| `max_expansion_tables` | `6` | bridge tables added by graph expansion |
| `max_values_shown` | `6` | values listed per column before "+N more" |
| `cache_entries` | `256` | view cache size (0 disables it) |

## `CrawlConfig`

| Field | Default | Meaning |
|---|---|---|
| `page_size` | `1000` | tables per listing page |
| `column_batch` | `200` | tables per column/constraint query |
| `workers` | `4` | parallel metadata queries |
| `timeout_s` | `60.0` | per metadata query |
| `include` | `()` | globs to include (empty = all) |
| `exclude` | `("information_schema.*", "pg_catalog.*", "sys.*", "*.*_staging*", "*.*tmp*")` | globs to exclude; exclude wins |

## Constructor options of `Guru`

Not part of `Config`, passed to `sg.Guru(...)` or `sg.Guru.connect(...)`:

| Option | Meaning |
|---|---|
| `name` | warehouse name, used for the default store path |
| `store` | a path or a `Store` object |
| `backend` | a `MeasureBackend` (default: the FactLang backend when a connector exists) |
| `crawl` (`connect` only) | `None`: crawl if the store is empty; `True`/`False` |
| `reader`, `connector`, `dialect` (`Guru` only) | bring your own reader/connector |
| `retriever`, `token_counter` (`Guru` only) | custom retrieval and token counting |

## See also

- [[CLI]] - `--config` and environment variables in practice
- [[Scale-and-Cost]] - which settings matter on big warehouses
- [[Sampling]] and [[PII-and-Sampling-Policy]]
- [[Home]]
