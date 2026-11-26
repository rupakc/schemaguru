# SchemaGuru 0.2.0 — Design Specification

Status: approved by the owner 2026-11-25 (scope and short design); this document is the
binding authority for the 0.2.0 plan. It **extends** the 0.1.0 specification
(`docs/specs/2026-11-24-schemaguru-design.md`, "the base spec"); everything the base
spec says still holds unless a section here amends it, and an amendment names the base
section it changes. **[call]** marks a decision made by the author (not the owner) that a
builder may revisit with a recorded ruling.

## 1. Intent

0.1.0 shipped the library. 0.2.0 does three things, chosen by the owner:

1. **Lineage** — the base spec's one named v2 feature (base §2 non-goals: "lineage graphs
   are v2"). Agents learn which tables and columns are *built from* which, so they can
   answer "where does `revenue` come from?", prefer a curated model over its raw inputs,
   and avoid double counting a figure that already aggregates another.
2. **Hardening** — clear the ~40 minor findings deferred during the 0.1.0 build and the
   final review's "can wait" items, so 0.2.0 carries no known debt from 0.1.0 that is
   not either fixed or ruled out with a reason.
3. **Scale proof** — base goal G5 (≥ 500,000 tables / 25M columns, context < 200 ms p95)
   was asserted but never measured at that size. 0.2.0 measures it reproducibly,
   publishes the numbers and fixes what the measurement exposes.

Out of scope (owner decision 2026-11-25): live validation of the cloud readers
(BigQuery, Snowflake, Redshift, Databricks) — no credentials in this release; and
warehouse query-history lineage (BigQuery JOBS, Snowflake ACCESS_HISTORY, Databricks
system lineage), which could only ship validated by replayed fixtures.

## 2. Goals, non-goals, anti-goals

### Goals
- **L1 Sources.** Lineage from (a) dbt manifests: table-level from `depends_on`,
  column-level by parsing each node's `compiled_code`; (b) warehouse view and
  materialized-view definitions crawled by the catalog readers, parsed for table- and
  column-level lineage. Humans augment or override lineage in overlays (owner principle:
  the machine drafts, humans augment/override, human always wins).
- **L2 Truthful.** Every lineage link states its source (`dbt:manifest`,
  `view_definition`, `overlay:<file>:<line>`) and is shown as declared/parsed and
  **unverified** — never as measured, never VERIFIED.
- **L3 Agent surface.** Lineage reaches agents through the rendered context, a new
  `lineage` tool (Python, MCP, HTTP server, CLI) and the framework adapters, which
  inherit tools from `guru.tools()`.
- **L4 Optional parser.** Column-level lineage needs `sqlglot`, shipped as the extra
  `schemaguru[lineage]`. Without it, table-level dbt lineage still works and a single
  INFO diagnostic says column-level lineage needs the extra. The core install gains no
  dependency.
- **H1 Hardening.** Every item in Appendix A ends 0.2.0 in exactly one state:
  *fixed* (with a test that failed before the fix), *already fixed* (with the commit and
  test that prove it), or *ruled out* (with a one-line reason in the ledger and, where
  user-visible, in the docs).
- **S1 Scale.** A reproducible benchmark at 500,000 tables / 25,000,000 columns
  measures crawl, graph build, scoping/render and lineage traversal time plus peak
  memory; results are committed and published; a nightly job fails on regression.

### Non-goals (0.2.0)
- Query-history lineage; lineage from arbitrary ETL code (Python, Spark, stored
  procedures). Only dbt `compiled_code` and catalog view definitions are parsed.
