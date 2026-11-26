### `Config`

| Key | Type | Default |
|---|---|---|
| `enum_max_distinct` | int | `20` |
| `fact_ttl_days` | int | `14` |
| `measure_on_demand` | Literal['queue', 'background', 'block'] | `'queue'` |
| `block_timeout_s` | float | `10.0` |
| `store_path` | str \| None | `None` |
| `llm_model` | str | `'claude-sonnet-5-5'` |
| `token_counter` | Literal['chars', 'tiktoken'] | `'chars'` |
| `agent_checks` | Literal['allow', 'deny'] | `'deny'` |
| `check_max_claims` | int \| None | `200` |

### `Budget`

| Key | Type | Default |
|---|---|---|
| `budget.max_bytes_per_probe` | int \| None | `1 GiB` |
| `budget.max_bytes_total` | int \| None | `10 GiB` |
| `budget.max_probes` | int \| None | `None` |
| `budget.max_seconds` | float \| None | `None` |
| `budget.crawl_bytes` | int \| None | `None` |
| `budget.draft_budget_tokens` | int \| None | `None` |
| `budget.period_s` | float \| None | `86400.0` |

### `SamplingConfig`

| Key | Type | Default |
|---|---|---|
| `sampling.llm_values` | Literal['masked', 'raw', 'none'] | `'masked'` |
| `sampling.per_glob` | Mapping[str, str] | `{}` |
| `sampling.sample_values_per_column` | int | `5` |
| `sampling.store_raw_samples` | bool | `False` |
| `sampling.measurement` | Literal['auto', 'off', 'force'] | `'auto'` |
| `sampling.sample_threshold_bytes` | int | `50 GiB` |
| `sampling.sample_threshold_rows` | int | `500_000_000` |
| `sampling.exact_key_limit` | int | `5_000_000` |
| `sampling.block_fraction_floor` | float | `0.001` |
| `sampling.block_fraction_ceiling` | float | `0.1` |
| `sampling.design_effect` | float | `2.0` |

### `GraphConfig`

| Key | Type | Default |
|---|---|---|
| `graph.w_support` | float | `1.0` |
| `graph.w_fanout` | float | `0.35` |
| `graph.w_hub` | float | `0.25` |
| `graph.unmeasured_penalty` | float | `1.0` |
| `graph.hop_limit` | int | `6` |
| `graph.thin_support` | float | `0.05` |
| `graph.attribution_fanin` | float | `10.0` |
| `graph.k_paths` | int | `3` |
| `graph.include_unmeasured` | bool | `False` |
| `graph.ok_threshold` | float | `0.95` |
| `graph.weak_threshold` | float | `0.05` |

### `ScopeConfig`

| Key | Type | Default |
|---|---|---|
| `scope.seed_k` | int | `8` |
| `scope.max_expansion_tables` | int | `6` |
| `scope.max_values_shown` | int | `6` |
| `scope.cache_entries` | int | `256` |

### `CrawlConfig`

| Key | Type | Default |
|---|---|---|
| `crawl.page_size` | int | `1000` |
| `crawl.column_batch` | int | `200` |
| `crawl.workers` | int | `4` |
| `crawl.timeout_s` | float | `60.0` |
| `crawl.include` | tuple[str, ...] | `()` |
| `crawl.exclude` | tuple[str, ...] | `('information_schema.*', 'pg_catalog.*', 'sys.*', '*.*_staging*', '*.*tmp*')` |
