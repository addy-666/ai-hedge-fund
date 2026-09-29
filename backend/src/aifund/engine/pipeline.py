"""Decision pipeline (docs/03 §3): one run per closed trigger bar per symbol.

Stages: pre-flight gates → feature snapshot → setup detection → decision → risk → execution. Phase 2 uses
the deterministic BASELINE decision (the detector's direction at a fixed confidence, no LLM); Phase 4
slots the analyst in at the decision stage without changing anything around it.

Every run writes exactly one ``decisions`` row (outcome, reason, stage, snapshot, setups, proposal,
confidences, risk worksheet, provenance). Any exception ends the run as ERROR with no order sent.
Runs for the same symbol are serialised by a per-symbol lock (duplicate-guard layer 2).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn

from sqlalchemy.orm import Session, sessionmaker

from aifund.agents.analyst import Analyst, AnalystInput, Verdict
from aifund.agents.portfolio_manager import PortfolioManager
from aifund.config.trading_config import ProfileConfig, SymbolConfig, TradingConfig
from aifund.domain.decision import FeatureSnapshot, FinalDecision, SetupCandidate
from aifund.domain.enums import (
    DealEntry,
    DecisionOutcome,
    Direction,
    IntentKind,
    IntentStatus,
    ReasonCode,
)
from aifund.domain.ids import new_id
from aifund.domain.market import Position, SymbolSpec, Tick
from aifund.domain.trade import gets_virtual_trade
from aifund.domain.values import to_decimal
from aifund.engine.equity import EquityTracker
from aifund.execution.executor import ExecutionResult, Executor
from aifund.market.bar_clock import BarClosed
from aifund.market.feature_registry import tf_prefix
from aifund.market.features import PortfolioContext, SnapshotError, build_snapshot
from aifund.market.sessions import calendars
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.decisions import DecisionRepository
from aifund.persistence.repositories.intents import IntentRepository
from aifund.persistence.repositories.market import FeatureSnapshotRepository
from aifund.persistence.repositories.virtual import VirtualTradeRepository
from aifund.ports.broker import BrokerError, BrokerPort, MarketDataPort
from aifund.ports.system import ClockPort
from aifund.reconcile.virtual import virtual_expiry
from aifund.risk.guards import GuardAction, GuardContext
from aifund.risk.limits import Exposure, check_loss_limits, drawdown_pct
from aifund.risk.manager import RiskManager, RiskRequest
from aifund.strategies.base import SetupDetector, TfRoles

BASELINE_CONFIDENCE = 70
MAX_TICK_AGE = timedelta(seconds=60)


@dataclass
class DecisionRecord:
    """What one run did; mirrored into the ``decisions`` row."""

    symbol: str
    bar_time: datetime
    outcome: DecisionOutcome = DecisionOutcome.ERROR
    stage: str = "PREFLIGHT"
    reason: ReasonCode | None = None
    detail: str = ""
    snapshot_id: str | None = None
    setups: list[dict[str, Any]] = field(default_factory=list)
    proposal: dict[str, Any] | None = None
    confidence: int | None = None
    risk_calc: dict[str, str] | None = None
    execution: ExecutionResult | None = None
    # the confidence pipeline (docs/03 §8); for the baseline all four equal its fixed confidence
    calibrated_confidence: int | None = None
    penalty_points: int | None = None
    final_confidence: int | None = None
    risk_factor: Decimal | None = None
    rules_matched: list[str] | None = None
    model: str | None = None
    cost_usd: Decimal | None = None
    prompt_version: str | None = None
    virtual: dict[str, Any] | None = None  # a blocked signal's counterfactual trade plan (docs/03 §14.4)
    decision_id: str = field(default_factory=new_id)


@dataclass(frozen=True)
class _Blocked:
    """What a blocked signal's virtual trade is planned from."""

    event: BarClosed
    sym_cfg: SymbolConfig
    roles: TfRoles
    spec: SymbolSpec
    tick: Tick
    snapshot: FeatureSnapshot
    decision: FinalDecision


class _Stop(Exception):
    pass


