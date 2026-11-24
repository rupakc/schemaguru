from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from schemaguru.config import Config
from schemaguru.diagnostics import Diagnostic, Severity
from schemaguru.measure.backend import MeasureResult
from schemaguru.measure.convert import to_facts
from schemaguru.measure.requests import (
    Freshness,
    KeyOverlapReq,
    PatternRate,
    ProfileColumns,
    Request,
    RowCount,
    SampleChoice,
    TopValues,
    Uniqueness,
)
from schemaguru.model.facts import Fact, Provenance, Status
from schemaguru.model.joins import JOIN_VALUE_KEYS, JoinRef
from schemaguru.model.refs import ObjectRef
from tests.fixtures.shop import ShopFacts

NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
CFG = Config()
ORD = ObjectRef(None, "shop", "orders")
JOIN = JoinRef(
    (ObjectRef(None, "shop", "orders", "customer_id"),),
    (ObjectRef(None, "shop", "customers", "customer_id"),),
)
SYS = SampleChoice("system", Decimal("0.01"), 7)
KEY = SampleChoice("keyhash", Decimal("0.1"), 7, ("customer_id",))


def _facts(
    req: Request,
    payload: dict[str, Any] | None,
    error: Diagnostic | None = None,
    config: Config = CFG,
) -> list[Fact]:
    return to_facts(
        MeasureResult(req, payload, None, error), observed_at=NOW, snapshot_id="s1", config=config
    )


def _by(facts: list[Fact], kind: str, column: str | None = None) -> Fact:
    hits = [
        f
        for f in facts
        if f.kind.name == kind and (column is None or getattr(f.subject, "column", None) == column)
    ]
    assert len(hits) == 1, (kind, column, facts)
    return hits[0]


def test_exact_profile_observed() -> None:
    req = ProfileColumns(ORD, ("deleted", "status"))
    facts = _facts(
        req,
        {
            "row_count": 10_000,
            "columns": {
                "deleted": {"non_null": 300, "distinct_keys": 1},
                "status": {"non_null": Decimal(10_000), "distinct_keys": 4},
            },
        },
    )
    for f in facts:
        assert f.status is Status.OBSERVED and f.method == "exact"
        assert f.provenance is Provenance.MEASURED and f.snapshot_id == "s1"
        assert f.observed_at == NOW
    assert _by(facts, "ROW_COUNT").value["rows"] == 10_000
    rate = _by(facts, "NULL_RATE", "deleted").value
    assert rate["rate"] == pytest.approx(0.97) and rate["dead"] is False
    status_rate = _by(facts, "NULL_RATE", "status").value["rate"]
    assert status_rate == 0.0 and isinstance(status_rate, float)
    d = _by(facts, "DISTINCT", "status").value["distinct_keys"]
    assert d == 4 and isinstance(d, int)


def test_empty_table_unmeasurable_not_zero() -> None:
    facts = _facts(
        ProfileColumns(ORD, ("v",)),
        {"row_count": 0, "columns": {"v": {"non_null": 0, "distinct_keys": 0}}},
    )
    assert _by(facts, "ROW_COUNT").value["rows"] == 0
    nr = _by(facts, "NULL_RATE", "v")
    assert nr.status is Status.UNMEASURABLE and nr.value["rate"] is None
    assert _by(facts, "DISTINCT", "v").status is Status.UNMEASURABLE


def test_all_null_column_is_dead() -> None:
    facts = _facts(
        ProfileColumns(ORD, ("c",)),
        {"row_count": 100, "columns": {"c": {"non_null": 0, "distinct_keys": 0}}},
    )
    nr = _by(facts, "NULL_RATE", "c")
    assert nr.status is Status.OBSERVED and nr.value["rate"] == 1.0 and nr.value["dead"] is True


def test_missing_metric_is_unknown_not_zero() -> None:
    facts = _facts(ProfileColumns(ORD, ("c",)), {"row_count": 100, "columns": {}})
    nr = _by(facts, "NULL_RATE", "c")
    assert nr.status is Status.UNMEASURABLE and nr.value["rate"] is None


