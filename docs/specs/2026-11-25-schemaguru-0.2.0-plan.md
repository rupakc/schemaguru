# SchemaGuru 0.2.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship SchemaGuru 0.2.0: declared/parsed lineage (dbt manifests, warehouse view definitions, human overlays) served to agents; every deferred 0.1.0 finding fixed, already fixed or ruled out; a reproducible, published 500,000-table / 25,000,000-column scale benchmark.

**Architecture:** Lineage is two new fact kinds whose precedence lives in one pure module (`model/lineage.py`). The store keeps an *effective-only* `lineage_edges` index, rewritten for a subject in the same transaction as its facts. SQL is parsed by `sqlglot` (extra `lineage`) in one reused, killable worker subprocess. The parent process holds raw SQL in memory only, and only long enough to parse it. Readers gain an optional `view_definitions`; crawl, dbt import and overlays emit the facts; rendering, a traversal (`knowledge/lineage.py`) and a `lineage` tool across Python, CLI, HTTP and MCP read them. Lineage never enters the join graph.

**Tech Stack:** Python 3.10–3.13, factlang 0.1.4, PyYAML, sqlite3/FTS5, psycopg (Postgres store), sqlglot 30.x (optional extra), pytest + hypothesis, ruff, mypy --strict, GitHub Actions, mkdocs-material.

