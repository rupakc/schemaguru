"""Crawl scale benchmark: 500k synthetic tables, prints tables/s and peak memory.

Run by hand or nightly (not collected by pytest): ``python benchmarks/bench_crawl.py [N]``.
"""

from __future__ import annotations

import resource
import sys
import tempfile
from pathlib import Path

from schemaguru.catalog import crawl
from schemaguru.config import CrawlConfig
from schemaguru.store.sqlite import SQLiteStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.unit.catalog.fake_reader import FakeReader


def main(n: int = 500_000) -> None:
    with tempfile.TemporaryDirectory() as d:
        store = SQLiteStore(Path(d) / "bench.db")
        try:
            report = crawl(FakeReader(n, tables_per_schema=10_000), store, config=CrawlConfig())
        finally:
            store.close()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mb = peak / (1 << 20) if sys.platform == "darwin" else peak / 1024
    print(
        f"tables={report.tables_written} duration={report.duration_s:.1f}s "
        f"rate={report.tables_written / max(report.duration_s, 1e-9):.0f} tables/s "
        f"peak_rss={peak_mb:.0f} MB queries={report.queries} errors={len(report.errors)}"
    )


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 500_000)
