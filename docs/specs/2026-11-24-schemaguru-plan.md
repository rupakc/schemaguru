# SchemaGuru v0.1.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build SchemaGuru — discover, measure, derive and serve warehouse schema intelligence to AI agents — with CI, PyPI release, docs site and wiki.

**Architecture:** Library (layers L0–L8, spec §5.1) over a local knowledge store (SQLite default, Postgres optional), fed by catalog readers, FactLang-backed measurement (exact; sampled for large tables), an LLM drafting pipeline and human YAML overlays; served as question-scoped views, tools, MCP and an optional HTTP server.

**Tech Stack:** Python 3.10–3.13, hatchling, factlang, PyYAML, sqlite3 (FTS5), pytest, hypothesis, ruff, mypy --strict, duckdb (tests), GitHub Actions, mkdocs-material + mkdocstrings.

**Spec:** `docs/specs/2026-11-24-schemaguru-design.md` — every implementer reads the spec sections named in its task (§n refers to it). FactLang's spec (`github.com/rupakc/factlang`, `docs/specs/2026-11-24-factlang-design.md`, incl. §18 Amendment A and §19 Amendment B) defines every FactLang API used here.

## Global Constraints

- Package `schemaguru`, `src/` layout, version `0.1.0` in `src/schemaguru/__about__.py`.
- Python `>=3.10`; CI matrix 3.10, 3.11, 3.12, 3.13 × ubuntu, macos, windows.
- Runtime dependencies exactly: `factlang>=0.1.2,<0.2`, `PyYAML>=6`. Everything else is an extra imported lazily inside functions. FactLang comes from PyPI via the pyproject pin.
- **FactLang seam:** only `src/schemaguru/measure/flbridge.py`, `src/schemaguru/catalog/executor.py` and `src/schemaguru/render/fragments.py` import `factlang`; everything else uses SchemaGuru types. Enforced by `tests/architecture/test_layers.py::test_factlang_seam`.
- **SchemaGuru never writes measurement SQL** (spec §5.2). The only SQL in `src/` is catalog-metadata SQL in `catalog/readers/*.py`, with bind parameters for every value.
- Layer rule spec §5.1; `mypy --strict src`, `ruff check`, `ruff format --check` blocking.
- Unknown is `None`, never 0/1.0/default (spec §25.1). Timestamps tz-aware UTC; naive rejected.
- Diagnostic codes `SGnnn` in the ranges of spec §20.
- Generic shop domain only: `customers, orders, order_items, products, payments, events, accounts, users, sessions`.
- No mention anywhere of the originating company, product, datasets or table names (denylist test, spec §22).
- MIT, `Copyright (c) 2026 Rupak Chakraborty`. Git author `Rupak Chakraborty <rupakc@users.noreply.github.com>` (repo-local config, already set).
- Conventional commits; each commit leaves pytest green; each message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Stage explicit paths only (never `git add -A`). Use `python3` and a venv at `~/schemaguru/.venv` (gitignored).

## Review Focus

1. **A catalog of 500k tables / 25M columns** — crawl memory stays bounded by page size; search p95 < 200 ms from SQLite (Task 4 `test_crawl_memory_bounded`, Task 13 `test_search_latency_500k`, nightly benchmark).
2. **Identifiers that differ only in case, contain spaces/quotes/dots, or are reserved words** — stored exactly, resolved uniquely or reported ambiguous, rendered correctly quoted (Task 2 `test_ref_exact_identity`, Task 8 `test_overlay_key_ambiguous_case`, Task 14 `test_render_quotes_odd_identifiers`).
3. **A table that vanishes or errors mid-crawl / mid-measure** — one item lost, run continues, report names it; withdrawn objects never deleted with their overlays (Task 4 `test_page_error_isolated`, `test_withdrawn_soft`, Task 6 `test_probe_error_isolated`).
4. **Personal data in sample values on the way to an LLM** — default mode never sends an unmasked email/phone/IBAN/card/hex token, including inside free text and in error messages (Task 11 `test_sweep_catches_embedded_pii`, Task 12 `test_draft_payload_masked`).
5. **A sampled number presented as exact, or a sample used to confirm** — sampled facts are `ESTIMATED`, rendered with "≈ … (sampled …)", never `VERIFIED` (Task 6 `test_sampled_never_verified`, Task 14 `test_render_estimated_marker`).

---

## FactLang availability (read before Tasks 4, 6, 9, 14)

FactLang is being built concurrently. SchemaGuru therefore talks to it through one seam
(`measure/flbridge.py` + `catalog/executor.py` + `render/fragments.py`) behind SchemaGuru
protocols (`MeasureBackend`, `MetadataExecutor`, `FragmentRenderer`). Every task tests
against **fakes** of those protocols. Each seam module also has integration tests marked
`@pytest.mark.factlang` that call the real FactLang and are **skipped with reason
"factlang capability <name> not yet available"** when `flbridge.capabilities()` lacks it.
When FactLang publishes the capability, the skip lifts automatically; Task 25 removes all
remaining skips before release.

## Dependency graph (fan-out)

```
T1 → T2 → T3 → {T4, T5, T7, T8, T11} (parallel)
T4 + T5 → T6 ;  T6 + T7 → T9 → T10
T11 + T6 → T12 ;  T9 → T13 → T14
T6 + T7 → T15 ;  T3 → T16
{T8, T10, T12, T14, T15, T16} → T17 → {T18, T19} (parallel)
T4 → T20 (readers, parallel sub-tasks per dialect) ;  T17 → T21
T19 + T20 + T21 → {T22, T23} (parallel) → T24 (acceptance) → T25 (release; gated on FactLang 0.1.0)
```

---

### Task 1: Repository skeleton, CI, Claude scaffolding

