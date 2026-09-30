"""Chaos suite (roadmap 9.2): the engine killed at random points, the database locked, the LLM down, the
broker timing out in storms — over seeded runs on the SimBroker. After every run, with the survivor booted
and reconciled:

- zero duplicates: every engine position at the broker came from exactly one filled OPEN intent, and no
  decision (bar) filled more than one;
- zero lost trades: the ``trades`` table matches the broker's deal history (``diff_ledger``, as nightly);
- zero orders on stale data: every opening fill happened within one trigger bar of the close of the bar it
  was decided on;
- nothing left mid-flight: no PENDING / SENT / RETRYING / UNKNOWN intent; every open engine position has a SL.

``CHAOS_RUNS`` sets how many kill runs (default 8; the nightly CI job runs 100).
"""

from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.llm.fake_llm import FakeLLM
from aifund.adapters.notify.null import NullNotifier
from aifund.adapters.sim.replay_feed import ReplayFeed
from aifund.adapters.sim.sim_broker import SimBroker, SimConfig
from aifund.agents.analyst import Analyst
from aifund.config.loader import load_trading_config
from aifund.config.trading_config import ProfileConfig, StrategyConfig, TradingConfig
from aifund.domain.enums import DealEntry, EngineState, IntentKind, IntentStatus, Mode, Timeframe
from aifund.engine.app import Engine, Options
from aifund.engine.guardian import HALT_FLAG, GuardianFiles
from aifund.persistence.db import make_engine, make_session_factory, unit_of_work
from aifund.persistence.repositories.system import EngineStateRepository
from aifund.persistence.tables import DecisionRow, EngineStateRow, OrderIntentRow, TradeRow
from aifund.ports.llm import LLMError
from aifund.reconcile.ledger_check import TradeFacts, diff_ledger
from tests.integration.conftest import alembic_config
from tests.integration.test_baseline_replay import PLAYBOOKS, START, WARMUP_DAYS, HourlyStub, echo_candidate
from tests.scenario.chaos import Chaos, ChaosBroker, arm_database, run_with_restarts
from tests.unit.risk.test_stops import XAU

pytestmark = pytest.mark.scenario
CONFIG = Path(__file__).resolve().parents[3] / "config" / "trading.example.yaml"
MAGIC = 20260928
RUNS = int(os.environ.get("CHAOS_RUNS", "8"))
HOURS = 6
OPEN_STATES = {IntentStatus.PENDING, IntentStatus.SENT, IntentStatus.RETRYING, IntentStatus.UNKNOWN}


def config(strategy: StrategyConfig) -> TradingConfig:
    cfg = load_trading_config(CONFIG).config
    profile = ProfileConfig(trigger_tf=Timeframe.M15, setup_tf=Timeframe.H1, context_tfs=[Timeframe.H4])
    return cfg.model_copy(
        update={
            "engine": cfg.engine.model_copy(
                update={
                    "mode": Mode.SIM,
                    "magic": MAGIC,
                    "account_label": "acc",
                    "auto_resume_after_crash": True,
                }
            ),
            "symbols": [s for s in cfg.symbols if s.canonical == "XAUUSD"],
            "profiles": {**cfg.profiles, "intraday_m15": profile},
            "strategy": strategy,
            "learning": cfg.learning.model_copy(update={"enabled": False}),
        }
    )


