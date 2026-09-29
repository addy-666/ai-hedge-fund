"""Engine state machine (docs/01 §9, roadmap 5.1): the one place engine state changes.

    STOPPED --START--> STARTING --STARTED--> RUNNING        (or --RESTORED_PAUSED / RESTORED_HALTED / ...)
    any state --BOOT--> STARTING                            (a new engine process; see restore_trigger)
    RUNNING --PAUSE | ERROR_BUDGET | DISCONNECTED--> PAUSED --RESUME--> RUNNING
    RUNNING | PAUSED --LIMIT_BREACH | GUARDIAN_HALT--> HALTED --REARM--> PAUSED
    RUNNING | PAUSED | HALTED --FLATTEN_ALL--> FLATTENING --FLATTENED--> HALTED
    RUNNING | PAUSED --STOP--> STOPPED

A trigger that is not in the table is ILLEGAL (``IllegalTransition``) — except the listed self-loops, which
make repeated safety triggers harmless (a second breach while HALTED, a PAUSE while PAUSED). Every change is
persisted to ``engine_state`` and appended to ``events``; HALTED and FLATTENING also alert. After a restart
the engine never lands in RUNNING by itself (``restore``): RUNNING resumes into PAUSED, HALTED survives, an
interrupted FLATTENING continues.

Mode is orthogonal to state (``mode_problems``): DEMO needs a demo account, LIVE a real account plus
``engine.allow_live`` and an operator confirmation; SIM and PAPER never send real orders.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from aifund.domain.enums import AccountTradeMode, EngineState, Mode
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.system import EngineStateRepository, EventRepository
from aifund.ports.system import ClockPort, NotifierPort, Severity

S = EngineState


class Trigger(StrEnum):
    BOOT = "BOOT"  # the engine process started: whatever was persisted, it is STARTING now
    START = "START"
    STARTED = "STARTED"  # startup checks and reconciliation passed
    START_FAILED = "START_FAILED"
    RESTORED_PAUSED = "RESTORED_PAUSED"  # restart: was RUNNING/PAUSED (or unknown) -> PAUSED
    RESTORED_HALTED = "RESTORED_HALTED"  # restart: a HALT survives restarts
    RESTORED_FLATTENING = "RESTORED_FLATTENING"  # restart: an interrupted kill switch continues
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    ERROR_BUDGET = "ERROR_BUDGET"  # a money-path loop kept failing
    DISCONNECTED = "DISCONNECTED"  # the broker was unreachable too long
    LIMIT_BREACH = "LIMIT_BREACH"  # daily/weekly loss or drawdown limit
    GUARDIAN_HALT = "GUARDIAN_HALT"  # the Guardian EA's halt flag
    REARM = "REARM"
    FLATTEN_ALL = "FLATTEN_ALL"
    FLATTENED = "FLATTENED"  # every engine position is closed
    STOP = "STOP"


T = Trigger
_HALTING = (T.LIMIT_BREACH, T.GUARDIAN_HALT)
TRANSITIONS: dict[tuple[EngineState, Trigger], EngineState] = {
    (S.STOPPED, T.START): S.STARTING,
    **{(s, T.BOOT): S.STARTING for s in EngineState},
    (S.STARTING, T.STARTED): S.RUNNING,
    (S.STARTING, T.START_FAILED): S.STOPPED,
    (S.STARTING, T.RESTORED_PAUSED): S.PAUSED,
    (S.STARTING, T.RESTORED_HALTED): S.HALTED,
    (S.STARTING, T.RESTORED_FLATTENING): S.FLATTENING,
    (S.RUNNING, T.PAUSE): S.PAUSED,
    (S.RUNNING, T.ERROR_BUDGET): S.PAUSED,
    (S.RUNNING, T.DISCONNECTED): S.PAUSED,
    (S.PAUSED, T.RESUME): S.RUNNING,
    (S.RUNNING, T.STOP): S.STOPPED,
    (S.PAUSED, T.STOP): S.STOPPED,
    (S.HALTED, T.REARM): S.PAUSED,
    (S.FLATTENING, T.FLATTENED): S.HALTED,
    # self-loops: repeated safety triggers are harmless
    (S.PAUSED, T.PAUSE): S.PAUSED,
    (S.PAUSED, T.ERROR_BUDGET): S.PAUSED,
    (S.PAUSED, T.DISCONNECTED): S.PAUSED,
    (S.FLATTENING, T.FLATTEN_ALL): S.FLATTENING,
    **{(s, t): S.HALTED for s in (S.RUNNING, S.PAUSED, S.HALTED) for t in _HALTING},
    **{(S.FLATTENING, t): S.FLATTENING for t in _HALTING},
    **{(s, T.FLATTEN_ALL): S.FLATTENING for s in (S.RUNNING, S.PAUSED, S.HALTED)},
}

# what each state allows (docs/01 §9)
ENTRIES = frozenset({S.RUNNING})
POSITION_MANAGEMENT = frozenset({S.RUNNING, S.PAUSED, S.HALTED, S.FLATTENING})
RECONCILIATION = frozenset({S.RUNNING, S.PAUSED, S.HALTED, S.FLATTENING})
LEARNING = frozenset({S.RUNNING, S.PAUSED, S.HALTED})
ALERTING = {S.HALTED: Severity.CRITICAL, S.FLATTENING: Severity.CRITICAL, S.PAUSED: Severity.WARN}


class IllegalTransition(Exception):
    pass


def apply(state: EngineState, trigger: Trigger) -> EngineState:
    try:
        return TRANSITIONS[(state, trigger)]
    except KeyError:
        raise IllegalTransition(f"{trigger.value} is not allowed in {state.value}") from None


def restore_trigger(persisted: EngineState) -> Trigger:
    """How a restart resumes (from STARTING): never straight back into RUNNING."""
    if persisted is S.HALTED:
        return T.RESTORED_HALTED
    if persisted is S.FLATTENING:
        return T.RESTORED_FLATTENING
    return T.RESTORED_PAUSED


def mode_problems(
    mode: Mode, account: AccountTradeMode | None, *, allow_live: bool, live_confirmed: bool
) -> list[str]:
    """Why this mode may not run against this account (empty: fine). SIM needs no account."""
    problems = []
    if mode is Mode.DEMO and account is not AccountTradeMode.DEMO:
        problems.append(f"mode DEMO needs a demo account (connected: {account})")
    if mode is Mode.LIVE:
        if account is not AccountTradeMode.REAL:
            problems.append(f"mode LIVE needs a real account (connected: {account})")
        if not allow_live:
            problems.append("mode LIVE needs engine.allow_live: true")
        if not live_confirmed:
            problems.append("mode LIVE needs an operator confirmation (SET_MODE LIVE with confirm)")
    return problems


class StateMachine:
    """Persisted engine state for one account. Transitions are serialised by a lock."""

    def __init__(
        self,
        account_id: str,
        mode: Mode,
        factory: sessionmaker[Session],
        clock: ClockPort,
        notifier: NotifierPort,
        *,
        on_change: Callable[[EngineState, EngineState, Trigger], None] | None = None,
    ) -> None:
        self._account = account_id
        self._mode = mode
        self._factory = factory
        self._clock = clock
        self._notifier = notifier
        self._on_change = on_change
        self._lock = asyncio.Lock()
        with unit_of_work(factory) as s:
            row = EngineStateRepository(s, clock).get_or_create(account_id, mode)
            self._state, self._reason = row.state, row.halt_reason

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def halt_reason(self) -> str | None:
        return self._reason

    @property
    def entries_allowed(self) -> bool:
        return self._state in ENTRIES

    def can(self, trigger: Trigger) -> bool:
        return (self._state, trigger) in TRANSITIONS

    async def fire(self, trigger: Trigger, reason: str = "", **detail: Any) -> EngineState:
        async with self._lock:
            before = self._state
            after = apply(before, trigger)
            stopped = (S.HALTED, S.FLATTENING)
            if after not in stopped:
                halt_reason = None
            elif before in stopped and self._reason:
                halt_reason = (
                    self._reason
                )  # keep the original cause (a flatten after a breach, a second breach)
            else:
                halt_reason = reason
            await asyncio.to_thread(self._persist, before, after, trigger, halt_reason, reason, detail)
            self._state, self._reason = after, halt_reason
        if after is not before:
            severity = ALERTING.get(after)
            if severity is not None:
                await self._notifier.notify(
                    severity, f"Engine {after.value}", f"{trigger.value}: {reason}".strip(": ")
                )
            if self._on_change is not None:
                self._on_change(before, after, trigger)
        return after

    def _persist(
        self, before: EngineState, after: EngineState, trigger: Trigger, halt_reason: str | None,
        reason: str, detail: dict[str, Any],
    ) -> None:  # fmt: skip
        with unit_of_work(self._factory) as s:
            EngineStateRepository(s, self._clock).set_state(self._account, after, halt_reason=halt_reason)
            if after is not before:
                severity = ALERTING.get(after, Severity.INFO)
                payload = {
                    "from": before.value,
                    "to": after.value,
                    "trigger": trigger.value,
                    "reason": reason,
                }
                EventRepository(s, self._clock).append("engine.state", severity, {**payload, **detail})
