from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from aifund.config.trading_config import LimitsConfig
from aifund.domain.enums import ReasonCode
from aifund.risk.limits import (
    EquityState,
    Exposure,
    LimitKind,
    check_loss_limits,
    check_new_exposure,
    drawdown_pct,
    trading_day_start,
    trading_week_start,
)

LIMITS = LimitsConfig()  # 5 positions, heat 3%, bucket 1.5%, daily 3%, weekly 6%, dd 10%, leverage 10x


def state(equity: str, day: str = "10000", week: str = "10000", peak: str = "10000") -> EquityState:
    return EquityState(
        equity=D(equity), day_start_equity=D(day), week_start_equity=D(week), peak_equity=D(peak)
    )


@pytest.mark.parametrize(
    ("s", "expected"),
    [
        (state("9800"), None),  # -2% on the day
        (state("9700"), LimitKind.DAILY_LOSS),  # exactly -3% trips it
        (state("9650", day="9900", week="10300"), LimitKind.WEEKLY_LOSS),  # -2.5% day, -6.3% week
        (state("9400", day="9500", week="9800", peak="10500"), LimitKind.MAX_DRAWDOWN),  # -10.5% from peak
        (state("10500", day="10000"), None),  # gains never breach
        (state("9000", day="0", week="0", peak="0"), None),  # no reference yet
    ],
)
def test_loss_limits(s: EquityState, expected: LimitKind | None) -> None:
    breach = check_loss_limits(s, LIMITS)
    assert (breach.kind if breach else None) is expected


def test_most_severe_breach_is_reported_and_described() -> None:
    breach = check_loss_limits(state("8900"), LIMITS)  # -11%: daily, weekly and drawdown all breached
    assert breach is not None
    assert breach.kind is LimitKind.MAX_DRAWDOWN
    assert breach.describe() == "MAX_DRAWDOWN: 11.00% >= limit 10.0%"
    assert drawdown_pct(state("9500", peak="10000")) == D(5)


def pos(symbol: str, bucket: str, risk: str, notional: str = "10000") -> Exposure:
    return Exposure(symbol=symbol, bucket=bucket, initial_risk_money=D(risk), notional=D(notional))


EQUITY = D("10000")


def test_new_trade_within_all_limits() -> None:
    assert check_new_exposure(pos("XAUUSD", "USD_INVERSE", "50"), [pos("BTCUSD", "CRYPTO", "50")], EQUITY,
                              LIMITS) is None  # fmt: skip


@pytest.mark.parametrize(
    ("new", "open_", "reason"),
    [
        (pos("X", "A", "10"), [pos(f"S{i}", f"B{i}", "10") for i in range(5)], ReasonCode.MAX_POSITIONS),
        (
            pos("X", "A", "150"),
            [pos("Y", "B", "100"), pos("Z", "C", "60")],
            ReasonCode.PORTFOLIO_HEAT,
        ),  # 3.1%
        (pos("X", "A", "100"), [pos("Y", "A", "60")], ReasonCode.BUCKET_HEAT),  # 1.6% in bucket A
        (
            pos("X", "A", "10", notional="60000"),
            [pos("Y", "B", "10", notional="45000")],
            ReasonCode.LEVERAGE_CAP,
        ),
    ],
)
def test_each_exposure_limit(new: Exposure, open_: list[Exposure], reason: ReasonCode) -> None:
    rejection = check_new_exposure(new, open_, EQUITY, LIMITS)
    assert rejection is not None
    assert rejection.reason is reason


def test_exactly_at_the_heat_limit_is_allowed() -> None:
    assert check_new_exposure(pos("X", "A", "150"), [pos("Y", "B", "150")], EQUITY, LIMITS) is None  # 3.0%


def test_symbol_heat_caps_stacked_positions_on_one_symbol() -> None:
    """Roadmap 10.8: families stacked on one symbol ride the same price; their risk is capped together."""
    capped = LIMITS.model_copy(update={"max_symbol_heat_pct": D("1.0")})
    stacked = [pos("XAUUSD", "USD_INVERSE", "50")]
    assert check_new_exposure(pos("XAUUSD", "USD_INVERSE", "50"), stacked, EQUITY, capped) is None  # 1.0%
    rejection = check_new_exposure(pos("XAUUSD", "USD_INVERSE", "60"), stacked, EQUITY, capped)  # 1.1%
    assert rejection is not None
    assert (rejection.reason, rejection.detail) == (ReasonCode.SYMBOL_HEAT, "XAUUSD heat 1.10% > 1.0%")
    other = [pos("BTCUSD", "CRYPTO", "90")]  # another symbol does not count
    assert check_new_exposure(pos("XAUUSD", "USD_INVERSE", "60"), other, EQUITY, capped) is None
    assert check_new_exposure(pos("XAUUSD", "USD_INVERSE", "60"), stacked, EQUITY, LIMITS) is None  # off


