# SchemaGuru — Design Specification

Status: APPROVED 2026-11-24 (owner; sampling of large tables added at approval, §8.6) · Date: 2026-11-24 · Process: Elephant-Goldfish (§24)

This document is the single source of truth for building SchemaGuru v1. A builder
with no access to the conversation that produced it must be able to implement the
library from this document alone. Decisions made by the owner are stated plainly;
decisions made by the spec author are marked **[call]** and may be revisited.

---

## 1. Intent

An AI agent that queries a data warehouse is only as good as what it knows about the
warehouse: which tables exist, what their columns mean, which columns really join,
what values an enum actually takes, which rows are soft-deleted, which path between
two tables multiplies rows. Today that knowledge is either missing (the agent guesses
from names) or hand-written prose that silently goes stale. A wrong guess does not
error — it returns a plausible, wrong number.

**SchemaGuru** is an open-source Python library that *discovers, measures, derives and
serves* that knowledge — the **schema intelligence** of a warehouse — and injects the
right slice of it into any agent's context:

1. **Discover** the catalog (schemas, tables, columns, types, keys, comments, stats)
   from any major warehouse, at the scale of hundreds of thousands of tables.
2. **Measure** facts with exact, read-only, cost-bounded probes (join match rates in
   both directions, fan-out, uniqueness, null rates, real enum values, freshness),
   expressed and verified as **FactLang** claims.
3. **Derive** a join graph and safe join paths (hub/cartesian guards, typed refusals).
4. **Draft** meaning (descriptions, use cases, pitfalls, entity types) with an LLM,
   and let people **augment or override** it in reviewable YAML files.
5. **Serve** a question-scoped, token-budgeted view of all of it to an agent — as
   prompt text, as tool calls, through an MCP server, or through framework adapters.
6. **Refresh** it as the warehouse drifts, reporting what changed and what went stale.

The library is MIT-licensed, generic, and contains no reference to any company,
product, dataset or table name of the project it was conceived in.

### 1.1 Lineage

SchemaGuru is a from-scratch reimplementation of the ideas of an internal predecessor
("the predecessor schema layer"). No code is copied. The concepts carried forward —
each exists because its absence caused a measured wrong answer in production — are
listed in §25 (Design Decisions) with their reasons; the most load-bearing are:

1. **"Not measured" is never 0, never 1.0, never PASS.** Unknown is `None`, end to end.
2. **Every number carries its method**; numbers measured by different methods are
   never compared.
3. **Exact by default; sampling only where a table is too large, and always labelled.**
   Small tables are measured exactly. Large tables (terabytes) are measured on a
   *representative* sample (owner decision, §8.6) — key-hash or block sampling, never
   `LIMIT` (which returns rows in storage order and once overstated a rate 3.3×). A
   sampled number is an `ESTIMATED` fact carrying its method, fraction and interval; it
   is never compared with an exact number and never makes a fact `VERIFIED`.
4. **Direction matters**: a join's forward match rate and its reverse coverage are
   different facts (1.0 forward vs 0.0002 backward was observed).
5. **Narrow is not wrong**: thin joins are returned *labelled with their coverage*;
   only broken joins are refused.
6. **Shape-based hub guard**: arriving at an entity hub on a many-to-one attribution
   edge and leaving on another is a cartesian hazard (a 213× blow-up was observed);
   the hub itself is not banned.
7. **Never infer a column's role from its name alone** (a frequency enum named
   `...email...` was hashed as PII; a role column was hashed as identity). Use
   values, shapes and cardinality.
8. **Human expectations are never auto-overwritten**: a declared enum or join is the
   alarm; measurement contradicting it is *drift*, reported, not silently "fixed".
9. **The measured type wins by being stated**, never by deleting a human note.
10. **Never mutate knowledge on the agent's query path** (it changes context mid-query
    and is lost on restart). Feedback is queued; writes happen in refresh.
11. **Judge relative to a baseline**: data moves between snapshots; a contradiction
    shared by the previous snapshot is DRIFT, not a regression of the new one.
12. **Byte-deterministic artifacts and views**: identical inputs render identical text
    (agent behaviour changed when only table order changed). Views are fingerprinted.
13. **Consequence-ordered rendering**: truncation drops confirmations, never warnings;
    "silence is not verification" is stated whenever warnings appear.

---

## 2. Goals, non-goals, anti-goals

### Goals
- **G1 Easy to use.** Three lines from install to an agent-ready context:
  ```python
  import schemaguru as sg
  guru = sg.Guru.connect("duckdb:///shop.duckdb")      # or any supported URL/connector
  print(guru.context("monthly revenue by customer segment"))
  ```
  No config file required; sensible defaults everywhere.
- **G2 Easy to integrate.** Works inside an existing project without restructuring it:
  accepts an existing DB-API connection / SQLAlchemy engine / BigQuery client / a
  plain `execute(sql)` callable; emits plain strings, JSON, tool specs, MCP.
- **G3 Easy to extend.** Every component is a documented extension point with a
  worked example and a test template (§21).
