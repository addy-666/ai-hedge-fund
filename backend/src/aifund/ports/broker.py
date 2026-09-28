"""Broker and market-data ports. Implemented by the MT5 gateway (VPS) and the SimBroker (dev/tests).

All timestamps crossing these ports are UTC; adapters convert broker server time at their boundary.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from aifund.domain.enums import Side, Timeframe
from aifund.domain.market import (
    AccountInfo,
    Bar,
    Deal,
    OrderRequest,
    OrderResult,
    Position,
    SymbolSpec,
    Tick,
)


class BrokerError(Exception):
    """The broker (MT5 terminal or SimBroker) returned an error or an unusable result."""


class BrokerUnavailable(BrokerError):
    """The broker is not reachable/connected. Callers pause new exposure; nothing is assumed about orders."""


@runtime_checkable
class MarketDataPort(Protocol):
    async def closed_bars(self, symbol: str, timeframe: Timeframe, count: int) -> list[Bar]:
        """The last ``count`` CLOSED bars, oldest first. Never includes the forming bar (docs/03 §2)."""
        ...

    async def tick(self, symbol: str) -> Tick | None:
        """Latest quote, or None if the symbol has no quote."""
        ...

    async def bars_range(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        """Bars with open time in [start, end] (UTC), oldest first. Callers ask only for the past; the last
        bar may still be forming if ``end`` is now (the trade enricher asks up to a trade's close)."""
        ...


@runtime_checkable
class BrokerPort(Protocol):
    async def account_info(self) -> AccountInfo: ...

    async def symbol_spec(self, symbol: str) -> SymbolSpec: ...

    async def positions(self, symbol: str | None = None) -> list[Position]:
        """Open positions of ALL magics (callers filter); guards must see foreign exposure too."""
        ...

    async def deals_for_position(self, position_id: int) -> list[Deal]: ...

    async def deals_between(self, start: datetime, end: datetime) -> list[Deal]: ...

    async def calc_profit(
        self, side: Side, symbol: str, volume: Decimal, price_open: Decimal, price_close: Decimal
    ) -> Decimal:
        """Profit in account currency for the move, using the broker's own conversion."""
        ...

    async def calc_margin(self, side: Side, symbol: str, volume: Decimal, price: Decimal) -> Decimal: ...

    async def order_check(self, request: OrderRequest) -> OrderResult: ...

    async def order_send(self, request: OrderRequest) -> OrderResult | None:
        """Send an order. ``None`` means the outcome is UNKNOWN (timeout / lost connection): the caller
        must resolve it against positions and deals and must never blindly resend (docs/03 §12.2)."""
        ...
