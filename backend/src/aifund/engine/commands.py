"""Operator commands (roadmap 5.3, docs/01 §4, docs/05): the API inserts ``commands`` rows; this runs them.

The poller claims the oldest PENDING command (→ RUNNING), runs its handler and finishes it DONE or FAILED with
a result, appending a ``command.updated`` event either way. A handler that raises fails its command — it never
takes the poller down. Handlers change engine state only through the state machine.

- START      STOPPED → STARTING → the startup checks (``Hooks.start``) → RUNNING, or back to STOPPED
- PAUSE      no new entries (RUNNING/PAUSED)
- RESUME     PAUSED → RUNNING only if the resume checks pass (``Hooks.resume_checks``: broker connected,
             reconciliation clean, no Guardian halt flag, loss limits clear)
- STOP       RUNNING/PAUSED → STOPPED (the process keeps polling for START)
- REARM      HALTED → PAUSED (the API demands re-auth first); refused while the Guardian halt flag is set
- FLATTEN_ALL  → FLATTENING; runs the kill switch for a few cycles and reports (its loop goes on until HALTED)
- CLOSE_POSITION {position_id}: closes one ENGINE position at the market (reason OPERATOR)
- RELOAD_CONFIG  validates the config file and records a new version; applies at the next restart
- SET_MODE {mode: LIVE, confirm: true}: records the operator's LIVE confirmation (audit log); the mode itself
             comes from ``engine.mode`` in the config and changes with a restart
- RUN_AUDIT, APPROVE_RULE {rule_id, version?, force?}, REJECT_RULE, RETIRE_RULE {rule_id}: the learning
             loop (engine/learning.py); FAILED when the engine runs without it (learning.enabled off)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.orm import Session, sessionmaker

from aifund.config.loader import ConfigError, load_trading_config
from aifund.config.trading_config import TradingConfig
from aifund.domain.enums import CloseReason, CommandType, EngineState, IntentStatus, Mode
from aifund.engine.kill_switch import Flattener, trigger_tf
from aifund.engine.learning import LearningError
from aifund.engine.state import StateMachine, Trigger
from aifund.execution.executor import Executor
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.system import (
    AuditLogRepository,
    CommandRepository,
    ConfigVersionRepository,
    EventRepository,
)
from aifund.ports.broker import BrokerPort, MarketDataPort
from aifund.ports.system import ClockPort, Severity
from aifund.risk.position_manager import PositionManager

log = structlog.get_logger(__name__)
LIVE_CONFIRMATION = "CONFIRM_LIVE"
PHASE_7 = {CommandType.RUN_AUDIT, CommandType.APPROVE_RULE, CommandType.REJECT_RULE, CommandType.RETIRE_RULE}


class CommandFailed(Exception):
    """A command that cannot be carried out now; its message becomes the command's result."""


