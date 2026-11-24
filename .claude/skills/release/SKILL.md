---
name: release
description: Cut a SchemaGuru release.
---

# release

1. Confirm CI is green on `main` and FactLang 0.1.x is on PyPI (the git install is for development only).
2. Set the version in `src/schemaguru/__about__.py`; move the changelog `Unreleased` section under the new version.
3. Run `python -m build` and `twine check dist/*`; inspect wheel and sdist contents.
4. Open a release commit `chore: release vX.Y.Z`; push to `main`; wait for CI.
5. Hand the owner the tag command and the Trusted Publishing steps (environment `pypi`); do not tag or publish yourself.
6. After publishing, verify `pip install schemaguru==X.Y.Z` in a clean venv.
