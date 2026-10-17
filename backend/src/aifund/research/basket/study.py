"""Run a basket strategy over exported history and judge it (roadmap 11.4). Research only.

The workshop searched its strategies on 2025-10-01 → 2026-09-30, so ``WORKSHOP_START`` splits every export:
before it is data no search saw — the pre-registered test of the frozen parameters; from it on is the period
the parameters were chosen on — in-sample, reported only for parity with the workshop's numbers.

A strategy with ONE declared variant is judged on the whole clean window, cut into ``folds + 1`` equal
segments for the stability check (there is no selection to pay for). One with several variants goes through
the anchored walk-forward, which chooses in-sample and pays for the choice out-of-sample (``walkforward.py``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np

from aifund.adapters import history_store as hs
from aifund.domain.enums import Timeframe
from aifund.domain.market import SymbolSpec
from aifund.research.basket.candles import Candles, load, server_minute
from aifund.research.basket.harness import BasketResult, outcomes, run
from aifund.research.basket.strategies.base import BasketStrategy
from aifund.research.basket.strategies.candle_streak import CandleStreak
from aifund.research.basket.strategies.fib_pullback import FibPullback
from aifund.research.basket.strategies.ict_dmss import DmssParams, IctDmss
from aifund.research.basket.strategies.index_opening import IndexOpeningS1
from aifund.research.basket.strategies.spike_fade import SpikeFade
from aifund.research.basket.strategies.sweep_pin import SweepPinFollow
from aifund.research.basket.strategies.sweep_rejection import SweepRejectionFollow
from aifund.research.signals import CostModel, SignalOutcome
from aifund.research.walkforward import Fold, WalkForwardResult, boundaries, walk_forward
from aifund.stats import summarize

WORKSHOP_START = datetime(2025, 10, 1, tzinfo=UTC)
PATH_TFS = (Timeframe.H1, Timeframe.M30, Timeframe.M15, Timeframe.M5, Timeframe.M1)  # coarse -> fine
RANGE_TF = Timeframe.M15  # session ranges: highs/lows of aligned windows equal the M1 ones

# name -> declared variants (the first is the default); frozen at the workshop's forward-test values
STRATEGIES: dict[str, dict[str, BasketStrategy]] = {
    "candle_streak": {"frozen": CandleStreak()},
    "fib_pullback": {"frozen": FibPullback()},
    "sweep_pin_follow": {"frozen": SweepPinFollow()},
    "sweep_rejection_follow": {"frozen": SweepRejectionFollow()},
    "spike_fade": {"frozen": SpikeFade()},
    "ict_dmss": {"c1": IctDmss(), "c1b": IctDmss(DmssParams(target_r=3.0))},
    "index_opening_s1": {"frozen": IndexOpeningS1()},
}


def stitch(series: Sequence[Candles]) -> Candles | None:
    """One price path from coarse to fine: each finer series takes over from its first bar on."""
    out: Candles | None = None
    for s in series:  # coarse -> fine
        if len(s) == 0:
            continue
        if out is None:
            out = s
            continue
        cut = out.first_open_at_or_after(int(s.t[0]))
        out = Candles(
            out.tf,
            _cat(out.t[:cut], s.t),
            _cat(out.o[:cut], s.o),
            _cat(out.h[:cut], s.h),
            _cat(out.low[:cut], s.low),
            _cat(out.c[:cut], s.c),
            _cat(out.spread[:cut], s.spread),
        )
    return out


def _cat(a: Any, b: Any) -> Any:
    return np.concatenate([a, b])


@dataclass
class SymbolData:
    symbol: str
    spec: SymbolSpec
    data: dict[Timeframe, Candles]
    path: Candles | None
    path_tfs: list[str] = field(default_factory=list)

    def first_minute(self, needs: Sequence[Timeframe]) -> int | None:
        """The first server minute where every needed series and the path have bars (None: never). A
        manifest's ``start`` is only what was ASKED for; the terminal may hold less."""
        series = [self.data[tf] for tf in needs if tf in self.data] + ([self.path] if self.path else [])
        if not series or any(len(c) == 0 for c in series) or any(tf not in self.data for tf in needs):
            return None
        return max(int(c.t[0]) for c in series)


def load_symbol(
    root: Path, symbol: str, spec: SymbolSpec, offset: int, needs: Sequence[Timeframe]
) -> SymbolData:
    point = float(spec.point)
    wanted = set(needs) | {RANGE_TF, *PATH_TFS}
    data = {
        tf: load(root, symbol, tf, point, offset) for tf in wanted if hs.bars_path(root, symbol, tf).is_file()
    }
    for tf in needs:  # a missing timeframe is built from the finer ones, anchored at server midnight as MT5's
        finer = [
            data[f] for f in PATH_TFS if f in data and f.minutes < tf.minutes and tf.minutes % f.minutes == 0
        ]
        source = stitch(finer)
        if tf not in data and source is not None:
            data[tf] = source.resample(tf)
    path_tfs = [tf for tf in PATH_TFS if tf in data and hs.bars_path(root, symbol, tf).is_file()]
    path = stitch([data[tf] for tf in path_tfs])
    inputs = {tf: c for tf, c in data.items() if tf in needs or tf == RANGE_TF}
    return SymbolData(symbol, spec, inputs, path, [tf.value for tf in path_tfs])


def study(
    strategy: BasketStrategy,
    sd: SymbolData,
    *,
    offset: int,
    costs: CostModel,
    start: datetime,
    end: datetime,
    detector: str,
) -> tuple[list[SignalOutcome], BasketResult | None]:
    if sd.path is None:
        return [], None
    signals = strategy.signals(sd.data, float(sd.spec.tick_size), sd.symbol)
    result = run(
        signals,
        sd.path,
        symbol=sd.symbol,
        start=server_minute(start, offset),
        end=server_minute(end, offset),
        max_trades_per_day=strategy.max_trades_per_day,
    )
    return outcomes(result, spec=sd.spec, costs=costs, offset_minutes=offset, detector=detector), result


def frozen_result(
    outcomes_: Sequence[SignalOutcome], start: datetime, end: datetime, folds: int
) -> WalkForwardResult:
    """A single declared variant: every signal in the window is out-of-sample; ``folds + 1`` equal segments
    for the stability check."""
    edges = boundaries(start, end, folds)
    empty = summarize([])
    parts = [
        Fold(k, a, b, "frozen", empty, [o for o in outcomes_ if a <= o.entry_time < b])
        for k, (a, b) in enumerate(pairwise(edges))
    ]
    every = [o for f in parts for o in f.out_of_sample]
    return WalkForwardResult(
        parts, every, summarize([o.r_net for o in every], times=[o.entry_time for o in every])
    )


def judge(
    variants: Mapping[str, Sequence[SignalOutcome]],
    start: datetime,
    end: datetime,
    *,
    folds: int,
    min_train_signals: int,
) -> WalkForwardResult:
    if len(variants) == 1:
        return frozen_result(next(iter(variants.values())), start, end, folds)
    return walk_forward(variants, start=start, end=end, folds=folds, min_train_signals=min_train_signals)
