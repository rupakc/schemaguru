"""The release workflow must read the version without importing the package.

The v0.1.0 release failed because the tag check imported ``schemaguru``,
whose ``__init__`` needs dependencies the job does not install.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text()


def _version_command() -> str:
    match = re.search(r'version=\$\(python -c "(.+)"\)', WORKFLOW)
    assert match, 'tag-check step must assign version=$(python -c "...")'
    return match.group(1)


def test_version_command_does_not_import_the_package() -> None:
    cmd = _version_command()
    assert "runpy" in cmd
    assert "__about__.py" in cmd
    assert not re.search(r"(from|import)\s+schemaguru", cmd)
    assert "PYTHONPATH" not in WORKFLOW


def test_version_command_runs_without_dependencies() -> None:
    # -I: isolated mode, no site env vars; cwd = repo root as in the job.
    # Windows cannot start a process without SYSTEMROOT (and COMSPEC).
    env: dict[str, str] = {}
    if os.name == "nt":
        for key in ("SYSTEMROOT", "COMSPEC"):
            if key in os.environ:
                env[key] = os.environ[key]
    result = subprocess.run(
        [sys.executable, "-I", "-c", _version_command()],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    from schemaguru.__about__ import __version__

    assert result.stdout.strip() == __version__


def test_build_step_installs_only_build_and_twine() -> None:
    installs = re.findall(r"pip install (.+)", WORKFLOW)
    assert installs == ["build twine"]
