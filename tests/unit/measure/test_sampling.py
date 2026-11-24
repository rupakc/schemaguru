from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from schemaguru.config import SamplingConfig
from schemaguru.measure.requests import (
    Freshness,
    KeyOverlapReq,
    PatternRate,
    ProfileColumns,
    RowCount,
    SampleChoice,
    TopValues,
    Uniqueness,
)
from schemaguru.measure.sampling import choose_sample, is_large, seed_for
from schemaguru.model.joins import JoinRef
from schemaguru.model.refs import ObjectRef
from schemaguru.model.structure import Table

GIB = 1 << 30
ORD = ObjectRef(None, "shop", "orders")
CUS = ObjectRef(None, "shop", "customers")
SMALL = Table(ORD, "table", row_count=10_000, bytes=1 << 20)
LARGE = Table(ORD, "table", row_count=4_000_000_000, bytes=200 * GIB)
JOIN = JoinRef(
    (ObjectRef(None, "shop", "orders", "customer_id"),),
    (ObjectRef(None, "shop", "customers", "customer_id"),),
)
CFG = SamplingConfig()
PROFILE = ProfileColumns(ORD, ("status", "deleted"))


def _choose(
    table: Table, req: object, cfg: SamplingConfig = CFG, **kw: object
) -> SampleChoice | None:
    args: dict[str, object] = {
        "key_distinct": None,
        "est_bytes": table.bytes,
        "max_bytes_per_probe": GIB,
        "seed": 7,
    }
    args.update(kw)
    return choose_sample(table, req, cfg, **args)  # type: ignore[arg-type]


def test_exact_small_table() -> None:
    assert _choose(SMALL, PROFILE) is None
    assert _choose(SMALL, KeyOverlapReq(JOIN), key_distinct=10_000_000) is None


def test_large_by_bytes_or_rows() -> None:
    assert is_large(LARGE, CFG)
    assert not is_large(SMALL, CFG)
    rows_only = Table(ORD, "table", row_count=600_000_000)
    assert is_large(rows_only, CFG)  # bytes unknown: the row threshold decides
    assert not is_large(Table(ORD, "table", row_count=600_000_000, bytes=1), CFG)  # bytes known
    assert not is_large(Table(ORD, "table"), CFG)  # nothing known: exact


def test_large_table_uses_system_sample() -> None:
    choice = _choose(LARGE, PROFILE)
    assert choice is not None and choice.method == "system" and choice.key == ()
    # fraction = max_bytes_per_probe / est_bytes = 1/200, inside [floor, ceiling]
    assert choice.fraction == Decimal("0.005")
    assert choice.method_string() == "sampled:system:0.005:7"


def test_system_fraction_clamped() -> None:
    huge = Table(ORD, "table", bytes=10_000 * GIB)
    assert _choose(huge, PROFILE, est_bytes=10_000 * GIB).fraction == Decimal("0.001")  # floor
    assert _choose(LARGE, PROFILE, est_bytes=2 * GIB).fraction == Decimal("0.1")  # ceiling
    # unknown cost: the cheapest representative sample (floor); unlimited budget: ceiling
    assert _choose(LARGE, PROFILE, est_bytes=None).fraction == Decimal("0.001")
    assert _choose(LARGE, PROFILE, max_bytes_per_probe=None).fraction == Decimal("0.1")


def test_bernoulli_when_no_block_sampling() -> None:
    choice = _choose(
        LARGE, TopValues(ObjectRef(None, "shop", "orders", "status"), 20), block_sampling=False
    )
    assert choice is not None and choice.method == "bernoulli"


