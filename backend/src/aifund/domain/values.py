"""Decimal helpers for prices and volumes. All money-path arithmetic uses Decimal, never float.

Rounding direction is always explicit: volumes round DOWN to the broker's step (never risk more than
sized), and prices round to the tick in the direction the caller specifies (e.g. stop-losses AWAY from
entry, take-profits TOWARD entry — docs/03 §10).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator


class Rounding(StrEnum):
    DOWN = "DOWN"  # toward -inf
    UP = "UP"  # toward +inf
    NEAREST = "NEAREST"  # half-even


_MODES = {Rounding.DOWN: ROUND_FLOOR, Rounding.UP: ROUND_CEILING, Rounding.NEAREST: ROUND_HALF_EVEN}


def to_decimal(value: Decimal | int | float | str) -> Decimal:
    """Convert without binary-float artefacts: floats go through their shortest repr."""
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, float):
        result = Decimal(repr(value))
    else:
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"not a number: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return result


def quantize_to_step(value: Decimal, step: Decimal, rounding: Rounding) -> Decimal:
    """Round ``value`` to an integer multiple of ``step`` in the given direction."""
    if step <= 0:
        raise ValueError(f"step must be positive, got {step}")
    units = (value / step).to_integral_value(rounding=_MODES[rounding])
    return (units * step).normalize() if units else Decimal(0)


def floor_volume(volume: Decimal, step: Decimal) -> Decimal:
    """Volumes always round down to the step (docs/03 §11)."""
    return quantize_to_step(volume, step, Rounding.DOWN)


def is_multiple_of(value: Decimal, step: Decimal) -> bool:
    return value % step == 0


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(None):
        raise ValueError("datetime must be timezone-aware UTC")
    return value


UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]
"""A datetime that must be tz-aware UTC. Broker server time is converted at the MT5 adapter boundary."""
