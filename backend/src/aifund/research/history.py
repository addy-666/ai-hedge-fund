"""Exported bar history held in memory for research, with O(log n) "closed as of" windows.

Same visibility rule as the replay feed: a bar is visible once it has CLOSED (open + timeframe <= moment),
so a study can never see the forming bar or the future.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from aifund.adapters import history_store as hs
from aifund.domain.enums import Timeframe
from aifund.domain.market import Bar, SymbolSpec


@dataclass(frozen=True)
class Series:
    timeframe: Timeframe
    bars: list[Bar]
    opens: list[datetime]
    closes: list[datetime]

    @classmethod
    def of(cls, timeframe: Timeframe, bars: Iterable[Bar]) -> Series:
        ordered = sorted(bars, key=lambda b: b.time)
        span = timedelta(minutes=timeframe.minutes)
        return cls(timeframe, ordered, [b.time for b in ordered], [b.time + span for b in ordered])

    def closed_at(self, moment: datetime, count: int) -> list[Bar]:
        """The last ``count`` bars that had closed at ``moment``."""
        end = bisect.bisect_right(self.closes, moment)
        return self.bars[max(0, end - count) : end]

    def opening_between(self, start: datetime, end: datetime) -> list[Bar]:
        """Bars with open time in [start, end]."""
        return self.bars[bisect.bisect_left(self.opens, start) : bisect.bisect_right(self.opens, end)]

    def last_closed(self, moment: datetime) -> Bar | None:
        end = bisect.bisect_right(self.closes, moment)
        return self.bars[end - 1] if end else None

    @property
    def first_open(self) -> datetime | None:
        return self.opens[0] if self.opens else None

    @property
    def last_close(self) -> datetime | None:
        return self.closes[-1] if self.closes else None


class History:
    def __init__(
        self, series: Mapping[tuple[str, Timeframe], Series], specs: Mapping[str, SymbolSpec]
    ) -> None:
        self._series = dict(series)
        self.specs = dict(specs)

    @classmethod
    def from_bars(
        cls, bars: Mapping[tuple[str, Timeframe], list[Bar]], specs: Mapping[str, SymbolSpec]
    ) -> History:
        return cls({key: Series.of(key[1], b) for key, b in bars.items()}, specs)

    @classmethod
    def load(
        cls,
        root: Path,
        symbols: list[str],
        timeframes: Iterable[Timeframe],
        *,
        references: Iterable[str] = (),
        reference_tf: Timeframe | None = None,
    ) -> History:
        """Read the Parquet export (``scripts/export_history.py``); missing files are simply absent.
        ``references``: cross-asset instruments (roadmap 10.3), read on ``reference_tf`` only; one that was
        never exported is skipped (its features are null, as live)."""
        specs = hs.read_specs(root)
        missing = [s for s in symbols if s not in specs]
        if missing:
            raise hs.HistoryError(f"no specs for {missing} in {root}")
        wanted = {s: set(timeframes) for s in symbols}
        for ref in references:
            if ref in specs and reference_tf is not None:
                wanted.setdefault(ref, set()).add(reference_tf)
        series: dict[tuple[str, Timeframe], Series] = {}
        for symbol, tfs in wanted.items():
            for tf in tfs:
                if hs.bars_path(root, symbol, tf).is_file():
                    series[(symbol, tf)] = Series.of(tf, hs.read_bars(root, symbol, tf, specs[symbol].digits))
        return cls(series, {s: specs[s] for s in wanted})

    def get(self, symbol: str, timeframe: Timeframe) -> Series | None:
        return self._series.get((symbol, timeframe))

    def series(self, symbol: str, timeframe: Timeframe) -> Series:
        found = self.get(symbol, timeframe)
        if found is None:
            raise hs.HistoryError(f"no {timeframe} history for {symbol}")
        return found