**Files:** Create `pyproject.toml`, `LICENSE`, `README.md` (stub), `CHANGELOG.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), `SECURITY.md`, `.gitignore` (incl. `.venv/`, `.schemaguru/`, `.superpowers/`, `dist/`), `src/schemaguru/{__init__,__about__}.py`, `src/schemaguru/py.typed`, `CLAUDE.md`, `AGENTS.md`, `MEMORY.md`, `.claude/agents/{goldfish-implementer,goldfish-spec-reviewer,warehouse-reader-engineer,graph-algorithms-reviewer,pii-privacy-reviewer,docs-writer,release-engineer}.md`, `.claude/skills/{add-warehouse-reader,add-fact-kind,add-detector,run-fixture-warehouse,release,egm-handoff,self-review}/SKILL.md`, `.github/workflows/ci.yml`, `tests/architecture/{__init__,test_layers,test_denylist}.py`, `tests/architecture/denylist.txt` is **not** committed — the denylist is read from env `SCHEMAGURU_DENYLIST` (CI secret, newline-separated) and the test skips when unset locally.

**Interfaces — Produces:** `schemaguru.__version__ == "0.1.0"`; `tests/architecture/test_layers.py` exposing `LAYERS: dict[str, int]` (module prefix → layer, from spec §5.1: `__about__/errors/diagnostics/_util`→0, `model`→1, `store`,`config`→2, `catalog`,`pii`,`overlays`→3, `measure`,`knowledge`→4, `graph`,`draft`→5, `scope`,`render`,`refresh`,`feedback`→6, `guru`,`agent`,`cli`→7, `server`→8) and `FACTLANG_SEAM = {"schemaguru.measure.flbridge", "schemaguru.catalog.executor", "schemaguru.render.fragments"}`.

- [ ] **Step 1:** `pyproject.toml`: hatchling; `requires-python=">=3.10"`; `dependencies = ["factlang>=0.1,<0.2", "PyYAML>=6"]`; extras: `bigquery=["google-cloud-bigquery>=3"]`, `snowflake=["snowflake-connector-python>=3"]`, `redshift=["redshift-connector>=2"]`, `databricks=["databricks-sql-connector>=3"]`, `postgres=["psycopg[binary]>=3.1"]`, `duckdb=["duckdb>=1.0"]`, `clickhouse=["clickhouse-connect>=0.7"]`, `mssql=["pyodbc>=5"]`, `oracle=["oracledb>=2"]`, `trino=["trino>=0.330"]`, `mysql=["pymysql>=1.1"]`, `postgres-store=["psycopg[binary]>=3.1"]`, `anthropic=["anthropic>=0.40"]`, `openai=["openai>=1.50"]`, `embeddings=["numpy>=1.26"]`, `tiktoken=["tiktoken>=0.7"]`, `mcp=["mcp>=1.0"]`, `server=["starlette>=0.37","uvicorn>=0.30"]`, `langchain=["langchain-core>=0.3"]`, `llamaindex=["llama-index-core>=0.11"]`, `openai-agents=["openai-agents>=0.1"]`, `all`=union, `dev=["pytest>=8","pytest-cov","pytest-asyncio>=0.23","hypothesis>=6","ruff","mypy","types-PyYAML","build","twine","duckdb>=1.0"]`, `docs=["mkdocs-material","mkdocstrings[python]"]`; `[project.scripts] schemaguru="schemaguru.cli:main"`; ruff (line 100, py310, `E,F,W,I,B,UP,SIM,RUF`); mypy strict; pytest markers `integration`, `live`, `factlang`, `benchmark`; coverage `fail_under=90` over `src/schemaguru` omitting `catalog/readers/{bigquery,snowflake,redshift,databricks,oracle}.py`, `server/*`, `agent/mcp.py`, `agent/adapters/*`, `draft/providers/{anthropic,openai_compat}.py`.
- [ ] **Step 2:** `test_layers.py`: `test_layering` (module-level `schemaguru.*` imports go to a lower layer or the same package), `test_factlang_seam` (only `FACTLANG_SEAM` modules import `factlang` at any level), `test_optional_deps_lazy` (third-party imports other than `factlang`/`yaml` appear only inside function bodies); each with a negative control writing a violating temp module. `test_denylist.py`: case-insensitive scan of every tracked text file for each denylist entry; skip when `SCHEMAGURU_DENYLIST` unset.
- [ ] **Step 3:** `ci.yml` (push to main + PR): `lint` (ruff check, ruff format --check, mypy --strict src), `test` (matrix; `pip install -e .[dev]`; `pytest -m "not integration and not live and not benchmark" --cov`), `denylist` (ubuntu; env from secret `SCHEMAGURU_DENYLIST`), `build` (needs lint+test; `python -m build`; `twine check dist/*`; upload artifact `dist`).
- [ ] **Step 4:** Claude scaffolding. `CLAUDE.md`: 5-line summary, commands, Global Constraints verbatim, the §5.1 layer table, FactLang seam rule, the §25 checklist, EGM workflow (§24). `AGENTS.md`: same for any agent + one paragraph per role. `MEMORY.md`: format header + first entries (approach A+C, FactLang dependency + Amendments A/B, LLM-drafts/human-wins, masked sampling default, large-table sampling, standalone). Agents: frontmatter (`name`, `description`, `tools`) + inputs/outputs/hard rules (goldfish: "spec + task + repo only; missing information → stop and report the gap"). Skills: checklist per operation (e.g. `add-warehouse-reader`: one module in `catalog/readers/`, recorded metadata fixture JSON, register in `catalog/registry.py`, conformance test param, wiki Warehouse Guide page).
- [ ] **Step 5:** `pip install -e .[dev] && pytest && ruff check && ruff format --check && mypy --strict src` → green. Commit `chore: repository skeleton, CI and agent scaffolding`; push; `gh run watch` green.

---

### Task 2: Model, diagnostics, errors, config

**Files:** Create `src/schemaguru/{errors,diagnostics}.py`, `src/schemaguru/_util/{__init__,hashing,timeutil}.py`, `src/schemaguru/model/{__init__,refs,facts,kinds,joins,chains,evidence}.py`, `src/schemaguru/config.py`; Test `tests/unit/model/test_refs.py`, `test_facts.py`, `test_kinds.py`, `tests/unit/test_config.py`, `tests/property/test_model_props.py`.

**Interfaces — Produces:**
- `errors.py`: `SchemaGuruError(Exception)`, `StoreVersionError`, `ConfigError`.
- `diagnostics.py`: `Severity(Enum)` `ERROR WARNING INFO`; `Diagnostic(code: str, severity, message: str, location: str | None = None, hint: str | None = None)` frozen, `.to_dict()`; `DiagnosticBag` (`add`, `extend`, `errors()`, `__iter__`, `to_list()`); `CODES: Mapping[str, str]` (code → one-line meaning) — the single registry later tasks append to by editing this file.
- `_util/hashing.py`: `stable_hash(obj: object) -> str` = `"sha256:" + hexdigest` of `json.dumps(obj, sort_keys=True, separators=(",",":"), default=_canon)` (Decimal→str, datetime→ISO-UTC, tuple→list). `_util/timeutil.py`: `utcnow() -> datetime`, `require_aware(dt) -> datetime` (raises `ValueError` on naive).
- `model/refs.py`: `ObjectRef(catalog: str | None, schema: str | None, table: str, column: str | None = None)` frozen; `.table_ref() -> ObjectRef`; `.canonical() -> str` (parts joined by `.`, a part is wrapped in `"` with `""` escaping iff it is not `[A-Za-z_][A-Za-z0-9_$]*`); `ObjectRef.parse(text) -> ObjectRef` (inverse of canonical; 2–4 parts as spec §4.1; raises `ValueError`); equality is exact (spec §4.1). `WarehouseId(name: str, dialect: str)`.
- `model/kinds.py`: `FactKind` — a registry of `KindSpec(name: str, subject: Literal["table","column","join","global"], meaning: bool, expectation: bool, identity_fields: tuple[str, ...] = ())` (for measured kinds the identity fields exclude the numbers — e.g. `JOIN` → `()` over `subject` only, `VALUES` → `("case_sensitive","split")`); built-ins exactly the spec §4.3 table (`DESCRIPTION USE_CASES ENTITY_TYPE PRIMARY_KEY JOIN VALUES NULL_RATE ROW_COUNT DISTINCT FRESHNESS TYPE_NOTE SOFT_DELETE PII PITFALL GLOSSARY METRIC NEVER_QUERY WAYPOINT_RULE`); meaning kinds = `DESCRIPTION USE_CASES ENTITY_TYPE PITFALL GLOSSARY`; expectation kinds = `VALUES JOIN PRIMARY_KEY SOFT_DELETE`. `register_fact_kind(spec: KindSpec, *, replace: bool = False) -> None`; `get_kind(name) -> KindSpec`.
- `model/facts.py`: `Provenance(Enum)` `CATALOG DECLARED MEASURED DRAFTED HUMAN`; `Status(Enum)` `OBSERVED VERIFIED CONTRADICTED UNVERIFIED ESTIMATED UNMEASURABLE ERROR STALE WAIVED`; `Fact` exactly spec §4.3 fields (`subject: ObjectRef | JoinRef`, `value: Mapping[str, Any]`) frozen, `__post_init__` enforces: `observed_at` aware or None; `method` non-None when `provenance is MEASURED`; `status is ESTIMATED` ⇒ `method.startswith("sampled:")`; `status is VERIFIED` ⇒ `method == "exact"` or provenance HUMAN/DECLARED with evidence. `Fact.make(kind, subject, value, provenance, status, **kw) -> Fact` computes `id = stable_hash([kind, subject.canonical(), provenance.value, value_identity])` where `value_identity` is the value minus measurement numbers for measured kinds (so re-measuring updates the same fact id) — the identity keys per kind are `KindSpec.identity_fields: tuple[str, ...]` (default `()` = whole value). `to_dict()/from_dict()` round-trip.
- `model/joins.py`: `JoinRef(left: tuple[ObjectRef, ...], right: tuple[ObjectRef, ...], cast_to: str | None = None)` (non-empty, equal length, each side one table), `.canonical()` = `"<left cols> -> <right cols>"`, `.reversed()`; `JOIN_VALUE_KEYS` = the spec §4.5 payload keys; `support(value: Mapping, *, reverse: bool) -> float | None`; `join_verdict(match_rate: float | None, ok: float, weak: float) -> Literal["OK","WEAK","BROKEN"] | None`.
- `model/chains.py`: `Step = JoinRef | FilterStep(table: ObjectRef, filter_text: str) | NoteStep(text: str)`; `Chain(name: str, steps: tuple[Step, ...], because: str)`.
- `model/evidence.py`: `Evidence(claim_text: str | None, verdict: str | None, sql: str | None, params: tuple, dialect: str | None, tier: str | None, duration_ms: float | None, bytes_processed: int | None, sample: Mapping[str, Any] | None, raw: Mapping[str, Any])`.
- `config.py` (layer 2): frozen dataclasses `Budget(max_bytes_per_probe: int | None = 1<<30, max_bytes_total: int | None = 10<<30, max_probes: int | None = None, max_seconds: float | None = None, crawl_bytes: int | None = None, draft_budget_tokens: int | None = None)`; `SamplingConfig(llm_values: Literal["masked","raw","none"] = "masked", per_glob: Mapping[str, str] = {}, sample_values_per_column: int = 5, store_raw_samples: bool = False, measurement: Literal["auto","off","force"] = "auto", sample_threshold_bytes: int = 50<<30, sample_threshold_rows: int = 500_000_000, exact_key_limit: int = 5_000_000, block_fraction_floor: float = 0.001, block_fraction_ceiling: float = 0.10, design_effect: float = 2.0)`; `GraphConfig(w_support=1.0, w_fanout=0.35, w_hub=0.25, unmeasured_penalty=1.0, hop_limit=6, thin_support=0.05, attribution_fanin=10.0, k_paths=3, include_unmeasured=False, ok_threshold=0.95, weak_threshold=0.05)`; `ScopeConfig(seed_k=8, max_expansion_tables=6, max_values_shown=6, cache_entries=256)`; `CrawlConfig(page_size=1000, column_batch=200, workers=4, timeout_s=60.0, include=(), exclude=("information_schema.*","pg_catalog.*","sys.*","*.*_staging*","*.*tmp*"))`; `Config(budget, sampling, graph, scope, crawl, enum_max_distinct=20, fact_ttl_days=14, measure_on_demand: Literal["queue","background","block"]="queue", store_path: str | None = None, llm_model: str = "claude-sonnet-5-5")`; `Config.from_mapping(d)`, `Config.from_env(prefix="SCHEMAGURU_")`, `Config.from_yaml(path)` — unknown keys → `ConfigError` naming the key.

- [ ] **Steps:** tests first: `test_ref_exact_identity` (`ObjectRef(None,"s","Orders") != ObjectRef(None,"s","orders")`), `test_canonical_roundtrip` (incl. `"Order Items"`, `a"b`, a dot in a name), `test_fact_naive_timestamp_rejected`, `test_measured_requires_method`, `test_estimated_requires_sampled_method`, `test_fact_id_stable_across_remeasure` (same JOIN subject, different match_rate → same id), `test_join_support_direction`, `test_join_verdict_thresholds` (0.95→OK, 0.05→WEAK, 0.049→BROKEN, None→None), `test_config_unknown_key`, property `test_fact_dict_roundtrip`. Implement; green; commit `feat(model): knowledge model, diagnostics and configuration`.

---

### Task 3: Store (SQLite) and migrations

**Files:** Create `src/schemaguru/store/{__init__,protocol,sqlite,schema_sql,export}.py`, `src/schemaguru/store/migrations/0001_initial.sql`; Test `tests/unit/store/test_sqlite.py`, `test_export.py`, `test_migrations.py`.

**Interfaces — Consumes:** Task 2 model. **Produces:**
- `store/protocol.py`: `Store` Protocol: `upsert_schemas(items: Iterable[Schema])`, `upsert_tables(items: Iterable[Table])`, `upsert_columns(table: ObjectRef, cols: Sequence[Column])`, `upsert_constraints(table: ObjectRef, cons: Sequence[Constraint])`, `mark_withdrawn(refs: Iterable[ObjectRef], at: datetime)`, `table(ref) -> Table | None`, `columns(ref) -> list[Column]`, `iter_tables(*, include_withdrawn=False) -> Iterator[Table]` (streaming), `resolve(text: str) -> tuple[ObjectRef | None, list[ObjectRef]]` (exact, then unique case-insensitive; returns candidates when ambiguous), `put_facts(facts: Iterable[Fact])`, `facts_for(subjects: Iterable[ObjectRef | JoinRef] | None, kinds: Iterable[str] | None) -> list[Fact]`, `search(query: str, k: int) -> list[tuple[ObjectRef, float]]`, `snapshot(label: str | None = None) -> str` (returns snapshot id; stores `checks_run`), `snapshot_facts(snapshot_id) -> list[Fact]`, `latest_snapshot() -> str | None`, `crawl_state_get(key) / crawl_state_set(key, value: Mapping)`, `ledger_add(kind: str, amount: int, unit: Literal["bytes","tokens","queries"], note: str)`, `ledger_total(kind, unit, since: datetime | None) -> int`, `queue_push(table: ObjectRef, reason: str, priority: float)`, `queue_pop(n: int) -> list[tuple[ObjectRef, str]]`, `feedback_add(table, column, failure_class)`, `feedback_summary() -> list[dict]`, `transaction() -> ContextManager`. Structural dataclasses `Schema`, `Table`, `Column`, `Constraint` exactly spec §4.2 live in `model/` (add `model/structure.py` in this task) with `withdrawn_at: datetime | None` on `Table`.
- `store/sqlite.py`: `SQLiteStore(path: str | os.PathLike, *, readonly=False)` — WAL; single writer connection guarded by a `threading.Lock`; FTS5 table `table_docs(ref, name_tokens, columns, text)` with `bm25`; if FTS5 is unavailable, `search` falls back to LIKE over a `docs` table and emits warning `SG701`. `name_tokens` splits snake/camel case (`customerOrderId` → `customer order id`).
- Migrations: numbered `.sql` files applied in order on open; `schema_version` table; opening a store whose version > library's → `StoreVersionError` (`SG702`).
- `store/export.py`: `export_json(store, path)` / `import_json(store, path)` — sorted, byte-deterministic.

- [ ] **Steps:** tests: `test_upsert_idempotent`, `test_resolve_exact_then_unique_ci`, `test_resolve_ambiguous_returns_candidates`, `test_search_ranks_name_hits_first`, `test_search_splits_camel_snake`, `test_fts5_fallback` (monkeypatch FTS5 probe → LIKE path, SG701), `test_snapshot_and_facts`, `test_queue_priority_order`, `test_ledger_totals`, `test_newer_store_refused`, `test_export_byte_deterministic` (export twice → identical bytes; insertion order shuffled), `test_concurrent_writers_serialised` (4 threads × 500 upserts, no `database is locked`). Implement; commit `feat(store): SQLite knowledge store with search, snapshots and migrations`.

---

### Task 4: Catalog protocol, executor seam, DuckDB + Postgres readers, crawl

**Files:** Create `src/schemaguru/catalog/{__init__,protocol,executor,registry,crawl,filters}.py`, `src/schemaguru/catalog/readers/{__init__,duckdb,postgres}.py`; Test `tests/unit/catalog/test_crawl.py`, `test_filters.py`, `test_reader_duckdb.py`, `tests/integration/catalog/test_reader_postgres.py`, `tests/unit/catalog/fake_reader.py` (a `FakeReader` generating N synthetic tables lazily), `benchmarks/bench_crawl.py`.

**Interfaces — Produces:**
- `catalog/protocol.py`: `Page[T](items: list[T], next_token: str | None)`; `ReaderCapabilities(comments: bool, stats: bool, last_altered: bool, constraints: bool, incremental: bool, cost_estimate: bool)`; `CatalogReader` Protocol exactly spec §6.1 (refs are `ObjectRef`).
- `catalog/executor.py` (FactLang seam): `MetadataExecutor` Protocol `run(sql: str, params: Sequence[Any] = (), *, timeout_s: float | None = None) -> list[dict[str, Any]]`; `FactLangExecutor(connector)` → `factlang.connectors.run_metadata`; `DBAPIExecutor(connection, paramstyle: str = "qmark")` (convenience for hosts without FactLang connectors; same SELECT/WITH/SHOW/DESCRIBE guard as FactLang R8, raising `SchemaGuruError` code `SG104`). Readers write SQL with `?` placeholders; executors convert to their paramstyle.
- `catalog/registry.py`: `register_reader(dialect: str, factory: Callable[[MetadataExecutor], CatalogReader])`, `reader_for(dialect: str, executor) -> CatalogReader` (unknown → `SchemaGuruError` `SG101` listing dialects).
- `catalog/filters.py`: `ScopeFilter(include: Sequence[str], exclude: Sequence[str])` with `.allows(ref: ObjectRef) -> bool` — `fnmatch` on `catalog.schema.table` and `schema.table`, case-sensitive.
- `catalog/crawl.py`: `crawl(reader, store, *, config: CrawlConfig, resume: bool = True, incremental: bool = True, now: Callable[[], datetime] = utcnow) -> CrawlReport` — pages of `page_size` tables, column/constraint batches of `column_batch`, thread pool of `workers`, checkpoint (`crawl_state`) after each page, `changed_since` when the reader supports it plus a name-only listing to detect drops (`mark_withdrawn`), page/schema errors recorded and skipped. `CrawlReport(tables_seen, tables_written, withdrawn, errors: list[Diagnostic], queries: int, bytes_billed: int | None, duration_s)`.
- Readers `duckdb` and `postgres` (spec §6.2: Postgres via `pg_catalog`, `reltuples`, `pg_description`; DuckDB via `duckdb_tables()/duckdb_columns()/duckdb_constraints()`).

- [ ] **Steps:** `test_crawl_pages_and_checkpoints` (FakeReader 5,000 tables, interrupt after page 2 via injected exception, resume → all 5,000, no duplicates), `test_page_error_isolated` (one schema raises → others written, error in report), `test_withdrawn_soft` (second crawl without a table → `withdrawn_at` set, facts and overlays untouched), `test_incremental_uses_changed_since`, `test_scope_filter_defaults_exclude_staging`, `test_crawl_memory_bounded` (FakeReader 200k tables, `tracemalloc` peak < 64 MiB), `test_reader_duckdb` (fixture DB with PK/FK/comments → exact `Table/Column/Constraint` objects; quoted table `"Order Items"`), postgres integration (marked, service container); `benchmarks/bench_crawl.py` (500k tables, prints tables/s, peak MB). Commit `feat(catalog): reader protocol, DuckDB/Postgres readers and resumable crawl`.

---

### Task 5: Fixture shop warehouse with known traps

**Files:** Create `tests/fixtures/shop/{__init__,build}.py`, `tests/fixtures/shop/EXPECTED.md` (human-readable list of the traps and their exact answers), `tests/conftest.py` (fixtures `shop_duckdb`, `shop_store`).

**Interfaces — Produces:** `build_shop(conn, dialect: Literal["duckdb","postgres"]) -> ShopFacts` where `ShopFacts` is a frozen dataclass of the exact expected answers. Tables (schema `shop`) and traps, each with exact counts in `ShopFacts`:
`customers` (1,000 rows, PK `customer_id`; `email` holds emails; `segment` enum `'retail','wholesale','vip'`), `orders` (10,000; PK `order_id`; `customer_id` → customers, 9,980 match + 20 orphans; `status` `'paid','refunded','cancelled'` plus 412 rows `'PAID'` (case drift); `created_at` STRING ISO-8601; `deleted` BOOLEAN TRUE or NULL, never FALSE, 300 TRUE), `order_items` (composite PK `(order_id, line_no)`), `products`, `payments` (`order_id` → orders thin-but-correct: covers 5% of orders, match 1.0), `events` (`session_id` → sessions; 0% direct match to orders), `sessions`, `accounts` (hub entity: sink reached by `orders.account_id` fan-in 40 and `events.account_id` fan-in 60 — the cartesian trap), `ref_parent`/`ref_child` (asymmetric join: forward 1.0, coverage 0.003), `broken_child` (`parent_id` match 0.01 → BROKEN), `empty_t` (0 rows), `all_null_t` (`c` all NULL), `anon_enum_t` (`kind` 5 distinct 32-hex@anon.example values over 10,000 rows — looks like identity by shape, enum by cardinality), `big_t` (2,000,000 rows, used with `sampling="force"`).
- [ ] **Steps:** build is deterministic (seeded `random.Random(7)`); `test_shop_builds_and_matches_expected` asserts every `ShopFacts` number by direct SQL on DuckDB. Commit `test: fixture shop warehouse with known traps`.

---

### Task 6: Measurement — requests, FactLang bridge, budget, sampling, scheduler, join candidates

**Files:** Create `src/schemaguru/measure/{__init__,requests,backend,flbridge,budget,sampling,estimates,scheduler,candidates,convert,catalog_bridge}.py`; Test `tests/unit/measure/test_{budget,sampling,estimates,scheduler,candidates,convert}.py`, `tests/unit/measure/fake_backend.py`, `tests/integration/measure/test_flbridge_duckdb.py` (`@pytest.mark.factlang`).

**Interfaces — Consumes:** Store (T3), model (T2), `ShopFacts` (T5). **Produces:**
- `measure/requests.py`: SchemaGuru-level measurement requests (frozen): `ProfileColumns(table: ObjectRef, columns: tuple[str, ...])` (row_count, non_null, distinct per column), `TopValues(column: ObjectRef, k: int)`, `KeyOverlapReq(join: JoinRef, multiplicity: bool = True)`, `Uniqueness(table: ObjectRef, columns: tuple[str, ...])`, `Freshness(column: ObjectRef)`, `PatternRate(column: ObjectRef, pattern: str)`, `RowCount(table: ObjectRef)`; each has `sample: SampleChoice | None` where `SampleChoice(method: Literal["keyhash","system","bernoulli"], fraction: Decimal, seed: int, key: tuple[str, ...] = ())`.
- `measure/backend.py`: `MeasureResult(request, payload: Mapping[str, Any] | None, evidence: Evidence | None, error: Diagnostic | None)`; `CostEstimate(request, bytes: int | None, method: Literal["dry_run","heuristic","unknown"])`; `MeasureBackend` Protocol: `measure(reqs: Sequence[Request]) -> list[MeasureResult]`, `estimate(reqs) -> list[CostEstimate]`, `check(claims: str) -> list[Evidence]`, `capabilities() -> frozenset[str]`.
- `measure/flbridge.py` (seam): `FactLangBackend(connector, *, catalog: factlang.Catalog)` translating requests to FactLang IR (`ColumnAggregate`, `ValueMembership(declared=())`, `KeyOverlap` with key tuples + `right_sample`, `CompositeKeyAggregate`, `PatternCoverage`) and `SampleChoice` → `SampleSpec`; calls `Checker.measure/estimate/check`; `capabilities()` reports which of `measure, estimate, sample, composite_join, run_metadata, render_join, parse_filter` the installed FactLang exposes (feature detection by attribute presence). `measure/catalog_bridge.py`: `StoreCatalog(store)` implementing FactLang's `Catalog` protocol from crawled columns (`None` for un-crawled), spec §5.2.
- `measure/budget.py`: `BudgetGuard(budget: Budget, store)` with `admit(estimate: CostEstimate) -> Literal["run","over_probe","over_total"]`, `record(result)` (writes ledger), `remaining_bytes() -> int | None`.
- `measure/sampling.py`: `choose_sample(table: Table, req: Request, cfg: SamplingConfig, *, key_distinct: int | None, est_bytes: int | None, max_bytes_per_probe: int | None, seed: int) -> SampleChoice | None` implementing spec §8.6 exactly: exact when not large or `measurement="off"`; never for `Freshness`/`RowCount`; keyhash for `KeyOverlapReq`/`Uniqueness`/distinct-of-key when `key_distinct > exact_key_limit` (fraction = `exact_key_limit / key_distinct`, same fraction+seed both sides); else `system` (or `bernoulli` when the backend lacks system for the dialect) with fraction = clamp(`max_bytes_per_probe / est_bytes`, floor, ceiling); `measurement="force"` samples every table at the floor-or-keyhash rule. `seed_for(table: ObjectRef, snapshot_id: str) -> int` = first 8 bytes of sha256 as int.
- `measure/estimates.py`: `wilson(successes: int, n: int, z: float = 1.96, design_effect: float = 1.0) -> tuple[float, float] | None` (None when n == 0; effective n = n / design_effect); `scale_count(count: int, fraction: Decimal) -> int`; `EstimatedValue(value: float | int | None, low: float | None, high: float | None, method: str, lower_bound: bool)`.
- `measure/convert.py`: `to_facts(result: MeasureResult, *, observed_at: datetime, snapshot_id: str, config: Config) -> list[Fact]` — exact → `OBSERVED` (method `"exact"`); sampled → `ESTIMATED` with `value["estimate"]` = `EstimatedValue.__dict__`, distinct/max as `lower_bound=True`; join payload → `JOIN` fact with every spec §4.5 key (missing ⇒ None) and `verdict = join_verdict(...)`; enum discovery: `TopValues` only issued when `distinct ≤ enum_max_distinct`, observed list `[{value,count}]`; error → `ERROR` fact with `value={"reason": code}`; empty table → `UNMEASURABLE`; all-null column → `NULL_RATE` 1.0 + `value["dead"]=True`.
- `measure/candidates.py`: `join_candidates(store, table: ObjectRef, *, max_per_column: int = 5, same_catalog_only: bool = True) -> list[tuple[JoinRef, str]]` (source label ∈ `constraint, overlay, dbt, heuristic, llm`) — heuristics exactly spec §8.4.3; cross-family types get `cast_to="string"`.
- `measure/scheduler.py`: `Scheduler(store, backend, config, *, now=utcnow)`; `plan_table(ref) -> list[Request]`; `run(refs: Sequence[ObjectRef] | None = None, *, drain_queue: bool = False) -> MeasureReport` — estimate → admit → measure (batched per table so the backend can fuse) → convert → `put_facts`; stops at total budget and reports the remaining queue. `MeasureReport(measured_tables, facts_written, over_budget: list[ObjectRef], errors: list[Diagnostic], bytes, sampled_tables)`. `BackgroundDrainer(scheduler, interval_s=5.0)` daemon thread used only when `measure_on_demand="background"`.

- [ ] **Steps (unit, with `FakeBackend` returning `ShopFacts`-consistent payloads):** `test_exact_small_table_observed`, `test_large_table_uses_system_sample`, `test_large_key_uses_keyhash_same_seed_both_sides`, `test_freshness_never_sampled`, `test_sampling_off_over_budget_is_error`, `test_sampled_never_verified`, `test_wilson_interval_values` (`wilson(50,1000)` ≈ (0.0382, 0.0653) to 4 dp), `test_distinct_from_sample_is_lower_bound`, `test_probe_error_isolated`, `test_budget_stops_and_reports_queue`, `test_join_fact_has_all_keys_none_when_missing`, `test_enum_discovery_only_below_threshold`, `test_candidates_heuristic_and_cast`, `test_candidate_never_becomes_fact_unmeasured`.
- [ ] **Integration (`factlang`-marked, auto-skip on missing capability):** on `shop_duckdb` via `FactLangBackend`: `test_shop_join_numbers_exact` (orders→customers match 0.998; ref_child coverage 0.003), `test_shop_case_drift_values`, `test_big_t_sampled_estimate_within_interval` (`sampling="force"`; true null rate inside the Wilson interval).
- [ ] Commit `feat(measure): FactLang-backed measurement with budgets, sampling and join candidates`.

---

### Task 7: Knowledge resolution, snapshots, diffs

**Files:** Create `src/schemaguru/knowledge/{__init__,resolve,snapshot,diff}.py`; Test `tests/unit/knowledge/test_resolve.py`, `test_diff.py`, `tests/property/test_resolve_props.py`.

**Interfaces — Produces:** `Effective(kind, subject, primary: Fact | None, expectation: Fact | None, observed: Fact | None, contradictions: tuple[str, ...], all: tuple[Fact, ...])`; `resolve(facts: Sequence[Fact], *, now: datetime, ttl_days: int, waivers: Sequence[Waiver] = ()) -> dict[tuple[str, str], Effective]` (key = `(kind, subject.canonical())`) implementing spec §4.4 rules 1–5 plus STALE (OBSERVED/VERIFIED older than ttl) and WAIVED (unexpired waiver matching the fact); `Waiver(fact_pattern: str, reason: str, expires: date)` lives in `model/` (add to `model/facts.py`). `snapshot.py`: `take(store, checks_run: frozenset[str]) -> str`. `diff.py`: `Change(kind, subject, before: Fact | None, after: Fact | None, change: Literal["added","removed","value","status"])`; `diff(before: Sequence[Fact], after: Sequence[Fact]) -> list[Change]` comparing by fact id + value + status (never timestamps), deterministic order.
- [ ] **Steps:** one test per precedence rule (`test_human_beats_drafted_meaning`, `test_expectation_kept_and_contradiction_shown`, `test_catalog_type_beats_note_and_both_shown`, `test_all_measured_retained_worst_warns`, `test_draft_marked_unverified`), `test_stale_after_ttl`, `test_waiver_expired_ignored`, `test_diff_ignores_timestamps`; property `test_resolve_order_independent` (shuffled input → identical output). Commit `feat(knowledge): precedence resolution, snapshots and diffs`.

---

### Task 8: Overlays (human YAML) and dbt import

**Files:** Create `src/schemaguru/overlays/{__init__,schema,loader,writer,dbt}.py`; Test `tests/unit/overlays/test_loader.py`, `test_writer.py`, `test_dbt.py`, `tests/property/test_overlay_props.py`, `tests/fixtures/overlays/*.yaml`, `tests/fixtures/dbt/manifest.json`.

**Interfaces — Produces:** `load_overlays(paths: Sequence[str | Path], store: Store) -> tuple[list[Fact], list[Chain], list[WaypointSpec], list[Waiver], DiagnosticBag]` — format exactly spec §14.1, `version: 1` required (`SG201` otherwise); line-numbered diagnostics `SG2xx` (`SG201` version, `SG202` unknown key with suggestion, `SG203` unresolvable ref, `SG204` ambiguous ref (candidates listed), `SG205` invalid filter text (from `fl.parse_filter` via `render/fragments.py` when available, else a syntax-only check), `SG206` waiver without expires, `SG207` expired waiver) — never raises on data; empty key = absent; facts get `provenance=HUMAN`, `source="overlay:<file>:<line>"`. `WaypointSpec(scope: Literal["pair","node"], tables: tuple[ObjectRef, ...], require: tuple[str, ...], forbid: tuple[str, ...], because: str, source: str)`. `writer.py`: `export_drafts(facts, path)` and `append_accepted(facts, path="schemaguru/accepted.yaml")` (machine-owned header comment `# generated by schemaguru accept — do not hand-edit`), never touching other files. `dbt.py`: `import_dbt(manifest_path, store) -> list[Fact]` (descriptions → DECLARED `DESCRIPTION`; tests `relationships`/`accepted_values`/`unique`/`not_null` → DECLARED expectations).
- [ ] **Steps:** `test_full_example_from_spec_loads` (the §14.1 YAML verbatim), `test_line_numbers_in_diagnostics`, `test_overlay_key_ambiguous_case`, `test_empty_key_is_absent`, `test_writer_never_edits_human_file`, `test_dbt_relationships_become_declared_joins`; property `test_loader_never_raises` (hypothesis YAML-ish text). Commit `feat(overlays): human YAML overlays, waivers and dbt import`.

---

### Task 9: Join graph — edges, cost, search, hub guard

**Files:** Create `src/schemaguru/graph/{__init__,edges,cost,search,hubs,calibrate}.py`; Test `tests/unit/graph/test_{edges,cost,search,hubs,calibrate}.py`.

**Interfaces — Consumes:** `Effective` (T7), `GraphConfig` (T2). **Produces:** `Edge(join: JoinRef, value: Mapping, verdict)`, `build_edges(effective) -> list[Edge]` (OK/WEAK only; BROKEN refused; unmeasured only if `include_unmeasured`; NaN/inf → unmeasured; malformed → skipped + `SG601`); `hop_cost(edge, *, reverse: bool, in_degree: int, cfg: GraphConfig) -> float` (spec §9.2 formula; `support == 0` → `math.inf`; weights read from `cfg` at call time); `Hubs.from_edges(edges, entity_tables: set[ObjectRef], cfg) -> Hubs` (spec §9.4 incl. the measured-shape fallback: sink reached by ≥2 attribution edges from distinct tables), `is_attribution(edge, cfg) -> bool`; `Path(hops: tuple[Hop, ...], cost: float, coverage: float | None, provisional: bool)`, `Hop(edge, reverse: bool)`; `Refusal(Enum)` `NO_EVIDENCE SAME_TABLE NO_ROUTE WAYPOINT_UNSATISFIABLE RULE_REJECTS_ALL HUB_PIVOT`; `PathResult(paths: tuple[Path, ...], refusal: Refusal | None, explanation: str)`; `find_paths(graph: Graph, src: ObjectRef, dst: ObjectRef, *, cfg, automaton: Automaton | None = None) -> PathResult` — Dijkstra over both directions, state key exactly spec §9.3, two passes (thin-support pruning then relaxed → `provisional=True`), Yen for `k_paths`, refusal diagnosis by relaxing one mechanism at a time (relaxed route never returned). `Graph.build(edges, hubs)` with lazy per-component adjacency. `calibrate.py`: `calibrate_hubs(edges) -> list[HubCalibration(hub, fan_ins: list[float], proposed_threshold: float | None, gap: tuple[float, float] | None)]` (centre of widest gap, geometric mean).
- [ ] **Steps (graphs built from `ShopFacts` numbers, no warehouse):** `test_broken_join_refused`, `test_support_zero_infinite`, `test_weights_read_at_call_time`, `test_reverse_traversal_uses_coverage`, `test_hop_in_state_key` (cheap 4-hop must not block 2-hop under `hop_limit=3`), `test_thin_join_returned_provisional_with_coverage`, `test_hub_pivot_refused_accounts` (orders→accounts→events refused HUB_PIVOT; accounts as endpoint allowed), `test_hub_measured_shape_without_entity_type`, `test_same_table_refused`, `test_k_paths_ordered`, `test_calibrate_proposes_gap_centre`. Commit `feat(graph): measured join graph, path search and hub guard`.

---

### Task 10: Waypoint rules and chain compilation

**Files:** Create `src/schemaguru/graph/{waypoints,chains}.py`; Test `tests/unit/graph/test_waypoints.py`, `test_chains.py`.

**Interfaces — Produces:** `parse_predicate(token: str) -> Predicate` for `node(t)`, `col(t.c)`, `edge(a.x->b.y)`, `any` (exact match; malformed → `ValueError` — fail closed at load, reported `SG602`); `compile_rules(specs: Sequence[WaypointSpec], src, dst) -> Automaton` (bitmask automaton, dead state, ≤16 requirements per rule else `SG603`; node-scope forbid withheld when the node is an endpoint); `Automaton.step(state, hop) -> state`, `.accepting(state) -> bool`, `.is_empty() -> bool`; `lint_rules(specs, edges) -> list[Diagnostic]` (`SG604` prohibition matches no edge). `chains.py`: `compile_chain(chain, graph) -> WaypointSpec | None` (linear chains only; each join oriented as the graph declares it; disconnected → None).
- [ ] **Steps:** `test_require_edge_forces_route_via_sessions` (events→orders via sessions), `test_forbid_node`, `test_node_forbid_withheld_at_endpoint`, `test_unsatisfiable_detected_offline`, `test_bad_token_fails_closed`, `test_more_than_16_requirements_rejected`, `test_chain_reoriented_to_graph`, `test_lint_dead_prohibition`. Commit `feat(graph): waypoint rules and chain compilation`.

---

### Task 11: PII detectors, masking, policy, outbound sweep

**Files:** Create `src/schemaguru/pii/{__init__,detectors,mask,policy,sweep}.py`; Test `tests/unit/pii/test_{detectors,mask,policy,sweep}.py`, `tests/property/test_mask_props.py`.

**Interfaces — Produces:** `Detector` Protocol `name: str; category: str; matches(value: str) -> bool`; built-ins exactly spec §11 (email incl. lists, phone E.164 + national, IBAN with mod-97 check, card with Luhn, ipv4/ipv6, uuid, hex ≥20 chars, secret-like (JWT `xxx.yyy.zzz` base64url or Shannon entropy ≥ 4.0 over ≥ 24 chars), person name (2–4 capitalised tokens with ≥1 hit in a bundled 2,000-name list, `enabled` flag), postal address heuristic, free text > 60 chars with spaces); `register_detector(d, *, replace=False)`. `mask_value(value: object) -> tuple[str, str | None]` (masked text, category) — shape tokens exactly `<email> <phone> <name> <iban> <card> <ip> <uuid> <hex32>` (hex of any length ≥20 renders `<hex{n}>`), `<free_text len=N>`; blank → treated as NULL (returns `("", None)`). `ValuePolicy.for_table(ref, cfg: SamplingConfig) -> Literal["masked","raw","none"]` (most specific glob wins). `classify_column(values: Sequence[tuple[str,int]], distinct: int | None, name: str) -> PIIFinding | None` (shape first, name second, cardinality third; the low-cardinality-hash trap → `PIIFinding(category="review", reason=...)`). `sweep(payload: str) -> tuple[str, int]` masks any detector hit inside an outbound string, returns hit count.
- [ ] **Steps:** detector truth tables (≥5 positive, ≥5 negative each, incl. `test_hex_16_not_masked`, `test_iban_checksum`, `test_luhn`), `test_mask_idempotent` (property), `test_policy_glob_specificity`, `test_name_alone_never_classifies` (column named `email_frequency` with values `daily/weekly` → not PII), `test_low_cardinality_hash_flagged_review` (`anon_enum_t.kind`), `test_sweep_catches_embedded_pii` (email inside a sentence and inside an error message). Commit `feat(pii): shape-first detectors, masking and outbound sweep`.

---

### Task 12: Drafting pipeline and LLM providers

**Files:** Create `src/schemaguru/draft/{__init__,provider,pipeline,inputs,verify,accept}.py`, `src/schemaguru/draft/prompts/table_v1.md`, `src/schemaguru/draft/providers/{__init__,callable,anthropic,openai_compat}.py`; Test `tests/unit/draft/test_{pipeline,inputs,verify,accept}.py`, `tests/unit/draft/test_providers.py`.

**Interfaces — Produces:** `LLMProvider` Protocol exactly spec §10.1; `CallableProvider(fn)`; `AnthropicProvider(model: str = "claude-sonnet-5-5", client=None, max_retries=1)` (tool-use structured output: a single tool `emit_draft` whose input_schema is `DRAFT_SCHEMA`; reads usage tokens into the ledger); `OpenAICompatibleProvider(base_url, api_key, model)` (JSON-schema response format). `DRAFT_SCHEMA` (JSON Schema) with exactly the spec §10.2 outputs. `build_inputs(store, table, cfg) -> DraftInput` applying `ValuePolicy` + `mask_value` + final `sweep` (records `pii_sweep_hits`). `draft_tables(refs, *, store, provider, config, budget) -> DraftReport` (one call per table; tables with ≤5 columns batched up to 8 per call; schema-invalid reply retried once then `ERROR` fact `SG401`; drafts stored as `DRAFTED` facts with `source=f"llm:{provider.name}:{model}"` and `value["prompt_hash"]`); never runs from scoping or rendering. `verify_drafts(facts, backend) -> list[Fact]` (spec §10.3: soft_delete → `ROWS t WHERE <p>` sanity via `backend.check`, type_notes → `PATTERN`, join_candidates → measured `KeyOverlapReq`, entity_type → `UNIQUE` on claimed key) — passing parts become `VERIFIED`. `accept(store, fact_ids) -> list[Fact]` → `HUMAN` facts with `source="accepted:<draft id>"`.
- [ ] **Steps (CallableProvider scripted replies, no network, no cassettes):** `test_draft_payload_masked` (customers.email values arrive as `<email>`), `test_raw_mode_only_on_opt_in`, `test_none_mode_sends_no_values`, `test_invalid_reply_retried_then_error`, `test_draft_never_overwrites_human`, `test_verified_soft_delete_becomes_verified`, `test_accept_creates_human_fact`, `test_token_budget_stops_run`; provider tests with fake SDK clients (Anthropic tool_use block parsing; OpenAI-compatible JSON). Commit `feat(draft): LLM drafting with masking, verification and acceptance`.

---

### Task 13: Scoping and retrieval

**Files:** Create `src/schemaguru/scope/{__init__,retrievers,embeddings,expand,budget,tokens}.py`; Test `tests/unit/scope/test_{retrievers,expand,budget,tokens}.py`, `tests/benchmark/test_search_latency.py` (`benchmark` marker).

**Interfaces — Produces:** `Retriever` Protocol `search(question: str, k: int) -> list[tuple[ObjectRef, float]]`; `LexicalRetriever(store)` (store FTS + verbatim-identifier boost ×2.0 **[call]** + glossary/metric hits mapped to tables); `Embedder` Protocol `embed(texts: Sequence[str]) -> list[list[float]]`; `EmbeddingRetriever(store, embedder)` (vectors stored in a store side table; cosine; extra `embeddings`); `HybridRetriever(*retrievers, k_rrf=60)` (reciprocal-rank fusion). `TokenCounter` Protocol `count(text) -> int`; `CharTokenCounter` (`ceil(len/4)`). `expand(seeds: Sequence[ObjectRef], graph, cfg: ScopeConfig) -> list[ObjectRef]` (best path between each seed pair; ≤ `max_expansion_tables` new tables, ranked by path cost). `fit(tables_ranked, renderer, budget_tokens, counter) -> list[tuple[ObjectRef, DetailLevel]]` with `DetailLevel(Enum)` `FULL STANDARD LITE NAME_ONLY` (spec §12.1 step 4; LITE column priority exactly as spec). `Scope(question, tables: tuple[tuple[ObjectRef, DetailLevel], ...], not_shown: int, unmeasured: tuple[ObjectRef, ...])`.
- [ ] **Steps:** `test_lexical_finds_orders_for_revenue_question` (shop store with drafted use_cases), `test_identifier_boost`, `test_glossary_maps_tables`, `test_rrf_fusion`, `test_expand_adds_bridge_table` (events + orders → sessions), `test_fit_degrades_detail_not_tables_first`, `test_lite_keeps_soft_delete_column`, `test_not_shown_count`; benchmark `test_search_latency_500k` (p95 < 200 ms). Commit `feat(scope): retrieval, graph expansion and token-budget fitting`.

---

### Task 14: Rendering, fragments, fingerprints

**Files:** Create `src/schemaguru/render/{__init__,view,profiles,table_block,legend,formats,fingerprint,fragments}.py`; Test `tests/unit/render/test_{table_block,profiles,formats,fingerprint}.py`, `tests/golden/test_golden_views.py`, `tests/golden/views/*.txt`.

**Interfaces — Produces:** `ViewBlock(stable: bool, text: str)`; `ContextView(question, blocks: tuple[ViewBlock, ...], tables, fingerprint: str, tokens: int)` with `.text`, `.to_markdown()`, `.to_json()`, `.to_xml()`; `Profile` Protocol `render_table(eff: TableKnowledge, level: DetailLevel) -> str`; built-ins `agent planner verifier router`; `register_profile(name, profile)`. `TableKnowledge` = everything resolved for one table (built by `render/view.py::collect(store, ref, now, cfg)`). Rules exactly spec §13.3: consequence order (⚠ lines before confirmations), hedges above, numbers to 4 significant digits, `values` capped at `max_values_shown` with `+N more`, case-drift line, ESTIMATED rendered `≈ <v> (sampled <fraction%>, ±<half-width>)`, unverified drafts tagged `(draft)`, provenance footer; legend block present iff any `⚠` and contains the sentence "Absence of a warning is not verification." `fingerprint(view) -> str` = `stable_hash(view.text)`; `fingerprint_views(guru_like, questions) -> dict[str, str]`. `render/fragments.py` (seam): `render_join_snippet(path: Path, dialect: str, soft_delete: Mapping[ObjectRef, str]) -> str | None` via `fl.sql.render_join(...).inline()` (None + `SG501` info when the capability is absent); `validate_filter(text, dialect) -> DiagnosticBag` via `fl.parse_filter`.
- [ ] **Steps:** `test_table_block_matches_spec_example` (orders block from shop facts equals golden), `test_warning_lines_first`, `test_legend_only_with_warnings`, `test_render_estimated_marker`, `test_render_quotes_odd_identifiers` (`"Order Items"`), `test_view_deterministic` (render twice, byte-equal; shuffled store insertion), `test_stable_blocks_first`, golden files for the 4 profiles × 3 questions. Commit `feat(render): context views, profiles, legend and fingerprints`.

---

### Task 15: Refresh and drift

**Files:** Create `src/schemaguru/refresh/{__init__,classify,run,report}.py`; Test `tests/unit/refresh/test_{classify,run,report}.py`.

**Interfaces — Produces:** `ChangeClass(Enum)` `BENIGN REVIEW PRE_EXISTING NOT_MEASURED`; `ChangeClassifier` Protocol `classify(change: Change, prev_effective, new_effective) -> ChangeClass | None` (None = no opinion); default classifier exactly spec §15.4, unknown → REVIEW; `register_change_classifier(c)`. `refresh(store, reader, scheduler, *, config) -> RefreshReport` (crawl incremental → stale/changed re-measure → expectations re-check → snapshot → diff → classify; a contradiction already present in the previous snapshot → `PRE_EXISTING`; checks missing vs previous `checks_run` → `NOT_MEASURED`, never "fixed"). `RefreshReport(changes: list[tuple[Change, ChangeClass]], crawl: CrawlReport, measure: MeasureReport, exit_code: int)` with `.to_json()`, `.to_markdown()`; exit code 0 / 1 (any REVIEW) / 3 (nothing measured).
- [ ] **Steps:** `test_enum_value_added_benign`, `test_enum_value_removed_review`, `test_verdict_change_review`, `test_preexisting_contradiction_not_regression`, `test_narrow_run_reports_not_measured`, `test_withdrawn_table_review`, `test_exit_codes`. Commit `feat(refresh): incremental refresh with relative drift classification`.

---

### Task 16: Runtime feedback and read-only guard helper

**Files:** Create `src/schemaguru/feedback/{__init__,intake,classifiers}.py`, `src/schemaguru/guard.py` (layer 6; add `"guard"` → 6 to `LAYERS`); Test `tests/unit/feedback/test_{intake,classifiers}.py`, `tests/unit/test_guard.py`.

**Interfaces — Produces:** `FailureClassifier` Protocol `classify(sql: str, error: str, dialect: str | None) -> list[FeedbackRecord]`; `FeedbackRecord(table: ObjectRef | None, column: str | None, failure_class: Literal["type_mismatch","unknown_column","unknown_table","ambiguous_column"])`; built-in classifiers per spec §16.2 (position from `[line:col]`/`at position N`/`LINE n:` patterns; string literals blanked; alias resolution; numeric pairs ignored; bare column attributed only when the query reads exactly one table); `report_error(store, sql, error, dialect=None) -> int` (records written; never raises; stores identifiers and counts only; enqueues re-probe with priority +1.0). `guard.check_sql(sql: str, dialect: str | None = None, *, never_query: Collection[ObjectRef] = ()) -> GuardVerdict(ok: bool, reasons: tuple[str, ...])` per spec §16.4.
- [ ] **Steps:** `test_type_mismatch_bigquery_style` (`No matching signature for operator = for argument types: STRING, INT64 at [3:15]` → `orders.created_at`), `test_postgres_style_operator_error`, `test_literal_not_attributed`, `test_numeric_pair_ignored`, `test_unattributed_refused`, `test_report_error_never_raises` (garbage input), `test_guard_literal_like_not_rejected` (`WHERE email LIKE '%@x.y'`), `test_guard_rejects_delete_and_multi_statement`, `test_guard_never_query_table`. Commit `feat(feedback): runtime error intake and read-only SQL guard`.

---

### Task 17: `Guru` façade (+ async)

**Files:** Create `src/schemaguru/guru.py`; Modify `src/schemaguru/__init__.py` (public API exports); Test `tests/unit/test_guru.py`, `tests/integration/test_guru_shop.py`.

**Interfaces — Produces:** `Guru(*, connector=None, reader: CatalogReader | None = None, store: Store | str | None = None, backend: MeasureBackend | None = None, config: Config | None = None, name: str = "default", dialect: str | None = None)`; `Guru.connect(url: str, *, name=..., store=None, config=None) -> Guru` (`factlang.connect` via the seam; store default `./.schemaguru/<name>.db`); methods exactly spec §19 plus `measure_queue()`, `verify(facts_or_claims, exact=False)`, `accept(ids)`, `export(path)`, `fingerprint_views(questions)`, `feedback_summary()`, `report_error(sql, error)`, `tools()`, `handle_tool_call(name, args)`, `prompt_block(question=None)`, `context(question, *, budget_tokens=4000, profile="agent", format="text") -> ContextView`; async twins `acrawl ameasure adraft acontext arefresh afind_path` (via `asyncio.to_thread` unless the backend is async). `context()` enqueues unmeasured scoped tables and never measures unless `measure_on_demand` says so. `schemaguru/__init__.py` exports exactly: `Guru, Config, Budget, SamplingConfig, GraphConfig, ScopeConfig, CrawlConfig, ObjectRef, JoinRef, Fact, FactKind, Provenance, Status, ContextView, PathResult, Refusal, RefreshReport, Diagnostic, SchemaGuruError, catalog, measure, draft, render, scope, graph, pii, overlays, guard, __version__`.
- [ ] **Steps:** `test_three_line_quickstart` (spec §2 G1 snippet on `shop_duckdb` with `FakeBackend` when FactLang lacks `measure`), `test_context_never_spends_by_default` (backend call log empty after `context()`), `test_background_mode_drains`, `test_exports_exact`, integration `test_guru_shop_end_to_end` (crawl → measure → load §14.1-style overlay → context("refund rate by month") contains orders block with case-drift and soft-delete lines, and payments thin-join coverage). Commit `feat: Guru façade and public API`.

---

### Task 18: Agent surface — tools, prompt block, MCP, adapters

**Files:** Create `src/schemaguru/agent/{__init__,tools,dispatch,prompt,mcp}.py`, `src/schemaguru/agent/adapters/{__init__,langchain,llamaindex,openai_agents,anthropic}.py`; Test `tests/unit/agent/test_{tools,dispatch,prompt}.py`, `tests/unit/agent/test_mcp.py` (skip without `mcp`), `tests/unit/agent/test_adapters.py` (each skip without its extra).

**Interfaces — Produces:** `tool_definitions(guru) -> list[dict]` — tools exactly spec §18.1 (`schema_context, search_tables, describe_table, find_join_path, column_values, check_fact, explain_term`), each `{"name","description","input_schema"}`; `check_fact` delegates to FactLang's `factlang_check` tool definition/handler via the seam (omitted when unavailable). `handle_tool_call(guru, name, args) -> dict` (always JSON-safe; unknown tool / bad args → `{"error": {"code": "SG801", ...}}`; never raises). `prompt_block(guru, question=None) -> str`. `mcp.serve(guru)` stdio server (tools + resource `schema://tables/{ref}`). Adapters map `tool_definitions` only: `langchain.tools(guru) -> list[BaseTool]`, `llamaindex.tool_spec(guru)`, `openai_agents.function_tools(guru)`, `anthropic.tools(guru) -> list[dict]` + `anthropic.run_tool_use(guru, block) -> dict`.
- [ ] **Steps:** `test_tool_schemas_valid_json_schema`, `test_dispatch_roundtrip_each_tool` (shop guru), `test_dispatch_bad_args_error_not_raise`, `test_prompt_block_contains_legend_and_usage`, adapter smoke tests, MCP in-process session test. Commit `feat(agent): tool definitions, prompt block, MCP server and framework adapters`.

---

### Task 19: CLI

**Files:** Create `src/schemaguru/cli/{__init__,main,commands}.py`, `src/schemaguru/__main__.py`; Test `tests/unit/cli/test_cli.py`.

**Interfaces — Produces:** `main(argv: Sequence[str] | None = None) -> int`; subcommands exactly spec §19.1 (`init crawl measure draft review accept export-drafts import-dbt context search path refresh report lint serve mcp doctor worker`), global `--url`, `--store`, `--config`, `--format text|json|markdown`; exit codes 0/1/2/3 per spec. `init` writes a commented `schemaguru.yaml` (overlay skeleton) and `schemaguru.config.yaml`; `review` is a line-based terminal prompt (`y/n/e(dit)/s(kip)/q`) over drafts; `doctor` checks connectivity, FTS5, FactLang capabilities, extras installed, budgets.
- [ ] **Steps:** one test per subcommand on the shop DuckDB (`--url duckdb:///<tmp>`), `test_json_format_parseable`, `test_exit_codes`, `test_review_accepts_with_scripted_stdin`. Commit `feat(cli): schemaguru command line`.

---

### Task 20: Remaining warehouse readers (parallel sub-tasks 20a–20i)

**Files (per dialect `<d>` ∈ bigquery, snowflake, redshift, databricks, clickhouse, tsql, oracle, trino, mysql):** Create `src/schemaguru/catalog/readers/<d>.py`, `tests/fixtures/catalog/<d>/*.json` (recorded system-view rows: ≥2 schemas, ≥5 tables incl. a view, a composite PK, an FK, a comment, a quoted/odd identifier), `docs/wiki/Warehouse-<D>.md`; Modify `.github/workflows/ci.yml` (Task 20 lead only: `integration` job with services `postgres:16`, `mysql:8.4`, `clickhouse/clickhouse-server:24.8`, `mcr.microsoft.com/mssql/server:2022-latest` (+ ODBC 18), `trinodb/trino:latest`; `pytest -m integration`; `SCHEMAGURU_REQUIRE_ENGINES=postgres,mysql,clickhouse,tsql,trino`).
**Interfaces:** each reader registered in `catalog/registry.py` under the FactLang dialect name; metadata sources exactly spec §6.2; `tests/conformance/test_readers.py` parametrised over all registered readers against their recorded fixtures via a `ReplayExecutor` (maps SQL fingerprint → recorded rows; unknown SQL fails the test) asserting identical `Table/Column/Constraint` output to `tests/fixtures/catalog/<d>/expected.json`; container-backed live engines run the same assertions against a seeded schema; `tests/live/test_readers_cloud.py` gated by `SCHEMAGURU_LIVE_<D>=1`.
- [ ] Each sub-task: fixture → failing conformance → reader → green → commit `feat(catalog): <d> reader`. BigQuery reader batches per dataset and reports bytes billed to `CrawlReport`.

---

### Task 21: Postgres store and server mode

**Files:** Create `src/schemaguru/store/postgres.py`, `src/schemaguru/store/migrations/pg/0001_initial.sql`, `src/schemaguru/server/{__init__,app,auth}.py`; Test `tests/integration/store/test_postgres_store.py` (reuses Task 3 tests via a `store` fixture parametrised over SQLite/Postgres), `tests/unit/server/test_app.py` (Starlette `TestClient`).

**Interfaces — Produces:** `PostgresStore(dsn: str)` passing the full Task 3 contract (tsvector search; optional pgvector for embeddings); `create_app(guru, *, authenticator: Authenticator) -> Starlette` with routes exactly spec §17 and `/mcp` (streamable HTTP via the `mcp` extra when installed); `Authenticator` Protocol `authenticate(headers) -> str | None`; `BearerTokens(tokens: Mapping[str, str])`; refresh guarded by `pg_try_advisory_lock`; CLI `serve` wires it.
- [ ] **Steps:** store contract suite green on both backends; `test_unauthenticated_401`, `test_context_endpoint_matches_library`, `test_refresh_lock_exclusive`. Commit `feat(server): Postgres store and optional HTTP server`.

---

### Task 22: Documentation site, demo and examples

**Files:** Create `mkdocs.yml`, `docs/index.md` (landing: problem → "the wrong join" story → how SchemaGuru fixes it → where it shines), `docs/getting-started.md`, `docs/guide/{concepts,crawl,measure,sampling,graph,drafting,pii,overlays,scoping,rendering,refresh,feedback,server,agents,configuration,cost-and-scale,security}.md`, `docs/extending/*.md` (one per spec §21 point), `docs/reference/{api,cli,diagnostics}.md`, `docs/demo.md` + `site-demo/{demo.js,demo.css,shop_export.json}`, `scripts/build_demo_export.py`, `examples/{duckdb_quickstart.py,langchain_agent.py,mcp_server.md,anthropic_tools.py,large_table_sampling.py}`, `.github/workflows/docs.yml`; Test `tests/conformance/test_diagnostics_doc.py` (codes in `docs/reference/diagnostics.md` == `CODES` == codes emitted in `src/` by regex `SG\d{3}`), `tests/conformance/test_docs_examples.py` (every ```` ```python ```` block in `docs/` and `examples/*.py` runs against the shop DuckDB with `FakeBackend` when FactLang lacks a capability).
- Demo (client-side only, no Pyodide dependency): search box over `shop_export.json` → scoped context view; join-graph explorer (SVG force layout) showing a path search and the accounts HUB_PIVOT refusal; before/after prompt comparison panel. `docs.yml`: build wheel, `python scripts/build_demo_export.py`, `mkdocs build --strict`, deploy to `gh-pages`.
- [ ] Commit `docs: documentation site, interactive demo and examples`.

### Task 23: GitHub Wiki

**Files:** `docs/wiki/*.md` — exactly the pages in spec §23.3 (Home, Why-Schema-Intelligence, Concepts, Architecture, one per component, Warehouse-<D> pages (from Task 20), Extending-* pages, CLI, Configuration, Diagnostics, Scale-and-Cost, Sampling, Security, FAQ, Glossary, Design-Decisions (§25 with reasons), Contributing, `_Sidebar.md`); each opens with a 3-line summary and ends with "See also"; Architecture has ASCII + Mermaid diagrams. `.github/workflows/wiki.yml` (sync `docs/wiki/` on push to main; `permissions: contents: write`). Test `tests/conformance/test_wiki_links.py`. **Owner prerequisite:** create the first wiki page once in the GitHub web UI (the Elephant asks the owner at this point).
- [ ] Commit `docs: GitHub wiki`.

### Task 24: Acceptance sweep

**Files:** `tests/acceptance/test_spec_acceptance.py`.
- One test per spec goal G1–G9 and per anti-goal (e.g. `test_no_unmasked_value_reaches_llm_by_default`, `test_no_writes_on_query_path` (store write counter unchanged across 100 `context()` calls), `test_no_full_dump_by_default`, `test_sampled_marked_everywhere`), all on the shop warehouse with the **real** FactLang (no skips allowed in this file). Fix any failure in the owning module. Commit `test: spec acceptance sweep`.

### Task 25: Release v0.1.0 (gated on FactLang 0.1.0 on PyPI)

**Files:** `.github/workflows/release.yml`, final `README.md`, `CHANGELOG.md` 0.1.0.
- Remove the CI git-install of FactLang; confirm no `factlang`-marked test is skipped.
- `release.yml` on tag `v*`: build, verify tag == `__version__`, `twine check`, job `pypi` (`environment: pypi`, `permissions: id-token: write`, `pypa/gh-action-pypi-publish@release/v1`), job `github-release` (`softprops/action-gh-release@v2`, `dist/*`, generated notes).
- **Owner step (Elephant gives it at this point):** on pypi.org → Publishing → "Add a new pending publisher": project `schemaguru`, owner `rupakc`, repository `schemaguru`, workflow `release.yml`, environment `pypi`; and create the `pypi` environment in the GitHub repo settings.
- README per spec §23.3 (badges CI, PyPI, Python, license, docs). Verify `pip install schemaguru==0.1.0` in a fresh venv and run the README quickstart on DuckDB. Commit `chore(release): 0.1.0`; tag `v0.1.0`; push tag.

---

## Self-review notes (Elephant)

- Spec coverage: §4 → T2,T7; §5 → T1 (layers/seam); §6 → T4,T20; §7 → T3,T21; §8 (incl. §8.6 sampling) → T6; §9 → T9,T10; §10 → T12; §11 → T11,T12; §12 → T13; §13 → T14; §14 → T8; §15 → T15; §16 → T16; §17 → T21; §18 → T18; §19 → T17,T19; §20 → T2 + every task's codes + T22 doc test; §21 → registries in T2,T3,T4,T11,T12,T13,T14,T15,T16,T21 + T22 extending docs; §22 → T5 + per-task tests + T20 conformance + T24; §23 → T1,T22,T23,T25; §24 → T1 scaffolding; §25 → CLAUDE.md checklist (T1) + tests named in tasks.
- Decisions made here (builder-level, recorded in `MEMORY.md` by T1): FactLang seam modules; SchemaGuru-level `Request` types decoupled from FactLang IR so work proceeds before FactLang ships `measure`; `DBAPIExecutor` convenience; `guard.py` at layer 6; verbatim-identifier boost ×2.0; fixture trap table design.