- Verifying lineage by data (e.g. checking that a view's row count matches its inputs).
- Using lineage links as join paths (see anti-goals).
- Lineage through `SELECT *` into tables/columns the store does not know (they are
  reported, not guessed — §4.4).
- A lineage UI beyond text/JSON rendering.

### Anti-goals (a failure even if it "works")
- A lineage link rendered as measured or verified.
- A lineage link used as a join edge by path search, scoping or the SQL snippet
  (base anti-goal: "a join path that multiplies rows returned without a warning").
- A raw view definition, or any literal from one, stored, logged, rendered or sent to an
  LLM (view SQL can contain PII literals such as `WHERE email = 'x@y.com'`).
- Parsing that can hang, exhaust memory or crash a crawl on hostile SQL.
- A hardening "fix" that silently loosens a guard (the read-only guard, masking or the
  InlineConnector refusals) to make a test pass.

## 3. Constraints (carried from the base spec, still binding)
- Python ≥ 3.10; `mypy --strict`; ruff; the gate
  `ruff check && ruff format --check && mypy --strict src && pytest -m "not integration and not live and not benchmark"`
  must pass before every push (the pre-push hook enforces it; never `--no-verify`).
- Core dependencies stay `factlang>=0.1.4,<0.2` (raised in 0.1.1; 0.2.0 requires 0.1.4 for zone-explicit databricks timestamps) and `PyYAML>=6`; everything else is an
  extra and imported lazily (the lazy-deps architecture test enforces this).
- The layer rules of base §5.1 hold; new modules are placed in a layer and the layer
  test learns them.
- Determinism (base §7.3, §12.2): the same store and inputs render byte-identical views
  and the same fact ids.
- Privacy model unchanged: masked by default, raw on opt-in, `none` mode; masking at
  storage, evidence, render sweep, tools and HTTP; errors scrubbed.
- Public API (base §19) is stable within 0.x minors: 0.2.0 may **add** names and
  optional parameters; it must not remove or change the meaning of a 0.1.0 name.
- A store written by 0.1.0 (SQLite or Postgres) opens under 0.2.0 by forward migration;
  no user action, no data loss.
- No mention of any proprietary predecessor or its organisation (the denylist CI job).

## 4. Lineage

### 4.1 Knowledge model (amends base §4.3)
Two new built-in `FactKind`s, registered like the others (base §21):

| `FactKind` | Subject | Value payload | Provenance |
|---|---|---|---|
| `LINEAGE` | target table `ObjectRef` | `upstream: tuple[str, ...]` — canonical refs of the input tables, sorted; `unresolved: tuple[str, ...]` — input names the store does not hold, verbatim, sorted | `DECLARED` (dbt), `CATALOG` (view definition), `HUMAN` (overlay) |
| `COLUMN_LINEAGE` | target column `ObjectRef` | `inputs: tuple[str, ...]` — canonical column refs, sorted; `transform ∈ {copy, cast, expression, aggregate, window, unknown}`; `unresolved: tuple[str, ...]` | same |

- `status` is always `UNVERIFIED`. The resolver must never promote either kind (a test
  pins it, mirroring "a sample is never VERIFIED").
- `source` is `dbt:manifest`, `view_definition` or `overlay:<file>:<line>`.
- Fact id: the existing `Fact.make` rule (kind, subject, provenance and the kind's value
  identity fields) with identity fields `upstream`/`inputs` (sorted). `source` is **not**
  part of the id **[call, builder check]** — provenance already separates dbt (DECLARED),
  view (CATALOG) and overlay (HUMAN) facts, and an overlay line number in the id would
  change ids whenever lines above move. dbt facts keep today's source form
  `dbt:manifest:<uid>`.
- `transform` classification of one output column's expression **[call]**: a bare
  column reference (optionally aliased) → `copy`; only `CAST`/`::`/`TRY_CAST` around a
  bare column → `cast`; an aggregate function anywhere in the expression, other than as a window function's own function under `OVER` → `aggregate` (amended 2026-11-26: a windowed `SUM(x) OVER ()` does not collapse rows, so it is `window`; `SUM(SUM(x)) OVER ()` still contains a plain aggregate → `aggregate`);
  a window function (and no aggregate) → `window`; any other expression → `expression`;
  the parser could not attribute the column → `unknown` (inputs empty). Only projection
  lineage is recorded; columns used only in `WHERE`/`JOIN ON`/`GROUP BY` are not inputs.
- Resolution (amends base §4.4) **[call, builder check]**, per subject and per level
  (table-level `LINEAGE` and column-level `COLUMN_LINEAGE` are resolved independently —
  a table-level human entry never hides column-level facts):
  1. A HUMAN entry *with* `upstream`/`inputs` and without `ignore` replaces every parsed
     fact of that level for that subject.
  2. `ignore: true` *without* `upstream`/`inputs` hides every parsed fact of that level
     for that subject; `ignore: true` *with* a list hides only parsed facts whose sorted
     list equals it exactly (an ignore that matches nothing is diagnostic SG213 WARN).
  3. Otherwise dbt and view facts are both effective; identical lists from both render
     as one line naming both sources, differing lists render once each.
  Hidden facts stay in the store and appear in `explain` output; they are never deleted.
  `lineage_edges` (§4.5) holds **effective** lineage only, recomputed for a subject in
  the same transaction whenever any lineage fact or lineage overlay entry for it changes,
  so traversal never has to filter hidden links.

### 4.2 dbt (amends base §8.4)
`import_dbt(manifest_path, store)` additionally emits:
- `LINEAGE` per model/snapshot/seed node whose relation is in the store, from
  `depends_on.nodes` mapped to relations (sources and refs; tests, macros, metrics and
  ephemeral models are not relations — an ephemeral model's own inputs are inherited by
  its consumers **[call]**).
- `COLUMN_LINEAGE` per column of such a node when `compiled_code` is present and the
  `lineage` extra is installed, parsed with the node's adapter dialect
  (`metadata.adapter_type`). A manifest without `compiled_code` (never compiled) yields
  table-level lineage only and one INFO diagnostic per import.
- Re-import retraction: lineage facts from `dbt:manifest` that the new manifest no
  longer produces are retracted, the same way overlay retraction works today (base
  §14.2, SG210 semantics).
- View re-crawl **[call, builder check]**: on each crawl that reads a view's definition,
  that view's `view_definition` lineage facts are *replaced* (old ones retracted, new
  ones written) in one transaction; a view the crawl finds dropped has its lineage
  facts retracted with the table. An unchanged definition hash skips re-parsing and
  leaves the facts as they are — only while the relation names the definition reads still
  resolve as they did when it was parsed (same relation, same columns); otherwise the view
  is re-parsed (final-review fix: an upstream entering scope or gaining a column).

