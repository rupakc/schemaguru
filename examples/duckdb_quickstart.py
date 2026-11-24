"""SchemaGuru on a local DuckDB warehouse, end to end, offline.

    python examples/duckdb_quickstart.py [path/to/warehouse.duckdb]

1. connect (the catalog is crawled on first use: metadata only),
2. ask for a question-scoped view — before anything is measured,
3. measure every table (exact: the shop tables are small) within the default budget,
4. load a human overlay (``examples/schemaguru.yaml``),
5. ask again, and ask for join paths — one safe, one refused.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from _shop import HERE, measured, shop_db, url

import schemaguru as sg


def main() -> None:
    db = shop_db()
    with tempfile.TemporaryDirectory() as tmp:
        guru = sg.Guru.connect(url(db), store=str(Path(tmp) / "shop.db"))
        try:
            question = "monthly revenue by customer segment"
            print("=== before measuring ===")
            print(guru.context(question))

            report = guru.measure(all=True)
            if measured(report):
                print(f"measured {len(report.measured_tables)} tables\n")

            for d in guru.load_overlays(HERE / "schemaguru.yaml"):
                print(f"overlay: {d.code} {d.message}")

            print("=== after measuring and the overlay ===")
            print(guru.context(question))

            for a, b in [("shop.payments", "shop.customers"), ("shop.orders", "shop.events")]:
                result = guru.find_path(a, b)
                if result.refusal is None:
                    print(f"{a} -> {b}:", " -> ".join(t.table for t in result.paths[0].tables))
                else:
                    print(f"{a} -> {b}: refused {result.refusal.value}: {result.explanation}")
        finally:
            guru.close()


if __name__ == "__main__":
    main()
