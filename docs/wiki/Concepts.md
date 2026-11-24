# Concepts

> Everything SchemaGuru knows is stored as a *fact*: a small statement about a table, a column or a join, with a kind, a value, a provenance and a status.
> Provenance says where a fact came from (catalog, declaration, measurement, LLM draft, human); status says how far it has been checked.
> When facts disagree, one fixed set of precedence rules decides what the agent is told, and contradictions are shown rather than hidden.

## Objects: refs and structure

- **`ObjectRef`** names a thing: `(catalog?, schema, table, column?)`. Its text form is
  `catalog.schema.table[.column]`, for example `shop.orders.status`. Names are stored exactly as
  the warehouse returns them; SchemaGuru never changes their case (case-folding can merge objects
  the warehouse treats as different). When you type a name, it is *resolved*: exact match first,
  then a case-insensitive match that must be unique. An ambiguous name is an error that lists the
  candidates, never a guess.
- **Structure** comes from the catalog: `Schema`, `Table` (kind, comment, row count, bytes,
  timestamps), `Column` (ordinal, native type, type *family* such as `integer` or `string`,
  nullability, comment) and `Constraint` (primary key, unique, foreign key, and whether the
  warehouse enforces it; many warehouses declare keys but do not enforce them).
- **`JoinRef`** names a directed join from child columns to parent columns, for example
  `shop.orders.customer_id -> shop.customers.customer_id`. Composite joins (several columns) are
  allowed. `cast_to` is set when the two sides have different type families.

## Facts

A `Fact` (in `schemaguru.model`) is a frozen, JSON-serialisable record:

| Field | Meaning |
|---|---|
| `id` | a content hash of the kind, the subject and the value fields that define identity |
| `kind` | what sort of statement it is (table below) |
| `subject` | the `ObjectRef` or `JoinRef` it is about |
| `value` | the kind-specific payload; a field that is not known is `None`, never a default |
| `provenance` | where it came from (next section) |
| `status` | how far it is checked (section after) |
| `method` | how a number was measured (`exact`, or `sampled:<method>:<fraction>:<seed>`); required for measured facts |
| `evidence` | the FactLang outcome behind it: claim text, verdict, a SQL *template*, numbers, duration, bytes |
| `source` | a short origin label, e.g. `overlay:schemaguru.yaml:12`, `llm:anthropic:claude-sonnet-5-5`, `dbt:manifest` |
| `observed_at` | when it was observed, timezone-aware UTC (naive timestamps are rejected) |

The built-in kinds (`schemaguru.model.FactKind`; the set is open, see [[Extending-FactKind]]):

| Kind | About | Payload, in short |
|---|---|---|
| `DESCRIPTION` | table or column | `text` |
| `USE_CASES` | table | `questions` the table answers |
| `ENTITY_TYPE` | table | `entity` (customer, order...) |
| `PRIMARY_KEY` | table | ordered `columns`, uniqueness numbers |
| `JOIN` | join | match rate, coverage, fan-in, cardinality, verdict ([[Join-Graph]]) |
| `VALUES` | column | declared values, observed values with counts, `case_sensitive`, `split` |
| `NULL_RATE` | column | `rate` (and `dead` for an all-NULL column) |
| `ROW_COUNT` | table | `rows` |
| `DISTINCT` | column | distinct non-null values |
| `FRESHNESS` | table | newest value of a timestamp column and its age |
| `TYPE_NOTE` | column | `stored_as` / `means`, e.g. VARCHAR holding ISO-8601 dates |
| `SOFT_DELETE` | table | a filter `predicate` that keeps live rows |
| `PII` | column | `category` (email, phone, name...) and the `detector` that found it |
| `PITFALL` | table | `rule`, `fix`, `severity` (warn or critical) |
| `GLOSSARY` | global | `term`, `definition`, `maps_to` tables |
| `METRIC` | global | a named metric definition |
| `NEVER_QUERY` | table | `reason` the agent must not use it |
| `WAYPOINT_RULE` | global | a path rule ([[Waypoint-Rules]]) |

Measured numbers are kept out of a fact's identity, so measuring a table again *replaces* the old
number instead of piling up copies. A sampled measurement and an exact one have different ids, so
a sample never overwrites an exact number, and a failed probe never overwrites the last good one.

## Provenance: where a fact came from

| Provenance | Source | Example |
|---|---|---|
| `CATALOG` | the warehouse's own metadata | column type, declared PK, catalog row-count statistics |
| `DECLARED` | imported metadata | a dbt `accepted_values` test |
| `MEASURED` | a FactLang probe that ran | `match 98.04%`, `null 10%` |
| `DRAFTED` | an LLM | a table description, a pitfall guess |
| `HUMAN` | a person, in an overlay file or by accepting a draft | `soft_delete: "deleted IS NULL OR NOT deleted"` |

## Status: how far a fact is checked

| Status | Meaning | Rendered as |
|---|---|---|
| `OBSERVED` | a measured number with no claim attached (row count, null rate, observed values) | `measured 2026-11-24` |
| `VERIFIED` | a FactLang claim passed | `verified 2026-11-24` |
| `CONTRADICTED` | a claim failed: the data disagrees | a ⚠ line |
| `UNVERIFIED` | drafted or declared, not (yet) checked | `(draft)`, `(human, unchecked)` |
| `ESTIMATED` | measured on a sample of a large table; never verified | `≈ 3.2% (sampled 1%, ...)` |
| `UNMEASURABLE` | nothing to measure, e.g. an empty table | `unmeasurable: no left keys` |
| `ERROR` | the probe failed or was over budget | an error note on that item only |
| `STALE` | observed/verified, but older than `fact_ttl_days` (14) | still shown, flagged |
| `WAIVED` | a contradiction a person acknowledged, with an expiry date | `WAIVED until 2027-01-31 (...)` |

## Precedence: which fact wins

For every `(kind, subject)` the *effective* knowledge is computed in one place,
`schemaguru.knowledge.resolve`, with these rules:

1. **Meaning** (descriptions, use cases, entity types, pitfalls, glossary): a person always beats
   an LLM. A draft is shown only with a "(draft)" marker unless its checkable part was verified.
2. **Expectations** (declared values, joins, primary keys, soft-delete predicates written by a
   person or a declaration) are never replaced by measurement. They are *checked*, and the view
   shows both: `declared values 'paid','refunded','cancelled' - observed also 'PAID' (412 rows)`.
3. **Structure** (does a column exist, what is its type): the catalog beats everything. A human
   note that disagrees is kept and shown next to the catalog's type, never deleted.
4. **Measurements** of the same kind: the newest wins; per-column facts are all retained and the
   worst verdict wins for warnings.

The result depends only on the *set* of facts, never on the order they were written, so the
same store always renders the same view.

## Unknown is not zero

A recurring rule: when something was not measured, its value is `None`, end to end. It is never
written as 0, 1.0, "OK" or "PASS". A table that was never measured renders as "unmeasured", an
empty table as "unmeasurable", a probe that failed as an error on that one item. The view's legend
ends with "Absence of a warning is not verification."

## See also

- [[Architecture]] - which layer produces which facts
- [[Measurement]] - where MEASURED facts come from
- [[Overlays]] - writing HUMAN facts
- [[Glossary]] - short definitions of every term
