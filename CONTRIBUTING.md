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

## Pull requests

Keep them focused, describe the behaviour change, and add tests. Update
`CHANGELOG.md` for user-visible changes.