- **G4 Many warehouses.** BigQuery, Snowflake, Redshift, Databricks, PostgreSQL,
  DuckDB, ClickHouse, SQL Server / Azure Synapse, Oracle, Trino (owner-approved list,
  aligned with FactLang's native dialects), plus MySQL/MariaDB because FactLang
  supports it **[call]**; anything else via a user-written `CatalogReader`.
- **G5 Scale.** Indexes warehouses with ≥ 500,000 tables / 25M columns using
  metadata-only, paged, resumable, incremental crawls; serves a question-scoped view
  in < 200 ms p95 from a local store at that size **[call: targets]**.
- **G6 Truthful.** Every fact shown to an agent states its provenance (declared /
  measured / drafted / human) and, for measured facts, the number, method and date.
- **G7 Robust.** No component raises on bad input data; one bad table, probe, draft
  or overlay costs that item, never the run.
- **G8 Agent-native.** Python API, LLM tool definitions, an MCP server, adapters for
  LangChain, LlamaIndex, OpenAI Agents SDK and the Anthropic SDK, and a paste-ready
  prompt block.
- **G9 Works without an LLM.** Discovery, measurement, graph, scoping and rendering
  run with zero LLM calls; the LLM only adds drafted meaning.

### Non-goals (v1)
- Writing to the warehouse in any form. SchemaGuru is read-only.
- Executing agent-written SQL. SchemaGuru gives agents knowledge; running their
  queries is the host's job (an optional read-only guard helper is provided, §16.4).
- Natural-language-to-SQL generation.
- Column-level lineage from ETL code / dbt manifests (import of dbt descriptions and
  tests as *declared* facts **is** in scope, §8.4; lineage graphs are v2).
- Sampled *verification*: a sample can refute an expectation but never confirm it (§8.6).
- A web UI beyond the docs site's demo (§23).

### Anti-goals (a failure even if it "works")
- Showing an agent a number as measured when it was not, or a draft as verified.
- A join path that multiplies rows returned without a warning.
- Sending an unmasked sample value to an LLM without the user's explicit opt-in.
- Any knowledge write on the agent's query path.
- A full schema dump into a prompt as the default behaviour.
- Requiring a running service to use the library.
- Any mention of the originating company, its datasets or table names.

---

## 3. Constraints

- License MIT, `Copyright (c) 2026 Rupak Chakraborty`.
- Package `schemaguru` on PyPI (free as of 2026-11-24), import name `schemaguru`.
- Repository `github.com/rupakc/schemaguru`, public, default branch `main`, commits
  pushed incrementally to `main`.
- Python 3.10–3.13 (matches FactLang) **[call]**; `src/` layout; `hatchling` backend.
- **Depends on FactLang** (`factlang>=0.1,<0.2`), including the FactLang spec's
  Amendment A (§18: R1–R10), which the owner directed into FactLang 0.1.0 on 2026-11-24 for claim language, SQL rendering
  across dialects, connectors and verdicts (owner decision). Until FactLang 0.1.0 is
  on PyPI, development and CI install it from `git+https://github.com/rupakc/factlang@main`;
  SchemaGuru 0.1.0 is not released before FactLang 0.1.0.
- Core runtime dependencies: `factlang`, `PyYAML` **[call]**. Everything else is an
  optional extra imported lazily.
- `mypy --strict`, `ruff check`, `ruff format --check` enforced in CI.
- Layering enforced by an AST test (§5.1).
- Generic example domain everywhere (docs, tests, fixtures): an online shop —
  `customers, orders, order_items, products, payments, events, accounts, users`.

---

## 4. Knowledge model

All knowledge objects are frozen dataclasses in `schemaguru.model`, JSON-serialisable
(`to_dict()` / `from_dict()`), with stable identifiers.

### 4.1 Identity
- `ObjectRef`: `(catalog?, schema, table, column?)`. **[call]** Names are stored
  **exactly as the catalog returns them** — SchemaGuru never case-folds identity
  (folding can merge or split objects the warehouse treats differently, e.g. MySQL
  `lower_case_table_names`, collation-dependent T-SQL). Equality is exact. User input
  (overlay keys, API arguments, agent tool args) is *resolved* to a stored ref:
  exact match first, then a case-insensitive match that must be unique; an ambiguous
  match is diagnostic `SG201`/`SG501` naming the candidates, never a guess. Canonical
  string form `catalog.schema.table[.column]`, quoted via FactLang's profile where needed.
- `WarehouseId`: a user-given name plus dialect (`"prod-snowflake"`). A store may
  hold several warehouses.

### 4.2 Structural objects (from the catalog)
- `Schema(ref, comment?, table_count?)`
- `Table(ref, kind ∈ {table, view, materialized_view, external, other}, comment?,
  row_count?, bytes?, created_at?, last_altered?, partitioning?, clustering?)`
- `Column(ref, ordinal, native_type, family, nullable?, comment?, default?)` —
  `family` uses FactLang's type families.
- `Constraint(kind ∈ {primary_key, unique, foreign_key}, table, columns (ordered),
  ref_table?, ref_columns?, enforced?)` — many warehouses declare but do not enforce.

### 4.3 Facts (the unit of intelligence)
Every statement SchemaGuru makes is a `Fact`:

```python
@dataclass(frozen=True)
class Fact:
    id: str                 # content hash of (kind, subject, value-defining fields)
    kind: FactKind          # see table below
    subject: ObjectRef | JoinRef
    value: Mapping[str, Any]        # kind-specific payload; unknown fields are None, never defaulted
    provenance: Provenance          # CATALOG | DECLARED | MEASURED | DRAFTED | HUMAN
    status: Status                  # see §4.4
    method: str | None              # measurement method (e.g. "exact"), mandatory when MEASURED
    evidence: Evidence | None       # FactLang outcome: claim text, verdict, sql, numbers, duration, bytes
    source: str | None              # "information_schema", "dbt:manifest", "llm:<model>", "overlay:<file>:<line>"
    observed_at: datetime | None    # tz-aware UTC; naive timestamps are rejected
    snapshot_id: str | None
```

| `FactKind` | Value payload | Typical provenance |
|---|---|---|
| `DESCRIPTION` | `text` (≤ 30 words for columns, ≤ 60 for tables, **[call]**) | warehouse comment, DRAFTED, HUMAN |
| `USE_CASES` | `questions: list[str]` (what this table answers) | DRAFTED, HUMAN |
| `ENTITY_TYPE` | `entity: str` (e.g. `customer`, `user`, `order`, `event`) | DRAFTED, HUMAN |
| `PRIMARY_KEY` | `columns: tuple` (ordered; composite in full) | CATALOG, MEASURED, HUMAN |
| `JOIN` | see §4.5 | CATALOG (declared FK), MEASURED, HUMAN, DRAFTED (candidate) |
| `VALUES` | `values: list`, `case_sensitive: True`, `split?`, `observed: list[{value,count}]`, `undeclared: list` | MEASURED (observed), HUMAN (expected) |
| `NULL_RATE` | `rate` | MEASURED |
| `ROW_COUNT` | `rows` | CATALOG (stats), MEASURED |
| `DISTINCT` | `distinct_keys` | MEASURED |
| `FRESHNESS` | `column`, `max_value`, `age_seconds` | MEASURED |
| `TYPE_NOTE` | `stored_as`, `means` (e.g. STRING holding ISO dates, INT holding epoch-ms) | MEASURED (pattern), HUMAN |
| `SOFT_DELETE` | `predicate` (FactLang filter text producing *live* rows), `marker_columns` | DRAFTED → verified, HUMAN |
| `PII` | `category` (email, phone, name, address, gov_id, card, iban, ip, free_text, other), `detector` | MEASURED (shape), DRAFTED, HUMAN |
| `PITFALL` | `rule`, `fix`, `severity ∈ {warn, critical}` | DRAFTED, HUMAN |
| `GLOSSARY` | `term`, `definition`, `maps_to: list[ObjectRef]` | HUMAN, DRAFTED |
| `METRIC` | `name`, `definition` (FactLang-checkable text or prose), `tables` | HUMAN |
| `NEVER_QUERY` | `reason` | HUMAN |
| `WAYPOINT_RULE` | §9.5 | HUMAN |

Kinds are an open registry (`sg.model.register_fact_kind(...)`, §21).

### 4.4 Status and precedence
`Status`: `OBSERVED` (**[call]** a measured number with no predicate — row count,
null rate, distinct count, observed values, freshness age — produced by an executed
FactLang primitive; it is a measurement, not a verified claim, and renders as
"measured <date>"), `VERIFIED` (a FactLang claim PASSED at `observed_at`), `CONTRADICTED`
(FAIL), `UNVERIFIED` (drafted or declared, not yet checked or not checkable),
`ESTIMATED` (**owner decision, §8.6**: a number measured on a sample of a large
table — carries `method`, sample fraction, sampled rows and, for ratios, a 95% interval;
renders as "≈ 3.2% (sampled 1%, ±0.4%)"; never VERIFIED), `UNMEASURABLE` (FactLang NO_DATA/UNPROBEABLE — e.g. empty table), `ERROR` (probe
failed), `STALE` (OBSERVED or VERIFIED but older than the freshness TTL, default 14 days —
still shown, flagged), `WAIVED` (contradiction acknowledged by a human with a mandatory
`expires` date).

**Resolution rule** (deterministic; implemented once, in `schemaguru.knowledge.resolve`):
For each `(subject, kind)` the *effective* knowledge is computed from all facts:
1. `HUMAN` beats `DRAFTED` for meaning kinds (`DESCRIPTION`, `USE_CASES`,
   `ENTITY_TYPE`, `PITFALL`, `GLOSSARY`) — always (owner decision).
2. `HUMAN` and `DECLARED` *expectations* (`VALUES`, `JOIN`, `PRIMARY_KEY`,
   `SOFT_DELETE`) are never replaced by measurement. They are *checked*; the
   rendered view shows the expectation **and** the measured contradiction (e.g.
   "declared values a,b — observed also: c (412 rows)").
3. For structural truth (does the column exist, what is its type), `CATALOG` beats
   everything; a human note contradicting it is kept and the contradiction is shown
   ("note says DATE; stored as STRING").
4. Among `MEASURED` facts of the same kind, the newest `observed_at` wins;
   per-column facts are *all* retained (a last-write-wins index once dropped 9 of 25
   mismatches) and the worst verdict wins for warnings.
5. `DRAFTED` facts are shown only with an "unverified draft" marker unless their
   checkable part has been VERIFIED by FactLang.

### 4.5 Joins
```python
@dataclass(frozen=True)
class JoinRef:                      # directed: child -> parent
    left: tuple[ObjectRef, ...]     # one or more columns (composite joins allowed)
    right: tuple[ObjectRef, ...]
    cast_to: str | None             # FactLang type family when types differ

JoinFact.value = {
  "match_rate": float|None,        # distinct non-null left keys matched / distinct non-null left keys
  "coverage": float|None,          # distinct right keys reached / distinct right keys (reverse support)
  "row_match_rate": float|None,
  "distinct_left": int|None, "distinct_right": int|None, "matched": int|None,
  "left_non_null": int|None,
  "fan_in": float|None,            # left_non_null / distinct_left  (child rows per parent key)
  "max_left_per_key": int|None, "max_right_per_key": int|None,
  "cardinality": "N:1"|"1:1"|"1:N"|"N:M"|None,
  "verdict": "OK"|"WEAK"|"BROKEN"|None,   # OK: match >= 0.95, WEAK: >= 0.05, else BROKEN [call: defaults, configurable]
}
```
`support(direction)` returns `match_rate` when traversed child→parent, `coverage`
when parent→child.

### 4.6 Identity chains (join recipes)
`Chain(name, steps: list[Step], because: str)`; a `Step` is a `JoinRef`, a filter
(FactLang filter text), or a note. Chains are authored by humans (or drafted and
then accepted). Linear chains are compiled into waypoint requirements (§9.5); the
compiler orients each join the way the measured graph declares it.

---

## 5. Architecture

```
                 ┌─────────────────────── agent surface ───────────────────────┐
                 │ Python API · tool specs · MCP server · framework adapters   │
                 └───────────────▲─────────────────────────────▲───────────────┘
                                 │                             │
             ┌──── scoping & rendering (views) ────┐     ┌─ feedback ─┐
             │ retrieval · graph expansion · budget│     │ error intake│
             └───────▲──────────────────▲──────────┘     └─────▲──────┘
                     │                  │                      │
        ┌──── knowledge (resolve) ────┐ ┌──── join graph ────┐  │
        │ facts + precedence          │ │ cost · search · hub│  │
        └──▲────────▲─────────▲───────┘ └─────────▲──────────┘  │
           │        │         │                   │             │
   ┌── crawl ──┐ ┌─ measure ─┐ ┌─ draft (LLM) ─┐ ┌─ overlays ─┐ ┌─ refresh ─┐
   │ catalogs  │ │ FactLang  │ │ pii mask      │ │ YAML human │ │ drift diff│
   └────▲──────┘ └────▲──────┘ └──────▲────────┘ └────────────┘ └───────────┘
        │             │               │
   ┌────┴─────────────┴─── store (SQLite default | Postgres) ───────────────┐
   └────────────────────────────────────────────────────────────────────────┘
        │             │
   catalog readers   FactLang connectors  ──►  warehouse (read-only)
```

### 5.1 Package layout and layers (lowest first; a module imports only from its own
or strictly lower layers — enforced by `tests/architecture/test_layers.py`)

