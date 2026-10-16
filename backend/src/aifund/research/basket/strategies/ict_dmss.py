"""ICT displacement market-structure shift (workshop EA ``ICT_DisplacementMSS``, C1 / C1b), NAS100 M30.

Written once for the bearish case; the bullish case runs on mirrored prices (high ↔ −low), as the EA. Within a
server day, a swing low (in side space) is confirmed ``k`` bars later; for bars opening 09:00–22:00 server, a
market-structure shift is a close below the last swing that formed before the highest high of the last ``n``
bars (with the previous close at/above it). Displacement: within the next 3 bars, a fair value gap inside the
leg — a bar whose high is below the low two bars earlier, with the middle bar's body ≥ 0.5 × ATR (the simple
mean of up to 14 true ranges). The bar where the gap appears is the signal: market entry at the next open;
stop 2.0 price units beyond the leg's extreme; skipped when the stop (from the signal close) is below 20 or
above 300, or the spread above 10. C1 holds until the 22:55 flat; C1b also takes profit at 3R from the signal
close. At most two entries per server day; bearish first when both fire on one bar.

The EA replays the day from its start on every bar; the state before a bar never depends on which bar is being
evaluated, so one pass per day per side gives the same signals.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from aifund.domain.enums import Side, Timeframe
from aifund.research.basket.candles import Candles, Floats, atr_partial, day_of, minute_of_day
from aifund.research.basket.harness import FlatRule, Signal
from aifund.research.basket.strategies.base import Params, hhmm


@dataclass(frozen=True)
class DmssParams(Params):
    swing_strength: int = 2
    leg_lookback: int = 8
    displacement_atr: float = 0.5
    atr_period: int = 14
    fvg_wait_bars: int = 3
    window_start: str = "09:00"
    window_end: str = "22:00"
    flat: str = "22:55"
    target_r: float | None = None  # C1: hold to the flat; C1b: 3.0
    stop_buffer: float = 2.0
    min_risk: float = 20.0
    max_risk: float = 300.0
    max_spread: float = 10.0


class IctDmss:
    name: ClassVar[str] = "ict_dmss"
    version: ClassVar[str] = "1.0"
    setup_tag: ClassVar[str] = "ict_dmss"
    symbols: ClassVar[tuple[str, ...]] = ("NAS100.r",)
    needs: ClassVar[tuple[Timeframe, ...]] = (Timeframe.M30,)
    max_trades_per_day: ClassVar[int | None] = 2

    def __init__(self, p: DmssParams | None = None) -> None:
        self.p = p or DmssParams()

    def params(self) -> dict[str, Any]:
        return self.p.asdict()

    def signals(self, data: Mapping[Timeframe, Candles], tick: float, symbol: str = "") -> list[Signal]:
        bars = data.get(Timeframe.M30)
        if bars is None or len(bars) < 2:
            return []
        atr = atr_partial(bars, self.p.atr_period)
        days = [day_of(int(t)) for t in bars.t]
        out: list[Signal] = []
        start = 0
        for i in range(1, len(bars) + 1):
            if i == len(bars) or days[i] != days[start]:
                for d in (-1, 1):  # bearish first, as the EA
                    out += self._day(bars, atr, start, i - 1, d)
                start = i
        out.sort(key=lambda s: s.decided_at)  # stable: bearish before bullish on one bar
        return out

    def _day(self, r: Candles, atr: Floats, ds: int, last: int, d: int) -> list[Signal]:
        p = self.p
        k, lb, fw = p.swing_strength, p.leg_lookback, p.fvg_wait_bars
        ws, we = hhmm(p.window_start), hhmm(p.window_end)

        def sh(i: int) -> float:
            return float(r.h[i]) if d < 0 else -float(r.low[i])

        def slo(i: int) -> float:
            return float(r.low[i]) if d < 0 else -float(r.h[i])

        def so(i: int) -> float:
            return float(r.o[i]) if d < 0 else -float(r.o[i])

        def sc(i: int) -> float:
            return float(r.c[i]) if d < 0 else -float(r.c[i])

        swings: list[tuple[int, float]] = []
        mss, mss_bar, ext, ext_bar = False, -1, 0.0, -1
        out = []
        flats = (FlatRule(hhmm(p.flat)),)
        for b in range(ds, last + 1):
            si = b - k
            if si - k >= ds:
                ls = slo(si)
                if all(slo(q) > ls for q in range(si - k, si)) and all(
                    slo(q) >= ls for q in range(si + 1, b + 1)
                ):
                    swings.append((si, ls))
            if not ws <= minute_of_day(int(r.t[b])) < we:
                mss = False
                continue
            if not mss and b >= 1:
                st = max(ds, b - lb)
                eb = st
                for q in range(st, b + 1):
                    if sh(q) >= sh(eb):
                        eb = q
                if eb < b:
                    prior = [px for idx, px in swings if idx < eb]
                    if prior and sc(b) < prior[-1] <= sc(b - 1):
                        mss, mss_bar, ext, ext_bar = True, b, sh(eb), eb
            if not mss:
                continue
            fb = next(
                (
                    q
                    for q in range(b, ext_bar + 1, -1)
                    if sh(q) < slo(q - 2)
                    and abs(sc(q - 1) - so(q - 1)) >= p.displacement_atr * float(atr[q - 1])
                ),
                -1,
            )
            if fb >= 0:
                entry, stop = sc(b), ext + p.stop_buffer
                risk = stop - entry
                if p.min_risk <= risk <= p.max_risk and float(r.spread[b]) <= p.max_spread:
                    real_entry, real_stop = (entry, stop) if d < 0 else (-entry, -stop)
                    side = Side.SELL if d < 0 else Side.BUY
                    sign = 1 if side is Side.BUY else -1
                    target = None if p.target_r is None else real_entry + sign * p.target_r * risk
                    out.append(
                        Signal(
                            decided_at=int(r.t[b]) + r.span,
                            side=side,
                            setup_tag=self.setup_tag,
                            stop_level=real_stop,
                            take_profit_level=target,
                            flats=flats,
                            max_entry_delay=5,
                        )
                    )
                mss = False
            elif b - mss_bar >= fw:
                mss = False
        return out
