"""Clock-driven replay of historical bars (MarketDataPort for SIM/PAPER and tests).

A bar becomes visible once it has CLOSED at the clock's current time (``open + timeframe <= now``), so
consumers can never see the forming bar or the future. The quote is synthesised from the last closed M1
bar: bid = close, ask = bid + spread (bar spread in points × point), timestamped at the bar's close.
"""

from __future__ import annotations

import bisect
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from aifund.adapters import history_store as hs
from aifund.domain.enums import Timeframe
from aifund.domain.market import Bar, SymbolSpec, Tick
from aifund.ports.broker import BrokerError
from aifund.ports.system import ClockPort

QUOTE_TF = Timeframe.M1


class ReplayFeed:
    def __init__(
        self,
        bars: dict[tuple[str, Timeframe], list[Bar]],
        specs: dict[str, SymbolSpec],
        clock: ClockPort,
        *,
        default_spread_points: int = 0,
    ) -> None:
        self._specs = specs
        self._clock = clock
        self._default_spread = default_spread_points
        self._bars: dict[tuple[str, Timeframe], list[Bar]] = {}
        self._close_times: dict[tuple[str, Timeframe], list[datetime]] = {}
        for key, series in bars.items():
            ordered = sorted(series, key=lambda b: b.time)
            span = timedelta(minutes=key[1].minutes)
            self._bars[key] = ordered
            self._close_times[key] = [b.time + span for b in ordered]

    @classmethod
    def from_history(
        cls, root: Path, symbols: list[str], timeframes: list[Timeframe], clock: ClockPort
    ) -> ReplayFeed:
        specs = hs.read_specs(root)
        missing = [s for s in symbols if s not in specs]
        if missing:
            raise hs.HistoryError(f"no specs for {missing} in {root}")
        bars = {
            (sym, tf): hs.read_bars(root, sym, tf, specs[sym].digits)
            for sym in symbols
            for tf in {*timeframes, QUOTE_TF}
        }
        return cls(bars, {s: specs[s] for s in symbols}, clock)

    @property
    def specs(self) -> dict[str, SymbolSpec]:
        return self._specs

    def symbols(self) -> list[str]:
        return sorted({sym for sym, _ in self._bars})

    def _series(self, symbol: str, timeframe: Timeframe) -> tuple[list[Bar], list[datetime]]:
        key = (symbol, timeframe)
        if key not in self._bars:
            raise BrokerError(f"no replay data for {symbol} {timeframe}")
        return self._bars[key], self._close_times[key]

    def closed_until(self, symbol: str, timeframe: Timeframe, moment: datetime) -> list[Bar]:
        """All bars of the series that had closed at ``moment``."""
        bars, closes = self._series(symbol, timeframe)
        return bars[: bisect.bisect_right(closes, moment)]

    def bars_closing_in(
        self, symbol: str, timeframe: Timeframe, after: datetime, until: datetime
    ) -> list[Bar]:
        """Bars whose close time is in (after, until] — used by the SimBroker to walk price paths."""
        bars, closes = self._series(symbol, timeframe)
        return bars[bisect.bisect_right(closes, after) : bisect.bisect_right(closes, until)]

    # ------------------------------------------------------------------ MarketDataPort

    async def closed_bars(self, symbol: str, timeframe: Timeframe, count: int) -> list[Bar]:
        if count <= 0:
            return []
        return self.closed_until(symbol, timeframe, self._clock.now())[-count:]

    async def tick(self, symbol: str) -> Tick | None:
        return self.tick_at(symbol, self._clock.now())

    def tick_at(self, symbol: str, moment: datetime) -> Tick | None:
        bars, closes = self._series(symbol, QUOTE_TF)
        idx = bisect.bisect_right(closes, moment)
        if idx == 0:
            return None
        bar = bars[idx - 1]
        spec = self._specs[symbol]
        spread_points = bar.spread_points or self._default_spread
        return Tick(
            symbol=symbol,
            time=closes[idx - 1],
            bid=bar.close,
            ask=bar.close + spec.point * Decimal(spread_points),
        )