class DecisionPipeline:
    def __init__(
        self,
        cfg: TradingConfig,
        *,
        broker: BrokerPort,
        market: MarketDataPort,
        factory: sessionmaker[Session],
        clock: ClockPort,
        executor: Executor,
        risk: RiskManager,
        detectors: Callable[[SymbolConfig, TfRoles], list[SetupDetector]],
        equity: EquityTracker,
        account_id: str,
        strategy_version: str = "baseline_v1",
        trading_enabled: Callable[[], bool] = lambda: True,
        profile_override: dict[str, ProfileConfig] | None = None,
        analyst: Analyst | None = None,
        portfolio: PortfolioManager | None = None,
    ) -> None:
        if cfg.strategy.analyst_enabled and analyst is None and not cfg.strategy.baseline_enabled:
            raise ValueError(
                "strategy.analyst_enabled needs an Analyst (an LLM); or enable the baseline instead"
            )
        self._cfg = cfg
        self._analyst = analyst if cfg.strategy.analyst_enabled else None
        self._portfolio = portfolio or PortfolioManager(max_total_penalty=cfg.learning.max_total_penalty)
        self._sessions = calendars(cfg.sessions)
        self._broker = broker
        self._market = market
        self._factory = factory
        self._clock = clock
        self._executor = executor
        self._risk = risk
        self._detectors = detectors
        self._equity = equity
        self._account = account_id
        self._version = strategy_version
        self._enabled = trading_enabled
        self._profiles = {**cfg.profiles, **(profile_override or {})}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._flip_flop_until: dict[str, datetime] = {}
        self._by_broker = {s.broker: s for s in cfg.symbols}

    # ------------------------------------------------------------------ helpers

    def _tx_sync(self, fn: Callable[[Session], Any]) -> Any:
        with unit_of_work(self._factory) as s:
            return fn(s)

    async def _tx(self, fn: Callable[[Session], Any]) -> Any:
        return await asyncio.to_thread(self._tx_sync, fn)

    def roles(self, symbol_cfg: SymbolConfig) -> TfRoles:
        profile = self._profiles[symbol_cfg.profile]
        return TfRoles(trigger=profile.trigger_tf, setup=profile.setup_tf, context=tuple(profile.context_tfs))

    # ------------------------------------------------------------------ entry point

    async def run(self, event: BarClosed) -> DecisionRecord:
        record = DecisionRecord(symbol=event.symbol, bar_time=event.bar_time)
        started = time.perf_counter()
        async with self._locks[event.symbol]:
            # the row exists from the start: intents reference it, and a crash leaves the stage it died in
            await self._tx(
                lambda s: DecisionRepository(s, self._clock).add(
                    id=record.decision_id,
                    account_id=self._account,
                    symbol=event.symbol,
                    trigger_tf=event.timeframe.value,
                    bar_time=event.bar_time,
                    stage_reached=record.stage,
                    outcome=DecisionOutcome.ERROR,
                    reason_detail="in progress",
                    prompt_version=self._version,
                )
            )
            try:
                await self._run(event, record)
            except _Stop:
                pass
            except Exception as exc:  # fail closed: nothing after this point sends an order
                record.outcome, record.reason = DecisionOutcome.ERROR, ReasonCode.INTERNAL_ERROR
                record.detail = f"{type(exc).__name__}: {exc}"
            await self._persist(event, record, int((time.perf_counter() - started) * 1000))
        return record

    def _end(
        self, record: DecisionRecord, outcome: DecisionOutcome, reason: ReasonCode | None, detail: str
    ) -> NoReturn:
        record.outcome, record.reason, record.detail = outcome, reason, detail
        raise _Stop

    def _block(
        self, record: DecisionRecord, b: _Blocked, outcome: DecisionOutcome, reason: ReasonCode, detail: str
    ) -> NoReturn:
        """End a run on a blocked signal, planning its virtual trade when the reason qualifies."""
        if gets_virtual_trade(outcome, reason):
            plan = self._virtual_plan(b)
            record.virtual = None if plan is None else {**plan, "blocked_by": reason.value}
        self._end(record, outcome, reason, detail)

    def _virtual_plan(self, b: _Blocked) -> dict[str, Any] | None:
        atr = self._stop_atr(b.snapshot, b.roles)
        stops = self._risk.counterfactual_stops(b.decision, b.tick, atr, b.spec) if atr else None
        if stops is None:
            return None
        entry_time = b.event.bar_time + timedelta(minutes=b.roles.trigger.minutes)  # the next bar's open
        session = self._sessions.get(b.sym_cfg.session) if b.sym_cfg.session else None
        expiry = virtual_expiry(
            entry_time,
            b.roles.trigger,
            self._cfg.position_management,
            session,
            trade_weekends=b.sym_cfg.trade_weekends,
        )
        if expiry is None:
            return None  # a real trade would have been flattened at once
        expires_at, reason = expiry
        return dict(
            side=b.decision.direction.to_side(),
            setup_tag=b.decision.setup_tag,
            entry_time=entry_time,
            sl_distance=stops.sl_distance,
            tp_distance=stops.tp_distance,
            expires_at=expires_at,
            expire_reason=reason,
        )

    def _stop_atr(self, snapshot: FeatureSnapshot, roles: TfRoles) -> Decimal | None:
        """ATR on the configured stops timeframe, as the Risk Manager receives it."""
        stops_tf = roles.trigger if self._cfg.risk.stops.atr_tf == "trigger" else roles.setup
        value = snapshot.features.get(f"{tf_prefix(stops_tf)}.atr14")
        if not isinstance(value, float):
            value = snapshot.features.get(f"{tf_prefix(roles.trigger)}.atr14")
        return to_decimal(value) if isinstance(value, float) else None

    async def _run(self, event: BarClosed, record: DecisionRecord) -> None:
        sym_cfg = self._by_broker.get(event.symbol)
        if sym_cfg is None:
            self._end(record, DecisionOutcome.SKIPPED, ReasonCode.INTERNAL_ERROR, "symbol not configured")
        roles = self.roles(sym_cfg)
        if event.timeframe is not roles.trigger:
            self._end(record, DecisionOutcome.SKIPPED, ReasonCode.INTERNAL_ERROR, "not the trigger timeframe")
        now = self._clock.now()

        # 1. pre-flight (cheap; before any snapshot or model call)
        record.stage = "PREFLIGHT"
        if not self._enabled():
            self._end(record, DecisionOutcome.SKIPPED, ReasonCode.ENGINE_NOT_RUNNING, "trading disabled")
        in_flight = await self._tx(lambda s: len(IntentRepository(s, self._clock).non_terminal(event.symbol)))
        if in_flight:
            self._end(record, DecisionOutcome.SKIPPED, ReasonCode.INTENT_IN_FLIGHT, f"{in_flight} unresolved")
        session = self._sessions.get(sym_cfg.session) if sym_cfg.session else None
        if not sym_cfg.trade_weekends and session is not None:
            if not session.is_open(now):
                self._end(record, DecisionOutcome.SKIPPED, ReasonCode.MARKET_CLOSED, "session closed")
            pm = self._cfg.position_management
            close = session.long_close_ahead(now, timedelta(hours=pm.long_close_hours))
            if close is not None and now >= close - timedelta(minutes=pm.no_entries_before_close_minutes):
                self._end(
                    record, DecisionOutcome.SKIPPED, ReasonCode.MARKET_CLOSED, f"shuts {close:%a %H:%M} UTC"
                )
        tick = await self._market.tick(event.symbol)
        if tick is None or now - tick.time > MAX_TICK_AGE:
            self._end(record, DecisionOutcome.SKIPPED, ReasonCode.STALE_TICK, "no fresh quote")
        spec = await self._broker.symbol_spec(event.symbol)
        if not spec.trade_allowed:
            self._end(record, DecisionOutcome.SKIPPED, ReasonCode.MARKET_CLOSED, "symbol closed for entries")
        spread_points = int(tick.spread / spec.point)
        if sym_cfg.max_spread_points is not None and spread_points > sym_cfg.max_spread_points:
            self._end(record, DecisionOutcome.SKIPPED, ReasonCode.SPREAD_TOO_WIDE, f"{spread_points} points")
        account = await self._broker.account_info()
        equity_state = self._equity.update(account.equity, now)
        breach = check_loss_limits(equity_state, self._cfg.risk.limits)
        if breach is not None:
            self._end(record, DecisionOutcome.SKIPPED, ReasonCode.LOSS_LIMIT, breach.describe())
        locked_until = self._flip_flop_until.get(event.symbol)
        if locked_until is not None and event.bar_time < locked_until:
            self._end(record, DecisionOutcome.SKIPPED, ReasonCode.FLIP_FLOP, f"locked until {locked_until}")

        # 2. snapshot
        record.stage = "SNAPSHOT"
        profile = self._profiles[sym_cfg.profile]
        tfs = [roles.trigger, roles.setup, *roles.context]
        bars = {tf: await self._market.closed_bars(event.symbol, tf, profile.bars_per_tf) for tf in tfs}
        positions = await self._broker.positions()
        own = [p for p in positions if p.magic == self._cfg.engine.magic]
        try:
            snapshot = build_snapshot(
                symbol=event.symbol,
                trigger_tf=roles.trigger,
                setup_tf=roles.setup,
                context_tfs=list(roles.context),
                bars=bars,
                as_of=now,
                tick=tick,
                min_bars=profile.bars_per_tf,
                portfolio=PortfolioContext(len(own), 0.0, 0.0, 0, float(drawdown_pct(equity_state))),
            )
        except SnapshotError as exc:
            self._end(record, DecisionOutcome.ERROR, exc.reason, exc.detail)
        record.snapshot_id = await self._tx(
            lambda s: FeatureSnapshotRepository(s, self._clock).get_or_add(snapshot).id
        )
        atr = snapshot.features.get(f"{tf_prefix(roles.trigger)}.atr14")
        spread_to_atr = snapshot.features.get("ctx.spread_to_atr")
        if isinstance(spread_to_atr, float) and spread_to_atr > float(sym_cfg.max_spread_to_atr):
            self._end(
                record, DecisionOutcome.SKIPPED, ReasonCode.SPREAD_TOO_WIDE, f"spread/ATR {spread_to_atr:.3f}"
            )

        # 3. setups
        record.stage = "SETUP"
        candidates: list[SetupCandidate] = []
        for detector in self._detectors(sym_cfg, roles):
            candidates += detector.detect(snapshot)
        record.setups = [c.model_dump(mode="json") for c in candidates]
        if not candidates:
            self._end(record, DecisionOutcome.NO_SETUP, None, "")
        if not isinstance(atr, float):
            self._end(record, DecisionOutcome.ERROR, ReasonCode.INSUFFICIENT_BARS, "no trigger ATR")

        # 4. decision: the LLM analyst + portfolio manager, or the deterministic baseline
        record.stage = "DECISION"
        if self._analyst is not None:
            position = next((p for p in own if p.symbol == event.symbol), None)
            portfolio = (
                f"{len(own)} open positions, equity {account.equity}, "
                f"drawdown {drawdown_pct(equity_state):.2f}%, "
                f"today {account.equity - equity_state.day_start_equity:+.2f}"
            )
            analysis = await self._analyst.analyse(
                AnalystInput(
                    decision_id=record.decision_id, symbol=event.symbol, snapshot=snapshot, roles=roles,
                    candidates=candidates, trigger_bars=bars[roles.trigger], trigger_atr=to_decimal(atr),
                    tick=tick,
                    spread_points=spread_points, position=position, portfolio=portfolio,
                )
            )  # fmt: skip
            record.model, record.cost_usd, record.prompt_version = (
                analysis.model, analysis.cost_usd, analysis.prompt_version,
            )  # fmt: skip
            record.proposal = {**(analysis.raw or {}), "source": "analyst", "notes": analysis.notes}
            if analysis.proposal is not None:
                record.confidence = analysis.proposal.confidence
            if analysis.verdict is Verdict.INVALID:
                self._end(record, DecisionOutcome.INVALID, analysis.reason, analysis.detail)
            if analysis.verdict is Verdict.HOLD or analysis.proposal is None:
                self._end(record, DecisionOutcome.HOLD, analysis.reason, analysis.detail)
            verdict = self._portfolio.decide(
                decision_id=record.decision_id,
                symbol=event.symbol,
                proposal=analysis.proposal,
                snapshot=snapshot,
            )
            decision = verdict.decision
            record.rules_matched = list(verdict.rules_matched)
        else:
            best = max(candidates, key=lambda c: c.strength)
            record.confidence = BASELINE_CONFIDENCE
            record.prompt_version = self._version
            record.proposal = {
                "direction": best.direction_hint.value,
                "setup_tag": best.setup_tag,
                "confidence": BASELINE_CONFIDENCE,
                "source": "baseline",
            }
            decision = FinalDecision(
                decision_id=record.decision_id,
                symbol=event.symbol,
                direction=best.direction_hint,
                setup_tag=best.setup_tag,
                llm_confidence=BASELINE_CONFIDENCE,
                calibrated_confidence=BASELINE_CONFIDENCE,
                penalty_points=0,
                final_confidence=BASELINE_CONFIDENCE,
                risk_factor=Decimal(1),
                invalidation_price=best.key_levels.get("invalidation"),
                target_price=best.key_levels.get("target"),
            )
            verdict = None
        record.calibrated_confidence = decision.calibrated_confidence
        record.penalty_points = decision.penalty_points
        record.final_confidence = decision.final_confidence
        record.risk_factor = decision.risk_factor
        blocked = _Blocked(event, sym_cfg, roles, spec, tick, snapshot, decision)
        if verdict is not None and verdict.blocked_by is not None:
            self._block(
                record, blocked, DecisionOutcome.RULE_BLOCKED, ReasonCode.RULE_BLOCK, verdict.blocked_by
            )
        if decision.final_confidence < self._cfg.risk.confidence_threshold:
            threshold = f"{decision.final_confidence} < {self._cfg.risk.confidence_threshold}"
            self._block(
                record, blocked, DecisionOutcome.BELOW_THRESHOLD, ReasonCode.BELOW_THRESHOLD, threshold
            )

        # 5–7. risk (guards, stops, sizing, exposure)
        record.stage = "RISK"
        request = await self._risk_request(
            event,
            sym_cfg,
            roles,
            spec,
            tick,
            snapshot,
            decision,
            account,
            equity_state,
            positions,
            own,
            to_decimal(atr),
        )
        outcome = await self._risk.evaluate(request)
        record.risk_calc = outcome.worksheet
        if outcome.start_flip_flop_lock:
            lock_bars = self._cfg.risk.guards.flip_flop_lock_bars
            self._flip_flop_until[event.symbol] = event.bar_time + timedelta(
                minutes=roles.trigger.minutes * lock_bars
            )
        if outcome.intent is None:
            rejection = outcome.rejection
            reason = rejection.reason if rejection else ReasonCode.INTERNAL_ERROR
            detail = rejection.detail if rejection else ""
            self._block(record, blocked, DecisionOutcome.RISK_REJECTED, reason, detail)

        # 8. execution (or, in dry-run mode, the record of what would have been sent)
        record.stage = "EXECUTION"
        if self._cfg.strategy.dry_run:
            intent = outcome.intent
            record.outcome, record.reason = DecisionOutcome.DRY_RUN, None
            record.detail = f"would send {intent.kind} {intent.side} {intent.volume} {intent.symbol}"
            return
        result = await self._executor.execute(outcome.intent, spec)
        record.execution = result
        if result.status in (IntentStatus.FILLED, IntentStatus.UNKNOWN):
            record.outcome = DecisionOutcome.ORDERED
            record.reason = None
            record.detail = f"{outcome.intent.kind} {result.status}"
            if outcome.action is GuardAction.CLOSE_AND_REVERSE and result.status is IntentStatus.FILLED:
                record.detail += " (re-open on the next evaluation)"
        else:
            self._end(
                record,
                DecisionOutcome.RISK_REJECTED,
                result.reason or ReasonCode.BROKER_REJECTED,
                result.detail,
            )

    async def _risk_request(
        self,
        event: BarClosed,
        sym_cfg: SymbolConfig,
        roles: TfRoles,
        spec: SymbolSpec,
        tick: Tick,
        snapshot: FeatureSnapshot,
        decision: FinalDecision,
        account: Any,
        equity_state: Any,
        positions: list[Position],
        own: list[Position],
        trigger_atr: Decimal,
    ) -> RiskRequest:
        now = self._clock.now()
        day_start = self._equity.day_start or now
        rank = snapshot.features.get(f"{tf_prefix(roles.trigger)}.atr14_pct_rank100")
        close = snapshot.features.get(f"{tf_prefix(roles.trigger)}.close")

        # facts for the guards and exposure (live broker state + our own records)
        deals = await self._broker.deals_between(now - timedelta(days=7), now)
        mine = [d for d in deals if d.symbol == event.symbol and d.magic == self._cfg.engine.magic]
        exits = [d for d in mine if d.entry is not DealEntry.IN]
        last_exit = max(exits, key=lambda d: d.time) if exits else None
        bars_since = None
        if last_exit is not None:
            bars_since = int((now - last_exit.time) / timedelta(minutes=roles.trigger.minutes))
        trades_today = len([d for d in mine if d.entry is DealEntry.IN and d.time >= day_start])

        def _facts(s: Session) -> tuple[list[Direction], int, dict[int, Any]]:
            directions = DecisionRepository(s, self._clock).recent_directions(
                event.symbol, roles.trigger.value
            )
            repo = IntentRepository(s, self._clock)
            reversals = repo.count_since(event.symbol, IntentKind.REVERSE_CLOSE, day_start)
            opens = repo.filled_opens([p.position_id for p in own])
            return directions, reversals, {pid: (r.risk_money, r.volume) for pid, r in opens.items()}

        directions, reversals, open_risk = await self._tx(_facts)
        exposure = []
        for p in own:
            risk_money = open_risk.get(p.position_id, (Decimal(0), p.volume))[0]
            bucket = next((s.correlation_bucket for s in self._cfg.symbols if s.broker == p.symbol), p.symbol)
            try:
                notional = (
                    await self._broker.calc_margin(p.side, p.symbol, p.volume, p.price_open)
                    * account.leverage
                )
            except BrokerError:
                notional = Decimal(0)
            exposure.append(Exposure(p.symbol, bucket, risk_money, notional))

        return RiskRequest(
            decision=decision,
            trigger_tf=roles.trigger,
            bar_time=event.bar_time,
            strategy_version=self._version,
            spec=spec,
            tick=tick,
            reference_price=to_decimal(close) if isinstance(close, float) else tick.bid,
            atr=self._stop_atr(snapshot, roles) or trigger_atr,
            atr_pct_rank=rank if isinstance(rank, float) else None,
            account=account,
            equity_state=equity_state,
            guards=GuardContext(
                symbol=event.symbol,
                direction=decision.direction,
                final_confidence=decision.final_confidence,
                confidence_threshold=self._cfg.risk.confidence_threshold,
                magic=self._cfg.engine.magic,
                now=now,
                trigger_tf=roles.trigger,
                broker_positions=[p for p in positions if p.symbol == event.symbol],
                in_flight_intents=0,
                bars_since_last_close=bars_since,
                last_close_was_loss=bool(last_exit and last_exit.net < 0),
                trades_today=trades_today,
                reversals_today=reversals,
                recent_directions=directions,
            ),
            open_exposure=exposure,
            bucket=sym_cfg.correlation_bucket,
        )

    async def _persist(self, event: BarClosed, record: DecisionRecord, latency_ms: int) -> None:
        def _write(s: Session) -> None:
            DecisionRepository(s, self._clock).update(
                record.decision_id,
                snapshot_id=record.snapshot_id,
                stage_reached=record.stage,
                outcome=record.outcome,
                reason_code=record.reason.value if record.reason else None,
                reason_detail=record.detail or None,
                setups=record.setups or None,
                proposal=record.proposal,
                llm_confidence=record.confidence,
                calibrated_confidence=record.calibrated_confidence,
                penalty_points=record.penalty_points,
                final_confidence=record.final_confidence,
                risk_factor=record.risk_factor,
                rules_matched=record.rules_matched,
                lessons_shown=[] if record.model is not None else None,
                rulebook_version=0,
                model=record.model,
                cost_usd=record.cost_usd,
                prompt_version=record.prompt_version or self._version,
                risk_calc=record.risk_calc,
                latency_ms=latency_ms,
            )
            if record.virtual is not None:
                VirtualTradeRepository(s, self._clock).add_pending(
                    account_id=self._account,
                    decision_id=record.decision_id,
                    snapshot_id=record.snapshot_id,
                    symbol=record.symbol,
                    **record.virtual,
                )

        await self._tx(_write)
