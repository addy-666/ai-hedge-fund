"""Daily segments (roadmap 11.12) by hand: continuing vs flipping, stops, the Friday flatten, stress."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D

import numpy as np
import pytest

from aifund.domain.enums import CloseReason, Direction, Side, SwapMode, Timeframe, VirtualStatus
from aifund.research.basket.candles import DAY, Candles, server_minute
from aifund.research.basket.segments import (
    Hold,
    Skip,
    long_closes,
    outcomes,
    play,
    stitched_spans,
    trades_weekends,
)
from aifund.research.basket.swap import SwapModel
from aifund.research.signals import CostModel
from tests.unit.risk.test_stops import XAU

MON = server_minute(datetime(2026, 10, 12, tzinfo=UTC), 0)  # a Monday, 00:00 on the server clock
H = 60
COSTS = CostModel(commission_per_lot=D("4"), slippage_points=10)  # 0.04 a round turn, 0.10 a leg


def hourly(opens: list[float], *, start: int = MON, spread: float = 0.2, low: float = 0.5) -> Candles:
    t = [start + i * H for i in range(len(opens))]
    return Candles.of(
        Timeframe.H1,
        t,
        opens,
        [o + 0.5 for o in opens],
        [o - low for o in opens],
        opens,
        [spread] * len(opens),
    )


def hold(at: int, side: Side, end: int, *, stop: float = 10.0, delay: int = 15) -> Hold:
    return Hold(
        decided_at=at, side=side, stop_distance=stop, end_at=end, setup_tag="t", max_entry_delay=delay
    )


def run(holds: list[Hold], path: Candles, **kw: object) -> list:  # type: ignore[type-arg]
    kw = {"flat_before_long_close": True, **kw}
    return play(holds, path, symbol="XAUUSD", start=0, end=10**9, **kw).segments  # type: ignore[arg-type]


def three(path: Candles, **kw: object) -> list:  # type: ignore[type-arg]
    return run(
        [
            hold(MON, Side.BUY, MON + 2 * H),  # enters at the ask
            hold(MON + 2 * H, Side.BUY, MON + 4 * H),  # carries on: same side, the bar the first exited at
            hold(MON + 4 * H, Side.SELL, MON + 5 * H),  # flips: a real exit and a real entry
        ],
        path,
        **kw,
    )


PATH = hourly([100, 101, 102, 103, 104, 105])


def test_a_continuing_segment_enters_at_the_mark_and_a_flip_pays_the_spread() -> None:
    a, b, c = three(PATH)
    assert (a.entry_price, a.exit_price, a.real_entry, a.real_exit) == (
        pytest.approx(100.2),
        102,
        True,
        False,
    )
    assert (b.entry_price, b.exit_price, b.real_entry, b.real_exit) == (102, 104, False, True)
    assert (c.entry_price, c.exit_price, c.real_entry, c.real_exit) == (104, pytest.approx(105.2), True, True)
    assert [s.exit_reason for s in (a, b, c)] == [CloseReason.TIME_STOP] * 3
    assert [round(s.r_gross, 6) for s in (a, b, c)] == [0.18, 0.2, -0.12]
    # one continuous long from the ask at 100.2 to the bid at 104: the two segments add up to it exactly
    assert (a.r_gross + b.r_gross) * 10 == pytest.approx(104 - 100.2)


def test_costs_are_paid_on_real_legs_only_and_stress_pays_them_at_every_boundary() -> None:
    primary = outcomes(
        play([], PATH, symbol="XAUUSD", start=0, end=1, flat_before_long_close=True), spec=XAU, costs=COSTS,
        swap=None, offset_minutes=0, detector="d",
    )  # fmt: skip
    assert primary == []
    res = play(
        [hold(MON, Side.BUY, MON + 2 * H), hold(MON + 2 * H, Side.BUY, MON + 4 * H),
         hold(MON + 4 * H, Side.SELL, MON + 5 * H)],
        PATH, symbol="XAUUSD", start=0, end=10**9, flat_before_long_close=True,
    )  # fmt: skip
    normal = outcomes(res, spec=XAU, costs=COSTS, swap=None, offset_minutes=0, detector="d")
    assert [float(o.r_net) for o in normal] == [0.168, 0.188, -0.144]  # a leg costs (0.02 + 0.10) / 10
    stressed_res = play(
        [hold(MON, Side.BUY, MON + 2 * H), hold(MON + 2 * H, Side.BUY, MON + 4 * H),
         hold(MON + 4 * H, Side.SELL, MON + 5 * H)],
        PATH, symbol="XAUUSD", start=0, end=10**9, flat_before_long_close=True, stress=True,
    )  # fmt: skip
    assert all(s.real_entry and s.real_exit for s in stressed_res.segments)
    stressed = outcomes(stressed_res, spec=XAU, costs=COSTS, swap=None, offset_minutes=0, detector="d")
    assert [float(o.r_net) for o in stressed] == [0.156, 0.156, -0.144]
    boundary = 0.2 / 10 + 2 * 0.012  # the spread on the carried entry, plus the costs of the two legs
    assert sum(float(o.r_net) for o in normal) - sum(float(o.r_net) for o in stressed) == pytest.approx(
        boundary
    )
    first = normal[0]
    assert (first.direction, first.status, first.tp_distance) == (Direction.LONG, VirtualStatus.EXPIRED, D(0))
    assert first.entry_time == datetime(2026, 10, 12, tzinfo=UTC)


def test_a_stop_ends_the_position_and_the_next_decision_enters_for_real() -> None:
    path = hourly([100, 100, 100, 100], low=1.0)  # every bar trades down to 99
    a, b = run([hold(MON, Side.BUY, MON + 2 * H, stop=0.9),
                hold(MON + 2 * H, Side.BUY, MON + 3 * H, stop=5.0)], path)  # fmt: skip
    assert (a.exit_reason, a.exit_price, a.exit_index) == (CloseReason.SL, pytest.approx(99.3), 0)
    assert a.mae_r == -1.0
    assert (b.real_entry, b.entry_price) == (True, pytest.approx(100.2))
    sell = run([hold(MON, Side.SELL, MON + 3 * H, stop=0.3)], path)[0]
    assert (sell.exit_reason, sell.exit_price) == (CloseReason.SL, pytest.approx(100.3))  # the ask touches it


def week_path(*, weekend: bool) -> Candles:
    """Half-hour bars Thursday 00:00 -> Friday 23:30, then (unless ``weekend``) nothing until Monday."""
    thu = MON + 3 * DAY
    times = list(range(thu, thu + 2 * DAY, 30))
    times += list(range(thu + 2 * DAY, thu + 4 * DAY, 30)) if weekend else []
    times += list(range(MON + 7 * DAY, MON + 8 * DAY, 30))
    n = len(times)
    opens = [100.0 + i * 0.01 for i in range(n)]
    return Candles.of(Timeframe.M30, times, opens, [o + 0.5 for o in opens], [o - 0.5 for o in opens], opens,
                      [0.2] * n)  # fmt: skip


def test_weekday_symbols_are_flat_before_the_weekend_and_enter_on_monday() -> None:
    path = week_path(weekend=False)
    thu, fri, sat = MON + 3 * DAY, MON + 4 * DAY, MON + 5 * DAY
    assert long_closes(path, 24 * 60) == [sat]
    week = 3 * DAY
    a, b = run([hold(thu, Side.BUY, fri, delay=week), hold(fri, Side.BUY, sat, delay=week),
                hold(sat, Side.BUY, MON + 8 * DAY, delay=week)], path)[:2]  # fmt: skip
    assert b.exit_reason is CloseReason.FLATTEN
    assert b.exit_minute == sat - 30  # the flatten 30 minutes before the long close
    assert (a.real_exit, b.real_entry) == (False, False)  # Thursday's carries on into Friday's
    monday = run([hold(sat, Side.BUY, MON + 7 * DAY + 12 * H, delay=week)], path)[0]
    assert (monday.entry_minute, monday.real_entry) == (MON + 7 * DAY, True)
    assert run([hold(sat, Side.BUY, MON + 8 * DAY)], path) == []  # 15 minutes is too late on Monday


def test_no_entry_inside_the_window_before_a_long_close() -> None:
    path = week_path(weekend=False)
    sat = MON + 5 * DAY
    res = play([hold(sat - 30, Side.BUY, sat + 3 * DAY, delay=3 * DAY)], path, symbol="X", start=0, end=10**9,
               flat_before_long_close=True)  # fmt: skip
    assert res.segments == []
    assert res.skipped == {Skip.PRE_CLOSE: 1}


def test_markets_open_on_weekends_hold_through_them() -> None:
    path = week_path(weekend=True)
    assert trades_weekends(path) is False  # one Saturday in two weeks of data is not most weeks
    sat = MON + 5 * DAY
    seg = run([hold(sat - 30, Side.BUY, sat + DAY)], path, flat_before_long_close=False)[0]
    assert (seg.exit_reason, seg.exit_minute) == (CloseReason.TIME_STOP, sat + DAY)


def test_crypto_quotes_on_most_saturdays() -> None:
    t = list(range(MON, MON + 21 * DAY, 4 * 60))
    c = Candles.of(Timeframe.H4, t, [1.0] * len(t), [1.0] * len(t), [1.0] * len(t), [1.0] * len(t))
    assert trades_weekends(c) is True
    assert trades_weekends(Candles.of(Timeframe.H4, [], [], [], [], [])) is False


def test_busy_open_at_end_and_late_entries_are_skipped() -> None:
    res = play([hold(MON, Side.BUY, MON + 3 * H), hold(MON + H, Side.SELL, MON + 4 * H),
                hold(MON + 4 * H, Side.BUY, MON + 9 * H), hold(MON + 20 * H, Side.BUY, MON + 30 * H)],
               PATH, symbol="X", start=0, end=10**9, flat_before_long_close=True)  # fmt: skip
    assert len(res.segments) == 1
    assert res.skipped == {Skip.BUSY: 1, Skip.OPEN_AT_END: 1, Skip.NO_ENTRY: 1}
    assert res.holds == 4


def test_swap_is_charged_on_the_rollovers_held() -> None:
    days = hourly([100.0] * 30, start=MON + 20 * H)  # Monday 20:00 -> Tuesday 01:00 and on
    spec = XAU.model_copy(
        update={"swap_long": D("-100"), "swap_short": D("0"), "swap_mode": SwapMode.POINTS,
                "swap_rollover3days": 3}
    )  # fmt: skip
    res = play([hold(MON + 20 * H, Side.BUY, MON + 30 * H)], days, symbol="X", start=0, end=10**9,
               flat_before_long_close=True)  # fmt: skip
    (o,) = outcomes(
        res, spec=spec, costs=CostModel(), swap=SwapModel.of(spec), offset_minutes=0, detector="d"
    )
    assert float(o.r_gross) == pytest.approx(-0.02)  # the spread: 0.2 over a stop of 10
    assert float(o.r_net) == pytest.approx(-0.02 - 1.0 / 10)  # one night of 100 points of 0.01


def test_stitched_spans_follow_the_finer_series_from_its_first_bar() -> None:
    h1 = Candles.of(Timeframe.H1, [0, 60, 120, 180], [1.0] * 4, [1.0] * 4, [1.0] * 4, [1.0] * 4)
    m5 = Candles.of(Timeframe.M5, [120, 125, 130], [1.0] * 3, [1.0] * 3, [1.0] * 3, [1.0] * 3)
    empty = Candles.of(Timeframe.M1, [], [], [], [], [])
    assert stitched_spans([h1, m5, empty]).tolist() == [60, 60, 5, 5, 5]
    path = Candles.of(Timeframe.H1, [0, 60, 120, 125, 2000], [1.0] * 5, [1.0] * 5, [1.0] * 5, [1.0] * 5)
    spans = np.array([60, 60, 5, 5, 5], dtype=np.int64)
    assert long_closes(path, 1000, spans) == [130]  # with the H1 span it would be 185
    assert long_closes(Candles.of(Timeframe.H1, [0], [1.0], [1.0], [1.0], [1.0]), 10) == []
