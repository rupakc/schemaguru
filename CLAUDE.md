# SchemaGuru - project rules

SchemaGuru is an open-source (MIT) Python library for automated schema intelligence on data warehouses.
It discovers the catalog, measures facts (join match both ways, fan-out, uniqueness, real enum values,
freshness) as FactLang claims, derives a join graph with hub/cartesian guards, and drafts meaning with
an LLM that human YAML overlays always override. It serves a question-scoped, token-budgeted view to any
agent (prompt text, tools, MCP, adapters) and refreshes it as the warehouse drifts.
Design: `docs/specs/2026-11-24-schemaguru-design.md`. Wrong guesses return plausible wrong numbers; every rule here protects against that.

## Commands

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"   # FactLang (factlang>=0.1.2,<0.2) comes from PyPI
pytest -m "not integration and not live and not benchmark" --cov
ruff check
ruff format --check
mypy --strict src
```

## Global constraints

- Package `schemaguru`, `src/` layout, version `0.1.0` in `src/schemaguru/__about__.py`.
- Python `>=3.10`; CI matrix 3.10, 3.11, 3.12, 3.13 × ubuntu, macos, windows.
- Runtime dependencies exactly: `factlang>=0.1.2,<0.2`, `PyYAML>=6`. Everything else is an extra imported lazily inside functions. FactLang comes from PyPI via the pyproject pin `factlang>=0.1.2,<0.2`.
- **FactLang seam:** only `src/schemaguru/measure/flbridge.py`, `src/schemaguru/catalog/executor.py` and `src/schemaguru/render/fragments.py` import `factlang`; everything else uses SchemaGuru types. Enforced by `tests/architecture/test_layers.py::test_factlang_seam`.
- **SchemaGuru never writes measurement SQL** (spec §5.2). The only SQL in `src/` is catalog-metadata SQL in `catalog/readers/*.py`, with bind parameters for every value.
- Layer rule spec §5.1; `mypy --strict src`, `ruff check`, `ruff format --check` blocking.
- Unknown is `None`, never 0/1.0/default (spec §25.1). Timestamps tz-aware UTC; naive rejected.
- Diagnostic codes `SGnnn` in the ranges of spec §20.
- Generic shop domain only: `customers, orders, order_items, products, payments, events, accounts, users, sessions`.
- No mention anywhere of the originating company, product, datasets or table names (denylist test, spec §22).
- MIT, `Copyright (c) 2026 Rupak Chakraborty`. Git author `Rupak Chakraborty <rupakc@users.noreply.github.com>` (repo-local config, already set).
- Conventional commits; each commit leaves pytest green; each message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Stage explicit paths only (never `git add -A`). Use `python3` and a venv at `~/schemaguru/.venv` (gitignored).

## Layers (spec 5.1)

| Layer | Packages / modules | Responsibility |
|---|---|---|
| 0 | `__about__`, `errors`, `diagnostics`, `_util/` | version, exception base, `Diagnostic`, stdlib helpers |
| 1 | `model/` | `ObjectRef`, `Fact`, `FactKind`, `Status`, `JoinRef`, `Chain`, `Evidence` |
| 2 | `store/`, `config.py`, `lineage/` | `Store` protocol, SQLite / Postgres stores, migrations; `GuruConfig`; SQL lineage parsing (optional `sqlglot`, worker process; no store or catalog imports) |
| 3 | `catalog/`, `pii/`, `overlays/` | catalog readers + crawl; shape detectors, masking, policy; human YAML overlays |
| 4 | `measure/`, `knowledge/` | probe planner, budget, scheduler (FactLang seam); resolve, snapshot, diff |
| 5 | `graph/`, `draft/` | edges, cost, path search, hub guard; LLM drafting pipeline + providers |
| 6 | `scope/`, `render/`, `refresh/`, `feedback/` | retrieval + budget; views + fingerprints; drift; error intake |
| 7 | `guru.py`, `agent/`, `cli/` | `Guru` facade; tool specs, MCP, adapters; `schemaguru` command |
| 8 | `server/` | optional HTTP service (extra `server`, Option C) |

A module imports only from its own package or strictly lower layers (module-level imports;
lazy imports inside functions are the escape hatch for optional extras only).
`schemaguru/__init__.py` re-exports the public API (spec 19) and is exempt.

## FactLang seam rule

Only `src/schemaguru/measure/flbridge.py`, `src/schemaguru/catalog/executor.py` and
`src/schemaguru/render/fragments.py` import `factlang` (at any level); everything else uses
SchemaGuru types and the protocols `MeasureBackend`, `MetadataExecutor`, `FragmentRenderer`.
Tests use fakes of those protocols; seam-module tests marked `@pytest.mark.factlang` call the
real FactLang and skip with "factlang capability <name> not yet available" until it ships.
SchemaGuru never writes measurement SQL (spec 5.2): if a primitive is missing, register it
through FactLang's public `fl.ir.register_primitive`. Enforced by
`tests/architecture/test_layers.py::test_factlang_seam`.

## Self-review checklist (spec 25)

Each item is a rule a reviewer checks on every change (skill `self-review`).

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
13. Consequence-ordered rendering; legend states silence != verification.
14. One bad item costs one item; loaders never raise on data.
15. Soft-delete flags may be TRUE/NULL (never FALSE); a load-bearing column is never
    dropped by a name pattern.
16. Composite keys are declared and grouped in full.
17. A narrower run never overwrites a fuller snapshot; missing checks are reported.
18. Waivers expire.
19. Timestamps are tz-aware UTC; naive is rejected.
20. No proprietary names anywhere.

## Process: Elephant-Goldfish (spec 24)

The **Elephant** (long-running session) owns the spec (`docs/specs/2026-11-24-schemaguru-design.md`),
the plan (`docs/specs/2026-11-24-schemaguru-plan.md`) and `MEMORY.md`, and writes no library code.

- Each plan task goes to a fresh **goldfish** implementer that receives only the spec, the
  task and the repo; a separate fresh reviewer checks the diff against the spec. Independent
  tasks fan out in parallel; same-layer tasks run sequentially.
- A goldfish needing information that is not in the spec = a spec defect: it stops and
  reports the gap; the Elephant amends the spec and records a `MEMORY.md` entry.
- Before approval a fresh goldfish reads the spec cold and lists what it could not build.
- Every commit is green and is pushed incrementally to `main`.
- To cut a task for a goldfish use the `egm-handoff` skill.

## How to extend

- New warehouse reader: skill `add-warehouse-reader`. New fact kind: `add-fact-kind`. New PII detector: `add-detector`.
- Fixture warehouse: `run-fixture-warehouse`. Releases: `release`. Self-review: `self-review`.
- Roles live in `.claude/agents/`; durable decisions in `MEMORY.md`.
