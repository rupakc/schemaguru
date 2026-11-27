"""The docs workflow checks what the browser runs, not what pip would install.

The playground unpacks the wheels without installing their dependencies (``unpackArchive``) and
loads only ``sqlite3`` and ``pyyaml``; a smoke test that lets pip fetch the PyPI dependencies,
or one that never runs Pyodide, would deploy a playground that cannot start.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _steps(text: str) -> list[dict[str, Any]]:
    steps = yaml.safe_load(text)["jobs"]["build-deploy"]["steps"]
    assert isinstance(steps, list)
    return steps


def _runs(text: str) -> str:
    return "\n".join(str(s.get("run", "")) for s in _steps(text))


def _lacks_browser_smoke(text: str) -> list[str]:
    """What is missing from the docs job, as short reasons (empty: nothing is)."""
    problems = []
    runs = _runs(text)
    smoke = [ln for ln in runs.splitlines() if "/tmp/smoke/bin/pip install" in ln]
    if not smoke or not all("--no-deps" in ln and "PyYAML" in ln for ln in smoke):
        problems.append("the smoke venv must pip install --no-deps the wheels plus PyYAML")
    if "scripts/pyodide_check.mjs" not in runs or "pyodide@0.29.0" not in runs:
        problems.append("no step runs scripts/pyodide_check.mjs on pyodide@0.29.0")
    if not any(str(s.get("uses", "")).startswith("actions/setup-node@") for s in _steps(text)):
        problems.append("no actions/setup-node step")
    if "tests/conformance/test_mkdocs_hooks.py" not in runs or "test_landing.py" not in runs:
        problems.append("the hook and landing conformance tests do not run")
    return problems


def test_docs_job_mirrors_the_browser() -> None:
    text = (ROOT / ".github" / "workflows" / "docs.yml").read_text("utf-8")
    assert _lacks_browser_smoke(text) == []


def test_pyodide_runs_outside_the_repo_before_the_build() -> None:
    steps = _steps((ROOT / ".github" / "workflows" / "docs.yml").read_text("utf-8"))
    names = [str(s.get("run", "")) for s in steps]
    node = next(i for i, r in enumerate(names) if "pyodide_check.mjs" in r)
    build = next(i for i, r in enumerate(names) if "mkdocs build" in r)
    assert node < build
    assert "RUNNER_TEMP" in names[node]  # npm i never touches the checkout


def test_negative_control_pip_resolving_smoke_is_caught() -> None:
    text = (ROOT / ".github" / "workflows" / "docs.yml").read_text("utf-8")
    broken = text.replace("--no-deps build/playground/*.whl PyYAML", "build/playground/*.whl")
    assert "the smoke venv must pip install --no-deps the wheels plus PyYAML" in (
        _lacks_browser_smoke(broken)
    )
    no_node = text.replace("scripts/pyodide_check.mjs", "scripts/other.mjs")
    assert any("pyodide_check" in p for p in _lacks_browser_smoke(no_node))