class World:
    def __init__(
        self, market: dict[Any, Any], db_url: str, cfg: TradingConfig, llm: FakeLLM | None = None,
        guardian: Path | None = None,
    ):  # fmt: skip
        from alembic import command

        command.upgrade(alembic_config(db_url), "head")
        self.db_url, self.cfg, self.llm, self.guardian = db_url, cfg, llm, guardian
        self.clock = FakeClock(START + timedelta(days=WARMUP_DAYS, seconds=5))
        self.feed = ReplayFeed(market, {"XAUUSD": XAU}, self.clock)
        self.broker = SimBroker(
            feed=self.feed, clock=self.clock, config=SimConfig(starting_balance=Decimal("10000"))
        )
        factory = make_session_factory(make_engine(db_url))
        with unit_of_work(factory) as s:  # it was running before the first kill
            repo = EngineStateRepository(s, self.clock)
            repo.get_or_create("acc", Mode.SIM)
            repo.set_state("acc", EngineState.RUNNING)

    def build(self, chaos: Chaos) -> Engine:
        factory = make_session_factory(make_engine(self.db_url))
        arm_database(factory, chaos)
        analyst = Analyst(self.llm, self.cfg.llm, playbooks=PLAYBOOKS) if self.llm is not None else None
        opts = Options(
            config_path=CONFIG, detectors=lambda _s, _r: [HourlyStub()], account_login=1, analyst=analyst,
            guardian=GuardianFiles(self.guardian) if self.guardian is not None else None,
        )  # fmt: skip
        broker = ChaosBroker(self.broker, chaos)
        return Engine(
            self.cfg, opts, broker=broker, market=self.feed, factory=factory, clock=self.clock,
            notifier=NullNotifier(),
        )  # fmt: skip

    async def run(self, chaos: Chaos, hours: float = HOURS) -> Any:
        return await run_with_restarts(
            self.build, chaos, until=self.clock.now() + timedelta(hours=hours), now=self.clock.now,
            advance=self.clock.advance, step=timedelta(seconds=10),
        )  # fmt: skip

    async def violations(self) -> list[str]:
        out: list[str] = []
        since = START
        deals = [d for d in await self.broker.deals_between(since, self.clock.now() + timedelta(days=1))
                 if d.magic == MAGIC]  # fmt: skip
        factory: sessionmaker[Session] = make_session_factory(make_engine(self.db_url))
        with factory() as s:
            intents = s.scalars(select(OrderIntentRow)).all()
            trades = s.scalars(select(TradeRow).where(TradeRow.account_id == "acc")).all()
            decisions = {d.id: d for d in s.scalars(select(DecisionRow)).all()}
            facts = [TradeFacts(t.position_id, t.symbol, t.status, t.open_time, t.volume_opened, t.net_pnl)
                     for t in trades]  # fmt: skip
            opens = [i for i in intents if i.kind is IntentKind.OPEN and i.status is IntentStatus.FILLED]
            # zero duplicates
            by_position: dict[int, list[OrderIntentRow]] = {}
            for i in opens:
                by_position.setdefault(i.position_id or -1, []).append(i)
            for d in deals:
                if d.entry is DealEntry.IN and len(by_position.get(d.position_id, [])) != 1:
                    out.append(
                        f"position {d.position_id}: {len(by_position.get(d.position_id, []))} filled opens"
                    )
            per_decision: dict[str | None, int] = {}
            for i in opens:
                per_decision[i.decision_id] = per_decision.get(i.decision_id, 0) + 1
            out += [f"decision {k} filled {n} opens" for k, n in per_decision.items() if n > 1]
            # zero orders on stale data
            fills = {d.position_id: d.time for d in deals if d.entry is DealEntry.IN}
            for i in opens:
                decision = decisions.get(i.decision_id or "")
                filled = fills.get(i.position_id or -1)
                if decision is None or filled is None:
                    continue
                late = filled - (decision.bar_time + timedelta(minutes=15))
                if late > timedelta(minutes=15):
                    out.append(f"intent {i.id} filled {late} after its bar closed")
            # nothing mid-flight
            out += [f"intent {i.id} left {i.status.value}" for i in intents if i.status in OPEN_STATES]
        factory.kw["bind"].dispose()
        # zero lost trades
        out += [d.render() for d in diff_ledger(facts, deals, magic=MAGIC, since=since)]
        for p in await self.broker.positions():
            if p.magic == MAGIC and not p.sl:
                out.append(f"position {p.position_id} without a stop loss")
        return out


@pytest.fixture(scope="module")
def market() -> dict[Any, Any]:
    from tests.fakes.synthetic import aggregate, random_walk_m1

    m1 = random_walk_m1("XAUUSD", START, (WARMUP_DAYS + 2) * 1440)
    series: dict[Any, Any] = {("XAUUSD", Timeframe.M1): m1}
    for tf in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
        series[("XAUUSD", tf)] = aggregate(m1, tf)
    return series


BASELINE = StrategyConfig(analyst_enabled=False, baseline_enabled=True)


