"""Profiles: the built-in set, the open registry, and each role's shape (spec 13.2)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from schemaguru.model.detail import DetailLevel
from schemaguru.render import TableKnowledge, collect, get_profile, profile_names, register_profile
from schemaguru.store.sqlite import SQLiteStore
from tests.golden.shop_knowledge import CONFIG, NOW, shop_store, t

VERIFIER_KEYS = re.compile(
    r"^(TABLE|NEVER_QUERY|WARN|PITFALL|ROWS|PK|SOFT_DELETE|COLUMNS|JOIN|VALUES|TYPE|PII|"
    r"NULLS|DISTINCT|FRESH|NOTE|MORE) "
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    with shop_store(tmp_path / "shop.db") as s:
        yield s


def k(store: SQLiteStore, name: str) -> TableKnowledge:
    return collect(store, t(name), NOW, CONFIG)


def test_builtin_profiles() -> None:
    assert {"agent", "planner", "verifier", "router"} <= set(profile_names())
    with pytest.raises(KeyError, match="unknown profile"):
        get_profile("nope")


def test_register_profile_is_open_and_refuses_silent_replacement(store: SQLiteStore) -> None:
    class Shout:
        def render_table(self, eff: TableKnowledge, level: DetailLevel) -> str:
            return eff.ref.canonical().upper()

    register_profile("test-shout", Shout())
    try:
        assert get_profile("test-shout").render_table(k(store, "orders"), DetailLevel.FULL) == (
            "SHOP.ORDERS"
        )
        with pytest.raises(ValueError, match="already registered"):
            register_profile("test-shout", Shout())
        register_profile("test-shout", Shout(), replace=True)
    finally:
        from schemaguru.render import profiles

        profiles._PROFILES.pop("test-shout", None)


def test_verifier_lines_are_keyed(store: SQLiteStore) -> None:
    v = get_profile("verifier")
    for name in ("orders", "customers", "events", "broken_child", "order_items"):
        for level in DetailLevel:
            for line in v.render_table(k(store, name), level).splitlines():
                assert VERIFIER_KEYS.match(line), (name, level, line)
    text = v.render_table(k(store, "orders"), DetailLevel.FULL)
    assert "PK order_id unique measured 2026-11-20" in text
    assert "VALUES status declared='paid','refunded','cancelled'" in text
    assert "JOIN customer_id -> shop.customers.customer_id N:1 match=0.9804" in text
    assert "SOFT_DELETE (deleted IS NULL OR NOT deleted) verified 2026-11-19" in text
    composite = v.render_table(k(store, "order_items"), DetailLevel.FULL)
    assert "PK order_id, line_no unique measured 2026-11-20" in composite


def test_router_is_one_line(store: SQLiteStore) -> None:
    r = get_profile("router")
    for name in ("orders", "customers", "events", "broken_child", "empty_t"):
        for level in DetailLevel:
            assert len(r.render_table(k(store, name), level).splitlines()) == 1
    assert r.render_table(k(store, "orders"), DetailLevel.FULL) == (
        "shop.orders — One row per order. (draft)"
    )
    assert "⚠ NEVER QUERY" in r.render_table(k(store, "broken_child"), DetailLevel.FULL)


def test_planner_has_use_cases_and_keys_but_few_columns(store: SQLiteStore) -> None:
    p = get_profile("planner")
    text = p.render_table(k(store, "orders"), DetailLevel.FULL)
    assert "Answers (draft): revenue; order volume; basket size" in text
    assert "Keys: PK order_id" in text
    assert "customer_id → shop.customers.customer_id" in text
    assert "created_at" not in text.split("Keys:")[1]  # not a key column
    assert len(text) < len(get_profile("agent").render_table(k(store, "orders"), DetailLevel.FULL))
    # warnings still lead
    lines = text.splitlines()
    assert lines[1].startswith("  ⚠")
