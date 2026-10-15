"""Loading an export for the basket research and judging a strategy (roadmap 11.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from aifund.adapters import history_store as hs
from aifund.domain.enums import Direction, Timeframe
from aifund.research.basket.candles import Candles
from aifund.research.basket.strategies.candle_streak import CandleStreak
from aifund.research.basket.study import frozen_result, judge, load_symbol, stitch, study
from aifund.research.signals import CostModel
from tests.unit.research.basket.test_harness import MON
from tests.unit.research.test_signals import flat
from tests.unit.risk.test_stops import XAU
from tests.unit.test_stats import T0, outcome


def candles(tf: Timeframe, times: list[int], price: float) -> Candles:
    n = len(times)
    return Candles.of(tf, times, [price] * n, [price] * n, [price] * n, [price] * n)


def test_stitch_hands_over_to_the_finer_series() -> None:
    h1 = candles(Timeframe.H1, [MON, MON + 60, MON + 120], 1.0)
    m15 = candles(Timeframe.M15, [MON + 60, MON + 75], 2.0)
    path = stitch([h1, candles(Timeframe.M30, [], 0.0), m15])
    assert path is not None
    assert list(path.t) == [MON, MON + 60, MON + 75]
    assert list(path.c) == [1.0, 2.0, 2.0]
    assert stitch([]) is None


def test_load_symbol_builds_missing_timeframes_and_the_path(tmp_path: Path) -> None:
    for tf in (Timeframe.M15, Timeframe.H1):
        hs.write_bars(tmp_path, "XAUUSD", tf, flat(tf, days=2))
    sd = load_symbol(tmp_path, "XAUUSD", XAU, 120, (Timeframe.M30, Timeframe.H4))
    assert set(sd.data) == {Timeframe.M15, Timeframe.M30, Timeframe.H4}
    assert len(sd.data[Timeframe.M30]) == len(sd.data[Timeframe.M15]) // 2
    assert sd.path_tfs == ["H1", "M15"]
    assert sd.path is not None
    assert len(sd.path) == len(sd.data[Timeframe.M15])  # M15 takes over from its first bar
    out, result = study(
        CandleStreak(), sd, offset=120, costs=CostModel(), start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2027, 1, 1, tzinfo=UTC), detector="candle_streak@1.1",
    )  # fmt: skip
    assert out == []  # flat bars never make a streak
    assert result is not None
    assert result.signals == 0


def test_study_without_a_path_does_nothing(tmp_path: Path) -> None:
    sd = load_symbol(tmp_path, "XAUUSD", XAU, 0, (Timeframe.H4,))
    assert sd.path is None
    when = datetime(2026, 1, 1, tzinfo=UTC)
    assert study(CandleStreak(), sd, offset=0, costs=CostModel(), start=when, end=when, detector="x") == (
        [],
        None,
    )


def test_a_frozen_strategy_is_judged_on_every_segment() -> None:
    outs = [outcome(d, r) for d, r in ((10, 1.0), (100, -1.0), (200, 2.0), (350, 0.5))]
    end = T0 + timedelta(days=365)
    wf = frozen_result(outs, T0, end, folds=3)
    assert [len(f.out_of_sample) for f in wf.folds] == [1, 1, 1, 1]
    assert [f.chosen for f in wf.folds] == ["frozen"] * 4
    assert (wf.summary.n, wf.positive_fold_share) == (4, 0.75)
    assert judge({"only": outs}, T0, end, folds=3, min_train_signals=1).summary.n == 4
    two = judge({"a": outs, "b": outs}, T0, end, folds=3, min_train_signals=1)
    assert len(two.folds) == 3  # the walk-forward: the first segment only trains
    assert outs[0].direction is Direction.LONG
    assert outs[0].r_net == D("1.0")
