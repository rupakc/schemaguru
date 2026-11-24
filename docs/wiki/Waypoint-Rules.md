# Waypoint Rules

> Waypoint rules are human-written constraints on join routes: "between these two tables, the route must use this join", or "never route through this table".
> They live in overlay YAML, need a `because:` reason, are compiled into a small automaton the path search follows, and fail closed: a broken rule refuses the searches it governs instead of being ignored.
> Identity chains (join recipes) are compiled into the same kind of rule.

## Why rules exist

Measurement tells SchemaGuru which joins *match*. It cannot tell it which joins are *meant*. Two
tables may be connected by several well-matching routes and only one of them answers the
business question. A waypoint rule records that knowledge where a reviewer can see it.

## Writing rules

In an overlay file (see [[Overlays]]):

```yaml
waypoints:
  - pair: [shop.events, shop.orders]
    require: "edge(shop.events.session_id->shop.sessions.session_id)"
    because: events link to orders only through sessions (measured 0% direct match)

  - node: shop.orders_archive
    forbid: [shop.orders_backup_2019]
    because: archive tables are frozen copies; never route through them
```

### Predicates

Matched exactly (no globbing, no case folding):

| Predicate | Matches |
|---|---|
| `node(t)` | the table `t` |
| `col(t.c)` | any edge with column `t.c` on either side |
| `edge(a.x->b.y)` | the edge declared `a.x -> b.y` (composite: `a.x, a.y->b.x, b.y`), in the orientation the graph declares it, whichever way it is walked |
| `any` | every element |

A bare table name (`shop.orders`) means `node(shop.orders)`. A term may list alternatives with
`|` (`edge(a.x->b.y) | edge(b.y->a.x)`): a requirement is met by any alternative; a prohibition
forbids each alternative.

### Two scopes

- **Pair scope** (`pair: [a, b]`) governs searches between `a` and `b`, in either direction. It may
  `require` and `forbid`.
- **Node scope** (`node: x`) governs every search and may only forbid, and only tables. It forbids
  routes through `x` and through every table in its `forbid:` list, **as intermediates**: each
  prohibition is withheld for a search where the forbidden table is an endpoint. `any`, `col` and
  `edge` in a node rule are an error (`SG602`).

A rule needs a non-empty `because:` and may carry at most 16 requirements (`SG603`). A route
satisfies a pair rule when it meets every requirement somewhere along the way (`any* p any*`) and
touches no prohibited element.

## What happens at load time

- Every rule is validated when overlays load; a malformed rule is reported (`SG602`/`SG603`) and
  kept, so the searches it governs **refuse** with `WAYPOINT_UNSATISFIABLE` until it is fixed. A
  broken safety rule never silently disappears.
- Whether a rule can be satisfied at all is decided offline, from the predicates alone. A rule
  that is satisfiable in principle but not in this graph surfaces as `RULE_REJECTS_ALL`.
- A prohibition or requirement that matches nothing in the graph is a lint warning (`SG604`):
  usually a typo, or a join that has not been measured yet.
- **Each `load_overlays` call replaces the previous rule set**, so pass every overlay path in one
  call: `guru.load_overlays("schemaguru.yaml", "schemaguru/")`.

## Worked example

Rules file `rules.yaml`:

```yaml
version: 1
warehouse: shop
waypoints:
  - node: shop.orders
    because: orders is being rebuilt this week; route around it
  - pair: [shop.order_items, shop.customers]
    require: "edge(shop.order_items.order_id->shop.orders.order_id)"
    because: line items reach customers only through their order
```

Real output on the measured shop fixture (2026-11-24, stand-in measurement backend), followed by
loading a file whose node rule wrongly tries to `require`:

```text
>>> guru.load_overlays('rules.yaml')
[]
>>> guru.find_path('shop.payments', 'shop.customers')
PathResult(paths=(), refusal=<Refusal.RULE_REJECTS_ALL: 'RULE_REJECTS_ALL'>, explanation='waypoint rules reject every route shop.payments -> shop.customers; without them a route exists')
>>> guru.find_path('shop.order_items', 'shop.customers')
PathResult(paths=(), refusal=<Refusal.WAYPOINT_UNSATISFIABLE: 'WAYPOINT_UNSATISFIABLE'>, explanation='the waypoint rules for this pair can never be satisfied: requirement edge(shop.order_items.order_id->shop.orders.order_id) always meets a prohibition')
>>> guru.find_path('shop.orders', 'shop.customers')
PathResult(paths=(Path(hops=(Hop(shop.orders -> shop.customers),), cost=2.6180776587937986, coverage=0.9803921568627451, provisional=False, estimated=False, revisits=False),), refusal=None, explanation='')
>>> guru.load_overlays('badrule.yaml')
Diagnostic(code='SG602', severity=<Severity.ERROR: 'ERROR'>, message='a node waypoint rule may only forbid, not require', location='overlay:badrule.yaml:4', hint='the searches this rule governs are refused until it is fixed')
>>> guru.find_path('shop.payments', 'shop.customers')
PathResult(paths=(), refusal=<Refusal.WAYPOINT_UNSATISFIABLE: 'WAYPOINT_UNSATISFIABLE'>, explanation='the waypoint rules for this pair can never be satisfied: SG602 at overlay:badrule.yaml:4: a node waypoint rule may only forbid, not require')
```

What it shows:

1. `payments → customers` normally goes through `orders`; the node rule forbids `orders` as an
   intermediate, so every route is rejected (`RULE_REJECTS_ALL`, and the explanation says a route
   exists without the rules).
2. The pair rule requires an edge that touches `orders`, while the node rule forbids `orders`:
   the two can never both hold (`WAYPOINT_UNSATISFIABLE`, decided without searching).
3. `orders → customers` is fine: a node prohibition is withheld when the node is an endpoint.
4. The second `load_overlays` call replaced the first rule set, and its malformed rule fails
   closed: the search it governs is refused and names the diagnostic.

## Identity chains

A chain is a named join recipe:

```yaml
chains:
  - name: order to payment provider
    because: payments are matched via the order reference, not the customer
    steps: [shop.orders.order_id -> shop.payments.order_id]
```

A *linear* chain (each join shares one table with the next, no table twice) is compiled into a
pair rule between its first and last table, with one requirement per join, oriented the way the
measured graph declares that join. Filter and note steps do not constrain the route. A chain
that cannot be compiled (no joins, not linear, or a join the graph does not hold) is reported as
an `SG605` warning and not applied, never dropped silently. Real `schemaguru lint` output on a
store that has not been measured yet:

```text
$ schemaguru lint
1 overlay path(s): 3 finding(s)
  ERROR SG203 schemaguru.yaml:36 cannot resolve table 'shop.orders_backup_2019' in the store
  INFO SG503  filter checked for syntax only (ansi): the installed FactLang lacks fl.parse_filter
  WARNING SG605 chain:order to payment provider chain 'order to payment provider' cannot be compiled into a waypoint rule: the graph holds no usable join shop.orders.order_id -> shop.payments.order_id in either direction
  WARNING SG604 overlay:schemaguru.yaml:28 waypoint requirement edge(shop.events.session_id->shop.sessions.session_id) matches no edge or table in the graph
[exit 1]
```

## See also

- [[Path-Search-and-Hub-Guard]] - the search the rules constrain
- [[Overlays]] - the file format
- [[Diagnostics]] - `SG602` to `SG605`
- [[Join-Graph]]
