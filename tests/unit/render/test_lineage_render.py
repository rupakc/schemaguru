"""Lineage lines per profile (0.2.0 spec 4.6): parsed or declared, never measured or verified."""

from __future__ import annotations

from pathlib import Path

import pytest

from schemaguru.model.detail import DetailLevel
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.kinds import FactKind
from schemaguru.model.lineage import (
    DBT_SOURCE,
    VIEW_SOURCE,
    column_lineage_value,
    table_lineage_value,
)
from schemaguru.render import collect, get_profile
from schemaguru.store.sqlite import SQLiteStore
from tests.golden.shop_knowledge import CONFIG, NOW, c, shop_facts, shop_store, t

_P, _S = Provenance, Status
ORDERS = "shop.orders"
PAYMENTS = "shop.payments"


def tl(
    upstream: list[str],
    unresolved: list[str] = (),  # type: ignore[assignment]
    prov: Provenance = _P.CATALOG,
    source: str = VIEW_SOURCE,
    table: str = "customers",
) -> Fact:
    return Fact.make(
        FactKind.LINEAGE,
        t(table),
        table_lineage_value(upstream, unresolved),
        prov,
        _S.UNVERIFIED,
        source=source,
    )


def cl(
    inputs: list[str],
    transform: str = "aggregate",
    column: str = "customer_id",
    table: str = "customers",
    prov: Provenance = _P.CATALOG,
    source: str = VIEW_SOURCE,
) -> Fact:
    return Fact.make(
        FactKind.COLUMN_LINEAGE,
        c(table, column),
        column_lineage_value(inputs, transform),
        prov,
        _S.UNVERIFIED,
        source=source,
    )


@pytest.fixture
def mk(tmp_path: Path):  # type: ignore[no-untyped-def]
    stores: list[object] = []

    def make(*extra: Fact) -> SQLiteStore:
        cm = shop_store(tmp_path / f"s{len(stores)}.db", [*shop_facts(), *extra])
        s = cm.__enter__()
        stores.append(cm)
        return s

    yield make
    for cm in stores:
        cm.__exit__(None, None, None)  # type: ignore[attr-defined]


def render(
    s: SQLiteStore,
    profile: str = "agent",
    level: DetailLevel = DetailLevel.STANDARD,
    table: str = "customers",
) -> str:
    return get_profile(profile).render_table(collect(s, t(table), NOW, CONFIG), level)


def test_agent_built_from_line(mk) -> None:  # type: ignore[no-untyped-def]
    s = mk(tl([ORDERS, PAYMENTS], source=DBT_SOURCE + ":model.x", prov=_P.DECLARED))
    lines = render(s).splitlines()
    i = next(n for n, ln in enumerate(lines) if "Built from:" in ln)
    assert lines[i] == (
        "  Built from: shop.orders, shop.payments · dbt:manifest (declared) · unverified"
    )
    warns = [n for n, ln in enumerate(lines) if "⚠" in ln or "WARN" in ln]
    assert all(n < i for n in warns)
    answers = [n for n, ln in enumerate(lines) if ln.startswith("  Answers")]
    assert all(i < n for n in answers)


def test_cap_and_more(mk) -> None:  # type: ignore[no-untyped-def]
    ups = ["shop.a", "shop.b", "shop.c", "shop.d", "shop.e"]
    s = mk(tl(ups))
    assert "Built from: shop.a, shop.b, shop.c (+2 more) · view_definition (parsed)" in render(s)
    assert "shop.d" not in render(s)


def test_unresolved_marked_not_in_catalog(mk) -> None:  # type: ignore[no-untyped-def]
    s = mk(tl([ORDERS], ["ext.raw"]))
    assert "Built from: shop.orders, ext.raw (not in catalog) ·" in render(s)


def test_merged_sources_one_line(mk) -> None:  # type: ignore[no-untyped-def]
    s = mk(
        tl([ORDERS, PAYMENTS], prov=_P.DECLARED, source=DBT_SOURCE + ":model.x"),
        tl([ORDERS, PAYMENTS]),
    )
    text = render(s)
    assert text.count("Built from:") == 1
    assert "dbt:manifest (declared), view_definition (parsed) · unverified" in text
    assert "lineage dbt:manifest, view_definition (unverified)" in text