def test_sampled_never_verified() -> None:
    req = ProfileColumns(ORD, ("deleted",), sample=SYS)
    facts = _facts(
        req,
        {
            "row_count": 1000,
            "sampled_rows": 1000,
            "columns": {"deleted": {"non_null": 30, "distinct_keys": 1}},
        },
    )
    assert facts and all(f.status is Status.ESTIMATED for f in facts)
    assert all(f.method == "sampled:system:0.01:7" for f in facts)
    assert not any(f.kind.name == "ROW_COUNT" for f in facts)  # row counts are never sampled
    nr = _by(facts, "NULL_RATE", "deleted").value
    est = nr["estimate"]
    assert est["value"] == pytest.approx(0.97) and est["lower_bound"] is False
    assert est["low"] < 0.97 < est["high"] and nr["sampled_rows"] == 1000
    # the block-sample interval is widened by the design effect (2.0)
    from schemaguru.measure.estimates import wilson

    assert (est["low"], est["high"]) == pytest.approx(wilson(970, 1000, design_effect=2.0))
    # a sampled fact can never be built as VERIFIED
    with pytest.raises(ValueError):
        Fact.make(
            "NULL_RATE",
            ObjectRef(None, "shop", "orders", "deleted"),
            nr,
            Provenance.MEASURED,
            Status.VERIFIED,
            method=SYS.method_string(),
        )


def test_distinct_from_sample_is_lower_bound() -> None:
    req = ProfileColumns(ORD, ("status",), sample=SYS)
    facts = _facts(
        req,
        {
            "row_count": 500,
            "sampled_rows": 500,
            "columns": {"status": {"non_null": 500, "distinct_keys": 3}},
        },
    )
    d = _by(facts, "DISTINCT", "status")
    assert d.status is Status.ESTIMATED
    assert d.value["estimate"]["lower_bound"] is True and d.value["estimate"]["value"] == 3
    assert d.value["estimate"]["high"] is None
    # an all-null column in a sample is not proven dead
    facts = _facts(
        req,
        {
            "row_count": 500,
            "sampled_rows": 500,
            "columns": {"status": {"non_null": 0, "distinct_keys": 0}},
        },
    )
    assert _by(facts, "NULL_RATE", "status").value["dead"] is None


def test_join_exact_matches_shop(shop_facts: ShopFacts) -> None:
    j = shop_facts.joins["orders->customers"]
    facts = _facts(
        KeyOverlapReq(JOIN),
        {
            "distinct_left": j.distinct_left,
            "matched": j.matched_keys,
            "distinct_right": j.right_distinct,
            "left_non_null": j.left_non_null,
            "matched_left_rows": j.matched_rows,
            "max_left_per_key": 25,
            "max_right_per_key": 1,
        },
    )
    f = _by(facts, "JOIN")
    assert f.status is Status.OBSERVED and f.subject == JOIN
    assert f.value["match_rate"] == pytest.approx(j.key_match_rate)
    assert f.value["row_match_rate"] == pytest.approx(j.row_match_rate)
    assert f.value["coverage"] == pytest.approx(j.coverage)
    assert f.value["fan_in"] == pytest.approx(j.left_non_null / j.distinct_left)
    assert f.value["cardinality"] == "N:1" and f.value["verdict"] == "OK"
    assert f.value["cast_required"] is False


def test_join_fact_has_all_keys_none_when_missing() -> None:
    f = _by(
        _facts(KeyOverlapReq(JOIN, multiplicity=False), {"distinct_left": 10, "matched": 0}), "JOIN"
    )
    assert set(JOIN_VALUE_KEYS) <= set(f.value)
    assert f.value["match_rate"] == 0.0 and f.value["verdict"] == "BROKEN"
    for k in (
        "coverage",
        "row_match_rate",
        "fan_in",
        "max_left_per_key",
        "cardinality",
        "distinct_right",
        "left_non_null",
    ):
        assert f.value[k] is None, k
    empty = _by(_facts(KeyOverlapReq(JOIN), {"distinct_left": 0, "matched": 0}), "JOIN")
    assert empty.status is Status.UNMEASURABLE
    assert all(empty.value[k] is None for k in JOIN_VALUE_KEYS)


def test_join_cardinalities_and_cast() -> None:
    cast = JoinRef(JOIN.left, JOIN.right, cast_to="string")

    def card(ml: int, mr: int) -> Any:
        p = {
            "distinct_left": 5,
            "matched": 5,
            "distinct_right": 5,
            "left_non_null": 5,
            "matched_left_rows": 5,
            "max_left_per_key": ml,
            "max_right_per_key": mr,
        }
        return _by(_facts(KeyOverlapReq(cast), p), "JOIN").value

    assert card(1, 1)["cardinality"] == "1:1"
    assert card(3, 1)["cardinality"] == "N:1"
    assert card(1, 3)["cardinality"] == "1:N"
    assert card(3, 3)["cardinality"] == "N:M"
    assert card(1, 1)["cast_required"] is True


