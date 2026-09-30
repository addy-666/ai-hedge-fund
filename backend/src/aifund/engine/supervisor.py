"""Supervised loops (docs/01 §6, roadmap 5.2): every engine loop runs under one supervisor.

Each loop is a ``run_once`` coroutine with a cadence and the engine states it runs in. The supervisor calls
it, beats its heartbeat (``heartbeats`` row ``loop.<name>``), and on an exception logs it with context, beats
an error heartbeat and retries with exponential backoff (a crashing loop restarts; it never takes the engine
down). Failures are counted in a sliding window: when a loop exhausts its error budget an alert goes out,
and for a MONEY-PATH loop (decisions, positions, reconciliation, equity, the kill switch) the engine moves to
PAUSED — no new entries while the part that watches the money is broken. Recovery is not automatic: the
operator resumes after looking.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog
from sqlalchemy.orm import Session, sessionmaker

from aifund.domain.enums import EngineState
from aifund.engine.state import StateMachine, Trigger
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.system import HeartbeatRepository
from aifund.ports.system import ClockPort, NotifierPort, Severity

log = structlog.get_logger(__name__)
ALL_STATES = frozenset(EngineState)


@dataclass(frozen=True)
class LoopSpec:
    name: str
    interval_s: float
    run_once: Callable[[], Awaitable[object]]
    runs_in: frozenset[EngineState] = ALL_STATES - {EngineState.STOPPED}
    money_path: bool = False
    error_budget: int = 5  # failures within ``budget_window`` that exhaust the budget
    budget_window: timedelta = timedelta(minutes=5)
    max_backoff_s: float = 60.0


@dataclass
class LoopHealth:
    runs: int = 0
    failures: int = 0
    consecutive: int = 0
    recent: deque[datetime] = field(default_factory=deque)
    last_error: str | None = None
    budget_exhausted: int = 0


class Supervisor:
    def __init__(
        self,
        loops: Iterable[LoopSpec],
        state: StateMachine,
        *,
        factory: sessionmaker[Session],
        clock: ClockPort,
        notifier: NotifierPort,
    ) -> None:
        self.loops = list(loops)
        names = [spec.name for spec in self.loops]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate loop names: {names}")
        self._state = state
        self._factory = factory
        self._clock = clock
        self._notifier = notifier
        self.health = {spec.name: LoopHealth() for spec in self.loops}

    async def run(self, stop: asyncio.Event) -> None:
        async with asyncio.TaskGroup() as group:
            for spec in self.loops:
                group.create_task(self.run_loop(spec, stop), name=f"loop.{spec.name}")

    async def run_loop(
        self, spec: LoopSpec, stop: asyncio.Event, *, max_iterations: int | None = None
    ) -> None:
        iterations = 0
        while not stop.is_set() and (max_iterations is None or iterations < max_iterations):
            iterations += 1
            delay = spec.interval_s
            if self._state.state in spec.runs_in:
                delay = await self.step(spec)
            await self._sleep(delay, stop)

    async def step(self, spec: LoopSpec) -> float:
        """One supervised run; returns how long to wait before the next."""
        health = self.health[spec.name]
        health.runs += 1
        try:
            await spec.run_once()
        except Exception as exc:
            return await self._failed(spec, health, exc)
        health.consecutive = 0
        await self._beat(spec.name, "ok", {"runs": health.runs, "failures": health.failures})
        return spec.interval_s

    async def _failed(self, spec: LoopSpec, health: LoopHealth, exc: Exception) -> float:
        now = self._clock.now()
        health.failures += 1
        health.consecutive += 1
        health.last_error = f"{type(exc).__name__}: {exc}"[:500]
        health.recent.append(now)
        while health.recent and now - health.recent[0] > spec.budget_window:
            health.recent.popleft()
        log.error("loop.failed", loop=spec.name, error=health.last_error, consecutive=health.consecutive)
        await self._beat(spec.name, "error", {"failures": health.failures, "last_error": health.last_error})
        if len(health.recent) >= spec.error_budget:
            health.recent.clear()
            health.budget_exhausted += 1
            try:
                await self._exhausted(spec, health)
            except Exception as err:  # handling a failure must never take the process down (roadmap 9.2)
                log.critical("loop.exhausted_unhandled", loop=spec.name, error=f"{type(err).__name__}: {err}")
        backoff: float = spec.interval_s * 2.0 ** min(health.consecutive, 16)
        return min(backoff, spec.max_backoff_s)

    async def _exhausted(self, spec: LoopSpec, health: LoopHealth) -> None:
        detail = f"{spec.error_budget} failures within {spec.budget_window}; last: {health.last_error}"
        if spec.money_path and self._state.can(Trigger.ERROR_BUDGET):
            await self._state.fire(Trigger.ERROR_BUDGET, f"loop {spec.name}: {detail}", loop=spec.name)
        else:
            await self._notifier.notify(Severity.WARN, f"Loop {spec.name} keeps failing", detail)

    async def _beat(self, name: str, status: str, detail: dict[str, object]) -> None:
        def write() -> None:
            with unit_of_work(self._factory) as s:
                HeartbeatRepository(s, self._clock).beat(f"loop.{name}", status, detail)

        try:
            await asyncio.to_thread(write)
        except Exception as exc:
            log.error("heartbeat.failed", loop=name, error=str(exc))

    async def _sleep(self, seconds: float, stop: asyncio.Event) -> None:
        sleeper = asyncio.ensure_future(self._clock.sleep(seconds))
        stopper = asyncio.ensure_future(stop.wait())
        _done, pending = await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
