"""Scale benchmark harness (0.2.0 spec 6): crawl, then query the local store.

``python3 -m benchmarks.scale.harness --tables N --out PATH --runner-class NAME [--workdir DIR]``

The crawl phases (full, then 1% changed) run in a child process so the peak RSS belongs to the
crawl alone; the parser worker's peak is ``RUSAGE_CHILDREN`` of that child. The query phases
(graph build, ``context``, join paths, lineage) run in the parent against the crawled store with
SYNTHETIC measured join facts (see ``generator``). Not library code: it uses internals.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmarks.scale.generator import (
    ScaleShape,
    SyntheticReader,
    changed_set,
    is_view,
    synthetic_join_facts,
)
from benchmarks.scale.questions import QUESTIONS, lineage_refs, path_pairs

VERSION = "0.2.0"
REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWN_LIMITS = [
    "join facts are synthetic (hash-derived rates, source benchmark:synthetic), not measured",
    "a view's declared columns are not derived from its definition",
    "context runs against a store with no column-level facts beyond the crawl",
]


def _rss_bytes(who: int | None = None) -> int | None:
    """Peak RSS in bytes (Linux reports KiB, macOS bytes); ``None`` where there is no
    ``resource`` module (Windows)."""
    try:
        import resource
    except ImportError:
        return None
    who = resource.RUSAGE_SELF if who is None else who
    peak = int(resource.getrusage(who).ru_maxrss)
    return peak if sys.platform == "darwin" else peak * 1024


def _children_rss() -> int | None:
    try:
        import resource
    except ImportError:
        return None
    return _rss_bytes(resource.RUSAGE_CHILDREN)


def percentile(samples: list[float], q: float) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _sample(fn: Any, calls: list[tuple[Any, ...]], ok: Any) -> tuple[list[float], int]:
    """Time ``fn`` per call (ms); also count the calls whose result ``ok`` accepts."""
    times: list[float] = []
    good = 0
    for args in calls:
        t0 = time.perf_counter()
        out = fn(*args)
        times.append((time.perf_counter() - t0) * 1000.0)
        good += bool(ok(out))
    return times, good


def _ctx_ok(view: Any) -> bool:
    return bool(getattr(view, "tables", ()))


def _path_ok(result: Any) -> bool:
    return bool(getattr(result, "paths", ()))


def _lineage_ok(result: Any) -> bool:
    return bool(getattr(result, "nodes", ()) or getattr(result, "edges", ()))


def crawl_phase(shape: ScaleShape, db: Path) -> dict[str, Any]:
    from schemaguru.catalog import crawl
    from schemaguru.config import CrawlConfig
    from schemaguru.store.sqlite import SQLiteStore

    store = SQLiteStore(db)
    out: dict[str, Any] = {}
    try:
        cfg = CrawlConfig()
        t0 = time.perf_counter()
        full = crawl(SyntheticReader(shape), store, config=cfg)
        out["crawl_full_s"] = time.perf_counter() - t0
        out["crawl_peak_rss_bytes"] = _rss_bytes()
        out["parser_peak_rss_bytes"] = _children_rss()
        out["lineage_parse_s"] = full.lineage_s
        out["tables_written"] = full.tables_written
        out["views_parsed"] = full.views_parsed
        out["errors"] = len(full.errors)
        changed = changed_set(shape)
        reader = SyntheticReader(shape, changed=changed, changed_at=datetime.now(timezone.utc))
        t0 = time.perf_counter()
        inc = crawl(reader, store, config=cfg)
        out["crawl_incremental_s"] = time.perf_counter() - t0
        out["incremental_tables_written"] = inc.tables_written
        out["incremental_changed"] = len(changed)
    finally:
        store.close()
    return out


def _sysctl_brand() -> str:
    out = subprocess.run(
        ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, check=True
    )
    return out.stdout


def _cpu_model(
    *,
    system: str | None = None,
    cpuinfo: Path = Path("/proc/cpuinfo"),
    sysctl: Callable[[], str] = _sysctl_brand,
) -> str:
    """The CPU model ("Apple M2 Pro", "AMD EPYC 7763 ..."), else the architecture."""
    system = platform.system() if system is None else system
    model = ""
    with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
        if system == "Darwin":
            model = sysctl().strip()
        elif system == "Linux":
            for line in cpuinfo.read_text().splitlines():
                key, _, value = line.partition(":")
                if key.strip() == "model name":
                    model = value.strip()
                    break
    return model or platform.processor() or platform.machine()


def _environment(runner_class: str) -> dict[str, Any]:
    ram = 0
    with contextlib.suppress(ValueError, OSError, AttributeError):
        ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    return {
        "cpu": _cpu_model(),
        "cores": os.cpu_count() or 0,
        "ram_bytes": int(ram),
        "python": platform.python_version(),
        "os": platform.platform(),
        "runner_class": runner_class,
    }


def _store_bytes(db: Path) -> int:
    return sum(p.stat().st_size for p in db.parent.glob(db.name + "*") if p.is_file())


def run(
    shape: ScaleShape,
    workdir: Path,
    *,
    runner_class: str,
    n_questions: int = 200,
    n_paths: int = 200,
    n_lineage: int = 200,
) -> dict[str, Any]:
    from schemaguru import Guru
    from schemaguru.store.sqlite import SQLiteStore

    workdir.mkdir(parents=True, exist_ok=True)
    db = workdir / "scale.db"
    for p in workdir.glob("scale.db*"):
        p.unlink()
    shape_json = json.dumps(asdict(shape))
    proc = subprocess.run(
        [sys.executable, "-m", "benchmarks.scale.harness", "--phase", "crawl",
         "--shape-json", shape_json, "--db", str(db)],
        capture_output=True, text=True, check=False, cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (str(REPO_ROOT), os.environ.get("PYTHONPATH")) if p
        )},
    )  # fmt: skip
    if proc.returncode != 0:
        raise RuntimeError(f"crawl phase failed ({proc.returncode}): {proc.stderr[-2000:]}")
    crawled: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])

    store = SQLiteStore(db)
    try:
        batch: list[Any] = []
        for fact in synthetic_join_facts(shape):
            batch.append(fact)
            if len(batch) >= 5000:
                store.put_facts(batch)
                batch = []
        if batch:
            store.put_facts(batch)
        guru = Guru(store=store)
        try:
            t0 = time.perf_counter()
            guru._current_graph()
            graph_build_s = time.perf_counter() - t0
            pairs = path_pairs(shape, n_paths)
            refs = lineage_refs(shape, n_lineage)
            if not pairs:
                raise RuntimeError("no join-path pairs in this shape: path_p95_ms would be empty")
            if not refs:
                raise RuntimeError("no lineage refs in this shape: lineage p95 would be empty")
            questions = QUESTIONS[:n_questions]
            ctx, nonempty_ctx = _sample(guru.context, [(q,) for q in questions], _ctx_ok)
            paths, nonempty_path = _sample(guru.find_path, list(pairs), _path_ok)
            up, nonempty_up = _sample(guru.lineage, [(r, "upstream", 2) for r in refs], _lineage_ok)
            down, nonempty_down = _sample(
                guru.lineage, [(r, "downstream", 2) for r in refs], _lineage_ok
            )
        finally:
            guru.close()
    finally:
        store.close()

    views = sum(1 for i in range(shape.tables) if is_view(i, shape))
    full_s = float(crawled["crawl_full_s"])
    metrics: dict[str, Any] = {
        "crawl_full_s": full_s,
        "crawl_peak_rss_bytes": crawled["crawl_peak_rss_bytes"],
        "parser_peak_rss_bytes": crawled["parser_peak_rss_bytes"],
        "crawl_incremental_s": crawled["crawl_incremental_s"],
        "incremental_ratio": crawled["crawl_incremental_s"] / max(full_s, 1e-9),
        "lineage_parse_s": crawled["lineage_parse_s"],
        "graph_build_s": graph_build_s,
        "context_p50_ms": percentile(ctx, 0.50),
        "context_p95_ms": percentile(ctx, 0.95),
        "path_p95_ms": percentile(paths, 0.95),
        "lineage_upstream_p95_ms": percentile(up, 0.95),
        "lineage_downstream_p95_ms": percentile(down, 0.95),
        "store_bytes": _store_bytes(db),
    }
    return {
        "version": VERSION,
        "shape": asdict(shape),
        "size": {
            "tables": shape.tables,
            "columns": shape.tables * shape.columns_per_table,
            "views": views,
        },
        "environment": _environment(runner_class),
        "metrics": metrics,
        "known_limits": KNOWN_LIMITS,
        "nightly": None,
        "checks": {
            "tables_written": crawled["tables_written"],
            "errors": crawled["errors"],
            "views_parsed": crawled["views_parsed"],
            "incremental_tables_written": crawled["incremental_tables_written"],
            "incremental_changed": crawled["incremental_changed"],
            "n_questions": len(questions),
            "context_nonempty": nonempty_ctx,
            "n_paths": len(pairs),
            "path_nonempty": nonempty_path,
            "n_lineage": len(refs),
            "lineage_nonempty": min(nonempty_up, nonempty_down),
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", type=int, default=ScaleShape.tables)
    ap.add_argument("--columns-per-table", type=int, default=ScaleShape.columns_per_table)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--runner-class", default="local")
    ap.add_argument("--workdir", type=Path)
    ap.add_argument("--phase", choices=("crawl",))
    ap.add_argument("--shape-json")
    ap.add_argument("--db", type=Path)
    a = ap.parse_args(argv)
    if a.phase == "crawl":
        shape = ScaleShape(**json.loads(a.shape_json))
        print(json.dumps(crawl_phase(shape, a.db)))
        return 0
    if a.out is None:
        ap.error("--out is required")
    shape = replace(ScaleShape(), tables=a.tables, columns_per_table=a.columns_per_table)
    import tempfile

    if a.workdir is not None:
        result = run(shape, a.workdir, runner_class=a.runner_class)
    else:
        with tempfile.TemporaryDirectory() as d:
            result = run(shape, Path(d), runner_class=a.runner_class)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
