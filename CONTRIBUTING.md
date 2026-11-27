# Contributing to SchemaGuru

Thanks for helping. Please read `CLAUDE.md`: it holds the project rules for humans
and agents alike.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"   # FactLang (factlang>=0.1.2,<0.2) comes from PyPI
```

## Checks (all blocking in CI)

```bash
pytest -m "not integration and not live and not benchmark" --cov
ruff check
ruff format --check
mypy --strict src
```

## Rules in short

- Runtime dependencies are `factlang` and `PyYAML` only; everything else is an extra,
  imported lazily inside functions.
- A module imports only from its own package or strictly lower layers
  (`tests/architecture/test_layers.py`).
- Only the three FactLang seam modules import `factlang`.
- SchemaGuru never writes measurement SQL; only catalog-metadata SQL with bind parameters.
- Unknown is `None`, never 0, 1.0 or a default.
- Every invariant ships with a negative control: a test that fails when the invariant is broken.
- Conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`), and every
  commit leaves the test suite green.

## Docs site

The site (mkdocs-material, `mkdocs.yml`) embeds an in-browser playground: the real library under
Pyodide, running from two wheels and a prebuilt store. `.github/workflows/docs.yml` builds all of
it from the commit; these are the same commands, run from the repository root in a virtualenv
(`build/` is gitignored and never committed).

```bash
pip install build "mkdocs-material" "mkdocstrings[python]"

# 1. the playground build directory: the schemaguru wheel, the pinned factlang wheel, the store
python -m build --wheel --outdir build/playground
pip install "$(ls build/playground/schemaguru-*.whl)[duckdb,lineage]"
pip download "factlang==$(python -c 'import importlib.metadata as m; print(m.version("factlang"))')" \
  --no-deps --only-binary=:all: -d build/playground
python scripts/build_playground_store.py --out build/playground

# 2. regenerate the generated pages and snippets (docs/generated/, reference/cli.md, ...);
#    commit the diff. CI fails when they are stale.
python3 scripts/gen_docs.py

# 3. build the site; warnings are errors, a missing playground piece is a warning
mkdocs build --strict

# 4. run the playground glue under Pyodide, as the browser does (needs node and a pyodide package
#    outside the repository)
mkdir -p /tmp/pyodide && (cd /tmp/pyodide && npm init -y && npm i pyodide@0.29.0)
node scripts/pyodide_check.mjs /tmp/pyodide/node_modules/pyodide build/playground

# 5. the hook and landing conformance tests (they need mkdocs, so CI runs them in docs.yml)
pytest tests/conformance/test_mkdocs_hooks.py tests/conformance/test_landing.py -q
```

The browser unpacks the wheels without installing their dependencies and loads only `sqlite3`
and `pyyaml`, so anything else the glue imports must come from the wheels. `docs.yml` smoke-tests
that with `pip install --no-deps build/playground/*.whl PyYAML` in a clean venv.

The browser tests in `tests/site` drive the built `site/` in Chromium. They are local-only (the CI
integration job does not run them): after `mkdocs build --strict`,

```bash
pip install playwright && playwright install chromium
pytest tests/site -m integration -q
```

The "which readers are tested live" statement is generated (`docs/generated/readers_tested.md`,
from the integration job's engine list in `ci.yml`); the README and the wiki carry a verbatim
copy between `readers-tested` markers, which `tests/conformance/test_docs_facts.py` checks.

## Pull requests

Keep them focused, describe the behaviour change, and add tests. Update
`CHANGELOG.md` for user-visible changes.
