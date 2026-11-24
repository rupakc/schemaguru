"""The FactLang fragment seam (spec 9.7): join snippets and filter validation."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from schemaguru.diagnostics import DiagnosticBag, Severity
from schemaguru.graph import Hop
from schemaguru.graph import Path as GPath
from schemaguru.measure import flbridge
from schemaguru.render import fragments
from schemaguru.store.sqlite import SQLiteStore
from tests.golden.shop_knowledge import shop_graph, shop_store, t


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    with shop_store(tmp_path / "shop.db") as s:
        yield s


@pytest.fixture
def no_factlang(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fragments, "_capable", lambda name: False)


def a_path(store: SQLiteStore) -> GPath:
    graph = shop_graph(store)
    edge = next(e for e in graph.edges if e.key.startswith("shop.orders.customer_id"))
    return GPath((Hop(edge, False),), 1.0, 0.98, False)


def test_join_snippet_absent_capability_is_none_with_sg501(
    store: SQLiteStore, no_factlang: None
) -> None:
    bag = DiagnosticBag()
    soft = {t("orders"): "deleted IS NULL OR NOT deleted"}
    assert fragments.render_join_snippet(a_path(store), "duckdb", soft, bag=bag) is None
    (d,) = list(bag)
    assert d.code == "SG501" and d.severity is Severity.INFO
    assert "render_join" in d.message
    # without a bag it is still None, and never raises
    assert fragments.render_join_snippet(a_path(store), "duckdb", soft) is None


def test_validate_filter_falls_back_to_syntax_check(no_factlang: None) -> None:
    ok = fragments.validate_filter("deleted IS NULL OR NOT deleted", "duckdb")
    assert not ok.errors()
    assert [d.code for d in ok] == ["SG503"]
    bad = fragments.validate_filter("status = 'paid", "duckdb")
    assert [d.code for d in bad.errors()] == ["SG502"]
    assert "unterminated" in bad.errors()[0].message
    assert fragments.validate_filter("a; drop", "duckdb").errors()


def test_capability_probe_follows_flbridge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flbridge, "capabilities", lambda: frozenset({"render_join"}))
    assert fragments._capable("render_join")
    assert not fragments._capable("parse_filter")


@pytest.mark.factlang
def test_render_join_snippet_with_factlang(store: SQLiteStore) -> None:
    if "render_join" not in flbridge.capabilities():
        pytest.skip("factlang capability render_join not yet available")
    soft = {t("orders"): "deleted IS NULL OR NOT deleted"}
    text = fragments.render_join_snippet(a_path(store), "duckdb", soft)
    assert text is not None and "JOIN" in text.upper() and "deleted" in text


@pytest.mark.factlang
def test_validate_filter_with_factlang() -> None:
    if "parse_filter" not in flbridge.capabilities():
        pytest.skip("factlang capability parse_filter not yet available")
    assert not fragments.validate_filter("deleted IS NULL OR NOT deleted", "duckdb").errors()
    assert fragments.validate_filter("deleted IS IS", "duckdb").errors()


def test_render_codes_are_registered() -> None:
    from schemaguru.diagnostics import CODES

    doc = (Path(__file__).resolve().parents[3] / "docs" / "reference" / "diagnostics.md").read_text(
        encoding="utf-8"
    )
    for code in ("SG501", "SG502", "SG503", "SG504"):
        assert code in CODES and f"| {code} |" in doc
