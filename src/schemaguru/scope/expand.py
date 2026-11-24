"""Graph expansion: the bridging tables between seeds (spec 12.1 step 3)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from schemaguru.config import GraphConfig, ScopeConfig
from schemaguru.graph.search import Graph, find_paths
from schemaguru.model.refs import ObjectRef


def expand(
    seeds: Sequence[ObjectRef],
    graph: Graph,
    cfg: ScopeConfig,
    *,
    graph_cfg: GraphConfig | None = None,
) -> list[ObjectRef]:
    """New tables on the best path between each pair of seeds, cheapest path first.

    Only the single best route per pair counts; a refused pair (no evidence, hub pivot, no
    route) adds nothing. A table on several paths is ranked by its cheapest one; ties break by
    canonical ref. At most ``cfg.max_expansion_tables`` tables; seeds are never returned.
    """
    if cfg.max_expansion_tables <= 0:
        return []
    gcfg = replace(graph_cfg or GraphConfig(), k_paths=1)
    tables = list(dict.fromkeys(s.table_ref() for s in seeds))
    seed_set = set(tables)
    best: dict[ObjectRef, float] = {}
    for i, a in enumerate(tables):
        for b in tables[i + 1 :]:
            res = find_paths(graph, a, b, cfg=gcfg)
            if res.refusal is not None or not res.paths:
                continue
            path = res.paths[0]
            for table in path.tables[1:-1]:
                if table not in seed_set and path.cost < best.get(table, float("inf")):
                    best[table] = path.cost
    ranked = sorted(best, key=lambda r: (best[r], r.canonical()))
    return ranked[: cfg.max_expansion_tables]
