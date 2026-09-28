"""Replay the Phase 2 stack on exported MT5 history (roadmap task 2.8). Proves plumbing, not edge.

    cd backend
    uv run python scripts/replay.py --from 2026-06-01 --to 2026-09-27
    uv run python scripts/replay.py --symbols XAUUSD --context H4      # D1 needs ~15 months of export

Uses <repo>/data/history (from scripts/export_history.py), config/trading.yaml (or the example), the real
mtf_trend_pullback detector and the SimBroker. Writes a throw-away SQLite DB under data/replay/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config

from aifund.adapters.clock import FakeClock
from aifund.adapters.notify.null import NullNotifier
from aifund.adapters.sim.replay_feed import ReplayFeed
from aifund.adapters.sim.sim_broker import SimBroker, SimConfig
from aifund.config.loader import load_trading_config
from aifund.config.settings import PROJECT_ROOT, Settings
from aifund.config.trading_config import ProfileConfig, SymbolConfig
from aifund.domain.enums import Timeframe
from aifund.engine.equity import EquityTracker
from aifund.engine.pipeline import DecisionPipeline
from aifund.engine.position_loop import PositionLoop
from aifund.engine.replay import run_replay
from aifund.execution.executor import Executor
from aifund.market.bar_clock import BarClock
from aifund.persistence.db import make_engine, make_session_factory
from aifund.persistence.repositories.cursors import DecisionCursorStore
from aifund.reconcile.enrichment import Enricher
from aifund.reconcile.reconciler import Reconciler
from aifund.reconcile.virtual import VirtualTracker
from aifund.risk.manager import RiskManager
from aifund.risk.position_manager import PositionManager
from aifund.strategies.base import SetupDetector, TfRoles
from aifund.strategies.mtf_trend_pullback import MtfTrendPullback


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD (UTC)")
    p.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD (UTC)")
    p.add_argument("--symbols", default="", help="comma-separated broker symbols (default: all configured)")
    p.add_argument("--context", default="H4", help="context timeframes, e.g. H4 or H4,D1 (default H4)")
    p.add_argument("--history", default=str(PROJECT_ROOT / "data" / "history"))
    p.add_argument("--balance", default="10000")
    return p.parse_args(argv)


async def main(argv: list[str]) -> int:
    args = parse(argv)
    settings = Settings()
    config_path = (
        settings.CONFIG_PATH
        if settings.CONFIG_PATH.is_file()
        else PROJECT_ROOT / "config" / "trading.example.yaml"
    )
    cfg = load_trading_config(config_path).config
    wanted = {s.strip() for s in args.symbols.split(",") if s.strip()}
    available = set(json.loads((Path(args.history) / "specs.json").read_text()))
    symbols = [s for s in cfg.symbols if (not wanted or s.broker in wanted) and s.broker in available]
    skipped = [
        s.broker for s in cfg.symbols if s.broker not in available and (not wanted or s.broker in wanted)
    ]
    if skipped:
        print(f"no exported history for {skipped}: skipped (export them on Windows to include them)")
    cfg = cfg.model_copy(update={"symbols": symbols})
    context = [Timeframe(tf.strip()) for tf in args.context.split(",")]
    profile = ProfileConfig(trigger_tf=Timeframe.M15, setup_tf=Timeframe.H1, context_tfs=context)
    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)

    db_path = PROJECT_ROOT / "data" / "replay" / f"replay_{datetime.now(UTC):%Y%m%d_%H%M%S}.db"
    url = f"sqlite:///{db_path}"
    alembic_cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic_cfg.attributes["url"] = url
    command.upgrade(alembic_cfg, "head")
    factory = make_session_factory(make_engine(url))

    clock = FakeClock(start + timedelta(seconds=5))
    names = [s.broker for s in symbols]
    feed = ReplayFeed.from_history(Path(args.history), names, [Timeframe.M15, Timeframe.H1, *context], clock)
    broker = SimBroker(feed=feed, clock=clock, config=SimConfig(starting_balance=Decimal(args.balance)))
    executor = Executor(broker, feed, factory, clock, account_id="replay")
    risk = RiskManager(cfg.risk, magic=cfg.engine.magic, broker=broker, clock=clock)

    def detectors(_s: SymbolConfig, roles: TfRoles) -> list[SetupDetector]:
        return [MtfTrendPullback(roles)]

    pipeline = DecisionPipeline(
        cfg,
        broker=broker,
        market=feed,
        factory=factory,
        clock=clock,
        executor=executor,
        risk=risk,
        detectors=detectors,
        equity=EquityTracker(cfg.engine.trading_day_boundary_utc),
        account_id="replay",
        profile_override={s.profile: profile for s in symbols},
    )
    manager = PositionManager(cfg.position_management, cfg.risk.stops, magic=cfg.engine.magic, account=1)
    loop = PositionLoop(
        cfg, manager, broker=broker, market=feed, executor=executor, factory=factory, clock=clock
    )
    bar_clock = BarClock(feed, clock, [(n, Timeframe.M15) for n in names], DecisionCursorStore(factory))
    report = await run_replay(
        pipeline=pipeline,
        bar_clock=bar_clock,
        position_loop=loop,
        virtual_tracker=VirtualTracker(broker, feed, factory, clock),
        enricher=Enricher(
            broker,
            feed,
            factory,
            clock,
            NullNotifier(),
            trigger_tfs={s.broker: cfg.profiles[s.profile].trigger_tf for s in cfg.symbols},
        ),
        reconciler=Reconciler(
            broker, factory, clock, NullNotifier(), account_id="replay", magic=cfg.engine.magic
        ),
        broker=broker,
        clock=clock,
        factory=factory,
        magic=cfg.engine.magic,
        end=end,
        progress=lambda m: print(m, flush=True),
    )
    print(report.render())
    print(f"decisions, intents, trades and deals: {db_path}")
    return 1 if report.violations else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
