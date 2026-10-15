"""Each ported workshop strategy on hand-built bars (roadmap 11.3): the signal it must fire, and not fire."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from aifund.domain.enums import Side, Timeframe
from aifund.research.basket.candles import DAY, Candles
from aifund.research.basket.harness import FlatRule
from aifund.research.basket.strategies.base import finest, hhmm
from aifund.research.basket.strategies.candle_streak import CandleStreak, StreakParams
from aifund.research.basket.strategies.fib_pullback import FibParams, FibPullback
from aifund.research.basket.strategies.ict_dmss import DmssParams, IctDmss
from aifund.research.basket.strategies.spike_fade import SpikeFade, SpikeParams
from aifund.research.basket.strategies.sweep_pin import PinParams, SweepPinFollow, is_pin
from aifund.research.basket.strategies.sweep_rejection import RejectionParams, SweepRejectionFollow
from tests.unit.research.basket.test_harness import MON

Row = tuple[float, float, float, float]


def bars(tf: Timeframe, rows: Sequence[Row], start: int, spread: float = 0.0) -> Candles:
    t = [start + i * tf.minutes for i in range(len(rows))]
    return Candles.of(tf, t, *([r[k] for r in rows] for k in range(4)), [spread] * len(rows))


def test_helpers() -> None:
    m15 = bars(Timeframe.M15, [(1, 1, 1, 1)], MON)
    assert (
        finest({Timeframe.M15: m15, Timeframe.H1: bars(Timeframe.H1, [(1, 1, 1, 1)], MON)}, Timeframe.H1)
        is m15
    )
    assert finest({Timeframe.H1: bars(Timeframe.H1, [], MON)}, Timeframe.M15) is None
    assert finest({}, Timeframe.H1) is None
    assert hhmm("23:30") == 1410


# ---------------------------------------------------------------- CandleStreak


STREAK = [(10, 11, 9, 9.5), (9.5, 10, 8, 8.5), (8.5, 9, 7, 7.5), (7.5, 9, 7.2, 8.8), (8.8, 10, 8.6, 9.9),
          (9.9, 11, 9.7, 10.8)]  # fmt: skip  # three down, then three up


def test_candle_streak_follows_three_same_colour_bars() -> None:
    h4 = bars(Timeframe.H4, STREAK, MON, spread=0.1)
    sigs = CandleStreak(StreakParams(atr_period=2)).signals({Timeframe.H4: h4}, 0.01)
    assert [(s.side, s.decided_at) for s in sigs] == [(Side.SELL, MON + 3 * 240), (Side.BUY, MON + 6 * 240)]
    sell, buy = sigs
    # ATR(2) at bar 2: TR 2 and 2 -> 2.0; the SELL's stop: highest high 11 + 0.5 ATR + the spread
    assert sell.stop_level == pytest.approx(11 + 1.0 + 0.1)
    # ATR(2) at bar 5: TR 1.4 (bar4: 10-8.6 vs |10-8.8|) and 1.3 (11-9.7) -> 1.35; lowest low 7.2
    assert buy.stop_level == pytest.approx(7.2 - 0.675)
    assert (buy.stop_floor, buy.stop_cap) == (pytest.approx(0.405), pytest.approx(4.05))
    assert (buy.take_profit_r, buy.max_hold, buy.flats) == (2.0, 1440, (FlatRule(1430, weekday=4),))
    assert CandleStreak().signals({}, 0.01) == []
    assert CandleStreak().params()["streak"] == 3


def test_candle_streak_skips_without_atr_or_at_the_friday_flat() -> None:
    assert CandleStreak().signals({Timeframe.H4: bars(Timeframe.H4, STREAK, MON)}, 0.01) == []  # ATR(14): NaN
    friday_late = MON + 4 * DAY + 23 * 60 + 52 - 3 * 240  # the streak's last bar closes Fri 23:52
    h4 = bars(Timeframe.H4, STREAK[3:], friday_late)
    assert CandleStreak(StreakParams(atr_period=1)).signals({Timeframe.H4: h4}, 0.01) == []


# ---------------------------------------------------------------- Fibonacci pullback

# a swing high 105 (bar 1), a swing low 90 (bar 3, confirmed on bar 4), a swing high 110 (bar 6, confirmed on
# bar 7): the long leg 90 -> 110 gives a limit at 110 - 0.618 x 20 = 97.64 (ATR(1) = each bar's TR, k = 1)
FIB = [(100, 101, 99, 100), (100, 105, 100, 104), (104, 104, 95, 96), (95, 95.5, 90, 91), (91, 99, 91, 98),
       (98, 105, 97, 104), (104, 110, 103, 109), (109, 109, 104, 105)]  # fmt: skip


def test_fib_pullback_sets_a_limit_on_a_confirmed_leg() -> None:
    h1 = bars(Timeframe.H1, FIB, MON)
    strategy = FibPullback(FibParams(zigzag_k=1.0, atr_period=1, ema_period=3))
    (sig,) = strategy.signals({Timeframe.H1: h1}, 0.01)  # the short at bar 4 closes above its EMA: filtered
    assert (sig.side, sig.decided_at) == (Side.BUY, MON + 8 * 60)
    assert sig.limit_price == pytest.approx(97.64)
    assert (sig.stop_level, sig.take_profit_level, sig.cancel_beyond) == (90.0, pytest.approx(122.36), 110.0)
    assert (sig.limit_expires_at, sig.max_hold) == (MON + 8 * 60, 200 * 60)
    assert strategy.params()["timeframe"] == "H1"


def test_fib_pullback_needs_the_trend_and_no_chasing() -> None:
    h1 = bars(Timeframe.H1, FIB, MON)
    assert (
        FibPullback(FibParams(zigzag_k=1.0, atr_period=1, ema_period=200)).signals({Timeframe.H1: h1}, 1)
        == []
    )
    chased = [*FIB[:-1], (109, 109, 97, 97.5)]  # confirms the high but closes below the entry
    h1 = bars(Timeframe.H1, chased, MON)
    assert (
        FibPullback(FibParams(zigzag_k=1.0, atr_period=1, ema_period=3)).signals({Timeframe.H1: h1}, 1) == []
    )
    assert FibPullback().signals({}, 0.01) == []


def test_fib_pullback_short_leg() -> None:
    mirror = [(200 - o, 200 - lo, 200 - h, 200 - c) for o, h, lo, c in FIB]
    h1 = bars(Timeframe.H1, mirror, MON)
    (sig,) = FibPullback(FibParams(zigzag_k=1.0, atr_period=1, ema_period=3)).signals({Timeframe.H1: h1}, 1)
    assert (sig.side, sig.limit_price, sig.stop_level) == (Side.SELL, pytest.approx(102.36), 110.0)


# ---------------------------------------------------------------- SessionSweepPinBar FOLLOW

RANGE = [(100, 101, 99, 100)] * 4  # 14:00-14:45: high 101, low 99


def pin_data(pin: Row, hour: int = 15) -> dict[Timeframe, Candles]:
    day = MON + DAY
    return {
        Timeframe.M15: bars(Timeframe.M15, RANGE, day + (hour - 1) * 60),
        Timeframe.H1: bars(Timeframe.H1, [pin], day + hour * 60),
    }


def test_is_pin() -> None:
    assert is_pin(100.6, 102, 100.4, 100.5, at_low=False, ratio=2, zone=0.3)
    assert not is_pin(100, 100, 100, 100, at_low=True, ratio=2, zone=0.3)


def test_sweep_pin_follows_an_upthrust_and_a_spring() -> None:
    strategy = SweepPinFollow(PinParams(rolling_hours=1, atr_period=1))
    (up,) = strategy.signals(pin_data((100.6, 102, 100.4, 100.5)), 0.01)  # swept 101, bearish pin -> BUY
    assert (up.side, up.decided_at) == (Side.BUY, MON + DAY + 16 * 60)
    assert up.stop_level == pytest.approx(100.39)
    assert up.stop_floor == pytest.approx(1.5 * 1.6)
    assert (up.take_profit_r, up.max_hold, up.flats) == (2.0, 240, (FlatRule(1410),))
    (down,) = strategy.signals(pin_data((99.4, 99.6, 98, 99.5)), 0.01)  # swept 99, bullish pin -> SELL
    assert (down.side, down.stop_level) == (Side.SELL, pytest.approx(99.61))


def test_sweep_pin_needs_the_window_the_range_and_a_pin() -> None:
    strategy = SweepPinFollow(PinParams(rolling_hours=1, atr_period=1))
    assert strategy.signals(pin_data((100.6, 102, 100.4, 100.5), hour=20), 0.01) == []
    assert strategy.signals(pin_data((100, 102, 100, 101.9)), 0.01) == []  # a marubozu, not a pin
    thin = {Timeframe.H1: pin_data((100.6, 102, 100.4, 100.5))[Timeframe.H1],
            Timeframe.M15: bars(Timeframe.M15, RANGE[:1], MON + DAY + 14 * 60)}  # fmt: skip
    assert strategy.signals(thin, 0.01) == []  # 15 of 60 minutes covered
    assert SweepPinFollow().signals(pin_data((100.6, 102, 100.4, 100.5)), 0.01) == []  # ATR(14): NaN
    assert SweepPinFollow().signals({}, 0.01) == []
    assert SweepPinFollow().params()["rolling_hours"] == 8.0


# ---------------------------------------------------------------- SessionSweepRejection FOLLOW


def rejection_data(
    sweep: Sequence[Row], trend_closes: Sequence[float] = (90, 100)
) -> dict[Timeframe, Candles]:
    day = MON + 2 * DAY
    range_bars = [(100, 101, 99, 100.5)] * 62  # 01:00-16:15: high 101, low 99
    m15 = bars(Timeframe.M15, [*range_bars, *sweep], day + 60)
    d1 = bars(Timeframe.D1, [(c, c, c, c) for c in trend_closes], day - len(trend_closes) * DAY)
    return {Timeframe.M15: m15, Timeframe.D1: d1}


def test_sweep_rejection_follows_an_upthrust_with_the_trend() -> None:
    strategy = SweepRejectionFollow(RejectionParams(atr_period=1))
    (sig,) = strategy.signals(rejection_data([(100.5, 101.5, 100.4, 100.8)]), 0.01)
    assert (sig.side, sig.decided_at) == (Side.BUY, MON + 2 * DAY + 16 * 60 + 45)
    assert sig.stop_level == pytest.approx(100.4 - 0.3 * 1.1)  # ATR(1) = TR 1.1
    assert (sig.stop_floor, sig.stop_cap, sig.cap_skips) == (pytest.approx(0.275), pytest.approx(3.3), True)
    assert (
        strategy.signals(rejection_data([(100.5, 101.5, 100.4, 100.8)], (100, 90)), 0.01) == []
    )  # downtrend


def test_sweep_rejection_spring_in_a_downtrend_sells() -> None:
    strategy = SweepRejectionFollow(RejectionParams(atr_period=1))
    (sig,) = strategy.signals(rejection_data([(99.5, 99.6, 98.5, 99.2)], (100, 90)), 0.01)
    assert sig.side is Side.SELL


@pytest.mark.parametrize(
    "sweep",
    [
        [(100.5, 106, 100.4, 100.8)],  # deeper than 1.5 ATR(10) = 3.54: a breakout
        [(100.5, 101.5, 100.4, 101.3), (101.3, 101.4, 100.6, 100.8)],  # stayed above: the side is disarmed
        [(100.5, 101.1, 100.4, 100.8)],  # too shallow (< 0.1 ATR beyond)
    ],
)
def test_sweep_rejection_state_machine_refuses(sweep: list[Row]) -> None:
    assert SweepRejectionFollow(RejectionParams(atr_period=10)).signals(rejection_data(sweep), 0.01) == []


def test_sweep_rejection_needs_data_and_a_complete_range() -> None:
    data = rejection_data([(100.5, 101.5, 100.4, 100.8)])
    assert SweepRejectionFollow().signals({Timeframe.M15: data[Timeframe.M15]}, 0.01) == []
    thin = {**data, Timeframe.M15: bars(Timeframe.M15, [(100.5, 101.5, 100.4, 100.8)], MON + 2 * DAY + 990)}
    assert SweepRejectionFollow(RejectionParams(atr_period=1)).signals(thin, 0.01) == []
    first_day = {**data, Timeframe.D1: bars(Timeframe.D1, [], MON)}
    assert SweepRejectionFollow(RejectionParams(atr_period=1)).signals(first_day, 0.01) == []  # trend unknown
    assert SweepRejectionFollow().params()["signal_tf"] == "M15"


# ---------------------------------------------------------------- SpikeFade


def spike_series(start: int, drop: float = 1.0) -> Candles:
    """80 quiet bars, then one bar falling ``drop``: the only shock, judged when the next bar opens (81)."""
    closes = [100 + 0.01 * (i % 3) for i in range(80)] + [100 - drop, 100 - drop]
    return bars(Timeframe.M1, [(c, c + 0.01, c - 0.01, c) for c in closes], start, spread=0.2)


def test_spike_fade_buys_after_a_shock_drop() -> None:
    p = SpikeParams(vol_bars=50, min_vol_bars=40)
    start = MON + DAY + 10 * 60  # Tuesday 10:00, no blackout
    (sig,) = SpikeFade(p).signals({Timeframe.M1: spike_series(start)}, 0.01)
    assert (sig.side, sig.decided_at) == (Side.BUY, start + 81)
    assert sig.stop_distance == pytest.approx(1.01)  # |99 - 100.01|
    assert (sig.max_hold, sig.flats, sig.max_entry_delay) == (30, (FlatRule(1410, weekday=4),), 0)
    (small,) = SpikeFade(p).signals({Timeframe.M1: spike_series(start, drop=0.3)}, 0.01)
    assert small.stop_distance == 0.5  # the $0.50 minimum
    (up,) = SpikeFade(p).signals({Timeframe.M1: spike_series(start, drop=-1.0)}, 0.01)
    assert up.side is Side.SELL


def test_spike_fade_respects_the_clock_and_the_spread() -> None:
    p = SpikeParams(vol_bars=50, min_vol_bars=40)
    for decision in (MON + DAY + 15 * 60 + 30, MON + 4 * DAY + 21 * 60, MON + DAY + 23 * 60 + 10):
        assert SpikeFade(p).signals({Timeframe.M1: spike_series(decision - 81)}, 0.01) == []
    assert SpikeFade(p).signals({Timeframe.M1: spike_series(MON + DAY + 10 * 60, drop=0.0)}, 0.01) == []
    wide = SpikeParams(vol_bars=50, min_vol_bars=40, max_spread=0.1)
    assert SpikeFade(wide).signals({Timeframe.M1: spike_series(MON + DAY + 10 * 60)}, 0.01) == []
    assert SpikeFade().signals({}, 0.01) == []
    flat = bars(Timeframe.M1, [(100, 100, 100, 100)] * 60, MON + DAY + 600)
    assert SpikeFade(p).signals({Timeframe.M1: flat}, 0.01) == []  # zero variance
    assert SpikeFade().params()["shock_sigma"] == 5.0


# ---------------------------------------------------------------- ICT displacement MSS

# swing low 98 (bar 1, confirmed on bar 2), leg high 105 (bar 3), a displacement bar 4 (body 4.6) and bar 5
# closing 97.5 below the swing with its high 100.3 under bar 3's low 102: the fair value gap -> SELL
ICT = [(100, 101, 99, 100), (100, 100.5, 98, 99), (99, 103, 98.5, 102.5), (102.5, 105, 102, 104.8),
       (104.8, 104.9, 100, 100.2), (100.2, 100.3, 97, 97.5)]  # fmt: skip
ICT_P = DmssParams(swing_strength=1, atr_period=1, displacement_atr=0.1, min_risk=0.0, max_risk=1000.0,
                   stop_buffer=0.5)  # fmt: skip


def test_ict_dmss_sells_a_bearish_shift_with_displacement() -> None:
    m30 = bars(Timeframe.M30, ICT, MON + DAY + 9 * 60)
    (sig,) = IctDmss(ICT_P).signals({Timeframe.M30: m30}, 0.01)
    assert (sig.side, sig.decided_at, sig.stop_level) == (Side.SELL, MON + DAY + 12 * 60, 105.5)
    assert (sig.take_profit_level, sig.flats) == (None, (FlatRule(22 * 60 + 55),))
    c1b = IctDmss(DmssParams(**{**ICT_P.asdict(), "target_r": 3.0})).signals({Timeframe.M30: m30}, 0.01)
    assert c1b[0].take_profit_level == pytest.approx(97.5 - 3 * 8.0)


def test_ict_dmss_mirrors_for_a_bullish_shift() -> None:
    mirror = [(200 - o, 200 - lo, 200 - h, 200 - c) for o, h, lo, c in ICT]
    (sig,) = IctDmss(ICT_P).signals({Timeframe.M30: bars(Timeframe.M30, mirror, MON + DAY + 9 * 60)}, 0.01)
    assert (sig.side, sig.stop_level) == (Side.BUY, 94.5)


def test_ict_dmss_refuses_outside_the_window_or_risk_band() -> None:
    late = bars(Timeframe.M30, ICT, MON + DAY + 20 * 60)  # bars 4-5 open at 22:00 and later
    assert IctDmss(ICT_P).signals({Timeframe.M30: late}, 0.01) == []
    tight = DmssParams(**{**ICT_P.asdict(), "max_risk": 5.0})
    assert IctDmss(tight).signals({Timeframe.M30: bars(Timeframe.M30, ICT, MON + DAY + 9 * 60)}, 0.01) == []
    no_gap = [*ICT[:5], (100.2, 102.5, 97, 97.5)]  # high above bar 3's low: no fair value gap, MSS waits
    assert (
        IctDmss(ICT_P).signals({Timeframe.M30: bars(Timeframe.M30, no_gap, MON + DAY + 9 * 60)}, 0.01) == []
    )
    assert IctDmss().signals({}, 0.01) == []
    assert IctDmss().params()["leg_lookback"] == 8


def test_fib_pullback_short_guards() -> None:
    # 20 quiet bars at 50 keep the EMA(20) far below, so the chased close passes the trend filter
    quiet = [(50, 50.1, 49.9, 50)] * 20
    chased = [*quiet, *FIB[:-1], (109, 109, 97, 97.5)]  # confirms 90 -> 110 but closes below 97.64
    strategy = FibPullback(FibParams(zigzag_k=1.0, atr_period=1, ema_period=20))
    assert strategy.signals({Timeframe.H1: bars(Timeframe.H1, chased, MON)}, 1) == []
    # an outside bar confirms its own high (k 0.5); the next bar confirms a low on that same index: no leg
    outside = [(100, 100, 100, 100), (100, 106, 94, 95), (95, 101, 95, 100)]
    loose = FibPullback(FibParams(zigzag_k=0.5, atr_period=1))
    assert loose.signals({Timeframe.H1: bars(Timeframe.H1, outside, MON)}, 1) == []


def test_sweep_rejection_waits_for_the_reclaim_window() -> None:
    p = RejectionParams(atr_period=10, reclaim_bars=2)
    data = rejection_data([(100.5, 101.6, 100.4, 101.3), (101.3, 101.7, 100.6, 100.8)])
    (sig,) = SweepRejectionFollow(p).signals(data, 0.01)  # swept and held, back inside on the second bar
    assert (sig.side, sig.decided_at) == (Side.BUY, MON + 2 * DAY + 17 * 60)


def test_ict_dmss_drops_a_shift_without_displacement_in_time() -> None:
    no_gap = [
        *ICT[:5],
        (100.2, 102.5, 97, 97.5),
        (97.5, 100.5, 96, 97),
        (97, 98, 95, 96),
        (96, 96.5, 94.5, 95),
    ]
    assert (
        IctDmss(ICT_P).signals({Timeframe.M30: bars(Timeframe.M30, no_gap, MON + DAY + 9 * 60)}, 0.01) == []
    )
