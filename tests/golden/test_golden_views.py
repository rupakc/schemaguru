"""Golden views: every built-in profile x three questions over the shop knowledge (spec 13).

The views are what an agent reads, so the files are meant to be read in review. Regenerate with
``SCHEMAGURU_UPDATE_GOLDEN=1 pytest tests/golden`` and read the diff.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from schemaguru.model.detail import DetailLevel
from schemaguru.render import ViewRenderer, collect, get_profile
from schemaguru.render.knowledge import join_subjects_by_table
from schemaguru.scope import CharTokenCounter, LexicalRetriever, build_scope
from schemaguru.store.sqlite import SQLiteStore
from tests.golden.golden import assert_golden
from tests.golden.shop_knowledge import CONFIG, NOW, QUESTIONS, shop_graph, shop_store, t

PROFILES = ("agent", "planner", "verifier", "router")
BUDGET = 1500


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SQLiteStore]:
    with shop_store(tmp_path_factory.mktemp("golden") / "shop.db") as s:
        yield s


def golden_view(store: SQLiteStore, profile: str, question: str, *, indexed: bool = False) -> str:
    """The view of ``question`` under ``profile`` with the golden settings (budget, scope).
    ``indexed`` reads each table's JOIN facts through the per-table index ``Guru`` uses."""
    counter = CharTokenCounter()
    graph = shop_graph(store)
    index = join_subjects_by_table(store.facts_for(None, ["JOIN"])) if indexed else None
    renderer = ViewRenderer(
        store, now=NOW, cfg=CONFIG, profile=profile, graph=graph, join_subjects=index
    )
    retriever = LexicalRetriever(store)
    # the candidates the fitter may pick from: their hazard warnings are reserved up front
    candidates = [r for r, _ in retriever.search(question, CONFIG.scope.seed_k)]
    scope = build_scope(
        question,
        retriever=retriever,
        store=store,
        graph=graph,
        renderer=renderer,
        counter=counter,
        budget_tokens=renderer.table_budget(BUDGET, counter, tables=candidates),
        cfg=CONFIG.scope,
        graph_cfg=CONFIG.graph,
    )
    view = renderer.view(
        question, scope.tables, not_shown=scope.not_shown, budget_tokens=BUDGET, counter=counter
    )
    assert view.tokens <= BUDGET
    assert not view.diagnostics
    return view.text


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("qi", range(len(QUESTIONS)))
def test_golden_view(store: SQLiteStore, profile: str, qi: int) -> None:
    assert_golden(f"{profile}__q{qi + 1}.txt", golden_view(store, profile, QUESTIONS[qi]))


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("qi", range(len(QUESTIONS)))
def test_golden_view_with_join_index(store: SQLiteStore, profile: str, qi: int) -> None:
    """The per-table JOIN index (``Guru``'s path) renders byte-identical views."""
    assert_golden(
        f"{profile}__q{qi + 1}.txt", golden_view(store, profile, QUESTIONS[qi], indexed=True)
    )


@pytest.mark.parametrize("profile", PROFILES)
def test_golden_orders_every_level(store: SQLiteStore, profile: str) -> None:
    k = collect(store, t("orders"), NOW, CONFIG)
    p = get_profile(profile)
    text = "\n\n".join(f"--- {lv.value}\n{p.render_table(k, lv)}" for lv in DetailLevel)
    assert_golden(f"orders__{profile}__levels.txt", text)
