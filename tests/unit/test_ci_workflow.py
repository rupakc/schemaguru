"""Every CI job that runs pytest checks out the full history.

``tests/conformance/test_hardening_ledger.py`` asks git whether each ``already fixed`` commit
exists; ``actions/checkout`` defaults to a depth-1 clone, where it does not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _jobs(text: str) -> dict[str, Any]:
    jobs = yaml.safe_load(text)["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _unfetched_pytest_jobs(text: str) -> list[str]:
    bad = []
    for name, job in _jobs(text).items():
        steps = job.get("steps", [])
        if not any("pytest" in str(s.get("run", "")) for s in steps):
            continue
        checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
        if not checkouts or any(c.get("with", {}).get("fetch-depth") != 0 for c in checkouts):
            bad.append(name)
    return bad


def test_every_pytest_job_checks_out_full_history() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert _unfetched_pytest_jobs(text) == []


def test_the_check_sees_a_shallow_pytest_job() -> None:
    shallow = "jobs:\n  t:\n    steps:\n      - uses: actions/checkout@v4\n      - run: pytest\n"
    deep = shallow.replace("v4\n", "v4\n        with:\n          fetch-depth: 0\n")
    no_pytest = "jobs:\n  l:\n    steps:\n      - uses: actions/checkout@v4\n      - run: ruff\n"
    assert _unfetched_pytest_jobs(shallow) == ["t"]
    assert _unfetched_pytest_jobs(deep) == []
    assert _unfetched_pytest_jobs(no_pytest) == []


def _denylist_job(text: str) -> dict[str, Any]:
    job = _jobs(text)["denylist"]
    assert isinstance(job, dict)
    return job


def _fails_on_main_without_secret(text: str) -> bool:
    """A step that runs only on a push to main and exits 1 when the secret env is empty."""
    job = _denylist_job(text)
    for step in job.get("steps", []):
        cond, run = str(step.get("if", "")), str(step.get("run", ""))
        if (
            "refs/heads/main" in cond
            and "push" in cond
            and "SCHEMAGURU_DENYLIST" in run
            and "-z" in run
            and "exit 1" in run
        ):
            return True
    return False


def test_denylist_fails_on_main_without_secret() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert _fails_on_main_without_secret(text)
    job = _denylist_job(text)
    # forks and pull requests from forks skip the job instead of failing it
    assert "head.repo.full_name == github.repository" in str(job.get("if", ""))
    assert job["env"]["SCHEMAGURU_DENYLIST"] == "${{ secrets.SCHEMAGURU_DENYLIST }}"


def test_the_denylist_check_sees_a_job_that_passes_silently() -> None:
    silent = (
        "jobs:\n  denylist:\n    steps:\n      - run: pytest tests/architecture/test_denylist.py\n"
    )
    assert not _fails_on_main_without_secret(silent)
    loud = (
        "jobs:\n  denylist:\n    steps:\n      - if: github.event_name == 'push' && "
        "github.ref == 'refs/heads/main'\n        run: |\n"
        '          if [ -z "$SCHEMAGURU_DENYLIST" ]; then exit 1; fi\n'
    )
    assert _fails_on_main_without_secret(loud)
