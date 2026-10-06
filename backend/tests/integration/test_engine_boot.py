"""Startup and restarts (roadmap 5.6): nothing trades before the engine checked, resolved and reconciled."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.config.loader import load_trading_config
from aifund.config.settings import PROJECT_ROOT
from aifund.config.trading_config import StrategyConfig, TradingConfig
from aifund.domain.enums import CommandType, EngineState, IntentStatus, Mode, TradeStatus
from aifund.engine.app import Engine, Options
from aifund.engine.detectors import detector_factory
from aifund.engine.guardian import HALT_FLAG, GuardianFiles
from aifund.engine.state import StateMachine, Trigger
from aifund.execution.executor import Executor
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.system import CommandRepository, EngineStateRepository
from aifund.persistence.tables import HeartbeatRow, OrderIntentRow, TradeRow
from aifund.ports.system import Severity
from tests.integration.test_executor import Crash, open_intent
from tests.integration.test_reconciler import MAGIC, World, make_world

EXAMPLE = PROJECT_ROOT / "config" / "trading.example.yaml"
PLAYBOOKS = PROJECT_ROOT / "config" / "playbooks"


def config(mode: Mode = Mode.SIM, **engine: Any) -> TradingConfig:
    cfg = load_trading_config(EXAMPLE).config
    return cfg.model_copy(
        update={
            "engine": cfg.engine.model_copy(
                update={"mode": mode, "magic": MAGIC, "account_label": "acc", **engine}
            ),
            "symbols": [s for s in cfg.symbols if s.broker == "XAUUSD"],
            "strategy": StrategyConfig(analyst_enabled=False, baseline_enabled=True),
        }
    )


def engine(
    w: World,
    cfg: TradingConfig | None = None,
    *,
    guardian: Path | None = None,
    checks: list[str] | None = None,
) -> Engine:
    cfg = cfg or config()

    async def broker_checks() -> list[str]:
        return list(checks or [])

    opts = Options(
        config_path=EXAMPLE,
        detectors=detector_factory(cfg, PLAYBOOKS),
        account_login=1,
        guardian=GuardianFiles(guardian) if guardian else None,
        broker_checks=broker_checks,
    )
    return Engine(
        cfg,
        opts,
        broker=w.broker,
        market=w.broker.feed,
        factory=w.factory,
        clock=w.clock,
        notifier=w.notifier,
    )


def persist(w: World, state: EngineState, reason: str | None = None) -> None:
    with unit_of_work(w.factory) as s:
        repo = EngineStateRepository(s, w.clock)
        repo.get_or_create("acc", Mode.SIM)
        repo.set_state("acc", state, halt_reason=reason)


def titles(w: World) -> list[tuple[Severity, str]]:
    return [(s, t) for s, t, _ in w.notifier.sent]


async def test_a_fresh_start_lands_paused_after_checking_everything(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)
    e = engine(w)
    assert await e.boot() is EngineState.PAUSED
    assert (Severity.CRITICAL, "Engine restarted") in titles(w)
    assert not e.entries_allowed()


@pytest.mark.parametrize(
    ("persisted", "auto", "expected"),
    [
        (EngineState.RUNNING, False, EngineState.PAUSED),  # docs/01 §9: never straight back to RUNNING
        (EngineState.RUNNING, True, EngineState.RUNNING),  # unless configured, and reconciliation passed
        (EngineState.PAUSED, True, EngineState.PAUSED),
        (EngineState.HALTED, True, EngineState.HALTED),  # a halt survives restarts
        (EngineState.STOPPED, True, EngineState.PAUSED),
    ],
)
async def test_restart_restores_the_state(
    factory: sessionmaker[Session],
    clock: FakeClock,
    persisted: EngineState,
    auto: bool,
    expected: EngineState,
) -> None:
    w = make_world(factory, clock)
    persist(w, persisted, "DAILY_LOSS: 3.1% >= limit 3.0%" if persisted is EngineState.HALTED else None)
    assert await engine(w, config(auto_resume_after_crash=auto)).boot() is expected


async def test_a_failed_check_leaves_it_stopped_and_pages(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)
    persist(w, EngineState.RUNNING)
    e = engine(w, checks=["AutoTrading is disabled in the terminal"])
    assert await e.boot() is EngineState.STOPPED
    assert (Severity.CRITICAL, "Engine failed to start") in titles(w)


async def test_demo_without_evidence_does_not_start(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)  # the SimBroker reports a DEMO account
    e = engine(w, config(Mode.DEMO))
    assert e.pipeline is None
    assert await e.boot() is EngineState.STOPPED
    body = next(b for _, t, b in w.notifier.sent if t == "Engine failed to start")
    assert "gate E1 refuses to start in DEMO" in body and "mtf_trend_pullback v1 on XAUUSD" in body  # noqa: PT018


async def test_an_intent_left_by_a_crash_is_resolved_before_anything_trades(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)

    def die(at: str) -> None:
        if at == "after_send":  # the order reached the broker; the process died before recording the fill
            raise Crash(at)

    crashed = Executor(w.broker, w.broker.feed, factory, clock, account_id="acc", checkpoint=die)
    intent = open_intent(w.clock)
    with pytest.raises(Crash):
        await crashed.execute(intent, await w.broker.symbol_spec("XAUUSD"))
    with factory() as s:
        assert s.get(OrderIntentRow, intent.id).status is IntentStatus.SENT  # type: ignore[union-attr]
    clock.advance(minutes=3)
    e = engine(w)
    assert await e.boot() is EngineState.PAUSED
    with factory() as s:
        row = s.get(OrderIntentRow, intent.id)
        assert row is not None
        assert row.status is IntentStatus.FILLED  # resolved from the broker's own records at startup
        trade = s.scalars(select(TradeRow).where(TradeRow.position_id == row.position_id)).one()
    assert trade.status is TradeStatus.OPEN  # and reconciled into the ledger before anything else ran


async def test_an_interrupted_kill_switch_continues_after_a_restart(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)
    for i in range(2):
        await w.executor.execute(open_intent(w.clock, key=f"{i:024d}"), await w.broker.symbol_spec("XAUUSD"))
    persist(w, EngineState.FLATTENING, "operator kill switch")
    e = engine(w)
    assert await e.boot() is EngineState.FLATTENING
    await e.simulate(clock.now() + timedelta(seconds=20))
    assert e.state.state is EngineState.HALTED
    assert [p for p in await w.broker.positions() if p.magic == MAGIC] == []


async def test_the_guardian_flag_halts_and_blocks_rearm(
    factory: sessionmaker[Session], clock: FakeClock, tmp_path: Path
) -> None:
    w = make_world(factory, clock)
    e = engine(w, guardian=tmp_path)
    await e.boot()
    await e.state.fire(Trigger.RESUME, "test")
    (tmp_path / HALT_FLAG).write_text("HARD_MAX_DRAWDOWN")
    assert not e.entries_allowed()  # checked before every pipeline run, even before the watch fires
    await e.simulate(clock.now() + timedelta(seconds=10))
    assert (e.state.state, e.state.halt_reason) == (EngineState.HALTED, "Guardian EA: HARD_MAX_DRAWDOWN")
    assert (tmp_path / "heartbeat.txt").read_text().split()[2] == "HALTED"
    assert await e.resume_checks() == ["Guardian halt flag set: HARD_MAX_DRAWDOWN"]


async def test_commands_and_heartbeats_run_in_the_simulated_engine(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)
    e = engine(w)
    await e.boot()
    with unit_of_work(factory) as s:
        CommandRepository(s, clock).enqueue(CommandType.RESUME, None, requested_by="test")
    await e.simulate(clock.now() + timedelta(seconds=15))
    assert e.state.state is EngineState.RUNNING
    with factory() as s:
        beats = {h.component: h.status for h in s.scalars(select(HeartbeatRow)).all()}
    assert beats["engine"] == "RUNNING"
    assert {"loop.commands", "loop.heartbeat", "loop.decisions", "loop.positions"} <= set(beats)


async def test_a_daily_loss_halt_rearms_at_the_next_trading_day(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)
    e = engine(w)
    await e.boot()
    await e.state.fire(Trigger.LIMIT_BREACH, "DAILY_LOSS: 3.10% >= limit 3.0%")
    await e._day_rearm()
    assert e.state.state is EngineState.HALTED
    clock.advance(timedelta(days=1).total_seconds())  # past 17:00 New York
    await e._day_rearm()
    assert e.state.state is EngineState.PAUSED
    await e.state.fire(Trigger.GUARDIAN_HALT, "x")  # not a daily halt: stays until REARM
    clock.advance(timedelta(days=1).total_seconds())
    await e._day_rearm()
    await e._day_rearm()
    assert e.state.state is EngineState.HALTED


async def test_simulate_needs_a_fake_clock(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    e = engine(w)
    e.clock = object()  # type: ignore[assignment]
    with pytest.raises(TypeError, match="FakeClock"):
        await e.simulate(clock.now())


def test_state_machine_reads_the_persisted_state(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    persist(w, EngineState.HALTED, "x")
    assert StateMachine("acc", Mode.SIM, factory, clock, w.notifier).state is EngineState.HALTED


async def test_a_restart_keeps_the_halt_cause_so_a_daily_halt_still_rearms(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    """Roadmap 9.2 (found by the chaos suite): a restart used to replace the cause with "restart from HALTED",
    so a daily-loss halt never re-armed at the next trading day once the engine had restarted."""
    w = make_world(factory, clock)
    persist(w, EngineState.HALTED, "DAILY_LOSS: 3.10% >= limit 3.0%")
    e = engine(w)
    assert await e.boot() is EngineState.HALTED
    assert e.state.halt_reason == "DAILY_LOSS: 3.10% >= limit 3.0%"
    await e._day_rearm()
    clock.advance(timedelta(days=1).total_seconds())
    await e._day_rearm()
    assert e.state.state is EngineState.PAUSED


async def test_the_demo_override_starts_without_evidence_and_says_so_on_every_boot(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    """Roadmap 10.11: the same DEMO config that refused to start above, with the operator's override."""
    from sqlalchemy import select

    from aifund.engine.app import OVERRIDE_EVENT
    from aifund.persistence.tables import EventRow

    w = make_world(factory, clock)
    e = engine(w, config(Mode.DEMO, demo_orders_without_evidence=True))
    assert e.pipeline is not None
    assert await e.boot() is EngineState.PAUSED
    assert (Severity.WARN, "Evidence override ON (DEMO)") in titles(w)
    await engine(w, config(Mode.DEMO, demo_orders_without_evidence=True)).boot()
    with factory() as s:
        events = s.scalars(select(EventRow).where(EventRow.type == OVERRIDE_EVENT)).all()
    assert len(events) == 2
    assert "Never valid in LIVE" in events[0].payload["detail"]
