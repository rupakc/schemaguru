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


def test_narrower_glob_wins_over_the_glob_that_contains_it() -> None:
    cfg = SamplingConfig(per_glob={"shop.o*": "raw", "shop.o*s*": "none"})
    assert mode("shop.orders", cfg) == "none"  # the narrower glob wins


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


def test_incomparable_globs_resolve_to_most_restrictive() -> None:
    cfg = SamplingConfig(per_glob={"shop.*": "raw", "*.customers": "none"})
    assert mode("db.shop.customers", cfg) == "none"
    cfg = SamplingConfig(per_glob={"shop.*": "none", "*.customers": "raw"})
    assert mode("db.shop.customers", cfg) == "none"
    cfg = SamplingConfig(per_glob={"shop.*": "masked", "*.customers": "raw"})
    assert mode("db.shop.customers", cfg) == "masked"


def test_bare_name_glob_does_not_widen_raw() -> None:
    cfg = SamplingConfig(per_glob={"customers": "raw", "shop.*": "none"})
    assert mode("db.shop.customers", cfg) == "none"
    # a glob with a dot never matches the bare table name
    assert mode("shop.customers", SamplingConfig(per_glob={"x.customers": "raw"})) == "masked"
    assert mode("shop.customers", SamplingConfig(per_glob={"customers": "raw"})) == "raw"


def test_unrelated_globs_never_widen_access() -> None:
    cfg = SamplingConfig(per_glob={"shop.??": "none", "shop.*s": "raw"})
    assert mode("shop.os", cfg) == "none"
    cfg = SamplingConfig(per_glob={"shop.??": "raw", "shop.*s": "none"})
    assert mode("shop.os", cfg) == "none"


def test_glob_containment_is_real() -> None:
    from schemaguru.pii.policy import _contains

    assert _contains("shop.cust*", "shop.*")
    assert not _contains("shop.*", "shop.cust*")
    assert _contains("shop.??", "shop.*")
    assert not _contains("shop.??", "shop.*s")
    assert not _contains("shop.*s", "shop.??")
    assert _contains("shop.o*s*", "shop.o*")
    assert _contains("shop.[ab]*", "shop.*")
    assert not _contains("shop.*", "shop.[ab]*")
    assert _contains("my-tbl", "my-*")
    assert _contains("a*b", "a*")
    assert not _contains("*a", "a*")


def test_class_and_nested_narrowing() -> None:
    cfg = SamplingConfig(per_glob={"shop.*": "raw", "shop.[co]*": "masked", "shop.orders": "none"})
    assert mode("shop.orders", cfg) == "none"
    assert mode("shop.customers", cfg) == "masked"
    assert mode("shop.items", cfg) == "raw"


# --- final fix wave: globs match case-insensitively (fail closed), unmatched globs linted ---


def test_glob_matches_case_insensitively() -> None:
    """``hr.*: none`` must cover Snowflake's upper-cased ``DB.HR.EMPLOYEES``: a case-sensitive
    match failed OPEN (the table fell back to the default, or to a wider glob)."""
    cfg = SamplingConfig(llm_values="raw", per_glob={"hr.*": "none"})
    for ref in (
        ObjectRef("DB", "HR", "EMPLOYEES"),
        ObjectRef(None, "Hr", "Employees"),
        ObjectRef(None, "hr", "employees"),
    ):
        assert ValuePolicy.for_table(ref, cfg) == "none", ref
    upper = SamplingConfig(per_glob={"DB.HR.*": "raw", "*.PAYMENTS": "none"})
    assert ValuePolicy.for_table(ObjectRef("db", "hr", "x"), upper) == "raw"
    assert ValuePolicy.for_table(ObjectRef(None, "shop", "payments"), upper) == "none"
    # negative control: a different name still does not match
    assert ValuePolicy.for_table(ObjectRef("DB", "FINANCE", "X"), cfg) == "raw"


def test_case_variants_of_one_glob_resolve_to_the_most_restrictive() -> None:
    cfg = SamplingConfig(per_glob={"shop.*": "raw", "SHOP.*": "none"})
    assert ValuePolicy.for_table(ObjectRef(None, "shop", "orders"), cfg) == "none"


def test_lint_per_glob_flags_globs_matching_no_table() -> None:
    from schemaguru.diagnostics import Severity
    from schemaguru.pii.policy import lint_per_glob

    tables = [ObjectRef("DB", "SHOP", "ORDERS"), ObjectRef("DB", "HR", "EMPLOYEES")]
    cfg = SamplingConfig(per_glob={"hr.*": "none", "shop.orders": "raw", "hrr.*": "none"})
    diags = lint_per_glob(cfg, tables)
    assert [d.code for d in diags] == ["SG507"]
    assert diags[0].severity is Severity.WARNING and "hrr.*" in diags[0].message
    assert lint_per_glob(SamplingConfig(), tables) == []
