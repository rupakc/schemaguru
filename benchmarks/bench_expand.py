"""Graph-expansion benchmark: ``expand`` with ``seed_k=8`` seeds on a synthetic 50k-edge graph.

Run by hand (not collected by pytest):
``python benchmarks/bench_expand.py [EDGES] [TABLES] [ROUNDS]``.
Prints graph build time and expand timings (first call builds the component's adjacency).
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

from schemaguru.config import GraphConfig, ScopeConfig
from schemaguru.graph.edges import build_edges
from schemaguru.graph.hubs import Hubs
from schemaguru.graph.search import Graph
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.scope.expand import expand

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.fixtures.shop import JoinFacts
from tests.unit.graph.shop_graph import effective, join_value, measured


def main(n_edges: int = 50_000, n_tables: int = 20_000, rounds: int = 3, seed: int = 11) -> None:
    rng = random.Random(seed)
    tables = [ObjectRef(None, f"s{i % 50}", f"t{i}") for i in range(n_tables)]
    facts, seen = [], set()
    while len(facts) < n_edges:
        a, b = rng.randrange(n_tables), rng.randrange(n_tables)
        if a == b or (a, b) in seen:
            continue
        seen.add((a, b))
        left, right = tables[a], tables[b]
        join = JoinRef(
            (ObjectRef(None, left.schema, left.table, f"fk_{b}"),),
            (ObjectRef(None, right.schema, right.table, "id"),),
        )
        rate = rng.choice((1.0, 0.99, 0.9, 0.6))
        matched = round(100 * rate)
        facts.append(measured(join, join_value(JoinFacts(100, 100, matched, matched, 100))))
    cfg = GraphConfig()
    t0 = time.perf_counter()
    edges = build_edges(effective(facts), cfg=cfg)
    graph = Graph.build(edges, Hubs.from_edges(edges, set(), cfg))
    build_s = time.perf_counter() - t0
    scope = ScopeConfig()
    timings = []
    for _ in range(rounds):
        seeds = rng.sample(sorted(graph.tables, key=lambda r: r.canonical()), scope.seed_k)
        t0 = time.perf_counter()
        added = expand(seeds, graph, scope, graph_cfg=cfg)
        timings.append((time.perf_counter() - t0) * 1000)
    timings_sorted = sorted(timings)
    print(
        f"edges={len(edges)} tables={len(graph.tables)} build={build_s:.1f}s "
        f"expand(seed_k={scope.seed_k}) first={timings[0]:.0f}ms "
        f"median={timings_sorted[len(timings) // 2]:.0f}ms max={timings_sorted[-1]:.0f}ms "
        f"last_added={len(added)}"
    )


if __name__ == "__main__":
    args = [int(a) for a in sys.argv[1:]]
    main(*args)
