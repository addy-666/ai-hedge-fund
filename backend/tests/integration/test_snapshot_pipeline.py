"""Closed bars from the ReplayFeed -> snapshot -> database -> identical snapshot back."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.sim.replay_feed import ReplayFeed
from aifund.domain.enums import Timeframe
from aifund.domain.market import SymbolSpec
from aifund.market.features import build_snapshot
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.market import FeatureSnapshotRepository
from tests.unit.market.test_features import AS_OF, LAST_OPEN, TFS, linear

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


async def test_snapshot_from_replay_sees_only_closed_bars_and_round_trips(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    # 305 bars per timeframe; the extra ones lie in the future of AS_OF or are still forming
    series = {tf: linear(tf, n=300) for tf in TFS}
    for tf in TFS:
        last = series[tf][-1]
        step = last.time - series[tf][-2].time
        series[tf] += [last.model_copy(update={"time": last.time + step * k}) for k in range(1, 6)]
    clock.set(AS_OF)
    feed = ReplayFeed({("X", tf): bars for tf, bars in series.items()}, {"X": SPEC}, clock)
    bars = {tf: await feed.closed_bars("X", tf, 300) for tf in TFS}
    snap = build_snapshot(
        symbol="X",
        trigger_tf=Timeframe.M15,
        setup_tf=Timeframe.H1,
        context_tfs=[Timeframe.H4, Timeframe.D1],
        bars=bars,
        as_of=clock.now(),
    )
    assert snap.bars_ref == LAST_OPEN  # nothing after AS_OF leaked in

    with unit_of_work(factory) as s:
        repo = FeatureSnapshotRepository(s, clock)
        row = repo.get_or_add(snap)
        assert repo.get_or_add(snap).id == row.id  # idempotent per bar
        snapshot_id = row.id
    with factory() as s:
        back = FeatureSnapshotRepository(s, clock).get(snapshot_id)
    assert back == snap  # floats, bools, None and timestamps survive exactly
