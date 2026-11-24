# Extending: JoinCandidateSource

> A join candidate source would let you plug in your own supply of "joins worth measuring", for example from a lineage tool or a query log.
> **It is not a plug-in point in 0.1.0 yet:** the spec lists it (section 21), but the candidate list is built by a fixed function from five built-in origins.
> This page explains what exists today and how to feed your own candidates through it.

## What exists today

`schemaguru.measure.join_candidates(store, table, max_per_column=5, same_catalog_only=True)`
returns the candidate joins with `table` on the child side, labelled by origin, in priority order
and de-duplicated (the first origin wins):

| Origin | Where it comes from |
|---|---|
| `constraint` | declared foreign keys in the catalog |
| `overlay` | `joins:` entries in your overlay YAML |
| `dbt` | dbt `relationships` tests (`schemaguru import-dbt`) |
| `heuristic` | `x_id` / `xId` / `x_fk` → a table `x`/`xs` with a single-column key of a compatible type; or a column named like another table's single-column PK in the same schema |
| `llm` | joins suggested in an LLM draft |

It only reads the store and never writes a fact; a candidate becomes a `JOIN` fact only when it
is measured. Heuristic and LLM guesses that were never measured leave nothing behind.

## Feeding your own candidates now

Because overlays are an origin, any external source of join knowledge can be turned into an
overlay file that SchemaGuru loads:

```python
import yaml

pairs = my_lineage_tool.joins()       # e.g. [("shop.orders", "customer_id", "shop.customers.customer_id")]
tables = {}
for table, column, target in pairs:
    tables.setdefault(table, {"joins": []})["joins"].append({"from": column, "to": target})
with open("schemaguru/lineage_joins.yaml", "w") as fh:
    yaml.safe_dump({"version": 1, "warehouse": "shop", "tables": tables}, fh, sort_keys=True)

guru.load_overlays("schemaguru.yaml", "schemaguru/")    # all overlay paths in one call
guru.measure(all=True)                                   # the new candidates are measured
```

These joins are then *human expectations* as well as candidates: they are measured, shown with
their numbers, and a declared join that turns out broken is shown as a contradiction, not
silently dropped. The generated file should be one you own (regenerate it, do not hand-edit it).

## What the plug-in will look like

The specification plans a `JoinCandidateSource` protocol discovered through an entry-point group
(`schemaguru.<point>`), returning labelled `JoinRef` candidates for a table, ranked after the
built-in origins. When it lands this page will describe it; until then, use the overlay route.

## Test template

```python
from schemaguru.measure import join_candidates

def test_overlay_join_becomes_candidate(guru):
    guru.load_overlays("schemaguru/lineage_joins.yaml")
    labels = dict((j.canonical(), origin)
                  for j, origin in join_candidates(guru.store, guru.table_ref("shop.orders")))
    assert labels["shop.orders.customer_id -> shop.customers.customer_id"] in ("constraint", "overlay")
```

## See also

- [[Measurement]] - candidates in the measurement plan
- [[Overlays]]
- [[Join-Graph]]
- [[Contributing]]
