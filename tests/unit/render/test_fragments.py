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
    bag = DiagnosticBag()
    text = fragments.render_join_snippet(a_path(store), "duckdb", soft, bag=bag)
    assert text is not None, list(bag)
    first = text.splitlines()[0]
    assert first.startswith("FROM ") and " JOIN " in first.upper()
    # the same aliases as the rendered path text, keyed per table for FactLang
    assert '"orders"' in first or "orders" in first
    assert "customer_id" in first
    if "parse_filter" in flbridge.capabilities():
        assert "deleted" in first
    else:  # no FactLang filter parser: the live-rows filter is named as NOT applied
        assert "deleted" not in first
        assert "-- live-rows filter for shop.orders not applied" in text
        assert "deleted IS NULL OR NOT deleted" in text


@pytest.mark.factlang
def test_render_join_snippet_composite_and_cast(store: SQLiteStore) -> None:
    """A composite (and cast) hop goes to FactLang as one ``JoinEdge`` (``EdgeRef`` holds one
    column pair only)."""
    if "render_join" not in flbridge.capabilities():
        pytest.skip("factlang capability render_join not yet available")
    from schemaguru.graph import Edge
    from schemaguru.model.joins import JoinRef
    from schemaguru.model.refs import ObjectRef

    base = a_path(store).hops[0].edge
    c = lambda tb, cl: ObjectRef(None, "shop", tb, cl)  # noqa: E731
    join = JoinRef(
        (c("order_items", "order_id"), c("order_items", "line_no")),
        (c("orders", "order_id"), c("orders", "account_id")),
        "string",
    )
    edge = Edge(join, base.value, base.verdict)
    path = GPath((Hop(edge, False),), 1.0, 0.98, False)
    bag = DiagnosticBag()
    text = fragments.render_join_snippet(path, "duckdb", {}, bag=bag)
    assert text is not None, list(bag)
    assert text.count("CAST(") == 4 and " AND " in text


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


def test_snippet_aliases_match_the_path_text(store: SQLiteStore, fake_factlang: dict) -> None:  # type: ignore[type-arg]
    from schemaguru.render.table_block import render_path

    graph = shop_graph(store)
    edge = next(e for e in graph.edges if e.key.startswith("shop.orders.account_id"))
    p = GPath((Hop(edge, False),), 1.0, 1.0, False)
    text = fragments.render_join_snippet(p, "duckdb", {t("orders"): "(((broken"})
    # FactLang keys aliases by table: the same names as the rendered path text
    assert fake_factlang["aliases"] == {
        (None, "shop", "orders"): "orders",
        (None, "shop", "accounts"): "accounts",
    }
    assert fake_factlang["edges"] == [
        (
            (((None, "shop", "orders"), "account_id"),),
            (((None, "shop", "accounts"), "account_id"),),
            None,
        )
    ]
    # an unparseable filter is never dropped silently
    assert text == "FROM <join>\n-- live-rows filter for shop.orders unparseable, omitted"
    # a revisited table is a self-join, which FactLang does not render: no snippet, SG501 info
    revisit = GPath((Hop(edge, False), Hop(edge, True)), 1.0, 1.0, False, revisits=True)
    assert "orders_2" in render_path(revisit)[0]
    bag = DiagnosticBag()
    assert fragments.render_join_snippet(revisit, "duckdb", {}, bag=bag) is None
    assert [d.code for d in bag] == ["SG501"] and "self-join" in next(iter(bag)).message


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


def test_quote_catalog_names() -> None:
    cols = ["id", "isDeleted", "STATUS", "plain_col"]

    def q(text: str, columns: list[str] = cols) -> str:
        out, ambiguous = fragments.quote_catalog_names(text, columns)
        assert ambiguous == []
        return out

    assert q("isDeleted IS NULL OR NOT isDeleted") == '"isDeleted" IS NULL OR NOT "isDeleted"'
    assert q("isdeleted = TRUE") == '"isDeleted" = TRUE'  # unique case-insensitive match
    assert q("status = 'isDeleted'") == "\"STATUS\" = 'isDeleted'"  # literals untouched
    # every unique match is quoted, lower case too: Snowflake/Oracle fold a bare name upward
    assert q("plain_col > 0") == '"plain_col" > 0'
    already = '"isDeleted" IS NULL'
    assert q(already) == already
    assert q("t.isDeleted IS NULL") == 't."isDeleted" IS NULL'  # qualified: the column part
    assert q("isDeletedX IS NULL") == "isDeletedX IS NULL"  # whole identifiers only, no match


def test_quote_snowflake_lower_case_column() -> None:
    """A column stored lower case (created quoted, e.g. by dbt) on Snowflake: bare it would fold
    to ``IS_DELETED``; it is written quoted in its exact case."""
    out, amb = fragments.quote_catalog_names("is_deleted IS NULL", ["ID", "is_deleted"])
    assert out == '"is_deleted" IS NULL' and amb == []


