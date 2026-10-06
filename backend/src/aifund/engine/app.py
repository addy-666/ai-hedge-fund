"""The engine: its parts, its loops and the startup sequence (roadmap 5.6, docs/01 §4 §6 §9, docs/06 §2).

Startup (``boot``), in order — any problem leaves the engine STOPPED with a critical alert; nothing trades:

1. the process starts: whatever state was persisted, the engine is STARTING (``BOOT``);
2. the equity references (day/week start, peak) are restored from ``engine_state``;
3. startup checks: the broker's own checks (MT5: terminal connected, algo trading allowed, account type,
   hedging, symbols), the mode rules, and the E1/G-LLM evidence (checked when the pipeline is built);
4. every non-terminal order intent is resolved (UNKNOWN resolution) before anything else can trade;
5. one reconciliation pass;
6. the state is restored: never straight back to RUNNING — PAUSED (the operator RESUMEs) unless
   ``engine.auto_resume_after_crash`` and the engine was RUNNING; HALTED survives; an interrupted FLATTENING
   continues.

Then every loop runs under the supervisor (``run``), or — in SIM — under a deterministic stepping scheduler on
a fake clock (``simulate``), so a whole engine can be replayed and tested.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy.orm import Session, sessionmaker

from aifund.agents.analyst import Analyst
from aifund.agents.auditor import Auditor
from aifund.agents.committee import Committee
from aifund.agents.reviewer import Reviewer
from aifund.config.evidence import EvidenceError, EvidenceRecord, GLlmSignoff
from aifund.config.trading_config import TradingConfig
from aifund.domain.enums import EngineState, IntentStatus
from aifund.engine.commands import CommandPoller, Hooks
from aifund.engine.detectors import Factory
from aifund.engine.equity import EquitySnapshotter, EquityTracker
from aifund.engine.guardian import GuardianFiles, GuardianWatch
from aifund.engine.kill_switch import Flattener
from aifund.engine.learning import Learning
from aifund.engine.news_feed import CalendarFile
from aifund.engine.pipeline import DecisionPipeline
from aifund.engine.position_loop import PositionLoop
from aifund.engine.reviews import ReviewQueue
from aifund.engine.state import (
    ENTRIES,
    LEARNING,
    POSITION_MANAGEMENT,
    RECONCILIATION,
    StateMachine,
    Trigger,
    mode_problems,
    restore_trigger,
)
from aifund.engine.summary import DailySummary
from aifund.engine.supervisor import ALL_STATES, LoopSpec, Supervisor
from aifund.execution.executor import Executor
from aifund.market.bar_clock import BarClock
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.cursors import DecisionCursorStore
from aifund.persistence.repositories.system import EventRepository, HeartbeatRepository
from aifund.ports.broker import BrokerPort, MarketDataPort
from aifund.ports.system import ClockPort, NotifierPort, Severity
from aifund.reconcile.enrichment import Enricher
from aifund.reconcile.reconciler import Reconciler
from aifund.reconcile.virtual import VirtualTracker
from aifund.risk.limits import LimitKind, check_loss_limits, trading_day_start
from aifund.risk.manager import RiskManager
from aifund.risk.position_manager import PositionManager
from aifund.vault.review_exporter import ReviewExporter

LIVE = ALL_STATES - {EngineState.STOPPED}
OVERRIDE_EVENT = "evidence.override"  # recorded on every boot with engine.demo_orders_without_evidence
log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Options:
    config_path: Path
    detectors: Factory
    account_login: int  # the broker account number: part of every position-manager idempotency key
    evidence: Sequence[EvidenceRecord] = ()
    g_llm: Sequence[GLlmSignoff] = ()
    analyst: Analyst | None = None
    committee: Committee | None = None  # the Phase 8 committee in shadow (committee.mode)
    challenger: Analyst | None = None  # the challenger prompt in shadow (strategy.challenger_prompt_version)
    learners: tuple[Reviewer | None, Auditor | None] = (None, None)  # the learning loop's LLM agents
    vault_exporter: ReviewExporter | None = None  # weekly review notes for the TRADING BRAIN vault
    guardian: GuardianFiles | None = None
    calendar: CalendarFile | None = None
    healthchecks: Callable[[], Awaitable[object]] | None = None  # the dead-man ping
    broker_checks: Callable[[], Awaitable[list[str]]] | None = None  # MT5 startup checks
    live_confirmed: bool = False


class Engine:
    def __init__(
        self,
        cfg: TradingConfig,
        opts: Options,
        *,
        broker: BrokerPort,
        market: MarketDataPort,
        factory: sessionmaker[Session],
        clock: ClockPort,
        notifier: NotifierPort,
    ) -> None:
        self.cfg, self.opts = cfg, opts
        self.broker, self.market, self.factory, self.clock, self.notifier = (
            broker,
            market,
            factory,
            clock,
            notifier,
        )
        self.account = cfg.engine.account_label
        magic = cfg.engine.magic
        self.state = StateMachine(self.account, cfg.engine.mode, factory, clock, notifier)
        self.executor = Executor(broker, market, factory, clock, account_id=self.account)
        self.risk = RiskManager(cfg.risk, magic=magic, broker=broker, clock=clock)
        self.manager = PositionManager(
            cfg.position_management, cfg.risk.stops, magic=magic, account=opts.account_login
        )
        self.tracker = EquityTracker.for_engine(cfg.engine)
        self.pipeline_error: str | None = None
        self.pipeline: DecisionPipeline | None = None
        try:
            self.pipeline = DecisionPipeline(
                cfg,
                broker=broker,
                market=market,
                factory=factory,
                clock=clock,
                executor=self.executor,
                risk=self.risk,
                detectors=opts.detectors,
                equity=self.tracker,
                account_id=self.account,
                trading_enabled=self.entries_allowed,
                analyst=opts.analyst,
                committee=opts.committee,
                challenger=opts.challenger,
                evidence=opts.evidence,
                g_llm=opts.g_llm,
                news=opts.calendar.current if opts.calendar is not None else None,
            )
        except (EvidenceError, ValueError) as exc:  # reported by the startup checks
            self.pipeline_error = str(exc)
        watches = [(s.broker, cfg.profiles[s.profile].trigger_tf) for s in cfg.symbols]
        self.bar_clock = BarClock(market, clock, watches, DecisionCursorStore(factory))
        self.positions = PositionLoop(
            cfg,
            self.manager,
            broker=broker,
            market=market,
            executor=self.executor,
            factory=factory,
            clock=clock,
        )
        self.reconciler = Reconciler(broker, factory, clock, notifier, account_id=self.account, magic=magic)
        self.enricher = Enricher(
            broker,
            market,
            factory,
            clock,
            notifier,
            trigger_tfs={s.broker: cfg.profiles[s.profile].trigger_tf for s in cfg.symbols},
        )
        self.virtual = VirtualTracker(broker, market, factory, clock)
        self.snapshotter = EquitySnapshotter(
            self.tracker,
            cfg.risk.limits,
            broker=broker,
            factory=factory,
            clock=clock,
            notifier=notifier,
            account_id=self.account,
            magic=magic,
            mode=cfg.engine.mode,
            halt=self._halt,
        )
        self.flattener = Flattener(
            cfg,
            self.manager,
            self.state,
            broker=broker,
            market=market,
            executor=self.executor,
            factory=factory,
            clock=clock,
            notifier=notifier,
        )
        reviewer, auditor = opts.learners
        self.learning = (
            Learning(cfg, factory, clock, notifier, auditor=auditor) if cfg.learning.enabled else None
        )
        self.reviews = (
            ReviewQueue(reviewer, market, factory, clock) if cfg.learning.enabled and reviewer else None
        )
        self.commands = CommandPoller(
            cfg,
            self.state,
            Hooks(
                start=self.start_checks,
                resume_checks=self.resume_checks,
                halt_flag=self.halt_flag,
                learning=self.learning.handle if self.learning is not None else None,
            ),
            broker=broker,
            market=market,
            executor=self.executor,
            manager=self.manager,
            flattener=self.flattener,
            factory=factory,
            clock=clock,
            config_path=opts.config_path,
        )
        self.guardian = GuardianWatch(opts.guardian, self.state, clock) if opts.guardian is not None else None
        self.daily = DailySummary(
            cfg.alerts.daily_summary_utc,
            account=self.account,
            state=lambda: self.state.state.value,
            factory=factory,
            clock=clock,
            notifier=notifier,
        )
        self._halted_day: datetime | None = None
        self.supervisor = Supervisor(
            self.loops(), self.state, factory=factory, clock=clock, notifier=notifier
        )

    # ------------------------------------------------------------------ gates

    def halt_flag(self) -> str | None:
        return self.opts.guardian.halt_flag() if self.opts.guardian is not None else None

    def entries_allowed(self) -> bool:
        """Checked by the pipeline before every run: RUNNING, and no Guardian halt flag."""
        return self.state.entries_allowed and self.halt_flag() is None

    async def _halt(self, reason: str) -> None:
        if self.state.can(Trigger.LIMIT_BREACH):
            await self.state.fire(Trigger.LIMIT_BREACH, reason)

    # ------------------------------------------------------------------ startup

    async def start_checks(self) -> list[str]:
        """Steps 3–5 of the startup sequence (also run by the START command)."""
        problems: list[str] = []
        if self.pipeline_error is not None:
            problems.append(self.pipeline_error)
        if self.opts.broker_checks is not None:
            problems += await self.opts.broker_checks()
        account = await self.broker.account_info()
        problems += mode_problems(
            self.cfg.engine.mode,
            account.trade_mode,
            allow_live=self.cfg.engine.allow_live,
            live_confirmed=self.opts.live_confirmed,
        )
        if problems:
            return problems  # nothing is sent to a broker that failed its checks
        unresolved = [r for r in await self.executor.recover() if r.status is IntentStatus.UNKNOWN]
        problems += [f"intent {r.intent_id} is still UNKNOWN after resolution" for r in unresolved]
        report = await self.reconciler.run_once()
        problems += [f"reconciliation: {e}" for e in report.errors]
        return problems

    async def resume_checks(self) -> list[str]:
        problems: list[str] = []
        flag = self.halt_flag()
        if flag is not None:
            problems.append(f"Guardian halt flag set: {flag}")
        report = await self.reconciler.run_once()
        problems += [f"reconciliation: {e}" for e in report.errors]
        account = await self.broker.account_info()
        breach = check_loss_limits(
            self.tracker.update(account.equity, self.clock.now()), self.cfg.risk.limits
        )
        if breach is not None:
            problems.append(f"loss limit still breached: {breach.describe()}")
        return problems

    async def boot(self) -> EngineState:
        persisted, cause = self.state.state, self.state.halt_reason
        await self.state.fire(Trigger.BOOT, f"engine process started (was {persisted.value})")
        await self.snapshotter.start()
        problems = await self.start_checks()
        if problems:
            await self.state.fire(Trigger.START_FAILED, "; ".join(problems))
            await self.notifier.notify(Severity.CRITICAL, "Engine failed to start", "\n".join(problems))
            return self.state.state
        auto = (
            self.cfg.engine.auto_resume_after_crash
            and persisted is EngineState.RUNNING
            and self.halt_flag() is None
        )
        if auto:
            await self.state.fire(Trigger.STARTED, "auto-resume after restart (reconciliation clean)")
        else:
            # HALTED / FLATTENING keep their original cause across restarts (the daily re-arm reads it)
            await self.state.fire(restore_trigger(persisted), cause or f"restart from {persisted.value}")
        await self.notifier.notify(
            Severity.CRITICAL,
            "Engine restarted",
            f"{persisted.value} -> {self.state.state.value} ({self.cfg.engine.mode.value})",
        )
        if self.cfg.engine.demo_orders_without_evidence:  # the config allows it in DEMO only (10.11)
            await self._record_override()
        return self.state.state

    async def _record_override(self) -> None:
        """Say loudly, on every boot, that DEMO orders run without research evidence (roadmap 10.11)."""
        detail = (
            "engine.demo_orders_without_evidence: DEMO orders for detectors without E1 evidence and an "
            "analyst without a G-LLM sign-off. Never valid in LIVE."
        )

        def record() -> None:
            with unit_of_work(self.factory) as s:
                EventRepository(s, self.clock).append(OVERRIDE_EVENT, Severity.WARN, {"detail": detail})

        await asyncio.to_thread(record)
        log.warning("evidence.override", detail=detail)
        await self.notifier.notify(Severity.WARN, "Evidence override ON (DEMO)", detail)

    # ------------------------------------------------------------------ loops (docs/01 §6)

    async def _decide(self) -> None:
        if self.pipeline is None:
            return
        for event in (await self.bar_clock.poll()).events:
            await self.pipeline.run(event)

    async def _heartbeat(self) -> None:
        await self.state.flush()  # a state change the database refused earlier (fail closed meanwhile)

        def beat() -> None:
            with unit_of_work(self.factory) as s:
                HeartbeatRepository(s, self.clock).beat(
                    "engine", self.state.state.value, {"mode": self.cfg.engine.mode.value}
                )

        await asyncio.to_thread(beat)
        if self.guardian is not None:
            await self.guardian.run_once()

    async def _vault_export(self) -> None:
        if self.opts.vault_exporter is not None:
            await asyncio.to_thread(self.opts.vault_exporter.run_once)

    async def _healthchecks(self) -> None:
        if self.opts.healthchecks is not None:
            await self.opts.healthchecks()

    async def _day_rearm(self) -> None:
        """A DAILY loss halt re-arms to PAUSED at the next trading day (docs/06 §9); others need REARM."""
        engine = self.cfg.engine
        day = trading_day_start(self.clock.now(), engine.trading_day_boundary, engine.trading_day_timezone)
        daily = self.state.state is EngineState.HALTED and (self.state.halt_reason or "").startswith(
            LimitKind.DAILY_LOSS.value
        )
        if not daily:
            self._halted_day = None
            return
        if self._halted_day is None:
            self._halted_day = day
        elif day > self._halted_day and self.halt_flag() is None:
            self._halted_day = None
            await self.state.fire(Trigger.REARM, "new trading day after a daily loss halt")

    def loops(self) -> list[LoopSpec]:
        managing = POSITION_MANAGEMENT - {EngineState.FLATTENING}  # the kill switch owns the book then
        return [
            LoopSpec("heartbeat", 5.0, self._heartbeat, runs_in=ALL_STATES, money_path=True),
            LoopSpec("commands", 1.0, self.commands.run_once, runs_in=ALL_STATES),
            LoopSpec("decisions", 2.0, self._decide, runs_in=ENTRIES, money_path=True),
            LoopSpec("positions", 5.0, self.positions.run_once, runs_in=managing, money_path=True),
            LoopSpec("reconciler", 30.0, self.reconciler.run_once, runs_in=RECONCILIATION, money_path=True),
            LoopSpec("equity", 60.0, self.snapshotter.run_once, runs_in=LIVE, money_path=True),
            LoopSpec(
                "kill_switch",
                5.0,
                self.flattener.run_once,
                runs_in=frozenset({EngineState.FLATTENING}),
                money_path=True,
            ),
            LoopSpec("virtual", 60.0, self.virtual.run_once, runs_in=LEARNING),
            LoopSpec("enricher", 60.0, self.enricher.run_once, runs_in=LEARNING),
            LoopSpec("day_rearm", 60.0, self._day_rearm, runs_in=frozenset({EngineState.HALTED})),
            *(
                [LoopSpec("learning", 60.0, self.learning.run_once, runs_in=LEARNING)]
                if self.learning is not None
                else []
            ),
            *([LoopSpec("reviews", 60.0, self.reviews.run_once, runs_in=LEARNING)] if self.reviews else []),
            *(
                [LoopSpec("vault_export", 3600.0, self._vault_export, runs_in=ALL_STATES)]
                if self.opts.vault_exporter is not None
                else []
            ),
            LoopSpec("daily_summary", 60.0, self.daily.run_once, runs_in=ALL_STATES),
            LoopSpec("healthchecks", 60.0, self._healthchecks, runs_in=ALL_STATES),
        ]

    # ------------------------------------------------------------------ running

    async def run(self, stop: asyncio.Event) -> None:
        await self.supervisor.run(stop)

    async def simulate(self, until: datetime, *, step: timedelta = timedelta(seconds=1)) -> None:
        """SIM: every loop at its cadence on the fake clock, deterministically (one step at a time)."""
        advance = getattr(self.clock, "advance", None)
        if advance is None:
            raise TypeError("simulate() needs a FakeClock")
        due = {spec.name: self.clock.now() for spec in self.supervisor.loops}
        while self.clock.now() < until:
            for spec in self.supervisor.loops:
                if self.clock.now() < due[spec.name]:
                    continue
                wait = spec.interval_s
                if self.state.state in spec.runs_in:
                    wait = await self.supervisor.step(spec)
                due[spec.name] = self.clock.now() + timedelta(seconds=wait)
            advance(step.total_seconds())