```
src/schemaguru/
  L0  __about__.py, errors.py, diagnostics.py, _util/        (stdlib only)
  L1  model/            ObjectRef, Fact, FactKind, Status, JoinRef, Chain, Evidence
  L2  store/            Store protocol, SQLiteStore, PostgresStore (extra), migrations/
      config.py         GuruConfig (pydantic-free dataclasses; from env / dict / YAML)
  L3  catalog/          CatalogReader protocol + one reader per dialect, crawl.py
      pii/              shape detectors, masking, policy
      overlays/         YAML schema, loader, writer, validation
  L4  measure/          probe planner (→ FactLang IR/claims), budget, scheduler, candidates
      knowledge/        resolve.py (precedence), snapshot.py, diff.py
  L5  graph/            edges, cost, search, hubs, waypoints, chains
      draft/            LLMProvider protocol, prompts, drafting pipeline, providers/
  L6  scope/            Retriever protocol, lexical (FTS), embeddings (extra), expansion, budget
      render/           view builders, formats, legend, fingerprint
      refresh/          incremental refresh, change classification, report
      feedback/         error intake, type-mismatch detector, re-probe queue
  L7  guru.py           Guru façade (the public entry point)
      agent/            tool specs, dispatcher, prompt block, mcp.py (extra), adapters/ (extras)
      cli/              `schemaguru` command
  L8  server/           optional HTTP service (extra `server`), same API over HTTP (Option C)
```

`schemaguru/__init__.py` re-exports the public API only (§19).

### 5.2 Relationship to FactLang (owner decision: depend on it)
SchemaGuru **never renders measurement SQL itself**. It uses FactLang for:
- **Connectors**: SchemaGuru accepts any `factlang.Connector`; `sg.Guru.connect(url)`
  builds one via `factlang.connect`. Catalog readers execute their metadata SQL
  through the same connector with `fl.connectors.run_metadata` (guarded, bound params;
  FactLang Amendment A R8).
- **Dialect knowledge**: identifier quoting/case rules and type families come from
  `factlang.sql` profiles.
- **Measurement**: every probe is either a FactLang claim (`JOIN … MATCH`, `UNIQUE`,
  `NULLS`, `VALUES`, `FRESH`, `PATTERN`, `ROWS`) run by a `factlang.Checker`, or a
  FactLang IR primitive compiled by FactLang for discovery (e.g.
  `ValueMembership(declared=())` returns the exact top-k observed values with counts;
  `ColumnAggregate(DISTINCT_KEYS, NON_NULL, ROW_COUNT)`; `KeyOverlap` with key tuples,
  which returns forward match, reverse `coverage`, row match and fan-in in one probe),
  executed with `Checker.measure` → `Measurement` (FactLang Amendment A R1–R4). Pricing
  uses `Checker.estimate` (R5); filters are parsed with `fl.parse_filter` and join
  snippets rendered with `fl.sql.render_join` (R6). If a needed primitive is missing, SchemaGuru registers it through
  FactLang's public `fl.ir.register_primitive` — never by emitting SQL strings.
- **Catalog**: **[call]** SchemaGuru always passes FactLang a store-backed
  `factlang.Catalog` (`measure/catalog_bridge.py`, answers from crawled columns, `None`
  for un-crawled tables) so FactLang never issues its own per-table
  `INFORMATION_SCHEMA` queries (billed on BigQuery).
- **Verdicts**: a fact is `VERIFIED` iff its FactLang outcome is `PASS`; FactLang's
  `Outcome.to_dict()` is stored verbatim as `Evidence`.
- **Agent tools**: SchemaGuru's tool set includes FactLang's `factlang_check` so an
  agent can verify a fact it is about to rely on.

The only SQL SchemaGuru authors directly is **catalog metadata SQL** (§6), which is
templated per dialect, reads only system views, and uses bind parameters for every
value.

---

## 6. Discovery (catalog readers and the crawl)

### 6.1 `CatalogReader` protocol
```python
class CatalogReader(Protocol):
    dialect: str
    def list_catalogs(self) -> Iterator[str]: ...                                 # may yield nothing
    def list_schemas(self, catalog: str | None, *, page_token: str | None) -> Page[Schema]: ...
    def list_tables(self, schema: SchemaRef, *, page_token: str | None,
                    changed_since: datetime | None) -> Page[Table]: ...
    def columns(self, tables: Sequence[TableRef]) -> Mapping[TableRef, list[Column]]: ...   # batched
    def constraints(self, tables: Sequence[TableRef]) -> Mapping[TableRef, list[Constraint]]: ...
    def capabilities(self) -> ReaderCapabilities   # which of: comments, stats, last_altered,
                                                   # constraints, incremental, cost_estimate
```
Every method is metadata-only (system catalogs / `INFORMATION_SCHEMA` / `SHOW`
commands / client APIs). Unknown → `None`, never a guess.

### 6.2 Readers shipped in v1 (one module each, each tested against recorded fixture
metadata *and*, where a container exists, a live engine in CI)
| Dialect | Source of metadata | Notes the builder must respect |
|---|---|---|
| BigQuery | `region-*.INFORMATION_SCHEMA.{SCHEMATA,TABLES,COLUMNS,TABLE_STORAGE,TABLE_CONSTRAINTS,KEY_COLUMN_USAGE}`, column descriptions from `COLUMN_FIELD_PATHS` | per-region; INFORMATION_SCHEMA queries are billed (≥ 10 MB each) → batch per dataset, count toward the budget |
| Snowflake | `<db>.INFORMATION_SCHEMA.*`, `SHOW PRIMARY KEYS / IMPORTED KEYS IN SCHEMA` | identifiers upper-case unless quoted; uses warehouse compute |
| Redshift | `SVV_ALL_SCHEMAS/TABLES/COLUMNS`, `SVV_TABLE_INFO`, `pg_constraint` | external (Spectrum) schemas via `SVV_EXTERNAL_*` |
| Databricks | Unity Catalog `system.information_schema.*` | three-level namespace |
| PostgreSQL | `pg_catalog` (not `information_schema`, which hides objects the role cannot use and is slow at scale) | `reltuples` as row estimate, `pg_description` comments |
| DuckDB | `duckdb_tables()/duckdb_columns()/duckdb_constraints()` | used heavily in tests |
| ClickHouse | `system.tables/columns` | no FKs; `Nullable(...)` unwrapping via FactLang families |
| SQL Server / Synapse | `sys.tables/columns/types/foreign_keys/extended_properties` | `MS_Description` comments |
| Oracle | `ALL_TABLES/ALL_TAB_COLUMNS/ALL_CONSTRAINTS/ALL_COL_COMMENTS` | upper-case folding |
| Trino | `<catalog>.information_schema.*` | federated; constraints usually absent |
| MySQL/MariaDB | `information_schema` | |

