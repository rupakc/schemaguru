# MEMORY - durable decision log

Each entry has the form:

```
## YYYY-MM-DD - decision
Why: the reason the decision was made.
How to apply: what to do (or not do) because of it.
```

Newest entries go at the bottom. The Elephant owns this file; amend the spec first, then
record the change here.

## 2026-11-24 - Approach A (library + local store + human YAML overlays), with C (server) optional
Why: a library with a local store is the smallest thing that serves an agent well and is easy
to adopt; human-edited YAML overlays give reviewable, diffable overrides. A server is useful for
teams but must not be required.
How to apply: the core is a Python library with a SQLite default store. Server mode (layer 8,
extra `server`, Postgres store) exposes the same API over HTTP and never adds behaviour the
library lacks.

## 2026-11-24 - Depend on FactLang, including its spec Amendments A (section 18) and B (section 19)
Why: SchemaGuru must never write measurement SQL. FactLang owns dialects, connectors, claims and
verdicts. Amendment A adds the primitives SchemaGuru needs (raw measurement, top-k values,
composite join keys, cost estimate, filter/join rendering, guarded metadata SQL). Amendment B adds
sampled measurement for very large tables.
How to apply: depend on `factlang>=0.1,<0.2`; CI installs it from git until 0.1.0 is on PyPI.
If a primitive is missing, register it through FactLang's public API, never emit SQL strings.

## 2026-11-24 - The LLM drafts meaning; a human always wins
Why: LLM descriptions are useful drafts but can be wrong; measured facts and human overlays are
the source of truth.
How to apply: precedence is overlay over measured over drafted. Measured types are stated next
to human notes, never by deleting them. Expectations are alarms and are never auto-edited.

## 2026-11-24 - LLM sample values are masked by default
Why: sending real cell values to a third-party model is a privacy decision the owner must opt
into.
How to apply: the default sample mode masks values; raw values need explicit opt-in; `none`
sends no values at all. PII shape detectors drive masking; idempotence is property-tested.

## 2026-11-24 - Large tables are measured on a representative sample; small tables exactly
Why: exact probes on terabyte tables are too slow and costly, while a `LIMIT` "sample" once
overstated a rate more than threefold.
How to apply: small tables exact; large tables key-hash or block sampled through FactLang
Amendment B, never `LIMIT`. Sampled numbers are `ESTIMATED`, labelled, never compared with
exact ones and never `VERIFIED`; a sample may refute but not confirm.

## 2026-11-24 - Standalone library, no reference to its origin
Why: SchemaGuru generalises lessons from an internal predecessor schema layer, but it must carry
no private code, names, datasets or table names.
How to apply: use only the generic shop domain (customers, orders, order_items, products,
payments, events, accounts, users, sessions). The `denylist` CI job and
`tests/architecture/test_denylist.py` enforce this from the secret `SCHEMAGURU_DENYLIST`.

## 2026-11-24 - FactLang seam rule
Why: FactLang is being built concurrently; isolating it behind three modules lets everything
else be tested with fakes and keeps the surface that must track FactLang small.
How to apply: only `measure/flbridge.py`, `catalog/executor.py` and `render/fragments.py` import
`factlang`; the rest uses the protocols `MeasureBackend`, `MetadataExecutor`,
`FragmentRenderer`. Seam integration tests carry `@pytest.mark.factlang` and skip until the
capability exists. `test_factlang_seam` enforces it.

## 2026-11-24 - Coverage omits the three FactLang seam modules until Task 25
Why: they cannot execute before FactLang ships the capabilities they call, so counting them
would force fake coverage or a lowered gate.
How to apply: `pyproject.toml` omits them (marked TODO); the 90% gate otherwise applies.
Task 25 removes the omission together with the remaining factlang skips.

## 2026-11-24 - Fact identity excludes everything measured
Why: re-measuring a join, rate or count must update the same fact (same id), not mint a new one.
How to apply: `KindSpec.identity_fields` is `None` (whole value; meaning/human kinds) or a tuple of
payload keys (possibly empty). Built-ins: JOIN, NULL_RATE, ROW_COUNT, DISTINCT `()`; FRESHNESS `("column",)` (one fact per column);
VALUES `("case_sensitive","split")`; TYPE_NOTE `("stored_as","means")`; PII `("category",)`;
PRIMARY_KEY `("columns",)`; SOFT_DELETE `("predicate",)`. JOIN ids also include `cast_to` (`JoinRef.identity_key()`). The id is `stable_hash([kind, subject,
provenance, identity])` and is always computed, never supplied.

## 2026-11-24 - MEASURED fact ids carry the method class (exact | sampled)
Why: a newer sampled measurement of one subject overwrote the older exact one (same id), and a
sample may refute but never replace an exact number (spec 8.5, 8.6).
How to apply: `Fact.make` appends `method_class(method)` (`"sampled"` iff the method starts with
`sampled:`, else `"exact"`) to the id input for MEASURED facts only. A missing identity field
now raises `ValueError` (present-but-`None` is fine): write `"split": None`, `"means": None`.

## 2026-11-24 - Measurement: plan, price, sample, admit, measure, convert (Task 6)
Why: spec 8; SchemaGuru never writes measurement SQL.
How to apply: `measure.scheduler.Scheduler` per table: `plan_table` -> `backend.estimate` (catalog
bytes as `heuristic` fallback) -> `choose_sample` -> `BudgetGuard.admit` -> one `measure` batch ->
`to_facts` -> `put_facts`; enum discovery (`TopValues`) is a second phase gated on the measured
distinct <= `enum_max_distinct`. A large table's row count is a CATALOG fact from stats, never
MEASURED. A failed/over-budget *heuristic or LLM* join candidate leaves no JOIN fact (diagnostic
only); a declared one gets an ERROR fact. Unit tests use `tests/unit/measure/fake_backend.py`
(DuckDB SQL in tests only; `system` rendered as seeded bernoulli). Codes SG301-SG305.
