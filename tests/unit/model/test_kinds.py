import pytest

from schemaguru.model.kinds import FactKind, KindSpec, get_kind, register_fact_kind

SPEC_KINDS = [
    "DESCRIPTION",
    "USE_CASES",
    "ENTITY_TYPE",
    "PRIMARY_KEY",
    "JOIN",
    "VALUES",
    "NULL_RATE",
    "ROW_COUNT",
    "DISTINCT",
    "FRESHNESS",
    "TYPE_NOTE",
    "SOFT_DELETE",
    "PII",
    "PITFALL",
    "GLOSSARY",
    "METRIC",
    "NEVER_QUERY",
    "WAYPOINT_RULE",
]


def test_builtins_exactly_spec_table() -> None:
    for name in SPEC_KINDS:
        assert get_kind(name) is getattr(FactKind, name)
        assert get_kind(name).name == name


def test_meaning_and_expectation_sets() -> None:
    meaning = {n for n in SPEC_KINDS if get_kind(n).meaning}
    expect = {n for n in SPEC_KINDS if get_kind(n).expectation}
    assert meaning == {"DESCRIPTION", "USE_CASES", "ENTITY_TYPE", "PITFALL", "GLOSSARY"}
    assert expect == {"VALUES", "JOIN", "PRIMARY_KEY", "SOFT_DELETE"}


def test_identity_fields() -> None:
    assert FactKind.JOIN.identity_fields == ()
    assert FactKind.FRESHNESS.identity_fields == ("column",)
    assert FactKind.VALUES.identity_fields == ("case_sensitive", "split")
    assert FactKind.DESCRIPTION.identity_fields is None


def test_register_and_replace() -> None:
    spec = KindSpec("SHOP_TEST_KIND", "table", False, False)
    register_fact_kind(spec)
    assert get_kind("SHOP_TEST_KIND") is spec
    with pytest.raises(ValueError):
        register_fact_kind(spec)
    other = KindSpec("SHOP_TEST_KIND", "column", False, False)
    register_fact_kind(other, replace=True)
    assert get_kind("SHOP_TEST_KIND") is other


def test_unknown_kind() -> None:
    with pytest.raises(KeyError):
        get_kind("NOPE")