def test_join_keyhash_sample_scales_counts() -> None:
    p = {
        "distinct_left": 100,
        "matched": 98,
        "distinct_right": 120,
        "left_non_null": 1000,
        "matched_left_rows": 990,
        "max_left_per_key": 30,
        "max_right_per_key": 1,
        "sampled_left_rows": 1000,
        "sampled_right_rows": 120,
    }
    f = _by(_facts(KeyOverlapReq(JOIN, sample=KEY), p), "JOIN")
    assert f.status is Status.ESTIMATED and f.method == "sampled:keyhash:0.1:7"
    assert f.value["match_rate"] == pytest.approx(0.98)
    assert f.value["distinct_left"] == 1000 and f.value["matched"] == 980
    assert f.value["estimate"]["method"] == "wilson95"
    assert f.value["estimate"]["low"] < 0.98 < f.value["estimate"]["high"]
    assert f.value["estimates"]["max_left_per_key"]["lower_bound"] is True
    assert f.value["sampled_left_rows"] == 1000 and f.value["verdict"] == "OK"


def test_error_fact_per_subject() -> None:
    err = Diagnostic("SG301", Severity.ERROR, "over budget")
    facts = _facts(ProfileColumns(ORD, ("a", "b")), None, err)
    assert {f.kind.name for f in facts} == {"ROW_COUNT", "NULL_RATE", "DISTINCT"}
    assert len(facts) == 5
    for f in facts:
        assert f.status is Status.ERROR and f.value["reason"] == "over_budget"
        assert f.value["code"] == "SG301"
    fr = _facts(
        Freshness(ObjectRef(None, "shop", "orders", "created_at")),
        None,
        Diagnostic("SG303", Severity.ERROR, "boom"),
    )
    assert fr[0].value["reason"] == "probe_error" and fr[0].value["column"] == "created_at"


def test_enum_discovery_only_below_threshold() -> None:
    col = ObjectRef(None, "shop", "orders", "status")
    vals = [{"value": "paid", "count": 5000}, {"value": "PAID", "count": 412}]
    f = _by(_facts(TopValues(col, 20), {"non_null": 5412, "distinct": 2, "values": vals}), "VALUES")
    assert f.status is Status.OBSERVED and f.value["observed"] == vals
    assert f.value["case_sensitive"] is True and f.value["split"] is None
    # above enum_max_distinct the list is a truncated top-k, not an enum: no VALUES fact
    assert _facts(TopValues(col, 20), {"non_null": 9, "distinct": 21, "values": vals}) == []
    sampled = _by(
        _facts(
            TopValues(col, 20, sample=SYS),
            {"non_null": 50, "distinct": 2, "values": vals, "sampled_rows": 50},
        ),
        "VALUES",
    )
    assert sampled.status is Status.ESTIMATED and sampled.value["seen_in_sample"] is True


def test_uniqueness_exact_and_sampled_refutation() -> None:
    req = Uniqueness(ORD, ("order_id",))
    ok = _by(
        _facts(
            req,
            {
                "row_count": 10,
                "non_null": 10,
                "distinct_keys": 10,
                "max_rows_per_key": 1,
                "duplicate_keys": 0,
            },
        ),
        "PRIMARY_KEY",
    )
    assert ok.status is Status.OBSERVED and ok.value["unique"] is True
    assert ok.value["columns"] == ["order_id"]
    samp = Uniqueness(
        ORD, ("order_id",), sample=SampleChoice("keyhash", Decimal("0.1"), 1, ("order_id",))
    )
    dup = _by(
        _facts(
            samp,
            {
                "row_count": 10,
                "non_null": 10,
                "distinct_keys": 9,
                "max_rows_per_key": 2,
                "duplicate_keys": 1,
                "sampled_rows": 10,
            },
        ),
        "PRIMARY_KEY",
    )
    assert dup.status is Status.ESTIMATED and dup.value["unique"] is False  # a sample refutes
    clean = _by(
        _facts(
            samp,
            {
                "row_count": 10,
                "non_null": 10,
                "distinct_keys": 10,
                "max_rows_per_key": 1,
                "duplicate_keys": 0,
                "sampled_rows": 10,
            },
        ),
        "PRIMARY_KEY",
    )
    assert clean.value["unique"] is None and clean.value["consistent_with_sample"] is True