### 6.3 Crawl at scale (`catalog/crawl.py`)
- **Metadata-only, paged, resumable.** The crawl walks catalogs → schemas → tables in
  pages (default 1,000 tables), fetches columns/constraints in batches (default 200
  tables per query), and checkpoints after every page into the store (`crawl_state`
  table). An interrupted crawl resumes from the checkpoint.
- **Incremental.** When the reader supports `changed_since` (`last_altered`), a
  refresh re-reads only changed tables plus a full *name-only* listing to detect drops.
- **Concurrency** bounded (default 4 workers) **[call]**; per-query timeout (default 60 s).
- **Scope filters**: include/exclude glob patterns on `catalog.schema.table`
  (default excludes: system schemas, `*_staging*`, `*tmp*`, `information_schema`)
  **[call: defaults]**. Excluded objects are never stored.
- **Streaming**: the crawl never holds the whole catalog in memory; it writes page by
  page. Memory is bounded by page size.
- **Cost**: each reader reports an estimate where possible; the crawl counts metadata
  queries and (BigQuery) bytes billed against `budget.crawl_bytes` (default: no cap,
  but always reported).
- **Withdrawn objects**: a table not seen in a full listing is marked `withdrawn_at`
  (soft), not deleted; its facts are retained for one refresh cycle and surfaced as
  drift. Human overlays on withdrawn objects are never deleted.
- **Failure isolation**: a schema or page that errors is recorded in `crawl_errors`
  and skipped; the crawl continues; the run summary names every skipped item.

---

## 7. Store

### 7.1 `Store` protocol
CRUD over the model plus: `search(query, k)` (lexical), `facts_for(subjects, kinds)`,
`snapshot()`/`diff(a, b)`, `crawl_state`, `budget_ledger`, `feedback_queue`,
transactions. All writes are idempotent upserts keyed on stable ids.

### 7.2 Backends
- **`SQLiteStore`** (default, stdlib `sqlite3`): one file, default
  `./.schemaguru/<warehouse>.db` **[call]**. WAL mode. FTS5 virtual table for lexical
  search over `(name, columns, comments, descriptions, use_cases, glossary)`; if the
  local SQLite lacks FTS5, falls back to a trigram LIKE index with a warning.
- **`PostgresStore`** (extra `postgres-store`): same schema, `tsvector` for search,
  optional `pgvector` for embeddings. For teams and for server mode (§17).
- Schema migrations are numbered SQL files in `store/migrations/`, applied on open;
  the store records its schema version; opening a newer store with an older library
  is refused with a clear message.

### 7.3 Determinism
`SOFT_DELETE.predicate` and every stored filter is FactLang filter text (§4.5 of the
FactLang spec, incl. the bare boolean atom), validated with `fl.parse_filter` on load.

Every export (`guru.export()` → JSON/YAML) is sorted and byte-deterministic for equal
content, so a snapshot can be committed and diffed.

---

## 8. Measurement

### 8.1 What is measured
| Probe | FactLang | Result fact |
|---|---|---|
| table row count | `ROWS t` (or catalog stats if fresh and the user allows stats) | `ROW_COUNT` |
| null rate per column | `ColumnAggregate(ROW_COUNT, NON_NULL)` fused per table | `NULL_RATE` |
| distinct count | `ColumnAggregate(DISTINCT_KEYS)` | `DISTINCT` |
| enum discovery | if `distinct ≤ enum_max_distinct` (default 20 **[call]**): `ValueMembership(declared=(), report_limit=enum_max_distinct)` | `VALUES` (observed) |
| declared enum check | `VALUES t.c IN [...] [SPLIT ':'] [ALLOW <=1%]` | `VALUES` status |
| PK uniqueness | `UNIQUE t.c [WITH [...]]` | `PRIMARY_KEY` status |
| join, both directions | `JOIN a.x -> b.y` + `KeyOverlap` reversed (coverage) + multiplicity | `JOIN` |
| freshness | `FRESH t.c WITHIN …` on detected timestamp columns | `FRESHNESS` |
| type notes | `PATTERN t.c LIKE '____-__-__%' RATE >=99%` etc. on STRING columns | `TYPE_NOTE` |
| PII shape | masking detectors run on sampled *top values* (see §11) plus `PATTERN` coverage claims | `PII` |

### 8.2 Laziness and priority
Measurement is **lazy by default** (owner-approved): a table is measured when
(a) an agent's question scopes to it for the first time (queued, not blocking —
the first answer renders with "unmeasured" markers; see §12.5) — **[call] an agent
question never spends warehouse money by itself**: the queue is drained only by an
explicit `guru.measure()` / `guru.measure_queue()` / `refresh()`, by
`schemaguru worker` (a foreground drain loop), or in-process when the user opts in with
`Config(measure_on_demand="background")`, which starts one daemon thread that drains
under the configured budget (SQLite writes go through the store's single writer
connection), (b) the user names
it (`guru.measure("shop.orders")`, globs allowed), or (c) a refresh re-measures stale
facts. Optional eager mode: `guru.measure(all=True, budget=...)` ordered by a priority
score = query frequency (from feedback) + declared-FK participation + log(row_count)
**[call]**.

### 8.3 Budgets
`Budget(max_bytes_per_probe, max_bytes_total, max_probes, max_seconds)`. Before
running, each probe is priced with `Checker.estimate` (dry-run bytes where the
connector supports it; otherwise "unknown", and SchemaGuru may add a heuristic from
catalog table bytes, labelled `method="heuristic"`). A probe over its
per-probe limit is not run → fact `ERROR` with reason `over_budget`. When the total
budget is exhausted the scheduler stops and reports the remaining queue. Every byte
spent is written to `budget_ledger`. Default budget: `max_bytes_total = 10 GiB` per
run, `max_bytes_per_probe = 1 GiB` **[call]**.

### 8.4 Join candidates (where joins to measure come from)
A join is measured only if it is a **candidate**. Candidates, in priority order:
1. Declared constraints from the catalog.
2. Imported metadata: dbt `relationships` tests / manifest, user YAML.
3. Name/type heuristics (never facts, only candidates): `x_id`/`xId`/`x_fk` →
   table named `x`/`xs` with a PK or `id` column of a compatible type family; exact
   column-name match to another table's single-column PK within the same schema.
   Bounded: ≤ 5 candidates per column, same catalog only unless configured **[call]**.
4. LLM-suggested joins (§10) — candidates only.
Every candidate becomes a `JOIN` fact only after measurement. Cross-family key types
(STRING vs INTEGER) are measured with `CAST string` and flagged
(`cast_required: true`), because the agent must cast too.

### 8.6 Sampling large tables (owner decision, 2026-11-24)
Tables holding terabytes cannot be read in full; small tables need no sampling.
- **When.** A table is *large* when catalog stats say `bytes ≥ sample_threshold_bytes`
  (default 50 GiB) or, with bytes unknown, `row_count ≥ sample_threshold_rows` (default
  500M) **[call: defaults, configurable per glob]**. Everything else is measured exactly.
  `sampling="off"` forces exact everywhere (probes over budget then become `ERROR
  over_budget`); `sampling="force"` samples every table (tests, demos).
- **How** — via FactLang's sampled measurement (FactLang Amendment B: `SampleSpec` on
  `Checker.measure` only; claims are never sampled):
  - *Key and join metrics* (`KeyOverlap`, uniqueness, fan-out, distinct keys): **key-hash
    sampling** — keep the rows whose hashed key falls in a bucket range. All rows of a
    sampled key are kept, so per-key multiplicities are exact for those keys; using the
    **same hash, seed and range on both sides of a join** keeps exactly the same keys on
    each side, so match rate and reverse coverage are unbiased estimates. Used only when
    the key's distinct count exceeds `exact_key_limit` (default 5M, the predecessor's
    threshold); target ≈ `exact_key_limit` sampled keys. Key-hash sampling may still scan
    the key columns in full (column-store billing), which is cheap because only key
    columns are read.
  - *Column profiles* (null rates, observed values/top-k, pattern rates): **block
    (SYSTEM) sampling** where the dialect has it — it is what actually cuts bytes billed —
    at a fraction chosen so the probe's estimated bytes fit `max_bytes_per_probe` (floor
    0.1%, ceiling 10% **[call]**); row-level Bernoulli where block sampling is unavailable.
    Block samples are clustered, so intervals are widened by a design-effect factor (2.0
    **[call]**) and labelled "approximate".
  - *Never sampled*: freshness (`MAX` of a timestamp must be exact), row counts (taken from
    catalog stats when the reader provides them, else exact `COUNT(*)`, else `ERROR
    over_budget`), and all claim verification.