def test_column_arrow_notes(mk) -> None:  # type: ignore[no-untyped-def]
    s = mk(cl(["shop.orders.amount", "shop.refunds.amount"]), cl(["shop.payments.x"], "copy"))
    text = render(s)
    assert "← shop.orders.amount, shop.refunds.amount (aggregate); shop.payments.x (copy)" in text
    assert "← " not in render(s, level=DetailLevel.LITE)


def test_planner_table_line_only(mk) -> None:  # type: ignore[no-untyped-def]
    s = mk(tl([ORDERS]), cl(["shop.orders.amount"]))
    text = render(s, "planner")
    assert "Built from: shop.orders · view_definition (parsed) · unverified" in text
    assert "←" not in text


def test_verifier_keyed_lines(mk) -> None:  # type: ignore[no-untyped-def]
    s = mk(
        tl(["shop.a", "shop.b", "shop.c", "shop.d"]),
        cl(["shop.orders.amount"], "copy"),
    )
    text = render(s, "verifier")
    assert "LINEAGE shop.customers <- shop.a, shop.b, shop.c, shop.d" in text.splitlines()
    assert (
        "COLUMN_LINEAGE shop.customers.customer_id <- shop.orders.amount copy" in text.splitlines()
    )
    lite = render(s, "verifier", DetailLevel.LITE)
    assert "LINEAGE shop.customers" in lite
    assert "COLUMN_LINEAGE" not in lite


def test_router_unchanged(mk) -> None:  # type: ignore[no-untyped-def]
    s0, s1 = mk(), mk(tl([ORDERS]), cl(["shop.orders.amount"]))
    assert render(s1, "router") == render(s0, "router")


def test_no_lineage_renders_as_010(mk) -> None:  # type: ignore[no-untyped-def]
    s0 = mk()
    s1 = mk(tl([ORDERS], table="orders"))  # lineage on another table only
    for profile in ("agent", "planner", "verifier", "router"):
        for lv in DetailLevel:
            assert render(s1, profile, lv) == render(s0, profile, lv)
    assert "lineage" not in render(s0)


def test_human_replaces_and_ignore_hides(mk) -> None:  # type: ignore[no-untyped-def]
    human = tl([PAYMENTS], prov=_P.HUMAN, source="overlay:/home/x/schemaguru.yaml:3")
    text = render(mk(tl([ORDERS]), human))
    assert "Built from: shop.payments · overlay (declared) · unverified" in text
    assert "shop.orders ·" not in text and "/home/x" not in text
    ign = Fact.make(
        FactKind.LINEAGE,
        t("customers"),
        table_lineage_value(None, ignore=True),
        _P.HUMAN,
        _S.UNVERIFIED,
        source="overlay:x",
    )
    assert "Built from" not in render(mk(tl([ORDERS]), ign))


def test_lineage_lines_swept(mk) -> None:  # type: ignore[no-untyped-def]
    s = mk(tl([ORDERS], ["maria.garcia@example.com"]))
    text = render(s)
    assert "maria.garcia@example.com" not in text
    assert "<email> (not in catalog)" in text


def test_never_says_measured(mk) -> None:  # type: ignore[no-untyped-def]
    s = mk(tl([ORDERS]), cl(["shop.orders.amount"]))
    seen = 0
    for profile in ("agent", "planner", "verifier"):
        for ln in render(s, profile).splitlines():
            frags = (
                [ln.strip()] if ln.strip().startswith(("Built from", "LINEAGE", "COLUMN_")) else []
            )
            frags += [ln.split("← ")[1]] if "← " in ln else []
            frags += [ln.split(" · lineage ")[1]] if " · lineage " in ln else []
            for frag in frags:
                seen += 1
                assert not any(w in frag.lower() for w in ("measured", "verified ")), frag
    assert seen >= 5
