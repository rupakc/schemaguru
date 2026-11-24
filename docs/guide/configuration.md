# Configuration

No configuration file is required; every default below is the documented behaviour.
`sg.Config` is a tree of frozen dataclasses. Build it in code, from a mapping, from YAML, or
from environment variables — all four give the same object.

```python
import schemaguru as sg

cfg = sg.Config(
    budget=sg.Budget(max_bytes_total=5 << 30),
    sampling=sg.SamplingConfig(llm_values="masked", per_glob={"shop.customers": "none"}),
    graph=sg.GraphConfig(attribution_fanin=20.0),
    measure_on_demand="queue",
)
same = sg.Config.from_mapping({
    "budget": {"max_bytes_total": 5 << 30},
    "sampling": {"llm_values": "masked", "per_glob": {"shop.customers": "none"}},
    "graph": {"attribution_fanin": 20.0},
})
print(cfg == same)

env = sg.Config.from_env(environ={
    "SCHEMAGURU_BUDGET__MAX_BYTES_TOTAL": "5368709120",
    "SCHEMAGURU_GRAPH__ATTRIBUTION_FANIN": "20",
    "SCHEMAGURU_MEASURE_ON_DEMAND": "background",
})
print(env.budget.max_bytes_total, env.graph.attribution_fanin, env.measure_on_demand)

guru = sg.Guru.connect("duckdb:///warehouse.duckdb", config=cfg)
guru.config = sg.Config(agent_checks="allow")    # switch at runtime (stops/restarts the drainer)
guru.close()
```

* `PREFIX_FIELD` sets a top-level field, `PREFIX_SECTION__FIELD` a nested one; values are
  parsed as YAML scalars. Tuple fields take a list or comma-separated text.
* `Config.from_yaml("schemaguru.config.yaml")` reads the same keys; `schemaguru init` writes a
  commented one. The CLI reads `--config`, else the environment.
* An unknown key or a value of the wrong type raises `ConfigError` naming the key.

## Every key

--8<-- "generated/config_reference.md"

## The settings that change behaviour most

| Setting | Effect |
|---|---|
| `measure_on_demand` | `queue` (default): questions never spend; `background`: a daemon thread drains the queue under the budget; `block`: measure scoped tables before answering, at most `block_timeout_s` |
| `agent_checks` | `deny` (default): no `check_fact` tool; `allow`: agents may run FactLang claims, each admitted through the budget |
| `sampling.llm_values` | `masked` (default), `raw` (opt-in), `none` — see [PII](pii.md) |
| `sampling.measurement` | `auto` (sample only large tables), `off`, `force` — see [Sampling](sampling.md) |
| `graph.attribution_fanin` | the hub-guard threshold; calibrate it ([Join graph](graph.md#the-hub-guard)) |
| `graph.include_unmeasured` | let unmeasured candidate joins into path search, at a penalty |
| `budget.*` | per-probe, per-run and per-period spending caps — see [Cost and scale](cost-and-scale.md) |
| `crawl.include` / `crawl.exclude` | what is indexed at all |