- **What it means.** Ratios from a sample are estimates with a Wilson 95% interval on the
  sampled rows; counts are scaled by 1/fraction and labelled; distinct counts and maxima
  from a block/Bernoulli sample are **lower bounds** and say so; observed values from a
  sample are "seen in sample", and absent rare values are not claimed absent.
- **Expectations on large tables.** A sample can *refute*: a declared enum with an
  undeclared value seen in the sample → `CONTRADICTED` (the value exists, evidence
  attached); a declared-unique key with a duplicate in a key-hash sample →
  `CONTRADICTED`. A sample can never *confirm*: no counter-example → `ESTIMATED`
  ("consistent with the sample"), never `VERIFIED`. Exact confirmation remains available
  with `guru.verify(..., exact=True)` within budget.
- **Determinism.** The seed is fixed per (table, snapshot) so re-runs reproduce; the
  method string is `sampled:<keyhash|system|bernoulli>:<fraction>:<seed>`. `LIMIT` is never
  a sampling method, anywhere.

### 8.5 Rules carried from the predecessor
- A probe failure produces an `ERROR` fact for that subject only.
- An empty table gives `UNMEASURABLE`, distinct from an all-null column
  (`NULL_RATE = 1.0`, flagged "dead column").
- Measured numbers are stored with `method="exact"`; any other method string is
  treated as an estimate and never compared with exact numbers.
- Rates are never rounded at storage; rendering rounds (4 significant digits).

---

## 9. Join graph and path search

### 9.1 Edges
One edge per measured `JOIN` fact with verdict `OK` or `WEAK`. `BROKEN` and
missing-object joins are **refused**, not discounted. Unmeasured candidate joins enter
the graph only when `graph.include_unmeasured=True` (default False **[call]**), with
`UNMEASURED_PENALTY`. NaN/inf are treated as unmeasured. A malformed fact costs one
edge.

### 9.2 Cost per hop (additive; lower is better)
```
cost(hop) = W_SUPPORT * -ln(support(direction))
          + W_FANOUT  * ln(1 + distinct_left)
          + W_HUB     * ln(1 + in_degree(arrival_table))
```
Defaults `W_SUPPORT=1.0, W_FANOUT=0.35, W_HUB=0.25`, `UNMEASURED_PENALTY=1.0`
(cost added instead of the support term); `support == 0 → ∞`. All weights are
read at call time from `GraphConfig` (so sweeps are falsifiable). The docs must state
honestly that the fan-out term is a weak proxy (it correlated −0.30 with true
rows-per-key in the predecessor) and exists because nothing measured replaces it yet.

### 9.3 Search
- Dijkstra over **both directions** of every edge (SQL joins are symmetric; a parent
  hub has no out-edges).
- State key `(table, waypoint_automaton_state, hops, arrived_on_attribution, hubs_entered_bitmask)`;
  hop count is part of the key so a cheap long route cannot block a short one under
  the hop bound.
- `HOP_LIMIT = 6` (configurable).
- A route may revisit a non-hub table only when the waypoint rules force it (rendered as a
  self-join with aliases); the search's state key is exactly the one above, with no
  visited-set pruning (which would make the label-setting search incomplete).
- Unknown fan-out (`distinct_left = None`) is priced at the graph's worst known
  fan-out term (`ln(1 + max known distinct_left)`), so unknown is never cheaper than known.
- **Thin support is a preference, not a ban.** Pass 1 prunes every hop (first,
  intermediate or last) with support < `THIN_SUPPORT = 0.05`; pass 2 drops that pruning.
  Pass-2 routes are always labelled `provisional` with their narrowest support and are
  appended to the k results after the pass-1 routes (de-duplicated), so a thin direct
  join is still offered — labelled — when a longer well-supported route exists.
  *(Amended 2026-11-24 after review: "intermediate" made a one-hop thin join unlabelled.)*
- `k` best paths (default 3) via Yen's algorithm **[call]**.
- `Path.coverage` = narrowest support over hops; `None` if any hop is unmeasured.

### 9.4 Hub guard
- A **hub** is a table that is a sink in the directed edge set and either has an
  `ENTITY_TYPE` fact **or** (**[call]**, so the guard works with zero LLM and no
  overlays, G9) is reached by ≥ 2 attribution edges from distinct tables (measured
  shape alone).
- An in-edge is an **attribution edge** when `fan_in > ATTRIBUTION_FANIN`.
- Refused shape: arrive at a hub on an attribution edge **and** leave on another
  attribution edge (cartesian hazard). Hub as source/target is allowed.
- The predecessor's threshold (9.5) was calibrated on its own data. SchemaGuru ships
  `ATTRIBUTION_FANIN = 10.0` as default **[call]** and a calibration helper
  (`guru.graph.calibrate_hubs()`) that reports the fan-in distribution of edges into
  each hub and proposes the centre of the widest gap; the user accepts it into config.

### 9.5 Waypoint rules
Human-authored constraints on paths, form `any* p any*`, compiled into a bitmask
automaton with a dead state (emptiness decidable offline). Predicates: `node(t)`,
`col(t.c)`, `edge(a.x->b.y)`, `any`. Exact matching only. A rule needs `because:`.
Pair scope may require and forbid; node scope may only forbid: a node rule
`node: X` forbids routes through X as an intermediate, and its optional `forbid:` list
adds further prohibitions; every node-scope prohibition is withheld for a search where
the forbidden node is an endpoint. `any`/`col`/`edge` are not allowed in node-scope
forbids (SG602). A chain that cannot be compiled (non-linear, missing join) is reported
as an SG605 warning, never dropped silently. *(Clarified 2026-11-24 after review.)* ≤ 16 requirements per rule. A rule that cannot
parse fails closed (error at load); a prohibition no edge can match is reported as a
lint warning.

### 9.6 Typed refusals
`find_path` returns `PathResult(paths, refusal?)` where `refusal ∈ {NO_EVIDENCE,
SAME_TABLE, NO_ROUTE, WAYPOINT_UNSATISFIABLE, RULE_REJECTS_ALL, HUB_PIVOT}` with an
explanation naming the blocking mechanism. Diagnostic re-runs relax exactly one
mechanism to tell the user *why*; the relaxed route is never returned as a path.

### 9.7 SQL join snippet
Each path renders a dialect-correct `FROM … JOIN … ON …` snippet (with casts, composite
keys and the effective `SOFT_DELETE` predicates of each table) via
`fl.sql.render_join(..., filters=...)` → `RenderedFragment.inline()`. It is advisory text for the agent, never executed by SchemaGuru.

---

## 10. Semantic drafting (LLM)

### 10.1 Provider abstraction
```python
class LLMProvider(Protocol):
    name: str
    def complete_json(self, system: str, user: str, schema: dict, *, max_tokens: int) -> dict: ...
    async def acomplete_json(...) -> dict: ...
```
Shipped: `AnthropicProvider` (extra `anthropic`; default model `claude-sonnet-5-5`
**[call]**, configurable), `OpenAICompatibleProvider` (extra `openai`; any
OpenAI-compatible endpoint incl. local servers), `CallableProvider(fn)`. Structured
output uses tool/JSON-schema mode; a reply that fails schema validation is retried
once, then that table's draft is recorded as `ERROR` — never partially applied.

### 10.2 What a draft produces (per table, one call; batched for small tables)
Inputs: table and column names, types, comments, catalog stats, measured facts
(null rates, distinct counts, observed values after the sampling policy §11), declared
constraints, neighbouring tables' names. Outputs (JSON schema enforced):
`description`, column `descriptions`, `use_cases` (≤ 5), `entity_type`, `pitfalls`
(rule + fix), `soft_delete` (predicate guess), `type_notes`, `pii` guesses,
`join_candidates`, `glossary` terms.