def test_large_key_uses_keyhash_same_seed_both_sides() -> None:
    choice = _choose(LARGE, KeyOverlapReq(JOIN), key_distinct=50_000_000)
    assert choice is not None and choice.method == "keyhash"
    assert choice.fraction == Decimal("0.1")  # exact_key_limit / key_distinct
    assert choice.key == ("customer_id",)
    # the bridge applies the same fraction and seed to the right side: determinism per seed
    assert choice == _choose(LARGE, KeyOverlapReq(JOIN), key_distinct=50_000_000)
    uniq = _choose(LARGE, Uniqueness(ORD, ("order_id", "line_no")), key_distinct=20_000_000)
    assert uniq is not None and uniq.method == "keyhash" and uniq.key == ("order_id", "line_no")
    assert uniq.fraction == Decimal("0.25")


def test_large_table_small_key_measured_exactly() -> None:
    # key metrics sample only when the key's distinct count exceeds exact_key_limit (spec 8.6)
    assert _choose(LARGE, KeyOverlapReq(JOIN), key_distinct=1_000_000) is None


def test_key_distinct_unknown_falls_back_to_row_count_bound() -> None:
    choice = _choose(LARGE, KeyOverlapReq(JOIN), key_distinct=None)
    assert choice is not None and choice.method == "keyhash"
    assert choice.fraction == Decimal("0.00125")  # 5M / 4G rows (an upper bound on keys)
    nothing = Table(ORD, "table", bytes=200 * GIB)  # large, but no row count and no distinct
    assert _choose(nothing, KeyOverlapReq(JOIN), key_distinct=None) is None


def test_freshness_never_sampled() -> None:
    col = ObjectRef(None, "shop", "orders", "created_at")
    for cfg in (CFG, SamplingConfig(measurement="force")):
        assert _choose(LARGE, Freshness(col), cfg) is None
        assert _choose(LARGE, RowCount(ORD), cfg) is None
    with pytest.raises(ValueError):
        Freshness(col, sample=SampleChoice("system", Decimal("0.1"), 1))


def test_sampling_off_is_exact_everywhere() -> None:
    off = SamplingConfig(measurement="off")
    assert _choose(LARGE, PROFILE, off) is None
    assert _choose(LARGE, KeyOverlapReq(JOIN), off, key_distinct=10**9) is None


def test_force_samples_small_tables() -> None:
    force = SamplingConfig(measurement="force")
    prof = _choose(SMALL, PROFILE, force)
    assert prof is not None and prof.method == "system" and prof.fraction == Decimal("0.001")
    key = _choose(SMALL, KeyOverlapReq(JOIN), force, key_distinct=1000)
    assert key is not None and key.method == "keyhash" and key.fraction == Decimal("0.001")
    big_key = _choose(SMALL, KeyOverlapReq(JOIN), force, key_distinct=50_000_000)
    assert big_key is not None and big_key.fraction == Decimal("0.1")
    pat = _choose(
        SMALL, PatternRate(ObjectRef(None, "shop", "orders", "created_at"), "____-%"), force
    )
    assert pat is not None and pat.method == "system"


def test_never_limit_and_fraction_at_least_one_bucket() -> None:
    choice = _choose(LARGE, KeyOverlapReq(JOIN), key_distinct=10**15)
    assert choice is not None and choice.fraction == Decimal("0.000001")


def test_seed_for_is_sha256_prefix() -> None:
    expected = int.from_bytes(
        hashlib.sha256(f"{ORD.canonical()}|snap-1".encode()).digest()[:8], "big"
    )
    assert seed_for(ORD, "snap-1") == expected
    assert seed_for(ORD, "snap-1") != seed_for(ORD, "snap-2")
    assert seed_for(ORD, "snap-1") != seed_for(CUS, "snap-1")


def test_sample_choice_validation() -> None:
    with pytest.raises(ValueError):
        SampleChoice("keyhash", Decimal("0.1"), 1)
    with pytest.raises(ValueError):
        SampleChoice("system", Decimal("0.1"), 1, key=("a",))
    with pytest.raises(ValueError):
        SampleChoice("system", Decimal("1.5"), 1)
    with pytest.raises(ValueError):
        SampleChoice("limit", Decimal("0.1"), 1)  # type: ignore[arg-type]