@dataclass(frozen=True)
class Hooks:
    """What commands need from the running engine (the startup sequence wires these)."""

    start: Callable[[], Awaitable[list[str]]]  # startup checks + reconciliation; returns problems
    resume_checks: Callable[[], Awaitable[list[str]]]  # why RUNNING would be unsafe now
    halt_flag: Callable[[], str | None]  # the Guardian EA's halt reason, if its flag file exists
    learning: Callable[[CommandType, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None


class CommandPoller:
    def __init__(
        self,
        cfg: TradingConfig,
        state: StateMachine,
        hooks: Hooks,
        *,
        broker: BrokerPort,
        market: MarketDataPort,
        executor: Executor,
        manager: PositionManager,
        flattener: Flattener,
        factory: sessionmaker[Session],
        clock: ClockPort,
        config_path: Path,
        flatten_cycles: int = 5,
        flatten_wait_s: float = 1.0,
    ) -> None:
        self._cfg = cfg
        self._state = state
        self._hooks = hooks
        self._broker = broker
        self._market = market
        self._executor = executor
        self._manager = manager
        self._flattener = flattener
        self._factory = factory
        self._clock = clock
        self._config_path = config_path
        self._flatten_cycles = flatten_cycles
        self._flatten_wait = flatten_wait_s
        self._handlers: dict[CommandType, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            CommandType.START: self._start,
            CommandType.PAUSE: self._pause,
            CommandType.RESUME: self._resume,
            CommandType.STOP: self._stop,
            CommandType.REARM: self._rearm,
            CommandType.FLATTEN_ALL: self._flatten_all,
            CommandType.CLOSE_POSITION: self._close_position,
            CommandType.RELOAD_CONFIG: self._reload_config,
            CommandType.SET_MODE: self._set_mode,
        }

    def _tx(self, fn: Callable[[Session], Any]) -> Any:
        with unit_of_work(self._factory) as s:
            return fn(s)

    async def run_once(self) -> int:
        """Execute every pending command, oldest first; returns how many ran."""
        ran = 0
        while True:
            claimed = await asyncio.to_thread(self._tx, lambda s: _claim(CommandRepository(s, self._clock)))
            if claimed is None:
                return ran
            command_id, type_, payload, requested_by = claimed
            ok, result = await self.execute(type_, payload or {})
            log.info("command.finished", command_id=command_id, type=type_, ok=ok, by=requested_by)

            await asyncio.to_thread(self._tx, partial(self._finish, command_id, type_, ok, result))
            ran += 1

    def _finish(self, cid: str, type_: str, ok: bool, result: dict[str, Any], s: Session) -> None:
        CommandRepository(s, self._clock).finish(cid, ok=ok, result=result)
        status = "DONE" if ok else "FAILED"
        EventRepository(s, self._clock).append(
            "command.updated",
            Severity.INFO if ok else Severity.WARN,
            {"command_id": cid, "type": type_, "status": status, "result": result},
        )

    async def execute(self, type_: str, payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        try:
            command = CommandType(type_)
        except ValueError:
            return False, {"error": f"unknown command {type_!r}"}
        try:
            if command in PHASE_7:
                if self._hooks.learning is None:
                    raise CommandFailed(f"{command.value} needs the learning loop (learning.enabled is off)")
                try:
                    return True, await self._hooks.learning(command, payload)
                except LearningError as exc:
                    raise CommandFailed(str(exc)) from exc
            return True, await self._handlers[command](payload)
        except CommandFailed as exc:
            return False, {"error": str(exc), "state": self._state.state.value}
        except Exception as exc:
            log.exception("command.crashed", type=type_)
            return False, {"error": f"{type(exc).__name__}: {exc}", "state": self._state.state.value}

    async def _fire(self, trigger: Trigger, reason: str) -> EngineState:
        if not self._state.can(trigger):
            raise CommandFailed(f"{trigger.value} is not allowed in {self._state.state.value}")
        return await self._state.fire(trigger, reason)

    # ------------------------------------------------------------------ handlers

    async def _start(self, _p: dict[str, Any]) -> dict[str, Any]:
        await self._fire(Trigger.START, "operator")
        problems = await self._hooks.start()
        if problems:
            await self._state.fire(Trigger.START_FAILED, "; ".join(problems))
            raise CommandFailed("startup checks failed: " + "; ".join(problems))
        return {"state": (await self._state.fire(Trigger.STARTED, "startup checks passed")).value}

    async def _pause(self, _p: dict[str, Any]) -> dict[str, Any]:
        return {"state": (await self._fire(Trigger.PAUSE, "operator")).value}

    async def _resume(self, _p: dict[str, Any]) -> dict[str, Any]:
        if self._state.state is not EngineState.PAUSED:
            raise CommandFailed(f"RESUME needs PAUSED, engine is {self._state.state.value}")
        problems = await self._hooks.resume_checks()
        if problems:
            raise CommandFailed("not safe to resume: " + "; ".join(problems))
        return {"state": (await self._fire(Trigger.RESUME, "operator")).value}

    async def _stop(self, _p: dict[str, Any]) -> dict[str, Any]:
        return {"state": (await self._fire(Trigger.STOP, "operator")).value}

    async def _rearm(self, _p: dict[str, Any]) -> dict[str, Any]:
        flag = self._hooks.halt_flag()
        if flag is not None:
            raise CommandFailed(f"the Guardian EA halt flag is still set ({flag}): clear it first")
        return {"state": (await self._fire(Trigger.REARM, "operator")).value}

    async def _flatten_all(self, _p: dict[str, Any]) -> dict[str, Any]:
        await self._fire(Trigger.FLATTEN_ALL, "operator kill switch")
        closed: list[int] = []
        report = None
        for cycle in range(self._flatten_cycles):
            if cycle:
                await self._clock.sleep(self._flatten_wait)
            report = await self._flattener.run_once()
            closed += report.closed
            if report.done or self._state.state is not EngineState.FLATTENING:
                break
        remaining = report.remaining if report is not None else []
        return {
            "state": self._state.state.value, "closed": closed, "remaining": remaining,
            "errors": report.errors if report is not None else [],
        }  # fmt: skip

    async def _close_position(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            ticket = int(payload["position_id"])
        except (KeyError, TypeError, ValueError):
            raise CommandFailed("CLOSE_POSITION needs a numeric position_id") from None
        positions = await self._broker.positions()
        pos = next((p for p in positions if p.ticket == ticket), None)
        if pos is None:
            raise CommandFailed(f"no open position {ticket}")
        if pos.magic != self._cfg.engine.magic:
            raise CommandFailed(
                f"position {ticket} is not the engine's (magic {pos.magic}): use the terminal"
            )
        tick = await self._market.tick(pos.symbol)
        if tick is None:
            raise CommandFailed(f"no quote for {pos.symbol}")
        spec = await self._broker.symbol_spec(pos.symbol)
        trigger = trigger_tf(self._cfg, pos.symbol)
        attempt = int(self._clock.now().timestamp())  # one key per request
        intent = self._manager.operator_close(
            pos, tick, trigger, self._clock.now(), reason=CloseReason.OPERATOR, label="close", attempt=attempt
        )
        result = await self._executor.execute(intent, spec)
        if result.status is not IntentStatus.FILLED:
            raise CommandFailed(f"close {result.status}: {result.detail}")
        return {"position_id": ticket, "status": result.status.value, "price": str(result.fill_price)}

    async def _reload_config(self, _p: dict[str, Any]) -> dict[str, Any]:
        try:
            loaded = load_trading_config(self._config_path)
        except ConfigError as exc:
            raise CommandFailed(f"config invalid, nothing changed: {exc}") from exc

        def record(s: Session) -> tuple[str, bool]:
            before = ConfigVersionRepository(s, self._clock).latest()
            row = ConfigVersionRepository(s, self._clock).record(loaded, created_by="RELOAD_CONFIG")
            return row.id, before is None or before.id != row.id

        version, changed = await asyncio.to_thread(self._tx, record)
        return {
            "config_version": version, "changed": changed,
            "note": "validated and recorded; restart (PAUSE first) to apply" if changed else "unchanged",
        }  # fmt: skip

    async def _set_mode(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            mode = Mode(str(payload.get("mode")))
        except ValueError:
            raise CommandFailed(f"unknown mode {payload.get('mode')!r}") from None
        if mode is not Mode.LIVE:
            raise CommandFailed("set engine.mode in the config and restart; SET_MODE only confirms LIVE")
        if payload.get("confirm") is not True:
            raise CommandFailed('LIVE needs {"confirm": true}')
        if not self._cfg.engine.allow_live:
            raise CommandFailed("LIVE needs engine.allow_live: true in the config")

        def confirm(s: Session) -> None:
            AuditLogRepository(s, self._clock).record(
                actor=str(payload.get("by", "operator")), action=LIVE_CONFIRMATION,
                after={"account": self._cfg.engine.account_label},
            )  # fmt: skip

        await asyncio.to_thread(self._tx, confirm)
        return {"confirmed": "LIVE", "note": "takes effect when the engine starts with engine.mode: LIVE"}


def _claim(repo: CommandRepository) -> tuple[str, str, dict[str, Any] | None, str] | None:
    row = repo.claim_next()
    return None if row is None else (row.id, row.type, row.payload, row.requested_by)
