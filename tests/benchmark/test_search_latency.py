"""Retrieval latency at scale (spec 12.4): search runs in the store's index, not in Python.

Both are absolute latency bounds, so both are marked ``benchmark`` (nightly; wall-clock bounds
flake on loaded runners and never gate a push). They build a synthetic shop-domain catalog
straight into the store.
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import pytest

from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Column, Table
from schemaguru.scope.retrievers import LexicalRetriever
from schemaguru.store.sqlite import SQLiteStore

NOUNS = [
    *("customers", "orders", "order_items", "products", "payments", "events", "accounts"),
    *("users", "sessions", "refunds", "invoices", "carts", "shipments", "returns", "reviews"),
    *("coupons", "stock", "prices", "visits", "clicks"),
]
QUALIFIERS = [
    *("daily", "weekly", "monthly", "raw", "staging", "clean", "archive", "snapshot", "agg"),
    *("dim", "fact", "hist"),
]
QUESTIONS = [
    "revenue by month from orders",
    "how many refunds per customer last week",
    "average basket size in carts",
    "payments that failed for accounts",
    "session clicks by product",
    "shipments delayed per region",
    "monthly active users",
    "stock levels for products",
    "coupon usage on invoices",
    "reviews with low rating",
]


def table_name(i: int) -> tuple[str, str]:
    qual = QUALIFIERS[(i // len(NOUNS)) % len(QUALIFIERS)]
    return f"s{i % 97}", f"{qual}_{NOUNS[i % len(NOUNS)]}_{i}"


def build_catalog(path: Path, n_tables: int, seed: int = 7) -> SQLiteStore:
    rng = random.Random(seed)
    store = SQLiteStore(path)
    batch: list[Table] = []
    with store.transaction():
        for i in range(n_tables):
            noun = NOUNS[i % len(NOUNS)]
            qual = QUALIFIERS[(i // len(NOUNS)) % len(QUALIFIERS)]
            schema, name = table_name(i)
            batch.append(
                Table(
                    ObjectRef(None, schema, name),
                    "table",
                    comment=f"{qual} {noun} {rng.choice(NOUNS)} {rng.choice(QUALIFIERS)}",
                )
            )
            if len(batch) == 2000:
                store.upsert_tables(batch)
                batch = []
        store.upsert_tables(batch)
        # a handful of tables with columns, so column tokens are indexed too
        for i in range(0, n_tables, max(1, n_tables // 500)):
            ref = ObjectRef(None, *table_name(i))
            store.upsert_columns(
                ref,
                [
                    Column(ObjectRef(None, ref.schema, ref.table, c), j, "T", "string")
                    for j, c in enumerate(("id", "amount", "created_at", "status"), 1)
                ],
            )
    return store


def p95_ms(store: SQLiteStore, rounds: int) -> float:
    r = LexicalRetriever(store)
    for q in QUESTIONS:  # warm caches
        r.search(q, 8)
    times = []
    for _ in range(rounds):
        for q in QUESTIONS:
            t0 = time.perf_counter()
            hits = r.search(q, 8)
            times.append((time.perf_counter() - t0) * 1000)
            assert hits
    times.sort()
    return times[int(0.95 * (len(times) - 1))]


@pytest.mark.benchmark
def test_search_latency_50k(tmp_path: Path) -> None:
    with build_catalog(tmp_path / "s50k.db", 50_000) as store:
        assert p95_ms(store, rounds=3) < 200


@pytest.mark.benchmark
def test_search_latency_500k(tmp_path: Path) -> None:
    with build_catalog(tmp_path / "s500k.db", 500_000) as store:
        assert p95_ms(store, rounds=5) < 200
