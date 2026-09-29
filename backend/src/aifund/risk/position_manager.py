"""Position manager (docs/03 §13): what, if anything, to do with one open engine position right now.

Priority (first applicable wins): pre-close flatten → time stop → missing-SL repair → post-fill stop
re-alignment → break-even → ATR trailing. Every action is an intent issued here (this module lives in the
risk layer, the only place intents are issued) and executed by the executor, so each modification is
recorded like any order.

Safety rules: stops are only ever TIGHTENED (never loosened), new levels respect the broker's stop and
freeze levels (inside the freeze distance nothing is modified this cycle), a missing SL whose planned
level has already been crossed closes the position instead, and trailing uses CLOSED bars only. Each
action is idempotent per minute: a failed attempt can be retried a minute later, a repeat within the
minute is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from aifund.config.trading_config import PositionManagementConfig, StopsConfig
from aifund.domain._issuance import issue_order_intent
from aifund.domain.enums import CloseReason, Direction, IntentKind, Side, Timeframe
from aifund.domain.ids import new_id
from aifund.domain.intent import OrderIntent, intent_comment, make_idempotency_key
from aifund.domain.market import Position, SymbolSpec, Tick
from aifund.domain.values import Rounding, quantize_to_step
from aifund.market.sessions import SessionCalendar


class ActionKind(StrEnum):
    PRE_CLOSE_FLATTEN = "PRE_CLOSE_FLATTEN"  # before a weekend / holiday closure (session calendar)
    TIME_STOP = "TIME_STOP"
    REPAIR_SL = "REPAIR_SL"
    STOP_BREACHED = "STOP_BREACHED"  # SL missing and its planned level already crossed: close
    REALIGN_SL = "REALIGN_SL"
    BREAK_EVEN = "BREAK_EVEN"
    TRAIL = "TRAIL"


_CLOSE_REASON = {
    ActionKind.PRE_CLOSE_FLATTEN: CloseReason.FLATTEN,
    ActionKind.TIME_STOP: CloseReason.TIME_STOP,
    ActionKind.STOP_BREACHED: CloseReason.ENGINE,
}


@dataclass(frozen=True)
class PositionAction:
    kind: ActionKind
    intent: OrderIntent
    detail: str


@dataclass(frozen=True)
class PositionFacts:
    position: Position
    spec: SymbolSpec
    tick: Tick
    trigger_tf: Timeframe
    trade_weekends: bool
    planned_sl_distance: Decimal | None  # from the FILLED OPEN intent (None for orphans)
    atr: Decimal | None = None  # trigger-TF ATR on closed bars
    last_closed_close: Decimal | None = None  # trigger-TF close of the last closed bar
    session: SessionCalendar | None = None  # the symbol's trading-session calendar


class PositionManager:
    def __init__(
        self,
        cfg: PositionManagementConfig,
        stops: StopsConfig,
        *,
        magic: int,
        account: int,
        realign_tolerance: Decimal = Decimal("0.10"),
    ) -> None:
        self._cfg = cfg
        self._stops = stops
        self._magic = magic
        self._account = account
        self._tolerance = realign_tolerance

    def plan(self, f: PositionFacts, now: datetime) -> PositionAction | None:
        pos, tick, spec = f.position, f.tick, f.spec
        buy = pos.side is Side.BUY
        exit_price = tick.bid if buy else tick.ask

        # 1. flatten before a long closure: weekends, and Fridays or holidays that close early. Only in the
        #    window before the close: once the market is shut a close cannot fill, so nothing is retried.
        lead = self._cfg.flatten_before_close_minutes
        if not f.trade_weekends and f.session is not None and lead is not None:
            close = f.session.long_close_ahead(now, timedelta(hours=self._cfg.long_close_hours))
            if close is not None and now >= close - timedelta(minutes=lead):
                return self._close(
                    f,
                    now,
                    ActionKind.PRE_CLOSE_FLATTEN,
                    IntentKind.FLATTEN,
                    f"market shuts {close:%a %H:%M} UTC",
                )

        # 2. time stop
        if self._cfg.time_stop_bars is not None:
            held = int((now - pos.time) / timedelta(minutes=f.trigger_tf.minutes))
            if held >= self._cfg.time_stop_bars:
                return self._close(f, now, ActionKind.TIME_STOP, IntentKind.CLOSE, f"held {held} bars")

        if self._frozen(pos, exit_price, spec):
            return None  # inside the freeze distance MT5 rejects modifications: try next cycle

        # 3. missing stop-loss
        if pos.sl is None:
            distance = f.planned_sl_distance or (self._stops.k_sl_default * f.atr if f.atr else None)
            if distance is None:
                return self._close(
                    f, now, ActionKind.STOP_BREACHED, IntentKind.CLOSE, "no SL and no way to size one"
                )
            level = self._round_sl(
                pos.price_open - distance if buy else pos.price_open + distance, pos.side, spec
            )
            if not self._valid_sl(level, pos.side, tick, spec):
                return self._close(
                    f, now, ActionKind.STOP_BREACHED, IntentKind.CLOSE, f"planned SL {level} crossed"
                )
            return self._modify(f, now, ActionKind.REPAIR_SL, level, "stop-loss was missing")

        # 4. re-align the stop after slippage changed the planned distance by more than the tolerance
        if f.planned_sl_distance:
            actual = abs(pos.price_open - pos.sl)
            if abs(actual - f.planned_sl_distance) / f.planned_sl_distance > self._tolerance:
                sign = 1 if buy else -1
                level = self._round_sl(pos.price_open - sign * f.planned_sl_distance, pos.side, spec)
                if self._tighter(level, pos.sl, pos.side) and self._valid_sl(level, pos.side, tick, spec):
                    return self._modify(f, now, ActionKind.REALIGN_SL, level, f"distance {actual} vs planned")

        # 5. break-even
        if self._cfg.break_even_at_r is not None and f.planned_sl_distance:
            gain = (exit_price - pos.price_open) if buy else (pos.price_open - exit_price)
            if gain >= self._cfg.break_even_at_r * f.planned_sl_distance:
                level = pos.price_open
                if self._tighter(level, pos.sl, pos.side) and self._valid_sl(level, pos.side, tick, spec):
                    return self._modify(f, now, ActionKind.BREAK_EVEN, level, f"+{gain} >= break-even")

        # 6. ATR trailing on closed bars
        if self._cfg.trailing.mode == "atr" and f.atr and f.last_closed_close is not None:
            raw = (
                f.last_closed_close - self._cfg.trailing.k * f.atr
                if buy
                else f.last_closed_close + self._cfg.trailing.k * f.atr
            )
            level = self._round_sl(raw, pos.side, spec)
            if self._tighter(level, pos.sl, pos.side) and self._valid_sl(level, pos.side, tick, spec):
                return self._modify(f, now, ActionKind.TRAIL, level, f"trail {self._cfg.trailing.k} ATR")
        return None

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _round_sl(level: Decimal, side: Side, spec: SymbolSpec) -> Decimal:
        # a stop-loss rounds AWAY from the position (never a tighter stop than intended)
        return quantize_to_step(level, spec.tick_size, Rounding.DOWN if side is Side.BUY else Rounding.UP)

    @staticmethod
    def _tighter(new: Decimal, current: Decimal, side: Side) -> bool:
        return new > current if side is Side.BUY else new < current

    @staticmethod
    def _valid_sl(level: Decimal, side: Side, tick: Tick, spec: SymbolSpec) -> bool:
        gap = spec.point * max(spec.stops_level_points, 1)
        return level <= tick.bid - gap if side is Side.BUY else level >= tick.ask + gap

    @staticmethod
    def _frozen(pos: Position, exit_price: Decimal, spec: SymbolSpec) -> bool:
        if not spec.freeze_level_points:
            return False
        freeze = spec.point * spec.freeze_level_points
        return any(level is not None and abs(exit_price - level) <= freeze for level in (pos.sl, pos.tp))

    def _key(self, f: PositionFacts, now: datetime, kind: ActionKind) -> str:
        return make_idempotency_key(
            account=self._account,
            symbol=f.position.symbol,
            trigger_tf=f.trigger_tf,
            bar_time=now.replace(second=0, microsecond=0),
            direction=Direction.LONG if f.position.side is Side.BUY else Direction.SHORT,
            strategy_version=f"pm:{kind}:{f.position.ticket}",
        )

    def _close(
        self, f: PositionFacts, now: datetime, kind: ActionKind, ik: IntentKind, detail: str
    ) -> PositionAction:
        pos = f.position
        intent_id = new_id()
        intent = issue_order_intent(
            id=intent_id,
            idempotency_key=self._key(f, now, kind),
            decision_id=None,
            kind=ik,
            close_reason=_CLOSE_REASON[kind],
            symbol=pos.symbol,
            side=pos.side.opposite,
            volume=pos.volume,
            price_ref=f.tick.entry_price(pos.side.opposite),
            risk_money=Decimal(0),
            risk_pct=Decimal(0),
            magic=self._magic,
            comment=intent_comment(intent_id),
            position_ticket=pos.ticket,
            created_at=now,
        )
        return PositionAction(kind, intent, detail)

    def _modify(
        self, f: PositionFacts, now: datetime, kind: ActionKind, sl: Decimal, detail: str
    ) -> PositionAction:
        pos = f.position
        intent_id = new_id()
        intent = issue_order_intent(
            id=intent_id,
            idempotency_key=self._key(f, now, kind),
            decision_id=None,
            kind=IntentKind.MODIFY_SLTP,
            symbol=pos.symbol,
            side=pos.side,
            volume=pos.volume,
            price_ref=pos.price_open,
            sl=sl,
            tp=pos.tp,
            risk_money=Decimal(0),
            risk_pct=Decimal(0),
            magic=self._magic,
            comment=intent_comment(intent_id),
            position_ticket=pos.ticket,
            created_at=now,
        )
        return PositionAction(kind, intent, f"{detail}: SL -> {sl}")
