from __future__ import annotations

import pytest

from schemaguru.config import SamplingConfig
from schemaguru.errors import ConfigError
from schemaguru.model.refs import ObjectRef
from schemaguru.pii import ValuePolicy


def mode(ref: str, cfg: SamplingConfig) -> str:
    return ValuePolicy.for_table(ObjectRef.parse(ref), cfg)


def test_default_is_masked() -> None:
    assert mode("shop.orders", SamplingConfig()) == "masked"
    assert mode("shop.orders", SamplingConfig(llm_values="none")) == "none"


def test_policy_glob_specificity() -> None:
    cfg = SamplingConfig(
        per_glob={"*": "none", "shop.*": "raw", "shop.cust*": "masked", "shop.customers": "none"}
    )
    assert mode("shop.customers", cfg) == "none"  # literal beats every wildcard
    assert mode("shop.customer_notes", cfg) == "masked"  # longer literal prefix beats shop.*
    assert mode("shop.orders", cfg) == "raw"
    assert mode("other.orders", cfg) == "none"


def test_tie_broken_by_fewer_wildcards() -> None:
    cfg = SamplingConfig(per_glob={"shop.o*": "raw", "shop.o*s*": "none"})
    assert mode("shop.orders", cfg) == "raw"


def test_catalog_names_match_schema_table_and_bare_forms() -> None:
    cfg = SamplingConfig(per_glob={"shop.orders": "raw", "payments": "none"})
    assert mode("wh.shop.orders", cfg) == "raw"
    assert mode("wh.shop.payments", cfg) == "none"
    full = SamplingConfig(per_glob={"wh.shop.*": "none", "shop.*": "raw"})
    assert mode("wh.shop.orders", full) == "none"  # longer literal prefix wins


def test_no_match_falls_back_to_default() -> None:
    assert mode("shop.orders", SamplingConfig(per_glob={"x.*": "raw"})) == "masked"


def test_invalid_mode_rejected() -> None:
    with pytest.raises(ConfigError):
        mode("shop.orders", SamplingConfig(per_glob={"shop.*": "bogus"}))
