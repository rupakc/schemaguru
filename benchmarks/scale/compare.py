"""Compare a scale-benchmark result with the hard targets and with committed baselines.

A hard-target miss whose metric name the result's ``known_limits`` names (a documented limit,
with its measured numbers) is a WARNING, not a failure; that metric is then regression-gated
like the others, so it cannot get worse unnoticed. An unlisted miss fails.

CLI exit codes: 0 pass; 1 problems; 2 no matching baseline (regression gate inactive, a
WARNING on stderr); 3 malformed result."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
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


EXIT_PROBLEMS = 1
EXIT_GATE_INACTIVE = 2  # no problems, but no matching baseline: the regression gate did not run
EXIT_MALFORMED = 3  # the result is not a scale-benchmark result
RSS_METRICS = ("crawl_peak_rss_bytes",)  # None only where there is no `resource` (Windows)


class MalformedResult(ValueError):
    """The result JSON lacks a section this comparator needs."""


def _need(result: Mapping[str, Any], *path: str) -> Any:
    node: Any = result
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            raise MalformedResult(f"result has no {'.'.join(path)!r}")
        node = node[key]
    return node


def _same_class(result: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    cls = _need(result, "environment", "runner_class")
    tables = _need(result, "size", "tables")
    return bool(
        b.get("environment", {}).get("runner_class") == cls
        and b.get("size", {}).get("tables") == tables
    )


def _matching(result: Mapping[str, Any], baselines: Sequence[Mapping[str, Any]]) -> list[Any]:
    _need(result, "environment", "runner_class")
    _need(result, "size", "tables")
    return [b for b in baselines if _same_class(result, b)]


def compare_notes(result: Mapping[str, Any], baselines: Sequence[Mapping[str, Any]]) -> list[str]:
    if _matching(result, baselines):
        return []
    cls, n = result["environment"]["runner_class"], result["size"]["tables"]
    return [f"no baseline for {cls} at {n} tables: regression gate inactive"]


def known_limit_metrics(result: Mapping[str, Any]) -> frozenset[str]:
    """The hard-target metrics a ``known_limits`` entry names (the metric key as a word)."""
    limits = result.get("known_limits")
    texts = [t for t in limits if isinstance(t, str)] if isinstance(limits, list) else []
    return frozenset(
        name for name in HARD_TARGETS if any(re.search(rf"\b{name}\b", t) for t in texts)
    )


def known_misses(result: Mapping[str, Any]) -> list[str]:
    """Warnings: hard targets missed by a metric ``known_limits`` documents."""
    metrics = _need(result, "metrics")
    out = []
    for name in sorted(known_limit_metrics(result)):
        value = metrics.get(name)
        if value is not None and value > HARD_TARGETS[name]:
            out.append(
                f"{name} = {value:.4g} misses the hard target {HARD_TARGETS[name]:.4g} "
                "(a documented known limit: reported, regression-gated, not failed)"
            )
    return out


def compare(result: Mapping[str, Any], baselines: Sequence[Mapping[str, Any]]) -> list[str]:
    """The problems: a missed or absent hard target (a miss ``known_limits`` names is a
    warning instead, see :func:`known_misses`), or a gated metric more than 25% above the
    matching-baseline median. Only a ``None`` peak RSS (no ``resource`` module) is excused."""
    problems: list[str] = []
    metrics = _need(result, "metrics")
    matching = _matching(result, baselines)
    known = known_limit_metrics(result)
    for name, bound in HARD_TARGETS.items():
        if name not in metrics:
            problems.append(f"{name} is missing from the result")
            continue
        value = metrics[name]
        if value is None:
            if name not in RSS_METRICS:
                problems.append(f"{name} is missing from the result (null)")
        elif value > bound and name not in known:
            problems.append(f"{name} = {value:.4g} exceeds the hard target {bound:.4g}")
    for name in (*REGRESSION_GATED, *sorted(known)):
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
    try:
        notes = compare_notes(result, baselines)
        problems = compare(result, baselines)
        known = known_misses(result)
    except MalformedResult as e:
        print(f"ERROR: malformed result: {e}", file=sys.stderr)
        return EXIT_MALFORMED
    for note in [*known, *notes]:
        print(f"WARNING: {note}", file=sys.stderr)
    for p in problems:
        print(f"FAIL: {p}")
    if problems:
        return EXIT_PROBLEMS
    return EXIT_GATE_INACTIVE if notes else 0


if __name__ == "__main__":
    raise SystemExit(main())
