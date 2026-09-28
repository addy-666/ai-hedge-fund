"""Risk Manager — the ONLY constructor of executable order intents (AGENTS.md invariant 1).

Sequence for a final decision (docs/03 §8–§11): account loss limits → confidence threshold (again, defence
in depth) → guards → price drift since the snapshot → ATR stops → broker-calculated sizing → portfolio
exposure → issue the intent. Any broker error on the way fails CLOSED (rejection, no intent).

For a reversal only the CLOSE intent is issued: the executor must verify the close before anything else
happens, and a close-and-reverse re-open is a fresh evaluation with fresh equity, prices and limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from aifund.config.trading_config import RiskConfig
from aifund.domain._issuance import issue_order_intent
from aifund.domain.decision import FinalDecision
from aifund.domain.enums import CloseReason, Direction, IntentKind, ReasonCode, Timeframe
from aifund.domain.ids import new_id
from aifund.domain.intent import OrderIntent, Rejection, intent_comment, make_idempotency_key
from aifund.domain.market import AccountInfo, Position, SymbolSpec, Tick
from aifund.ports.broker import BrokerError, BrokerPort
from aifund.ports.system import ClockPort
from aifund.risk.guards import GuardAction, GuardContext, evaluate_guards
from aifund.risk.limits import EquityState, Exposure, check_loss_limits, check_new_exposure, drawdown_pct
from aifund.risk.sizing import SizingInputs, SizingRejected, SizingResult, size_position
from aifund.risk.stops import StopError, StopPlan, plan_stops

MAX_PRICE_DRIFT_ATR = Decimal("0.5")


@dataclass(frozen=True)
class RiskRequest:
    decision: FinalDecision
    trigger_tf: Timeframe
    bar_time: datetime
    strategy_version: str
    spec: SymbolSpec
    tick: Tick
    reference_price: Decimal  # trigger-TF close in the snapshot the decision was made on
    atr: Decimal  # ATR on the configured stops timeframe
    atr_pct_rank: float | None
    account: AccountInfo
    equity_state: EquityState
    guards: GuardContext
    open_exposure: list[Exposure]
    bucket: str
    commission_per_lot: Decimal = Decimal(0)


@dataclass(frozen=True)
class RiskOutcome:
    intent: OrderIntent | None = None
    rejection: Rejection | None = None
    action: GuardAction | None = None
    stops: StopPlan | None = None
    sizing: SizingResult | None = None
    start_flip_flop_lock: bool = False
    worksheet: dict[str, str] = field(default_factory=dict)


class RiskManager:
    def __init__(self, cfg: RiskConfig, *, magic: int, broker: BrokerPort, clock: ClockPort) -> None:
        self._cfg = cfg
        self._magic = magic
        self._broker = broker
        self._clock = clock

    async def evaluate(self, req: RiskRequest) -> RiskOutcome:
        ws: dict[str, str] = {}
        d = req.decision

        def reject(reason: ReasonCode, detail: str, **extra: object) -> RiskOutcome:
            ws["rejected"] = f"{reason}: {detail}"
            return RiskOutcome(rejection=Rejection(reason=reason, detail=detail), worksheet=ws, **extra)  # type: ignore[arg-type]

        if d.direction is Direction.NONE:
            return reject(ReasonCode.INTERNAL_ERROR, "risk manager received a NONE decision")
        if req.atr <= 0 or req.account.equity <= 0:  # invalid inputs: check before anything scales by them
            return reject(
                ReasonCode.INTERNAL_ERROR, f"invalid inputs: ATR {req.atr}, equity {req.account.equity}"
            )
        breach = check_loss_limits(req.equity_state, self._cfg.limits)
        if breach is not None:
            return reject(ReasonCode.LOSS_LIMIT, breach.describe())
        if d.final_confidence < self._cfg.confidence_threshold:
            return reject(
                ReasonCode.BELOW_THRESHOLD, f"{d.final_confidence} < {self._cfg.confidence_threshold}"
            )

        verdict = evaluate_guards(
            req.guards,
            self._cfg.guards,
            max_positions_per_symbol=self._cfg.limits.max_positions_per_symbol,
            max_trades_per_symbol_per_day=self._cfg.limits.max_trades_per_symbol_per_day,
        )
        ws["guard_action"] = verdict.action.value
        if verdict.rejection is not None:
            return reject(
                verdict.rejection.reason,
                verdict.rejection.detail,
                start_flip_flop_lock=verdict.start_flip_flop_lock,
            )
        if verdict.action in (GuardAction.CLOSE_ONLY, GuardAction.CLOSE_AND_REVERSE):
            assert verdict.target_position is not None
            return RiskOutcome(
                intent=self._close_intent(req, verdict.target_position), action=verdict.action, worksheet=ws
            )

        side = d.direction.to_side()
        entry = req.tick.entry_price(side)
        drift = abs(entry - req.reference_price)
        ws["entry_ref"], ws["price_drift"] = format(entry, "f"), format(drift, "f")
        if drift > MAX_PRICE_DRIFT_ATR * req.atr:
            return reject(
                ReasonCode.PRICE_MOVED, f"price moved {drift} (> {MAX_PRICE_DRIFT_ATR} ATR) since the bar"
            )

        try:
            stops = self._plan_stops(d, req.tick, req.atr, req.spec)
        except StopError as exc:
            return reject(ReasonCode.INTERNAL_ERROR, f"no valid stop: {exc}")
        ws.update(
            sl=format(stops.sl, "f"),
            tp=format(stops.tp, "f"),
            sl_atr_multiple=format(stops.sl_atr_multiple, "f"),
            rr=format(stops.rr, "f"),
            stop_clamped=stops.clamped or "none",
        )

        try:
            loss_per_lot = abs(
                await self._broker.calc_profit(side, req.spec.symbol, Decimal(1), entry, stops.sl)
            )
            margin_per_lot = await self._broker.calc_margin(side, req.spec.symbol, Decimal(1), entry)
        except BrokerError as exc:
            return reject(ReasonCode.INTERNAL_ERROR, f"broker calculator failed: {exc}")
        try:
            sizing = size_position(
                SizingInputs(
                    equity=req.account.equity,
                    free_margin=req.account.free_margin,
                    final_confidence=d.final_confidence,
                    loss_per_lot=loss_per_lot,
                    margin_per_lot=margin_per_lot,
                    notional_per_lot=margin_per_lot * req.account.leverage,
                    commission_per_lot=req.commission_per_lot,
                    atr_pct_rank=req.atr_pct_rank,
                    drawdown_pct=drawdown_pct(req.equity_state),
                    rule_risk_factor=d.risk_factor,
                ),
                req.spec,
                self._cfg,
            )
        except SizingRejected as exc:
            ws.update({f"sizing.{k}": v for k, v in exc.worksheet.items()})
            return reject(exc.reason, exc.detail, stops=stops)
        ws.update({f"sizing.{k}": v for k, v in sizing.worksheet.items()})

        exposure = Exposure(req.spec.symbol, req.bucket, sizing.initial_risk_money, sizing.notional)
        rejection = check_new_exposure(exposure, req.open_exposure, req.account.equity, self._cfg.limits)
        if rejection is not None:
            return reject(rejection.reason, rejection.detail, stops=stops, sizing=sizing)

        intent_id = new_id()
        intent = issue_order_intent(
            id=intent_id,
            idempotency_key=make_idempotency_key(
                account=req.account.login,
                symbol=req.spec.symbol,
                trigger_tf=req.trigger_tf,
                bar_time=req.bar_time,
                direction=d.direction,
                strategy_version=req.strategy_version,
            ),
            decision_id=d.decision_id,
            kind=IntentKind.OPEN,
            symbol=req.spec.symbol,
            side=side,
            volume=sizing.lots,
            price_ref=entry,
            sl=stops.sl,
            tp=stops.tp,
            sl_distance=stops.sl_distance,
            tp_distance=stops.tp_distance,
            risk_money=sizing.initial_risk_money,
            risk_pct=sizing.initial_risk_money / req.account.equity * 100,
            magic=self._magic,
            comment=intent_comment(intent_id),
            created_at=self._clock.now(),
        )
        return RiskOutcome(intent=intent, action=GuardAction.OPEN, stops=stops, sizing=sizing, worksheet=ws)

    def _plan_stops(self, d: FinalDecision, tick: Tick, atr: Decimal, spec: SymbolSpec) -> StopPlan:
        side = d.direction.to_side()
        return plan_stops(
            side=side,
            entry_ref=tick.entry_price(side),
            spread=tick.spread,
            atr=atr,
            spec=spec,
            cfg=self._cfg.stops,
            invalidation=d.invalidation_price,
            target=d.target_price,
        )

    def counterfactual_stops(
        self, d: FinalDecision, tick: Tick, atr: Decimal, spec: SymbolSpec
    ) -> StopPlan | None:
        """The stop/target stage 10 would plan for a signal that was blocked (for virtual trades).

        Exactly the planning a real order gets; nothing is issued. None if no valid stop exists.
        """
        if d.direction is Direction.NONE or atr <= 0:
            return None
        try:
            return self._plan_stops(d, tick, atr, spec)
        except StopError:
            return None

    def _close_intent(self, req: RiskRequest, pos: Position) -> OrderIntent:
        intent_id = new_id()
        return issue_order_intent(
            id=intent_id,
            idempotency_key=make_idempotency_key(
                account=req.account.login,
                symbol=req.spec.symbol,
                trigger_tf=req.trigger_tf,
                bar_time=req.bar_time,
                direction=req.decision.direction,
                strategy_version=f"{req.strategy_version}:close:{pos.ticket}",
            ),
            decision_id=req.decision.decision_id,
            kind=IntentKind.REVERSE_CLOSE,
            close_reason=CloseReason.REVERSAL,
            symbol=req.spec.symbol,
            side=pos.side.opposite,
            volume=pos.volume,
            price_ref=req.tick.entry_price(pos.side.opposite),
            risk_money=Decimal(0),
            risk_pct=Decimal(0),
            magic=self._magic,
            comment=intent_comment(intent_id),
            position_ticket=pos.ticket,
            created_at=self._clock.now(),
        )
