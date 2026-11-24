# Join graph and path search

`guru.find_path(a, b)` answers "how do I join these two tables?" from **measured** joins
only. It returns up to `k` routes with their join keys and weakest support, or a typed
refusal that names what blocks the route. It never returns a join it has not measured
(unless you opt in to unmeasured candidates).

```python
import schemaguru as sg

guru = sg.Guru.connect("duckdb:///warehouse.duckdb")
guru.measure(all=True)

result = guru.find_path("shop.payments", "shop.customers")
for path in result.paths:
    print(" -> ".join(t.canonical() for t in path.tables),
          "coverage", path.coverage, "provisional" if path.provisional else "")
for a, b in [("shop.orders", "shop.events"), ("shop.orders", "shop.orders")]:
    r = guru.find_path(a, b)
    print(r.refusal.value, "-", r.explanation)
```

## Edges

One edge per measured `JOIN` fact with verdict `OK` (match ≥ 95%) or `WEAK` (≥ 5%). `BROKEN`
joins and joins to missing objects are **refused, not discounted**. Unmeasured candidates
enter only with `GraphConfig(include_unmeasured=True)`, at a penalty. A malformed fact costs
one edge (`SG601`), never the graph.

## Cost of a hop

Lower is better; costs add along a route:

```text
cost(hop) = w_support * -ln(support in the traversed direction)
          + w_fanout  * ln(1 + distinct_left)
          + w_hub     * ln(1 + in_degree(arrival table))
```

Support is the match rate child → parent and the coverage parent → child. Defaults:
`w_support=1.0`, `w_fanout=0.35`, `w_hub=0.25`, `unmeasured_penalty=1.0` (used in place of
the support term when support is unknown); support 0 is never traversed. An unknown fan-out
is priced at the graph's worst known one, so unknown is never cheaper than known.

!!! note "The fan-out term is a weak proxy"
    In the system SchemaGuru's design descends from, `ln(1 + distinct_left)` correlated only
    −0.30 with the true rows per key. It stays because nothing measured replaces it yet. All
    weights are read from `GraphConfig` at call time, so you can sweep them.

## Search

* Dijkstra over **both directions** of every edge (SQL joins are symmetric), at most
  `hop_limit` (6) hops; the hop count is part of the search state, so a cheap long route
  cannot hide a short one.
* `k_paths` (3) best routes by Yen's algorithm.
* **Thin support is a preference, not a ban.** A first pass avoids any hop with support
  below `thin_support` (5%); a second pass allows them. Second-pass routes are appended after
  the first-pass ones and marked `provisional` with their narrowest support, so a thin but
  correct direct join is still offered — labelled.
* `Path.coverage` is the narrowest support over the hops, `None` when any hop is unknown;
  `Path.estimated` marks numbers from a sample.

## The hub guard

Two joins can each be right and still multiply rows together. In the shop, `orders` and
`events` both point at `accounts` many-to-one; joining `orders → accounts → events` pairs
every order with every event of its account. The join-paths part of
`guru.context("events per account and session")`:

```text
--8<-- "generated/events_paths.txt"
```

* An edge is an **attribution edge** when its fan-in (child rows per parent key) exceeds
  `attribution_fanin` (10.0).
* A **hub** is a table that only receives edges and either has an `ENTITY_TYPE` fact or is
  reached by attribution edges from at least two distinct tables — measured shape alone, so
  the guard works with no LLM and no overlay.
* The refused shape is *arriving at a hub on an attribution edge and leaving on another*
  (`HUB_PIVOT`). The hub itself is not banned: routes that start or end there are fine.

The default threshold is a judgement call. `calibrate_hubs` reports the fan-ins into each
hub and proposes the centre of the widest gap; accept it into `GraphConfig` yourself:

```python
from datetime import datetime, timezone

from schemaguru.graph import build_edges, calibrate_hubs
from schemaguru.knowledge import resolve

effective = resolve(guru.store.facts_for(None, ["JOIN"]), now=datetime.now(timezone.utc),
                    ttl_days=guru.config.fact_ttl_days)
for c in calibrate_hubs(build_edges(effective, cfg=guru.config.graph)):
    print(c.hub.canonical(), c.fan_ins, c.proposed_threshold)
```

## Typed refusals

| Refusal | Meaning |
|---|---|
| `NO_EVIDENCE` | no usable measured join touches one of the tables |
| `SAME_TABLE` | both ends are the same table |
| `NO_ROUTE` | measured joins exist, but no chain of them connects the tables within the hop limit |
| `WAYPOINT_UNSATISFIABLE` | a waypoint rule for this pair can never be met |
| `RULE_REJECTS_ALL` | routes exist, but every one breaks a waypoint rule |
| `HUB_PIVOT` | every route pivots through a hub on two attribution edges |

To say *why*, the search re-runs with exactly one mechanism relaxed; the relaxed route is
never returned as a path.

## Waypoint rules

People can constrain routes in an overlay: a *pair* rule may require and forbid, a *node*
rule may only forbid (a table as an intermediate step). Predicates are `node(t)`, `col(t.c)`,
`edge(a.x->b.y)` and `any`, matched exactly; every rule needs a `because:`.

```yaml
waypoints:
  - pair: [shop.events, shop.orders]
    require: "edge(shop.events.session_id->shop.sessions.session_id)"
    because: events link to orders only through sessions
  - node: shop.accounts
    because: accounts is an attribution hub, never a bridge
```

A malformed rule refuses the searches it governs (`SG602`, fail closed); more than 16
requirements is `SG603`; a requirement that matches nothing is a lint warning (`SG604`).
Identity *chains* (`chains:` in an overlay) compile into pair rules; a chain that cannot be
compiled is reported (`SG605`), never dropped silently.

## SQL join snippets

Each path can render a dialect-correct `FROM … JOIN … ON …` snippet (casts, composite keys,
the tables' soft-delete predicates) through FactLang's `render_join`. It is advice for the
agent; SchemaGuru never executes it. With a FactLang that lacks `render_join` the path is
shown with its join keys only (`SG501`).

```python
print(guru.handle_tool_call("find_join_path", {"from": "payments", "to": "customers"})["text"])
guru.close()
```
