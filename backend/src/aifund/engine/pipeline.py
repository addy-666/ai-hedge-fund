"""Decision pipeline (docs/03 §3): one run per closed trigger bar per symbol.

Stages: pre-flight gates → feature snapshot → setup detection → decision → risk → execution. Phase 2 uses
the deterministic BASELINE decision (the detector's direction at a fixed confidence, no LLM); Phase 4
slots the analyst in at the decision stage without changing anything around it. Phase 7: the learned rules
(ACTIVE enforced, SHADOW logged) apply to whichever decision is taken, baseline or analyst, and the analyst
sees the ACTIVE rules near its candidates as lessons.

Every run writes exactly one ``decisions`` row (outcome, reason, stage, snapshot, setups, proposal,
confidences, risk worksheet, provenance). Any exception ends the run as ERROR with no order sent.
Runs for the same symbol are serialised by a per-symbol lock (duplicate-guard layer 2).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn

import structlog
from sqlalchemy.orm import Session, sessionmaker

from aifund.agents.analyst import Analyst, AnalystInput, Verdict
from aifund.agents.committee import Committee, Deliberation
from aifund.agents.portfolio_manager import PortfolioManager, with_rules
from aifund.config.evidence import (
    Deployment,
    EvidenceRecord,
    GLlmSignoff,
    profile_key,
    require_evidence,
    require_g_llm,
)
from aifund.config.trading_config import ProfileConfig, SymbolConfig, TradingConfig
from aifund.domain.decision import FeatureSnapshot, FinalDecision, SetupCandidate
from aifund.domain.enums import (
    DealEntry,
    DecisionOutcome,
    Direction,
    IntentKind,
    IntentStatus,
    ReasonCode,
    Timeframe,
    VirtualArm,
)
from aifund.domain.ids import new_id
from aifund.domain.market import Bar, Position, SymbolSpec, Tick
from aifund.domain.trade import gets_virtual_trade
from aifund.domain.values import to_decimal
from aifund.engine.calibration import CalibrationCache
from aifund.engine.equity import EquityTracker
from aifund.engine.rulebook import RulebookCache
from aifund.execution.executor import ExecutionResult, Executor
from aifund.market.bar_clock import BarClosed
from aifund.market.cross_asset import CrossAssetInput
from aifund.market.feature_registry import tf_prefix
from aifund.market.features import PortfolioContext, SnapshotError, build_snapshot
from aifund.market.news import NewsCalendar
from aifund.market.sessions import calendars
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.api import BarCacheRepository
from aifund.persistence.repositories.decisions import DecisionRepository
from aifund.persistence.repositories.intents import IntentRepository
from aifund.persistence.repositories.learning import RuleEvaluationRepository
from aifund.persistence.repositories.market import FeatureSnapshotRepository
from aifund.persistence.repositories.virtual import VirtualTradeRepository
from aifund.ports.broker import BrokerError, BrokerPort, MarketDataPort
from aifund.ports.system import ClockPort
from aifund.reconcile.virtual import virtual_expiry
from aifund.risk.guards import GuardAction, GuardContext
from aifund.risk.limits import Exposure, check_loss_limits, drawdown_pct
from aifund.risk.manager import RiskManager, RiskRequest
from aifund.rules.dsl import RuleContext, proposal_features
from aifund.rules.engine import RuleVerdict, lesson
from aifund.strategies.base import SetupDetector, TfRoles

log = structlog.get_logger(__name__)
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
    rulebook_version: int = 0
    rule_evaluations: list[dict[str, Any]] = field(default_factory=list)
    lessons_shown: list[str] | None = None  # rule ids shown to the analyst (None: no LLM call)
    model: str | None = None
    cost_usd: Decimal | None = None
    prompt_version: str | None = None
    virtual: dict[str, Any] | None = None  # a blocked signal's counterfactual trade plan (docs/03 §14.4)
    shadows: list[dict[str, Any]] = field(default_factory=list)  # G-LLM shadow plans (docs/09 §7)
    committee: dict[str, Any] | None = None  # the committee's deliberation in shadow (roadmap 8.4)
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


def _tradable(verdict: RuleVerdict, decision: FinalDecision, threshold: int) -> bool:
    return verdict.blocked_by is None and decision.final_confidence >= threshold


def _record_rules(record: DecisionRecord, verdict: RuleVerdict) -> None:
    record.rules_matched = verdict.matched or None
    record.rule_evaluations = [
        {"rule_id": e.rule_id, "rule_version": e.rule_version, "mode": e.mode, "matched": e.matched,
         "action_applied": e.action_applied}
        for e in verdict.evaluations
    ]  # fmt: skip


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
        evidence: Sequence[EvidenceRecord] = (),
        g_llm: Sequence[GLlmSignoff] = (),
        news: Callable[[], NewsCalendar | None] | None = None,
        rulebook: RulebookCache | None = None,
        committee: Committee | None = None,
        calibration: CalibrationCache | None = None,
    ) -> None:
        if cfg.committee.mode == "shadow" and committee is None:
            raise ValueError("committee.mode shadow needs the committee (specialists and critic: an LLM)")
        if cfg.strategy.analyst_enabled and analyst is None and not cfg.strategy.baseline_enabled:
            raise ValueError(
                "strategy.analyst_enabled needs an Analyst (an LLM); or enable the baseline instead"
            )
        self._cfg = cfg
        self._profiles = {**cfg.profiles, **(profile_override or {})}
        # gate E1 (docs/09 §7): outside SIM every detector needs matching, passing research evidence
        strategy = cfg.strategy
        sends_orders = not strategy.dry_run and (
            strategy.baseline_enabled or (strategy.analyst_enabled and strategy.analyst_orders)
        )  # an analyst in shadow with the baseline off decides, records its shadows, and sends nothing
        require_evidence(
            self._deployments(detectors), evidence, mode=cfg.engine.mode, dry_run=not sends_orders
        )
        # G-LLM (docs/09 §7): outside SIM the analyst's decisions reach the Risk Manager only after sign-off
        require_g_llm(
            g_llm,
            prompt_version=f"analyst_v{cfg.strategy.analyst_prompt_version}",
            model=cfg.llm.analyst_model,
            mode=cfg.engine.mode,
            analyst_orders=cfg.strategy.analyst_orders and not cfg.strategy.dry_run,
        )
        self._analyst = analyst if cfg.strategy.analyst_enabled else None
        # confidence calibration per source (docs/04 §9): identity until a model is ACTIVE
        self._calibration = calibration or CalibrationCache(factory, clock)
        # the committee runs in shadow beside the analyst (config requires the analyst for it): never orders
        self._committee = committee if cfg.committee.mode == "shadow" and self._analyst is not None else None
        self._portfolio = portfolio or PortfolioManager(calibrator=self._calibration.calibrator("analyst"))
        if self._committee is not None:
            self._committee.use_calibrator(self._calibration.calibrator("committee"))
        self._rulebook = rulebook or RulebookCache(
            factory, clock, max_total_penalty=cfg.learning.max_total_penalty
        )
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
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._flip_flop_until: dict[str, datetime] = {}
        self._by_broker = {s.broker: s for s in cfg.symbols}
        self._cached: dict[tuple[str, Timeframe], datetime] = {}
        self._news = news  # the Guardian EA's calendar (5.7b); None = no calendar source

    # ------------------------------------------------------------------ helpers

    def _tx_sync(self, fn: Callable[[Session], Any]) -> Any:
        with unit_of_work(self._factory) as s:
            return fn(s)

    async def _tx(self, fn: Callable[[Session], Any]) -> Any:
        return await asyncio.to_thread(self._tx_sync, fn)

    async def _cross_input(self, symbol: str, bars: dict[Timeframe, list[Bar]]) -> CrossAssetInput | None:
        """The other instruments' closed bars for the cross-asset features (roadmap 10.2), or None when
        ``cross_asset`` is off. An instrument the broker cannot serve gets no bars, so its features are null:
        missing context never blocks a decision and never becomes a guess (a rule or hypothesis that needs
        it simply does not match)."""
        ca = self._cfg.cross_asset
        instruments = [i for i in self._cfg.instruments() if i.broker != symbol]
        if not instruments:
            return None
        others: dict[str, list[Bar]] = {}
        for inst in instruments:
            try:
                others[inst.slug] = await self._market.closed_bars(inst.broker, ca.timeframe, ca.bars)
            except BrokerError as exc:
                log.warning("cross_asset.unavailable", instrument=inst.broker, error=str(exc)[:200])
                others[inst.slug] = []
        subject = bars.get(ca.timeframe)
        if subject is None:
            subject = await self._market.closed_bars(symbol, ca.timeframe, ca.bars)
        max_age = timedelta(minutes=ca.max_age_minutes)
        return CrossAssetInput(timeframe=ca.timeframe, max_age=max_age, others=others, subject=subject)

    async def _cache_bars(self, bars: dict[Timeframe, list[Bar]]) -> None:
        """Keep the dashboard's chart cache current with the bars just read (new ones only; best effort)."""
        fresh: list[Bar] = []
        for tf, series in bars.items():
            if not series:
                continue
            key = (series[-1].symbol, tf)
            last = self._cached.get(key)
            fresh += [b for b in series if last is None or b.time > last]
            self._cached[key] = series[-1].time
        if not fresh:
            return
        try:
            await self._tx(lambda s: BarCacheRepository(s).upsert(fresh))
        except Exception as exc:
            log.warning("bar_cache.failed", error=str(exc))

    def _deployments(
        self, detectors: Callable[[SymbolConfig, TfRoles], list[SetupDetector]]
    ) -> list[Deployment]:
        out = []
        for sym in self._cfg.symbols:
            roles = self.roles(sym)
            profile = profile_key(roles.trigger, roles.setup, roles.context)
            out += [Deployment(d, sym.broker, profile) for d in detectors(sym, roles)]
        return out

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

    def _baseline(
        self, decision_id: str, symbol: str, candidates: list[SetupCandidate]
    ) -> tuple[FinalDecision, dict[str, Any]]:
        """The deterministic baseline: the strongest candidate at a fixed confidence (Phase 2)."""
        best = max(candidates, key=lambda c: c.strength)
        proposal = {
            "direction": best.direction_hint.value,
            "setup_tag": best.setup_tag,
            "confidence": BASELINE_CONFIDENCE,
            "source": "baseline",
        }
        decision = FinalDecision(
            decision_id=decision_id,
            symbol=symbol,
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
        return decision, proposal

    def _shadow(self, record: DecisionRecord, arm: VirtualArm, b: _Blocked) -> None:
        plan = self._virtual_plan(b)
        if plan is not None:
            record.shadows.append({**plan, "arm": arm})

    async def _deliberate(self, inp: AnalystInput, rules: Callable[..., RuleVerdict]) -> Deliberation | None:
        """The committee in shadow: a failure there is logged and never touches the real decision."""
        assert self._committee is not None  # only called with a committee
        try:
            return await self._committee.deliberate(inp, rules)
        except Exception as exc:
            log.warning("committee.failed", decision_id=inp.decision_id, error=f"{type(exc).__name__}: {exc}")
            return None

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
        calendar = self._news() if self._news is not None else None
        news_cfg = self._cfg.risk.news
        if news_cfg.enabled:  # gate 5: fail closed without a calendar
            if calendar is None:
                self._end(record, DecisionOutcome.SKIPPED, ReasonCode.NEWS_BLACKOUT, "no news calendar")
            news = calendar.blackout(
                sym_cfg.news_currencies, news_cfg.impact, now,
                before_min=news_cfg.blackout_minutes_before, after_min=news_cfg.blackout_minutes_after,
            )  # fmt: skip
            if news is not None:
                self._end(
                    record, DecisionOutcome.SKIPPED, ReasonCode.NEWS_BLACKOUT,
                    f"{news.impact} {news.currency} {news.title} at {news.time:%H:%M} UTC",
                )  # fmt: skip
        news_minutes: tuple[int | None, int | None] = (None, None)
        if calendar is not None:
            news_minutes = (
                calendar.minutes_to_next(sym_cfg.news_currencies, ["HIGH"], now),
                calendar.minutes_since_last(sym_cfg.news_currencies, ["HIGH"], now),
            )
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
        await self._cache_bars(bars)
        cross = await self._cross_input(event.symbol, bars)
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
                news_minutes=news_minutes,
                cross_asset=cross,
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

        # 4. decision: the LLM analyst + portfolio manager, or the deterministic baseline; the learned rules
        #    (docs/04 §7) apply to whichever is taken
        record.stage = "DECISION"
        rules = await asyncio.to_thread(self._rulebook.current)
        await asyncio.to_thread(self._calibration.refresh)
        record.rulebook_version = rules.version

        def rule_context(direction: Direction, setup_tag: str | None, confidence: int | None) -> RuleContext:
            features = {
                **snapshot.features,
                **proposal_features(direction, setup_tag, confidence, snapshot.features),
            }
            return RuleContext(sym_cfg.canonical, direction, setup_tag, roles.trigger.value, features)

        threshold = self._cfg.risk.confidence_threshold
        raw_baseline, baseline_proposal = self._baseline(record.decision_id, event.symbol, candidates)
        baseline_rules = rules.evaluate(
            rule_context(raw_baseline.direction, raw_baseline.setup_tag, BASELINE_CONFIDENCE)
        )
        baseline = with_rules(raw_baseline, baseline_rules)
        verdict = None
        taken: RuleVerdict = baseline_rules
        if self._analyst is None:
            record.confidence, record.prompt_version = BASELINE_CONFIDENCE, self._version
            record.proposal, decision = baseline_proposal, baseline
        else:
            position = next((p for p in own if p.symbol == event.symbol), None)
            portfolio = (
                f"{len(own)} open positions, equity {account.equity}, "
                f"drawdown {drawdown_pct(equity_state):.2f}%, "
                f"today {account.equity - equity_state.day_start_equity:+.2f}"
            )
            lessons = rules.lessons(
                [
                    rule_context(direction, c.setup_tag, None)
                    for c in candidates
                    for direction in (
                        [c.direction_hint] if c.direction_hint in (Direction.LONG, Direction.SHORT)
                        else [Direction.LONG, Direction.SHORT]
                    )
                ]
            )  # fmt: skip
            record.lessons_shown = [br.rule.rule_id for br in lessons]
            bar_input = AnalystInput(
                decision_id=record.decision_id, symbol=event.symbol, snapshot=snapshot, roles=roles,
                candidates=candidates, trigger_bars=bars[roles.trigger], trigger_atr=to_decimal(atr),
                tick=tick, spread_points=spread_points, position=position, portfolio=portfolio,
                lessons=[lesson(br) for br in lessons],
            )  # fmt: skip
            deliberation: Deliberation | None = None
            if self._committee is None:
                analysis = await self._analyst.analyse(bar_input)
            else:  # the committee deliberates concurrently, in shadow

                def committee_rules(direction: Direction, setup_tag: str, confidence: int) -> RuleVerdict:
                    return rules.evaluate(rule_context(direction, setup_tag, confidence))

                analysis, deliberation = await asyncio.gather(
                    self._analyst.analyse(bar_input), self._deliberate(bar_input, committee_rules)
                )
            record.model, record.cost_usd, record.prompt_version = (
                analysis.model, analysis.cost_usd, analysis.prompt_version,
            )  # fmt: skip
            analyst_proposal = {
                **(analysis.raw or {}), "source": "analyst", "verdict": analysis.verdict.value,
                "prompt_version": analysis.prompt_version, "notes": analysis.notes,
            }  # fmt: skip
            analyst_rules: RuleVerdict | None = None
            if analysis.verdict is Verdict.PROPOSAL and analysis.proposal is not None:
                p = analysis.proposal
                analyst_rules = rules.evaluate(rule_context(p.direction, p.setup_tag, p.confidence))
                verdict = self._portfolio.decide(
                    decision_id=record.decision_id, symbol=event.symbol, proposal=p, rules=analyst_rules
                )
                considered = set(p.lessons_considered) - set(record.lessons_shown)
                if considered:
                    log.info("lessons.mismatch", decision_id=record.decision_id, not_shown=sorted(considered))
            # G-LLM shadows (docs/09 §7): what each arm would have traded on this bar, rules applied to both
            if _tradable(baseline_rules, baseline, threshold):
                self._shadow(
                    record,
                    VirtualArm.SHADOW_BASELINE,
                    _Blocked(event, sym_cfg, roles, spec, tick, snapshot, baseline),
                )
            if (
                verdict is not None
                and analyst_rules is not None
                and _tradable(analyst_rules, verdict.decision, threshold)
            ):
                analyst_arm = _Blocked(event, sym_cfg, roles, spec, tick, snapshot, verdict.decision)
                self._shadow(record, VirtualArm.SHADOW_ANALYST, analyst_arm)
            if deliberation is not None:  # the committee's shadow (roadmap 8.4): what it would have traded
                record.committee = deliberation.record()
                chosen = deliberation.decision
                tradable = (
                    chosen is not None
                    and chosen.blocked_by is None
                    and chosen.decision.final_confidence >= threshold
                )
                record.committee["tradable"] = tradable
                if chosen is not None and tradable:
                    committee_arm = _Blocked(event, sym_cfg, roles, spec, tick, snapshot, chosen.decision)
                    self._shadow(record, VirtualArm.SHADOW_COMMITTEE, committee_arm)

            if self._cfg.strategy.analyst_orders:  # G-LLM signed off (or SIM): the analyst decides
                record.proposal = analyst_proposal
                if analysis.proposal is not None:
                    record.confidence = analysis.proposal.confidence
                if analysis.verdict is Verdict.INVALID:
                    self._end(record, DecisionOutcome.INVALID, analysis.reason, analysis.detail)
                if verdict is None or analyst_rules is None:
                    self._end(record, DecisionOutcome.HOLD, analysis.reason, analysis.detail)
                decision, taken = verdict.decision, analyst_rules
            elif self._cfg.strategy.baseline_enabled:  # analyst in shadow, the baseline trades
                record.proposal = {**baseline_proposal, "shadow_analyst": analyst_proposal}
                record.confidence, record.prompt_version = BASELINE_CONFIDENCE, self._version
                decision, verdict = baseline, None
            else:  # analyst in shadow and nothing else trades
                record.proposal = analyst_proposal
                if analysis.proposal is not None:
                    record.confidence = analysis.proposal.confidence
                if analyst_rules is not None:
                    _record_rules(record, analyst_rules)
                detail = f"analyst {analysis.verdict.value} in shadow (strategy.analyst_orders off: no G-LLM)"
                self._end(record, DecisionOutcome.SHADOW, analysis.reason, detail)
        _record_rules(record, taken)
        record.calibrated_confidence = decision.calibrated_confidence
        record.penalty_points = decision.penalty_points
        record.final_confidence = decision.final_confidence
        record.risk_factor = decision.risk_factor
        blocked = _Blocked(event, sym_cfg, roles, spec, tick, snapshot, decision)
        if taken.blocked_by is not None:
            self._block(
                record, blocked, DecisionOutcome.RULE_BLOCKED, ReasonCode.RULE_BLOCK, taken.blocked_by
            )
        if decision.final_confidence < threshold:
            detail = f"{decision.final_confidence} < {threshold}"
            if decision.penalty_points:
                detail += f" (rule penalty {decision.penalty_points})"
            self._block(record, blocked, DecisionOutcome.BELOW_THRESHOLD, ReasonCode.BELOW_THRESHOLD, detail)

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
                proposal=(
                    {**(record.proposal or {}), "committee": record.committee}
                    if record.committee is not None
                    else record.proposal
                ),
                llm_confidence=record.confidence,
                calibrated_confidence=record.calibrated_confidence,
                penalty_points=record.penalty_points,
                final_confidence=record.final_confidence,
                risk_factor=record.risk_factor,
                rules_matched=record.rules_matched,
                lessons_shown=record.lessons_shown,
                rulebook_version=record.rulebook_version,
                model=record.model,
                cost_usd=record.cost_usd,
                prompt_version=record.prompt_version or self._version,
                risk_calc=record.risk_calc,
                latency_ms=latency_ms,
            )
            if record.rule_evaluations:
                RuleEvaluationRepository(s).add_many(record.decision_id, record.rule_evaluations)
            planned = [{**record.virtual, "arm": VirtualArm.BLOCKED}] if record.virtual is not None else []
            for plan in planned + record.shadows:
                VirtualTradeRepository(s, self._clock).add_pending(
                    account_id=self._account,
                    decision_id=record.decision_id,
                    snapshot_id=record.snapshot_id,
                    symbol=record.symbol,
                    **plan,
                )

        await self._tx(_write)
