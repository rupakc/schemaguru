from __future__ import annotations

import hashlib
import json
import sys
import tracemalloc
from dataclasses import replace
from pathlib import Path

import pytest
from benchmarks.scale.generator import (
    ScaleShape,
    SyntheticReader,
    changed_set,
    definition,
    is_view,
    synthetic_join_facts,
    table_ref,
    upstream_of,
    view_level,
)
from benchmarks.scale.harness import run
from benchmarks.scale.questions import QUESTIONS, lineage_refs, path_pairs

from schemaguru.model.structure import Table


def listing(reader: SyntheticReader) -> list[Table]:
    out: list[Table] = []
    for schema in reader.list_schemas(None, page_token=None).items:
        token = None
        while True:
            page = reader.list_tables(schema, page_token=token, changed_since=None)
            out += page.items
            token = page.next_token
            if token is None:
                break
    return out


def digest(reader: SyntheticReader) -> str:
    h = hashlib.sha256()
    for t in listing(reader):
        h.update(f"{t.ref.canonical()}|{t.kind}|{t.comment}|{t.row_count}".encode())
        for c in reader.columns([t.ref])[t.ref]:
            h.update(f"{c.ref.canonical()}|{c.native_type}".encode())
    return h.hexdigest()


def test_deterministic() -> None:
    s = ScaleShape(tables=2_000, columns_per_table=6)
    assert digest(SyntheticReader(s)) == digest(SyntheticReader(s))
    assert digest(SyntheticReader(s)) != digest(SyntheticReader(replace(s, seed=1)))


def test_shape_counts() -> None:
    s = ScaleShape(tables=2_000)
    tables = listing(SyntheticReader(s))
    assert len(tables) == 2_000
    assert len(SyntheticReader(s).columns([tables[0].ref])[tables[0].ref]) == 50
    assert 2_000 * s.columns_per_table == 100_000
    views = [t for t in tables if t.kind == "view"]
    assert abs(len(views) - 200) <= 1
    assert all(is_view(i, s) == (tables[i].kind == "view") for i in range(len(tables)))


def test_lineage_acyclic_depth_bounded() -> None:
    s = ScaleShape(tables=3_000)

    def depth(i: int) -> int:
        ups = upstream_of(i, s)
        assert all(u < i for u in ups)  # strictly lower: no cycle is possible
        return 0 if not ups else 1 + max(depth(u) for u in ups)

    depths = [depth(i) for i in range(s.tables) if is_view(i, s)]
    assert max(depths) == s.max_lineage_depth
    assert not any(upstream_of(i, s) for i in range(s.tables) if not is_view(i, s))
    refs = lineage_refs(s, 50)
    assert refs and all(view_level(int(r.table.rsplit("_", 1)[1]), s) >= 2 for r in refs)
    assert "CREATE VIEW" in definition(next(i for i in range(s.tables) if is_view(i, s)), s)


def test_pages_bounded_memory() -> None:
    s = ScaleShape(tables=50_000)
    reader = SyntheticReader(s)
    tracemalloc.start()
    try:
        n = 0
        for schema in reader.list_schemas(None, page_token=None).items:
            token = None
            while True:
                page = reader.list_tables(schema, page_token=token, changed_since=None)
                n += len(page.items)
                token = page.next_token
                if token is None:
                    break
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert n == 50_000
    assert peak < 16 * 2**20


def test_changed_set_size() -> None:
    s = ScaleShape(tables=2_000)
    c = changed_set(s)
    assert len(c) == 20 and all(0 <= i < 2_000 for i in c)
    assert c == changed_set(s) and c != changed_set(s, seed=2)


def test_questions_and_pairs() -> None:
    s = ScaleShape(tables=2_000)
    assert len(QUESTIONS) == 200 and len(set(QUESTIONS)) == 200
    assert len(path_pairs(s)) == 200
    facts = list(synthetic_join_facts(s))
    assert facts and all(f.source == "benchmark:synthetic" and f.method == "exact" for f in facts)
    assert table_ref(0, s).schema


@pytest.mark.skipif(sys.platform == "win32", reason="peak RSS needs the resource module")
def test_harness_small_run(tmp_path: Path) -> None:
    result = run(ScaleShape(tables=2_000, columns_per_table=8), tmp_path, runner_class="test")
    assert set(result) == {
        "version", "shape", "size", "environment", "metrics", "known_limits", "nightly",
    }  # fmt: skip
    assert result["size"] == {"tables": 2_000, "columns": 16_000, "views": 200}
    keys = {
        "crawl_full_s", "crawl_peak_rss_bytes", "parser_peak_rss_bytes", "crawl_incremental_s",
        "incremental_ratio", "lineage_parse_s", "graph_build_s", "context_p50_ms",
        "context_p95_ms", "path_p95_ms", "lineage_upstream_p95_ms", "lineage_downstream_p95_ms",
        "store_bytes",
    }  # fmt: skip
    assert set(result["metrics"]) == keys
    assert all(isinstance(v, int | float) for v in result["metrics"].values())
    assert result["environment"]["runner_class"] == "test"
    json.dumps(result)
