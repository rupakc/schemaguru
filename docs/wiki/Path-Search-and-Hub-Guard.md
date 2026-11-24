# Path Search and Hub Guard

> `guru.find_path(a, b)` returns up to three measured join routes between two tables, cheapest first, or a typed refusal that names exactly why there is none.
> Thin joins are offered last and labelled "provisional" with their coverage; broken joins never appear; a route that pivots through a hub on two many-to-one edges is refused as a cartesian hazard.
> Each route comes with its join keys and, when FactLang can render it, a dialect-correct SQL snippet that is advice for the agent, never executed.

## Asking for a path

```python
result = guru.find_path("shop.payments", "shop.customers")    # or guru.afind_path(...)
result.paths       # tuple of Path, best first (up to GraphConfig.k_paths, default 3)
result.refusal     # None, or a sg.Refusal
result.explanation # plain-language reason when refused
```

Through the agent tool `find_join_path` the same result arrives as text. Real output on the shop
fixture (2026-11-24, stand-in measurement backend):

```text
  shop.payments → shop.orders → shop.customers · support 98.04%
    payments.order_id = orders.order_id (1:1)
    orders.customer_id = customers.customer_id (N:1)
```

Each hop lists its join keys and the measured cardinality. The `support` figure is the weakest
hop's match rate (or coverage, for a hop walked parent → child).

## How the search works

- **Dijkstra over both directions** of every edge, because a SQL join works either way and a
  parent hub has no outgoing edges.
- **The state** is `(table, waypoint-automaton state, hops, arrived on an attribution edge, hubs
  entered)`. The hop count is part of the state so a cheap long route cannot hide a short one
  under the hop limit (`hop_limit`, default 6).
- **No visited-set pruning** (that would make the search miss routes). A table is revisited only
  when a waypoint rule forces it (rendered as an aliased self-join); a hub is never entered twice.
- **k best paths** with Yen's algorithm (`k_paths`, default 3).

### Thin is a preference, not a ban

The search runs twice. **Pass 1** prunes every hop whose known support is below `thin_support`
(5%). **Pass 2** drops that pruning and must use at least one thin hop; its routes are appended
after pass 1's, de-duplicated, and marked `provisional` with their narrowest support. So a thin
but correct direct join is still offered, labelled, even when a longer well-supported route
exists. Refusing thin joins would refuse correct answers; hiding their thinness would mislead.

## The hub guard

A hub (defined on [[Join-Graph]]) is fine as a start or end. What is refused is one *shape*:
**arriving at a hub on an attribution edge and leaving it on another attribution edge.** That
shape joins two unrelated child tables through their shared parent, so every child row on one
side pairs with every child row on the other for the same key: a cartesian product.

On the shop fixture both `orders` (250 rows per account) and `events` (333 rows per account) point
at `accounts`. The search refuses to go `events → accounts → orders`. Real output:

```text
every route shop.events -> shop.orders pivots through hub shop.accounts on two attribution edges (fan-in > 10): a cartesian hazard
```

The guard bans the shape, not the table: `orders → accounts` alone, or `events → accounts`
alone, are perfectly good routes and are offered.

## Typed refusals

When there is no route, `result.refusal` is one of:

| Refusal | Meaning |
|---|---|
| `NO_EVIDENCE` | no usable measured join touches one of the tables (broken joins are refused; unmeasured candidates need `include_unmeasured`) |
| `SAME_TABLE` | both ends are the same table |
| `NO_ROUTE` | the tables are not connected by usable joins within the hop limit |
| `WAYPOINT_UNSATISFIABLE` | a waypoint rule can never be satisfied (or is malformed, which fails closed) |
| `RULE_REJECTS_ALL` | routes exist, but the waypoint rules reject every one |
| `HUB_PIVOT` | every route pivots through a hub on two attribution edges |

To name the blocker, the search re-runs with exactly one mechanism relaxed (hub guard, rules,
thin pruning...) and reports which relaxation would have found a route. **The relaxed route is
never returned as a path.**

Real outputs on the shop fixture:

```text
no chain of usable joins connects shop.ref_parent and shop.ref_child
```

(Why: the fixture declares no foreign key from `ref_child.parent_id`, and the name heuristic
looks for a table called `parent` or `parents`, not `ref_parent`. So the join was never a
candidate and never measured. A missing candidate is the most common cause of `NO_ROUTE`; declare
the join in an overlay (`joins:` under the table, see [[Overlays]]) and the next measurement run
measures it.)

On a freshly crawled store, before anything is measured (real `schemaguru path` output, exit 1):

```text
$ schemaguru path shop.orders shop.customers
no usable measured join touches shop.orders, shop.customers (broken joins are refused; unmeasured candidates need include_unmeasured)
[exit 1]
```

## SQL join snippets

Each path can render a `FROM ... JOIN ... ON ...` snippet in the warehouse's dialect, including
casts for cross-family keys, all columns of composite keys and each table's effective
soft-delete predicate, through FactLang's `fl.sql.render_join`. The snippet is advisory text for
the agent; SchemaGuru never executes it. When the installed FactLang lacks `render_join` (true
before FactLang 0.1.0), no snippet is offered (info `SG501`) and the join keys above are what the
agent gets. That is why the examples on this page show keys and no SQL.

## See also

- [[Join-Graph]] - edges, cost and the hub definition
- [[Waypoint-Rules]] - requiring or forbidding parts of a route
- [[Agent-Integration]] - the `find_join_path` tool
- [[Rendering]] - how paths appear in a context view