def test_quote_twin_columns_exact_case_wins_else_ambiguous() -> None:
    twins = ["is_deleted", "IS_DELETED"]
    assert fragments.quote_catalog_names("is_deleted = TRUE", twins) == ('"is_deleted" = TRUE', [])
    assert fragments.quote_catalog_names("IS_DELETED = TRUE", twins) == ('"IS_DELETED" = TRUE', [])
    # neither exact: ambiguous, left unquoted and reported
    assert fragments.quote_catalog_names("Is_Deleted = TRUE", twins) == (
        "Is_Deleted = TRUE",
        ["Is_Deleted"],
    )


def test_quote_never_captures_keywords() -> None:
    cols = ["DATE", "NULL", "created_at", "TRUE"]
    out, amb = fragments.quote_catalog_names(
        "created_at > DATE '2024-01-01' AND created_at IS NOT NULL OR flag = TRUE", cols
    )
    assert out == '"created_at" > DATE \'2024-01-01\' AND "created_at" IS NOT NULL OR flag = TRUE'
    assert amb == []


def _factlang_filter_keywords(parser: object) -> frozenset[str]:
    """FactLang's filter keyword set, read directly: a rename fails loudly, never self-compares."""
    try:
        return frozenset(parser._FILTER_KEYWORDS)  # type: ignore[attr-defined]
    except AttributeError:
        pytest.fail(
            "factlang.syntax.parser._FILTER_KEYWORDS is gone: re-pin "
            "schemaguru.render.fragments.FILTER_KEYWORDS against FactLang's filter keywords"
        )


def test_filter_keywords_match_factlang() -> None:
    import factlang.syntax.parser as parser  # a hard dependency: never skipped

    assert _factlang_filter_keywords(parser) == fragments.FILTER_KEYWORDS


def test_filter_keywords_check_fails_when_factlang_renames_it() -> None:
    """Negative control: a parser module without the attribute fails, it does not pass."""
    import types

    renamed = types.ModuleType("factlang.syntax.parser")
    with pytest.raises(pytest.fail.Exception, match="_FILTER_KEYWORDS is gone"):
        _factlang_filter_keywords(renamed)


def test_ambiguous_filter_column_is_reported(store: SQLiteStore, fake_factlang: dict) -> None:  # type: ignore[type-arg]
    class Cat:
        def columns(self, ref: object) -> dict[str, str]:
            return {"IsDeleted": "boolean", "ISDELETED": "boolean", "customer_id": "integer"}

    bag = DiagnosticBag()
    soft = {t("orders"): "isdeleted IS NULL"}
    assert fragments.render_join_snippet(a_path(store), "snowflake", soft, bag=bag, catalog=Cat())
    (d,) = [d for d in bag if d.code == "SG506"]
    assert d.severity is Severity.WARNING and "isdeleted" in d.message


@pytest.mark.factlang
@pytest.mark.parametrize("dialect", ["postgres", "snowflake"])
def test_live_rows_filter_on_a_mixed_case_column(tmp_path: Path, dialect: str) -> None:
    """``isDeleted`` (mixed case, as the catalog returns it) is checked against the catalog by
    FactLang and rendered quoted; unquoted, Postgres would fold it to ``isdeleted``."""
    if not {"render_join", "parse_filter"} <= flbridge.capabilities():
        pytest.skip("factlang capability render_join/parse_filter not yet available")
    from schemaguru.graph import Edge
    from schemaguru.measure.catalog_bridge import StoreCatalog
    from schemaguru.model.joins import JoinRef
    from schemaguru.model.refs import ObjectRef
    from schemaguru.model.structure import Column, Schema, Table

    o, c = ObjectRef(None, "app", "Orders"), ObjectRef(None, "app", "customers")
    with SQLiteStore(tmp_path / "s.db") as s:
        s.upsert_schemas([Schema(None, "app")])
        s.upsert_tables([Table(o, "table"), Table(c, "table")])
        s.upsert_columns(o, [
            Column(ObjectRef(None, "app", "Orders", "customer_id"), 1, "INTEGER", "integer"),
            Column(ObjectRef(None, "app", "Orders", "isDeleted"), 2, "BOOLEAN", "boolean"),
        ])  # fmt: skip
        s.upsert_columns(c, [
            Column(ObjectRef(None, "app", "customers", "customer_id"), 1, "INTEGER", "integer")
        ])  # fmt: skip
        join = JoinRef(
            (ObjectRef(None, "app", "Orders", "customer_id"),),
            (ObjectRef(None, "app", "customers", "customer_id"),),
        )
        path = GPath((Hop(Edge(join, {}, None, measured=False), False),), 1.0, None, True)
        bag = DiagnosticBag()
        soft = {o: "isDeleted IS NULL OR NOT isDeleted"}
        text = fragments.render_join_snippet(path, dialect, soft, bag=bag, catalog=StoreCatalog(s))
        assert text is not None, list(bag)
        assert '."isDeleted" IS NULL' in text and ".isDeleted" not in text, text
        # a column the catalog does not have: FactLang's check refuses the filter, never silently
        bad = {o: "is_gone IS NULL"}
        text2 = fragments.render_join_snippet(path, dialect, bad, bag=bag, catalog=StoreCatalog(s))
        assert (
            text2 is not None
            and "unparseable, omitted" in text2
            and "is_gone" not in text2.split("\n")[0]
        )
