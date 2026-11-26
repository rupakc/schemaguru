from __future__ import annotations

from typing import Any

from benchmarks.scale.compare import HARD_TARGETS, REGRESSION_GATED, compare, compare_notes


def result(runner: str = "ci", tables: int = 1000, **metrics: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "crawl_full_s": 100.0, "path_p95_ms": 10.0, "graph_build_s": 5.0, "lineage_parse_s": 20.0,
        "crawl_peak_rss_bytes": 10**9, "incremental_ratio": 0.05, "context_p95_ms": 100.0,
        "lineage_upstream_p95_ms": 10.0, "lineage_downstream_p95_ms": 10.0, "store_bytes": 1,
    }  # fmt: skip
    base.update(metrics)
    return {
        "environment": {"runner_class": runner},
        "size": {"tables": tables},
        "metrics": base,
    }


def test_hard_target_miss() -> None:
    problems = compare(result(context_p95_ms=250.0), [])
    assert len(problems) == 1 and "context_p95_ms" in problems[0]
    assert compare(result(crawl_peak_rss_bytes=HARD_TARGETS["crawl_peak_rss_bytes"] + 1), [])
    assert compare(result(crawl_peak_rss_bytes=None), []) == []  # Windows: skipped


def test_regression_over_25_percent() -> None:
    problems = compare(result(crawl_full_s=126.0), [result()])
    assert len(problems) == 1 and "crawl_full_s" in problems[0]


def test_exactly_25_percent_passes() -> None:
    assert compare(result(crawl_full_s=125.0), [result()]) == []


def test_other_runner_class_ignored() -> None:
    assert compare(result(crawl_full_s=500.0), [result("laptop"), result(tables=5)]) == []


def test_no_baseline_note() -> None:
    notes = compare_notes(result(), [result("laptop")])
    assert notes == ["no baseline for ci at 1000 tables: regression gate inactive"]
    assert compare_notes(result(), [result()]) == []


def test_store_bytes_never_gated() -> None:
    assert "store_bytes" not in REGRESSION_GATED
    assert compare(result(store_bytes=10**12), [result()]) == []
