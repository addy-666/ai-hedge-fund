"""Equity references for the loss limits: day start, week start, peak — across boundaries, DST and restarts.

The trading day rolls at 17:00 New York: 21:00 UTC in summer, 22:00 UTC in winter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D

from aifund.engine.equity import EquityRefs, EquityTracker
from aifund.risk.limits import drawdown_pct


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


WED = utc(2026, 9, 30, 12, 0)  # trading day started Tue 21:00 UTC; week started Sun 27th 21:00 UTC


def tracker(refs: EquityRefs | None = None, last: str | None = None) -> EquityTracker:
    return EquityTracker("17:00", "America/New_York", refs=refs, last_equity=D(last) if last else None)


def test_first_observation_sets_every_reference() -> None:
    t = tracker()
    state = t.update(D("10000"), WED)
    assert (state.day_start_equity, state.week_start_equity, state.peak_equity) == (D("10000"),) * 3
    assert t.refs.day_start_at == utc(2026, 9, 29, 21, 0)
    assert t.refs.week_start_at == utc(2026, 9, 27, 21, 0)


def test_within_a_day_only_the_peak_moves() -> None:
    t = tracker()
    t.update(D("10000"), WED)
    t.update(D("10300"), utc(2026, 9, 30, 14, 0))
    state = t.update(D("10094"), utc(2026, 9, 30, 16, 0))
    assert (state.day_start_equity, state.peak_equity) == (D("10000"), D("10300"))
    assert drawdown_pct(state) == D("2")  # (10300 - 10094) / 10300 = 2%


def test_the_day_rolls_at_17_new_york_from_the_last_equity_before_it() -> None:
    t = tracker()
    t.update(D("10000"), utc(2026, 9, 30, 20, 59))
    state = t.update(D("9990"), utc(2026, 9, 30, 21, 0))  # a new day; 10 lost in the last minute
    # the new day starts from the higher of the last equity seen and the current one: never forgets a loss
    assert state.day_start_equity == D("10000")
    assert t.refs.day_start_at == utc(2026, 9, 30, 21, 0)
    later = t.update(D("10050"), utc(2026, 10, 1, 21, 0))
    assert later.day_start_equity == D("10050")  # rose meanwhile: the current equity is the higher


def test_winter_days_roll_at_22_utc() -> None:
    t = tracker()
    t.update(D("10000"), utc(2026, 12, 2, 12, 0))
    assert t.update(D("9800"), utc(2026, 12, 2, 21, 30)).day_start_equity == D("10000")  # 16:30 EST: same day
    assert t.update(D("9800"), utc(2026, 12, 2, 22, 0)).day_start_equity == D("9800")  # 17:00 EST: new day


def test_the_week_rolls_on_sunday() -> None:
    t = tracker()
    t.update(D("10000"), utc(2026, 10, 2, 20, 0))  # Friday
    t.update(D("9500"), utc(2026, 10, 2, 20, 30))
    state = t.update(D("9500"), utc(2026, 10, 4, 21, 5))  # Sunday 17:05 New York: a new week
    assert (state.week_start_equity, state.day_start_equity) == (D("9500"), D("9500"))
    assert state.peak_equity == D("10000")  # the peak never resets


def test_a_restart_mid_day_keeps_the_references() -> None:
    refs = EquityRefs(
        day_start_at=utc(2026, 9, 29, 21, 0), day_start_equity=D("10000"),
        week_start_at=utc(2026, 9, 27, 21, 0), week_start_equity=D("10200"), peak_equity=D("10400"),
    )  # fmt: skip
    state = tracker(refs, last="9800").update(D("9700"), WED)
    # the 3% loss since the day started is still visible after the restart
    assert (state.day_start_equity, state.week_start_equity, state.peak_equity) == (
        D("10000"),
        D("10200"),
        D("10400"),
    )


def test_down_across_the_boundary_the_loss_is_not_forgotten() -> None:
    refs = EquityRefs(
        day_start_at=utc(2026, 9, 28, 21, 0), day_start_equity=D("10000"),
        week_start_at=utc(2026, 9, 27, 21, 0), week_start_equity=D("10000"), peak_equity=D("10000"),
    )  # fmt: skip
    # last seen 10000 on Tuesday morning; restarted Wednesday with 9600 after losses while down
    state = tracker(refs, last="10000").update(D("9600"), WED)
    assert state.day_start_equity == D("10000")  # counted against today: conservative
    assert (state.day_start_equity - state.equity) / state.day_start_equity * 100 == D("4")  # a 4% day loss
