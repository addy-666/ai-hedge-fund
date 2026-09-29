"""``python -m aifund.engine`` — the engine process (roadmap 5.6).

    cd backend
    uv run python -m aifund.engine                         # BROKER=mt5 (Windows): DEMO / LIVE on the terminal
    BROKER=sim uv run python -m aifund.engine --days 2     # the whole engine replayed over data/history (SIM)

The trading mode is ``engine.mode`` in the config. PAPER (live data, simulated fills) needs a live-data
simulated broker that does not exist yet: run DEMO with ``strategy.dry_run: true`` for a no-orders run on
live data.
Exit codes: 0 stopped cleanly, 2 could not start (the reason is printed and logged).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from aifund.adapters import history_store as hs
from aifund.adapters.clock import FakeClock, SystemClock
from aifund.adapters.llm.deepseek import DeepSeekClient
from aifund.adapters.notify.telegram import FanOut, HealthchecksPinger, LogNotifier, TelegramNotifier
from aifund.adapters.sim.replay_feed import ReplayFeed
from aifund.adapters.sim.sim_broker import SimBroker, SimConfig
from aifund.agents.analyst import Analyst
from aifund.agents.auditor import Auditor
from aifund.agents.reviewer import Reviewer
from aifund.config.evidence import load_evidence, load_g_llm
from aifund.config.loader import ConfigError, LoadedConfig, load_trading_config
from aifund.config.settings import PROJECT_ROOT, BrokerKind, Settings
from aifund.config.trading_config import TradingConfig
from aifund.domain.enums import EngineState, Mode, Timeframe
from aifund.engine.app import Engine, Options
from aifund.engine.commands import LIVE_CONFIRMATION
from aifund.engine.detectors import detector_factory
from aifund.engine.guardian import GuardianFiles
from aifund.engine.news_feed import CalendarFile
from aifund.engine.state import Trigger
from aifund.observability import configure_logging, get_logger, register_settings_secrets
from aifund.persistence.db import make_engine, make_session_factory, unit_of_work
from aifund.persistence.migrations import alembic_config, pending_migration
from aifund.persistence.repositories.llm import LLMCallRepository
from aifund.persistence.repositories.system import ConfigVersionRepository
from aifund.persistence.tables import AuditLogRow
from aifund.ports.system import ClockPort, NotifierPort
from aifund.vault.review_exporter import ReviewExporter

PLAYBOOKS = PROJECT_ROOT / "config" / "playbooks"
EVIDENCE = PROJECT_ROOT / "config" / "evidence"
EXPORTS = PROJECT_ROOT / "exports" / "vault"
log = get_logger("aifund.engine")


class StartupError(Exception):
    pass


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=float, default=2.0, help="SIM: simulate the last N days of the export")
    p.add_argument("--history", default=str(PROJECT_ROOT / "data" / "history"), help="SIM: the export")
    return p.parse_args(argv)


def notifier_for(
    settings: Settings, cfg: TradingConfig, client: httpx.AsyncClient, clock: ClockPort
) -> NotifierPort:
    token, chat = settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID
    if cfg.alerts.telegram and token is not None and chat:
        telegram = TelegramNotifier(
            token.get_secret_value(), chat, client, clock, account=cfg.engine.account_label
        )
        return FanOut(LogNotifier(), telegram)
    return LogNotifier()


def _llm(
    settings: Settings, cfg: TradingConfig, factory: Any, clock: ClockPort, notifier: NotifierPort,
    client: httpx.AsyncClient,
) -> DeepSeekClient:  # fmt: skip
    assert settings.DEEPSEEK_API_KEY is not None
    return DeepSeekClient(
        cfg.llm,
        settings.DEEPSEEK_API_KEY.get_secret_value(),
        factory=factory,
        clock=clock,
        notifier=notifier,
        http_client=client,
    )


def _recorder(factory: Any, clock: ClockPort) -> Any:
    async def record(call_id: str, parsed: dict[str, Any] | None, valid: bool, error: str | None) -> None:
        def write() -> None:
            with unit_of_work(factory) as s:
                LLMCallRepository(s, clock).record_parse(call_id, parsed=parsed, valid=valid, error=error)

        await asyncio.to_thread(write)

    return record


def analyst_for(
    settings: Settings,
    cfg: TradingConfig,
    factory: Any,
    clock: ClockPort,
    notifier: NotifierPort,
    client: httpx.AsyncClient,
) -> Analyst | None:
    if not cfg.strategy.analyst_enabled:
        return None
    if settings.DEEPSEEK_API_KEY is None:
        raise StartupError("strategy.analyst_enabled needs DEEPSEEK_API_KEY in .env (or run the baseline)")
    return Analyst(
        _llm(settings, cfg, factory, clock, notifier, client),
        cfg.llm,
        playbooks=PLAYBOOKS,
        version=cfg.strategy.analyst_prompt_version,
        record_parse=_recorder(factory, clock),
    )


def learners_for(
    settings: Settings,
    cfg: TradingConfig,
    factory: Any,
    clock: ClockPort,
    notifier: NotifierPort,
    client: httpx.AsyncClient,
) -> tuple[Reviewer | None, Auditor | None]:
    """The trade reviewer and the auditor (docs/04). Without an API key the loop still mines and validates;
    surviving clusters become candidates directly and trades go unreviewed."""
    if not cfg.learning.enabled:
        return None, None
    if settings.DEEPSEEK_API_KEY is None:
        log.info(
            "learning.no_llm", detail="no DEEPSEEK_API_KEY: no trade reviews, the miner proposes directly"
        )
        return None, None
    llm = _llm(settings, cfg, factory, clock, notifier, client)
    record = _recorder(factory, clock)
    return Reviewer(llm, cfg.llm, record_parse=record), Auditor(llm, cfg.llm, record_parse=record)


def prepare(settings: Settings, database_url: str) -> tuple[LoadedConfig, Any, bool]:
    """Config, a migrated database, the LIVE confirmation. StartupError explains any refusal."""
    try:
        loaded = load_trading_config(settings.CONFIG_PATH)
    except ConfigError as exc:
        raise StartupError(f"trading config: {exc}") from exc
    if loaded.config.engine.mode is Mode.PAPER:
        raise StartupError("mode PAPER is not available yet: use DEMO with strategy.dry_run: true")
    engine = make_engine(database_url)
    problem = pending_migration(engine, database_url)
    if problem is not None:
        raise StartupError(problem)
    factory = make_session_factory(engine)
    with unit_of_work(factory) as s:
        ConfigVersionRepository(s, SystemClock()).record(loaded, created_by="engine")
        live = (
            s.scalars(select(AuditLogRow).where(AuditLogRow.action == LIVE_CONFIRMATION)).first() is not None
        )
    return loaded, factory, live


async def run_mt5(settings: Settings, loaded: LoadedConfig, factory: Any, live_confirmed: bool) -> int:
    from aifund.adapters.mt5.gateway import MT5Credentials, MT5Gateway  # Windows only

    cfg = loaded.config
    if not (settings.MT5_LOGIN and settings.MT5_PASSWORD and settings.MT5_SERVER):
        raise StartupError("BROKER=mt5 needs MT5_LOGIN, MT5_PASSWORD and MT5_SERVER in .env")
    clock = SystemClock()
    async with httpx.AsyncClient() as client:
        notifier = notifier_for(settings, cfg, client, clock)
        gw = MT5Gateway(
            MT5Credentials(
                login=settings.MT5_LOGIN,
                password=settings.MT5_PASSWORD.get_secret_value(),
                server=settings.MT5_SERVER,
                path=settings.MT5_PATH,
                portable=settings.MT5_PORTABLE,
            ),
            clock=clock,
        )
        try:
            account = await gw.connect()
            symbols = [s.broker for s in cfg.symbols]

            async def broker_checks() -> list[str]:
                report = await gw.startup_checks(symbols, mode=cfg.engine.mode, require_hedging=True)
                return report.problems

            guardian_dir = await gw.common_files_dir() or PROJECT_ROOT / "data" / "guardian"
            pinger = (
                HealthchecksPinger(settings.HEALTHCHECKS_URL.get_secret_value(), client)
                if settings.HEALTHCHECKS_URL
                else None
            )
            opts = Options(
                config_path=settings.CONFIG_PATH,
                detectors=detector_factory(cfg, PLAYBOOKS),
                account_login=account.login,
                evidence=load_evidence(EVIDENCE),
                g_llm=load_g_llm(EVIDENCE),
                analyst=analyst_for(settings, cfg, factory, clock, notifier, client),
                learners=learners_for(settings, cfg, factory, clock, notifier, client),
                vault_exporter=ReviewExporter(factory, clock, out_dir=EXPORTS, playbooks=PLAYBOOKS),
                guardian=GuardianFiles(guardian_dir),
                calendar=CalendarFile(guardian_dir / "calendar.csv", clock),
                healthchecks=pinger.ping if pinger is not None else None,
                broker_checks=broker_checks,
                live_confirmed=live_confirmed,
            )
            engine = Engine(cfg, opts, broker=gw, market=gw, factory=factory, clock=clock, notifier=notifier)
            state = await engine.boot()
            log.info(
                "engine.booted",
                state=state.value,
                mode=cfg.engine.mode.value,
                account=cfg.engine.account_label,
            )
            stop = asyncio.Event()
            _on_signals(stop)
            await engine.run(stop)
        finally:
            await gw.close()
    return 0


async def run_sim(settings: Settings, loaded: LoadedConfig, args: argparse.Namespace) -> int:
    """The full engine over the exported history on a fake clock, in a throw-away database."""
    from alembic import command

    cfg = loaded.config.model_copy(
        update={"engine": loaded.config.engine.model_copy(update={"mode": Mode.SIM})}
    )
    root = Path(args.history)
    manifest = hs.read_manifest(root)
    end = manifest.end.replace(second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    db = PROJECT_ROOT / "data" / "sim" / f"engine_{start:%Y%m%d}_{end:%Y%m%d}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)
    url = f"sqlite:///{db}"
    command.upgrade(alembic_config(url), "head")
    factory = make_session_factory(make_engine(url))
    clock = FakeClock(start)
    symbols = [s.broker for s in cfg.symbols if s.broker in hs.read_specs(root)]
    cfg = cfg.model_copy(update={"symbols": [s for s in cfg.symbols if s.broker in symbols]})
    timeframes = sorted(
        {tf for p in cfg.profiles.values() for tf in (p.trigger_tf, p.setup_tf, *p.context_tfs)}
        | {Timeframe.M1},
        key=lambda t: t.minutes,
    )
    feed = ReplayFeed.from_history(root, symbols, timeframes, clock)
    broker = SimBroker(feed=feed, clock=clock, config=SimConfig(starting_balance=Decimal("10000")))
    async with httpx.AsyncClient() as client:
        notifier = LogNotifier()
        opts = Options(
            config_path=settings.CONFIG_PATH,
            detectors=detector_factory(cfg, PLAYBOOKS),
            account_login=1,
            analyst=analyst_for(settings, cfg, factory, clock, notifier, client),
            learners=learners_for(settings, cfg, factory, clock, notifier, client),
        )
        engine = Engine(
            cfg, opts, broker=broker, market=feed, factory=factory, clock=clock, notifier=notifier
        )
        state = await engine.boot()
        if state is EngineState.PAUSED:
            await engine.state.fire(Trigger.RESUME, "SIM: no operator")
        print(
            f"SIM {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC on {symbols}; database {db}", flush=True
        )
        await engine.simulate(end, step=timedelta(seconds=5))
    health = {name: (h.runs, h.failures) for name, h in engine.supervisor.health.items()}
    print(f"final state {engine.state.state.value}; loops (runs, failures): {health}")
    return 0


def _on_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(
            NotImplementedError, AttributeError
        ):  # Windows: Ctrl+C raises KeyboardInterrupt
            loop.add_signal_handler(sig, stop.set)


async def amain(argv: list[str]) -> int:
    args = parse(argv)
    settings = Settings()
    register_settings_secrets(settings)
    configure_logging("engine", log_dir=PROJECT_ROOT / "logs")
    try:
        if settings.BROKER is BrokerKind.SIM:
            try:
                loaded = load_trading_config(settings.CONFIG_PATH)
            except ConfigError as exc:
                raise StartupError(f"trading config: {exc}") from exc
            return await run_sim(settings, loaded, args)
        loaded, factory, live = prepare(settings, settings.DATABASE_URL)
        return await run_mt5(settings, loaded, factory, live)
    except (StartupError, ConfigError) as exc:
        print(f"engine not started: {exc}", file=sys.stderr)
        log.error("engine.not_started", reason=str(exc))
        return 2


def main() -> None:
    try:
        sys.exit(asyncio.run(amain(sys.argv[1:])))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
