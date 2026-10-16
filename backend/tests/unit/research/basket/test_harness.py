"""The basket harness's entry, stop, target and exit rules (roadmap 11.2), on hand-built one-minute paths."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from aifund.domain.enums import CloseReason, Direction, Side, Timeframe, VirtualStatus
from aifund.research.basket.candles import Candles, server_minute
from aifund.research.basket.harness import FlatRule, Signal, Skip, Trade, outcomes, run, simulate
from aifund.research.signals import CostModel
from tests.unit.risk.test_stops import XAU

MON = server_minute(datetime(2026, 10, 12, tzinfo=UTC), 0)  # a Monday, 00:00 on the server clock


def path(
    *rows: tuple[float, float, float, float], spread: float = 0.0, start: int = MON, step: int = 1
) -> Candles:
    t = [start + i * step for i in range(len(rows))]
    return Candles.of(
        Timeframe.M1, t, *([r[k] for r in rows] for k in range(4)),
        [spread] * len(rows),
    )  # fmt: skip


def buy(**kw: object) -> Signal:
    return Signal(**{"decided_at": MON, "side": Side.BUY, "setup_tag": "t", **kw})  # type: ignore[arg-type]


def sell(**kw: object) -> Signal:
    return Signal(**{"decided_at": MON, "side": Side.SELL, "setup_tag": "t", **kw})  # type: ignore[arg-type]


def trade(out: Trade | Skip) -> Trade:
    assert isinstance(out, Trade), out
    return out


def test_market_buy_pays_the_spread_and_takes_profit() -> None:
    p = path((100, 101, 99.5, 100.5), (100.5, 102.5, 100.4, 102), spread=0.2)
    t = trade(simulate(buy(stop_distance=1.0, take_profit_r=2.0), p))
    assert t.entry_price == pytest.approx(100.2)
    assert (t.exit_reason, t.exit_price, t.r) == (CloseReason.TP, pytest.approx(102.2), pytest.approx(2.0))
    assert t.mfe_r == pytest.approx(2.3)
    assert t.mae_r == pytest.approx(-0.7)


def test_stop_first_when_one_bar_touches_both() -> None:
    p = path((100, 103, 98, 100))
    t = trade(simulate(buy(stop_distance=1.0, take_profit_r=2.0), p))
    assert (t.exit_reason, t.r) == (CloseReason.SL, pytest.approx(-1.0))


def test_a_gap_through_the_stop_fills_at_the_open() -> None:
    p = path((100, 100.5, 99.5, 100), (97, 97.5, 96, 97))
    t = trade(simulate(buy(stop_distance=1.0), p))
    assert (t.exit_price, t.r) == (97.0, pytest.approx(-3.0))


def test_a_sell_exits_on_the_ask() -> None:
    p = path((100, 100.5, 99.8, 100), (100, 100.85, 99.9, 100.5), spread=0.2)
    t = trade(simulate(sell(stop_distance=1.0), p))  # entry 100 (bid), stop 101 hit by ask high 101.05
    assert (t.exit_reason, t.exit_price, t.r) == (CloseReason.SL, 101.0, pytest.approx(-1.0))
    p = path((100, 100.1, 99, 99.5), spread=0.2)
    assert simulate(sell(stop_distance=1.0, take_profit_r=1.0), p) is Skip.OPEN_AT_END  # ask low 99.2 > 99
    t = trade(simulate(sell(stop_distance=1.0, take_profit_r=0.5), p))
    assert (t.exit_reason, t.exit_price, t.r) == (CloseReason.TP, 99.5, pytest.approx(0.5))


def test_late_or_missing_entry_bars() -> None:
    assert simulate(buy(stop_distance=1.0), path((100, 101, 99, 100), start=MON + 30)) is Skip.NO_ENTRY
    late = path((100, 100.5, 99.5, 100), start=MON + 30)
    assert simulate(buy(stop_distance=1.0, max_entry_delay=30), late) is Skip.OPEN_AT_END  # fmt: skip
    assert simulate(buy(stop_distance=1.0), path()) is Skip.NO_ENTRY


def test_structure_stop_floor_cap_and_bad_levels() -> None:
    p = path((100, 100.1, 99.9, 100), (100, 100.1, 90, 95))
    assert trade(simulate(buy(stop_level=99.8, stop_floor=2.0), p)).stop_distance == 2.0
    assert trade(simulate(buy(stop_level=90.0, stop_cap=3.0), p)).stop_distance == 3.0
    assert simulate(buy(stop_level=90.0, stop_cap=3.0, cap_skips=True), p) is Skip.STOP_TOO_WIDE
    assert simulate(buy(stop_level=101.0), p) is Skip.BAD_STOP
    assert simulate(buy(stop_distance=1.0, take_profit_level=99.0), p) is Skip.BAD_TARGET
    with pytest.raises(ValueError, match="stop_level or stop_distance"):
        simulate(buy(), p)


def test_time_stop_and_session_flat() -> None:
    rows = [(100, 100.2, 99.8, 100)] * 5
    t = trade(simulate(buy(stop_distance=1.0, max_hold=3), path(*rows)))
    assert (t.exit_reason, t.exit_minute) == (CloseReason.TIME_STOP, MON + 3)
    t = trade(simulate(buy(stop_distance=1.0, max_hold=3, flats=(FlatRule(2),)), path(*rows)))
    assert (t.exit_reason, t.exit_minute) == (CloseReason.FLATTEN, MON + 2)
    t = trade(simulate(sell(stop_distance=1.0, flats=(FlatRule(2),)), path(*rows, spread=0.1)))
    assert (t.exit_reason, t.exit_price) == (
        CloseReason.FLATTEN,
        pytest.approx(100.1),
    )  # a sell buys back at ask


def test_flat_rules_find_the_next_time() -> None:
    daily = FlatRule(23 * 60 + 30)
    assert daily.next_after(MON + 600) == MON + 23 * 60 + 30
    assert daily.next_after(MON + 23 * 60 + 45) == MON + 1440 + 23 * 60 + 30
    friday = FlatRule(23 * 60 + 50, weekday=4)
    assert friday.next_after(MON) == MON + 4 * 1440 + 23 * 60 + 50


def test_limit_buy_fills_on_the_ask_without_a_target_on_the_fill_bar() -> None:
    p = path((101, 101.5, 100.5, 101), (100.5, 103, 99.6, 102), (102, 103.5, 101.5, 103), spread=0.1)
    sig = buy(limit_price=99.8, stop_level=98.0, take_profit_level=103.0, cancel_beyond=104.0)
    t = trade(simulate(sig, p))
    assert (t.entry_minute, t.entry_price) == (MON + 1, 99.8)  # ask low 99.7 <= 99.8
    assert (t.exit_minute, t.exit_reason) == (MON + 2, CloseReason.TP)  # not on the fill bar (high 103)


def test_limit_sell_fills_at_the_better_price() -> None:
    p = path((99, 99.5, 98.8, 99.2), (101, 101.2, 100.8, 101), (101, 101, 101, 101))
    t = trade(simulate(sell(limit_price=100.0, stop_level=102.0, max_hold=1), p))
    assert t.entry_price == 101.0  # gapped above the limit: filled at the open


@pytest.mark.parametrize(
    ("rows", "kw", "why"),
    [
        ([(101, 104.5, 100.8, 104)], {}, Skip.CANCELLED),  # beyond B before filling
        ([(101, 101.2, 97.9, 98)], {}, Skip.CANCELLED),  # the stop traded first
        ([(101, 101.2, 100.5, 101)] * 3, {"limit_expires_at": MON + 2}, Skip.CANCELLED),
        ([(101, 101.2, 100.5, 101)] * 3, {}, Skip.NO_ENTRY),
    ],
)
def test_limit_cancellations(
    rows: list[tuple[float, float, float, float]], kw: dict[str, int], why: Skip
) -> None:
    sig = buy(limit_price=99.8, stop_level=98.0, cancel_beyond=104.0, **kw)
    assert simulate(sig, path(*rows)) is why


def test_limit_sell_cancellations_and_replacement() -> None:
    sig = sell(limit_price=100.2, stop_level=102.0, cancel_beyond=96.0)
    assert simulate(sig, path((99, 99.5, 95.5, 96))) is Skip.CANCELLED
    assert simulate(sig, path((99, 102.1, 98.8, 99))) is Skip.CANCELLED
    assert simulate(sig, path((99, 99.5, 98.8, 99), (99, 100.5, 98.9, 100)), until=MON + 1) is Skip.CANCELLED


def test_run_plays_one_position_at_a_time_with_a_daily_cap() -> None:
    rows = [(100, 100.2, 99.8, 100)] * 30
    sigs = [buy(stop_distance=5.0, max_hold=10, decided_at=MON + k) for k in (0, 5, 12, 25)]
    result = run(sigs, path(*rows), symbol="XAUUSD", start=MON, end=MON + 24, max_trades_per_day=1)
    assert result.signals == 3  # the one at +25 is outside [start, end)
    assert [t.entry_minute for t in result.trades] == [MON]
    assert result.skipped == {Skip.BUSY: 1, Skip.DAILY_CAP: 1}
    assert result.resolution is Timeframe.M1
    late = run(
        [buy(stop_distance=5.0, decided_at=MON + 40)], path(*rows), symbol="X", start=MON, end=MON + 99
    )
    assert late.skipped == {Skip.NO_ENTRY: 1}


def test_a_newer_signal_replaces_a_pending_limit() -> None:
    rows = [(101, 101.2, 100.5, 101), (101, 101.2, 100.5, 101), (100, 100.5, 99.5, 100), (100, 100, 100, 100)]
    first = buy(limit_price=99.8, stop_level=98.0, max_hold=1)
    second = buy(limit_price=99.6, stop_level=98.0, decided_at=MON + 1, max_hold=1)
    result = run([first, second], path(*rows), symbol="X", start=MON, end=MON + 9)
    assert result.skipped == {Skip.CANCELLED: 1}
    assert [t.signal for t in result.trades] == [second]


def test_outcomes_in_utc_net_of_costs() -> None:
    rows = [
        (100, 101, 99.5, 100.5),
        (100.5, 102.5, 100.4, 102),
        (102, 102.1, 101.9, 102),
        (102, 102, 102, 102),
    ]
    sigs = [
        buy(stop_distance=1.0, take_profit_r=2.0),
        sell(stop_distance=1.0, max_hold=1, decided_at=MON + 2),
    ]
    result = run(sigs, path(*rows), symbol="XAUUSD", start=MON, end=MON + 3)
    out = outcomes(result, spec=XAU, costs=CostModel(slippage_points=10), offset_minutes=180, detector="x@1")
    win, timed = out
    assert win.entry_time == datetime(2026, 10, 11, 21, tzinfo=UTC)  # 00:00 server on UTC+3
    assert (win.status, win.direction, win.r_gross, win.r_net) == (
        VirtualStatus.CLOSED, Direction.LONG, D("2.0000"), D("1.9000"),
    )  # fmt: skip  # 0.10 slippage at entry, none at a target
    assert (timed.status, timed.direction, timed.exit_reason) == (
        VirtualStatus.EXPIRED, Direction.SHORT, CloseReason.TIME_STOP,
    )  # fmt: skip
