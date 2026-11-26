"""The nightly scale benchmark (0.2.0 spec 6.2): never on push or pull request, inside the
GitHub-hosted job limit, runs the harness and the comparator, keeps the result, and runs at the
nightly size the committed 0.2.0 result records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "bench.yml"
RESULT = ROOT / "benchmarks" / "results" / "0.2.0.json"


def _doc() -> dict[str, Any]:
    doc = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(doc, dict)
    return doc


def _triggers(doc: dict[str, Any]) -> dict[str, Any]:
    on = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` key as True
    assert isinstance(on, dict)
    return on


def _runs(job: dict[str, Any]) -> str:
    return "\n".join(str(s.get("run", "")) for s in job["steps"])


def test_triggers_and_limits() -> None:
    doc = _doc()
    on = _triggers(doc)
    assert set(on) == {"schedule", "workflow_dispatch"}
    assert on["schedule"] == [{"cron": "17 3 * * *"}]
    job = doc["jobs"]["bench"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] <= 350
    runs = _runs(job)
    assert "python3 -m benchmarks.scale.harness" in runs
    assert '--tables "$TABLES"' in runs and "--runner-class github-ubuntu-latest" in runs
    assert (
        "python3 -m benchmarks.scale.compare bench.json --baseline-dir benchmarks/results" in runs
    )
    uploads = [
        s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@v4")
    ]
    assert uploads and uploads[0]["with"]["path"] == "bench.json"
    assert uploads[0].get("if") == "always()"  # a failing run keeps its numbers


def test_an_inactive_gate_warns_but_does_not_fail() -> None:
    """Exit 2 (no baseline for this runner class and size) passes with a visible warning until
    the GitHub baseline is committed; 1 (a missed target or a regression) and 3 still fail."""
    runs = _runs(_doc()["jobs"]["bench"])
    assert '"$code" -eq 2' in runs and "::warning" in runs
    assert 'exit "$code"' in runs


def test_default_size_is_the_recorded_nightly_size() -> None:
    on = _triggers(_doc())
    default = int(on["workflow_dispatch"]["inputs"]["tables"]["default"])
    env = _doc()["jobs"]["bench"]["env"]["TABLES"]
    assert str(default) in env  # the scheduled run (no inputs) uses the same size
    nightly = json.loads(RESULT.read_text())["nightly"]
    assert nightly["tables"] == default and nightly["reason"]
    assert default in (500_000, 250_000, 100_000)
