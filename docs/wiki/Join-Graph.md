# Join Graph

> The join graph has one edge per measured join that is usable: verdict OK (match ≥ 95%) or WEAK (match ≥ 5%); broken joins are refused, not discounted.
> Each hop has a cost built from how well the join is supported in the direction travelled, how wide the child key is, and how many joins arrive at the target table.
> Path search, the hub guard and scoping all use this one graph, rebuilt automatically whenever the knowledge changes.

## Edges

An edge comes from the *effective* `JOIN` knowledge of a join subject (after precedence, see
[[Concepts]]):

- **Measured, verdict `OK` or `WEAK`** → an edge carrying the measured numbers.
- **Measured, verdict `BROKEN`** (match below 5%) → no edge. A broken join is refused, never
  offered at a discount: on the shop fixture `broken_child.parent_id -> ref_parent.id` matches 1%,
  and the graph simply does not hold it.
- **A probe error or nothing to measure** → no edge (there is no measurement of the join).
- **A candidate never measured** → an edge only if `GraphConfig.include_unmeasured=True`
  (default False), marked unmeasured and paying `unmeasured_penalty` instead of a support term.
- `NaN` or infinite numbers are treated as unknown. A malformed fact costs exactly one edge and
  reports `SG601`.

The graph is built once per write generation and per day, so it always reflects what the store
holds now.

## Direction and support

Every edge points child → parent (`orders.customer_id -> customers.customer_id`). SQL joins are
symmetric, so the search may traverse an edge both ways, but the *support* differs:

- child → parent: `support = match_rate` (how many child keys find a parent),
- parent → child: `support = coverage` (how many parent keys have a child).

`Path.coverage` is the narrowest support over a route's hops; it is `None` if any hop's support is
unknown, never a default.

## Hop cost

```text
cost(hop) = W_SUPPORT * -ln(support(direction))
          + W_FANOUT  * ln(1 + distinct_left)
          + W_HUB     * ln(1 + in_degree(arrival_table))
```

Lower is better; costs add up along a route. Defaults (all in `sg.GraphConfig`, read at call time
so a weight sweep is a fair experiment):

| Weight | Default | Effect |
|---|---|---|
| `w_support` | 1.0 | a well-supported hop is cheap; `support == 0` costs infinity (never taken) |
| `w_fanout` | 0.35 | a wide child key costs a little more |
| `w_hub` | 0.25 | arriving at a table that many joins point into costs a little more |
| `unmeasured_penalty` | 1.0 | replaces the support term when support is unknown |

Unknown fan-out (`distinct_left` not measured) is priced at the graph's *worst* known fan-out
term, so unknown is never cheaper than known.

**An honest note on the fan-out term.** It is a weak proxy: in the project these ideas come from,
it correlated only −0.30 with the true rows-per-key multiplication. It stays because nothing
measured replaces it yet. The hub guard (next page) is what actually stops row multiplication.

## Hubs

A **hub** is a sink of the directed edge set (joins point into it, none leave it) that either has
an `ENTITY_TYPE` fact or is reached by at least two *attribution edges* from different tables. An
in-edge is an attribution edge when its measured `fan_in` (child rows per key) is above
`attribution_fanin` (default 10.0). The measured-shape rule means the hub guard works with no LLM
and no overlays at all. See [[Path-Search-and-Hub-Guard]].

### Calibrating the threshold

The default 10.0 is a starting point. `sg.graph.calibrate_hubs(edges)` lists, for every sink, the
measured fan-in of each edge into it and proposes a threshold at the centre of the widest gap
between consecutive fan-ins (on a log scale). It changes nothing; you copy a proposal into
`GraphConfig(attribution_fanin=...)` if you agree. Real output on the shop fixture (2026-11-24,
stand-in measurement backend):

```text
HubCalibration(hub=ObjectRef(catalog=None, schema='shop', table='accounts', column=None), fan_ins=[250.0, 333.3333333333333], proposed_threshold=288.67513459481285, gap=(250.0, 333.3333333333333))
HubCalibration(hub=ObjectRef(catalog=None, schema='shop', table='customers', column=None), fan_ins=[9.803921568627452], proposed_threshold=None, gap=None)
HubCalibration(hub=ObjectRef(catalog=None, schema='shop', table='products', column=None), fan_ins=[100.0], proposed_threshold=None, gap=None)
HubCalibration(hub=ObjectRef(catalog=None, schema='shop', table='ref_parent', column=None), fan_ins=[1.0, 1.0], proposed_threshold=None, gap=None)
HubCalibration(hub=ObjectRef(catalog=None, schema='shop', table='sessions', column=None), fan_ins=[4.0], proposed_threshold=None, gap=None)
```

`accounts` is the hub with two attribution edges (orders at 250 rows per account, events at 333).
The proposal for it, about 289, only says where the gap *between those two edges* lies; a sensible
threshold separates attribution edges from ordinary ones across the whole graph, which here means
anything between about 10 and 100. With only a handful of hubs, read the proposals as a picture of
your data, not an answer.

## Using the graph directly

```python
from schemaguru.graph import build_edges, Graph, Hubs, entity_tables, find_paths
from schemaguru.knowledge.resolve import resolve
```

Most users never need this: `guru.find_path(a, b)` and `guru.context(...)` build and cache the
graph for you.

## See also

- [[Path-Search-and-Hub-Guard]] - how routes are found and refused
- [[Waypoint-Rules]] - human constraints on routes
- [[Measurement]] - where the join numbers come from
- [[Configuration]] - `GraphConfig`