**Spec:** `docs/specs/2026-11-25-schemaguru-0.2.0-design.md` ("§n"). It extends `docs/specs/2026-11-24-schemaguru-design.md` ("base §n"). Every implementer reads the § sections named in its task. The 0.1.0 build ledger (`.superpowers/sdd/2026-11-24-schemaguru-plan/progress.md`, `task-24-deferred.md`, `task-*-report.md`; git-ignored, on the maintainer's machine) explains each Appendix A item.

## Global Constraints

- Gate before every commit and push, chained with `&&`, never piped: `ruff check && ruff format --check && mypy --strict src && pytest -m "not integration and not live and not benchmark"`. The pre-push hook enforces it; never `--no-verify`.
- Commit author `Rupak Chakraborty <rupakc@users.noreply.github.com>` (repo-local git config, already set). Every commit message ends with the trailer `Co-Authored-By: Claude <model> <noreply@anthropic.com>`, where `<model>` is the implementing model's name (e.g. `Opus 5.5`). Use conventional commits.
- Stage explicit paths only; never `git add -A` or `git add .`; never `--no-verify`.
- Agents never dispatch subagents and never use `gh`.
- No mention of any proprietary predecessor or organisation anywhere: code, docs, fixtures, commit messages (the denylist CI job). Use only generic shop-domain names (`customers, orders, order_items, products, payments, events, accounts, users, sessions`, plus `refunds`, `analytics.*` models).
- Core dependencies unchanged: exactly `factlang>=0.1.4,<0.2` and `PyYAML>=6`. New extra `lineage = ["sqlglot>=30,<31"]`, also added to `all` and to `dev`. Every third-party import other than `factlang`/`yaml` is lazy (inside a function), as enforced by `tests/architecture/test_layers.py::test_optional_deps_lazy`.
- Public API additive only (§3, base §19): new names and optional parameters only; no 0.1.0 name is removed or changes meaning. New names are added to `tests/unit/test_guru.py::EXPORTS` (inserted before `"__version__"`), never removed or renamed.
- Red-first: every behaviour change lands with a test that failed before the change. Paste the red run's one-line failure into the commit body (`red: <test id>: <error>`).
- Guards only tighten (§2 anti-goals, §5): the read-only guard (`src/schemaguru/guard.py`), the metadata guard (`catalog/executor.py::check_metadata_sql`), masking (`src/schemaguru/pii/`) and the `InlineConnector` refusals. A false-rejection fix ships with a test proving the hostile form is still refused. Masking trades keep recall (over-masking acceptable, a leak is not), and each trade is documented.
- Layer rules (base §5.1): add `"schemaguru.lineage": 2` to `LAYERS` in `tests/architecture/test_layers.py`. The FactLang seam stays exactly `measure/flbridge.py`, `catalog/executor.py`, `render/fragments.py`.
- Determinism (base §7.3, §12.2): the same store and inputs give byte-identical views and the same fact ids; lineage lists are sorted and de-duplicated.
- Privacy (§4.3): no raw view definition, dbt `compiled_code`, or literal from either is persisted, logged, rendered, put in a diagnostic or exception message, or sent to an LLM. The store keeps only facts, a sha256 and a literal-free template.
- Lineage is never `VERIFIED`, never shown as measured, and never a join edge.
- New diagnostic codes go into `src/schemaguru/diagnostics.py::CODES`. `python3 scripts/gen_docs.py` regenerates `docs/reference/*.md` and `docs/generated/*` in the same task that changes codes, CLI, tools or config (`tests/conformance/test_docs_generated.py`, `test_diagnostics_doc.py`). A new CLI subcommand also gets a row in `docs/wiki/CLI.md` (`test_wiki_facts.py`).
- Work in the venv `~/schemaguru/.venv` with `python3`. Every commit leaves the gate green.

## Review Focus

1. **A host script without an `if __name__ == "__main__":` guard, a REPL or a notebook calls `guru.crawl()`.** Expected: lineage parsing works; the worker never re-imports the user's script. Pinned by Task 12 `test_worker_from_unguarded_script`.
2. **A view definition arrives as a full DDL statement, not a bare `SELECT`.** Examples: DuckDB `CREATE VIEW … AS`, Snowflake `create or replace secure view v (a comment 'x', b) copy grants as …`, MySQL `ALGORITHM=… SQL SECURITY DEFINER`, Redshift `… with no schema binding`, a trailing `;` or a leading comment. Expected: the query part is parsed, and a column list renames the outputs. Pinned by Task 11 `test_parse_accepts_view_wrappers`.
3. **Names in a definition differ in case or quoting from the catalog.** Examples: Snowflake `SHOP.ORDERS` against stored `shop.orders`; `"Order Items"`; `Orders` and `orders` both stored. Expected: the store's exact-then-unique-case-insensitive rule resolves the name; an ambiguous name goes to `unresolved`, never a guess. Pinned by Task 14 `test_relation_names_case_and_quoting`.
4. **A view reads a table outside the crawl scope** (an excluded schema or another database). Expected: the name is listed `(not in catalog)`, never dropped. Pinned by Task 14 `test_out_of_scope_upstream_is_unresolved`.
5. **A view is redefined after a human `ignore` entry targeted its old parsed link.** Expected: the old view facts are retracted, the ignore now matches nothing (SG213 on the next load) and nothing hidden comes back. Pinned by Task 16 `test_ignore_after_view_redefinition`.

## Dependency graph

```
T1 (audit) → T2 … T8 (hardening by area; any order, each before the lineage task touching its files)
T2 → T9 → T10 → T11 → T12 → T13 → T14 → {T15, T16} → T17 → T18
T18 → T19 → T20 → T21 → T22
```

---

### Task 1: Hardening audit — the disposition ledger

**Files:**
- Create: `docs/specs/2026-11-25-schemaguru-0.2.0-hardening-ledger.md`
- Test: `tests/conformance/test_hardening_ledger.py`

**Interfaces:**
- Produces: the ledger, a Markdown table with header `| ID | Item | State | Evidence |` and one row per ID below. The `State` and `Evidence` values are:

  | State | Evidence |
  |---|---|
  | `open` | the owning task (`Task 3`) |
  | `already fixed` | `commit <sha≥7> · test <path>::<name>` |
  | `fixed` | `test <path>::<name>` |
  | `ruled out` | a one-line reason (≥ 10 chars), plus `· docs: <page>` when the item is user-visible |

  Tasks 2–8 turn `open` rows into `fixed` or `ruled out`.
- Produces: `tests/conformance/test_hardening_ledger.py` with `ITEM_IDS: tuple[str, ...]`, `parse_ledger(text: str) -> dict[str, tuple[str, str, str]]` (id → item, state, evidence) and `ALLOWED_STATES = ("open", "fixed", "already fixed", "ruled out")`.

**Item IDs** (Appendix A order; every finding is one ID):
- **A1:** .1 `Garcia,Maria` · .2 `M. Garcia` · .3 lowercase names · .4 names not in the list · .5 lone-surname possessives · .6 addresses inside sentences · .7 `password=hunter2` (<8) · .8 credential glued to word chars (`x_<key>`) · .9 `github_pat_`/`xapp-` · .10 zero-padded ids as phone · .11 path-like as secret · .12 version strings as `<ip>` · .13 place/brand names · .14 `password: varchar(255)` · .15 `_contains` class-range interiors · .16 `detectors.py` fn typed `object` + `type: ignore` · .17 document hex-needs-a-letter and `<secret>`/`<address>`.
- **A2:** .1 path check only on a FROM item's first token · .2 `.avro/.orc/.bz2/.gzip` · .3 metadata guard backtick false rejection · .4 `SHOW CREATE` false rejection · .5 quote scanner ignores backslash escapes · .6 `FactLangExecutor` skips the local guard · .7 fullwidth `；`/`－－` in `check_predicate` · .8 look-alike quotes via non-Unicode ODBC (InlineConnector) · .9 raising `tzinfo` → `RuntimeError` not SG109.
- **A3:** .1 resume ignores scope hash · .2 schemas created before the cursor skipped on resume · .3 no temp-dir cleanup test · .4 `list_schemas` ignores `page_token` · .5 explicit-refs crash pushes non-queued tables · .6 `queue_push` failure masks the original exception · .7 keyhash vs profile DISTINCT precedence by batch order · .8 back-off reset untested · .9 refill with in-flight probes overshoots · .10 fused bytes charged N times · .11 sampled max-per-key says 1:1 · .12 rule-2 id↔id candidate noise · .13 `flbridge` imports `factlang.semantics.refs`.
- **A4:** .1 PG `NOT VALID` FK enforced · .2 PG unique indexes · .3 PG integration lacks partitioned/unanalysed tables · .4 reader `row_count` unlabelled · .5 Oracle `ALL_PART_TABLES` nullable `c.owner` · .6 Oracle MV comments · .7 Snowflake clustering expressions as names · .8 Snowflake `ValueError` vs `CatalogError` · .9 Snowflake `SHOW` caps · .10 BQ `_PARTITIONTIME` pseudo-columns · .11 BQ cross-project `TABLE_STORAGE` · .12 BQ region default docs · .13 BQ FK join by constraint name · .14 Redshift re-reads schema-wide info per page · .15 Redshift ignores `changed_since` · .16 `bytes_billed_is_lower_bound` duck-typed · .17 fixture handlers match SQL by substring · .18 keyset paging vs collations.
- **A5:** .1 `from_dict` trusts the stored id · .2 `stable_hash` Decimal/date/mixed-key normalisation · .3 py3.10 `'Z'` isoformat · .4 Config env coercion for tuple fields · .5 `per_glob` mutable · .6 `_value_identity` must fail loudly · .7 `facts_for` kind filter in Python · .8 nested transaction without SAVEPOINT · .9 stale doc on subject change · .10 fixed `.tmp` export name · .11 `iter_schemas` fetchall · .12 COMMIT failure resets depth · .13 glossary not in search docs · .14 SG702 code only in message text · .15 stale index after A→B→A backend switch · .16 read-only empty `table_docs` without diagnostic · .17 no byte-equality test for `snapshot_facts` JSON · .18 two JSON encoders · .19 MEASURED/CATALOG `observed_at=None` fresh · .20 ESTIMATED never STALE · .21 superseded ESTIMATED keeps ESTIMATED · .22 DRAFTED ERROR/UNMEASURABLE without unverified marker · .23 `diff` vs `resolve` tie-breaks differ · .24 `diff` has no `checks_run` · .25 `subject_key` belongs in `model/` · .26 `test_all_measured_retained_worst_warns` asserts nothing · .27 no test for a sampled extra also seen exactly.
- **A6:** .1 waiver kind/subject not validated · .2 duplicate YAML keys · .3 glossary `maps_to` accepts columns · .4 `facts_to_overlay` drops kinds silently · .5 list merge duplicates changed joins · .6 `mkstemp` 0600 + check/replace window · .7 JOIN values carry only cardinality · .8 entry error retracts previous good fact · .9 dbt unique count only sees resolvable columns · .10 `dbt.py` docstring missing · .11 merged rule requirements uncapped · .12 bare ref with quoted `(` rejected.
- **A7:** .1 per-graph memo not thread-safe · .2 hop-limit naming capped at 2×hop_limit · .3 via-success search near the 1 s bound · .4 identifier denylist misses ALLCAPS+CamelCase (`ZORBClient`) · .5 qualified `t.isDeleted` left bare · .6 ambiguous case-insensitive match left bare.
- **A8:** .1 `--format json` errors not JSON · .2 Sampling page thresholds · .3 owner name in wiki footer · .4 clearer error if the wiki repo is missing · .5 wiki-facts negative controls synthetic only · .6 layer walker wiring lacks a tmp-tree control · .7 facade import reported "no layer" · .8 no-op set comprehension in `test_layers` · .9 nested `TYPE_CHECKING` and I2 guard lack real-tree controls · .10 `tests/test_version.py` `__all__` check · .11 denylist job passes silently without the secret.
- **A9** (0.1.1 patch review + check_fact): .1 `InlineConnector` class docstring omits the trino `… UTC` form and the databricks zone-less refusal · .2 FL507 tests assert only `SG109` (pin `precision 39` / `64-bit`) · .3 `test_trino_rejects_the_old_timestamp_form` also asserts the zone-less form (split or rename) · .4 `literal_refusal` is a public name (shipped in 0.1.1 — renaming removes a released name; expected `ruled out`, keep it and document it) · .5 `check_fact` has no claim cap: SchemaGuru calls `Checker.check` directly (`measure/flbridge.py`), bypassing FactLang's `max_claims`; add a SchemaGuru-side cap (default 200, configurable, `None` disables) refused before any warehouse spend with a tool error whose code is `too_many_claims`.

- [ ] **Step 1: Write the failing test.** `test_ledger_lists_every_item_once` asserts that `parse_ledger(...)` keys equal `set(ITEM_IDS)` and that no ID repeats. `test_ledger_states_and_evidence_valid` asserts every state is in `ALLOWED_STATES` and checks the evidence:
  - `already fixed` and `fixed`: the evidence names a test whose file exists and contains `def <name>`. For `already fixed`, `git cat-file -e <sha>` succeeds; skip that check when `ROOT/.git` is absent (sdist).
  - `ruled out`: the reason is ≥ 10 chars.
  - `open`: the evidence matches `Task \d+`.

  Add negative controls `test_parse_ledger_rejects_bad_rows` (a duplicate ID, an unknown state, a missing test file — each must fail).
- [ ] **Step 2: Run it.** `pytest tests/conformance/test_hardening_ledger.py -v`. Expected: FAIL (ledger file missing).
- [ ] **Step 3: Audit each ID against `main`.** First read the 0.1.0 ledger lines that recorded the item, for its exact meaning. Then:
  - **The code already behaves correctly:** find the commit (`git log -S'<symbol>' --oneline -- src/` or `git log -L`). Find a test that pins the behaviour, or write one. Prove that test fails without the fix in a throwaway worktree: `git worktree add ../sg-audit HEAD && git -C ../sg-audit show <sha> -- <src path> > ../sg-audit.patch && git -C ../sg-audit apply -R ../sg-audit.patch && (cd ../sg-audit && pytest <test id>)` → must FAIL. Then `git worktree remove --force ../sg-audit`. Mark the row `already fixed`. A new test committed here must pass on `main`.
  - **The code is still wrong:** mark `open` and name the owning task (A5→Task 2, A1→Task 3, A2 and A9.1–A9.4→Task 4, A3→Task 5, A4→Task 6, A6→Task 7, A7/A8 and A9.5→Task 8).
  - **Fixing would loosen a guard or masking, or would change a 0.1.0 fact id:** mark `ruled out` with the reason.
  - **Recorded rulings:** A8.3 → `ruled out`: "the owner is the named MIT copyright holder; intended (spec [call])".
- [ ] **Step 4: Run the test.** `pytest tests/conformance/test_hardening_ledger.py -v`. Expected: PASS.
- [ ] **Step 5: Gate, then commit.** Run the gate. Then `git add docs/specs/2026-11-25-schemaguru-0.2.0-hardening-ledger.md tests/conformance/test_hardening_ledger.py <any test files added>` and `git commit -m "docs(hardening): audit every deferred 0.1.0 item into a disposition ledger"`.

---

**Rules for Tasks 2–8 (the hardening fix tasks):**
- Each task handles only the IDs its ledger rows mark `open`.
- For each such ID: write the red test named in the table, run it and see it FAIL, make the change, see it PASS.
- Then change the row to `fixed · test <id>`, or to `ruled out · <reason>` when the fix would break a Global Constraint. For a `ruled out` user-visible item, add one line under a `## Known limits` heading of the named docs page.
- A row the audit marked `already fixed` is not touched.
- Each task ends with the gate and one commit, staging the touched `src/`, `tests/` and `docs/` paths plus the ledger.

### Task 2: Hardening — store and model (A5), plus the 0.1.0 fixture store

**Files:**
- Create: `tests/fixtures/store_v010/build_v010.py`, `tests/fixtures/store_v010/shop.db`, `tests/fixtures/store_v010/facts.json`, `tests/unit/store/test_v010_store.py`
- Modify: `src/schemaguru/model/facts.py`, `src/schemaguru/_util/hashing.py`, `src/schemaguru/_util/timeutil.py`, `src/schemaguru/config.py`, `src/schemaguru/store/{sqlite,postgres,export}.py`, `src/schemaguru/errors.py`, `src/schemaguru/knowledge/{resolve,diff}.py`, `src/schemaguru/model/refs.py`, `src/schemaguru/render/table_block.py`, `src/schemaguru/diagnostics.py`; `tests/golden/views/*` only if a render fix (A5.19, A5.22) moves a golden (regenerate with `SCHEMAGURU_UPDATE_GOLDEN=1`, review each diff by eye, stage the changed files explicitly)
- Test: `tests/unit/model/test_facts.py`, `tests/unit/test_config.py`, `tests/unit/store/{contract,test_sqlite,test_export,test_migrations}.py`, `tests/unit/knowledge/{test_resolve,test_diff}.py`, `tests/unit/render/test_table_block.py`, `tests/property/test_model_props.py`

**Interfaces:**
- Produces: `tests/fixtures/store_v010/shop.db`, a SQLite store written by tag `v0.1.0` from `tests.golden.shop_knowledge.shop_store`, checkpointed to one file (`PRAGMA wal_checkpoint(TRUNCATE)`, `journal_mode=DELETE`). Also `facts.json`, the sorted `[f.to_dict() for f in store.iter_facts()]` written by v0.1.0.
- Produces: `build_v010.py OUTDIR`, run as `git worktree add ../sg-v010 v0.1.0 && PYTHONPATH=../sg-v010/src:../sg-v010 python3 tests/fixtures/store_v010/build_v010.py tests/fixtures/store_v010 && git worktree remove --force ../sg-v010`.
- Produces: `tests/unit/store/test_v010_store.py::open_v010(tmp_path) -> SQLiteStore`, which opens a copy of the fixture. Task 10 extends this file.
- Produces: `StoreVersionError.code == "SG702"`.
- Produces: `diff(before, after, *, checks_run: frozenset[str] | None = None)`.
- Produces: `schemaguru.model.subject_key` (with `knowledge.diff.subject_key` kept as an alias).
- Constraint on every hashing fix: every fact id in `facts.json` still equals `Fact.make(...)`'s recomputation under 0.2.0 (`test_v010_fact_ids_still_verify`).

| ID | After the fix | Red test |
|---|---|---|
| A5.1 | `Fact.from_dict` recomputes the id; a mismatch raises `ValueError("fact id mismatch …")` | `test_facts.py::test_from_dict_rejects_tampered_id` |
| A5.2 | `stable_hash(Decimal("1.0")) == stable_hash(Decimal("1.00"))`; `date` → ISO; mixed-type keys never `TypeError`; no 0.1.0 id changes. The Decimal/date normalisation lives in the shared `_canon`, so the stored text equals the hashed text and A5.1's recomputation cannot reject a stored fact | `test_model_props.py::test_stable_hash_normalises` + `test_v010_fact_ids_still_verify` + `test_model_props.py::test_decimal_date_fact_roundtrips_id` (property over Decimal/date values: `Fact.from_dict(json.loads(canonical_json(f.to_dict()))).id == f.id`) |
| A5.3 | `"2026-01-01T00:00:00Z"` parses on 3.10 wherever a stored timestamp is read | `test_facts.py::test_from_dict_accepts_z_suffix` |
| A5.4 | `Config.from_env` turns `SCHEMAGURU_CRAWL__EXCLUDE="a.*,b.*"` into a tuple | `test_config.py::test_env_tuple_fields_coerced` |
| A5.5 | `SamplingConfig.per_glob` is read-only (`TypeError` on item assignment) | `test_config.py::test_per_glob_immutable` |
| A5.6 | a missing identity field raises (expected `already fixed`) | `test_facts.py::test_missing_identity_field_raises` |
| A5.7 | kind filter in SQL on index `(subject_key, kind)` (expected `already fixed`, migration 0002) | `contract.py::test_facts_for_filters_kinds` |
| A5.8 | a nested `transaction()` uses `SAVEPOINT`; an exception caught by the outer block rolls back only the inner writes | `contract.py::test_nested_transaction_savepoint` |
| A5.9 | upserting a fact whose subject moved reindexes the old and the new table | `contract.py::test_reindex_on_subject_change` |
| A5.10 | `export_json` writes a unique temp file in the target directory, then `os.replace`; two concurrent exports both succeed | `test_export.py::test_concurrent_exports_do_not_clash` |
| A5.11 | `iter_schemas` streams in pages (spy: no single `fetchall` over all rows) | `contract.py::test_iter_schemas_streams` |
| A5.12 | a failing `COMMIT` rolls back, resets the depth, and the next transaction works | `test_sqlite.py::test_commit_failure_recovers` |
| A5.13 | GLOSSARY terms are searchable: search "net revenue" finds a table the term `maps_to` | `contract.py::test_glossary_terms_searchable` |
| A5.14 | `StoreVersionError` carries `.code == "SG702"` | `test_migrations.py::test_newer_store_refused_has_code` |
| A5.15 | FTS5→LIKE→FTS5 reopen rebuilds a stale index (the backend that last wrote the index is recorded under `crawl_state` key `"_search_backend"`; no new table, so migration 3 stays Task 10's) | `test_sqlite.py::test_backend_switch_rebuilds_index` |
| A5.16 | read-only open of a store with tables but an empty `table_docs` adds `SG706` WARNING "the read-only store's search index is empty; open it read-write once to rebuild it" | `test_sqlite.py::test_readonly_empty_index_warns` |
| A5.17 | `snapshot` written from shuffled input stores byte-identical JSON | `contract.py::test_snapshot_bytes_deterministic` |
| A5.18 | the store and `export_json` share one encoder (`_util.hashing.canonical_json`); `stable_hash` keeps its own encoder call because ids must not change (documented in its docstring); both call the shared `_canon` (A5.2) | `test_export.py::test_store_and_export_share_encoder` |
| A5.19 | unknown age stays non-STALE (base §4.4), and the view says `observed: date unknown` | `test_table_block.py::test_unknown_age_marked` |
| A5.20 | an ESTIMATED fact older than the TTL becomes STALE and still renders `≈ … (sampled …)` | `test_resolve.py::test_estimated_goes_stale` |
| A5.21 | a superseded ESTIMATED fact becomes STALE like OBSERVED | `test_resolve.py::test_superseded_estimate_stale` |
| A5.22 | every DRAFTED line carries `(draft)` whatever its status (ERROR, UNMEASURABLE) | `test_table_block.py::test_draft_error_marked_draft` |
| A5.23 | `diff` and `resolve` choose the same fact from one set (shared `_newest_first`) | `test_model_props.py::test_diff_resolve_same_tiebreak` |
| A5.24 | `diff(..., checks_run=…)` never reports a fact of an un-run check as removed; `refresh` passes `snapshot_checks_run` | `test_diff.py::test_unrun_checks_not_removed` |
| A5.25 | `subject_key` lives in `model/` and the old import path still works | `test_facts.py::test_subject_key_in_model` |
| A5.26 | the test asserts the worst warning text | `test_resolve.py::test_all_measured_retained_worst_warns` (strengthened; the red step = the stricter assertion failing against a planted regression) |
| A5.27 | a value seen by a sample and by an exact measurement renders without `[sampled]` | `test_resolve.py::test_sampled_extra_seen_exactly` |

- [ ] **Step 1: Build the fixture store.** Write `build_v010.py`. Run it from the v0.1.0 worktree (command above). Write `test_v010_store.py::test_v010_fact_ids_still_verify`: open a copy, and for every fact in `facts.json`, `Fact.from_dict(d).id == Fact.make(kind, subject, value, provenance, status, method=..., …).id`. Write `test_v010_golden_views_unchanged`: render the three golden questions with `tests/golden` settings on the copy; output equals `tests/golden/views/{agent,planner,verifier,router}__q{1,2,3}.txt` byte for byte.
- [ ] **Step 2: Fix the `open` A5 rows** using the red-first cycle from the table. Register `SG706` in `CODES`. Then run `python3 scripts/gen_docs.py`.
- [ ] **Step 3: Gate, then commit.** Commit message: `fix(store,model): close the 0.1.0 store and model findings; add the v0.1.0 fixture store`.

### Task 3: Hardening — PII masking (A1)

**Files:**
- Modify: `src/schemaguru/pii/{detectors,mask,sweep,policy}.py`, `src/schemaguru/pii/data/names.txt`, `docs/guide/pii.md`, `docs/wiki/PII-and-Sampling-Policy.md`
- Test: `tests/unit/pii/{test_hardening,test_policy}.py`, `tests/property/test_mask_props.py`, `tests/conformance/test_docs_examples.py`
- A masking change may move a golden view: regenerate with `SCHEMAGURU_UPDATE_GOLDEN=1`, review each diff by eye, and stage the changed `tests/golden/views/*` files explicitly.

**Interfaces:**
- Consumes: `mask_free_text(text) -> tuple[str, int]`, `mask_value(v) -> tuple[str, str | None]`, `sweep(payload) -> tuple[str, int]` (unchanged signatures).
- Produces: no new names. Recall wins every trade (Global Constraints).

| ID | After the fix | Red test (`test_hardening.py::…`) |
|---|---|---|
| A1.1 | `mask_free_text("call Garcia,Maria today")` contains `<name>`, and no `Garcia`/`Maria` remains | `test_comma_joined_name_masked` |
| A1.2 | `"M. Garcia"` → `<name>` | `test_initial_surname_masked` |
| A1.3 | `"maria garcia"` (lowercase full name from the list) → `<name>` | `test_lowercase_name_masked` |
| A1.4 | a capitalised First Last pair after a person cue (`customer`, `contact`, `Mr`, `Ms`, `Dr`, `by`, `from`) is masked even off the list | `test_cued_unlisted_name_masked` |
| A1.5 | `"Garcia's order"` → `<name>'s order` | `test_surname_possessive_masked` |
| A1.6 | `"ship to 42 Elm Street, Springfield today"` masks the address span in free text and in `sweep` | `test_address_in_sentence_masked` |
| A1.7 | `password=hunter2` / `pwd: abc` → value `<secret>` whatever its length, after a credential key | `test_short_credential_masked` |
| A1.8 | `x_sk_live_…`, `token_ghp_…` glued to word characters → `<secret>` | `test_glued_credential_masked` |
| A1.9 | `github_pat_…` and `xapp-…` masked (expected `already fixed`, `pii/sweep.py:47-57`) | `test_new_token_prefixes_masked` |
| A1.10 | zero-padded ids (`000123456`, ≥ 3 leading zeros, no separators) are not `<phone>`; any value with `+` or phone separators still is | `test_zero_padded_id_not_phone` |
| A1.11 | relative/absolute paths (`/var/data/x.csv`, `s3://bucket/key`) are not `<secret>` unless a token pattern matches inside | `test_path_not_secret` |
| A1.12 | `1.2.3` and `v10.0.1` are not `<ip>`; a valid dotted quad still is | `test_version_not_ip` |
| A1.13 | place/brand names before a business noun (`Austin Store`) — expected `ruled out` (recall: a name list cannot tell them apart; documented) | — |
| A1.14 | `password: varchar(255)` (a SQL type after a credential key) is not `<secret>` | `test_credential_key_with_sql_type_kept` |
| A1.15 | `_contains` handles class-range interiors (`[a-f]` contains `c`) | `test_policy.py::test_contains_class_range_interior` |
| A1.16 | detector functions typed `Callable[[str], bool]`; no `type: ignore` left in `detectors.py` | `test_hardening.py::test_detectors_have_no_type_ignore` (reads `detectors.py`; fails while any `type: ignore` remains; `mypy --strict` stays in the gate) |
| A1.17 | `docs/guide/pii.md` states that a hex token needs a letter and lists every token (`<email> <phone> <name> <iban> <card> <ip> <uuid> <secret> <address>`) | `tests/conformance/test_docs_examples.py::test_pii_tokens_documented` |

- [ ] **Step 1:** Run the table's red-first cycle per `open` ID. For each trade, add a property test in `test_mask_props.py` asserting that every masked category in the existing corpus is still masked (`test_recall_not_reduced`).
- [ ] **Step 2: Gate, then commit.** Commit message: `fix(pii): close the 0.1.0 masking findings without losing recall`.

### Task 4: Hardening — read-only guard and InlineConnector (A2, A9.1–A9.4)

**Files:**
- Modify: `src/schemaguru/guard.py`, `src/schemaguru/catalog/executor.py`, `src/schemaguru/draft/verify.py`, `docs/guide/security.md`
- Test: `tests/unit/test_guard.py`, `tests/unit/catalog/test_executor.py` (also the home of the A9.1–A9.3 tests), `tests/unit/draft/test_verify.py`

**Interfaces:**
- Consumes: `check_sql(...) -> GuardVerdict` (`guard.py:279`), `check_metadata_sql(sql)` (`executor.py:50`), `InlineConnector` (`executor.py:535`), `inline_problem(value)` (`executor.py:355`), `check_predicate` (`draft/verify.py:70`). Signatures are unchanged.

| ID | After the fix | Red test |
|---|---|---|
| A2.1 | every table position of a FROM clause (after `,`, `JOIN`, `LATERAL`, inside parentheses) is path-checked: `SELECT * FROM t, 'x.parquet'` refused SG801 | `test_guard.py::test_file_in_any_from_item_refused` |
| A2.2 | `.avro .orc .bz2 .gzip` are refused like `.parquet` | `test_guard.py::test_more_file_extensions_refused` (parametrised) |
| A2.3 | `` SELECT `table_name` FROM information_schema.tables `` accepted; `` SELECT `a`; DROP TABLE x `` and `` SELECT `x` -- `y` `` still refused | `test_executor.py::test_backtick_identifiers_accepted_hostile_refused` |
| A2.4 | expected `ruled out`: accepting `SHOW` would loosen the guard beyond FactLang's FL703 guard, and no shipped reader sends it | — |
| A2.5 | backslash escapes are judged in every variant (expected `already fixed`, `test_executor.py:244`) | — |
| A2.6 | `FactLangExecutor.run` calls `check_metadata_sql` before delegating (expected `already fixed`, `executor.py:123,186`) | `test_executor.py::test_factlang_executor_runs_local_guard` |
| A2.7 | `check_predicate` refuses `；` (U+FF1B) and `－－` (U+FF0D×2) outside literals | `test_verify.py::test_fullwidth_separators_refused` |
| A2.8 | `inline_problem` refuses U+2018, U+2019, U+201A, U+201B, U+2032, U+02BC, U+FF07, U+0060 inside a string value → SG109 (a non-Unicode code page may map them to `'`) | `test_executor.py::test_inline_refuses_quote_lookalikes` (parametrised) |
| A2.9 | a `datetime` whose `tzinfo.utcoffset` raises → `CatalogError` `SG109`, never `RuntimeError` | `test_executor.py::test_inline_raising_tzinfo_is_sg109` |
| A9.1 | the `InlineConnector` class docstring documents the trino `… UTC` timestamp form and the databricks zone-less refusal | `test_executor.py::test_inline_connector_docstring_lists_forms` (asserts both phrases in `InlineConnector.__doc__`) |
| A9.2 | the FL507 tests pin the reason, not only `SG109`: the message names `precision 39` and `64-bit` | `test_executor.py::test_fl507_refusals_pin_reason` |
| A9.3 | `test_trino_rejects_the_old_timestamp_form` asserts only the old form; the zone-less form gets its own test | `test_executor.py::test_trino_rejects_the_old_timestamp_form` + `test_trino_rejects_zone_less_timestamp` |
| A9.4 | expected `ruled out`: `literal_refusal` shipped as a public name in 0.1.1; renaming removes a released name (§3). Keep it; document it in `docs/guide/security.md` under `## Known limits` | — |

- [ ] **Step 1:** Run the red-first cycle per `open` ID. Every accepted-form test sits next to a still-refused hostile form in the same test.
- [ ] **Step 2: Gate, then commit.** Commit message: `fix(guard): tighten path and look-alike handling; false-rejection fixes keep hostile forms refused`.

### Task 5: Hardening — crawl, resume and measurement (A3)

**Files:**
- Modify: `src/schemaguru/catalog/crawl.py`, `src/schemaguru/catalog/readers/*.py` (`list_schemas`), `src/schemaguru/measure/{scheduler,budget,candidates,convert,flbridge}.py`
- Test: `tests/unit/catalog/test_crawl.py`, `tests/unit/catalog/test_readers_warehouse.py`, `tests/unit/measure/{test_scheduler,test_budget,test_candidates,test_convert}.py`, `tests/architecture/test_layers.py`

**Interfaces:**
- Consumes: `crawl(reader, store, *, config, resume, incremental, now, crawl_bytes)` (`crawl.py:408`), `_scope_hash` and `STATE_KEY`/`LAST_KEY` (`crawl.py:37-38,73`).

| ID | After the fix | Red test |
|---|---|---|
| A3.1 | `crawl(resume=True)` after a scope (include/exclude) change discards the cursor and crawls from the top | `test_crawl.py::test_resume_with_changed_scope_restarts` |
| A3.2 | on resume, a schema listed before the cursor that the store has no row for is crawled | `test_crawl.py::test_resume_crawls_schema_created_before_cursor` |
| A3.3 | the crawl's temp dir is gone after success and after an exception | `test_crawl.py::test_crawl_removes_temp_dir` |
| A3.4 | a reader that does not page schemas returns `Page([], None)` for any non-`None` `page_token` | `test_readers_warehouse.py::test_list_schemas_respects_page_token` (parametrised over `dialects()`) |
| A3.5 | a crashed explicit-refs measure re-queues only tables that were queued before it | `test_scheduler.py::test_explicit_crash_requeues_only_queued` |
| A3.6 | the original exception propagates when `queue_push` also fails (the push failure is added as a note) | `test_scheduler.py::test_queue_push_failure_keeps_original` |
| A3.7 | DISTINCT precedence is fixed: an exact profile count beats a keyhash estimate in any batch order | `test_convert.py::test_distinct_precedence_order_independent` |
| A3.8 | the background drain back-off resets to the base interval after a success | `test_scheduler.py::test_backoff_resets_after_success` |
| A3.9 | the per-period refill subtracts in-flight charges; total admitted ≤ budget | `test_budget.py::test_refill_with_inflight_never_overshoots` |
| A3.10 | one fused FactLang probe is charged its bytes once (check against FactLang 0.1.3 `Measurement`) | `test_budget.py::test_fused_probe_charged_once` |
| A3.11 | a sampled join never reports `1:1` from a sampled max-per-key of 1 (cardinality `None`, "sampled") | `test_convert.py::test_sampled_cardinality_not_one_to_one` |
| A3.12 | no rule-2 candidate between two bare `id` columns | `test_candidates.py::test_no_id_to_id_candidate` |
| A3.13 | `flbridge` imports only FactLang public modules (`factlang`, `factlang.ir`, `factlang.sql`, `factlang.connectors`, `factlang.agent`) | `test_layers.py::test_factlang_public_modules_only` + negative control |

- [ ] **Step 1:** Run the red-first cycle per `open` ID.
- [ ] **Step 2: Gate, then commit.** Commit message: `fix(crawl,measure): resume, queue and budget edge cases from 0.1.0`.

### Task 6: Hardening — readers (A4)

**Files:**
- Modify: `src/schemaguru/catalog/readers/{postgres,oracle,snowflake,bigquery,redshift,_common}.py`, `src/schemaguru/catalog/crawl.py`, `src/schemaguru/catalog/protocol.py`, `tests/conformance/record_warehouses.py`, `docs/wiki/Warehouse-BigQuery.md`, `tests/fixtures/catalog/*/{queries,expected}.json` (regenerated, never hand-edited), `tests/integration/catalog/engines.py`, `docs/wiki/Warehouse-{PostgreSQL,Oracle,Snowflake,BigQuery,Redshift}.md`
- Test: `tests/unit/catalog/test_readers_warehouse.py`, `tests/unit/catalog/test_reader_warehouse_helpers.py`, `tests/integration/catalog/test_reader_postgres.py`, `tests/unit/catalog/test_crawl.py`, `tests/conformance/{test_readers,test_replay,test_wiki_facts}.py`

**Interfaces:**
- Produces: `BillingReader(Protocol)` in `catalog/protocol.py` with `bytes_billed: int` and `bytes_billed_is_lower_bound: bool`; `crawl` uses `isinstance(reader, BillingReader)` (runtime_checkable) instead of `getattr`.

| ID | After the fix | Red test |
|---|---|---|
| A4.1 | Postgres FK with `convalidated = false` → `Constraint(enforced=False)` | `test_readers_warehouse.py::test_postgres_not_valid_fk_unenforced` (Fake rules) |
| A4.2 | Postgres unique indexes not backed by a constraint → `Constraint("unique", enforced=True)` | `…::test_postgres_unique_index_reported` |
| A4.3 | integration seeds a partitioned parent and an unanalysed table: the parent's `partitioning` is set, the unanalysed `row_count is None` | `test_reader_postgres.py::test_partitioned_and_unanalysed` |
| A4.4 | catalog row counts render `(catalog stats)` and never become MEASURED (expected `already fixed`, `render/table_block.py:750`) | — |
| A4.5 | Oracle partitioning joins on the table row's owner, so a NULL `c.owner` keeps the partitioning | `…::test_oracle_partitioning_null_owner` |
| A4.6 | Oracle MV comments come from `ALL_MVIEW_COMMENTS` | `…::test_oracle_mv_comment` |
| A4.7 | Snowflake `clustering` holds each key expression verbatim (`LINEAR(a, b)` → `("a", "b")`; `to_date(ts)` → `("to_date(ts)",)`) | `…::test_snowflake_clustering_expressions` |
| A4.8 | malformed Snowflake metadata raises `CatalogError("SG102", …)` | `…::test_snowflake_bad_metadata_is_catalog_error` |
| A4.9 | Snowflake listings over 10,000 objects page (no single capped `SHOW`) | `…::test_snowflake_lists_past_show_cap` |
| A4.10 | BigQuery `_PARTITIONTIME`/`_PARTITIONDATE` are not reported as columns | `…::test_bigquery_pseudo_columns_hidden` |
| A4.11 | `TABLE_STORAGE` is qualified with the dataset's project | `…::test_bigquery_cross_project_storage` |
| A4.12 | `docs/wiki/Warehouse-BigQuery.md` documents the default region qualifier and how to set it | `tests/conformance/test_wiki_facts.py::test_bigquery_region_documented` |
| A4.13 | BigQuery FK columns join on `(constraint_catalog, constraint_schema, constraint_name)` plus position; same-named constraints in two datasets never cross | `…::test_bigquery_fk_join_full_key` |
| A4.14 | Redshift reads schema-wide info once per schema (query count over 3 pages == 1) | `…::test_redshift_schema_info_once` |
| A4.15 | Redshift `capabilities().incremental` is `True` only if `list_tables` honours `changed_since` | `…::test_redshift_incremental_truthful` |
| A4.16 | `BillingReader` protocol (above) | `tests/unit/catalog/test_crawl.py::test_billing_reader_protocol` |
| A4.17 | `record_warehouses` handlers raise on a statement whose fingerprint they were not written for | `tests/conformance/test_replay.py::test_scripted_rejects_unknown_statement` |
| A4.18 | keyset paging and `ORDER BY` use one binary collation (`COLLATE "C"` Postgres/Redshift, `BINARY` MySQL, `Latin1_General_BIN2` T-SQL, default elsewhere): names `a, B, b, ä` across a page boundary are each listed once | `test_readers_warehouse.py::test_paging_collation_stable` (parametrised) |

- [ ] **Step 1:** Run the red-first cycle per `open` ID. Whenever a reader query changes, regenerate its fixtures:
  - Synthetic dialects (bigquery, snowflake, redshift, databricks, oracle): `python3 -m tests.conformance.record_warehouses <dialect>`.
  - DuckDB: `python3 -m tests.conformance.record duckdb`.
  - mysql, clickhouse, tsql, trino: start each with the CI image from `.github/workflows/ci.yml` and the same env vars (`docker run -d --rm -p 3306:3306 -e MYSQL_ROOT_PASSWORD=root mysql:8.4`, etc.), export `SCHEMAGURU_TEST_<ENGINE>_DSN` as in `ci.yml`, then run `python3 -m tests.conformance.record <engine>`.

  Review each `expected.json` diff by eye.
- [ ] **Step 2: Gate.** Also run `pytest -m integration tests/integration/catalog -rs` with the containers up, then commit. Commit message: `fix(readers): constraint, paging, billing and dialect edge cases from 0.1.0`.

### Task 7: Hardening — overlays, dbt and rules (A6)

**Files:**
- Modify: `src/schemaguru/overlays/{loader,schema,writer,dbt}.py`, `src/schemaguru/graph/waypoints.py`, `docs/guide/overlays.md`, `docs/wiki/Overlays.md`
- Test: `tests/unit/overlays/{test_loader,test_writer,test_dbt}.py`, `tests/unit/graph/test_waypoints.py`

| ID | After the fix | Red test |
|---|---|---|
| A6.1 | waiver `fact` must name a registered kind (SG208) and a subject glob that matches a stored ref (SG203 WARNING) | `test_loader.py::test_waiver_kind_and_subject_validated` |
| A6.2 | a duplicate mapping key → SG208 naming both lines; that entry is not applied | `test_loader.py::test_duplicate_keys_diagnosed` |
| A6.3 | glossary `maps_to` accepts tables only; a column → SG203 "is a column, expected a table" | `test_loader.py::test_glossary_maps_to_tables_only` |
| A6.4 | `facts_to_overlay` logs one WARNING naming each kind it cannot write, with counts; `export-drafts` prints it | `test_writer.py::test_unwritable_kinds_reported` |
| A6.5 | merging joins keys on `(to, from)`: a changed join replaces the old entry | `test_writer.py::test_changed_join_replaces` |
| A6.6 | the atomic write keeps an existing file's mode (new files `0o644` under umask) and re-checks the ownership header right before `os.replace` | `test_writer.py::test_atomic_write_mode_and_recheck` |
| A6.7 | expected `ruled out`: human joins are declarations; measured numbers come only from measurement (base §4.5) | — |
| A6.8 | ruled out [call] with docs: overlays.md Known limits "a malformed entry's previous facts are retracted; fix the entry and reload" | — |
| A6.9 | the single-unique rule counts every `unique` test attached to the table, resolvable or not | `test_dbt.py::test_unique_count_includes_unresolvable` |
| A6.10 | `overlays/dbt.py` has a module docstring (what is imported, all DECLARED/UNVERIFIED) | `test_dbt.py::test_module_docstring` |
| A6.11 | requirements merged across rules are capped at 16 → SG603 | `test_waypoints.py::test_merged_requirements_capped` |
| A6.12 | a rule ref with a quoted `(` (`shop."weird(name"`) parses | `test_waypoints.py::test_quoted_paren_ref_accepted` |

- [ ] **Step 1:** Run the red-first cycle per `open` ID.
- [ ] **Step 2: Gate, then commit.** Commit message: `fix(overlays): validation, merge and write-safety findings from 0.1.0`.

### Task 8: Hardening — graph, render fragments, CLI, docs, tests, CI (A7, A8, A9.5)

**Files:**
- Modify: `src/schemaguru/graph/search.py`, `src/schemaguru/render/fragments.py`, `src/schemaguru/cli/commands.py`, `docs/guide/sampling.md`, `docs/wiki/Sampling.md`, `.github/workflows/ci.yml`, `tests/architecture/test_layers.py`, `tests/conformance/test_wiki_facts.py`, `tests/test_version.py`, `src/schemaguru/config.py`, `src/schemaguru/agent/{dispatch,tools}.py` (the `check_fact` claim cap, A9.5); `tests/golden/views/*` only if a fragment fix (A7.4–A7.6) moves a golden (regenerate with `SCHEMAGURU_UPDATE_GOLDEN=1`, review each diff by eye, stage the changed files explicitly); docs regenerated by `scripts/gen_docs.py` (`docs/reference/*`, `docs/generated/*`)
- Test: `tests/unit/graph/test_search.py`, `tests/unit/render/test_fragments.py`, `tests/unit/cli/test_cli.py`, `tests/unit/agent/test_dispatch.py`, `tests/unit/test_ci_workflow.py` (create)

| ID | After the fix | Red test |
|---|---|---|
| A7.1 | `Graph` memo growth is guarded by a lock: 8 threads × 50 `find_path` calls on one graph give results identical to serial runs | `test_search.py::test_shared_graph_thread_safe` |
| A7.2 | the refusal names the hop limit actually searched (`GraphConfig.hop_limit`), not a cap | `test_search.py::test_refusal_names_hop_limit` |
| A7.3 | the via-success test asserts expansions/visited counts, not wall time | `test_search.py::test_via_success_bounded_work` |
| A7.4 | `ZORBClient`-style identifiers (ALLCAPS run + CamelCase) are quoted in fragments | `test_fragments.py::test_allcaps_camel_quoted` |
| A7.5 | qualified `t.isDeleted` → `t."isDeleted"` (dialect quoting) | `test_fragments.py::test_qualified_camel_quoted` |
| A7.6 | an ambiguous case-insensitive match stays as written and emits SG506 | `test_fragments.py::test_ambiguous_case_match_sg506` |
| A8.1 | with `--format json` every error goes to stderr as `{"error": {"code", "message"}}` (expected `already fixed`, `cli/commands.py:129`) | `test_cli.py::test_json_errors_are_json` |
| A8.2 | the sampling pages state both thresholds: bytes when known, rows otherwise | `test_wiki_facts.py::test_sampling_thresholds_stated` |
| A8.3 | ruled out (Task 1) | — |
| A8.4 | expected `already fixed` (`.github/workflows/wiki.yml` clone step) | — |
| A8.5 | a negative control mutates a copy of the real `CLI.md` (drops one row) and the checker fails | `test_wiki_facts.py::test_negative_control_real_page` |
| A8.6 | the real-tree walker (`_py_files` + `check_layering`) flags a violation in a tmp package tree | `test_layers.py::test_walker_flags_tmp_tree` |
| A8.7 | importing the facade reports "imports the public facade 'schemaguru'; import the defining module" | `test_layers.py::test_facade_import_message` |
| A8.8 | the no-op comprehension is removed (`test_layer_table_matches_spec_5_1`) | none (review only, no behaviour); ledger evidence = `test tests/architecture/test_layers.py::test_layer_table_matches_spec_5_1` (the existing test) |
| A8.9 | real-tree negative controls for nested `TYPE_CHECKING` and the I2 guard | `test_layers.py::test_nested_type_checking_real_tree` |
| A8.10 | `tests/test_version.py` asserts `__all__[-1] == "__version__"` (expected `already fixed`) | — |
| A8.11 | the `denylist` job fails on a push to `main` when `SCHEMAGURU_DENYLIST` is empty and skips on forks and PRs from forks | `test_ci_workflow.py::test_denylist_fails_on_main_without_secret` (parses `ci.yml`) |
| A9.5 | `check_fact` refuses a call with more claims than `Config.check_max_claims` (default 200; `None` disables) before any warehouse spend, as a tool error with code `too_many_claims`; SchemaGuru's own cap because `measure/flbridge.py` calls `Checker.check` directly and bypasses FactLang's `max_claims`. The `check_fact` tool spec description states the cap. Then run `python3 scripts/gen_docs.py` | `tests/unit/agent/test_dispatch.py::test_check_fact_claim_cap` (201 claims refused with `too_many_claims`, the backend's `check` never called; exactly 200 accepted; `None` accepts 201) |

- [ ] **Step 1:** Run the red-first cycle per `open` ID. After this task, the ledger has no `open` row.
- [ ] **Step 2: Gate, then commit.** Commit message: `fix: graph, fragment, CLI, docs and CI findings from 0.1.0; ledger closed`.

---

### Task 9: Lineage fact kinds and their resolution rules

**Files:**
- Create: `src/schemaguru/model/lineage.py`
- Modify: `src/schemaguru/model/kinds.py`, `src/schemaguru/model/facts.py` (`__post_init__`), `src/schemaguru/model/__init__.py`, `docs/extending/fact-kind.md`
- Test: `tests/unit/model/test_lineage.py`, `tests/unit/model/test_kinds.py` (extend `SPEC_KINDS` with `LINEAGE`, `COLUMN_LINEAGE`), `tests/unit/knowledge/test_resolve.py`

**Interfaces:**
- Produces (in `model/kinds.py`): `FactKind.LINEAGE = _k("LINEAGE", "table", identity=("upstream", "ignore"))` and `FactKind.COLUMN_LINEAGE = _k("COLUMN_LINEAGE", "column", identity=("inputs", "ignore"))`. Also `LINEAGE_KINDS: frozenset[str] = frozenset({"LINEAGE", "COLUMN_LINEAGE"})`. Builder ruling: `ignore` is in the identity so that a human ignore entry and a replace entry with the same list never share an id; `source` is not in the identity (§4.1).
- Produces (in `model/lineage.py`):
  - `Level = Literal["table", "column"]`
  - `Transform = Literal["copy", "cast", "expression", "aggregate", "window", "unknown"]`, `TRANSFORMS: tuple[str, ...]`
  - `DBT_SOURCE = "dbt:manifest"`, `VIEW_SOURCE = "view_definition"`
  - `RawName = tuple[str | None, str | None, str]` (catalog, schema, name, as written)
  - `@dataclass(frozen=True) ResolvedTable(ref: str, columns: tuple[str, ...])` (`ref` is the canonical table ref; columns are in ordinal order)
  - `@dataclass(frozen=True) LineageEdge(upstream_key: str, downstream_key: str, level: Level, fact_id: str)`
  - `table_lineage_value(upstream: Iterable[str] | None, unresolved: Iterable[str] = (), *, ignore: bool = False) -> dict[str, Any]` → `{"upstream": sorted-unique list | None, "unresolved": sorted-unique list, "ignore": bool}`. `upstream=None` is allowed only with `ignore=True` (else `ValueError`).
  - `column_lineage_value(inputs: Iterable[str] | None, transform: str | None, unresolved: Iterable[str] = (), *, ignore: bool = False) -> dict[str, Any]` → `{"inputs", "transform", "unresolved", "ignore"}`. `transform` must be in `TRANSFORMS` unless `ignore` (then `None` is allowed).
  - `source_label(source: str | None) -> str`: `"dbt:manifest:<uid>"` → `"dbt:manifest"`, `"view_definition"` → itself, `"overlay:<file>:<line>"` → itself, anything else → itself or `"unknown"`.
  - `@dataclass(frozen=True) LineageLink(subject: str, level: Level, inputs: tuple[str, ...], transform: str | None, unresolved: tuple[str, ...], sources: tuple[str, ...], fact_ids: tuple[str, ...])`
  - `@dataclass(frozen=True) LineageResolution(links: tuple[LineageLink, ...], hidden: tuple[Fact, ...], unmatched_ignores: tuple[Fact, ...])`
  - `resolve_lineage(facts: Iterable[Fact]) -> LineageResolution`
  - `edges_of(resolution: LineageResolution) -> list[LineageEdge]`
- Rules for `resolve_lineage` (§4.1), applied per `(subject, level)` and independent of input order:
  1. HUMAN facts with a list and `ignore` false replace every DECLARED/CATALOG fact of that subject and level. Each HUMAN list is its own link.
  2. `ignore` without a list hides every parsed fact. `ignore` with a list hides only parsed facts whose sorted `upstream`/`inputs` equals it exactly. An ignore that hides nothing goes to `unmatched_ignores`.
  3. Otherwise every DECLARED/CATALOG fact is effective. Facts with an equal `(inputs, transform, unresolved)` merge into one link: `sources` is the sorted union of `source_label`, `fact_ids` sorted.

  Links are sorted by `(subject, level, inputs, transform)`. `edges_of` emits one edge per (input, fact id); unresolved names are never edges.
- `Fact.__post_init__`: a kind in `LINEAGE_KINDS` with status ≠ `UNVERIFIED` or provenance ∉ {DECLARED, CATALOG, HUMAN} raises `ValueError("lineage is declared or parsed, never measured or verified")`.

- [ ] **Step 1: Write the failing tests** in `test_lineage.py`:
  - `test_lineage_never_verified`: `Fact.make(FactKind.LINEAGE, t, table_lineage_value(["shop.orders"]), Provenance.HUMAN, Status.VERIFIED, evidence=Evidence())` raises; so does `Status.OBSERVED`. The MEASURED arm is `Fact.make(..., Provenance.MEASURED, Status.UNVERIFIED, method="exact", evidence=Evidence())` under `pytest.raises(ValueError, match="never measured or verified")` (with `method="exact"` the existing MEASURED-needs-a-method check cannot fire, so only the new rule can).
  - `test_value_builders_sort_and_validate`: `table_lineage_value(["b", "a", "a"])["upstream"] == ["a", "b"]`; `table_lineage_value(None)` raises; `column_lineage_value(["x"], "sum")` raises.
  - `test_id_ignores_source`: the same value with sources `dbt:manifest:model.a` and `dbt:manifest:model.b` gives the same id.
  - `test_human_replaces_parsed_per_level`: a HUMAN table-level link leaves COLUMN_LINEAGE parsed facts effective.
  - `test_ignore_without_list_hides_all`.
  - `test_ignore_with_list_hides_exact_only`: an ignore of `[a, b]` hides `[b, a]` but not `[a]`.
  - `test_unmatched_ignore_reported`.
  - `test_identical_lists_merge_sources`: dbt + view with an equal list → one link, `sources == ("dbt:manifest", "view_definition")`.
  - `test_order_independent`: hypothesis permutations give an equal `LineageResolution`.
  - `test_edges_skip_unresolved`.

  In `test_resolve.py`: `test_resolver_never_promotes_lineage` — `resolve()` over lineage facts (old and new `observed_at`, any waivers) keeps every status `UNVERIFIED`.
- [ ] **Step 2: Run them.** `pytest tests/unit/model/test_lineage.py tests/unit/knowledge/test_resolve.py -v`. Expected: FAIL (`AttributeError: LINEAGE`).
- [ ] **Step 3: Implement** the kinds, the `__post_init__` rule and `model/lineage.py`. Export the new names from `schemaguru.model`. Add the two kinds to the built-in table in `docs/extending/fact-kind.md`.
- [ ] **Step 4: Run them.** Same command. Expected: PASS.
- [ ] **Step 5: Gate, then commit.** `git add src/schemaguru/model/{lineage,kinds,facts,__init__}.py docs/extending/fact-kind.md tests/unit/model/test_lineage.py tests/unit/model/test_kinds.py tests/unit/knowledge/test_resolve.py` and `git commit -m "feat(model): LINEAGE and COLUMN_LINEAGE kinds with human-wins resolution"`.

### Task 10: Store — migration 3, the `lineage_edges` index, and 0.1.0 forward migration

**Files:**
- Create: `src/schemaguru/store/migrations/0003_lineage.sql`, `src/schemaguru/store/migrations/pg/0002_lineage.sql`
- Modify: `src/schemaguru/store/{protocol,sqlite,postgres,__init__}.py`, `src/schemaguru/diagnostics.py` (SG705), `docs/extending/store.md`, `docs/wiki/Extending-Store.md`
- Test: `tests/unit/store/contract.py`, `tests/unit/store/test_migrations.py`, `tests/unit/store/test_v010_store.py`, `tests/integration/store/test_postgres_store.py`

**Interfaces:**
- Consumes: `resolve_lineage`, `edges_of`, `LineageEdge`, `LINEAGE_KINDS` (Task 9).
- Produces: the DDL (SQLite; the Postgres version is equivalent):

  ```sql
  CREATE TABLE lineage_edges (
      warehouse      TEXT NOT NULL DEFAULT '',   -- reserved: one store holds one warehouse in 0.2.0
      upstream_key   TEXT NOT NULL,
      downstream_key TEXT NOT NULL,
      level          TEXT NOT NULL CHECK (level IN ('table', 'column')),
      fact_id        TEXT NOT NULL,
      PRIMARY KEY (warehouse, downstream_key, level, upstream_key, fact_id)
  ) WITHOUT ROWID;
  CREATE INDEX lineage_edges_upstream ON lineage_edges(upstream_key, level);
  CREATE INDEX lineage_edges_downstream ON lineage_edges(downstream_key, level);
  CREATE TABLE lineage_state (
      table_ref         TEXT PRIMARY KEY,
      definition_sha256 TEXT NOT NULL
  ) WITHOUT ROWID;
  ```
- Produces (in `store/protocol.py`): a runtime-checkable `LineageIndex(Protocol)` with:
  - `lineage_edges(self, keys: Sequence[str], *, direction: Literal["upstream", "downstream"], level: Level) -> list[LineageEdge]`. `"upstream"` returns edges whose `downstream_key ∈ keys`; `"downstream"` returns edges whose `upstream_key ∈ keys`. Results are sorted by `(downstream_key, upstream_key, fact_id)`, and keys are chunked like `facts_for`.
  - `lineage_state_get(self, refs: Sequence[ObjectRef]) -> dict[ObjectRef, str]`
  - `lineage_state_set(self, items: Mapping[ObjectRef, str | None]) -> None` (`None` deletes the row).

  Also `lineage_index(store: Store) -> LineageIndex | None`. `Store` itself is unchanged (custom stores keep working; Task 18 raises SG705 for downstream traversal on them).
- Edge maintenance: inside `put_facts` and `delete_facts` (same transaction), for each touched subject key of a lineage kind, delete that subject's edges of that level. Then re-insert `edges_of(resolve_lineage(<all lineage facts of the subject, read inside the transaction>))`.
- `SG705` meaning: "the store does not implement the lineage index (a custom Store): downstream lineage is unavailable".

- [ ] **Step 1: Write the failing tests.** In `contract.py` (runs for SQLite here and Postgres in integration):
  - `test_lineage_edges_follow_facts`: put a dbt + a view fact, then `lineage_edges(["shop.v"], direction="upstream", level="table")` returns both fact ids.
  - `test_lineage_edges_effective_only`: a HUMAN ignore removes the edges; deleting the ignore restores them.
  - `test_lineage_edges_same_transaction`: an exception inside `transaction()` after `put_facts` leaves no edges.
  - `test_lineage_state_roundtrip`.
  - `test_downstream_query_uses_index`: SQLite only; `EXPLAIN QUERY PLAN` mentions `lineage_edges_upstream`.

  In `test_migrations.py`: `test_v2_store_migrates_to_3` (a store created at version 2 opens read-write → versions `[1, 2, 3]`) and `test_readonly_v2_store_refused_until_migrated`. In `test_v010_store.py`: `test_v010_migrates_facts_and_views_unchanged` (version 3; facts equal `facts.json`; `lineage_edges` empty; golden views still equal). In `tests/integration/store/test_postgres_store.py`: `test_pg_v1_store_migrates` (create with `schema_sql.migrations("pg")[:1]` applied, put facts, reopen normally → version 2, facts equal).
- [ ] **Step 2: Run them.** `pytest tests/unit/store -v`. Expected: FAIL (`no such table: lineage_edges`).
- [ ] **Step 3: Implement** the migrations, `LineageIndex` and the edge maintenance in both stores. Register SG705 and run `python3 scripts/gen_docs.py`. Document `LineageIndex` in both store extension pages.
- [ ] **Step 4: Run them.** `pytest tests/unit/store -v`, and with a local `postgres:16` container: `SCHEMAGURU_TEST_POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/postgres pytest -m integration tests/integration/store -rs`. Expected: PASS.
- [ ] **Step 5: Gate, then commit.** Commit message: `feat(store): lineage_edges index kept in step with lineage facts; migration 3 / pg 2`.

### Task 11: Lineage parser (`schemaguru.lineage`, in-process)

**Files:**
- Create: `src/schemaguru/lineage/{__init__,dialects,parse}.py`
- Modify: `src/schemaguru/pii/mask.py` (`definition_template`), `pyproject.toml` (extra `lineage`, `all`, `dev`), `tests/architecture/test_layers.py` (`"schemaguru.lineage": 2`), `src/schemaguru/cli/commands.py` (`_MODULE_EXTRAS["sqlglot"] = "lineage"`, `_EXTRAS["lineage"] = "sqlglot"`)
- Test: `tests/unit/lineage/{__init__,test_parse,test_dialects}.py`, `tests/unit/pii/test_definition_template.py`, `tests/property/test_lineage_props.py`

**Interfaces:**
- Consumes: `RawName`, `ResolvedTable`, `Transform` (Task 9).
- Produces (in `dialects.py`):
  - `SQLGLOT_DIALECTS: Mapping[str, str]`, the identity map over `bigquery clickhouse databricks duckdb mysql oracle postgres redshift snowflake trino tsql`. It is kept here because only seam modules may import FactLang; a test pins it equal to `factlang.sql.SQLGLOT_NAMES`.
  - `DBT_ADAPTERS: Mapping[str, str] = {"sqlserver": "tsql", "synapse": "tsql", "fabric": "tsql", "mariadb": "mysql", "spark": "spark"}`
  - `sqlglot_dialect(name: str | None) -> str | None`: a key of `SQLGLOT_DIALECTS` → its value; a value of `DBT_ADAPTERS` (including `spark`, which is not in `SQLGLOT_DIALECTS`) → itself; anything else → `None`.
- Produces (in `parse.py`):
  - `FailureReason = Literal["parse_error", "too_large", "timeout", "unsupported_dialect", "worker_died"]`
  - `class LineageFailure(Exception)` with `.reason: FailureReason`; `str(e) == reason`, never SQL text.
  - `MAX_DEFINITION_CHARS = 1_000_000`
  - `lineage_available() -> bool` (`importlib.util.find_spec("sqlglot") is not None`)
  - `@dataclass(frozen=True) ColumnLineage(column: str, inputs: tuple[str, ...], transform: Transform, unresolved: tuple[str, ...])`
  - `@dataclass(frozen=True) ParsedLineage(upstream: tuple[str, ...], unresolved: tuple[str, ...], columns: tuple[ColumnLineage, ...])`
  - `extract_tables(sql: str, dialect: str) -> tuple[RawName, ...]`: relations read. CTE names and a recursive CTE's self reference are excluded. Sorted and unique.
  - `parse_lineage(sql: str, dialect: str, schema: Mapping[RawName, ResolvedTable], *, columns: bool = True) -> ParsedLineage`

  Both accept a bare query or `CREATE [OR REPLACE] [SECURE|MATERIALIZED] VIEW <name> [(<cols>)] [options] AS <query>` with comments and a trailing `;`. A column list renames the outputs. Both raise only `LineageFailure`: any other exception, including `RecursionError`, becomes `parse_error`. Both silence the `sqlglot` logger while they run (`logging.getLogger("sqlglot").disabled = True`).

  `upstream` holds the `ResolvedTable.ref` values; names absent from `schema` go to `unresolved` as dotted text, as written. Column inputs are canonical column refs (`ObjectRef.parse(ref)` + column → `.canonical()`); an input of an unresolved table goes to `unresolved` as `<name>.<col>`. `SELECT *` expands from `ResolvedTable.columns`; a `*` over an unresolved table yields no columns (§2 non-goal).

  Transform classification (§4.1):
  - a bare column (aliased or not) → `copy`
  - only `CAST`, `::` or `TRY_CAST` around a bare column → `cast`
  - any aggregate → `aggregate`
  - a window function and no aggregate → `window` (its PARTITION/ORDER columns are inputs)
  - anything else → `expression`
  - not attributable → `unknown` with empty inputs

  Only projection lineage is recorded: WHERE/JOIN ON/GROUP BY/QUALIFY columns are not inputs. Set operations merge inputs per output position. `UNNEST`/lateral inputs are the array column with transform `expression`.
- Produces (in `schemaguru/pii/mask.py`, L3, so `lineage` (L2) never imports `pii`): `TEMPLATE_MAX_CHARS = 20_000` and `definition_template(sql: str) -> str | None`. String literals are replaced by `?` through the existing `sql_template` in the same module. Callers at L3 (crawl, dbt) hand it to `analyse_definitions` (Task 12). Numeric literals in code regions also become `?`. Returns `None` when unsure or when the result is over `TEMPLATE_MAX_CHARS` [builder call: bounds stored evidence].

- [ ] **Step 1: Write the failing tests.**
  - `test_dialects.py::test_table_matches_factlang`: `SQLGLOT_DIALECTS == factlang.sql.SQLGLOT_NAMES` (tests may import FactLang). Plus `test_dbt_adapter_aliases`, `test_dbt_adapter_targets_accepted` (`sqlglot_dialect("spark") == "spark"`, `sqlglot_dialect("tsql") == "tsql"`) and `test_unknown_dialect_none` (`sqlglot_dialect("nonsense") is None`).
  - `test_parse.py::test_constructs` is table-driven over the families ANSI/postgres, snowflake, bigquery, tsql and clickhouse. Each case is `(dialect, sql, schema, expected_upstream, expected_columns)`. The matrix is every §4.4 construct below × every family listed; a cell that does not apply is a `pytest.param(..., marks=pytest.mark.skip(reason="<why>"))` that names its reason (e.g. `no QUALIFY in postgres`), never silently absent. Constructs: plain/alias copy, cast (all three spellings), expression, aggregate (`SUM(o.amount)` → inputs `("shop.orders.amount",)`; `COUNT(*)` → inputs `()`), window, CTE, recursive CTE, subquery, `UNION ALL` (per-position merge), `INTERSECT`, `EXCEPT`, join, `UNNEST`/lateral, `QUALIFY` (no inputs), `SELECT *`, an unqualified column in a join with no schema (`unknown`), an unresolved table. Exact example:

    ```python
    SCHEMA = {(None, "shop", "orders"): ResolvedTable("shop.orders", ("order_id", "amount", "status")),
              (None, "shop", "refunds"): ResolvedTable("shop.refunds", ("order_id", "amount"))}
    p = parse_lineage("SELECT o.order_id, SUM(o.amount) - SUM(r.amount) AS net FROM shop.orders o "
                      "LEFT JOIN shop.refunds r ON r.order_id = o.order_id WHERE o.status = 'paid' "
                      "GROUP BY o.order_id", "postgres", SCHEMA)
    assert p.upstream == ("shop.orders", "shop.refunds") and p.unresolved == ()
    assert {c.column: (c.inputs, c.transform) for c in p.columns} == {
        "order_id": (("shop.orders.order_id",), "copy"),
        "net": (("shop.orders.amount", "shop.refunds.amount"), "aggregate")}
    ```
  - `test_parse_accepts_view_wrappers` (Review Focus 2): the DuckDB, Snowflake (`secure`, column list with comments, `copy grants`), MySQL (`ALGORITHM=UNDEFINED DEFINER=… SQL SECURITY DEFINER`), Redshift (`with no schema binding`) forms, a trailing `;` and a leading comment. Each yields the same upstream; the column list renames the outputs.
  - `test_star_over_wide_table`: 1,000 columns → 1,000 `copy` entries.
  - `test_failures_carry_no_sql`: `str(exc) == "parse_error"` for `"SELECT FROM WHERE 'secret@x.io'"`.
  - `test_columns_false_skips_column_work`.
  - `tests/unit/pii/test_definition_template.py::test_literals_replaced`: `definition_template("CREATE VIEW v AS SELECT * FROM c WHERE email = 'a@b.co' AND id = 4242 AND t1.col2 > 1e5") == "CREATE VIEW v AS SELECT * FROM c WHERE email = ? AND id = ? AND t1.col2 > ?"`. Plus `test_unsure_or_long_is_none`.
  - `test_lineage_props.py::test_never_raises_other_than_failure`: hypothesis builds fragments ≤ 200 chars from SQL tokens and junk; `parse_lineage`/`extract_tables` return or raise `LineageFailure` only (`deadline=None`, `max_examples=300`).
- [ ] **Step 2: Run them.** `pytest tests/unit/lineage tests/property/test_lineage_props.py -v`. Expected: FAIL (`ModuleNotFoundError: schemaguru.lineage`).
- [ ] **Step 3: Implement** the modules and `pyproject.toml` extras (`lineage`, plus `all` and `dev`), with `sqlglot` imported only inside functions. Add the layer entry. Run `pip install -e ".[dev]"`.
- [ ] **Step 4: Run them.** Same command, plus `pytest tests/architecture -v`. Expected: PASS.
- [ ] **Step 5: Gate, then commit.** Commit message: `feat(lineage): sqlglot-backed table and column lineage parser (extra lineage)`.

### Task 12: Parser worker process with hard limits; definition analysis

**Files:**
- Create: `src/schemaguru/lineage/worker.py`, `src/schemaguru/lineage/analyse.py`, `tests/unit/lineage/hostile_worker.py`
- Modify: `src/schemaguru/lineage/__init__.py`, `src/schemaguru/diagnostics.py` (SG211, SG212)
- Test: `tests/unit/lineage/{test_worker,test_analyse}.py`

**Interfaces:**
- Consumes: Task 11's `extract_tables`, `parse_lineage`, `LineageFailure`, `FailureReason`, `sqlglot_dialect`, `MAX_DEFINITION_CHARS`. Task 9's `table_lineage_value`, `column_lineage_value`, `FactKind.LINEAGE`/`COLUMN_LINEAGE`.
- Produces (in `worker.py`):
  - `DEFAULT_TIMEOUT_S = 5.0`
  - `@dataclass(frozen=True) ExtractRequest(sql: str, dialect: str)` and `@dataclass(frozen=True) LineageRequest(sql: str, dialect: str, schema: Mapping[RawName, ResolvedTable], columns: bool = True)`
  - `WorkerResult = tuple[RawName, ...] | ParsedLineage | LineageFailure`
  - `class ParserPool`:
    - `__init__(self, *, timeout_s: float = DEFAULT_TIMEOUT_S, max_chars: int = MAX_DEFINITION_CHARS, memory_limit_bytes: int | None = 1 << 30, command: Sequence[str] | None = None)`
    - `map(self, requests: Sequence[ExtractRequest | LineageRequest]) -> list[WorkerResult]`, in input order
    - `restarts: int` (property), `close()`, `__enter__`/`__exit__`
  - `serve(handler: Callable[[dict[str, Any]], dict[str, Any]]) -> None`: the line loop (read a JSON line from stdin, call `handler`, write the JSON reply to stdout, flush; exit on EOF). A handler may write to `sys.stdout` itself.
  - `handle(request: dict[str, Any]) -> dict[str, Any]`: the real request handler. `main() -> None` is `serve(handle)`, the entry point of `python -m schemaguru.lineage.worker`.

  Mechanism [builder ruling, see §4.4]:
  - `command` defaults to `[sys.executable, "-m", "schemaguru.lineage.worker"]`. This is a fresh interpreter like `multiprocessing` `spawn`, but it never re-imports the host's `__main__` (Review Focus 1).
  - The pool starts lazily and is reused; startup is paid once per pool.
  - The protocol is one JSON object per line over stdin/stdout. The child's stderr is `DEVNULL`.
  - A reader thread feeds a `queue.Queue`. The parent waits `timeout_s` per request (`Queue.get(timeout=…)`; this works on Windows pipes too).
  - On timeout, kill the child, wait for it, restart it, and fail that request with `timeout`. The cap is per request: a definition analysed in two `map` calls (extract, then lineage) can take up to 2 × `timeout_s` (recorded in the docs, Task 21).
  - On EOF, broken pipe or a non-JSON reply, restart and fail with `worker_died`.
  - Over `max_chars` → `too_large` without sending. `sqlglot_dialect(dialect) is None` → `unsupported_dialect` without sending.
  - The child applies `resource.setrlimit(RLIMIT_AS, …)` on Linux only and disables the `sqlglot` logger.
- Produces (in `analyse.py`):
  - `@dataclass(frozen=True) DefinitionItem(target: ObjectRef, sql: str, dialect: str)`
  - `@dataclass(frozen=True) DefinitionResult(target: ObjectRef, dialect: str, sha256: str, parsed: ParsedLineage | None, failure: FailureReason | None, template: str | None)`
  - `Resolver = Callable[[Sequence[RawName], ObjectRef], Mapping[RawName, ResolvedTable]]`
  - `analyse_definitions(items: Sequence[DefinitionItem], *, resolve: Resolver, pool: ParserPool, template: Callable[[str], str | None], columns: bool = True) -> list[DefinitionResult]`. It makes two `pool.map` calls per batch (extract, then lineage) and computes `sha256` in the parent and `template` through the injected callable (crawl and dbt, both L3, pass `schemaguru.pii.mask.definition_template`, so `lineage` imports nothing from `pii`). The raw SQL lives only in `items`.
  - `lineage_facts(result: DefinitionResult, *, target_columns: Sequence[str], provenance: Provenance, source: str, observed_at: datetime | None, evidence: bool) -> list[Fact]`:
    - One LINEAGE fact when `upstream` or `unresolved` is non-empty.
    - One COLUMN_LINEAGE fact per parsed column that matches a target column (exact, then unique case-insensitive). Others are skipped.
    - All facts are `UNVERIFIED`.
    - With `evidence=True` and a template, `Evidence(sql=template, dialect=result.dialect)` is attached to the LINEAGE fact only.
  - `failure_diagnostic(target: ObjectRef, reason: FailureReason) -> Diagnostic`: SG211 WARNING, message exactly `f"lineage could not be parsed for {target.canonical()}: {reason}"`, `location=target.canonical()`.
  - `extra_missing_diagnostic() -> Diagnostic`: SG212 INFO, message exactly ``"column-level lineage needs the `lineage` extra (pip install 'schemaguru[lineage]')"``.

- [ ] **Step 1: Write the failing tests.** `hostile_worker.py` calls `serve(handler)` with a handler that delegates to `handle` except: `sql == "HANG"` sleeps 60 s, `"DIE"` calls `os._exit(3)`, and `"GARBAGE"` writes a non-JSON line to `sys.stdout`. Tests in `test_worker.py`:
  - `test_timeout_kills_and_replaces`: `ParserPool(timeout_s=0.5, command=[sys.executable, "-m", "tests.unit.lineage.hostile_worker"])` (cwd = repo root). `.map([ExtractRequest("SELECT 1 FROM a", "duckdb"), ExtractRequest("HANG", "duckdb"), ExtractRequest("SELECT 1 FROM b", "duckdb")])`, mapped through `r.reason if isinstance(r, LineageFailure) else r`, equals `[((None, None, "a"),), "timeout", ((None, None, "b"),)]` (exceptions compare by identity, so the reason is compared, not the instance), with `restarts == 1` and elapsed < 5 s.
  - `test_death_reported_and_replaced` and `test_garbage_reply_is_worker_died`.
  - `test_too_large_not_sent` (`max_chars=10`) and its negative control `test_at_limit_is_sent`.
  - `test_unsupported_dialect`.
  - `test_startup_paid_once`: 50 requests, one child pid, `restarts == 0`.
  - `test_real_worker_parses` (the default command).
  - `test_worker_from_unguarded_script` (Review Focus 1): write a tmp `.py` file with no main guard that builds a `ParserPool` and prints `map([...])`. Run it with `subprocess.run([sys.executable, script], timeout=60)` → exit 0 and the expected output.

  In `test_analyse.py` (every call passes `template=definition_template`): `test_facts_from_result` (the ids are deterministic across runs), `test_evidence_is_template_only` (no literal of the input appears in `fact.to_dict()`), `test_failure_diagnostic_text_fixed`, `test_column_match_case_insensitive_unique`.
- [ ] **Step 2: Run them.** `pytest tests/unit/lineage -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `worker.py` and `analyse.py`. Register SG211/SG212 in `CODES` (meanings from §4.4) and run `python3 scripts/gen_docs.py`.
- [ ] **Step 4: Run them.** Same command. Expected: PASS on macOS; Linux and Windows run in CI.
- [ ] **Step 5: Gate, then commit.** Commit message: `feat(lineage): reused, killable parser worker with size, time and memory limits`.

### Task 13: Readers — `view_definitions` for every shipped reader

**Files:**
- Modify: `src/schemaguru/catalog/protocol.py`, `src/schemaguru/catalog/__init__.py`, `src/schemaguru/catalog/readers/{postgres,redshift,duckdb,snowflake,bigquery,databricks,mysql,clickhouse,tsql,oracle,trino}.py`, `tests/conformance/{replay,record,record_warehouses}.py`, `tests/integration/catalog/engines.py`, `tests/fixtures/catalog/*/{queries,expected}.json` (regenerated), `docs/extending/catalog-reader.md`, `docs/wiki/Extending-CatalogReader.md`, `docs/wiki/Warehouse-*.md` (one "View definitions:" line each)
- Test: `tests/unit/catalog/test_readers_factlang_guard.py`, `tests/unit/catalog/test_readers_warehouse.py`, `tests/unit/catalog/test_reader_duckdb.py`, `tests/conformance/test_readers.py`, `tests/integration/catalog/{test_readers_engines,test_reader_postgres}.py`

**Interfaces:**
- Produces (in `catalog/protocol.py`):
  - `ReaderCapabilities.view_definitions: bool = False` (a new last field)
  - a runtime-checkable `ViewDefinitionReader(Protocol)` with `view_definitions(self, tables: Sequence[ObjectRef]) -> Mapping[ObjectRef, str]`. The spec's `TableRef` is SchemaGuru's `ObjectRef`. The result is batched like `columns`, keyed by the exact refs passed, and holds views/materialized views only. A missing or empty definition is absent.
  - `reads_view_definitions(reader: CatalogReader) -> bool`: the capability is set and the method exists.

  `CatalogReader` is unchanged, so user readers work unchanged.
- Shipped readers report `view_definitions=True`. They use bind parameters for every value and read these catalog relations (confirm each against the engine docs and record any deviation in the module docstring):
  - Postgres: `pg_views.definition`, `pg_matviews.definition`
  - Redshift: `pg_views.definition` (late-binding and MVs where it exposes them)
  - DuckDB: `duckdb_views().sql`
  - Snowflake: `INFORMATION_SCHEMA.VIEWS.VIEW_DEFINITION`
  - BigQuery: `INFORMATION_SCHEMA.VIEWS.VIEW_DEFINITION`, plus materialized views from `INFORMATION_SCHEMA.MATERIALIZED_VIEWS` if it has a definition column, else `INFORMATION_SCHEMA.TABLES.ddl` for `table_type = 'MATERIALIZED VIEW'` (record the ruling)
  - Databricks: `information_schema.views.view_definition`
  - MySQL: `information_schema.VIEWS.VIEW_DEFINITION`
  - ClickHouse: `system.tables.as_select`
  - T-SQL: `sys.sql_modules.definition` joined to `sys.objects`/`sys.schemas` (no quote characters: FactLang's T-SQL guard)
  - Oracle: `ALL_VIEWS.TEXT`, `ALL_MVIEWS.QUERY`
  - Trino: `information_schema.views.view_definition`
- `tests/conformance/replay.py::read_all(...)` also calls `view_definitions` for every listed view/MV in batches of 200 when the reader declares the capability. It adds `"view_definitions": {canonical ref: text}` to its output.

- [ ] **Step 1: Write the failing tests.**
  - `test_readers_factlang_guard.py::_drive` also calls `reader.view_definitions([ref])`. Add `test_view_definition_sql_reads_catalog_only`, parametrised over `dialects()`: every captured statement passes `check_metadata_sql` and FactLang's guard, and every relation that `_util.sqltext.table_uses` finds is in a per-dialect `CATALOG_RELATIONS` allow-list defined in the test. Negative control: `SELECT * FROM shop.orders` is flagged. (`check_metadata_sql` has no relation allow-list to extend; the test pins it.)
  - `test_conformance` (`tests/conformance/test_readers.py`): `test_fixture_has_view_definitions` asserts that for each reader with the capability, `expected["view_definitions"]` is non-empty and covers every view in `expected["tables"]`.
  - `test_readers_warehouse.py::test_postgres_view_definitions` (Fake rules; Postgres has no recorded fixture dir) and `test_view_definitions_absent_for_tables`.
  - Integration `test_readers_engines.py::test_view_definition_live` (postgres, mysql, clickhouse, tsql, trino): the seeded view's definition parses (`extract_tables`) to the seeded base table.
- [ ] **Step 2: Run them.** `pytest tests/unit/catalog tests/conformance -v`. Expected: FAIL.
- [ ] **Step 3: Implement** each reader. Extend the recorders and the engine seeds (each seed already builds a view), then regenerate the fixtures exactly as in Task 6 Step 1. Review the `expected.json` diffs by eye.
- [ ] **Step 4: Run them.** The unit/conformance command, then `pytest -m integration tests/integration/catalog -rs` with the containers up. Expected: PASS.
- [ ] **Step 5: Gate, then commit.** Commit message: `feat(catalog): view_definitions for every shipped reader (metadata only)`.

### Task 14: Crawl integration — view lineage, re-crawl replacement, privacy

**Files:**
- Create: `src/schemaguru/store/names.py`, `tests/unit/lineage_helpers.py` (`privacy_warehouse(tmp_path)`: builds the DuckDB warehouse of the privacy test below and returns `(reader, literals)`; Task 18 and Task 22 reuse it)
- Modify: `src/schemaguru/catalog/crawl.py`, `src/schemaguru/cli/commands.py` (crawl prints `lineage_diagnostics`), `tests/unit/catalog/fake_reader.py` (adds `ViewFakeReader`)
- Test: `tests/unit/store/test_names.py`, `tests/unit/catalog/test_crawl_lineage.py`, `tests/unit/test_lineage_privacy.py` (part 1)

**Interfaces:**
- Consumes: `reads_view_definitions` (Task 13), `ParserPool`, `analyse_definitions`, `lineage_facts`, `failure_diagnostic`, `extra_missing_diagnostic`, `lineage_available` (Tasks 11–12), `definition_template` (`pii/mask.py`, Task 11), `lineage_index` (Task 10), `VIEW_SOURCE`, `LINEAGE_KINDS` (Task 9).
- Produces: `store/names.py::resolve_relation_names(store: Store, names: Sequence[RawName], default: ObjectRef) -> dict[RawName, ResolvedTable]`. Resolution order:
  - 3-part names as written.
  - 2-part names as `(default.catalog, s, n)`, then `(None, s, n)`.
  - 1-part names as `(default.catalog, default.schema, n)`, then `(None, default.schema, n)`.

  Each candidate goes through `store.resolve` (exact, then unique case-insensitive). Ambiguous or not found → absent from the result (so `unresolved`).
- Produces: `CrawlReport` gains `views_parsed: int = 0`, `lineage_s: float = 0.0` (wall time of the lineage phase; Task 19's `lineage_parse_s` reads it) and `lineage_diagnostics: list[Diagnostic] = field(default_factory=list)` (last fields). `crawl(...)` gains keyword-only `lineage: bool = True` and `parser_pool: ParserPool | None = None`. `lineage_diagnostics` never blocks the incremental baseline (`LAST_KEY` still depends on `errors` only).
- Lineage phase, run after the walk and withdrawal in `crawl()`, only when `lineage` is set and `reads_view_definitions(reader)`:
  - **Candidates:** live views/MVs written this run (refs recorded in the on-disk `_Seen`, table `v(ref TEXT PRIMARY KEY)`), plus live views/MVs the store holds with no `lineage_state` row (found in one streaming pass). The second set covers resumed crawls.
  - **Per batch** of `CrawlConfig.column_batch`: call `reader.view_definitions` (an exception → SG102 in `errors`). If a sha256 equals the stored `lineage_state` value, skip that view (its facts are left as they are). Otherwise build `DefinitionItem(target, sql, dialect=reader.dialect)` items and call `analyse_definitions(items, resolve=partial(resolve_relation_names, store), pool=pool, template=definition_template)`, then `lineage_facts(result, target_columns=…, provenance=CATALOG, source=VIEW_SOURCE, observed_at=now(), evidence=True)` per result.
  - **One transaction per batch:** retract the view's previous `view_definition` facts (`facts_for([view, *its columns], LINEAGE_KINDS)` filtered to `source == VIEW_SOURCE`), put the new facts, and set `lineage_state` to the sha256. On a failure, set `lineage_state` to `None` so the view is retried next crawl, and add SG211 to `lineage_diagnostics`.
  - **After the batch:** drop the raw texts.
  - **Withdrawal:** withdrawn views/MVs also lose their `view_definition` facts and `lineage_state`.
  - **Without the extra** (`not lineage_available()`): `reader.view_definitions` is never called (no raw SQL is held for nothing), no view facts and no state rows; one SG212 in `lineage_diagnostics` when the reader has the `view_definitions` capability. The SG212 text is unchanged (it names the `lineage` extra).

- [ ] **Step 1: Write the failing tests.**
  - `test_names.py::test_relation_names_case_and_quoting` (Review Focus 3): stored `shop.orders`, `shop."Order Items"`, `ops.Orders` + `ops.orders`. Then `(None, "SHOP", "ORDERS")` → `shop.orders`; `(None, "shop", "Order Items")` → resolved; `(None, "ops", "ORDERS")` → absent (ambiguous).
  - `test_crawl_lineage.py`:
    - `test_views_get_lineage_facts`
    - `test_unchanged_definition_not_reparsed` (spy on `ParserPool.map`: zero calls on the second crawl)
    - `test_changed_definition_replaces_facts` (old ids gone, new present, in one transaction)
    - `test_dropped_view_retracts_lineage`
    - `test_failure_is_sg211_and_crawl_continues` (a hostile pool)
    - `test_resume_parses_views_from_earlier_run`
    - `test_incremental_reads_only_changed_views`
    - `test_without_extra_one_sg212` (monkeypatch `lineage_available` → False; also asserts `view_definitions` is never called and one SG212 is reported)
    - `test_lineage_diagnostics_do_not_block_baseline`
    - `test_out_of_scope_upstream_is_unresolved` (Review Focus 4): the view reads `staging_raw.orders` under the default exclude → `unresolved == ("staging_raw.orders",)`.
  - `test_lineage_privacy.py::test_view_literals_never_stored_or_logged`: a DuckDB view (built by `lineage_helpers.privacy_warehouse`) `CREATE VIEW shop.v_vip AS SELECT * FROM shop.customers WHERE email = 'maria.garcia@example.com' AND phone = '+1 415 555 0100' AND customer_id = 73914286501 AND note <> 'hunter2secret'`. Crawl with `caplog.set_level(logging.DEBUG)`, close the store, then read the bytes of the `.db`, `-wal` and `-shm` files plus `export_json` output. None of the literals (and none of their UTF-8/UTF-16 encodings) appear in the bytes (the id literal has 11 digits so that it cannot occur by chance in the stores' sha256 hex strings), in `caplog.text` or in any diagnostic. The template evidence contains `email = ?`.
- [ ] **Step 2: Run them.** `pytest tests/unit/store/test_names.py tests/unit/catalog/test_crawl_lineage.py tests/unit/test_lineage_privacy.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `names.py` and the crawl phase.
- [ ] **Step 4: Run them.** Same command plus `pytest tests/unit/catalog -v`. Expected: PASS; the existing crawl tests are unchanged.
- [ ] **Step 5: Gate, then commit.** Commit message: `feat(crawl): parse view definitions into lineage; replace on change; never keep the SQL`.

### Task 15: dbt lineage and re-import retraction

**Files:**
- Modify: `src/schemaguru/overlays/dbt.py`, `src/schemaguru/overlays/__init__.py`, `src/schemaguru/guru.py`, `src/schemaguru/cli/commands.py` (`import_dbt` → `apply_dbt`; prints lineage counts), `src/schemaguru/diagnostics.py` (SG214), `.github/workflows/ci.yml` (job `no-extras`)
- Create: `tests/fixtures/dbt/{manifest_lineage,manifest_lineage_v2,manifest_uncompiled}.json`, `tests/unit/overlays/test_dbt_lineage.py`, `tests/minimal/test_lineage_without_extra.py`
- Test: `tests/unit/cli/test_cli.py`, `tests/unit/test_guru.py`, `tests/unit/test_ci_workflow.py`, `tests/property/test_overlay_props.py`

**Interfaces:**
- Consumes: Tasks 9–14 (`ParserPool`, `analyse_definitions`, `lineage_facts`, `resolve_relation_names`, `DBT_ADAPTERS`, `extra_missing_diagnostic`, `definition_template` from `pii/mask.py`, passed as `template=` to `analyse_definitions`).
- Produces:
  - `import_dbt(manifest_path, store, *, diagnostics=None, parser_pool: ParserPool | None = None) -> list[Fact]` (existing positional signature). It additionally emits:
    - `LINEAGE` per model/snapshot/seed node whose relation is in the store, built from `depends_on.nodes`. Dependencies that are ephemeral models are replaced by their own inputs, transitively and cycle-safe. Tests, macros and metrics are ignored. A relation not in the store goes to `unresolved` as `".".join(p for p in (database, schema, identifier or alias or name) if p)`. There is no fact when both lists are empty.
    - `COLUMN_LINEAGE` from `compiled_code`, dialect `DBT_ADAPTERS.get(a, a)` with `a = metadata.adapter_type`, when the extra is present.

    Both are DECLARED/UNVERIFIED with `source=f"dbt:manifest:{uid}"`. A manifest with no `compiled_code` anywhere adds one SG214 INFO. If `compiled_code` is present but the extra is missing → one SG212. Parse failures → SG211.
  - `DBT_LINEAGE_STATE = "dbt_lineage_facts"`
  - `retract_dbt_lineage(store: Store, loaded: Sequence[Fact]) -> int`: deletes stored `dbt:manifest` lineage facts that `loaded` no longer contains. The candidates are only `LINEAGE_KINDS` facts whose source starts with `"dbt:manifest:"`. Its state shape mirrors `overlays/loader.py::retract_removed`.
  - `apply_dbt(manifest_path, store, *, diagnostics=None, parser_pool=None) -> list[Fact]`: `import_dbt`, then `put_facts` and `retract_dbt_lineage` in one `store.transaction()`. An unreadable manifest (SG209) retracts nothing.
  - `Guru.import_dbt(self, manifest_path: str | os.PathLike[str]) -> list[Diagnostic]`: calls `apply_dbt`, invalidates caches, notes diagnostics.
  - `SG214` INFO: "the dbt manifest has no compiled_code (run `dbt compile`): only table-level lineage was imported". The spec names no code for this INFO [builder call].
  - CI job `no-extras` (ubuntu, Python 3.12): `pip install . pytest` then `pytest tests/minimal -p no:cacheprovider`.

- [ ] **Step 1: Write the failing tests** (`test_dbt_lineage.py`):
  - `test_table_lineage_from_depends_on` (sources and refs → `upstream`)
  - `test_ephemeral_inputs_inherited`
  - `test_tests_macros_metrics_not_relations`
  - `test_unresolved_relation_named`
  - `test_column_lineage_from_compiled_code` (postgres adapter; `net_revenue` ← two inputs, `aggregate`)
  - `test_adapter_alias_sqlserver_is_tsql`
  - `test_adapter_spark_not_unsupported` (a `spark` manifest's `compiled_code` parses; no SG211 `unsupported_dialect`)
  - `test_uncompiled_manifest_one_sg214_table_level_only`
  - `test_reimport_retracts_removed_lineage` (`manifest_lineage_v2` drops one dependency → its fact is gone; descriptions from 0.1.0 kinds are untouched)
  - `test_bad_manifest_retracts_nothing`
  - `test_never_raises` (extends `tests/property/test_overlay_props.py::test_import_dbt_never_raises` to the lineage path)

  Also: `test_cli.py::test_import_dbt_reports_lineage`, `test_guru.py::test_guru_import_dbt`, and `tests/minimal/test_lineage_without_extra.py::test_table_level_without_sqlglot`. The minimal test uses only stdlib, pytest and schemaguru: a SQLite store with tables upserted directly, then `apply_dbt` on `manifest_lineage.json` → LINEAGE facts, no COLUMN_LINEAGE, exactly one SG212. It also makes `sqlglot` unimportable (`monkeypatch.setitem(sys.modules, "sqlglot", None)`), so the same assertions hold in the `[dev]` job where sqlglot is installed. Finally `test_ci_workflow.py::test_no_extras_job`.
- [ ] **Step 2: Run them.** `pytest tests/unit/overlays tests/minimal tests/unit/cli/test_cli.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Register SG214 and run `python3 scripts/gen_docs.py`.
- [ ] **Step 4: Run them.** Same command. Expected: PASS.
- [ ] **Step 5: Gate, then commit.** Commit message: `feat(dbt): table and column lineage from manifests; retract on re-import`.

### Task 16: Overlays — the `lineage:` section with ignore semantics

**Files:**
- Modify: `src/schemaguru/overlays/{schema,loader}.py`, `src/schemaguru/guru.py` (`load_overlays`), `src/schemaguru/diagnostics.py` (SG213), `docs/guide/overlays.md`, `docs/wiki/Overlays.md`, `src/schemaguru/cli/commands.py` (`init` skeleton gains a commented `lineage:` example)
- Create: `tests/fixtures/overlays/lineage.yaml` (the §4.9 example)
- Test: `tests/unit/overlays/test_loader_lineage.py`, `tests/unit/test_guru.py`

**Interfaces:**
- Consumes: `table_lineage_value`, `column_lineage_value`, `resolve_lineage`, `TRANSFORMS` (Task 9).
- Produces:
  - `"lineage"` in `overlays/schema.py::TOP_KEYS`, and `LINEAGE_KEYS = ("table", "column", "upstream", "inputs", "transform", "ignore")`.
  - Each entry becomes one HUMAN/UNVERIFIED fact with `source=overlay:<file>:<line>`.
  - Validation, all SG208 unless noted:
    - exactly one of `table`/`column`
    - `upstream` only with `table`, `inputs` only with `column`
    - an unresolvable or ambiguous ref → SG203/SG204, and the entry is dropped
    - `transform` ∈ `TRANSFORMS`, default `"unknown"` when absent
    - `ignore` must be a bool
    - a non-ignore entry needs a list

  - `lineage_ignore_diagnostics(store: Store, loaded: Sequence[Fact]) -> list[Diagnostic]` in `overlays/loader.py`: for each HUMAN ignore fact, `resolve_lineage(store.facts_for([subject], LINEAGE_KINDS)).unmatched_ignores` → SG213 WARNING "lineage ignore entry matches no parsed link", located at the fact's source minus `overlay:`. `Guru.load_overlays` appends these after `put_facts`/`retract_removed`; `schemaguru lint` therefore reports them.
  - SG213 is registered in `CODES`.

- [ ] **Step 1: Write the failing tests** (`test_loader_lineage.py`):
  - `test_spec_example_loads` (three facts, sources with line numbers)
  - `test_validation_errors` (parametrised over each rule above)
  - `test_human_entry_replaces_parsed_edges`
  - `test_ignore_hides_link_and_is_kept_in_store` (the hidden fact is still in `facts_for`)
  - `test_unmatched_ignore_sg213`
  - `test_removed_entry_retracted_and_edges_restored` (via `retract_removed`)
  - `test_ignore_after_view_redefinition` (Review Focus 5): crawl a view, load an ignore of its parsed list, redefine the view and re-crawl, reload → SG213, `lineage_edges` holds the new link only, and nothing the ignore hid returns.
- [ ] **Step 2: Run them.** `pytest tests/unit/overlays/test_loader_lineage.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Run `python3 scripts/gen_docs.py`.
- [ ] **Step 4: Run them.** Same command plus `pytest tests/unit/overlays tests/property/test_overlay_props.py`. Expected: PASS.
- [ ] **Step 5: Gate, then commit.** Commit message: `feat(overlays): human lineage entries; ignore hides parsed links, human always wins`.

### Task 17: Rendering lineage per profile

**Files:**
- Modify: `src/schemaguru/render/{knowledge,table_block,profiles}.py`
- Create: `tests/golden/lineage_knowledge.py`, `tests/golden/test_golden_lineage.py`, `tests/golden/views/lineage__{agent,planner,verifier,router}__{standard,lite}.txt`
- Test: `tests/unit/render/test_lineage_render.py`, `tests/golden/test_golden_views.py` (unchanged, must stay green)

**Interfaces:**
- Consumes: `resolve_lineage`, `LineageResolution`, `LineageLink`, `source_label`, `LINEAGE_KINDS` (Task 9).
- Produces:
  - `TableKnowledge.lineage: LineageResolution`, a new last field defaulting to the empty resolution. `collect` sends lineage facts to `resolve_lineage` and leaves them out of `resolve()`.
  - `Analysis.built_from: list[str]` holds the table-level line texts.
  - Column notes and verifier keyed lines use the existing `notes`/`keyed` lists.
- Exact copy (refs are canonical; resolved inputs come first, sorted, then unresolved inputs, sorted, rendered `<name> (not in catalog)`; a cap of 3 then `(+N more)`):
  - **Table line** (agent and planner, rank `LITE_UP`): `Built from: shop.orders, shop.payments (+1 more) · dbt:manifest (declared), view_definition (parsed) · unverified`. It goes after the warning lines and before `Answers` [builder call: base §25.13 keeps warnings first]. Source markers: dbt and overlay → `declared`, `view_definition` → `parsed`. There is one line per link of the table.
  - **Column note** (agent, rank `STANDARD_UP`): `← shop.orders.amount, shop.refunds.amount (aggregate)`. Several links are joined by `; `. A link with no inputs and no unresolved names is not shown.
  - **Footer** (agent): when any link is shown, the provenance gains `lineage dbt:manifest, view_definition (unverified)`.
  - **Verifier:** `LINEAGE shop.v_paid <- shop.orders, shop.payments` (the full list, no cap) at `LITE_UP`; `COLUMN_LINEAGE shop.v_paid.amount <- shop.orders.amount copy` at `STANDARD_UP` (column keyed).
  - **Router:** unchanged. No lineage text is ever worded `measured` or `VERIFIED`.
  - Every lineage line passes the existing `finish()` sweep.

- [ ] **Step 1: Write the failing tests** (`test_lineage_render.py`):
  - `test_agent_built_from_line`
  - `test_cap_and_more`
  - `test_unresolved_marked_not_in_catalog`
  - `test_merged_sources_one_line`
  - `test_column_arrow_notes`
  - `test_planner_table_line_only`
  - `test_verifier_keyed_lines`
  - `test_router_unchanged`
  - `test_no_lineage_renders_as_010` (a table without lineage gives byte-identical output with and without the new code path)
  - `test_lineage_lines_swept`: an unresolved name `"maria.garcia@example.com"` renders `<email> (not in catalog)`.
  - `test_never_says_measured`

  Golden: `lineage_knowledge.py` builds the shop store plus a dbt model `analytics.revenue_daily` (DECLARED), a view `shop.v_paid_orders` (CATALOG) and one overlay link (HUMAN). `test_golden_lineage.py` renders each profile at STANDARD and LITE for those tables through `assert_golden`.
- [ ] **Step 2: Run them.** `pytest tests/unit/render/test_lineage_render.py tests/golden -v`. Expected: FAIL (the new golden files are missing; everything else passes).
- [ ] **Step 3: Implement.** Create the goldens with `SCHEMAGURU_UPDATE_GOLDEN=1 pytest tests/golden/test_golden_lineage.py` and read every file. `git status --porcelain tests/golden/views` must show only `?? tests/golden/views/lineage__…` lines (untracked new files; `git diff --stat` cannot list them, and a ` M` line means an existing golden moved: a defect).
- [ ] **Step 4: Run them.** `pytest tests/golden tests/unit/render -v`. Expected: PASS.
- [ ] **Step 5: Gate, then commit.** Commit message: `feat(render): Built from / column lineage lines per profile, marked unverified`.

### Task 18: Lineage traversal and the `lineage` tool (Python, CLI, HTTP, MCP); anti-goals; privacy end-to-end

**Files:**
- Create: `src/schemaguru/knowledge/lineage.py`, `src/schemaguru/render/lineage.py`
- Modify: `src/schemaguru/guru.py` (`lineage`, `alineage`), `src/schemaguru/agent/tools.py` (`TOOL_NAMES`, spec, `ToolHost.lineage`), `src/schemaguru/agent/dispatch.py` (`_lineage` in `_HANDLERS`), `src/schemaguru/agent/prompt.py` (`USAGE` gains one line: "Use `lineage` to see what a table or column is built from or feeds; it is declared or parsed, never measured, and never a join path."), `src/schemaguru/cli/{main,commands}.py`, `src/schemaguru/server/app.py` (route plus the docstring table), `src/schemaguru/__init__.py` (export `LineageResult`), `docs/generated/tools.json`, `docs/reference/cli.md` (regenerated), `docs/wiki/CLI.md`, `docs/guide/{agents,server}.md`, `docs/wiki/{Agent-Integration,Server}.md`
- Test: `tests/unit/knowledge/test_lineage_traverse.py`, `tests/unit/agent/test_lineage_tool.py`, `tests/unit/agent/test_tools.py` (append `lineage` to `SPEC_TOOLS` and to the maxLength-caps test; assert the `ref` argument has `maxLength` 512), `tests/acceptance/test_spec_acceptance.py` (append `lineage` to the tool list in `test_g8_agent_native_surfaces`), `tests/unit/cli/test_cli.py`, `tests/unit/server/test_app.py`, `tests/unit/test_guru.py` (`EXPORTS` += `"LineageResult"`, inserted before `"__version__"`), `tests/unit/test_lineage_antigoals.py`, `tests/unit/test_lineage_privacy.py` (part 2), `tests/unit/lineage_helpers.py` (adds `alpha_beta_guru(tmp_path) -> Guru`: a Guru over tables `shop.alpha` and `shop.beta` with no JOIN facts and `LINEAGE beta ← alpha`; Task 22 reuses it)

**Interfaces:**
- Consumes: `LineageIndex`/`lineage_index` (Task 10), `resolve_lineage` (Task 9), `ToolHost`, `_result`, `outbound_mode`, `ToolError` (`agent/dispatch.py`).
- Produces (in `knowledge/lineage.py`):
  - `MAX_NODES = 200`, `MAX_DEPTH = 5`, `Direction = Literal["upstream", "downstream"]`
  - `@dataclass(frozen=True) LineageNode(ref: str, level: Level, depth: int, in_catalog: bool)`
  - `@dataclass(frozen=True) LineageEdgeView(upstream: str, downstream: str, sources: tuple[str, ...], transform: str | None)`
  - `@dataclass(frozen=True) LineageResult(root: str, direction: Direction, level: Level, depth: int, nodes: tuple[LineageNode, ...], edges: tuple[LineageEdgeView, ...], truncated: bool, cycles: tuple[tuple[str, ...], ...])`
  - `LineageResult.to_dict() -> dict[str, Any]` with keys `ref, direction, level, depth, nodes[{ref, level, depth, in_catalog}], edges[{upstream, downstream, source: [labels], transform}], truncated, cycles`
  - `traverse(store: Store, root: ObjectRef, *, direction: Direction = "upstream", depth: int = 2, level: Level | None = None, max_nodes: int = MAX_NODES) -> LineageResult`

  Traversal is breadth-first, with at most two store calls per depth. Upstream uses `facts_for(frontier, LINEAGE_KINDS)` + `resolve_lineage`; unresolved names become leaf nodes with `in_catalog=False`. Downstream uses `lineage_index(store).lineage_edges(frontier, direction="downstream", level=…)`, then `facts_for` on the found subjects for sources and transforms. A store without the index raises `SchemaGuruError("SG705 …")` for downstream only. The level is inferred from `root.column`. A visited node is never expanded twice. An edge back to an ancestor is recorded in `cycles` as the path, and the traversal stops at `max_nodes` with `truncated=True`. `depth` outside 1..5 raises `ValueError`.
- Produces (in `render/lineage.py`): `lineage_text(result: LineageResult) -> str`, with exact copy:

  ```
  Lineage upstream of analytics.revenue_daily (table level, depth 2) — declared/parsed, unverified; not a join path
    analytics.revenue_daily <- shop.orders [dbt:manifest]
    analytics.revenue_daily <- shop.payments [dbt:manifest, view_definition]
    shop.payments <- raw.payments (not in catalog) [view_definition]
  (truncated at 200 nodes)
  cycle: shop.a -> shop.b -> shop.a
  ```
  Column-level edges add ` (<transform>)` before the `[…]`. With nothing found: `No lineage recorded for <ref> (<direction>).`
- Produces: `Guru.lineage(self, ref: Target, direction: str = "upstream", depth: int = 2, level: str | None = None) -> LineageResult` and `async alineage(...)`. Refs resolve via `store.resolve`, then `table_ref`, then `column_ref`, like `server/app.py::subject_of`; `level="table"` on a column ref uses its table.
- Produces: tool `lineage`, appended after `explain_term` in `TOOL_NAMES`:
  - Description: "Which tables and columns a table or column is built from (upstream) or feeds (downstream), from dbt manifests, view definitions and overlays. Declared or parsed, never measured; not a join path."
  - Args: `ref` (string), `direction` (enum upstream/downstream), `depth` (integer 1..5), `level` (enum table/column); required `["ref"]`.
  - Result: `{"text": lineage_text(r), **r.to_dict()}` through `_result(..., outbound_mode(guru, tables))`.
- Produces: CLI `schemaguru lineage REF [--downstream] [--depth N] [--level table|column]` (global `--format`); the json output is the tool payload.
- Produces: HTTP `GET /lineage?ref=&direction=&depth=&level=` (reader role) → `guru.handle_tool_call("lineage", …)`; `depth` via `int_param`, `ref` via `need_param`. MCP and adapters inherit the tool via `guru.tools()`.

- [ ] **Step 1: Write the failing tests.**
  - `test_lineage_traverse.py`:
    - `test_upstream_depth_two`
    - `test_downstream_uses_index` (spy: `lineage_edges` is called, `iter_facts` is never called)
    - `test_column_level_inferred`
    - `test_cycle_reported_terminates` (a↔b via overlays)
    - `test_cap_200_truncates` (a fan-in of 300)
    - `test_depth_bounds`
    - `test_custom_store_downstream_sg705`
    - `test_unresolved_leaf`
    - `test_deterministic_order`
  - `test_lineage_tool.py`:
    - `test_tool_listed_last_with_schema` (also: `lineage` is in `SPEC_TOOLS`, `ref` has `maxLength` 512, and `USAGE` mentions `lineage`)
    - `test_tool_result_shape`
    - `test_bad_args_sg803`
    - `test_adapters_and_mcp_expose_lineage` (`importorskip("mcp")`; the `extras` CI job forbids the skip)
  - `test_cli.py::test_lineage_text_and_json`
  - `test_app.py::test_get_lineage` and `test_get_lineage_bad_depth_400`
  - `test_lineage_antigoals.py`: uses `lineage_helpers.alpha_beta_guru` (tables `shop.alpha` and `shop.beta` with no JOIN facts and `LINEAGE beta ← alpha`).
    - `test_find_path_refuses`: `guru.find_path("shop.alpha", "shop.beta")` is a `Refusal`.
    - `test_scope_does_not_add_upstream`: `guru.context("alpha")` does not list `shop.beta` through expansion.
    - `test_graph_reads_no_lineage`: the `Guru._current_graph` store call kinds are exactly `["JOIN", "ENTITY_TYPE"]` (a spy).
    - `test_snippet_unaffected`: the `find_join_path` result for the pair is an error with no SQL.
  - `test_lineage_privacy.py::test_literals_absent_from_every_surface`: `lineage_helpers.privacy_warehouse` (Task 14), crawled. None of the four literals appears in any of: `guru.context(...)` for all four profiles; `describe_table` and `lineage` tool results; `schema_context`; HTTP `/lineage`, `/tables/shop.v_vip`, `/facts?subject=shop.v_vip`; CLI `lineage --format json`; `guru.diagnostics`; `caplog.text`.
- [ ] **Step 2: Run them.** `pytest tests/unit/knowledge/test_lineage_traverse.py tests/unit/agent tests/unit/cli tests/unit/server tests/unit/test_lineage_antigoals.py tests/unit/test_lineage_privacy.py tests/unit/test_guru.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Regenerate with `python3 scripts/gen_docs.py`. Add the `lineage` row to `docs/wiki/CLI.md` and the route row to the server docs and docstring table.
- [ ] **Step 4: Run them.** Same command plus `pytest tests/conformance`. Expected: PASS.
- [ ] **Step 5: Gate, then commit.** Commit message: `feat(agent): lineage traversal and tool across Python, CLI, HTTP and MCP`.

### Task 19: Scale benchmark — generator, harness, comparator

**Files:**
- Create: `benchmarks/__init__.py`, `benchmarks/scale/{__init__,generator,questions,harness,compare}.py`, `tests/unit/benchmarks/{__init__,test_scale_generator,test_scale_compare}.py`

**Interfaces:**
- Consumes: `crawl`, `SQLiteStore`, `Guru(store=..., reader=...)`, or the lower-level pieces the harness needs (`ViewRenderer`/`build_scope` as `tests/golden` does, `Graph`, `traverse`). The harness is not library code; it may use internals.
- Produces (in `generator.py`):
  - `@dataclass(frozen=True) ScaleShape(tables: int = 500_000, columns_per_table: int = 50, tables_per_schema: int = 5_000, view_share: float = 0.10, fks_per_table: int = 2, comment_share: float = 0.5, max_lineage_depth: int = 6, seed: int = 20261125)`
  - `class SyntheticReader` (`CatalogReader` + `ViewDefinitionReader`, `dialect = "duckdb"`, capabilities with `incremental=True`, `constraints=True`, `view_definitions=True`), built as `SyntheticReader(shape, *, changed: frozenset[int] = frozenset(), changed_at: datetime | None = None)`. It serves pages lazily (O(page) memory).
  - `table_ref(i: int) -> ObjectRef`, `is_view(i: int) -> bool`, `upstream_of(i: int) -> tuple[int, ...]` (acyclic, chain depth ≤ `max_lineage_depth`), `definition(i: int) -> str`
  - `changed_set(shape: ScaleShape, fraction: float = 0.01, seed: int = 1) -> frozenset[int]`
  - `synthetic_join_facts(shape) -> Iterator[Fact]`: MEASURED `JOIN` facts for the declared FK pairs with deterministic match rates, labelled `method="exact"`, `source="benchmark:synthetic"` and documented as synthetic.
- Produces (in `questions.py`): `QUESTIONS: tuple[str, ...]` (200 fixed shop-domain questions), `path_pairs(shape: ScaleShape, n: int = 200) -> list[tuple[ObjectRef, ObjectRef]]` and `lineage_refs(shape: ScaleShape, n: int = 200) -> list[ObjectRef]` (views at depth ≥ 2).
- Produces (in `harness.py`): `run(shape: ScaleShape, workdir: Path, *, runner_class: str) -> dict[str, Any]`, plus a CLI `python3 -m benchmarks.scale.harness --tables N [--columns-per-table 50] --out PATH --runner-class NAME [--workdir DIR]`. The crawl phases run in a child process (`subprocess` of `python3 -m benchmarks.scale.harness --phase crawl …`), so the peak RSS (`resource.getrusage(RUSAGE_SELF).ru_maxrss`, normalised to bytes: Linux KiB, macOS bytes; `resource` is imported lazily inside the function, and on Windows the RSS metrics are `None` and are skipped by `compare`) belongs to the crawl alone; the parser worker's peak is `RUSAGE_CHILDREN` of that child. The result JSON has exactly these keys:

  ```json
  {"version": "0.2.0", "shape": {...ScaleShape...}, "size": {"tables": 0, "columns": 0, "views": 0},
   "environment": {"cpu": "", "cores": 0, "ram_bytes": 0, "python": "", "os": "", "runner_class": ""},
   "metrics": {"crawl_full_s": 0, "crawl_peak_rss_bytes": 0, "parser_peak_rss_bytes": 0,
               "crawl_incremental_s": 0, "incremental_ratio": 0, "lineage_parse_s": 0,
               "graph_build_s": 0, "context_p50_ms": 0, "context_p95_ms": 0, "path_p95_ms": 0,
               "lineage_upstream_p95_ms": 0, "lineage_downstream_p95_ms": 0, "store_bytes": 0},
   "known_limits": [], "nightly": null}
  ```
  Here `incremental_ratio = crawl_incremental_s / crawl_full_s` with 1% changed, and `lineage_parse_s` is the full crawl's `CrawlReport.lineage_s` (Task 14). Context latency is measured from the local store with a warm cache disabled (distinct questions). All p95 values come from 200 samples.
- Produces (in `compare.py`):
  - `HARD_TARGETS = {"crawl_peak_rss_bytes": 2 * 2**30, "incremental_ratio": 0.10, "context_p95_ms": 200.0, "lineage_upstream_p95_ms": 50.0, "lineage_downstream_p95_ms": 50.0}` (each value is an upper bound)
  - `REGRESSION_GATED = ("crawl_full_s", "path_p95_ms", "graph_build_s", "lineage_parse_s")` (`store_bytes` is reported, never gated: spec §6.1), `TOLERANCE = 0.25`
  - `compare(result: Mapping[str, Any], baselines: Sequence[Mapping[str, Any]]) -> list[str]`: the problems. Only baselines with the same `environment.runner_class` and `size.tables` count. With none, regression gating is skipped and the note `no baseline for <class> at <N> tables: regression gate inactive` is returned separately via `compare_notes(...) -> list[str]`.
  - CLI: `python3 -m benchmarks.scale.compare RESULT --baseline-dir benchmarks/results`; exits 1 on problems.

- [ ] **Step 1: Write the failing tests.**
  - `test_scale_generator.py`:
    - `test_deterministic` (two readers with the same seed give equal sha256 over the full listing at `tables=2_000`)
    - `test_shape_counts` (columns = tables × 50; views = 10% ± 1 table)
    - `test_lineage_acyclic_depth_bounded`
    - `test_pages_bounded_memory` (`tracemalloc` peak < 16 MiB listing 50,000 tables)
    - `test_changed_set_size`
    - `test_harness_small_run` (`tables=2_000`, tmp workdir; every metric key present, numeric or, for the RSS keys on Windows, `None`; runs in < 120 s; marked `skipif(sys.platform == "win32")`)
  - `test_scale_compare.py`:
    - `test_hard_target_miss`
    - `test_regression_over_25_percent`
    - `test_other_runner_class_ignored`
    - `test_no_baseline_note`
    - a negative control at exactly +25% that passes
- [ ] **Step 2: Run them.** `pytest tests/unit/benchmarks -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Same command. Expected: PASS.
- [ ] **Step 5: Gate, then commit.** Commit message: `feat(bench): deterministic 500k-table catalog generator, harness and regression comparator`.

### Task 20: Scale — full-size run, bottleneck fixes, nightly workflow, Scale page

**Files:**
- Create: `.github/workflows/bench.yml`, `benchmarks/results/0.2.0.json`, `docs/guide/scale.md`, `docs/wiki/Scale.md`, `tests/unit/test_bench_workflow.py`, `tests/conformance/test_scale_page.py`
- Modify: `mkdocs.yml` (nav "Scale" under Guide), `docs/wiki/_Sidebar.md`, `docs/guide/cost-and-scale.md` and `docs/wiki/Scale-and-Cost.md` (link to Scale), plus any `src/` files a bottleneck fix touches, with a test.

**Interfaces:**
- Consumes: Task 19 (`run`, `compare`, CLI flags).
- Produces, in `bench.yml`:
  - `on: schedule: [{cron: "17 3 * * *"}]` and `workflow_dispatch` with input `tables` (default = the nightly size below). No `push`/`pull_request` trigger.
  - Job `bench` on `ubuntu-latest`, `timeout-minutes: 350`. Steps: checkout; setup-python 3.12; `pip install -e ".[dev]"`; `python3 -m benchmarks.scale.harness --tables "$TABLES" --out bench.json --runner-class github-ubuntu-latest`; `python3 -m benchmarks.scale.compare bench.json --baseline-dir benchmarks/results`; `actions/upload-artifact@v4` with `bench.json`.
- Produces: `benchmarks/results/0.2.0.json`, the full 500,000-table / 25,000,000-column run with the local machine named in `environment`.

- [ ] **Step 1: Write the failing tests.**
  - `test_bench_workflow.py::test_triggers_and_limits`: triggers are exactly `{schedule, workflow_dispatch}`; timeout ≤ 360; it runs both harness and compare; it uploads the artifact.
  - `test_scale_page.py::test_page_quotes_committed_results`: every metric in `docs/guide/scale.md`'s results table equals `benchmarks/results/0.2.0.json` (formatted as on the page), and the page names the machine and the reproduce command. The same holds for `docs/wiki/Scale.md`.
- [ ] **Step 2: Run them.** `pytest tests/unit/test_bench_workflow.py tests/conformance/test_scale_page.py -v`. Expected: FAIL.
- [ ] **Step 3: Run the full size locally.** `python3 -m benchmarks.scale.harness --tables 500000 --out benchmarks/results/0.2.0.json --runner-class local-<hostname-free machine label, e.g. apple-m3-max-64g> --workdir <scratch dir with ≥ 60 GB free>`, then `python3 -m benchmarks.scale.compare benchmarks/results/0.2.0.json --baseline-dir benchmarks/results`.
- [ ] **Step 4: Fix any missed hard target.** Profile (`python3 -m cProfile -o prof.out -m benchmarks.scale.harness --phase <phase> …`). Fix within the existing architecture (an index, batching, a cache) with a red test that reproduces the slow path at small scale: an asserted query count, `EXPLAIN QUERY PLAN` index use, or a `benchmark`-marked latency test. Re-run Step 3. A miss that needs an architecture change goes into `known_limits` with its measured numbers, for the release notes.
- [ ] **Step 5: Choose the nightly size.** Project the local per-table store bytes, RSS and time onto `ubuntu-latest` (4 vCPU, 16 GiB RAM, ~14 GB free disk, 6 h job limit with a 350-minute budget). Choose the largest of `{500_000, 250_000, 100_000}` that fits with 30% headroom. Record `"nightly": {"tables": N, "reason": "<measured numbers that forced it>"}` in the results JSON, and set it as the workflow default. The GitHub-runner limit is therefore *projected*. The controller confirms it with the first `workflow_dispatch` run and commits that run's JSON as `benchmarks/results/baseline-github-ubuntu-latest.json`. Until then, `compare` reports the gate inactive.
- [ ] **Step 6: Write the Scale pages.** Both pages cover the shape, the machine, the results table, the targets with pass/fail, the nightly size and its reason, known limits, and the exact reproduce commands. Add the nav and sidebar entries.
- [ ] **Step 7: Run the checks.** `pytest tests/unit/test_bench_workflow.py tests/conformance -v`, then `mkdocs build --strict`. Expected: PASS.
- [ ] **Step 8: Gate, then commit.** Commit message: `feat(bench): published 500k/25M results, nightly regression workflow and Scale docs`.

### Task 21: Documentation, README, CHANGELOG, wiki

**Files:**
- Create: `docs/guide/lineage.md`, `docs/wiki/Lineage.md`
- Modify: `mkdocs.yml`, `docs/wiki/{_Sidebar,Home,Catalog-and-Crawl,Overlays,Rendering,Agent-Integration,Server,PII-and-Sampling-Policy,Security,FAQ,Glossary}.md`, `docs/guide/{crawl,overlays,rendering,agents,server,pii,security,concepts}.md`, `docs/index.md`, `README.md`, `CHANGELOG.md`, `CLAUDE.md` and `AGENTS.md` (layer table gains `lineage` = 2; the seam is unchanged), `.claude/skills/add-warehouse-reader/SKILL.md` (adds a `view_definitions` step)
- Test: `tests/conformance/{test_docs_examples,test_wiki_links,test_wiki_facts,test_docs_generated}.py`

**Interfaces:**
- Consumes: every public name from Tasks 9–20 as defined in their Interfaces blocks.
- `lineage.md` sections:
  - Sources (dbt `depends_on` and `compiled_code`; view definitions per reader, the §4.3 table; overlays)
  - The `lineage` extra (column-level needs it; view lineage of any level needs it; SG212)
  - The overlay syntax with ignore semantics (the §4.9 example)
  - What agents see (the copy from Task 17 and Task 18)
  - Privacy of view SQL (sha256 + literal-free template only)
  - Limits (1,000,000 chars, 5 s per parser request (a definition takes two: extract, then lineage), the 1 GiB memory cap on Linux only, `SELECT *` over unknown tables, projection-only lineage, never a join path, never verified)
  - Diagnostics SG211–SG214, SG705
- README gains a lineage example of ≤ 8 lines: `guru.import_dbt("target/manifest.json")`, then `print(guru.handle_tool_call("lineage", {"ref": "analytics.revenue_daily", "depth": 2})["text"])` (`guru.lineage(...)` returns a `LineageResult`, which has no `__str__`), and a mention of `pip install 'schemaguru[lineage]'`.
- `CHANGELOG.md` gets `## [0.2.0] - UNRELEASED` with Added (lineage, tools, `view_definitions`, scale benchmark), Changed, Fixed (one line per `fixed` ledger area) and Known limits (the `ruled out` user-visible items and benchmark `known_limits`).

- [ ] **Step 1:** Write the pages and edits. Run `python3 scripts/gen_docs.py && python3 scripts/build_demo_export.py`.
- [ ] **Step 2:** Run `pytest tests/conformance -v && mkdocs build --strict`. Expected: PASS. A failing wiki-facts test means a page is wrong, not the test.
- [ ] **Step 3: Gate, then commit.** Commit message: `docs: lineage guide, Scale links, README example, 0.2.0 changelog and wiki`.

### Task 22: Release preparation — version 0.2.0 and the acceptance sweep

**Files:**
- Modify: `src/schemaguru/__about__.py` (`__version__ = "0.2.0"`), `tests/test_version.py`, `CHANGELOG.md` (`## [0.2.0] - <today's date>`), `docs/specs/2026-11-25-schemaguru-0.2.0-hardening-ledger.md` (verify there is no `open` row)
- Create: `tests/acceptance/test_release_020.py`

**Interfaces:**
- Consumes: everything above. No new library names.

- [ ] **Step 1: Write the acceptance tests** (`test_release_020.py`, one test per 0.2.0 goal and anti-goal):
  - `test_version_is_020`: `schemaguru.__version__ == "0.2.0"` and `CHANGELOG.md` has a `## [0.2.0] - YYYY-MM-DD` heading (no `UNRELEASED`). This is the red-first test.
  - `test_l1_sources`: a DuckDB shop with a view, `manifest_lineage.json` and `tests/fixtures/overlays/lineage.yaml` → LINEAGE facts with provenance DECLARED, CATALOG and HUMAN, sources `dbt:manifest:*`, `view_definition` and `overlay:*:*`.
  - `test_l2_truthful`: every lineage fact is `UNVERIFIED`; a rendered agent view and the tool text contain `unverified` and never `measured`/`VERIFIED` on lineage lines.
  - `test_l3_agent_surfaces`: `guru.lineage`, `handle_tool_call("lineage")`, `cli.main(["lineage", …])`, HTTP `/lineage` via Starlette `TestClient`, and `"lineage"` in the MCP tool list (`importorskip("mcp")`) and in every adapter's tool names.
  - `test_l4_optional_parser`: monkeypatch `lineage_available` → False and `sys.modules["sqlglot"] = None` → dbt table-level lineage, no COLUMN_LINEAGE, exactly one SG212. Also `pyproject.toml` `dependencies == ["factlang>=0.1.4,<0.2", "PyYAML>=6"]` and `optional-dependencies["lineage"] == ["sqlglot>=30,<31"]`.
  - `test_h1_ledger_closed`: every `ITEM_IDS` row's state is `fixed`, `already fixed` or `ruled out` (reuse `tests/conformance/test_hardening_ledger.py::parse_ledger`).
  - `test_s1_results_published`: `benchmarks/results/0.2.0.json` has `size.tables == 500_000` and `size.columns == 25_000_000`. Each `HARD_TARGETS` metric meets its bound or is named in `known_limits`, and that name appears in CHANGELOG `0.2.0`. `.github/workflows/bench.yml` exists.
  - `test_antigoal_no_verified_lineage` (the `Fact.make` VERIFIED lineage raises)
  - `test_antigoal_no_lineage_join_edge` (reuse `lineage_helpers.alpha_beta_guru` → `Refusal`)
  - `test_antigoal_no_raw_definition_anywhere` (reuse `lineage_helpers.privacy_warehouse`)
  - `test_antigoal_hostile_sql_cannot_hang` (the `HANG` hostile worker through `crawl` finishes with SG211 `timeout` within `timeout_s + 10` s)
  - `test_antigoal_guards_only_tightened`: collection and existence checks only (it never runs tests inside a test; the gate runs them): every `fixed` A1/A2 ledger row names a test file that exists and defines that test, and `pytest --collect-only -q tests/unit/test_guard.py tests/unit/catalog/test_executor.py` lists the hostile-form tests.
- [ ] **Step 2: Run them.** `pytest tests/acceptance/test_release_020.py -v`. Expected: FAIL on `test_version_is_020` (and nothing else), then PASS after Step 3. Any other failure is a defect: fix it in the owning area with a red test first.
- [ ] **Step 3: Set the version and date.** Bump `__version__` and `tests/test_version.py` to `0.2.0`; date the CHANGELOG heading.
- [ ] **Step 4: Full verification.** Run the gate; then `pytest -m integration -rs` with the CI containers up; `pytest tests/acceptance -rs` in a venv with `.[dev,mcp,anthropic,langchain,llamaindex,openai-agents]` and `SCHEMAGURU_FORBID_SKIPS=1`; `mkdocs build --strict`; `python3 -m build && twine check dist/*`. All pass. Do not tag and do not publish; the controller releases.
- [ ] **Step 5: Commit.** `git add src/schemaguru/__about__.py tests/test_version.py CHANGELOG.md tests/acceptance/test_release_020.py docs/specs/2026-11-25-schemaguru-0.2.0-hardening-ledger.md` and `git commit -m "chore(release): 0.2.0 acceptance sweep and version"`.