def test_freshness_and_pattern_and_rowcount() -> None:
    col = ObjectRef(None, "shop", "orders", "created_at")
    fr = _by(_facts(Freshness(col), {"max_value": "2026-11-24T11:00:00Z"}), "FRESHNESS")
    assert fr.value["column"] == "created_at" and fr.value["age_seconds"] == 3600.0
    naive = _by(_facts(Freshness(col), {"max_value": datetime(2026, 11, 24, 10)}), "FRESHNESS")
    assert naive.value["age_seconds"] == 7200.0 and naive.value["assumed_utc"] is True
    none = _by(_facts(Freshness(col), {"max_value": None}), "FRESHNESS")
    assert none.status is Status.UNMEASURABLE and none.value["age_seconds"] is None
    odd = _by(_facts(Freshness(col), {"max_value": "not a date"}), "FRESHNESS")
    assert odd.status is Status.OBSERVED and odd.value["age_seconds"] is None
    pat = _by(
        _facts(
            PatternRate(col, "____-__-__%", means="ISO date text"),
            {"non_null": 200, "matching": Decimal(200)},
        ),
        "TYPE_NOTE",
    )
    assert pat.value["rate"] == 1.0 and pat.value["means"] == "ISO date text"
    assert pat.value["holds"] is True
    assert _by(_facts(RowCount(ORD), {"row_count": 7}), "ROW_COUNT").value["rows"] == 7


def test_no_rows_is_unmeasurable() -> None:
    f = _facts(RowCount(ORD), None)
    assert len(f) == 1 and f[0].status is Status.UNMEASURABLE


KEYHASH_PK = SampleChoice("keyhash", Decimal("0.05"), 1, ("order_id",))


def test_keyhash_uniqueness_scales_and_labels_counts() -> None:
    req = Uniqueness(ORD, ("order_id",), sample=KEYHASH_PK)
    p = {"row_count": 500, "non_null": 500, "distinct_keys": 500, "max_rows_per_key": 1,
         "duplicate_keys": 0, "sampled_rows": 500}  # fmt: skip
    facts = _facts(req, p)
    pk = _by(facts, "PRIMARY_KEY")
    assert pk.status is Status.ESTIMATED
    assert pk.value["row_count"] == 10_000 and pk.value["distinct_keys"] == 10_000
    assert pk.value["non_null"] == 10_000 and pk.value["duplicate_keys"] == 0
    est = pk.value["estimates"]
    assert est["distinct_keys"]["method"] == "scaled" and est["distinct_keys"]["value"] == 10_000
    assert est["distinct_keys"]["lower_bound"] is False
    assert est["max_rows_per_key"]["lower_bound"] is True
    assert pk.value["estimate"] == est["distinct_keys"]
    # keyhash on exactly a single-column key is the spec-8.6 distinct count of that key
    d = _by(facts, "DISTINCT", "order_id")
    assert d.status is Status.ESTIMATED and d.value["distinct_keys"] == 10_000
    assert d.value["estimate"]["method"] == "scaled"


def test_non_keyhash_uniqueness_does_not_pass_raw_counts_as_totals() -> None:
    req = Uniqueness(ORD, ("order_id", "status"),
                     sample=SampleChoice("keyhash", Decimal("0.05"), 1, ("order_id",)))  # fmt: skip
    p = {"row_count": 500, "non_null": 499, "distinct_keys": 480, "max_rows_per_key": 2,
         "duplicate_keys": 3, "sampled_rows": 500}  # fmt: skip
    facts = _facts(req, p)
    pk = _by(facts, "PRIMARY_KEY")
    assert pk.value["row_count"] is None and pk.value["distinct_keys"] is None
    assert pk.value["sample_counts"]["distinct_keys"] == 480
    assert pk.value["estimates"]["distinct_keys"]["lower_bound"] is True
    assert pk.value["complete"] is False and pk.value["unique"] is False
    assert not any(f.kind.name == "DISTINCT" for f in facts)  # not keyhash on this key


def test_system_sample_estimates_are_approximate() -> None:
    req = ProfileColumns(ORD, ("deleted",), sample=SYS)
    facts = _facts(req, {"row_count": 1000, "sampled_rows": 1000,
                         "columns": {"deleted": {"non_null": 30, "distinct_keys": 1}}})  # fmt: skip
    for f in facts:
        assert f.value["approximate"] is True and f.value["estimate"]["approximate"] is True
    bern = ProfileColumns(ORD, ("deleted",), sample=SampleChoice("bernoulli", Decimal("0.01"), 7))
    facts = _facts(bern, {"row_count": 1000, "sampled_rows": 1000,
                          "columns": {"deleted": {"non_null": 30,
                                                  "distinct_keys": 1}}})  # fmt: skip
    assert all(f.value["estimate"]["approximate"] is False for f in facts)
    assert all("approximate" not in f.value for f in facts)
