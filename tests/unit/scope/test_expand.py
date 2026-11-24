from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import replace

from schemaguru.config import GraphConfig, ScopeConfig
from schemaguru.graph.edges import build_edges
from schemaguru.graph.hubs import Hubs
from schemaguru.graph.search import Graph, find_paths
from schemaguru.model.facts import Fact
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.scope.expand import expand
from tests.fixtures.shop import JoinFacts
from tests.unit.graph.shop_graph import effective, j, join_value, measured, shop_facts, t

CFG = GraphConfig()


def _graph(facts: Sequence[Fact]) -> Graph:
    edges = build_edges(effective(facts), cfg=CFG)
    return Graph.build(edges, Hubs.from_edges(edges, set(), CFG))


def _syn(left: str, right: str) -> Fact:
    jf = JoinFacts(100, 100, 100, 100, 100)
    return measured(j(left, right), join_value(jf))


def test_expand_adds_bridge_table() -> None:
    # events -> sessions <- orders: sessions bridges the two seeds.
    g = _graph([*shop_facts(), _syn("orders.session_id", "sessions.session_id")])
    assert expand([t("events"), t("orders")], g, ScopeConfig()) == [t("sessions")]
    # seeds already adjacent: nothing to add; a seed is never returned as a bridge
    assert expand([t("orders"), t("customers")], g, ScopeConfig()) == []
    assert expand([t("events"), t("orders"), t("sessions")], g, ScopeConfig()) == []


def test_expand_ranks_by_path_cost_and_caps() -> None:
    g = _graph(shop_facts())
    # payments -> orders -> customers (1 bridge) and payments ... products via order_items
    got = expand([t("payments"), t("customers"), t("products")], g, ScopeConfig())
    assert set(got) == {t("orders"), t("order_items")}
    capped = expand(
        [t("payments"), t("customers"), t("products")],
        g,
        replace(ScopeConfig(), max_expansion_tables=1),
    )
    assert len(capped) == 1 and capped[0] == got[0]
    assert (
        expand([t("payments"), t("products")], g, replace(ScopeConfig(), max_expansion_tables=0))
        == []
    )


def test_expand_tolerates_refusals_and_unknown_seeds() -> None:
    g = _graph(shop_facts())
    # orders <-> events pivots through the accounts hub: refused, so no bridge is invented
    assert expand([t("orders"), t("events")], g, ScopeConfig()) == []
    assert expand([t("nope"), t("orders")], g, ScopeConfig()) == []
    assert expand([], g, ScopeConfig()) == []
    assert expand([t("orders"), t("orders")], g, ScopeConfig()) == []


def test_expand_is_deterministic() -> None:
    g = _graph(shop_facts())
    seeds = [t("payments"), t("customers"), t("products")]
    assert expand(seeds, g, ScopeConfig()) == expand(seeds, g, ScopeConfig())


def _reference_expand(
    seeds: Sequence[ObjectRef], graph: Graph, cfg: ScopeConfig
) -> list[ObjectRef]:
    """The pairwise ``find_paths`` implementation expand() replaced (canonical pair order)."""
    gcfg = replace(GraphConfig(), k_paths=1)
    tables = sorted({s.table_ref() for s in seeds}, key=lambda r: r.canonical())
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


def test_expand_equals_pairwise_reference_on_random_graphs() -> None:
    nonempty = 0
    for seed in range(20):
        rng = random.Random(seed)
        n = rng.randint(8, 25)
        tabs = [ObjectRef(None, "r", f"t{i}") for i in range(n)]
        facts, seen = [], set()
        for _ in range(rng.randint(n, 3 * n)):
            a, b = rng.randrange(n), rng.randrange(n)
            if a == b or (a, b) in seen:
                continue
            seen.add((a, b))
            join = JoinRef(
                (ObjectRef(None, "r", tabs[a].table, f"fk{b}"),),
                (ObjectRef(None, "r", tabs[b].table, "id"),),
            )
            m = round(100 * rng.choice((1.0, 0.99, 0.9, 0.6)))
            facts.append(measured(join, join_value(JoinFacts(100, 100, m, m, 100))))
        g = _graph(facts)
        cfg = replace(ScopeConfig(), max_expansion_tables=rng.choice((2, 5, 50)))
        pool = sorted(g.tables, key=lambda r: r.canonical())
        picks = rng.sample(pool, min(len(pool), 5))
        want = _reference_expand(picks, g, cfg)
        nonempty += bool(want)
        assert expand(picks, g, cfg) == want
        assert expand(picks[::-1], g, cfg) == want
    assert nonempty >= 5
