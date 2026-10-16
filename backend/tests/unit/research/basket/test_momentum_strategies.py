"""tsmom_daily and intraday_momentum (roadmap 11.13): signs, warm-up, no lookahead, mirrors, sessions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest

from aifund.domain.enums import Side, Timeframe
from aifund.research.basket.candles import DAY, Candles, server_minute
from aifund.research.basket.strategies.intraday_momentum import (
    IntradayMomentum,
    IntradayParams,
    ServerClock,
    session_for,
)
from aifund.research.basket.strategies.tsmom_daily import TsmomDaily, TsmomParams


def wall(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> int:
    """A server wall-clock time as server minutes."""
    return server_minute(datetime(y, mo, d, h, mi, tzinfo=UTC), 0)


START = wall(2026, 6, 1)  # a Monday


def daily(closes: list[float], *, start: int = START, step: int = DAY) -> Candles:
    t = [start + i * step for i in range(len(closes))]
    return Candles.of(Timeframe.D1, t, closes, [c + 1 for c in closes], [c - 1 for c in closes], closes)


# ---------------------------------------------------------------- tsmom_daily


def test_tsmom_holds_the_sign_of_the_lookback_return_from_its_first_full_window() -> None:
    d1 = daily([100 + 0.5 * i for i in range(40)])  # one bar every calendar day, rising
    holds = TsmomDaily(TsmomParams(lookback_days=28)).holds({Timeframe.D1: d1})
    # bar i closes at START + (i + 1) days; the first with a close 28 days earlier is i = 28
    # (ATR20 is ready at 19)
    assert holds[0].decided_at == START + 29 * DAY
    assert len(holds) == 40 - 28 - 1  # the last bar has no next decision to hold until
    assert {h.side for h in holds} == {Side.BUY}
    assert holds[0].end_at == START + 30 * DAY
    assert holds[0].stop_distance == pytest.approx(1.5 * 2.0)  # every true range is 2
    assert holds[0].max_entry_delay == 3 * DAY


def test_tsmom_lookback_is_in_calendar_days_on_a_five_day_week() -> None:
    weekdays = [START + (w * 7 + d) * DAY for w in range(8) for d in range(5)]
    closes = [100.0 + i for i in range(len(weekdays))]
    d1 = Candles.of(Timeframe.D1, weekdays, closes, [c + 1 for c in closes], [c - 1 for c in closes], closes)
    holds = TsmomDaily(TsmomParams(lookback_days=14, atr_period=2)).holds({Timeframe.D1: d1})
    # the first bar closes on day 1; the first close 14 calendar days later is week 3's Monday bar (index 10),
    # closing on day 15 — ten trading bars, not fourteen
    assert holds[0].decided_at == START + 15 * DAY
    assert all(h.side is Side.BUY for h in holds)
    # with ATR20 the warm-up binds instead: bar 19 (week 4's Friday) closes on day 26
    assert (
        TsmomDaily(TsmomParams(lookback_days=14)).holds({Timeframe.D1: d1})[0].decided_at == START + 26 * DAY
    )


def test_tsmom_without_enough_history_or_with_a_flat_return_holds_nothing() -> None:
    strat = TsmomDaily()
    assert strat.holds({}) == []
    assert strat.holds({Timeframe.D1: daily([100.0])}) == []
    assert strat.holds({Timeframe.D1: daily([100.0] * 60)}) == []  # zero return every day
    assert strat.params() == {"lookback_days": 28, "atr_period": 20, "stop_atr": 1.5}


def test_tsmom_reads_only_closed_bars() -> None:
    closes = [100 + ((i * 7) % 11) - 5 + 0.3 * i for i in range(70)]
    strat = TsmomDaily(TsmomParams(lookback_days=14))
    full = strat.holds({Timeframe.D1: daily(closes)})
    for cut in (40, 55):
        part = strat.holds({Timeframe.D1: daily(closes[:cut])})
        assert part == [h for h in full if h.end_at <= START + cut * DAY]


def test_tsmom_on_the_reflected_market_takes_the_other_side() -> None:
    closes = [100 + ((i * 7) % 11) - 5 + 0.3 * i for i in range(70)]
    strat = TsmomDaily(TsmomParams(lookback_days=14))
    up = strat.holds({Timeframe.D1: daily(closes)})
    down = strat.holds({Timeframe.D1: daily([10_000 / c for c in closes])})  # ln(1/x) = -ln(x)
    assert [h.decided_at for h in up] == [h.decided_at for h in down]
    assert all(a.side is not b.side for a, b in zip(up, down, strict=True))
    forced = strat.holds({Timeframe.D1: daily(closes)}, force=Side.BUY)
    assert {h.side for h in forced} == {Side.BUY}
    assert len(forced) >= len(up)


# ---------------------------------------------------------------- the server clock and sessions


@pytest.mark.parametrize(
    ("day", "hh_mm", "zone", "expected"),
    [
        (date(2026, 7, 15), "09:30", "America/New_York", wall(2026, 7, 15, 16, 30)),  # summer
        (
            date(2026, 1, 15),
            "09:30",
            "America/New_York",
            wall(2026, 1, 15, 16, 30),
        ),  # winter: same wall clock
        (date(2026, 7, 15), "09:00", "Europe/Berlin", wall(2026, 7, 15, 10, 0)),
        (date(2026, 1, 15), "09:00", "Europe/Berlin", wall(2026, 1, 15, 10, 0)),
        (
            date(2026, 3, 10),
            "09:00",
            "Europe/Berlin",
            wall(2026, 3, 10, 11, 0),
        ),  # US on summer time, EU not yet
        (date(2026, 7, 15), "09:00", "Asia/Tokyo", wall(2026, 7, 15, 3, 0)),
        (date(2026, 1, 15), "09:00", "Asia/Tokyo", wall(2026, 1, 15, 2, 0)),
    ],
)
def test_exchange_times_on_the_server_clock(day: date, hh_mm: str, zone: str, expected: int) -> None:
    clock = ServerClock()
    assert clock.minute(day, hh_mm, zone) == expected
    assert clock.day_of(expected, zone) == day


def test_sessions_by_broker_symbol() -> None:
    assert session_for("NAS100.r") == session_for("NAS100")
    assert session_for("JP225") == session_for("JPN225")
    assert session_for("GER40") is not None
    assert session_for("BTCUSD") is None


# ---------------------------------------------------------------- intraday_momentum

MON = wall(2026, 7, 13)  # a Monday in US summer: the NAS100 cash session is 16:30-23:00 on the server


def m5(
    price: Callable[[int], float], days: int = 3, skip: Callable[[int], bool] = lambda t: False
) -> Candles:
    """Five-minute bars around the clock; each bar opens at price(t) and closes at price(t + 5)."""
    t = [m for m in range(MON, MON + days * DAY, 5) if not skip(m)]
    o = [price(m) for m in t]
    c = [price(m + 5) for m in t]
    return Candles.of(Timeframe.M5, t, o, [max(a, b) + 0.1 for a, b in zip(o, c, strict=True)],
                      [min(a, b) - 0.1 for a, b in zip(o, c, strict=True)], c, [0.05] * len(t))  # fmt: skip


def d1_before(days: int = 30) -> Candles:
    closes = [100.0] * days
    return daily(closes, start=MON - days * DAY)


def rising(m: int) -> float:
    return 100 + (m - MON) * 0.001


def intraday(window: str, path: Candles, symbol: str = "NAS100.r") -> list:  # type: ignore[type-arg]
    strat = IntradayMomentum(IntradayParams(window=window))
    return strat.holds(symbol, {Timeframe.D1: d1_before()}, path)


def test_one_hold_per_session_from_the_window_end_to_15_minutes_before_the_close() -> None:
    holds = intraday("open30", m5(rising))
    assert [h.decided_at for h in holds] == [MON + k * DAY + 17 * 60 for k in range(3)]  # 16:30 + 30 min
    assert [h.end_at for h in holds] == [MON + k * DAY + 22 * 60 + 45 for k in range(3)]
    assert {h.side for h in holds} == {Side.BUY}
    assert holds[0].stop_distance == pytest.approx(2.0)  # 1.0 x ATR20(D1), every D1 range 2
    assert intraday("open60", m5(rising))[0].decided_at == MON + 17 * 60 + 30


def test_close30_needs_the_prior_session_close() -> None:
    holds = intraday("close30", m5(rising))
    assert [h.decided_at for h in holds] == [MON + k * DAY + 17 * 60 for k in (1, 2)]  # none on the first day


def test_a_falling_or_reflected_market_sells() -> None:
    def wave(m: int) -> float:
        return 100 + 3 * ((m // 37) % 5) - 0.0005 * (m - MON)

    for window in ("close30", "open30", "open60"):
        up = intraday(window, m5(wave))
        down = intraday(window, m5(lambda m: 10_000 / wave(m)))
        assert [h.decided_at for h in up] == [h.decided_at for h in down]
        assert all(a.side is not b.side for a, b in zip(up, down, strict=True))
    assert {h.side for h in intraday("open30", m5(lambda m: 200 - (m - MON) * 0.001))} == {Side.SELL}


def test_the_decision_reads_only_bars_closed_by_then() -> None:
    def spike(m: int) -> float:  # the bar opening at the decision closes far below: too late to matter
        return 50.0 if m == MON + 17 * 60 + 5 else rising(m)

    assert intraday("open30", m5(rising))[0].side is Side.BUY
    assert intraday("open30", m5(spike))[0].side is Side.BUY


def test_holidays_half_days_and_missing_bars_give_no_signal() -> None:
    def half_day(m: int) -> bool:  # the second session stops trading at 20:00
        return MON + DAY + 20 * 60 <= m < MON + 2 * DAY

    holds = intraday("open30", m5(rising, skip=half_day))
    assert [h.decided_at for h in holds] == [MON + 17 * 60, MON + 2 * DAY + 17 * 60]
    # the half day did not trade to its close, so the third session's prior close is the first's
    assert [h.decided_at for h in intraday("close30", m5(rising, skip=half_day))] == [MON + 2 * DAY + 17 * 60]

    def no_open(m: int) -> bool:
        return m == MON + 16 * 60 + 30

    assert intraday("open30", m5(rising, skip=no_open))[0].decided_at == MON + DAY + 17 * 60

    def gap_before_decision(m: int) -> bool:
        return MON + 16 * 60 + 35 <= m < MON + 17 * 60

    assert intraday("open30", m5(rising, skip=gap_before_decision))[0].decided_at == MON + DAY + 17 * 60


def test_weekends_and_symbols_without_a_session_give_nothing() -> None:
    sat = wall(2026, 7, 18)
    path = Candles.of(Timeframe.M5, list(range(sat, sat + 2 * DAY, 5)), *([[100.0] * 576] * 4))
    assert IntradayMomentum().holds("NAS100.r", {Timeframe.D1: d1_before()}, path) == []
    assert IntradayMomentum().holds("BTCUSD", {Timeframe.D1: d1_before()}, m5(rising)) == []
    assert IntradayMomentum().holds("NAS100.r", {}, m5(rising)) == []
    short_d1 = {Timeframe.D1: d1_before(days=5)}  # ATR20 not ready yet: no stop, no position
    assert IntradayMomentum(IntradayParams(window="open30")).holds("NAS100.r", short_d1, m5(rising)) == []
    assert IntradayMomentum().params() == {"window": "close30", "atr_period": 20, "stop_atr": 1.0}
    with pytest.raises(ValueError, match="window"):
        IntradayMomentum(IntradayParams(window="open15"))


def test_forcing_a_side_is_the_always_long_control() -> None:
    falling = m5(lambda m: 200 - (m - MON) * 0.001)
    strat = IntradayMomentum(IntradayParams(window="open30"))
    forced = strat.holds("NAS100.r", {Timeframe.D1: d1_before()}, falling, force=Side.BUY)
    assert {h.side for h in forced} == {Side.BUY}
    assert len(forced) == 3
