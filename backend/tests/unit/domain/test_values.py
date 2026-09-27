from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from aifund.domain.values import (
    Rounding,
    UtcDatetime,
    floor_volume,
    is_multiple_of,
    quantize_to_step,
    to_decimal,
)

steps = st.sampled_from([Decimal(s) for s in ("0.001", "0.01", "0.1", "0.5", "1", "0.05", "100")])
volumes = st.decimals(min_value=Decimal("0"), max_value=Decimal("10000"), places=6, allow_nan=False)


@given(volume=volumes, step=steps)
def test_floor_volume_is_a_multiple_of_step_never_above_input_and_within_one_step(
    volume: Decimal, step: Decimal
) -> None:
    result = floor_volume(volume, step)
    assert is_multiple_of(result, step)
    assert result <= volume
    assert volume - result < step


def test_floor_volume_never_rounds_up_to_min() -> None:
    # The prototype turned 0.0056 into 0.01 (2x) and 0.003 into 0.0 via round(x, 2).
    assert floor_volume(Decimal("0.0056"), Decimal("0.001")) == Decimal("0.005")
    assert floor_volume(Decimal("0.003"), Decimal("0.001")) == Decimal("0.003")
    assert floor_volume(Decimal("0.0099"), Decimal("0.01")) == Decimal("0")


@pytest.mark.parametrize(
    ("value", "step", "rounding", "expected"),
    [
        ("1.08473", "0.0001", Rounding.DOWN, "1.0847"),
        ("1.08473", "0.0001", Rounding.UP, "1.0848"),
        ("1.08475", "0.0001", Rounding.NEAREST, "1.0848"),  # half-even
        ("2345.678", "0.01", Rounding.DOWN, "2345.67"),
        ("2345.671", "0.01", Rounding.UP, "2345.68"),
        ("100.3", "0.25", Rounding.DOWN, "100.25"),
        ("-1.5", "1", Rounding.DOWN, "-2"),
    ],
)
def test_quantize_to_step(value: str, step: str, rounding: Rounding, expected: str) -> None:
    assert quantize_to_step(Decimal(value), Decimal(step), rounding) == Decimal(expected)


def test_quantize_rejects_non_positive_step() -> None:
    with pytest.raises(ValueError, match="step must be positive"):
        quantize_to_step(Decimal(1), Decimal(0), Rounding.DOWN)


def test_to_decimal_avoids_binary_float_artifacts_and_rejects_non_finite() -> None:
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal("2345.67") == Decimal("2345.67")
    for bad in (float("nan"), float("inf"), "abc", "15%"):
        with pytest.raises(ValueError, match="not a"):
            to_decimal(bad)


class _M(BaseModel):
    t: UtcDatetime


def test_utc_datetime_rejects_naive_and_non_utc() -> None:
    _M(t=datetime(2026, 9, 28, tzinfo=UTC))
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        _M(t=datetime(2026, 9, 28))
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        _M(t=datetime(2026, 9, 28, tzinfo=timezone(timedelta(hours=3))))