### 10.3 Verification of drafts
Every checkable part of a draft becomes a FactLang claim and is run (within budget):
`soft_delete` → `ROWS t WHERE <predicate>` and a non-emptiness/partition sanity
check; `type_notes` → `PATTERN`; `join_candidates` → `JOIN`; `entity_type` → `UNIQUE`
on the claimed key. Passing parts become `VERIFIED`; prose parts stay `UNVERIFIED`
(drafted) until a human accepts them.

### 10.4 Guarantees
- Drafting **never runs on the agent query path**; it runs in `guru.draft(...)`,
  `schemaguru draft`, or refresh.
- Drafts never overwrite human facts; they are stored as `DRAFTED` facts.
- A human accepts drafts with `schemaguru review` (interactive TUI-less prompt in the
  terminal) or `guru.accept(fact_ids)` / by copying into overlays; accepted drafts
  become `HUMAN` facts with `source="accepted:<draft id>"`.
- Prompts live in `draft/prompts/*.md`, are versioned, and their hash is recorded on
  every drafted fact.
- Cost: token usage per call is recorded in the ledger; `draft_budget_tokens` caps a run.

---

## 11. Sampling and PII policy (owner decision)

Three modes, configured globally and overridable per catalog/schema/table glob:

| Mode | What the LLM sees from data values | Default |
|---|---|---|
| `masked` | observed values, with anything detected as personal masked to its shape (`<email>`, `<phone>`, `<name>`, `<iban>`, `<card>`, `<ip>`, `<uuid>`, `<hex32>`, `<free_text len=…>`) | **yes (default)** |
| `raw` | observed values unmasked — **only when the user explicitly opts in** | opt-in |
| `none` | no values at all; only names, types, comments, statistics | available **[call: added]** |

Rules:
- Values are only ever the *top observed values* of low-cardinality columns plus at
  most `sample_values_per_column = 5` **[call]** example values for other columns,
  drawn by FactLang's exact top-k (`ValueMembership(declared=())`) — never random
  rows, never whole rows.
- **Detection is by value shape first, then column-name signals, then cardinality**
  — never by name alone (lesson 7). Detectors: email (incl. lists), phone (E.164 and
  national forms), IBAN (checksum), card (Luhn), IPv4/IPv6, UUID, long hex (≥ 20 hex
  chars — 16 corrupts JSON numbers), JWT/secret-like high-entropy strings, person-name
  heuristic (capitalised 2–4 token strings with a name-list hit, opt-out), postal
  address heuristic, free text (> 60 chars with spaces). Each detector is a plug-in.
- In `masked` mode, a column whose values are masked is tagged with a `PII` fact
  (MEASURED, `detector=<name>`), which also reaches agents ("contains emails").
- **Low-cardinality trap**: a column with few distinct values whose values look like
  hashes/emails is flagged for review rather than auto-classified (an anonymised enum
  looks like identity by shape and like an enum by cardinality).
- Raw samples are never written to the store in `masked`/`none` mode; in `raw` mode
  they are stored only if `store_raw_samples=True` (default False).
- A final shape sweep runs over every outbound LLM payload in `masked` mode; any hit
  is masked and counted (`pii_sweep_hits` in the run report).
- Blank values (`''`, whitespace) are treated as NULL for detection.

---

## 12. Scoping and retrieval

### 12.1 Pipeline for `guru.context(question, budget_tokens=4000)`
1. **Retrieve seeds**: `Retriever.search(question, k=seed_k=8)` over table documents.
   Default `LexicalRetriever` = FTS5 BM25 over name tokens (snake/camel split),
   column names, comments, descriptions, use cases, glossary; plus exact-match boosts
   for identifiers that appear verbatim in the question. Optional
   `EmbeddingRetriever` (extra `embeddings`, pluggable `Embedder`) and
   `HybridRetriever` (reciprocal-rank fusion) **[call]**.
2. **Glossary/metric hits** pull in their mapped tables.
3. **Graph expansion**: best path between each pair of seeds (bounded: at most
   `max_expansion_tables=6` extra tables), so the agent sees the bridging tables.
4. **Budget fit**: tables ranked (seed score, then bridge); each table rendered at the
   richest *detail level* that fits: `full` → `standard` → `lite` (columns chosen by
   priority: PK, FK/join columns, columns referenced by pitfalls or soft-delete,
   columns with descriptions/values, then the rest) → `name-only`. Token counting via
   a pluggable `TokenCounter` (default chars/4 heuristic; `tiktoken`/Anthropic counter
   as extras).
5. **Always-on blocks** within budget: global pitfalls, never-query list, legend.
6. Return a `ContextView` (§13).

### 12.2 Determinism and caching
Same store snapshot + same question + same config ⇒ byte-identical view. Views are
cached on `(snapshot_id, question_hash, config_hash)` in an LRU (default 256 entries).

### 12.3 Recall safety nets
The view always ends with a short "Not shown" line (count of other candidate tables)
and the agent tools (`search_tables`, `describe_table`) let the agent fetch more.

### 12.4 Scale
Search must not scan all tables in Python; it runs in the store (FTS index). The
graph for path search is loaded per-connected-component lazily and cached.

### 12.5 First touch
If a scoped table has never been measured, the view marks it "unmeasured" and the
table is enqueued for lazy measurement (§8.2); the view is never blocked on it unless
`measure_on_demand="block"` with a time cap.

---

## 13. Rendering (views)

### 13.1 `ContextView`
`.text` (default format), `.to_markdown()`, `.to_json()`, `.to_xml()` (tagged sections
for models that prefer XML), `.blocks` (ordered `ViewBlock(stable: bool, text)` so
hosts can place prompt-cache breakpoints after stable blocks), `.tables`,
`.fingerprint` (sha256 of text), `.tokens`.

### 13.2 Profiles (per role)
Built-in profiles — the set is open (`sg.render.register_profile`): `agent` (default,
executor-style detail), `planner` (tables + use cases + join paths, few columns),
`verifier` (keyed, parseable: COLUMNS / VALUES / PITFALLS / PK lines), `router`
(table names + one-line descriptions only, for classification).

### 13.3 Table block (agent profile), illustrative
```
[shop.orders] — one row per order. Answers: revenue, order volume, basket size.
  order_id      INT64     PK (unique, verified 2026-11-20)
  customer_id   INT64     → shop.customers.customer_id  N:1  match 99.8%
  status        STRING    values: 'paid','refunded','cancelled' (+ observed 'PAID': 412 rows ⚠ case)
  created_at    STRING    ⚠ stored as STRING, holds ISO-8601 timestamps — CAST before comparing
  deleted       BOOL      live rows: (deleted IS NULL OR NOT deleted) — never FALSE, TRUE or NULL
  ⚠ Pitfall: refunds appear as a second row with negative amount — SUM nets them.
  Provenance: drafted descriptions (unverified) · measured 2026-11-20
```
Rules: consequence ordering (warnings before confirmations); hedges placed above what
they qualify; thin joins state coverage as a number; values lists capped
(`max_values_shown=6`) with "+N more"; unverified drafts marked; a legend appears only
when a ⚠ appears and says "absence of a warning is not verification".

### 13.4 Fingerprints
`guru.fingerprint_views(questions=[...])` renders a fixed question set and returns
hashes, so a host's CI can detect prompt drift after a refresh.

---

## 14. Overlays (human YAML)

### 14.1 Format (`schemaguru.yaml` or a directory `schemaguru/*.yaml`; versioned schema)
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
### 14.2 Rules
- Loaded with line numbers; every error is a diagnostic with file:line (`SG2xx`),
  never an exception that sinks the load. An empty YAML key is treated as absent.
- Overlays are the only place humans write; SchemaGuru **never edits a human file**.
  `schemaguru export-drafts --to schemaguru/drafts/` writes drafts to a *separate*
  generated file for review; `schemaguru accept` moves accepted entries into the
  store as HUMAN facts or (with `--write-overlay`) appends to a dedicated
  `schemaguru/accepted.yaml` that is machine-owned and marked as such.
- dbt import: `schemaguru import-dbt manifest.json` maps model/column descriptions to
  DECLARED descriptions and `relationships`/`accepted_values`/`unique` tests to
  DECLARED expectations.