async def test_the_undisturbed_run_trades(market: dict[Any, Any], tmp_path: Path) -> None:
    """The control: without chaos the same world opens trades (so the kill runs test something)."""
    w = World(market, f"sqlite:///{tmp_path / 'db.sqlite'}", config(BASELINE))
    chaos = Chaos.seeded(0)
    chaos.kills = set()
    run = await w.run(chaos)
    assert (run.kills, run.incarnations) == (0, 1)
    assert await w.violations() == []
    with make_session_factory(make_engine(w.db_url))() as s:
        filled = s.scalars(select(OrderIntentRow).where(OrderIntentRow.status == IntentStatus.FILLED)).all()
    assert len(filled) >= 2


@pytest.mark.parametrize("seed", range(1, RUNS + 1))
async def test_killed_at_random_points(market: dict[Any, Any], tmp_path: Path, seed: int) -> None:
    w = World(market, f"sqlite:///{tmp_path / 'db.sqlite'}", config(BASELINE))
    chaos = Chaos.seeded(seed)
    run = await w.run(chaos)
    assert run.kills >= 1, f"seed {seed}: no kill point was reached ({chaos.points} points)"
    assert await w.violations() == [], run.log


@pytest.mark.parametrize("seed", [101, 102, 103])
async def test_broker_timeout_storms(market: dict[Any, Any], tmp_path: Path, seed: int) -> None:
    w = World(market, f"sqlite:///{tmp_path / 'db.sqlite'}", config(BASELINE))
    chaos = Chaos.seeded(seed, broker_storm=0.15)
    await w.run(chaos)
    assert await w.violations() == [], chaos.log


@pytest.mark.parametrize("seed", [201, 202])
async def test_a_locked_database(market: dict[Any, Any], tmp_path: Path, seed: int) -> None:
    w = World(market, f"sqlite:///{tmp_path / 'db.sqlite'}", config(BASELINE))
    chaos = Chaos.seeded(seed, db_locked=0.10)
    await w.run(chaos)
    assert await w.violations() == [], chaos.log


async def test_an_llm_outage_sends_nothing_and_breaks_nothing(market: dict[Any, Any], tmp_path: Path) -> None:
    """The analyst decides (analyst_orders); its provider fails every other call and the process dies too."""
    calls = iter(range(10_000))

    def flaky(request: Any) -> Any:
        return LLMError("503 from the provider") if next(calls) % 2 else echo_candidate(request)

    strategy = StrategyConfig(analyst_enabled=True, analyst_orders=True)
    w = World(market, f"sqlite:///{tmp_path / 'db.sqlite'}", config(strategy), llm=FakeLLM([flaky]))
    chaos = Chaos.seeded(301)
    await w.run(chaos)
    assert await w.violations() == [], chaos.log
    with make_session_factory(make_engine(w.db_url))() as s:
        invalid = s.scalars(select(DecisionRow).where(DecisionRow.reason_code == "LLM_ERROR")).all()
    assert invalid  # the outage was felt, and ended as INVALID decisions, not orders


async def test_the_guardian_halt_holds_through_kills(market: dict[Any, Any], tmp_path: Path) -> None:
    """Roadmap 9.1 under chaos: the Guardian EA raises its halt flag mid-run while the engine keeps getting
    killed. From then on no position opens, every incarnation boots into HALTED, and the heartbeat file the
    EA watches carries the state."""
    w = World(market, f"sqlite:///{tmp_path / 'db.sqlite'}", config(BASELINE), guardian=tmp_path / "common")
    (tmp_path / "common").mkdir()
    await w.run(Chaos.seeded(401), hours=3)
    flagged = w.clock.now()
    (tmp_path / "common" / HALT_FLAG).write_text("HARD_DAILY_LOSS")
    await w.run(Chaos.seeded(402), hours=3)
    assert await w.violations() == []
    deals = await w.broker.deals_between(flagged, w.clock.now() + timedelta(days=1))
    assert [d for d in deals if d.magic == MAGIC and d.entry is DealEntry.IN] == []
    with make_session_factory(make_engine(w.db_url))() as s:
        state = s.scalars(select(EngineStateRow)).one()
    assert (state.state, state.halt_reason) == (EngineState.HALTED, "Guardian EA: HARD_DAILY_LOSS")
    assert (tmp_path / "common" / "heartbeat.txt").read_text().split()[2] == "HALTED"
