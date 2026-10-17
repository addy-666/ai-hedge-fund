"""What a basket strategy is (roadmap 11.3): frozen parameters in, signals on closed bars out."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, ClassVar, Protocol

from aifund.domain.enums import Timeframe
from aifund.research.basket.candles import Candles
from aifund.research.basket.harness import Signal

FINE = (Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.H1)


class BasketStrategy(Protocol):
    name: ClassVar[str]
    version: ClassVar[str]
    setup_tag: ClassVar[str]
    symbols: ClassVar[tuple[str, ...]]  # the markets the workshop researched it on (the default)
    needs: ClassVar[tuple[Timeframe, ...]]  # timeframes it reads (a missing one = no signals)
    max_trades_per_day: ClassVar[int | None]

    def params(self) -> dict[str, Any]: ...

    def signals(self, data: Mapping[Timeframe, Candles], tick: float, symbol: str = "") -> list[Signal]:
        """Signals on closed bars; ``symbol`` (the broker's) for markets with exchange hours."""
        ...


@dataclass(frozen=True)
class Params:
    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def finest(data: Mapping[Timeframe, Candles], coarsest: Timeframe) -> Candles | None:
    """The finest series loaded, no coarser than ``coarsest`` (session ranges are built from it)."""
    for tf in FINE:
        if tf.minutes > coarsest.minutes:
            return None
        if tf in data and len(data[tf]):
            return data[tf]
    return None


def hhmm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)
