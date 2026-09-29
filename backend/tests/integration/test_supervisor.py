"""Supervised loops (roadmap 5.2): a crashing loop restarts; a failing money-path loop pauses the engine."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.notify.null import NullNotifier
from aifund.domain.enums import EngineState, Mode
from aifund.engine.state import StateMachine, Trigger
from aifund.engine.supervisor import LoopSpec, Supervisor
from aifund.persistence.repositories.system import HeartbeatRepository
from aifund.ports.system import Severity


class Flaky:
    """Fails ``fail`` times, then succeeds."""

    def __init__(self, fail: int) -> None:
        self.fail = fail
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1
        if self.calls <= self.fail:
            raise RuntimeError(f"boom {self.calls}")


async def running(factory: sessionmaker[Session], clock: FakeClock, notifier: NullNotifier) -> StateMachine:
    sm = StateMachine("acc", Mode.DEMO, factory, clock, notifier)
    await sm.fire(Trigger.START)
    await sm.fire(Trigger.STARTED)
    return sm


def supervisor(sm: StateMachine, factory: sessionmaker[Session], clock: FakeClock, notifier: NullNotifier,
               *loops: LoopSpec) -> Supervisor:  # fmt: skip
    return Supervisor(loops, sm, factory=factory, clock=clock, notifier=notifier)


async def test_a_crashing_loop_restarts_with_backoff_and_recovers(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    notifier = NullNotifier()
    sm = await running(factory, clock, notifier)
    flaky = Flaky(fail=3)
    spec = LoopSpec("virtual", 1.0, flaky, error_budget=10)
    sup = supervisor(sm, factory, clock, notifier, spec)
    start = clock.now()
    await sup.run_loop(spec, asyncio.Event(), max_iterations=5)
    assert flaky.calls == 5  # it kept running
    health = sup.health["virtual"]
    assert (health.failures, health.consecutive, health.last_error) == (3, 0, "RuntimeError: boom 3")
    assert clock.now() - start == timedelta(seconds=2 + 4 + 8 + 1 + 1)  # backoff 2, 4, 8 s, then the cadence
    with factory() as s:
        (beat,) = HeartbeatRepository(s, clock).all()
    assert (beat.component, beat.status, beat.detail) == ("loop.virtual", "ok", {"runs": 5, "failures": 3})
    assert sm.state is EngineState.RUNNING


async def test_a_money_path_loop_that_keeps_failing_pauses_the_engine(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    notifier = NullNotifier()
    sm = await running(factory, clock, notifier)
    spec = LoopSpec("reconciler", 30.0, Flaky(fail=100), money_path=True, error_budget=3,
                    budget_window=timedelta(hours=1), max_backoff_s=60)  # fmt: skip
    sup = supervisor(sm, factory, clock, notifier, spec)
    await sup.run_loop(spec, asyncio.Event(), max_iterations=3)
    assert sm.state is EngineState.PAUSED
    assert sup.health["reconciler"].budget_exhausted == 1
    ((severity, title, body),) = notifier.sent
    assert (severity, title) == (Severity.WARN, "Engine PAUSED")
    assert "ERROR_BUDGET: loop reconciler: 3 failures within 1:00:00" in body


async def test_other_loops_only_alert_and_old_failures_expire(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    notifier = NullNotifier()
    sm = await running(factory, clock, notifier)
    spec = LoopSpec("enricher", 60.0, Flaky(fail=100), error_budget=3, budget_window=timedelta(minutes=5),
                    max_backoff_s=600)  # fmt: skip
    sup = supervisor(sm, factory, clock, notifier, spec)
    await sup.run_loop(spec, asyncio.Event(), max_iterations=3)  # 120 s, 240 s, 480 s apart: never 3 in 5 min
    assert (sup.health["enricher"].budget_exhausted, notifier.sent) == (0, [])
    fast = LoopSpec("enricher2", 1.0, Flaky(fail=100), error_budget=3)
    sup2 = supervisor(sm, factory, clock, notifier, fast)
    await sup2.run_loop(fast, asyncio.Event(), max_iterations=3)
    assert sm.state is EngineState.RUNNING  # not a money-path loop
    assert [(s, t) for s, t, _ in notifier.sent] == [(Severity.WARN, "Loop enricher2 keeps failing")]


async def test_loops_run_only_in_their_states_and_stop_on_request(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    notifier = NullNotifier()
    sm = StateMachine("acc", Mode.DEMO, factory, clock, notifier)  # STOPPED
    entries = Flaky(0)
    commands = Flaky(0)
    sup = supervisor(
        sm, factory, clock, notifier,
        LoopSpec("decisions", 2.0, entries, runs_in=frozenset({EngineState.RUNNING})),
        LoopSpec("commands", 1.0, commands, runs_in=frozenset(EngineState)),
    )  # fmt: skip
    stop = asyncio.Event()

    async def later() -> None:
        while commands.calls < 5:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(sup.run(stop), later())
    assert entries.calls == 0  # nothing trades while STOPPED
    assert commands.calls >= 5


def test_loop_names_must_be_unique(factory: sessionmaker[Session], clock: FakeClock) -> None:
    sm = StateMachine("acc", Mode.DEMO, factory, clock, NullNotifier())
    with pytest.raises(ValueError, match="duplicate loop names"):
        supervisor(sm, factory, clock, NullNotifier(), LoopSpec("a", 1, Flaky(0)), LoopSpec("a", 1, Flaky(0)))


async def test_a_heartbeat_failure_does_not_stop_the_loop(
    clock: FakeClock, factory: sessionmaker[Session]
) -> None:
    sm = await running(factory, clock, NullNotifier())
    ok = Flaky(0)
    spec = LoopSpec("x", 1.0, ok)

    def broken() -> Session:
        raise OSError("disk full")

    sup = Supervisor([spec], sm, factory=broken, clock=clock, notifier=NullNotifier())  # type: ignore[arg-type]
    await sup.run_loop(spec, asyncio.Event(), max_iterations=2)
    assert ok.calls == 2
