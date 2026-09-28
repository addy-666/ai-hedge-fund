"""Bar clock with FakeClock + ReplayFeed: one event per closed bar, never the forming bar, restart-safe."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from aifund.adapters.clock import FakeClock
from aifund.adapters.sim.replay_feed import ReplayFeed
from aifund.domain.enums import Timeframe
from aifund.domain.market import Bar, SymbolSpec
from aifund.market.bar_clock import BarClock, InMemoryCursorStore

T0 = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
M15 = Timeframe.M15
SPEC = SymbolSpec.model_validate(
    dict(
        symbol="X",
        digits=2,
        point="0.01",
        tick_size="0.01",
        tick_value="1",
        contract_size="100",
        volume_min="0.01",
        volume_max="100",
        volume_step="0.01",
        stops_level_points=0,
        freeze_level_points=0,
        filling_mode_flags=2,
        currency_profit="USD",
        currency_margin="USD",
    )
)


def m15_bars(start: datetime, count: int, symbol: str = "X") -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            timeframe=M15,
            time=start + timedelta(minutes=15 * i),
            open=D(100),
            high=D(101),
            low=D(99),
            close=D(100),
            tick_volume=1,
        )
        for i in range(count)
    ]


def setup(
    clock_at: datetime,
    *,
    bars: list[Bar] | None = None,
    store: InMemoryCursorStore | None = None,
    watches: list[tuple[str, Timeframe]] | None = None,
    extra: dict | None = None,
):  # type: ignore[no-untyped-def,type-arg]
    clock = FakeClock(clock_at)
    series = {("X", M15): bars if bars is not None else m15_bars(T0, 40)}
    series.update(extra or {})
    specs = {sym: SPEC.model_copy(update={"symbol": sym}) for sym, _ in series}
    feed = ReplayFeed(series, specs, clock)
    store = store or InMemoryCursorStore()
    clock_ = BarClock(feed, clock, watches or [("X", M15)], store, grace=timedelta(seconds=3))
    return clock_, clock, store


async def test_first_start_initialises_silently_then_emits_each_new_bar_once() -> None:
    bc, clock, store = setup(T0 + timedelta(minutes=30, seconds=10))
    assert (await bc.poll()).events == []  # 08:15 bar already closed before start: not traded
    assert store.cursors[("X", M15)] == T0 + timedelta(minutes=15)
    clock.set(T0 + timedelta(minutes=45, seconds=1))
    assert (await bc.poll()).events == []  # 08:30 bar closed 1 s ago: within grace
    clock.set(T0 + timedelta(minutes=45, seconds=4))
    (event,) = (await bc.poll()).events
    assert (event.bar_time, event.close_time) == (T0 + timedelta(minutes=30), T0 + timedelta(minutes=45))
    assert (await bc.poll()).events == []  # polling again emits nothing


async def test_two_hours_of_polling_every_2s_emits_exactly_8_consecutive_bars() -> None:
    bc, clock, _ = setup(T0 + timedelta(seconds=5))
    await bc.poll()  # initialise
    events = []
    end = T0 + timedelta(hours=2, seconds=5)
    while clock.now() < end:
        await clock.sleep(2)
        events += (await bc.poll()).events
    times = [e.bar_time for e in events]
    assert len(events) == 8
    assert times == [T0 + timedelta(minutes=15 * i) for i in range(8)]


async def test_the_forming_bar_is_never_emitted() -> None:
    bc, clock, _ = setup(T0 + timedelta(minutes=14, seconds=50))
    await bc.poll()
    clock.set(T0 + timedelta(minutes=29, seconds=59))  # 08:15 bar still forming
    (event,) = (await bc.poll()).events
    assert event.bar_time == T0  # only the 08:00 bar, closed at 08:15


async def test_restart_does_not_re_emit_and_continues_with_the_next_bar() -> None:
    store = InMemoryCursorStore()
    bc, clock, _ = setup(T0 + timedelta(minutes=15, seconds=5), store=store)
    await bc.poll()
    clock.set(T0 + timedelta(minutes=30, seconds=5))
    assert len((await bc.poll()).events) == 1
    # process restarts with the persisted cursor
    bc2, clock2, _ = setup(T0 + timedelta(minutes=30, seconds=20), store=store)
    assert (await bc2.poll()).events == []
    clock2.set(T0 + timedelta(minutes=45, seconds=5))
    (event,) = (await bc2.poll()).events
    assert event.bar_time == T0 + timedelta(minutes=30)


@pytest.mark.parametrize(
    ("restart_at", "emitted"),
    [
        (timedelta(minutes=90, seconds=30), True),  # latest bar (09:15) closed 30 s ago: fresh
        (timedelta(minutes=104), True),  # closed 14 min ago: still within one period
        (timedelta(minutes=104, seconds=59), True),
    ],
)
async def test_after_downtime_only_the_latest_bar_is_considered(restart_at: timedelta, emitted: bool) -> None:
    store = InMemoryCursorStore()
    store.save("X", M15, T0)  # last processed: the 08:00 bar
    bc, _, _ = setup(T0 + restart_at, store=store)
    report = await bc.poll()
    assert [e.bar_time for e in report.events] == ([T0 + timedelta(minutes=75)] if emitted else [])


async def test_a_late_bar_after_a_stall_is_skipped_not_traded() -> None:
    store = InMemoryCursorStore()
    store.save("X", M15, T0)
    bars = m15_bars(T0, 3)  # feed stops after the 08:30 bar (closes 08:45)
    bc, _, _ = setup(T0 + timedelta(minutes=45 + 20), bars=bars, store=store)
    report = await bc.poll()
    assert report.events == []
    assert report.skipped_stale_bars == [("X", M15)]
    assert store.cursors[("X", M15)] == T0 + timedelta(minutes=30)


async def test_stale_feed_is_reported_once_and_recovery_is_reported() -> None:
    bars = m15_bars(T0, 4)  # last bar 08:45, closes 09:00
    bc, clock, _ = setup(T0 + timedelta(minutes=60, seconds=5), bars=bars)
    await bc.poll()
    clock.set(T0 + timedelta(minutes=91))  # 31 min since last close > 2 periods
    first = await bc.poll()
    assert first.newly_stale == [("X", M15)]
    assert (await bc.poll()).newly_stale == []  # reported once
    assert bc.stale == {("X", M15)}


async def test_stale_detection_respects_market_hours() -> None:
    feed_clock = FakeClock(T0 + timedelta(minutes=91))
    feed = ReplayFeed({("X", M15): m15_bars(T0, 4)}, {"X": SPEC}, feed_clock)
    bc = BarClock(feed, feed_clock, [("X", M15)], InMemoryCursorStore(), market_open=lambda s, t: False)
    assert (await bc.poll()).newly_stale == []  # weekend: silence is expected


async def test_watches_are_independent_and_errors_are_isolated() -> None:
    other = [b.model_copy(update={"symbol": "Y"}) for b in m15_bars(T0, 40)]
    bc, clock, _ = setup(
        T0 + timedelta(minutes=15, seconds=5),
        watches=[("X", M15), ("Y", M15), ("Z", M15)],
        extra={("Y", M15): other},
    )
    first = await bc.poll()
    assert "no replay data for Z" in first.errors[("Z", M15)]  # reported, not raised
    clock.set(T0 + timedelta(minutes=30, seconds=5))
    report = await bc.poll()
    assert sorted(e.symbol for e in report.events) == ["X", "Y"]


async def test_run_loop_dispatches_events() -> None:
    bc, clock, _ = setup(T0 + timedelta(seconds=5))
    seen = []

    async def on_bar(event):  # type: ignore[no-untyped-def]
        seen.append(event.bar_time)

    stop_at = T0 + timedelta(minutes=46)
    await bc.run(on_bar, interval_s=2, should_stop=lambda: clock.now() >= stop_at)
    assert seen == [T0, T0 + timedelta(minutes=15), T0 + timedelta(minutes=30)]