- Waivers require `expires`; an expired waiver is ignored and reported.

---

## 15. Refresh and drift

`guru.refresh()` / `schemaguru refresh`:
1. Incremental crawl (§6.3); withdrawn/new/changed objects recorded.
2. Re-measure facts older than `fact_ttl` (14 d) and all facts on changed tables,
   within budget, highest-priority first.
3. Re-check all human/declared expectations on touched tables.
4. Build a **change report** between the previous and new snapshot, facts compared
   by identity and value (never timestamps). Each change classified:
   - `benign`: a measurement moved but kept its verdict; enum values only added;
     new non-PII columns/tables; PK still unique.
   - `review`: verdict changes, removed facts, enum values removed/renamed, new
     PII-looking columns, withdrawn tables, an expectation newly contradicted.
   - Unknown classes default to `review`.
5. **Relative judgement**: a contradiction already present in the previous snapshot
   is `drift (pre-existing)`, not a new regression.
6. Output: `RefreshReport` (JSON + Markdown), exit codes for CI (§20).

A refresh that measured fewer checks than the previous snapshot never overwrites it
silently: the new snapshot records `checks_run`, and the diff reports missing checks
as `not_measured`, not as "fixed".

---

## 16. Runtime feedback and guard

### 16.1 Error intake
`guru.report_error(sql, error_message, dialect=None)` — hosts call it when an agent
query fails. Records are keyed on `(table, column, failure_class)`, store only
identifiers and counts (never the SQL text **[call]**), and enqueue a re-probe. Never
raises; never writes facts directly.

### 16.2 Failure classes in v1
`type_mismatch` (parse the error's position or operand types, resolve the token at
that position in the SQL, aliases resolved; string literals blanked first; numeric
pairs ignored), `unknown_column`, `unknown_table`, `ambiguous_column`. Each class is a
plug-in `FailureClassifier`.

### 16.3 Effect
Feedback raises the table's measurement priority and, for `unknown_*`, triggers a
targeted re-crawl of that table at the next refresh. `guru.feedback_summary()` lists
hot spots including "no fact covers this column".

### 16.4 Read-only guard helper (`sg.guard.check_sql(sql, dialect)`)
Optional helper for hosts: single statement, must start with SELECT/WITH, DML/DDL
keyword blocklist applied after stripping comments and string literals (a literal
like `'%@x.y'` must not trigger), length cap, `never_query` tables rejected. It
returns a verdict object; it does not execute.

---

## 17. Server mode (Option C, extra `server`)

`schemaguru serve --store postgres://… --port 8080` runs an HTTP service (Starlette +
uvicorn **[call]**) exposing the same operations as the Python API (`/context`,
`/search`, `/tables/{ref}`, `/paths`, `/facts`, `/refresh` (admin), `/mcp` over
streamable HTTP). Auth: bearer tokens from config (v1), pluggable `Authenticator`.
The server is a thin layer over `Guru`; no logic lives only in the server. Multiple
replicas share a Postgres store; refresh is guarded by a store-level advisory lock.

---

## 18. Agent surface

### 18.1 Tools (`guru.tools()` → JSON-schema tool specs; `guru.handle_tool_call(name, args)`)
| Tool | Purpose |
|---|---|
| `schema_context` | the scoped view for a question (§12) |
| `search_tables` | ranked table refs + one-line descriptions |
| `describe_table` | full table block |
| `find_join_path` | paths/refusal between tables (§9) with SQL snippet |
| `column_values` | observed/declared values of a column |
| `check_fact` | delegates to FactLang `factlang_check` |
| `explain_term` | glossary/metric lookup |
Results are JSON with a `text` field for direct inclusion.

### 18.2 Prompt block
`guru.prompt_block(question=None)` returns a system-prompt fragment: how to use the
tools, the legend, global pitfalls; with a question it embeds the scoped view.

### 18.3 MCP (extra `mcp`)
`schemaguru mcp --url <warehouse> [--store …]` — stdio MCP server exposing the tools
above plus resources `schema://tables/{ref}`.

### 18.4 Framework adapters (each an extra, a thin module, tested)
`schemaguru.adapters.langchain` (Tools + a retriever), `.llamaindex` (tool spec),
`.openai_agents` (function tools), `.anthropic` (tool definitions + a runner helper
for the Anthropic SDK tool runner). Each adapter only maps `guru.tools()`; no logic.

---

## 19. Public API (stable in 0.x within a minor)

```python
import schemaguru as sg

guru = sg.Guru.connect("snowflake://…", name="prod", store="./.schemaguru/prod.db",
                       config=sg.Config(sampling="masked", budget=sg.Budget(max_bytes_total=5 * 2**30)))
guru = sg.Guru(connector=fl_connector, reader=sg.catalog.reader_for(fl_connector))  # bring your own

guru.crawl(include=["analytics.*"], exclude=["*_tmp"])     # metadata index
guru.measure("analytics.orders")                           # lazy by default; explicit here
guru.draft(["analytics.*"], provider=sg.draft.AnthropicProvider())
guru.load_overlays("schemaguru/")
view = guru.context("refund rate by month", budget_tokens=3000)
paths = guru.find_path("analytics.events", "analytics.orders")
report = guru.refresh()
guru.tools(); guru.handle_tool_call("search_tables", {"query": "refunds"})
```
Async twins exist for every I/O method (`acrawl`, `ameasure`, `acontext`, …).

### 19.1 CLI (`schemaguru`, stdlib argparse)
`init` (writes a commented `schemaguru.yaml` + config), `crawl`, `measure`, `draft`,
`review`, `accept`, `export-drafts`, `import-dbt`, `context "<question>"`, `search`,
`path A B`, `refresh`, `report`, `lint` (overlays), `serve`, `mcp`, `doctor`
(connectivity, permissions, FTS5, budgets). Global `--format text|json|markdown`.
Exit codes: 0 ok, 1 review-class changes / contradictions found (refresh, lint),
2 invalid input, 3 errors or nothing measured.

---

## 20. Diagnostics and errors
Codes `SGnnn`: `SG1xx` connection/catalog, `SG2xx` overlays, `SG3xx` measurement &
budget, `SG4xx` drafting/LLM, `SG5xx` scoping/rendering, `SG6xx` graph, `SG7xx`
store, `SG8xx` server/agent. Each `Diagnostic(code, severity, message, location?,
hint?)`. The table lives in `docs/reference/diagnostics.md`; a test asserts it equals
the set of codes the source emits. Exceptions: `SchemaGuruError` base; library code
raises only for programmer errors (bad arguments) and unrecoverable store errors.

---

## 21. Extension points (each: protocol, registry, worked example, test template)
`CatalogReader` (new warehouse) · `Store` · `Retriever` / `Embedder` · `TokenCounter`
· `LLMProvider` · PII `Detector` · `FailureClassifier` · `FactKind` · render
`Profile` / `Format` · `ChangeClassifier` · candidate `JoinCandidateSource` ·
`Authenticator` (server). Discovery via entry-point groups `schemaguru.<point>`.

---

## 22. Testing strategy
- **Unit** per module; **property tests** (hypothesis) for ref normalisation,
  resolve precedence (order-independence), overlay loader never raising, masking
  idempotence, renderer determinism.
- **Fixture warehouse** (`tests/fixtures/shop/`): a generated DuckDB + Postgres shop
  dataset with **known traps** and known answers: soft-delete flag TRUE/NULL; STRING
  dates; enum case drift; composite PK junction table; a thin-but-correct join; a
  broken join; a hub with two attribution edges (cartesian hazard); a reverse-coverage
  asymmetric join; a PII column; an anonymised-looking low-cardinality enum; an empty
  table; an all-null column. End-to-end tests assert SchemaGuru discovers each trap
  and the rendered view states it.
- **Reader conformance**: each dialect reader against recorded metadata fixtures
  (JSON of system-view rows) and live containers in CI where available (Postgres,
  MySQL, ClickHouse, SQL Server, Trino, DuckDB); cloud warehouses (BigQuery,
  Snowflake, Redshift, Databricks) via opt-in `tests/live/`.
- **Scale benchmark** (`benchmarks/`): synthetic catalog generator (500k tables, 25M
  columns) → crawl into SQLite from a fake reader, assert memory bound and search
  p95; runs nightly, not per-PR.
