from __future__ import annotations

from decimal import Decimal

import pytest

from schemaguru.measure.estimates import EstimatedValue, scale_count, wilson


def test_wilson_interval_values() -> None:
    lo, hi = wilson(50, 1000)  # type: ignore[misc]
    # brief: ~(0.0382, 0.0653) to 4 dp; the exact Wilson bounds are 0.038130 and 0.065314
    assert lo == pytest.approx(0.0382, abs=1e-4) and hi == pytest.approx(0.0653, abs=1e-4)
    assert lo == pytest.approx(0.0381301, abs=1e-6) and hi == pytest.approx(0.0653139, abs=1e-6)


def test_wilson_unknown_when_no_rows() -> None:
    assert wilson(0, 0) is None


def test_wilson_extremes_stay_in_unit_interval() -> None:
    lo, hi = wilson(0, 100)  # type: ignore[misc]
    assert lo == 0.0 and 0 < hi < 0.05
    lo, hi = wilson(100, 100)  # type: ignore[misc]
    assert hi == pytest.approx(1.0) and 0.95 < lo < 1


def test_design_effect_widens_interval() -> None:
    lo1, hi1 = wilson(50, 1000)  # type: ignore[misc]
    lo2, hi2 = wilson(50, 1000, design_effect=2.0)  # type: ignore[misc]
    assert lo2 < lo1 and hi2 > hi1
    # effective n = n / design_effect: a deff of 2 on 1000 rows equals 500 plain rows
    assert (lo2, hi2) == pytest.approx(wilson(25, 500))  # type: ignore[arg-type]


def test_wilson_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        wilson(5, 3)
    with pytest.raises(ValueError):
        wilson(-1, 3)
    with pytest.raises(ValueError):
        wilson(1, 3, design_effect=0.5)


def test_scale_count() -> None:
    assert scale_count(37, Decimal("0.01")) == 3700
    assert scale_count(1, Decimal("0.3")) == 3  # 3.33 rounds to 3
    assert scale_count(0, Decimal("0.001")) == 0
    with pytest.raises(ValueError):
        scale_count(1, Decimal(0))


def test_estimated_value_is_plain_data() -> None:
    ev = EstimatedValue(0.05, 0.04, 0.06, "wilson95", False)
    assert ev.__dict__ == {
        "value": 0.05,
        "low": 0.04,
        "high": 0.06,
        "method": "wilson95",
        "lower_bound": False,
        "approximate": False,
    }
