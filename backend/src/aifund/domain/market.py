"""Market data and broker state as the core sees it. Adapters convert broker objects into these types."""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aifund.domain.enums import (
    AccountTradeMode,
    DealEntry,
    DealReason,
    MarginMode,
    Side,
    Timeframe,
)
from aifund.domain.values import UtcDatetime

_FROZEN = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Bar(BaseModel):
    """One OHLC bar. ``time`` is the bar's open time in UTC."""

    model_config = _FROZEN

    symbol: str
    timeframe: Timeframe
    time: UtcDatetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    tick_volume: int = Field(ge=0)
    spread_points: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> Self:
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("inconsistent OHLC: high/low must bound open and close")
        return self


class Tick(BaseModel):
    model_config = _FROZEN

    symbol: str
    time: UtcDatetime
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def _not_crossed(self) -> Self:
        if self.ask < self.bid:
            raise ValueError("crossed quote: ask < bid")
        return self

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def entry_price(self, side: Side) -> Decimal:
        """Price a market order on ``side`` would fill at: buys at the ask, sells at the bid."""
        return self.ask if side is Side.BUY else self.bid


class SymbolSpec(BaseModel):
    """Broker contract specification for one symbol (docs/02 `symbols` table)."""

    model_config = _FROZEN

    symbol: str
    digits: int = Field(ge=0, le=10)
    point: Decimal = Field(gt=0)
    tick_size: Decimal = Field(gt=0)
    tick_value: Decimal = Field(gt=0)
    contract_size: Decimal = Field(gt=0)
    volume_min: Decimal = Field(gt=0)
    volume_max: Decimal = Field(gt=0)
    volume_step: Decimal = Field(gt=0)
    stops_level_points: int = Field(ge=0)
    freeze_level_points: int = Field(ge=0)
    filling_mode_flags: int = Field(ge=0)
    currency_profit: str
    currency_margin: str
    trade_allowed: bool = True

    @model_validator(mode="after")
    def _volumes_consistent(self) -> Self:
        if self.volume_min > self.volume_max:
            raise ValueError("volume_min > volume_max")
        return self


class AccountInfo(BaseModel):
    model_config = _FROZEN

    login: int = Field(gt=0)
    server: str
    trade_mode: AccountTradeMode
    margin_mode: MarginMode
    currency: str
    leverage: int = Field(gt=0)
    balance: Decimal
    equity: Decimal
    margin: Decimal = Field(ge=0)
    free_margin: Decimal
    trade_allowed: bool
    trade_expert: bool


class Position(BaseModel):
    """An open broker position. ``ticket`` is MT5's position identifier."""

    model_config = _FROZEN

    ticket: int = Field(gt=0)
    symbol: str
    side: Side
    volume: Decimal = Field(gt=0)
    price_open: Decimal = Field(gt=0)
    sl: Decimal | None = None
    tp: Decimal | None = None
    magic: int = Field(ge=0)
    comment: str = ""
    time: UtcDatetime
    profit: Decimal = Decimal(0)
    swap: Decimal = Decimal(0)


class Deal(BaseModel):
    """A broker deal (fill). Several deals make up a position's life (docs/03 §14.2)."""

    model_config = _FROZEN

    ticket: int = Field(gt=0)
    order: int = Field(ge=0)
    position_id: int = Field(ge=0)
    time: UtcDatetime
    time_server_epoch: int
    symbol: str
    side: Side
    entry: DealEntry
    reason: DealReason
    magic: int = Field(ge=0)
    volume: Decimal = Field(ge=0)
    price: Decimal = Field(ge=0)
    profit: Decimal = Decimal(0)
    commission: Decimal = Decimal(0)
    swap: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    comment: str = ""

    @property
    def net(self) -> Decimal:
        return self.profit + self.commission + self.swap + self.fee


class OrderResult(BaseModel):
    """Outcome of an order_send / order_check as returned by the broker adapter."""

    model_config = _FROZEN

    retcode: int
    retcode_name: str
    order: int = 0
    deal: int = 0
    volume: Decimal = Decimal(0)
    price: Decimal = Decimal(0)
    comment: str = ""