### 4.3 View definitions (amends base §6.1, §6.2)
- `ReaderCapabilities` gains `view_definitions: bool`. `CatalogReader` gains an optional
  method `view_definitions(tables: Sequence[TableRef]) -> Mapping[TableRef, str]`,
  batched like `columns`. A reader that does not implement it reports `False`; the crawl
  never calls it then. User-written readers keep working unchanged.
- Shipped readers implement it from metadata only. Sources (the builder confirms each
  against the engine's documentation and the existing fixtures):

| Dialect | Source |
|---|---|
| PostgreSQL / Redshift | `pg_views.definition`, `pg_matviews.definition` (Redshift: also `SVV_ALL_*`/late-binding views where exposed) |
| DuckDB | `duckdb_views().sql` |
| Snowflake | `INFORMATION_SCHEMA.VIEWS.VIEW_DEFINITION` |
| BigQuery | `INFORMATION_SCHEMA.VIEWS.VIEW_DEFINITION`, `MATERIALIZED_VIEWS` |
| Databricks | `information_schema.views.view_definition` |
| MySQL/MariaDB | `information_schema.VIEWS.VIEW_DEFINITION` |
| ClickHouse | `system.tables.as_select` |
| SQL Server | `sys.sql_modules.definition` |
| Oracle | `ALL_VIEWS.TEXT`, `ALL_MVIEWS.QUERY` |
| Trino | `information_schema.views.view_definition` |

- Every statement passes `check_metadata_sql` (the guard learns the new catalog views;
  a test pins that each new statement passes and that no data table is named).
- Incremental crawls re-read definitions only for views changed since the cursor where
  the reader supports `changed_since`, otherwise all views in the scoped schemas.
- **Privacy:** the raw definition text is held in memory only for parsing. The store
  keeps the lineage facts, a sha256 of the definition (to skip re-parsing unchanged
  views) and, as evidence, a *template* of the definition with every literal replaced by
  a placeholder (same convention as base evidence: SQL template only). No raw
  definition is persisted, logged, rendered, put in a diagnostic or sent to an LLM.

### 4.4 Parsing (`schemaguru.lineage`, new module at layer **L3** or lower — its callers
`catalog/crawl.py` and `overlays/dbt.py` are L3; the builder updates the layer test)
- Uses `sqlglot` (extra `lineage = ["sqlglot>=30,<31"]`, the same range FactLang's own
  `sqlglot` extra uses, so the two never conflict) imported lazily.
- Dialect mapping from SchemaGuru/FactLang dialect names to sqlglot dialects; if
  FactLang exposes such a mapping in its public API, use it; otherwise SchemaGuru keeps
  its own table (the 0.1.0 ruling "FactLang public API only" stands).
- `SELECT *` and unqualified columns are expanded with the store's known columns as the
  schema; names the store does not hold go to `unresolved`, never guessed.