- **LLM tests** use a scripted `CallableProvider`; no recorded cassettes.
- **Golden views**: rendered views of the fixture warehouse committed; diffs reviewed.
- **Architecture**: layer test; "no proprietary names" test (a denylist file kept
  outside the package, checked in CI over the repo).
- Coverage gate 90% on core.

---

## 23. Repository, CI, docs, Claude scaffolding

### 23.1 Layout
```
schemaguru/
  src/schemaguru/…            (§5.1)
  tests/{unit,property,integration,live,architecture,golden}/  benchmarks/
  docs/                        mkdocs-material site source (+ specs/, wiki/)
  site-demo/                   interactive demo assets
  examples/                    runnable integration examples (duckdb shop, langchain, mcp, anthropic)
  .github/workflows/{ci,release,docs,wiki,bench}.yml
  .claude/{agents,skills}/…
  CLAUDE.md AGENTS.md MEMORY.md README.md LICENSE CONTRIBUTING.md CHANGELOG.md
  CODE_OF_CONDUCT.md SECURITY.md pyproject.toml
```

### 23.2 CI (GitHub Actions)
- `ci.yml` (push/PR): ruff, ruff format --check, mypy --strict, unit+property+golden
  on 3.10–3.13 × ubuntu/macos/windows; integration job with service containers
  (postgres, mysql, clickhouse, mssql, trino) + duckdb; coverage gate; architecture
  tests; `python -m build`, `twine check`; upload wheel+sdist as artifacts.
- `release.yml` (tag `v*`): build, GitHub Release with assets and notes, publish to
  PyPI via Trusted Publishing (environment `pypi`, `id-token: write`). Owner steps
  will be given at release time.
- `docs.yml` (main): build mkdocs site + demo → `gh-pages`.
- `wiki.yml`: sync `docs/wiki/` → GitHub wiki (first page created once by hand).
- `bench.yml` (nightly): scale benchmark.

### 23.3 Documentation
- **README**: what/why, 30-second example, install matrix (extras per warehouse),
  feature table, warehouse support table, links.
- **Wiki** (plain language, each page opens with a 3-line summary, ends with "see
  also"): Home, Why Schema Intelligence, Concepts (facts, provenance, status,
  precedence), Architecture, each component (Catalog & Crawl, Store, Measurement,
  Join Graph, Path Search & Hub Guard, Drafting, PII & Sampling, Overlays, Scoping,
  Rendering, Refresh & Drift, Feedback, Server, Agent Integration), Warehouse Guides
  (one per dialect: permissions, cost, quirks), Extending (one page per extension
  point), CLI, Configuration, Diagnostics, Scale & Cost, Security, FAQ, Glossary,
  Design Decisions (§25), Contributing.
- **gh-pages site**: landing page (problem → "the wrong join" story → how
  SchemaGuru fixes it), "where it shines" (agents on huge warehouses, onboarding
  analysts, data-contract CI, migration audits), interactive demo: a precomputed
  fixture-shop knowledge export rendered client-side — search box → scoped context
  view, join-graph explorer showing path search and the hub-guard refusal, before/
  after prompt comparison; plus API docs (mkdocstrings) and guides. A Pyodide live
  mode is the builder's call after a spike; the demo must not depend on it.

### 23.4 Claude scaffolding
- `CLAUDE.md` — rules: layers, FactLang-only measurement SQL, no sampling, `None` for
  unknown, precedence rules, no knowledge writes on the query path, no proprietary
  names, commands, EGM workflow.
- `AGENTS.md` — same for non-Claude agents + roles. `MEMORY.md` — dated decision log.
- `.claude/agents/`: `goldfish-implementer`, `goldfish-spec-reviewer`,
  `warehouse-reader-engineer`, `graph-algorithms-reviewer`, `pii-privacy-reviewer`,
  `docs-writer`, `release-engineer`.
- `.claude/skills/`: `add-warehouse-reader`, `add-fact-kind`, `add-detector`,
  `run-fixture-warehouse`, `release`, `egm-handoff`, `self-review` (checklist from
  §25).

---

## 24. Process: Elephant-Goldfish
- **Elephant** (long-running session) owns this spec, the plan
  (`docs/specs/2026-11-24-schemaguru-plan.md`) and `MEMORY.md`; writes no library code.
- **Goldfish spec check** before approval: a fresh agent reads this spec cold and lists
  what it could not build; resolved in the spec or asked of the owner.
- **Goldfish implementers**: each plan task to a fresh agent with only spec + task +
  repo; a separate fresh reviewer checks the diff against the spec; independent tasks
  fan out in parallel; same-layer tasks run sequentially.
- A goldfish needing information not in the spec = spec defect → amend + `MEMORY.md`.
- Every commit green; pushed incrementally to `main`.

### 24.1 Milestones
1. Repo skeleton, license, pyproject, CI lint/test/build, Claude scaffolding.
2. Model, diagnostics, config, store (SQLite) + migrations.
3. Catalog readers (DuckDB + Postgres first), crawl, scale benchmark harness.
4. Measurement via FactLang (planner, budget, scheduler, candidates), fixture warehouse.
5. Knowledge resolve, overlays, dbt import.
6. Join graph, cost, search, hub guard, waypoints, chains.
7. PII detectors/masking, drafting pipeline + providers.
8. Scoping, retrieval, rendering, fingerprints.
9. Refresh/drift, feedback, guard helper.
10. Agent tools, prompt block, MCP, adapters, CLI.
11. Remaining readers (cloud + others) with fixtures; live opt-in tests.
12. Server mode (Postgres store, HTTP, auth).
13. Docs site + demo, wiki, release workflow; v0.1.0 after FactLang 0.1.0.

---

## 25. Design decisions (lineage, with reasons) — the self-review checklist
(Each item is a rule a reviewer checks on every change.)
1. Unknown is `None`; never default a metric; never coerce `None` to 0 or 1.0.
2. Methods travel with numbers; never compare across methods.
3. Exact by default; large tables sampled representatively (key-hash / block), never
   `LIMIT`; sampled numbers are `ESTIMATED`, labelled, never compared with exact ones and
   never `VERIFIED`; a sample may refute but not confirm.
4. Forward match and reverse coverage are distinct facts.
5. Thin joins are returned labelled; broken joins are refused.
6. Hub guard refuses a *shape*, not a node; banning nodes disconnected most questions.
7. No role inferred from names alone; values, shapes, cardinality.
8. Expectations are alarms; never auto-edit them to match data.
9. Measured types are stated next to human notes, never by deleting them.
10. No writes on the query path.
11. Judge relative to the previous snapshot.
12. Byte-deterministic exports and views; fingerprint views.
13. Consequence-ordered rendering; legend states silence ≠ verification.
14. One bad item costs one item; loaders never raise on data.
15. Soft-delete flags may be TRUE/NULL (never FALSE); a load-bearing column is never
    dropped by a name pattern.
16. Composite keys are declared and grouped in full.
17. A narrower run never overwrites a fuller snapshot; missing checks are reported.
18. Waivers expire.
19. Timestamps are tz-aware UTC; naive is rejected.
20. No proprietary names anywhere.

## 26. Approaches considered
- **A. Library + local knowledge store + human YAML overlays** — scales to hundreds
  of thousands of tables via indexed store and scoped retrieval; overlays keep human
  knowledge reviewable in git. Cost: two sources of knowledge → strict precedence (§4.4).
- **B. YAML files only** (the predecessor's way) — simple and reviewable, but
  gigabytes of YAML at scale, weak search, unreadable diffs.
- **C. Server-first** — fits big shared deployments, but adoption requires running a
  service, against "easy to integrate".

**Chosen (owner): A, with C as an optional add-on (`schemaguru serve`, §17).**
Also decided by the owner: sample large (terabyte) tables representatively, measure
small ones exactly (§8.6); depend on FactLang; LLM drafts meaning and humans augment
or override (human always wins); sampling `masked` by default, `raw` on opt-in;
SchemaGuru is standalone (no obligation to match any existing system); build from
scratch with concepts, not code.

## 27. Left to the builder
Internal class names below the public API; exact SQL of each catalog reader (must
use only system views listed in §6.2 or their documented equivalents); message
wording (codes fixed); FTS tokenizer details; the demo's visual design; whether
Pyodide live mode ships (after a spike).
