"""Feature snapshot builder (docs/03 §5, docs/02 §3).

Turns CLOSED bars of every timeframe in a symbol's profile into one immutable ``FeatureSnapshot``. The
builder validates its inputs before computing anything and raises ``SnapshotError`` with a reason code
(the pipeline records it as the decision's outcome — no LLM call, no order):

- INSUFFICIENT_BARS — fewer than ``min_bars`` bars on any timeframe;
- FORMING_BAR       — a bar that had not closed at ``as_of`` (a lookahead bug upstream);
- STALE_DATA        — unordered/duplicate bars, a gap longer than ``max_gap``, or a trigger bar that
                      closed too long before ``as_of``.

NaN (indicator warm-up, division by a zero range) becomes ``None`` so nothing unrepresentable reaches the
database or a prompt.
"""

from __future__ import annotations

import math
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise

import numpy as np

from aifund.domain.decision import FeatureSnapshot, FeatureValue
from aifund.domain.enums import EmaStack, ReasonCode, Timeframe
from aifund.domain.market import Bar, Tick
from aifund.market import indicators as ind
from aifund.market.feature_registry import FEATURE_SET_VERSION, tf_prefix
from aifund.market.regime import classify_regime


class SnapshotError(Exception):
    def __init__(self, reason: ReasonCode, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PortfolioContext:
    """Account state at decision time (filled by the pipeline from Phase 2; optional before)."""

    open_positions_count: int
    symbol_open_risk_pct: float
    portfolio_heat_pct: float
    consecutive_losses_symbol: int
    drawdown_pct: float


def session_of(moment: datetime) -> str:
    """UTC session buckets: ASIA 23-07, LONDON 07-12, OVERLAP 12-16, NY 16-21, OFF 21-23."""
    hour = moment.hour
    if hour >= 23 or hour < 7:
        return "ASIA"
    if hour < 12:
        return "LONDON"
    if hour < 16:
        return "OVERLAP"
    if hour < 21:
        return "NY"
    return "OFF"


def _clean(value: float | None) -> float | None:
    if value is None:
        return None
    v = float(value)
    return None if math.isnan(v) or math.isinf(v) else v


def _ratio(num: float, den: float) -> float | None:
    if den == 0 or math.isnan(den) or math.isnan(num):
        return None
    return _clean(num / den)


def _validate(tf: Timeframe, bars: list[Bar], as_of: datetime, min_bars: int, max_gap: timedelta) -> None:
    if len(bars) < min_bars:
        raise SnapshotError(ReasonCode.INSUFFICIENT_BARS, f"{tf}: {len(bars)} bars < {min_bars}")
    span = timedelta(minutes=tf.minutes)
    times = [b.time for b in bars]
    for prev, cur in pairwise(times):
        if cur <= prev:
            raise SnapshotError(ReasonCode.STALE_DATA, f"{tf}: bars not strictly increasing at {cur}")
        if cur - prev > max_gap:
            raise SnapshotError(ReasonCode.STALE_DATA, f"{tf}: gap of {cur - prev} before {cur}")
    if times[-1] + span > as_of:
        raise SnapshotError(ReasonCode.FORMING_BAR, f"{tf}: bar {times[-1]} closes after {as_of}")


def _stack(e20: float, e50: float, e200: float) -> str | None:
    if any(math.isnan(v) for v in (e20, e50, e200)):
        return None
    if e20 > e50 > e200:
        return EmaStack.BULL.value
    if e20 < e50 < e200:
        return EmaStack.BEAR.value
    return EmaStack.MIXED.value


def _bars_since_swing_break(close: np.ndarray, sh: np.ndarray, sl: np.ndarray) -> int | None:
    for back, i in enumerate(range(len(close) - 1, 0, -1)):
        hi, lo = sh[i - 1], sl[i - 1]
        if (not math.isnan(hi) and close[i] > hi) or (not math.isnan(lo) and close[i] < lo):
            return back
    return None


def tf_features(bars: list[Bar]) -> dict[str, FeatureValue]:
    """Per-timeframe features at the last bar, keyed by base name (no timeframe prefix)."""
    o = np.array([float(b.open) for b in bars])
    h = np.array([float(b.high) for b in bars])
    lo = np.array([float(b.low) for b in bars])
    c = np.array([float(b.close) for b in bars])
    v = np.array([float(b.tick_volume) for b in bars])

    e20, e50, e200 = ind.ema(c, 20), ind.ema(c, 50), ind.ema(c, 200)
    stoch_k, stoch_d = ind.slow_stochastic(h, lo, c)
    atr = ind.atr(h, lo, c, 14)
    rsi = ind.rsi(c, 14)
    sh, sl = ind.confirmed_swings(h, lo)
    a = float(atr[-1])
    close = float(c[-1])
    rng = float(h[-1] - lo[-1])
    body = abs(float(c[-1] - o[-1]))
    body_ratio = _ratio(body, rng)
    prev_vol = float(v[-21:-1].mean()) if len(v) > 20 else math.nan

    if body_ratio is None:
        candle = None
    elif body_ratio < 0.1:
        candle = "DOJI"
    else:
        candle = "BULL" if c[-1] > o[-1] else "BEAR"

    return {
        "dist_ema20_atr": _ratio(close - e20[-1], a),
        "dist_ema50_atr": _ratio(close - e50[-1], a),
        "dist_ema200_atr": _ratio(close - e200[-1], a),
        "ema50_slope_atr": _ratio(float(e50[-1] - e50[-6]), a) if len(e50) > 5 else None,
        "ema_stack": _stack(float(e20[-1]), float(e50[-1]), float(e200[-1])),
        "adx14": _clean(ind.adx(h, lo, c, 14)[-1]),
        "close_above_ema200": None if math.isnan(e200[-1]) else bool(close > e200[-1]),
        "rsi14": _clean(rsi[-1]),
        "rsi14_slope3": _clean(rsi[-1] - rsi[-4]) if len(rsi) > 3 else None,
        # MACD variation below a billionth of price is float noise, not signal
        "macd_hist_z": _clean(ind.zscore(ind.macd_histogram(c), 100, min_std=1e-9 * abs(close))[-1]),
        "atr14": _clean(a),
        "atr14_pct_rank100": _clean(ind.percentile_rank(atr, 100)[-1]),
        "bb_width_pct_rank100": _clean(ind.percentile_rank(ind.bollinger_width(c, 20), 100)[-1]),
        "range_to_atr": _ratio(rng, a),
        "nr7": bool(ind.nr7(h, lo)[-1]),
        "rel_tick_volume20": _ratio(float(v[-1]), prev_vol),
        "dist_swing_high_atr": _ratio(float(sh[-1]) - close, a),
        "dist_swing_low_atr": _ratio(close - float(sl[-1]), a),
        "bars_since_swing_break": _bars_since_swing_break(c, sh, sl),
        "body_to_range": body_ratio,
        "upper_wick_to_range": _ratio(float(h[-1] - max(o[-1], c[-1])), rng),
        "lower_wick_to_range": _ratio(float(min(o[-1], c[-1]) - lo[-1]), rng),
        "candle_dir": candle,
        "close": close,
        "ema50_above_ema200": None if math.isnan(e200[-1]) else bool(e50[-1] > e200[-1]),
        "low_dist_ema50_atr": _ratio(float(lo[-1] - e50[-1]), a),
        "high_dist_ema50_atr": _ratio(float(h[-1] - e50[-1]), a),
        "stoch_k": _clean(stoch_k[-1]),
        "stoch_d": _clean(stoch_d[-1]),
        "stoch_cross_up": _cross(stoch_k, stoch_d, up=True),
        "stoch_cross_down": _cross(stoch_k, stoch_d, up=False),
    }


def _cross(fast: np.ndarray, slow: np.ndarray, *, up: bool) -> bool | None:
    if len(fast) < 2 or np.isnan(fast[-2:]).any() or np.isnan(slow[-2:]).any():
        return None
    if up:
        return bool(fast[-2] <= slow[-2] and fast[-1] > slow[-1])
    return bool(fast[-2] >= slow[-2] and fast[-1] < slow[-1])


def build_snapshot(
    *,
    symbol: str,
    trigger_tf: Timeframe,
    setup_tf: Timeframe,
    context_tfs: list[Timeframe],
    bars: dict[Timeframe, list[Bar]],
    as_of: datetime,
    tick: Tick | None = None,
    portfolio: PortfolioContext | None = None,
    min_bars: int = 300,
    max_gap: timedelta = timedelta(days=5),
    stale_after: timedelta | None = None,
    tf_cache: MutableMapping[tuple[Timeframe, datetime, int], dict[str, FeatureValue]] | None = None,
    news_minutes: tuple[int | None, int | None] = (None, None),
) -> FeatureSnapshot:
    """``tf_cache`` (optional, ONE cache per symbol) reuses a timeframe's features while its window of bars is
    unchanged — keyed by the window's last bar time and length, so values are identical to a fresh build.
    Research uses it: the setup/context timeframes change only once per their own bar. ``news_minutes``:
    (to the next, since the last) HIGH-impact event from the news calendar, when the engine has one."""
    timeframes = [trigger_tf, setup_tf, *context_tfs]
    for tf in timeframes:
        if tf not in bars:
            raise SnapshotError(ReasonCode.INSUFFICIENT_BARS, f"{tf}: no bars supplied")
        _validate(tf, bars[tf], as_of, min_bars, max_gap)

    trigger = bars[trigger_tf]
    trigger_close_time = trigger[-1].time + timedelta(minutes=trigger_tf.minutes)
    limit = stale_after if stale_after is not None else timedelta(minutes=2 * trigger_tf.minutes)
    if as_of - trigger_close_time > limit:
        raise SnapshotError(ReasonCode.STALE_DATA, f"{trigger_tf}: last bar closed at {trigger_close_time}")

    features: dict[str, FeatureValue] = {}
    per_tf: dict[Timeframe, dict[str, FeatureValue]] = {}
    for tf in timeframes:
        key = (tf, bars[tf][-1].time, len(bars[tf]))
        cached = tf_cache.get(key) if tf_cache is not None else None
        per_tf[tf] = cached if cached is not None else tf_features(bars[tf])
        if tf_cache is not None:
            tf_cache[key] = per_tf[tf]
        prefix = tf_prefix(tf)
        features.update({f"{prefix}.{k}": v for k, v in per_tf[tf].items()})

    trig = per_tf[trigger_tf]
    atr_trigger = trig["atr14"]
    close = float(trigger[-1].close)
    setup = per_tf[setup_tf]
    stack_score = {"BULL": 1, "BEAR": -1}
    htf_score = sum(stack_score.get(str(per_tf[tf]["ema_stack"]), 0) for tf in [setup_tf, *context_tfs])

    pdh = pdl = None
    if Timeframe.D1 in bars and isinstance(atr_trigger, float):
        prev_day = bars[Timeframe.D1][-1]  # the last CLOSED day
        pdh = _ratio(float(prev_day.high) - close, atr_trigger)
        pdl = _ratio(close - float(prev_day.low), atr_trigger)

    spread_to_atr = None
    if tick is not None and isinstance(atr_trigger, float):
        spread_to_atr = _ratio(float(tick.spread), atr_trigger)

    adx = setup["adx14"]
    rank = setup["atr14_pct_rank100"]
    stack = setup["ema_stack"]
    features.update(
        {
            "ctx.session": session_of(trigger_close_time),
            "ctx.day_of_week": trigger_close_time.weekday(),
            "ctx.minutes_to_next_high_impact_news": news_minutes[0],
            "ctx.minutes_since_last_high_impact_news": news_minutes[1],
            "ctx.spread_to_atr": spread_to_atr,
            "ctx.regime": classify_regime(
                adx=adx if isinstance(adx, float) else None,
                ema_stack=EmaStack(stack) if isinstance(stack, str) else None,
                atr_pct_rank=rank if isinstance(rank, float) else None,
            ).value,
            "ctx.htf_trend_score": htf_score,
            "ctx.dist_pdh_atr": pdh,
            "ctx.dist_pdl_atr": pdl,
            "ctx.open_positions_count": portfolio.open_positions_count if portfolio else None,
            "ctx.symbol_open_risk_pct": portfolio.symbol_open_risk_pct if portfolio else None,
            "ctx.portfolio_heat_pct": portfolio.portfolio_heat_pct if portfolio else None,
            "ctx.consecutive_losses_symbol": portfolio.consecutive_losses_symbol if portfolio else None,
            "ctx.drawdown_pct": portfolio.drawdown_pct if portfolio else None,
        }
    )
    return FeatureSnapshot(
        symbol=symbol,
        trigger_tf=trigger_tf,
        bar_time=trigger[-1].time,
        feature_set_version=FEATURE_SET_VERSION,
        features=features,
        bars_ref={tf: bars[tf][-1].time for tf in timeframes},
    )