- Supported constructs: CTEs (incl. recursive — the recursive member's self reference is
  not an input), subqueries, `UNION [ALL]`/`INTERSECT`/`EXCEPT` (inputs merged per output
  position), joins, lateral/`UNNEST` (input is the array column, transform `expression`),
  `QUALIFY`, window functions. Anything else parses as far as sqlglot allows; a column
  it cannot attribute is `unknown`.
- Limits **[call, builder check]**: definitions over 1,000,000 characters are skipped
  before parsing. Parsing runs in a **reused worker process** (one long-lived
  `multiprocessing` worker started with the `spawn` method on every OS, fed definitions
  in batches) with a hard per-definition wall-clock cap of 5 s: on timeout the worker is
  killed and replaced and that definition gets diagnostic SG211 `timeout` and no
  lineage. A cooperative check alone is not enough — a single `sqlglot` parse cannot be
  interrupted, and the anti-goal forbids a hang. Process start-up is paid once per crawl
  or import, not per definition; the benchmark (§6) measures the total. A worker that
  dies (crash, OOM) is replaced the same way and its in-flight definition is reported. A sqlglot exception
  of any type is caught per definition, converted to a diagnostic, and never aborts the
  crawl or the import.
- New diagnostics: `SG211` WARN "lineage could not be parsed for <ref>: <reason>"
  (reason is a fixed category — `parse_error`, `too_large`, `timeout`,
  `unsupported_dialect`, `worker_died` — never SQL text); `SG212` INFO "column-level lineage needs the
  `lineage` extra (pip install 'schemaguru[lineage]')", emitted at most once per
  crawl/import; `SG213` WARN "lineage ignore entry matches no parsed link" (§4.1).

### 4.5 Store (amends base §7)
- A forward migration (SQLite `0003_lineage.sql` — `0002_scope.sql` exists; Postgres
  `pg/0002` — the Postgres `0001` already covers SQLite 1–2)
  adds a `lineage_edges` index table: `(warehouse, upstream_key, downstream_key, level ∈
  {table, column}, fact_id)` with indexes on `upstream_key` and `downstream_key`, kept in
  step with `LINEAGE`/`COLUMN_LINEAGE` facts in the same transaction. Downstream queries
  run in the store, never by scanning facts in Python (G5).
- A 0.1.0 store migrates on open; a test opens a 0.1.0 fixture store (SQLite, built
  once from tag `v0.1.0` by a committed script and committed as a fixture) and a
  0.1.0-schema Postgres store (integration job) and checks facts and views are
  unchanged.

### 4.6 Rendering (amends base §13)
- `agent` profile, table block: one line `Built from: <ref>, <ref> (+N more) · <source>`
  after the description; per column, when the column has lineage:
  `← <input>[, <input>] (<transform>)`, capped at 3 inputs with `+N more`. Unresolved
  inputs render as `<name> (not in catalog)`. Lines are marked as parsed/declared, never
  as measured; the existing provenance footer names the lineage source.
- `planner` profile: the table-level line only. `verifier` profile: keyed lines
  `LINEAGE <table> <- <refs>` and `COLUMN_LINEAGE <col> <- <refs> <transform>`.
  `router` profile: unchanged.
- A table with no lineage renders exactly as in 0.1.0 (golden views unchanged except
  where the fixture warehouse gains views/dbt lineage — those golden diffs are reviewed).
- Masking: rendered refs are identifiers, but the render sweep still runs over every
  lineage line.

### 4.7 Scoping (amends base §12)
Lineage does not add tables to a scoped view by itself **[call]**: scoping stays
question-driven. When both a model and one of its direct upstream tables are scoped,
the model's block carries `Built from` so the agent can see the relationship. There is
no "prefer the model" hint in 0.2.0 **[call, builder check]**: producing one would mean
inferring it from free text, which the spec forbids; a human can already say it in a
`PITFALL` or description overlay.

### 4.8 Agent surface (amends base §18, §19.1, §17)
- Tool `lineage`: args `ref` (table or column; resolved like other tool args, base
  §4.1), `direction ∈ {upstream, downstream}` default `upstream`, `depth` integer 1–5
  default 2, `level ∈ {table, column}` default inferred from `ref`. Returns JSON with a
  `text` field, `nodes`, `edges` (each edge with `source` and `transform`), and
  `truncated: bool` when the 200-node cap **[call]** is hit. Cycles (possible with
  mis-declared dbt or overlays) are detected and reported, never looped.
