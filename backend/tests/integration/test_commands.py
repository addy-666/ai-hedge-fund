"""Operator commands (roadmap 5.3) and the FLATTEN_ALL kill switch (5.4) against the SimBroker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.sim.sim_broker import INVALID, RejectWith
from aifund.config.loader import load_trading_config
from aifund.config.settings import PROJECT_ROOT
from aifund.domain.enums import CloseReason, CommandStatus, CommandType, EngineState, IntentStatus, Mode
from aifund.engine.commands import LIVE_CONFIRMATION, CommandPoller, Hooks
from aifund.engine.kill_switch import Flattener
from aifund.engine.state import StateMachine, Trigger
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.system import CommandRepository
from aifund.persistence.tables import AuditLogRow, CommandRow, EventRow, OrderIntentRow
from aifund.ports.system import Severity
from aifund.risk.position_manager import PositionManager
from tests.integration.test_executor import open_intent
from tests.integration.test_reconciler import MAGIC, World, make_world

EXAMPLE = PROJECT_ROOT / "config" / "trading.example.yaml"
CFG = load_trading_config(EXAMPLE).config


@dataclass
class Checks:
    start: list[str] = field(default_factory=list)
    resume: list[str] = field(default_factory=list)
    flag: str | None = None

    def hooks(self) -> Hooks:
        async def start() -> list[str]:
            return self.start

        async def resume() -> list[str]:
            return self.resume

        return Hooks(start=start, resume_checks=resume, halt_flag=lambda: self.flag)


@dataclass
class Rig:
    w: World
    sm: StateMachine
    poller: CommandPoller
    flattener: Flattener
    checks: Checks

    async def send(self, type_: CommandType, payload: dict[str, Any] | None = None) -> CommandRow:
        with unit_of_work(self.w.factory) as s:
            cid = CommandRepository(s, self.w.clock).enqueue(type_, payload, requested_by="test").id
        assert await self.poller.run_once() == 1
        with self.w.factory() as s:
            row = s.get(CommandRow, cid)
            assert row is not None
            s.expunge(row)
            return row


async def rig(factory: sessionmaker[Session], clock: FakeClock, *, config_path: Path = EXAMPLE) -> Rig:
    w = make_world(factory, clock)
    sm = StateMachine("acc", Mode.DEMO, factory, clock, w.notifier)
    manager = PositionManager(CFG.position_management, CFG.risk.stops, magic=MAGIC, account=1)
    kw: dict[str, Any] = dict(
        broker=w.broker, market=w.broker.feed, executor=w.executor, factory=factory, clock=clock
    )
    flattener = Flattener(CFG, manager, sm, notifier=w.notifier, alert_after=2, **kw)
    checks = Checks()
    poller = CommandPoller(
        CFG, sm, checks.hooks(), manager=manager, flattener=flattener, config_path=config_path, **kw
    )
    return Rig(w, sm, poller, flattener, checks)


async def running(r: Rig) -> None:
    assert (await r.send(CommandType.START)).status is CommandStatus.DONE
    assert r.sm.state is EngineState.RUNNING


async def open_positions(r: Rig, n: int) -> list[int]:
    tickets = []
    for i in range(n):
        result = await r.w.executor.execute(
            open_intent(r.w.clock, key=f"{i:024d}"), await r.w.broker.symbol_spec("XAUUSD")
        )
        assert result.status is IntentStatus.FILLED
        assert result.position_id is not None
        tickets.append(result.position_id)
    return tickets


# ---------------------------------------------------------------- 5.4 kill switch


async def test_flatten_all_closes_five_positions_through_one_failure_then_halts(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    r = await rig(factory, clock)
    await running(r)
    tickets = await open_positions(r, 5)
    r.w.broker.inject(RejectWith(INVALID))  # the first close attempt is rejected once
    row = await r.send(CommandType.FLATTEN_ALL)
    assert row.status is CommandStatus.DONE
    assert row.result is not None
    assert (row.result["state"], sorted(row.result["closed"]), row.result["remaining"]) == (
        "HALTED",
        sorted(tickets),
        [],
    )
    assert r.flattener.attempts[tickets[0]] == 2  # retried on the next cycle
    assert [p for p in await r.w.broker.positions() if p.magic == MAGIC] == []
    with factory() as s:
        closes = s.scalars(select(OrderIntentRow).where(OrderIntentRow.kind == "CLOSE")).all()
    assert sorted(c.status.value for c in closes) == ["FILLED"] * 5 + ["REJECTED"]
    assert {c.close_reason for c in closes} == {CloseReason.FLATTEN}
    assert r.sm.state is EngineState.HALTED
    titles = [t for _, t, _ in r.w.notifier.sent]
    assert titles == ["Engine FLATTENING", "Engine HALTED"]  # one failure is not worth a page


async def test_the_kill_switch_keeps_trying_and_pages_when_a_close_keeps_failing(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    r = await rig(factory, clock)
    await running(r)
    (ticket,) = await open_positions(r, 1)
    r.w.broker.inject(*[RejectWith(INVALID)] * 5)
    row = await r.send(CommandType.FLATTEN_ALL)
    assert row.result is not None
    assert (row.status, row.result["state"], row.result["remaining"]) == (
        CommandStatus.DONE,
        "FLATTENING",
        [ticket],
    )
    assert r.sm.state is EngineState.FLATTENING  # the loop goes on
    assert (Severity.CRITICAL, f"Kill switch cannot close XAUUSD #{ticket}") in [
        (s, t) for s, t, _ in r.w.notifier.sent
    ]
    report = await r.flattener.run_once()  # the fault queue is empty now
    assert (report.done, r.sm.state) == (True, EngineState.HALTED)
    assert (await r.flattener.run_once()).closed == []  # nothing to do outside FLATTENING


async def test_flatten_with_nothing_open_halts_at_once(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    r = await rig(factory, clock)
    await running(r)
    row = await r.send(CommandType.FLATTEN_ALL)
    assert row.result == {"state": "HALTED", "closed": [], "remaining": [], "errors": []}


# ---------------------------------------------------------------- 5.3 commands


async def test_start_pause_resume_stop(factory: sessionmaker[Session], clock: FakeClock) -> None:
    r = await rig(factory, clock)
    r.checks.start = ["terminal is not connected"]
    failed = await r.send(CommandType.START)
    assert failed.status is CommandStatus.FAILED
    assert failed.result is not None
    assert "startup checks failed: terminal is not connected" in failed.result["error"]
    assert r.sm.state is EngineState.STOPPED
    r.checks.start = []
    await running(r)
    assert (await r.send(CommandType.PAUSE)).result == {"state": "PAUSED"}
    r.checks.resume = ["reconciliation deferred XAUUSD"]
    refused = await r.send(CommandType.RESUME)
    assert refused.status is CommandStatus.FAILED
    assert refused.result is not None and "not safe to resume" in refused.result["error"]  # noqa: PT018
    r.checks.resume = []
    assert (await r.send(CommandType.RESUME)).result == {"state": "RUNNING"}
    assert (await r.send(CommandType.STOP)).result == {"state": "STOPPED"}
    wrong = await r.send(CommandType.PAUSE)
    assert wrong.result is not None
    assert (wrong.status, wrong.result["error"]) == (CommandStatus.FAILED, "PAUSE is not allowed in STOPPED")
    resume_stopped = await r.send(CommandType.RESUME)
    assert resume_stopped.result is not None and "RESUME needs PAUSED" in resume_stopped.result["error"]  # noqa: PT018
    with factory() as s:
        updates = s.scalars(select(EventRow).where(EventRow.type == "command.updated")).all()
    assert len(updates) == 8


async def test_rearm_waits_for_the_guardian_flag(factory: sessionmaker[Session], clock: FakeClock) -> None:
    r = await rig(factory, clock)
    await running(r)
    await r.sm.fire(Trigger.GUARDIAN_HALT, "equity -4.1%")
    r.checks.flag = "DAILY_LOSS 4.1%"
    refused = await r.send(CommandType.REARM)
    assert refused.result is not None
    assert "halt flag is still set (DAILY_LOSS 4.1%)" in refused.result["error"]
    r.checks.flag = None
    assert (await r.send(CommandType.REARM)).result == {"state": "PAUSED"}


async def test_close_position_closes_only_engine_positions(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    r = await rig(factory, clock)
    await running(r)
    (ticket,) = await open_positions(r, 1)
    for payload, error in (({}, "numeric position_id"), ({"position_id": 999}, "no open position 999")):
        row = await r.send(CommandType.CLOSE_POSITION, payload)
        assert row.result is not None and error in row.result["error"]  # noqa: PT018
    foreign = await r.w.executor.execute(
        open_intent(r.w.clock, key="f" * 24, magic=777),
        await r.w.broker.symbol_spec("XAUUSD"),
    )
    row = await r.send(CommandType.CLOSE_POSITION, {"position_id": foreign.position_id})
    assert row.result is not None and "not the engine's (magic 777)" in row.result["error"]  # noqa: PT018
    done = await r.send(CommandType.CLOSE_POSITION, {"position_id": ticket})
    assert done.status is CommandStatus.DONE
    assert done.result is not None and done.result["status"] == "FILLED"  # noqa: PT018
    assert [p.ticket for p in await r.w.broker.positions()] == [foreign.position_id]
    with factory() as s:
        close = s.scalars(select(OrderIntentRow).where(OrderIntentRow.kind == "CLOSE")).one()
    assert close.close_reason is CloseReason.OPERATOR


async def test_reload_config_validates_and_records(
    factory: sessionmaker[Session], clock: FakeClock, tmp_path: Path
) -> None:
    path = tmp_path / "trading.yaml"
    path.write_text(EXAMPLE.read_text())
    r = await rig(factory, clock, config_path=path)
    first = await r.send(CommandType.RELOAD_CONFIG)
    assert first.result is not None and first.result["changed"] is True  # noqa: PT018
    again = await r.send(CommandType.RELOAD_CONFIG)
    assert again.result is not None
    assert (again.result["changed"], again.result["note"]) == (False, "unchanged")
    path.write_text(EXAMPLE.read_text().replace("risk_per_trade_pct: 0.5", "risk_per_trade_pct: 50"))
    bad = await r.send(CommandType.RELOAD_CONFIG)
    assert bad.status is CommandStatus.FAILED
    assert bad.result is not None and "config invalid, nothing changed" in bad.result["error"]  # noqa: PT018


async def test_set_mode_only_records_a_live_confirmation(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    r = await rig(factory, clock)
    for payload, error in (
        ({"mode": "DEMO"}, "set engine.mode in the config"),
        ({"mode": "MOON"}, "unknown mode"),
        ({"mode": "LIVE"}, "confirm"),
        ({"mode": "LIVE", "confirm": True}, "allow_live"),
    ):
        row = await r.send(CommandType.SET_MODE, payload)
        assert row.result is not None and error in row.result["error"], payload  # noqa: PT018
    r.poller._cfg = CFG.model_copy(
        update={"engine": CFG.engine.model_copy(update={"allow_live": True, "mode": Mode.SIM})}
    )
    ok = await r.send(CommandType.SET_MODE, {"mode": "LIVE", "confirm": True, "by": "anubrata"})
    assert ok.status is CommandStatus.DONE
    with factory() as s:
        (audit,) = s.scalars(select(AuditLogRow)).all()
    assert (audit.action, audit.actor) == (LIVE_CONFIRMATION, "anubrata")


async def test_learning_commands_unknown_types_and_crashing_handlers_fail_cleanly(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    r = await rig(factory, clock)
    audit = await r.send(CommandType.RUN_AUDIT)
    assert audit.result is not None and "learning.enabled is off" in audit.result["error"]  # noqa: PT018
    assert await r.poller.execute("LAUNCH_ROCKET", {}) == (
        False,
        {"error": "unknown command 'LAUNCH_ROCKET'"},
    )

    async def crash(_p: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("bug")

    r.poller._handlers[CommandType.PAUSE] = crash
    ok, result = await r.poller.execute("PAUSE", {})
    assert (ok, result["error"]) == (False, "RuntimeError: bug")
    assert await r.poller.run_once() == 0  # nothing pending


def test_stale_pending_commands_run_in_order(factory: sessionmaker[Session], clock: FakeClock) -> None:
    with unit_of_work(factory) as s:
        repo = CommandRepository(s, clock)
        a = repo.enqueue(CommandType.PAUSE, None, requested_by="x").id
        clock.advance(timedelta(seconds=1).total_seconds())
        repo.enqueue(CommandType.RESUME, None, requested_by="x")
        assert repo.claim_next().id == a  # type: ignore[union-attr]
