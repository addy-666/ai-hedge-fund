"""SimBroker — an MT5-shaped simulated broker (BrokerPort) driven by a ReplayFeed and a clock.

Model (deliberately conservative, documented so results are not over-trusted):

- Hedging account; market orders only; fills at the synthesised quote (buy at ask, sell at bid) plus an
  adverse slippage of ``slippage_points``. Commission is charged per lot per side.
- Stop-loss / take-profit are evaluated on every M1 bar after the position opened. Bars are bid prices;
  a SELL position's exits are checked against ask = bid + bar spread. If SL and TP are both touched in
  one bar, the SL is assumed to have filled first. A bar that gaps through the SL fills at the bar open
  (worse); TP fills at the TP price (never better).
- Profit uses the symbol's tick value in account currency (constant; no cross-currency drift) and is
  rounded to cents. Swaps are not simulated.
- Validation mirrors MT5 retcodes: volume limits/step (INVALID_VOLUME), stop side & distance
  (INVALID_STOPS), stale/no quote (MARKET_CLOSED), margin (NO_MONEY), price moved beyond deviation
  (REQUOTE), unsupported filling (INVALID_FILL), unknown position (POSITION_CLOSED).
- Fault injection for resilience tests: LOST_ACK (executes, caller gets None), DROPPED (nothing happens,
  caller gets None), RejectWith(retcode), and disconnection (every call raises BrokerUnavailable).

All state changes happen lazily: every port method first walks the price path up to ``clock.now()``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from aifund.adapters.sim.replay_feed import QUOTE_TF, ReplayFeed
from aifund.domain.enums import AccountTradeMode, DealEntry, DealReason, MarginMode, Side
from aifund.domain.market import (
    AccountInfo,
    Deal,
    FillingMode,
    OrderRequest,
    OrderResult,
    Position,
    SymbolSpec,
    Tick,
    TradeAction,
)
from aifund.domain.values import is_multiple_of
from aifund.ports.broker import BrokerError, BrokerUnavailable
from aifund.ports.system import ClockPort

CENT = Decimal("0.01")
OK, DONE = 0, 10009
REQUOTE, INVALID, INVALID_VOLUME, INVALID_STOPS = 10004, 10013, 10014, 10016
TRADE_DISABLED, MARKET_CLOSED, NO_MONEY, FROZEN, INVALID_FILL = 10017, 10018, 10019, 10029, 10030
POSITION_CLOSED, INVALID_CLOSE_VOLUME = 10036, 10038
_NAMES = {
    OK: "OK", DONE: "DONE", REQUOTE: "REQUOTE", INVALID: "INVALID", INVALID_VOLUME: "INVALID_VOLUME",
    INVALID_STOPS: "INVALID_STOPS", TRADE_DISABLED: "TRADE_DISABLED", MARKET_CLOSED: "MARKET_CLOSED",
    NO_MONEY: "NO_MONEY", FROZEN: "FROZEN", INVALID_FILL: "INVALID_FILL", POSITION_CLOSED: "POSITION_CLOSED",
    INVALID_CLOSE_VOLUME: "INVALID_CLOSE_VOLUME",
}  # fmt: skip
FILLING_FLAG = {FillingMode.FOK: 1, FillingMode.IOC: 2}


class Fault(StrEnum):
    LOST_ACK = "LOST_ACK"  # the order executes but the caller gets None (UNKNOWN outcome)
    DROPPED = "DROPPED"  # the order never executes and the caller gets None


@dataclass(frozen=True)
class RejectWith:
    retcode: int


@dataclass(frozen=True)
class SimConfig:
    login: int = 90_000_001
    server: str = "SimBroker"
    currency: str = "USD"
    leverage: int = 100
    starting_balance: Decimal = Decimal("10000")
    commission_per_lot_side: Decimal = Decimal("0")
    slippage_points: int = 0
    max_quote_age: timedelta = timedelta(minutes=5)


@dataclass
class _Pos:
    ticket: int
    symbol: str
    side: Side
    volume: Decimal
    price_open: Decimal
    sl: Decimal | None
    tp: Decimal | None
    magic: int
    comment: str
    time: datetime


@dataclass
class SimBroker:
    feed: ReplayFeed
    clock: ClockPort
    config: SimConfig = field(default_factory=SimConfig)

    def __post_init__(self) -> None:
        self.balance = self.config.starting_balance
        self._positions: dict[int, _Pos] = {}
        self._deals: list[Deal] = []
        self._next_id = 1000
        self._walked_until = self.clock.now()
        self._faults: deque[Fault | RejectWith] = deque()
        self.connected = True
        self.sent: list[OrderRequest] = []

    # ------------------------------------------------------------------ test hooks

    def inject(self, *faults: Fault | RejectWith) -> None:
        """Queue faults consumed by the next order_send calls, in order."""
        self._faults.extend(faults)

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _guard(self) -> None:
        if not self.connected:
            raise BrokerUnavailable("SimBroker disconnected")
        self._walk()

    def _spec(self, symbol: str) -> SymbolSpec:
        spec = self.feed.specs.get(symbol)
        if spec is None:
            raise BrokerError(f"unknown symbol {symbol}")
        return spec

    # ------------------------------------------------------------------ price path & SL/TP

    def _walk(self) -> None:
        now = self.clock.now()
        if now <= self._walked_until:
            return
        for symbol in sorted({p.symbol for p in self._positions.values()}):
            spec = self._spec(symbol)
            for bar in self.feed.bars_closing_in(symbol, QUOTE_TF, self._walked_until, now):
                spread = spec.point * Decimal(bar.spread_points)
                for pos in [p for p in self._positions.values() if p.symbol == symbol and p.time <= bar.time]:
                    self._check_exits(
                        pos, bar.open, bar.high, bar.low, spread, bar.time + timedelta(minutes=1)
                    )
        self._walked_until = now

    def _check_exits(
        self, pos: _Pos, o: Decimal, h: Decimal, low: Decimal, spread: Decimal, when: datetime
    ) -> None:
        if pos.side is Side.BUY:  # exits sell at the bid
            if pos.sl is not None and low <= pos.sl:
                self._close(pos, pos.volume, o if o <= pos.sl else pos.sl, DealReason.SL, when, "sl")
            elif pos.tp is not None and h >= pos.tp:
                self._close(pos, pos.volume, pos.tp, DealReason.TP, when, "tp")
        else:  # exits buy at the ask
            ask_o, ask_h, ask_l = o + spread, h + spread, low + spread
            if pos.sl is not None and ask_h >= pos.sl:
                self._close(pos, pos.volume, ask_o if ask_o >= pos.sl else pos.sl, DealReason.SL, when, "sl")
            elif pos.tp is not None and ask_l <= pos.tp:
                self._close(pos, pos.volume, pos.tp, DealReason.TP, when, "tp")

    # ------------------------------------------------------------------ money

    def _profit(self, side: Side, symbol: str, volume: Decimal, p_open: Decimal, p_close: Decimal) -> Decimal:
        spec = self._spec(symbol)
        direction = 1 if side is Side.BUY else -1
        raw = (p_close - p_open) * direction / spec.tick_size * spec.tick_value * volume
        return raw.quantize(CENT, rounding=ROUND_HALF_EVEN)

    def _margin(self, symbol: str, volume: Decimal, price: Decimal) -> Decimal:
        spec = self._spec(symbol)
        notional = volume * spec.contract_size
        if spec.currency_margin != self.config.currency:
            notional *= price  # base currency priced in the account currency (approximation)
        return (notional / self.config.leverage).quantize(CENT, rounding=ROUND_HALF_EVEN)

    def _commission(self, volume: Decimal) -> Decimal:
        return (-self.config.commission_per_lot_side * volume).quantize(CENT, rounding=ROUND_HALF_EVEN)

    def _exit_price(self, pos: _Pos, tick: Tick) -> Decimal:
        return tick.bid if pos.side is Side.BUY else tick.ask

    def _floating(self) -> Decimal:
        total = Decimal(0)
        for pos in self._positions.values():
            tick = self.feed.tick_at(pos.symbol, self.clock.now())
            if tick is not None:
                total += self._profit(
                    pos.side, pos.symbol, pos.volume, pos.price_open, self._exit_price(pos, tick)
                )
        return total

    def _used_margin(self) -> Decimal:
        return sum(
            (self._margin(p.symbol, p.volume, p.price_open) for p in self._positions.values()), Decimal(0)
        )

    # ------------------------------------------------------------------ deals

    def _record(
        self, pos: _Pos, side: Side, entry: DealEntry, reason: DealReason, volume: Decimal, price: Decimal,
        profit: Decimal, when: datetime, comment: str, order: int,
    ) -> Deal:  # fmt: skip
        commission = self._commission(volume)
        deal = Deal(
            ticket=self._new_id(),
            order=order,
            position_id=pos.ticket,
            time=when,
            time_server_epoch=int(when.timestamp()),
            symbol=pos.symbol,
            side=side,
            entry=entry,
            reason=reason,
            magic=pos.magic,
            volume=volume,
            price=price,
            profit=profit,
            commission=commission,
            comment=comment,
        )
        self._deals.append(deal)
        self.balance += profit + commission
        return deal

    def _close(
        self, pos: _Pos, volume: Decimal, price: Decimal, reason: DealReason, when: datetime, comment: str,
        order: int = 0,
    ) -> Deal:  # fmt: skip
        profit = self._profit(pos.side, pos.symbol, volume, pos.price_open, price)
        deal = self._record(
            pos, pos.side.opposite, DealEntry.OUT, reason, volume, price, profit, when, comment, order
        )
        pos.volume -= volume
        if pos.volume <= 0:
            del self._positions[pos.ticket]
        return deal

    # ------------------------------------------------------------------ validation

    def _stops_ok(
        self, side: Side, sl: Decimal | None, tp: Decimal | None, tick: Tick, spec: SymbolSpec
    ) -> bool:
        level = spec.point * spec.stops_level_points
        if side is Side.BUY:
            return (sl is None or tick.bid - sl >= max(level, spec.point)) and (
                tp is None or tp - tick.bid >= max(level, spec.point)
            )
        return (sl is None or sl - tick.ask >= max(level, spec.point)) and (
            tp is None or tick.ask - tp >= max(level, spec.point)
        )

    def _validate(self, req: OrderRequest) -> tuple[int, Tick | None]:
        spec = self.feed.specs.get(req.symbol)
        if spec is None:
            return INVALID, None
        now = self.clock.now()
        tick = self.feed.tick_at(req.symbol, now)
        if tick is None or now - tick.time > self.config.max_quote_age:
            return MARKET_CLOSED, tick
        if req.action is TradeAction.SLTP:
            pos = self._positions.get(req.position_ticket or 0)
            if pos is None:
                return POSITION_CLOSED, tick
            freeze = spec.point * spec.freeze_level_points
            exit_px = self._exit_price(pos, tick)
            for level in (pos.sl, pos.tp):
                if freeze and level is not None and abs(exit_px - level) <= freeze:
                    return FROZEN, tick
            return (OK if self._stops_ok(pos.side, req.sl, req.tp, tick, spec) else INVALID_STOPS), tick
        assert req.side is not None and req.volume is not None  # noqa: PT018 - enforced by OrderRequest
        if req.filling in FILLING_FLAG and not spec.filling_mode_flags & FILLING_FLAG[req.filling]:
            return INVALID_FILL, tick
        if req.position_ticket is not None:
            pos = self._positions.get(req.position_ticket)
            if pos is None:
                return POSITION_CLOSED, tick
            if req.side is not pos.side.opposite:
                return INVALID, tick
            if req.volume > pos.volume or not is_multiple_of(req.volume, spec.volume_step):
                return INVALID_CLOSE_VOLUME, tick
            return OK, tick
        if not spec.trade_allowed:
            return TRADE_DISABLED, tick
        if (
            req.volume < spec.volume_min
            or req.volume > spec.volume_max
            or not is_multiple_of(req.volume, spec.volume_step)
        ):
            return INVALID_VOLUME, tick
        if not self._stops_ok(req.side, req.sl, req.tp, tick, spec):
            return INVALID_STOPS, tick
        free = self.balance + self._floating() - self._used_margin()
        if self._margin(req.symbol, req.volume, tick.entry_price(req.side)) > free:
            return NO_MONEY, tick
        return OK, tick

    def _result(self, code: int, **kw: object) -> OrderResult:
        return OrderResult(retcode=code, retcode_name=_NAMES.get(code, f"UNKNOWN_{code}"), **kw)

    # ------------------------------------------------------------------ BrokerPort

    async def account_info(self) -> AccountInfo:
        self._guard()
        equity = self.balance + self._floating()
        margin = self._used_margin()
        return AccountInfo(
            login=self.config.login,
            server=self.config.server,
            trade_mode=AccountTradeMode.DEMO,
            margin_mode=MarginMode.HEDGING,
            currency=self.config.currency,
            leverage=self.config.leverage,
            balance=self.balance,
            equity=equity,
            margin=margin,
            free_margin=equity - margin,
            trade_allowed=True,
            trade_expert=True,
        )

    async def symbol_spec(self, symbol: str) -> SymbolSpec:
        self._guard()
        return self._spec(symbol)

    async def positions(self, symbol: str | None = None) -> list[Position]:
        self._guard()
        out = []
        for p in sorted(self._positions.values(), key=lambda x: x.ticket):
            if symbol is not None and p.symbol != symbol:
                continue
            tick = self.feed.tick_at(p.symbol, self.clock.now())
            floating = (
                self._profit(p.side, p.symbol, p.volume, p.price_open, self._exit_price(p, tick))
                if tick
                else Decimal(0)
            )
            out.append(
                Position(
                    ticket=p.ticket, position_id=p.ticket, symbol=p.symbol, side=p.side, volume=p.volume,
                    price_open=p.price_open, sl=p.sl, tp=p.tp, magic=p.magic, comment=p.comment, time=p.time,
                    profit=floating,
                )
            )  # fmt: skip
        return out

    async def deals_for_position(self, position_id: int) -> list[Deal]:
        self._guard()
        return [d for d in self._deals if d.position_id == position_id]

    async def deals_between(self, start: datetime, end: datetime) -> list[Deal]:
        self._guard()
        return [d for d in self._deals if start <= d.time <= end]

    async def calc_profit(
        self, side: Side, symbol: str, volume: Decimal, price_open: Decimal, price_close: Decimal
    ) -> Decimal:
        self._guard()
        return self._profit(side, symbol, volume, price_open, price_close)

    async def calc_margin(self, side: Side, symbol: str, volume: Decimal, price: Decimal) -> Decimal:
        self._guard()
        return self._margin(symbol, volume, price)

    async def order_check(self, request: OrderRequest) -> OrderResult:
        self._guard()
        code, _ = self._validate(request)
        return self._result(code)

    async def order_send(self, request: OrderRequest) -> OrderResult | None:
        self._guard()
        self.sent.append(request)
        fault = self._faults.popleft() if self._faults else None
        if fault is Fault.DROPPED:
            return None
        if isinstance(fault, RejectWith):
            return self._result(fault.retcode)
        code, tick = self._validate(request)
        if code != OK or tick is None:
            return self._result(code)
        result = self._execute(request, tick)
        return None if fault is Fault.LOST_ACK else result

    def _execute(self, req: OrderRequest, tick: Tick) -> OrderResult:
        spec = self._spec(req.symbol)
        if req.action is TradeAction.SLTP:
            pos = self._positions[req.position_ticket or 0]
            pos.sl, pos.tp = req.sl, req.tp
            return self._result(DONE, comment="Request executed")
        assert req.side is not None and req.volume is not None and req.price is not None  # noqa: PT018
        market = tick.entry_price(req.side)
        if abs(market - req.price) > spec.point * req.deviation_points:
            return self._result(REQUOTE, price=market, comment="Requote")
        slip = spec.point * self.config.slippage_points
        fill = market + slip if req.side is Side.BUY else market - slip
        order = self._new_id()
        now = self.clock.now()
        if req.position_ticket is not None:
            pos = self._positions[req.position_ticket]
            deal = self._close(pos, req.volume, fill, DealReason.EXPERT, now, req.comment, order)
        else:
            pos = _Pos(
                ticket=order, symbol=req.symbol, side=req.side, volume=req.volume, price_open=fill, sl=req.sl,
                tp=req.tp, magic=req.magic, comment=req.comment, time=tick.time,
            )  # fmt: skip
            self._positions[order] = pos
            deal = self._record(
                pos,
                req.side,
                DealEntry.IN,
                DealReason.EXPERT,
                req.volume,
                fill,
                Decimal(0),
                now,
                req.comment,
                order,
            )
        return self._result(
            DONE, order=order, deal=deal.ticket, volume=req.volume, price=fill, comment="Request executed"
        )
