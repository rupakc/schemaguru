# Overlays

> Overlays are YAML files where people write what they know: descriptions, use cases, expected values, soft-delete rules, pitfalls, joins, path rules, glossary terms, tables to avoid and acknowledged contradictions.
> They become `HUMAN` facts that always beat LLM drafts, and their expectations are *checked* against the data, never overwritten by it.
> Loading never raises on bad data: every problem is a diagnostic with file and line, and SchemaGuru never edits a file you wrote.

## Where overlays live

A single `schemaguru.yaml`, or a directory `schemaguru/` of `*.yaml` files, in your repository.
`schemaguru init` writes a commented skeleton (and a config file):

```text
$ schemaguru init --dir demo --warehouse shop
wrote demo/schemaguru.yaml
wrote demo/schemaguru.config.yaml
next: schemaguru crawl --url <warehouse-url> && schemaguru context "<a question>"
[exit 0]
```

Load them with:

```python
diagnostics = guru.load_overlays("schemaguru.yaml", "schemaguru/")
```

**Each call replaces the previously loaded rules, chains and waivers**, so pass every overlay path
in one call. The facts are upserted into the store.

## The format

This is the full example from the design spec, and also a test fixture
([`spec_example.yaml`](https://github.com/rupakc/schemaguru/blob/main/tests/fixtures/overlays/spec_example.yaml)):

```yaml
version: 1
warehouse: shop
tables:
  shop.orders:
    description: One row per order placed on the storefront.
    use_cases: [revenue by month, average basket size]
    entity_type: order
    soft_delete: "deleted IS NULL OR NOT deleted"
    pitfalls:
      - rule: Refunds are a second row with negative amount.
        fix: Filter amount > 0 for gross revenue.
    columns:
      status:
        description: Payment state of the order.
        values: [paid, refunded, cancelled]      # an expectation: checked, never auto-edited
        values_split: ":"                        # optional
      created_at:
        type_note: ISO-8601 timestamp stored as STRING
    joins:
      - to: shop.customers.customer_id
        from: customer_id
        cardinality: N:1
chains:
  - name: order to payment provider
    because: payments are matched via the order reference, not the customer
    steps: [shop.orders.order_id -> shop.payments.order_id]
waypoints:
  - pair: [shop.events, shop.orders]
    require: "edge(shop.events.session_id->shop.sessions.session_id)"
    because: events link to orders only through sessions (measured 0% direct match)
glossary:
  - term: active customer
    definition: ordered in the last 90 days
    maps_to: [shop.orders, shop.customers]
never_query:
  - table: shop.orders_backup_2019
    reason: frozen copy, not maintained
waivers:
  - fact: "VALUES shop.orders.status"
    reason: legacy upper-case rows being migrated
    expires: 2027-01-31
```

### Keys

| Section | Keys |
|---|---|
| top level | `version` (must be `1`), `warehouse`, `tables`, `chains`, `waypoints`, `glossary`, `never_query`, `waivers`, `lineage` |
| a table | `description`, `use_cases`, `entity_type`, `soft_delete`, `pitfalls`, `columns`, `joins` |
| a column | `description`, `values`, `values_split`, `type_note` |
| a pitfall | `rule`, `fix`, `severity` (`warn` or `critical`) |
| a join | `to`, `from`, `cardinality` (`N:1`, `1:1`, `1:N`, `N:M`), `cast_to` |
| a chain | `name`, `because`, `steps` (joins `a.x -> b.y`, or `filter:`, `table:`, `note:` steps) |
| a waypoint | `pair` or `node`, `require`, `forbid`, `because` ([[Waypoint-Rules]]) |
| glossary | `term`, `definition`, `maps_to` |
| never_query | `table`, `reason` |
| waiver | `fact` (`"<KIND> <subject>"`), `reason`, `expires` |
| lineage | `table` or `column`, `upstream` (with `table`), `inputs` (with `column`), `transform`, `ignore` |

An unknown key is `SG202` with a close-match suggestion ("did you mean ..."). An empty key
(`description:` with nothing after it) is treated as absent.

## Expectations are alarms

`values`, `joins`, the table's primary key and `soft_delete` are **expectations**. SchemaGuru
checks them against measurements and shows both sides; it never edits your list to match the
data. On the shop fixture, after loading the file above and measuring, the `orders` block says
(real output, 2026-11-24, stand-in measurement backend):

```text
  ⚠ WAIVED until 2027-01-31 (legacy upper-case rows being migrated): status: declared values 'paid','refunded','cancelled' — observed also 'PAID' (412 rows)
  ⚠ status: case drift: 'paid'/'PAID' differ only in case — compare case-insensitively
```

The declared list stays `paid, refunded, cancelled`; the measured fourth value is shown next to it;
and because a person acknowledged it with a waiver, the line says so and until when. When the
waiver expires it is ignored (and reported, `SG207`), and the contradiction is a plain warning
again. A waiver without a valid `expires` date is ignored (`SG206`). Waivers always expire.

A `type_note` that disagrees with the catalog's type is kept and shown next to the measured type;
the measured type wins by being stated, never by deleting your note. In the same view:

```text
    created_at   VARCHAR  text holding ISO-8601 date text (see warning); note: ISO-8601 timestamp stored as STRING (human, unchecked); distinct 10,000 (measured 2026-11-24)
```

## Validation

- **References** are resolved against the store: exact first, then a unique case-insensitive
  match. Unknown is `SG203`, ambiguous is `SG204` with the candidates. (The spec example's
  `shop.orders_backup_2019` does not exist in the fixture, so it reports `SG203`, see below.)
- **Filters** (`soft_delete`, chain `filter:` steps) are validated with FactLang's
  `parse_filter` when available; otherwise a syntax-only check runs and info `SG503` says so. A
  bad filter is `SG205`/`SG502` and the fact is not loaded.
- **Waypoint rules** are validated and linted at load ([[Waypoint-Rules]]).
- An unreadable file or a malformed entry is `SG208`; the rest of the file still loads. Files are
  capped at 8 MiB.

Check overlays in CI with `schemaguru lint` (exit 1 on any finding). It works on a copy of the
store, so linting never writes facts. Real output on a crawled but unmeasured shop store:

```text
$ schemaguru lint
1 overlay path(s): 3 finding(s)
  ERROR SG203 schemaguru.yaml:36 cannot resolve table 'shop.orders_backup_2019' in the store
  INFO SG503  filter checked for syntax only (ansi): the installed FactLang lacks fl.parse_filter
  WARNING SG605 chain:order to payment provider chain 'order to payment provider' cannot be compiled into a waypoint rule: the graph holds no usable join shop.orders.order_id -> shop.payments.order_id in either direction
  WARNING SG604 overlay:schemaguru.yaml:28 waypoint requirement edge(shop.events.session_id->shop.sessions.session_id) matches no edge or table in the graph
[exit 1]
```

## dbt import

```bash
schemaguru import-dbt target/manifest.json
```

maps a dbt manifest onto `DECLARED` facts (for relations and columns that exist in the store):
model and column descriptions → `DESCRIPTION`; `relationships` tests → `JOIN` expectations;
`accepted_values` → `VALUES` expectations; `unique` → `PRIMARY_KEY` only when it is a table's
single `unique` test on a column that is also `not_null`. `not_null` itself is not imported. An
unreadable manifest is `SG209` and yields nothing.

## Machine-owned files

SchemaGuru never edits a human file. It writes only files it owns, marked by a first-line
header:

- `schemaguru export-drafts schemaguru/drafts.yaml` - drafts to review and copy from;
- `schemaguru accept --write-overlay schemaguru/accepted.yaml` - accepted drafts.

Writing to a path whose first line is not that writer's own header is refused.

## Validation and known limits

Duplicate keys in a mapping are `SG208` (neither entry is applied); a waiver needs a registered
fact kind (`SG208`) and a subject glob that matches the store (`SG203`, warning); glossary
`maps_to` takes tables only. A malformed entry's previous facts are retracted on the next
load: fix the entry and reload. Joins in an overlay are declarations; measured numbers come only
from measurement.

## See also

- [[Waypoint-Rules]] - the `waypoints` and `chains` sections
- [[Concepts]] - why HUMAN beats DRAFTED and expectations are never replaced
- [[Drafting]] - accepting LLM drafts into overlays
- [[CLI]] - `init`, `lint`, `import-dbt`, `accept`

### Lineage

```yaml
lineage:
  - table: analytics.revenue_daily
    upstream: [shop.orders, shop.refunds]
  - column: analytics.revenue_daily.net_revenue
    inputs: [shop.orders.amount, shop.refunds.amount]
    transform: aggregate
  - table: analytics.legacy_view
    upstream: [shop.orders]
    ignore: true
```

A human entry replaces the parsed links of its subject and level; `ignore: true` hides them (all of
them without a list, only an exactly equal list with one). Hidden facts stay in the store. An ignore
that matches no parsed link is `SG213`.
