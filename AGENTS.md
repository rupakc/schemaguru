# SchemaGuru - guidance for coding agents

Applies to any agent working in this repository (Claude Code, Codex, Copilot, others). The
authoritative rules are the same as in `CLAUDE.md`; this file repeats them for tools that read
`AGENTS.md`.

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

- Package `schemaguru`, `src/` layout, version in `src/schemaguru/__about__.py`.
- Python `>=3.10`; CI matrix 3.10, 3.11, 3.12, 3.13 × ubuntu, macos, windows.
- Runtime dependencies exactly: `factlang>=0.1.2,<0.2`, `PyYAML>=6`. Everything else is an extra imported lazily inside functions. FactLang is installed from PyPI everywhere (CI, docs, dev).
- Docs site and playground: build, generate and test them as `CONTRIBUTING.md` ("Docs site") says; `docs.yml` runs the same commands.
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

## Roles

Role definitions with tools and hard rules live in `.claude/agents/`.

### goldfish-implementer
Receives one plan task and implements it test-first (watch the test fail, then pass), obeying the layer rules, the seam rule and the checklist. It commits with explicit paths, runs the full gate, pushes and watches CI. It does not dispatch other agents.

### goldfish-spec-reviewer
A fresh reader. Given the spec it lists everything it could not build; given a diff plus the spec it lists deviations. Every gap is a numbered item quoting the section. It does not fix anything.

### warehouse-reader-engineer
Writes one reader module per dialect: system-view SQL only, bind parameters for every value, run through the metadata executor. Adds a recorded metadata fixture and a conformance-test parameter, registers it, and writes the Warehouse Guide page.

### graph-algorithms-reviewer
Checks that forward match and reverse coverage stay distinct, thin joins are labelled, broken joins refused, and the hub guard refuses a shape rather than a node. Looks for order-dependence and non-determinism.

### pii-privacy-reviewer
Checks that LLM sample values are masked by default, raw only on explicit opt-in, `none` available, masking is idempotent, and that no role is inferred from a name alone. Read-only review with a verdict.

### docs-writer
Each wiki page opens with a three-line summary and ends with see-also links. Examples use only the generic shop domain. Never mentions any originating company, product or dataset.

### release-engineer
Runs the full gate, checks the version in `__about__.py` and the changelog, builds with `python -m build`, runs `twine check`, and hands the owner the exact tag. Pushing a `v<version>` tag runs `.github/workflows/release.yml`: it checks the tag equals `__version__`, builds and `twine check`s, publishes to PyPI by Trusted Publishing (environment `pypi`, no token) and creates the GitHub Release with the built files and generated notes. Never create or push a tag without the owner.
