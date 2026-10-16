"""The pooled short-horizon momentum study (roadmap 11.15, docs/09 §9). Research only.

Two families, each ONE hypothesis pooled over every symbol of the declared universe (all or none):
``tsmom_daily`` and ``intraday_momentum``, with their declared grids (the first variant is the default the
walk-forward falls back to). Outcomes are segments (``segments.py``); same-day outcomes are clustered by their
decision date (``date_cluster``) for the bootstrap.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from aifund.adapters import history_store as hs
from aifund.domain.enums import Side, Timeframe
from aifund.domain.market import SymbolSpec
from aifund.research.basket.candles import Candles, Ints, load, server_minute
from aifund.research.basket.segments import (
    Hold,
    SegmentResult,
    outcomes,
    play,
    stitched_spans,
    trades_weekends,
)
from aifund.research.basket.strategies.intraday_momentum import IntradayMomentum, IntradayParams, session_for
from aifund.research.basket.strategies.tsmom_daily import TsmomDaily, TsmomParams
from aifund.research.basket.study import PATH_TFS, stitch
from aifund.research.basket.swap import SwapModel, SwapUnavailable
from aifund.research.signals import CostModel, SignalOutcome

TARGET_TRADES_PER_MONTH = 100  # operator, 2026-10-16 (docs/09 §9.3); the report flags a run below it
DAYS_PER_MONTH = 30.44
INTRADAY_PATH = Timeframe.M5  # the export's M1 is capped; the cash-open anchors need a 5-minute grid

FAMILIES: dict[
    str, dict[str, TsmomDaily | IntradayMomentum]
] = {  # declared 2026-10-16; the first is the default
    "tsmom_daily": {
        "d28": TsmomDaily(TsmomParams(lookback_days=28)),
        "d14": TsmomDaily(TsmomParams(lookback_days=14)),
        "d56": TsmomDaily(TsmomParams(lookback_days=56)),
    },
    "intraday_momentum": {
        "close30": IntradayMomentum(IntradayParams(window="close30")),
        "open30": IntradayMomentum(IntradayParams(window="open30")),
        "open60": IntradayMomentum(IntradayParams(window="open60")),
    },
}


def date_cluster(o: SignalOutcome) -> date:
    """Outcomes decided on the same (UTC) date move together: they are one draw in the bootstrap."""
    return o.bar_time.date()


@dataclass
class MomentumData:
    symbol: str
    spec: SymbolSpec
    d1: Candles
    path: Candles
    spans: Ints | None  # per-bar length when the path is stitched from several timeframes
    path_tfs: list[str]
    weekends: bool  # the market quotes on weekends (no flatten before long closes)
    swap: SwapModel | None


def load_momentum(root: Path, symbol: str, spec: SymbolSpec, offset: int, family: str) -> MomentumData | None:
    """One symbol's bars for ``family`` (None: the family does not trade it or its bars are missing). Raises
    ``SwapUnavailable`` for ``tsmom_daily`` without swap in the export: it holds positions overnight."""
    point = float(spec.point)
    if not hs.bars_path(root, symbol, Timeframe.D1).is_file():
        return None
    d1 = load(root, symbol, Timeframe.D1, point, offset)
    if family == "intraday_momentum":
        if session_for(symbol) is None or not hs.bars_path(root, symbol, INTRADAY_PATH).is_file():
            return None
        path = load(root, symbol, INTRADAY_PATH, point, offset)
        swap = _swap_or_none(spec)
        return MomentumData(symbol, spec, d1, path, None, [INTRADAY_PATH.value], trades_weekends(path), swap)
    tfs = [tf for tf in PATH_TFS if hs.bars_path(root, symbol, tf).is_file()]
    series = [load(root, symbol, tf, point, offset) for tf in tfs]
    path_or_none = stitch(series)
    if path_or_none is None:
        return None
    swap = SwapModel.of(spec)
    return MomentumData(
        symbol, spec, d1, path_or_none, stitched_spans(series), [tf.value for tf in tfs],
        trades_weekends(path_or_none), swap,
    )  # fmt: skip


def _swap_or_none(spec: SymbolSpec) -> SwapModel | None:
    try:
        return SwapModel.of(spec)
    except SwapUnavailable:
        return None  # intraday positions are flat before every rollover


def holds(strategy: TsmomDaily | IntradayMomentum, md: MomentumData, force: Side | None = None) -> list[Hold]:
    data = {Timeframe.D1: md.d1}
    if isinstance(strategy, TsmomDaily):
        return strategy.holds(data, force=force)
    return strategy.holds(md.symbol, data, md.path, force=force)


def run(
    strategy: TsmomDaily | IntradayMomentum,
    md: MomentumData,
    *,
    costs: CostModel,
    offset: int,
    start: datetime,
    end: datetime,
    stress: bool = False,
    force: Side | None = None,
) -> tuple[list[SignalOutcome], SegmentResult]:
    """One variant on one symbol over [start, end): outcomes net of costs and swap."""
    result = play(
        holds(strategy, md, force),
        md.path,
        symbol=md.symbol,
        start=server_minute(start, offset),
        end=server_minute(end, offset),
        flat_before_long_close=not md.weekends,
        stress=stress,
        spans=md.spans,
    )
    detector = f"{strategy.name}@{strategy.version}"
    outs = outcomes(result, spec=md.spec, costs=costs, swap=md.swap, offset_minutes=offset, detector=detector)
    return outs, result


def pooled(
    strategy: TsmomDaily | IntradayMomentum,
    universe: Sequence[MomentumData],
    *,
    costs: CostModel,
    offset: int,
    start: datetime,
    end: datetime,
    stress: bool = False,
    force: Side | None = None,
) -> list[SignalOutcome]:
    """Every symbol's outcomes, in entry-time order."""
    out: list[SignalOutcome] = []
    for md in universe:
        out += run(
            strategy, md, costs=costs, offset=offset, start=start, end=end, stress=stress, force=force
        )[0]
    return sorted(out, key=lambda o: (o.entry_time, o.symbol))


def per_month(n: int, start: datetime, end: datetime) -> float:
    return n / max((end - start) / timedelta(days=1), 1.0) * DAYS_PER_MONTH


def clusters_per_year(outcomes_: Sequence[SignalOutcome]) -> float:
    """Distinct decision dates per year over the span the outcomes cover."""
    days = {date_cluster(o) for o in outcomes_}
    if len(days) < 2:
        return 0.0
    span = (max(days) - min(days)).days + 1
    return len(days) / span * 365.25
