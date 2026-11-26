"""Compare a scale-benchmark result with the hard targets and with committed baselines."""

from __future__ import annotations

import argparse
import json
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

HARD_TARGETS = {
    "crawl_peak_rss_bytes": 2 * 2**30,
    "incremental_ratio": 0.10,
    "context_p95_ms": 200.0,
    "lineage_upstream_p95_ms": 50.0,
    "lineage_downstream_p95_ms": 50.0,
}
# store_bytes is reported, never gated (spec 6.1)
REGRESSION_GATED = ("crawl_full_s", "path_p95_ms", "graph_build_s", "lineage_parse_s")
TOLERANCE = 0.25


def _same_class(result: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    same_class = (
        b.get("environment", {}).get("runner_class") == result["environment"]["runner_class"]
    )
    return bool(same_class and b.get("size", {}).get("tables") == result["size"]["tables"])


def _matching(result: Mapping[str, Any], baselines: Sequence[Mapping[str, Any]]) -> list[Any]:
    return [b for b in baselines if _same_class(result, b)]


def compare_notes(result: Mapping[str, Any], baselines: Sequence[Mapping[str, Any]]) -> list[str]:
    if _matching(result, baselines):
        return []
    cls, n = result["environment"]["runner_class"], result["size"]["tables"]
    return [f"no baseline for {cls} at {n} tables: regression gate inactive"]


def compare(result: Mapping[str, Any], baselines: Sequence[Mapping[str, Any]]) -> list[str]:
    """The problems: a missed hard target, or a gated metric more than 25% above the best
    matching-baseline median. A metric that is ``None`` (no RSS on Windows) is skipped."""
    problems: list[str] = []
    metrics = result["metrics"]
    for name, bound in HARD_TARGETS.items():
        value = metrics.get(name)
        if value is not None and value > bound:
            problems.append(f"{name} = {value:.4g} exceeds the hard target {bound:.4g}")
    matching = _matching(result, baselines)
    for name in REGRESSION_GATED:
        value = metrics.get(name)
        seen = [b["metrics"][name] for b in matching if b["metrics"].get(name) is not None]
        if value is None or not seen:
            continue
        best = statistics.median(seen)
        if value > best * (1 + TOLERANCE):
            problems.append(
                f"{name} = {value:.4g} regressed more than {TOLERANCE:.0%} "
                f"over the baseline median {best:.4g}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("result", type=Path)
    ap.add_argument("--baseline-dir", type=Path, default=Path("benchmarks/results"))
    a = ap.parse_args(argv)
    result = json.loads(a.result.read_text())
    baselines = [
        json.loads(p.read_text())
        for p in sorted(a.baseline_dir.glob("*.json"))
        if p.resolve() != a.result.resolve()
    ]
    for note in compare_notes(result, baselines):
        print(f"note: {note}")
    problems = compare(result, baselines)
    for p in problems:
        print(f"FAIL: {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
