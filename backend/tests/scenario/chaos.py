"""Chaos harness (roadmap 9.2): the whole engine on the SimBroker, killed at random points and restarted.

A *kill* is ``Kill`` (a BaseException): no ``except Exception`` in the engine can catch it, so it unwinds the
process exactly like a crash — the transaction in flight is rolled back, nothing in memory survives. The
SimBroker (the exchange) and the SQLite file (the disk) do survive. Each incarnation is a fresh ``Engine`` on
a fresh SQLAlchemy engine, booted through the real startup sequence (UNKNOWN intents resolved from the
broker's
records, reconciliation) and auto-resumed (``engine.auto_resume_after_crash``).

Kill points are counted over every broker call (before it reaches the broker and after it returned — "the
order went out, the process died before recording it") and every database commit (before and after). A seeded
``Chaos`` picks which of those points kill.

Other faults a run can switch on: broker timeout storms (``BrokerUnavailable`` bursts), a locked database
(``OperationalError: database is locked`` on commits), and an LLM outage (the analyst's provider down).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from aifund.ports.broker import BrokerUnavailable


class Kill(BaseException):
    """The process dies here."""


@dataclass
class Chaos:
    """Counts kill points; kills at the chosen ones. Faults are switched on per run."""

    rng: random.Random
    kills: set[int] = field(default_factory=set)
    points: int = 0
    log: list[str] = field(default_factory=list)
    broker_storm: float = 0.0  # probability a broker call raises BrokerUnavailable
    db_locked: float = 0.0  # probability a commit fails with "database is locked"
    armed: bool = True
    pending: str | None = None  # a kill chosen inside a commit hook, delivered at the next point

    @classmethod
    def seeded(cls, seed: int, *, max_kills: int = 3, horizon: int = 4000, **faults: float) -> Chaos:
        rng = random.Random(seed)
        kills = {rng.randint(1, horizon) for _ in range(rng.randint(1, max_kills))}
        return cls(rng, kills, **faults)

    def point(self, where: str, *, defer: bool = False) -> None:
        if not self.armed:
            return
        if self.pending is not None:
            pending, self.pending = self.pending, None
            raise Kill(pending)
        self.points += 1
        if self.points in self.kills:
            self.log.append(f"kill #{self.points} at {where}")
            if defer:  # raising inside SQLAlchemy's after_commit is masked by its rollback: die just after it
                self.pending = where
                return
            raise Kill(where)


class ChaosBroker:
    """The SimBroker behind a kill / timeout-storm proxy (the same object also serves market data)."""

    def __init__(self, broker: Any, chaos: Chaos) -> None:
        self._broker = broker
        self._chaos = chaos

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._broker, name)
        if not callable(target) or name.startswith("_"):
            return target

        async def call(*args: Any, **kwargs: Any) -> Any:
            chaos = self._chaos
            chaos.point(f"broker.{name} (before)")
            if chaos.armed and chaos.broker_storm and chaos.rng.random() < chaos.broker_storm:
                raise BrokerUnavailable(f"chaos: {name} timed out")
            result = await target(*args, **kwargs)
            chaos.point(f"broker.{name} (after)")
            return result

        return call


def arm_database(factory: sessionmaker[Session], chaos: Chaos) -> None:
    """Kill points around every commit; with ``db_locked``, commits fail at random as a locked SQLite does."""

    def before(_session: Session) -> None:
        chaos.point("db.commit (before)")
        if chaos.armed and chaos.db_locked and chaos.rng.random() < chaos.db_locked:
            raise OperationalError("COMMIT", {}, Exception("database is locked"))

    def after(_session: Session) -> None:
        chaos.point("db.commit (after)", defer=True)

    event.listen(factory, "before_commit", before)
    event.listen(factory, "after_commit", after)


@dataclass
class Run:
    seed: int
    kills: int = 0
    incarnations: int = 0
    log: list[str] = field(default_factory=list)


async def run_with_restarts(
    build: Callable[[Chaos], Any],
    chaos: Chaos,
    *,
    until: datetime,
    now: Callable[[], datetime],
    advance: Callable[[float], None],
    step: timedelta,
    restart_delay_s: float = 45.0,
    max_incarnations: int = 40,
) -> Run:
    """Boot, run, get killed, restart … until ``until``; then a last, chaos-free boot (the survivor)."""
    run = Run(seed=0)
    while now() < until and run.incarnations < max_incarnations:
        run.incarnations += 1
        engine = build(chaos)
        try:
            try:
                await engine.boot()
            except Exception as exc:  # the entry point lets it end the process; the task restarts it
                chaos.log.append(f"boot failed: {type(exc).__name__}: {exc}")
                advance(restart_delay_s)
                continue
            await engine.simulate(until, step=step)
        except Kill:
            run.kills += 1
            advance(restart_delay_s)  # the process supervisor restarts it a little later
            continue
        finally:
            engine.factory.kw["bind"].dispose()  # the dead process's connections go with it
    chaos.armed = False
    survivor = build(chaos)
    await survivor.boot()
    await survivor.reconciler.run_once()
    run.log = chaos.log
    return run
