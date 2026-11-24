"""How SchemaGuru measures a table too large to read in full, offline.

    python examples/large_table_sampling.py [path/to/warehouse.duckdb]

Part 1 asks the sampling policy what it would do for a 2 TiB fact table and for a small one
(pure functions: no warehouse involved). Part 2 forces sampling on the small shop tables
(``SamplingConfig(measurement="force")``, meant for tests and demos, at 5%) so you can see how
sampled numbers render: ``≈`` estimates with their method, never "verified".
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from _shop import measured, shop_db, url

import schemaguru as sg
from schemaguru.measure import KeyOverlapReq, ProfileColumns, TopValues, choose_sample, is_large
from schemaguru.model.structure import Table

TIB = 1 << 40


def describe(choice: object) -> str:
    if choice is None:
        return "exact"
    return f"{choice.method} sample, fraction {choice.fraction}"  # type: ignore[attr-defined]


def part1() -> None:
    cfg = sg.SamplingConfig()  # defaults: large = bytes >= 50 GiB, or rows >= 500M
    budget = sg.Budget()
    big = Table(
        sg.ObjectRef(None, "shop", "events_raw"), "table", row_count=40_000_000_000, bytes=2 * TIB
    )
    small = Table(sg.ObjectRef(None, "shop", "orders"), "table", row_count=10_000, bytes=1 << 20)
    join = sg.JoinRef(
        (sg.ObjectRef(None, "shop", "events_raw", "session_id"),),
        (sg.ObjectRef(None, "shop", "sessions", "session_id"),),
    )
    requests = {
        "join match/coverage (KeyOverlap)": KeyOverlapReq(join),
        "null rates and distincts (profile)": ProfileColumns(big.ref, ("session_id", "kind")),
        "top values of kind": TopValues(sg.ObjectRef(None, "shop", "events_raw", "kind"), 20),
    }
    for table in (big, small):
        print(f"{table.ref.canonical()}: large = {is_large(table, cfg)}")
        for label, req in requests.items():
            choice = choose_sample(
                table,
                req,
                cfg,
                key_distinct=4_000_000_000 if table is big else 10_000,
                est_bytes=table.bytes,
                max_bytes_per_probe=budget.max_bytes_per_probe,
                seed=42,
                pk=("event_id",),
            )
            print(f"  {label:38} -> {describe(choice)}")
    print()


def part2() -> None:
    db = shop_db()
    # force: sample every table; the floor fraction (default 0.1%) is raised to 5% because the
    # shop tables are tiny and 0.1% of 10,000 rows is about ten rows
    cfg = sg.Config(sampling=sg.SamplingConfig(measurement="force", block_fraction_floor=0.05))
    with tempfile.TemporaryDirectory() as tmp:
        guru = sg.Guru.connect(url(db), store=str(Path(tmp) / "shop.db"), config=cfg)
        try:
            if measured(guru.measure("shop.orders")):
                view = guru.handle_tool_call("describe_table", {"table": "orders"})
                print(view["text"])
        finally:
            guru.close()


if __name__ == "__main__":
    part1()
    part2()
