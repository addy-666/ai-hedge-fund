"""Fibonacci trend pullback (workshop ``fib_research``, the recommended "textbook" configuration).

Swings come from an ATR ZigZag on CLOSED H1 bars (Wilder ATR14, reversal k = 1.5 ATR): a swing high is known
once a bar's low falls k × ATR below the running high (mirror for lows), so nothing repaints. When a swing
completes an impulse leg A → B (a low then a high for a long), a limit order goes in at the 61.8% retracement
of the leg, with the stop at 100% (A, the leg's origin) and the target at the 161.8% extension
(B + 0.618 × leg). Only in the direction of the EMA200 (the closing bar on the right side, after 200 bars of
warm-up), and only while price has not already crossed the entry. The order is cancelled when price goes
beyond B or touches the stop before filling, after 40 bars, or when a newer leg replaces it; a filled trade is
closed after 200 bars at the latest (≈ 200 h here; the research found the hold limit never mattered).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from aifund.domain.enums import Side, Timeframe
from aifund.research.basket.candles import Candles, atr_wilder, ema
from aifund.research.basket.harness import Signal
from aifund.research.basket.strategies.base import Params


@dataclass(frozen=True)
class FibParams(Params):
    zigzag_k: float = 1.5
    atr_period: int = 14
    entry_retrace: float = 0.618
    stop_retrace: float = 1.0
    target_retrace: float = -0.618  # beyond B: the 161.8% extension
    ema_period: int = 200
    expiry_bars: int = 40
    max_hold_bars: int = 200


class FibPullback:
    name: ClassVar[str] = "fib_pullback"
    version: ClassVar[str] = "1"
    setup_tag: ClassVar[str] = "fib_pullback"
    symbols: ClassVar[tuple[str, ...]] = ("XAUUSD",)
    needs: ClassVar[tuple[Timeframe, ...]] = (Timeframe.H1,)
    max_trades_per_day: ClassVar[int | None] = None

    def __init__(self, p: FibParams | None = None, timeframe: Timeframe = Timeframe.H1) -> None:
        self.p = p or FibParams()
        self.tf = timeframe

    def params(self) -> dict[str, Any]:
        return {**self.p.asdict(), "timeframe": self.tf.value}

    def signals(self, data: Mapping[Timeframe, Candles], tick: float, symbol: str = "") -> list[Signal]:
        bars = data.get(self.tf)
        if bars is None or len(bars) == 0:
            return []
        p = self.p
        atr = atr_wilder(bars, p.atr_period)
        trend = ema(bars.c, p.ema_period)
        span = bars.span
        out: list[Signal] = []
        up = True  # the ZigZag's current leg direction
        hi, hi_j, lo, lo_j = float(bars.h[0]), 0, float(bars.low[0]), 0
        last_low = last_high = math.nan
        last_low_j = last_high_j = -1
        for j in range(len(bars)):
            a = float(atr[j])
            h, low, c = float(bars.h[j]), float(bars.low[j]), float(bars.c[j])
            new_long = new_short = False
            if up:
                if h > hi:
                    hi, hi_j = h, j
                if low < hi - p.zigzag_k * a:
                    last_high, last_high_j = hi, hi_j
                    up, lo, lo_j = False, low, j
                    new_long = True
            else:
                if low < lo:
                    lo, lo_j = low, j
                if h > lo + p.zigzag_k * a:
                    last_low, last_low_j = lo, lo_j
                    up, hi, hi_j = True, h, j
                    new_short = True
            if not (new_long or new_short):
                continue
            if new_long:
                start, end, d = last_low, last_high, 1
                if math.isnan(start) or last_low_j >= last_high_j:
                    continue
            else:
                start, end, d = last_high, last_low, -1
                if math.isnan(start) or last_high_j >= last_low_j:
                    continue
            if j < p.ema_period or (d == 1 and c <= trend[j]) or (d == -1 and c >= trend[j]):
                continue
            leg = abs(end - start)
            entry = end - d * p.entry_retrace * leg
            if (d == 1 and c <= entry) or (d == -1 and c >= entry):
                continue  # already through the entry: no chasing
            expiry_j = min(j + p.expiry_bars, len(bars) - 1)
            out.append(
                Signal(
                    decided_at=int(bars.t[j]) + span,
                    side=Side.BUY if d == 1 else Side.SELL,
                    setup_tag=self.setup_tag,
                    stop_level=end - d * p.stop_retrace * leg,
                    take_profit_level=end - d * p.target_retrace * leg,
                    limit_price=entry,
                    limit_expires_at=int(bars.t[expiry_j]) + span,
                    cancel_beyond=end,
                    max_hold=p.max_hold_bars * span,
                )
            )
        return out
