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


class _Inline:
    def __init__(self, text: str) -> None:
        self.text = text

    def inline(self) -> str:
        return self.text


@pytest.fixture
def fake_factlang(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """A FactLang with ``parse_filter`` and ``sql.render_join`` (real ones: Task 25)."""
    import sys
    import types

    import factlang

    seen: dict[str, object] = {}

    def parse_filter(text: str) -> tuple[object, list[object]]:
        if "(((" in text:
            return None, [types.SimpleNamespace(severity="ERROR", message="bad")]
        return text, []

    def render_join(edges: object, *, dialect: str, filters: object, aliases: object) -> _Inline:
        seen.update(edges=edges, dialect=dialect, filters=filters, aliases=aliases)
        return _Inline("FROM <join>")

    refs = types.ModuleType("factlang.semantics.refs")
    refs.Ident = lambda text, quoted: text  # type: ignore[attr-defined]
    refs.TableRef = lambda *parts: tuple(parts)  # type: ignore[attr-defined]
    refs.ColumnRef = lambda table, column: (table, column)  # type: ignore[attr-defined]
    refs.EdgeRef = lambda left, right: (left, right)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "factlang.semantics", types.ModuleType("factlang.semantics"))
    monkeypatch.setitem(sys.modules, "factlang.semantics.refs", refs)
    sql = types.ModuleType("factlang.sql")
    sql.render_join = render_join  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "factlang.sql", sql)
    monkeypatch.setattr(factlang, "parse_filter", parse_filter, raising=False)
    monkeypatch.setattr(fragments, "_capable", lambda name: True)
    return seen


def test_snippet_aliases_match_the_path_text(store: SQLiteStore, fake_factlang: dict) -> None:  # type: ignore[type-arg]
    from schemaguru.render.table_block import render_path

    graph = shop_graph(store)
    edge = next(e for e in graph.edges if e.key.startswith("shop.orders.account_id"))
    p = GPath((Hop(edge, False), Hop(edge, True)), 1.0, 1.0, False, revisits=True)
    text = fragments.render_join_snippet(p, "duckdb", {t("orders"): "(((broken"})
    assert fake_factlang["aliases"] == {0: "orders", 1: "accounts", 2: "orders_2"}
    assert "orders_2" in render_path(p)[0]
    # an unparseable filter is never dropped silently
    assert text == "FROM <join>\n-- live-rows filter for shop.orders unparseable, omitted"


def test_only_checked_live_rows_filters_reach_sql(tmp_path: Path, fake_factlang: dict) -> None:  # type: ignore[type-arg]
    from schemaguru.config import Config
    from schemaguru.model.detail import DetailLevel
    from schemaguru.model.facts import Fact, Provenance, Status
    from schemaguru.model.kinds import FactKind
    from schemaguru.render import ViewRenderer
    from tests.golden.shop_knowledge import NOW, human, shop_facts

    extra = [
        human(
            FactKind.SOFT_DELETE,
            t("customers"),
            {"predicate": "email IS NOT NULL", "marker_columns": None},
        ),
        Fact.make(
            FactKind.SOFT_DELETE,
            t("payments"),
            {"predicate": "amount > 0", "marker_columns": ["amount"]},
            Provenance.DRAFTED,
            Status.UNVERIFIED,
        ),
        Fact.make(
            FactKind.SOFT_DELETE,
            t("order_items"),
            {"predicate": "line_no > 0", "marker_columns": ["line_no"]},
            Provenance.DRAFTED,
            Status.CONTRADICTED,
        ),
    ]
    with shop_store(tmp_path / "s.db", [*shop_facts(), *extra]) as s:
        r = ViewRenderer(s, now=NOW, cfg=Config(), graph=shop_graph(s), dialect="duckdb")
        tables = [
            (t(n), DetailLevel.LITE) for n in ("orders", "customers", "payments", "order_items")
        ]
        paths = next(b for b in r.view("q", tables).blocks if b.kind == "join_paths").text
    assert "-- live-rows filter for shop.payments unverified, omitted" in paths
    assert "-- live-rows filter for shop.order_items contradicted, omitted" in paths
    assert "customers" not in "".join(ln for ln in paths.splitlines() if "omitted" in ln)
    passed = {str(v) for v in fake_factlang["filters"].values()}  # type: ignore[union-attr]
    assert "amount > 0" not in passed and "line_no > 0" not in passed
