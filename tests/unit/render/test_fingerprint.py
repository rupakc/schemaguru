"""Whole views: determinism, block order, legend, budget, join paths, fingerprints (13.1, 13.4)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from schemaguru._util.hashing import stable_hash
from schemaguru.graph import Hop
from schemaguru.graph import Path as GPath
from schemaguru.model.detail import DetailLevel
from schemaguru.model.refs import ObjectRef
from schemaguru.render import (
    LEGEND_SENTENCE,
    ContextView,
    ViewRenderer,
    fingerprint,
    fingerprint_views,
)
from schemaguru.render.table_block import render_path
from schemaguru.scope import CharTokenCounter, fit
from schemaguru.store.sqlite import SQLiteStore
from tests.golden.shop_knowledge import CONFIG, NOW, shop_graph, shop_store, t

TABLES = [
    (t("orders"), DetailLevel.FULL),
    (t("customers"), DetailLevel.STANDARD),
    (t("payments"), DetailLevel.LITE),
    (t("products"), DetailLevel.NAME_ONLY),
]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    with shop_store(tmp_path / "shop.db") as s:
        yield s


def render(store: SQLiteStore, tables: list[tuple[ObjectRef, DetailLevel]] = TABLES) -> ContextView:
    r = ViewRenderer(store, now=NOW, cfg=CONFIG, graph=shop_graph(store))
    return r.view("revenue by segment", tables, not_shown=3)


def test_view_deterministic(tmp_path: Path) -> None:
    texts = []
    for seed in (None, 1, 2):
        with shop_store(tmp_path / f"s{seed}.db", shuffle=seed) as s:
            v = render(s)
            texts.append((v.text, v.to_json(), v.to_xml(), v.to_markdown(), v.fingerprint))
    assert texts[0] == texts[1] == texts[2]


def test_view_renders_twice_byte_equal(store: SQLiteStore) -> None:
    assert render(store).text.encode() == render(store).text.encode()


def test_stable_blocks_first(store: SQLiteStore) -> None:
    v = render(store)
    flags = [b.stable for b in v.blocks]
    assert flags == sorted(flags, reverse=True)
    assert flags[0] and not flags[-1]
    kinds = [b.kind for b in v.blocks]
    assert kinds[:3] == ["global_pitfalls", "never_query", "legend"]
    assert kinds[-1] == "not_shown"
    assert (
        v.blocks[-1].text == "Not shown: 3 other candidate tables (search_tables / describe_table)."
    )


def test_legend_only_with_warnings(store: SQLiteStore, tmp_path: Path) -> None:
    v = render(store)
    assert "⚠" in v.text
    legend = [b for b in v.blocks if b.kind == "legend"]
    assert len(legend) == 1 and LEGEND_SENTENCE in legend[0].text
    assert LEGEND_SENTENCE == "Absence of a warning is not verification."
    with shop_store(tmp_path / "quiet.db", []) as quiet:
        q = ViewRenderer(quiet, now=NOW, cfg=CONFIG).view(
            "q", [(t("ref_parent"), DetailLevel.FULL)]
        )
    assert "⚠" not in q.text
    assert not [b for b in q.blocks if b.kind == "legend"]


def test_view_fits_budget_including_separators(store: SQLiteStore) -> None:
    counter = CharTokenCounter()
    graph = shop_graph(store)
    ranked = [t(n) for n in ("orders", "customers", "payments", "order_items", "products")]
    sizes = []
    for budget in (200, 300, 450, 600, 900, 2000):
        r = ViewRenderer(store, now=NOW, cfg=CONFIG, graph=graph)
        chosen = fit(ranked, r, r.table_budget(budget, counter), counter)
        v = r.view("q", chosen, not_shown=2, budget_tokens=budget, counter=counter)
        assert v.tokens <= budget, (budget, v.tokens)
        assert v.tokens == counter.count(v.text)
        assert not v.diagnostics
        sizes.append(len(chosen))
    assert sizes[0] < sizes[-1] == len(ranked)  # the budget really bound the tables
    # always-on blocks that cannot fit are still shown (the legend must accompany a warning),
    # and the view says it is over budget
    r = ViewRenderer(store, now=NOW, cfg=CONFIG)
    assert r.table_budget(20, counter) == 0
    v = r.view("q", [], budget_tokens=20, counter=counter)
    assert [d.code for d in v.diagnostics] == ["SG504"]
    with pytest.raises(ValueError, match="budget"):
        ViewRenderer(store, now=NOW, cfg=CONFIG).table_budget(-1, counter)


def test_paths_block_lists_minimal_paths(store: SQLiteStore) -> None:
    v = render(
        store,
        [
            (t("customers"), DetailLevel.LITE),
            (t("order_items"), DetailLevel.LITE),
            (t("orders"), DetailLevel.LITE),
        ],
    )
    paths = next(b for b in v.blocks if b.kind == "join_paths").text
    # rank order: customers first, so the route is told from customers
    assert "  shop.customers → shop.orders · coverage 100%" in paths
    assert "orders.customer_id = customers.customer_id (N:1)" in paths
    # customers..order_items goes through orders, which is shown: composed, not repeated
    assert "shop.customers → shop.orders → shop.order_items" not in paths
    assert "shop.order_items → shop.orders → shop.customers" not in paths


def test_path_with_revisit_renders_alias(store: SQLiteStore) -> None:
    graph = shop_graph(store)
    edge = next(e for e in graph.edges if e.key.startswith("shop.orders.account_id"))
    p = GPath((Hop(edge, False), Hop(edge, True)), 1.0, 1.0, False, revisits=True)
    lines = render_path(p)
    assert lines[0].startswith("  shop.orders → shop.accounts → shop.orders AS orders_2")
    assert "orders_2.account_id = accounts.account_id" in "\n".join(lines)


def test_thin_provisional_path_states_coverage(store: SQLiteStore) -> None:
    graph = shop_graph(store)
    edge = next(e for e in graph.edges if e.key.startswith("shop.payments.order_id"))
    p = GPath((Hop(edge, True),), 3.0, 0.05, True)
    head = render_path(p)[0]
    assert "provisional: narrowest support 5%" in head
    assert "shop.orders → shop.payments" in head


def test_fingerprint_is_hash_of_text(store: SQLiteStore) -> None:
    v = render(store)
    assert fingerprint(v) == stable_hash(v.text) == v.fingerprint


def test_fingerprint_views(store: SQLiteStore) -> None:
    class Guru:
        def __init__(self) -> None:
            self.r = ViewRenderer(store, now=NOW, cfg=CONFIG)

        def context(self, question: str) -> ContextView:
            return self.r.view(question, TABLES)

    fps = fingerprint_views(Guru(), ["a", "b"])
    assert list(fps) == ["a", "b"]
    assert fps["a"] == fps["b"]  # same tables: the question itself is not in the view text
    assert fps == fingerprint_views(Guru(), ["a", "b"])
