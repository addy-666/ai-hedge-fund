"""SessionSweepRejection v1.0, FOLLOW with the daily trend (workshop set "BTCUSD-Follow-Trend-PreNY-M15").

Each server day the range is the high/low of 01:00–16:30 (from the finest bars exported; at least 80% of those
minutes must have data). From 16:30 every closed M15 bar updates a state machine per side, as the EA's
``UpdateSide``: a bar that sweeps the level by ≥ 0.1 × ATR(14, M15) arms the side and tracks the sweep's
extreme; a sweep deeper than 1.5 ATR is a breakout (the side is done for the day); a close back inside on the
same bar is the signal (only the first per side per day); a bar that stays beyond disarms the side. FOLLOW
trades WITH the sweep (an upthrust at the high buys, a spring at the low sells), only in the direction of the
previous closed day's close against its EMA20, and only for bars opening 16:30–20:00. Market entry at the next
bar's open; stop at the signal bar's other end ± 0.3 ATR, at least 0.25 ATR, skipped when wider than 3 ATR;
target 2R; closed after 240 minutes or at the 23:30 flat; at most two entries per server day.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from aifund.domain.enums import Side, Timeframe
from aifund.research.basket.candles import DAY, Candles, Floats, atr_sma, day_of, ema, minute_of_day
from aifund.research.basket.harness import FlatRule, Signal
from aifund.research.basket.strategies.base import Params, finest, hhmm


@dataclass(frozen=True)
class RejectionParams(Params):
    signal_tf: str = "M15"
    range_start: str = "01:00"
    range_end: str = "16:30"
    min_cover_pct: float = 80.0
    trade_start: str = "16:30"
    trade_end: str = "20:00"
    reclaim_bars: int = 1
    min_penetration_atr: float = 0.1
    max_penetration_atr: float = 1.5
    atr_period: int = 14
    trend_ema: int = 20
    stop_buffer_atr: float = 0.3
    stop_floor_atr: float = 0.25
    stop_cap_atr: float = 3.0
    target_r: float = 2.0
    max_hold_min: int = 240
    flat: str = "23:30"


@dataclass
class _Side:
    active: bool = True
    pierced: bool = False
    ext: float = 0.0
    count: int = 0

    def update(
        self, up: bool, o: float, h: float, low: float, c: float, level: float, atr: float, p: RejectionParams
    ) -> bool:
        """``up``: the range high (upthrust); else the range low (spring). True on a valid rejection."""
        if not self.active:
            return False
        if not self.pierced:
            pierce = (
                h > level + p.min_penetration_atr * atr if up else low < level - p.min_penetration_atr * atr
            )
            if pierce:
                self.pierced, self.ext, self.count = True, (h if up else low), 0
        else:
            self.ext = max(self.ext, h) if up else min(self.ext, low)
            self.count += 1
        if not self.pierced:
            return False
        depth = self.ext - level if up else level - self.ext
        if depth > p.max_penetration_atr * atr:
            self.active = self.pierced = False
            return False
        if (c < level) if up else (c > level):
            self.pierced = False
            self.active = False  # first valid sweep only (no wick or body condition in this set)
            return True
        if self.count >= p.reclaim_bars - 1:
            self.active = self.pierced = False
        return False


class SweepRejectionFollow:
    name: ClassVar[str] = "sweep_rejection_follow"
    version: ClassVar[str] = "1.0"
    setup_tag: ClassVar[str] = "sweep_rejection_follow"
    symbols: ClassVar[tuple[str, ...]] = ("BTCUSD",)
    needs: ClassVar[tuple[Timeframe, ...]] = (Timeframe.M15, Timeframe.D1)
    max_trades_per_day: ClassVar[int | None] = 2

    def __init__(self, p: RejectionParams | None = None) -> None:
        self.p = p or RejectionParams()

    def params(self) -> dict[str, Any]:
        return self.p.asdict()

    def signals(self, data: Mapping[Timeframe, Candles], tick: float, symbol: str = "") -> list[Signal]:
        p = self.p
        tf = Timeframe(p.signal_tf)
        bars, days, fine = data.get(tf), data.get(Timeframe.D1), finest(data, tf)
        if bars is None or days is None or fine is None:
            return []
        atr = atr_sma(bars, p.atr_period)
        trend_line = ema(days.c, p.trend_ema)
        rs, re, ts, te = (hhmm(x) for x in (p.range_start, p.range_end, p.trade_start, p.trade_end))
        flats = (FlatRule(hhmm(p.flat)),)
        out: list[Signal] = []
        day = -1
        rng: tuple[float, float] | None = None
        hi_side, lo_side = _Side(), _Side()
        for i in range(len(bars)):
            t = int(bars.t[i])
            if day_of(t) != day:
                day, rng = day_of(t), None
                hi_side, lo_side = _Side(), _Side()
                hl = fine.high_low(day * DAY + rs, day * DAY + re)
                if (
                    hl is not None
                    and hl[2] * fine.span >= p.min_cover_pct / 100.0 * (re - rs)
                    and hl[0] > hl[1]
                ):
                    rng = (hl[0], hl[1])
            mod = minute_of_day(t)
            a = float(atr[i])
            if rng is None or mod < re or math.isnan(a) or a <= 0:
                continue
            o, h, low, c = float(bars.o[i]), float(bars.h[i]), float(bars.low[i]), float(bars.c[i])
            upthrust = hi_side.update(True, o, h, low, c, rng[0], a, p)
            spring = lo_side.update(False, o, h, low, c, rng[1], a, p)
            if not (upthrust or spring) or not ts <= mod < te:
                continue
            trend = self._trend(days, trend_line, t)
            for swept_low in (True, False):  # the spring is evaluated first, as the EA
                if not (spring if swept_low else upthrust):
                    continue
                side = Side.SELL if swept_low else Side.BUY  # FOLLOW
                if trend != (1 if side is Side.BUY else -1):
                    continue
                level = low - p.stop_buffer_atr * a if side is Side.BUY else h + p.stop_buffer_atr * a
                out.append(
                    Signal(
                        decided_at=t + bars.span,
                        side=side,
                        setup_tag=self.setup_tag,
                        stop_level=level,
                        stop_floor=p.stop_floor_atr * a,
                        stop_cap=p.stop_cap_atr * a,
                        cap_skips=True,
                        take_profit_r=p.target_r,
                        max_hold=p.max_hold_min,
                        flats=flats,
                        max_entry_delay=2,
                    )
                )
                break  # one order per bar
        return out

    @staticmethod
    def _trend(days: Candles, line: Floats, minute: int) -> int:
        """+1/-1: the previous CLOSED day's close above/below its EMA; 0 unknown."""
        k = days.first_open_at_or_after(day_of(minute) * DAY) - 1  # the last day opening before today
        if k < 0:
            return 0
        close, value = float(days.c[k]), float(line[k])
        return 1 if close > value else -1 if close < value else 0
