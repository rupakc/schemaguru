from __future__ import annotations

from schemaguru.catalog.filters import ScopeFilter
from schemaguru.config import CrawlConfig
from schemaguru.model.refs import ObjectRef


def R(s: str, t: str, c: str | None = None) -> ObjectRef:
    return ObjectRef(c, s, t)


def test_empty_include_allows_all_and_exclude_wins() -> None:
    f = ScopeFilter((), ("sys.*",))
    assert f.allows(R("shop", "orders"))
    assert not f.allows(R("sys", "tables"))
    g = ScopeFilter(("shop.*",), ("shop.orders",))
    assert g.allows(R("shop", "customers"))
    assert not g.allows(R("shop", "orders"))
    assert not g.allows(R("other", "customers"))


def test_matches_three_part_and_is_case_sensitive() -> None:
    f = ScopeFilter(("main.shop.*",), ())
    assert f.allows(R("shop", "orders", "main"))
    assert not f.allows(R("shop", "orders", "other"))
    assert not f.allows(R("shop", "orders"))  # no catalog -> no three-part name
    assert not ScopeFilter(("Shop.*",), ()).allows(R("shop", "orders"))
    # a column ref is judged by its table
    assert f.allows(ObjectRef("main", "shop", "orders", "id"))


def test_scope_filter_defaults_exclude_staging() -> None:
    cfg = CrawlConfig()
    f = ScopeFilter(cfg.include, cfg.exclude)
    assert f.allows(R("shop", "orders"))
    assert not f.allows(R("shop", "orders_staging_2026"))
    assert not f.allows(R("shop", "tmp_orders"))
    assert not f.allows(R("shop", "orders_tmp"))
    assert not f.allows(R("information_schema", "tables"))
    assert not f.allows(R("pg_catalog", "pg_class"))
    assert not f.allows(R("sys", "objects"))


def test_excludes_schema_only_for_schema_wide_patterns() -> None:
    f = ScopeFilter(("shop.orders",), ("sys.*", "*.*tmp*"))
    assert f.excludes_schema(None, "sys")
    assert not f.excludes_schema(None, "shop")  # include is never used to skip a schema
    assert not ScopeFilter((), ()).excludes_schema(None, "shop")
