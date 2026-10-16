"""Swap as a price distance (roadmap 11.11): every mode by hand, the triple night, no credit, refusals."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from aifund.domain.enums import Side, SwapMode
from aifund.research.basket.candles import DAY, server_minute
from aifund.research.basket.swap import SwapModel, SwapUnavailable
from tests.unit.risk.test_stops import XAU

MON = server_minute(datetime(2026, 10, 12, tzinfo=UTC), 0)  # a Monday, 00:00 on the server clock
HOUR = 60


def model(mode: SwapMode, long: str = "-50", short: str = "20", triple: int = 3) -> SwapModel:
    spec = XAU.model_copy(
        update={"swap_long": D(long), "swap_short": D(short), "swap_mode": mode, "swap_rollover3days": triple}
    )
    return SwapModel.of(spec)


def test_points_mode_charges_points_times_point() -> None:
    m = model(SwapMode.POINTS)
    assert m.night(Side.BUY, 4000.0) == pytest.approx(50 * 0.01)  # -50 points of 0.01
    assert m.night(Side.SELL, 4000.0) == 0.0  # +20: a credit is never booked


def test_deposit_mode_converts_money_per_lot_like_the_commission() -> None:
    spec = XAU.model_copy(
        update={"tick_value": D("2"), "swap_long": D("-7"), "swap_short": D("-3"),
                "swap_mode": SwapMode.CURRENCY_DEPOSIT, "swap_rollover3days": 3}
    )  # fmt: skip
    m = SwapModel.of(spec)
    assert m.night(Side.BUY, 4000.0) == pytest.approx(7 * 0.01 / 2)  # $7 per lot; one tick is $2 per lot
    assert m.night(Side.SELL, 4000.0) == pytest.approx(3 * 0.01 / 2)


def test_interest_mode_is_annual_percent_of_the_price_over_360_days() -> None:
    m = model(SwapMode.INTEREST_CURRENT, long="-3.6", short="-1.8")
    assert m.night(Side.BUY, 4000.0) == pytest.approx(4000 * 0.036 / 360)  # 0.4
    assert m.night(Side.SELL, 2000.0) == pytest.approx(2000 * 0.018 / 360)


def test_disabled_mode_charges_nothing() -> None:
    assert model(SwapMode.DISABLED).night(Side.BUY, 4000.0) == 0.0


def test_nights_are_the_rollovers_held_through() -> None:
    m = model(SwapMode.POINTS, triple=3)  # MT5 Wednesday -> server weekday 2
    assert m.triple_weekday == 2
    assert m.nights(MON + 9 * HOUR, MON + 20 * HOUR) == 0  # inside one day
    assert m.nights(MON + 20 * HOUR, MON + DAY + 9 * HOUR) == 1  # Monday's rollover
    assert (
        m.nights(MON, MON + DAY) == 1
    )  # entered AT a rollover: that one is not held; closed at the next: it is
    assert m.nights(MON + 2 * DAY + 9 * HOUR, MON + 3 * DAY + 9 * HOUR) == 3  # Wednesday's counts three
    assert m.nights(MON + 9 * HOUR, MON + 7 * DAY + 9 * HOUR) == 9  # a week: 1 + 1 + 3 + 1 + 1 + 1 + 1


def test_charge_is_one_night_times_the_nights() -> None:
    m = model(SwapMode.POINTS)
    held = m.charge(Side.BUY, MON + 20 * HOUR, MON + 3 * DAY + 9 * HOUR, 4000.0)  # Mon, Tue, Wed (x3)
    assert held == pytest.approx(5 * 0.5)


def test_a_spec_without_swap_or_with_an_unpriced_mode_is_refused() -> None:
    with pytest.raises(SwapUnavailable, match="no swap in the export"):
        SwapModel.of(XAU)
    with pytest.raises(SwapUnavailable, match="not implemented"):
        model(SwapMode.UNSUPPORTED)
