# Sampling large tables

Small tables are measured **exactly**. A table too large to read in full is measured on a
*representative* sample, and every number from it says so. `LIMIT` is never a sampling
method: it returns rows in storage order and can overstate a rate several times.

## When a table is large

A table is large when catalog statistics say `bytes ≥ sample_threshold_bytes` (50 GiB), or,
with bytes unknown, `row_count ≥ sample_threshold_rows` (500M). Everything else is exact.

```python
import schemaguru as sg
from schemaguru.measure import KeyOverlapReq, ProfileColumns, choose_sample, is_large
from schemaguru.model.structure import Table

cfg = sg.SamplingConfig()
big = Table(sg.ObjectRef(None, "shop", "events_raw"), "table", row_count=40_000_000_000,
            bytes=2 << 40)
join = sg.JoinRef((sg.ObjectRef(None, "shop", "events_raw", "session_id"),),
                  (sg.ObjectRef(None, "shop", "sessions", "session_id"),))
print(is_large(big, cfg))
for req in (KeyOverlapReq(join), ProfileColumns(big.ref, ("session_id", "kind"))):
    choice = choose_sample(big, req, cfg, key_distinct=4_000_000_000, est_bytes=big.bytes,
                           max_bytes_per_probe=sg.Budget().max_bytes_per_probe, seed=42)
    print(type(req).__name__, choice.method, choice.fraction)
```

`SamplingConfig(measurement="off")` measures everything exactly (probes over budget then
become `ERROR over_budget`); `"force"` samples every table, for tests and demos.

## How

| Metric | Method | Why |
|---|---|---|
| joins (`KeyOverlap`), uniqueness, fan-out, distinct keys | **key-hash**: keep rows whose hashed key falls in a bucket range | all rows of a kept key are kept, so per-key multiplicities are exact; the same hash, seed and range on **both sides** of a join keep the same keys, so match rate and coverage are unbiased. Used only above `exact_key_limit` (5M distinct keys), aiming at about that many keys |
| null rates, pattern rates | **block** (`SYSTEM`) sampling where the dialect has it, else row-level Bernoulli | block sampling is what actually cuts bytes billed; its fraction is chosen to fit `max_bytes_per_probe` (floor 0.1%, ceiling 10%), and intervals are widened by a design effect (2.0) and labelled *approximate* |
| top values | key-hash on the primary key only | FactLang reads the input more than once for this probe (`FL405`), so block samples are refused |
| freshness, row counts, claim verification | **never sampled** | a maximum must be exact; row counts come from catalog statistics or an exact count |

Two honest costs: on column-billed warehouses (BigQuery) key-hash sampling still scans the
key columns in full — cheap, because only key columns are read — and key-hash top values on
the primary key do **not** reduce bytes scanned there, so top values on a huge table can be
over budget (`SG301`).

## What a sampled number means

* A sampled fact is `ESTIMATED`, never `VERIFIED`, and is never compared with an exact one.
* Ratios carry a Wilson 95% interval: `≈ 3.2% (sampled 1%, ±0.4%)`.
* Counts are scaled by 1/fraction and labelled; distinct counts and maxima from a sample are
  **lower bounds** (`distinct ≥ 377 (sampled 5%, lower bound)`).
* Observed values are "seen in sample"; a rare value the sample missed is not claimed absent.
* The method string is `sampled:<keyhash|system|bernoulli>:<fraction>:<seed>`; the seed is
  fixed per table and snapshot, so re-runs reproduce.

## A sample can refute, never confirm

A declared enum with an undeclared value seen in the sample is `CONTRADICTED` (the value
exists). A declared-unique key with a duplicate in a key-hash sample is `CONTRADICTED`. No
counter-example in a sample is only `ESTIMATED` ("consistent with the sample"). For exact
confirmation within budget, re-measure the tables behind sampled facts:

```py
facts = guru.store.facts_for([guru.table_ref("events_raw")], None)
exact = guru.verify(facts, exact=True)   # re-measures the sampled tables exactly, if affordable
```

`examples/large_table_sampling.py` forces sampling on the small shop tables at 5% so you
can see the rendering, intervals and the lower bounds side by side with the true numbers.
