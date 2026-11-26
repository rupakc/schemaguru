"""The Scale pages (docs site and wiki) publish exactly the committed 0.2.0 result (spec 6.2).

Every metric of ``benchmarks/results/0.2.0.json`` has a row in each page's results table, shown
with the formatting below, and a hard-target row says ``pass`` or ``miss`` as the comparator
would. The pages name the machine, the nightly size and the reproduce command.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from benchmarks.scale.compare import HARD_TARGETS

ROOT = Path(__file__).resolve().parents[2]
RESULT = json.loads((ROOT / "benchmarks" / "results" / "0.2.0.json").read_text())
PAGES = (ROOT / "docs" / "guide" / "scale.md", ROOT / "docs" / "wiki" / "Scale.md")
REPRODUCE = "python3 -m benchmarks.scale.harness --tables 500000"
_ROW = re.compile(r"^\|\s*`(?P<name>[a-z0-9_]+)`\s*\|(?P<rest>.*)\|\s*$")


def fmt(name: str, value: Any) -> str:
    """A metric as the pages show it."""
    if value is None:
        return "n/a"
    if name.endswith("_bytes"):
        return f"{value / 2**30:.2f} GiB" if value >= 2**30 else f"{value / 2**20:.0f} MiB"
    if name.endswith("_ms"):
        return f"{value:,.1f} ms"
    if name.endswith("_s"):
        return f"{value:,.1f} s"
    return f"{value:.3f}"


def _rows(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        m = _ROW.match(line.strip())
        if m:
            out[m["name"]] = [c.strip() for c in m["rest"].split("|")]
    return out


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_page_quotes_committed_results(page: Path) -> None:
    text = page.read_text()
    rows = _rows(text)
    metrics = RESULT["metrics"]
    assert set(rows) == set(metrics), set(rows) ^ set(metrics)
    for name, value in metrics.items():
        cells = rows[name]
        assert cells[1] == fmt(name, value), (name, cells)
        if name in HARD_TARGETS:
            verdict = "pass" if value is None or value <= HARD_TARGETS[name] else "miss"
            assert verdict in cells[2].lower(), (name, cells)
    env = RESULT["environment"]
    assert env["runner_class"] in text and env["cpu"] in text
    assert f"{env['cores']} cores" in text and f"{env['ram_bytes'] / 2**30:.0f} GiB" in text
    assert REPRODUCE in text
    assert "python3 -m benchmarks.scale.compare" in text
    nightly = RESULT["nightly"]
    assert f"{nightly['tables']:,}" in text
    size = RESULT["size"]
    assert f"{size['tables']:,}" in text and f"{size['columns']:,}" in text


def test_negative_control_a_changed_number_fails(tmp_path: Path) -> None:
    """The row check bites: a page quoting a different number does not match."""
    name, value = "context_p95_ms", RESULT["metrics"]["context_p95_ms"]
    good = f"| `{name}` | what | {fmt(name, value)} | target |"
    bad = f"| `{name}` | what | {fmt(name, value + 1.0)} | target |"
    assert _rows(good)[name][1] == fmt(name, value)
    assert _rows(bad)[name][1] != fmt(name, value)
