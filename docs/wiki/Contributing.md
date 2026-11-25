# Contributing

> Contributions are welcome: bug reports, warehouse readers, detectors, docs and fixes.
> Set up a virtual environment, install FactLang from git and SchemaGuru in editable mode, and run the four blocking checks (pytest, ruff check, ruff format --check, mypy --strict).
> A few project rules keep the library honest; they are listed here with the test that enforces each.

## Setup

```bash
git clone https://github.com/rupakc/schemaguru && cd schemaguru
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"   # FactLang (factlang>=0.1.1,<0.2) comes from PyPI
```

## Checks (all blocking in CI)

```bash
pytest -m "not integration and not live and not benchmark" --cov
ruff check
ruff format --check
mypy --strict src
```

Test markers:

| Marker | Needs | Runs |
|---|---|---|
| (none) | nothing | every push, on Python 3.10-3.13 × Linux/macOS/Windows |
| `integration` | service containers (PostgreSQL, MySQL, ClickHouse, SQL Server, Trino) or a local engine; also the Postgres store | CI integration job; locally they skip without a DSN |
| `live` | real cloud warehouse credentials | opt-in only (`tests/live/`) |
| `factlang` | the real FactLang (installed from PyPI) | every push; FactLang 0.1.1 provides every capability, so none skip |
| `benchmark` | time | nightly, not per pull request |

The test layout: `tests/unit`, `tests/property` (Hypothesis), `tests/integration`,
`tests/golden` (committed rendered views of the shop fixture; diffs are reviewed),
`tests/conformance` (reader conformance and this wiki's link check), `tests/architecture`
(layers, FactLang seam, denylist), `tests/benchmark`.

## The rules, and what enforces them

| Rule | Enforced by |
|---|---|
| A module imports only from its own package or strictly lower layers ([[Architecture]]) | `tests/architecture/test_layers.py` |
| Only `measure/flbridge.py`, `catalog/executor.py` and `render/fragments.py` import `factlang` | `test_layers.py::test_factlang_seam` |
| SchemaGuru never writes measurement SQL; the only SQL is catalog metadata SQL with bind parameters | review; the metadata guard (`check_metadata_sql`) |
| Runtime dependencies are `factlang` and `PyYAML` only; everything else is an extra imported lazily inside functions | review, `pyproject.toml` |
| Unknown is `None`, never 0, 1.0 or a default; timestamps are timezone-aware UTC | model validation, property tests |
| Diagnostic codes live in `diagnostics.CODES` and `docs/reference/diagnostics.md` | a test asserts both agree with the source |
| No proprietary names anywhere | `tests/architecture/test_denylist.py` (CI secret) |
| Wiki links resolve; every wiki page has a 3-line summary and a "See also" | `tests/conformance/test_wiki_links.py` |
| Every invariant ships with a negative control: a test that fails when the invariant is broken | review |

The full self-review checklist is the twenty rules on [[Design-Decisions]].

## Examples and fixtures

Use the generic shop domain only: `customers, orders, order_items, products, payments, events,
accounts, users, sessions`. The shop fixture (`tests/fixtures/shop/`) has known traps and exact
answers (`EXPECTED.md`); a new trap should come with its expected answer.

## Commits and pull requests

- Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`); every commit leaves the suite
  green.
- Keep pull requests focused, describe the behaviour change, add tests, and update
  `CHANGELOG.md` for user-visible changes.
- Stage explicit paths (never `git add -A`).

## Editing this wiki

The wiki is generated from `docs/wiki/` in the repository: edit the Markdown there and open a
pull request. On every push to `main` that touches `docs/wiki/`, the `wiki` workflow
(`.github/workflows/wiki.yml`) copies the folder to the GitHub wiki, so edits made directly in the
wiki web editor are overwritten. Rules for pages:

- start with `# Title`, then a three-line `>` summary;
- end with `## See also` linking related pages;
- link pages as `[[Page-Name]]`, `[[text|Page-Name]]` or `[text](Page-Name)`; link repository files
  by absolute GitHub URL;
- when you paste output, generate it by running the library and say so.

## Extension points

Each has a page: [[Extending-CatalogReader]], [[Extending-Store]],
[[Extending-Retriever-and-Embedder]], [[Extending-TokenCounter]], [[Extending-LLMProvider]],
[[Extending-Detector]], [[Extending-FailureClassifier]], [[Extending-FactKind]],
[[Extending-Profile-and-Format]], [[Extending-ChangeClassifier]],
[[Extending-JoinCandidateSource]], [[Extending-Authenticator]].

## See also

- [[Architecture]]
- [[Design-Decisions]]
- [`CONTRIBUTING.md`](https://github.com/rupakc/schemaguru/blob/main/CONTRIBUTING.md) in the repository
- [[Home]]