def test_leverage_matters_at_high_broker_leverage() -> None:
    # 0.25 lot of gold at 1:500 needs only ~$207 margin but is ~$103,750 notional = 10.4x a $10k account
    rejection = check_new_exposure(pos("XAUUSD", "USD_INVERSE", "50", notional="103750"), [], EQUITY, LIMITS)
    assert rejection is not None
    assert rejection.reason is ReasonCode.LEVERAGE_CAP


# ---------------------------------------------------------------- trading day / week
# The broker's day rolls at 17:00 New York (server midnight at UTC+3/+2): 21:00 UTC in summer, 22:00 UTC in
# winter. New York moved to EDT on 2026-03-08 and back to EST on 2026-11-01, both Sundays.

NY = "America/New_York"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize(
    ("moment", "day_start"),
    [
        (utc(2026, 9, 28, 20, 59), utc(2026, 9, 27, 21, 0)),  # summer: 16:59 EDT -> yesterday 17:00
        (utc(2026, 9, 28, 21, 0), utc(2026, 9, 28, 21, 0)),
        (utc(2026, 9, 28, 23, 30), utc(2026, 9, 28, 21, 0)),
        # 00:30 IST on the 29th is 19:00 UTC on the 28th -> still the day that started 27th 21:00 UTC
        (datetime(2026, 9, 29, 0, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
         utc(2026, 9, 27, 21, 0)),
        (utc(2026, 12, 2, 21, 30), utc(2026, 12, 1, 22, 0)),  # winter: 16:30 EST -> yesterday 22:00 UTC
        (utc(2026, 12, 2, 22, 0), utc(2026, 12, 2, 22, 0)),
        # spring forward: Sat 17:00 EST = 22:00 UTC, Sun 17:00 EDT = 21:00 UTC (a 23-hour trading day)
        (utc(2026, 3, 8, 20, 30), utc(2026, 3, 7, 22, 0)),
        (utc(2026, 3, 8, 21, 30), utc(2026, 3, 8, 21, 0)),
        # fall back: Sat 17:00 EDT = 21:00 UTC, Sun 17:00 EST = 22:00 UTC (a 25-hour trading day)
        (utc(2026, 11, 1, 21, 30), utc(2026, 10, 31, 21, 0)),
        (utc(2026, 11, 1, 22, 0), utc(2026, 11, 1, 22, 0)),
    ],
)  # fmt: skip
def test_trading_day_start(moment: datetime, day_start: datetime) -> None:
    assert trading_day_start(moment, "17:00", NY) == day_start  # non-UTC input is converted, not trusted


def test_naive_moments_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        trading_day_start(datetime(2026, 9, 28, 12, 0), "17:00", NY)


def test_trading_week_starts_on_sunday_at_the_boundary() -> None:
    assert trading_week_start(utc(2026, 9, 30, 12, 0), "17:00", NY) == utc(2026, 9, 27, 21, 0)  # Sunday
    assert trading_week_start(utc(2026, 9, 27, 22, 0), "17:00", NY) == utc(2026, 9, 27, 21, 0)
    assert trading_week_start(utc(2026, 9, 27, 20, 0), "17:00", NY) == utc(2026, 9, 20, 21, 0)
    # across the fall-back weekend: the week opens Sunday 17:00 EST = 22:00 UTC (a fixed 21:00 UTC was wrong)
    assert trading_week_start(utc(2026, 11, 4, 12, 0), "17:00", NY) == utc(2026, 11, 1, 22, 0)
    assert trading_week_start(utc(2026, 11, 1, 21, 30), "17:00", NY) == utc(2026, 10, 25, 21, 0)  # before it
    assert trading_week_start(utc(2026, 3, 11, 12, 0), "17:00", NY) == utc(2026, 3, 8, 21, 0)  # spring