- Python: `guru.lineage(ref, direction="upstream", depth=2, level=None) -> LineageResult`.
- CLI: `schemaguru lineage REF [--downstream] [--depth N] [--level table|column]
  [--format text|json]`.
- HTTP server: `GET /lineage?ref=…&direction=…&depth=…&level=…`, same masking and error
  handling as the other endpoints.
- MCP and the framework adapters expose the tool automatically through `guru.tools()`.

### 4.9 Overlays (amends base §14.1)
```yaml
lineage:
  - table: analytics.revenue_daily
    upstream: [shop.orders, shop.refunds]
  - column: analytics.revenue_daily.net_revenue
    inputs: [shop.orders.amount, shop.refunds.amount]
    transform: aggregate
  - table: analytics.legacy_view
    upstream: [shop.orders]
    ignore: true          # hides this parsed link; human always wins
```
Validation follows the existing overlay rules (unknown refs → existing diagnostics,
duplicate keys → diagnostic, `transform` must be one of §4.1's values).

## 5. Hardening

Appendix A lists every deferred item, grouped by area, as recorded in the 0.1.0 build
ledger. Rules for the plan and its tasks:
- The source ledger is the git-ignored 0.1.0 build workspace
  `.superpowers/sdd/2026-11-24-schemaguru-plan/` (`progress.md`, `task-24-deferred.md`)
  on the maintainer's machine; Appendix A is its complete extract and is the binding
  list for builders who cannot see it.
- The first hardening task **audits** each item against current `main`: many were
  "carried into" a later 0.1.0 task and may already be fixed. An item is marked *already
  fixed* only with the commit and a test that would fail without it.
- A *fix* lands with a test that fails before the fix (red first). Masking changes keep
  the fail-safe direction: when a change trades recall for precision, recall wins
  (over-masking is acceptable; a leak is not), and the trade is documented.
- *Ruled out* items carry a one-line reason in the ledger; user-visible ones also get a
  line in the relevant guide page ("Known limits").
- Guard and InlineConnector changes may only **tighten** (anti-goal); a false-rejection
  fix (e.g. backtick quoting in the metadata guard) must come with a test proving the
  hostile form is still rejected.

## 6. Scale benchmark

### 6.1 What is measured (closes base G5 and base §22 "Scale benchmark")
A deterministic synthetic catalog generator (seeded; documented shape) produces
**500,000 tables and 25,000,000 columns** across schemas, with declared FKs, comments,
and a share of views carrying definitions and dbt-style lineage **[call: 10% views,
lineage depth up to 6]**. A fake `CatalogReader` serves it page by page. Measured:

| Metric | Target **[call: base G5 targets, plus new ones]** |
|---|---|
| Full crawl into SQLite, wall time | reported (no fixed target); regression-gated |
| Peak RSS during crawl | ≤ 2 GiB (pages are streamed; nothing holds the whole catalog) |
| Incremental crawl with 1% changed | ≤ 10% of full-crawl time |
| `guru.context(question)` latency, 200 fixed questions | p95 < 200 ms from the local store |
| Join-path search, 200 pairs | p95 reported; regression-gated |
| `guru.lineage(depth=2)` upstream and downstream, 200 refs | p95 < 50 ms |
| Store size on disk | reported |

The generator, harness and question set live in `benchmarks/`; `pytest -m benchmark`
keeps running the smaller in-suite benchmarks.

### 6.2 Results and regression gate
- Results are written as JSON (`benchmarks/results/<version>.json`: environment —
  CPU model, cores, RAM, Python, OS — plus every metric) and committed for 0.2.0.
- A `bench.yml` workflow runs nightly and on `workflow_dispatch` on `ubuntu-latest`. It
  fails when a hard target in §6.1 is missed or a regression-gated metric is more than
  25% **[call]** worse than the committed baseline for the same runner class. It is not
  a required check on PRs.
- If the full size cannot run on a GitHub-hosted runner (disk, RAM or the 6-hour job
  limit), the builder records the measured limit, runs the nightly at the largest size
  that fits, and runs the full 500k/25M size locally for the committed 0.2.0 result,
  stating the machine in the results file and docs. This is a ruling the ledger must
  record with the measurements that forced it.
- Bottlenecks found are fixed in 0.2.0 when the fix is within the existing
  architecture; anything needing an architecture change is reported in the release
  notes with the measured numbers rather than hidden.
- The docs site and wiki get a "Scale" page with the published numbers and how to
  reproduce them.

## 7. Documentation, release
- Guide pages: `lineage.md` (sources, extra, overlay, privacy of view SQL, limits),
  scale page, updates to tools/CLI/server/overlays/readers pages; README gains a short
  lineage example; CHANGELOG `0.2.0`; wiki and docs site regenerate as in 0.1.0.
- `__version__ = "0.2.0"`; tag `v0.2.0` only after CI on the tagged commit is green;
  the release workflow is unchanged (the `pypi` environment's owner approval stays).

## 8. Testing
- Lineage parser: a table-driven test per construct in §4.4 per major dialect family
  (ANSI/Postgres, Snowflake, BigQuery, T-SQL, ClickHouse), a property test that parsing
  never raises on fuzzed SQL fragments, and the limits (size, timeout) with negative
  controls.
- Privacy: a test plants PII literals in view definitions and asserts they appear
  nowhere in the store, rendered views, tool/HTTP output, diagnostics or logs.
- Readers: each shipped reader's `view_definitions` against its recorded fixtures; live
  engines in the integration CI job (DuckDB, Postgres, MySQL, ClickHouse, SQL Server,
  Trino) cover real SQL.
- dbt: fixture manifests with and without `compiled_code`, ephemeral models, re-import
  retraction.
- Anti-goals: path search, scoping and the SQL snippet never use lineage edges (a test
  with a lineage link between two otherwise unjoinable tables expects a refusal); the
  resolver never promotes lineage to VERIFIED.
- Store migration from 0.1.0 (§4.5).
- Golden views updated and reviewed.

## Appendix A — Deferred items from the 0.1.0 build (audit list)

Grouped by area; the `T<n>` tag is the 0.1.0 task that recorded it. Items marked
"carry into T<k>" may have been fixed there — the audit task checks.

**A1 PII masking** (T11, T12, final review "PII recall")
- Leaks: "Garcia,Maria", "M. Garcia", lowercase names, names not in the list, lone-surname
  possessives; addresses inside sentences not swept; "password=hunter2" (< 8 chars) not
  masked; credentials glued to word chars (`x_<key>`); `github_pat_` / `xapp-` prefixes
  not covered.
- Over-masking (fail-safe, fix only if recall is kept): zero-padded IDs as phone;
  path-like values as secret; version strings as `<ip>`; place/brand names ("Austin
  Store"); "password: varchar(255)"; `_contains` alphabet misses class-range interior
  chars.
- Hygiene: `detectors.py` fn typed `object` + `type: ignore`; document hex-needs-a-letter
  and the `<secret>`/`<address>` tokens.

**A2 Read-only guard and InlineConnector** (T4, T12, T16b, final review)
- Path check only on the first token of a FROM item; `.avro/.orc/.bz2/.gzip` not listed.
- Metadata guard: backtick quoting / `SHOW CREATE` false rejections; quote scanner ignores
  backslash escapes.
- `FactLangExecutor` does not run the local guard.
- Fullwidth `；` / `－－` pass `check_predicate` (inert in FactLang — confirm and pin).
- Residual look-alike quotes via non-Unicode Windows ODBC connections (InlineConnector).
- `tzinfo` raising during normalisation surfaces as `RuntimeError`, not SG109.

**A3 Crawl, resume, measurement** (T4, T6)
- Resume does not re-check the scope hash; schemas created before the cursor are skipped
  on resume; no temp-dir cleanup test.
- `list_schemas` ignores `page_token`.
- Explicit-refs crash pushes non-queued tables; `queue_push` failure masks the original
  exception; keyhash vs profile DISTINCT precedence by batch order; back-off reset
  untested; refill with in-flight probes overshoots (untested); fused bytes possibly
  charged N times; sampled max-per-key cardinality may say 1:1; rule-2 id↔id candidate
  noise; `flbridge` imports `factlang.semantics.refs` (not public API).

**A4 Readers** (T4, T20a, T20b, final review "reader edge cases")
- Postgres: `NOT VALID` FKs reported enforced; unique indexes not reported; integration
  test lacks partitioned / unanalysed tables; reader `row_count` estimate unlabelled
  (must be CATALOG provenance).
- Oracle: `ALL_PART_TABLES` join on nullable `c.owner`; materialized-view comments.
- Snowflake: clustering expressions as names; `ValueError` vs `CatalogError`; `SHOW` caps.
- BigQuery: `_PARTITIONTIME` pseudo-columns; cross-project `TABLE_STORAGE`; region default
  docs; FK join by constraint name.
- Redshift: re-reads schema-wide info per page; ignores `changed_since` (capabilities must
  then report `incremental=False`); `bytes_billed_is_lower_bound` duck-typed.
- Fixture handlers match SQL by substring; keyset paging vs collations.

**A5 Store and model** (T2, T3, T7)
- `from_dict` trusts the stored id; `stable_hash` Decimal/date/mixed-key normalisation;
  py3.10 `'Z'` isoformat; Config env type coercion for tuple fields; `per_glob` mutable
  in a frozen dataclass; `_value_identity` must fail loudly on a missing identity field.
- `facts_for` kind filter in Python (push to SQL + index `(subject_key, kind)`); nested
  transaction without SAVEPOINT; stale doc on subject change; fixed `.tmp` export name;
  `iter_schemas` fetchall; COMMIT failure resets depth; glossary not in search docs;
  SG702 code only in message text; stale non-empty index after A→B→A backend switch;
  read-only empty `table_docs` without diagnostic; no byte-equality test for
  `snapshot_facts` JSON; two JSON encoders (`ensure_ascii` differs).
- Status/freshness: MEASURED/CATALOG fact with `observed_at=None` treated as fresh;
  ESTIMATED never STALE; superseded ESTIMATED keeps ESTIMATED; DRAFTED ERROR/UNMEASURABLE
  without unverified marker; `diff` vs `resolve` tie-break rules differ; `diff` has no
  `checks_run` input; `subject_key` helper belongs in `model/`;
  `test_all_measured_retained_worst_warns` asserts nothing about warnings; no test for a
  sampled extra also seen exactly.

**A6 Overlays, dbt, rules** (T8, T10, T24)
- Waiver kind/subject not validated; duplicate YAML keys not diagnosed; glossary
  `maps_to` accepts column refs; `facts_to_overlay` drops non-overlay kinds silently;
  list merge by equality duplicates changed joins; `mkstemp` 0600 + check/replace window;
  JOIN values carry only cardinality; an overlay entry error retracts its previous good
  fact (document or fix).
- dbt: unique count only sees resolvable columns; `dbt.py` module docstring missing.
- Rules: merged requirements across rules uncapped; bare ref with quoted `(` rejected.

**A7 Graph and scoping** (T9, T24a)
- Per-graph memo not thread-safe (shared `Graph` under the server); hop-limit naming capped
  at 2×hop_limit; via-success search near the 1 s benchmark bound.
- Denylist misses ALLCAPS+CamelCase (`ZORBClient`); qualified `t.isDeleted` left bare;
  ambiguous case-insensitive match left bare.

**A9 0.1.1 patch review and check_fact** (added 2026-11-25 after the 0.1.1 FactLang 0.1.3 patch)
- `InlineConnector` class docstring omits the trino `… UTC` form and the databricks
  zone-less refusal; FL507 tests assert only `SG109`; `test_trino_rejects_the_old_timestamp_form`
  mixes two assertions; `literal_refusal` became a public name in 0.1.1 (keep — removing a
  released name breaks §3).
- `check_fact` has no claim cap: SchemaGuru calls FactLang's `Checker.check` directly (to keep
  its own evidence scrubbing) and so bypasses FactLang 0.1.3's `max_claims` (default 200).
  Add a SchemaGuru-side cap with the same default, refused before any warehouse spend, as a
  tool error with code `too_many_claims` (FactLang's stable code), configurable, `None`
  disables.

**A8 CLI, docs, tests, CI** (T1, T19, T23)
- `--format json` errors are plain text on stderr (no JSON error object).
- Docs: Sampling page states the rows threshold only when bytes are unknown — clarify;
  owner name in wiki footer (intended — the owner is the named MIT copyright holder; no
  change **[call]**); clearer error if the wiki repo is missing.
- Tests: wiki-facts negative controls only exercise helpers on synthetic input; layer
  walker wiring not proven by a tmp-tree control; facade import reported as "no layer";
  no-op set comprehension in `test_layers`; nested `TYPE_CHECKING` handling and the I2
  guard lack negative controls on the real tree; `tests/test_version.py` `__all__`
  assertion (check still valid).
- CI: the denylist job passes silently when the secret is empty on a fork (decide: fail
  on `main`, skip on forks).
