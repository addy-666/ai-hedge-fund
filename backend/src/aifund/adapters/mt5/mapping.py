"""Pure conversions between MetaTrader5 objects and domain types. No calls into the terminal.

MT5 returns namedtuples (attribute access) and numpy structured arrays for rates (item access); prices
are C doubles. Doubles are converted through their shortest repr and prices are then quantized to the
symbol's digits, so ``1.0847300000000001`` becomes exactly ``1.08473``.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from aifund.adapters.mt5 import constants as c
from aifund.adapters.mt5.server_time import server_epoch_to_utc
from aifund.domain.enums import DealReason, Timeframe
from aifund.domain.market import (
    AccountInfo,
    Bar,
    Deal,
    OrderRequest,
    OrderResult,
    Position,
    SymbolSpec,
    Tick,
    TradeAction,
)


class MappingError(ValueError):
    """A broker object could not be converted into a valid domain object."""


def dec(value: Any) -> Decimal:
    """Exact decimal from an MT5 double (or numpy scalar) via its shortest repr."""
    return Decimal(repr(float(value)))


def price(value: Any, digits: int) -> Decimal:
    return dec(value).quantize(Decimal(1).scaleb(-digits))


def _opt_price(value: Any, digits: int) -> Decimal | None:
    """MT5 reports 'no SL/TP' as 0.0."""
    return None if float(value) == 0.0 else price(value, digits)


def account_from_mt5(info: Any) -> AccountInfo:
    try:
        return AccountInfo(
            login=int(info.login),
            server=str(info.server),
            trade_mode=c.ACCOUNT_TRADE_MODE[int(info.trade_mode)],
            margin_mode=c.MARGIN_MODE[int(info.margin_mode)],
            currency=str(info.currency),
            leverage=int(info.leverage),
            balance=dec(info.balance),
            equity=dec(info.equity),
            margin=dec(info.margin),
            free_margin=dec(info.margin_free),
            trade_allowed=bool(info.trade_allowed),
            trade_expert=bool(info.trade_expert),
        )
    except (KeyError, ValueError) as exc:
        raise MappingError(f"account_info: {exc}") from exc


def symbol_spec_from_mt5(info: Any) -> SymbolSpec:
    try:
        return SymbolSpec(
            symbol=str(info.name),
            digits=int(info.digits),
            point=dec(info.point),
            tick_size=dec(info.trade_tick_size),
            tick_value=dec(info.trade_tick_value),
            contract_size=dec(info.trade_contract_size),
            volume_min=dec(info.volume_min),
            volume_max=dec(info.volume_max),
            volume_step=dec(info.volume_step),
            stops_level_points=int(info.trade_stops_level),
            freeze_level_points=int(info.trade_freeze_level),
            filling_mode_flags=int(info.filling_mode),
            currency_profit=str(info.currency_profit),
            currency_margin=str(info.currency_margin),
            # Close-only / long-only / short-only / disabled symbols are not open for new entries.
            trade_allowed=int(info.trade_mode) == c.SYMBOL_TRADE_MODE_FULL,
        )
    except ValueError as exc:
        raise MappingError(f"symbol_info({getattr(info, 'name', '?')}): {exc}") from exc


def tick_from_mt5(tick: Any, symbol: str, digits: int, offset: timedelta) -> Tick:
    msc = getattr(tick, "time_msc", 0) or int(tick.time) * 1000
    return Tick(
        symbol=symbol,
        time=server_epoch_to_utc(msc / 1000, offset),
        bid=price(tick.bid, digits),
        ask=price(tick.ask, digits),
    )


def bars_from_mt5(rates: Any, symbol: str, timeframe: Timeframe, digits: int, offset: timedelta) -> list[Bar]:
    """Rates rows (numpy structured array or mappings) → bars, oldest first."""
    try:
        bars = [
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                time=server_epoch_to_utc(int(row["time"]), offset),
                open=price(row["open"], digits),
                high=price(row["high"], digits),
                low=price(row["low"], digits),
                close=price(row["close"], digits),
                tick_volume=int(row["tick_volume"]),
                spread_points=int(row["spread"]),
            )
            for row in rates
        ]
    except (KeyError, ValueError) as exc:
        raise MappingError(f"rates for {symbol} {timeframe}: {exc}") from exc
    bars.sort(key=lambda b: b.time)
    return bars


def position_from_mt5(pos: Any, digits: int, offset: timedelta) -> Position:
    return Position(
        ticket=int(pos.ticket),
        position_id=int(pos.identifier),
        symbol=str(pos.symbol),
        side=c.TYPE_TO_SIDE[int(pos.type)],
        volume=dec(pos.volume),
        price_open=price(pos.price_open, digits),
        sl=_opt_price(pos.sl, digits),
        tp=_opt_price(pos.tp, digits),
        magic=int(pos.magic),
        comment=str(pos.comment),
        time=server_epoch_to_utc((getattr(pos, "time_msc", 0) or int(pos.time) * 1000) / 1000, offset),
        profit=dec(pos.profit),
        swap=dec(pos.swap),
    )


def deal_from_mt5(deal: Any, offset: timedelta) -> Deal | None:
    """Trade deals only; balance/credit/commission/bonus deals (type >= 2) return None."""
    side = c.TYPE_TO_SIDE.get(int(deal.type))
    if side is None:
        return None
    msc = getattr(deal, "time_msc", 0) or int(deal.time) * 1000
    return Deal(
        ticket=int(deal.ticket),
        order=int(deal.order),
        position_id=int(deal.position_id),
        time=server_epoch_to_utc(msc / 1000, offset),
        time_server_epoch=int(deal.time),
        symbol=str(deal.symbol),
        side=side,
        entry=c.DEAL_ENTRY[int(deal.entry)],
        reason=c.DEAL_REASON.get(int(deal.reason), DealReason.OTHER),
        magic=int(deal.magic),
        volume=dec(deal.volume),
        price=dec(deal.price),
        profit=dec(deal.profit),
        commission=dec(deal.commission),
        swap=dec(deal.swap),
        fee=dec(getattr(deal, "fee", 0.0)),
        comment=str(deal.comment),
    )


def order_result_from_mt5(result: Any) -> OrderResult:
    code = int(result.retcode)
    return OrderResult(
        retcode=code,
        retcode_name=c.retcode_name(code),
        order=int(getattr(result, "order", 0) or 0),
        deal=int(getattr(result, "deal", 0) or 0),
        volume=dec(getattr(result, "volume", 0.0) or 0.0),
        price=dec(getattr(result, "price", 0.0) or 0.0),
        comment=str(getattr(result, "comment", "") or ""),
    )


def request_to_mt5(req: OrderRequest) -> dict[str, Any]:
    """Domain request → MT5 request dict. Decimals become floats only here, at the boundary."""
    if req.action is TradeAction.SLTP:
        out: dict[str, Any] = {
            "action": c.TRADE_ACTION_SLTP,
            "symbol": req.symbol,
            "position": req.position_ticket,
            "sl": float(req.sl) if req.sl is not None else 0.0,
            "tp": float(req.tp) if req.tp is not None else 0.0,
            "magic": req.magic,
        }
        return out
    if req.side is None or req.volume is None or req.price is None:  # excluded by OrderRequest validation
        raise MappingError("DEAL request without side/volume/price")
    out = {
        "action": c.TRADE_ACTION_DEAL,
        "symbol": req.symbol,
        "volume": float(req.volume),
        "type": c.SIDE_TO_ORDER_TYPE[req.side],
        "price": float(req.price),
        "deviation": req.deviation_points,
        "magic": req.magic,
        "comment": req.comment,
        "type_time": c.ORDER_TIME_GTC,
    }
    if req.sl is not None:
        out["sl"] = float(req.sl)
    if req.tp is not None:
        out["tp"] = float(req.tp)
    if req.filling is not None:
        out["type_filling"] = c.ORDER_FILLING[req.filling]
    if req.position_ticket is not None:
        out["position"] = req.position_ticket
    return out
