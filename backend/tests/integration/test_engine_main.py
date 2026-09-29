"""``python -m aifund.engine`` (roadmap 5.6): refusals, notifier/analyst wiring, SIM and MT5 runs on fakes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters import history_store as hs
from aifund.adapters.clock import FakeClock
from aifund.adapters.notify.telegram import FanOut, LogNotifier
from aifund.agents.analyst import Analyst
from aifund.config.loader import ConfigError, load_trading_config
from aifund.config.settings import PROJECT_ROOT, BrokerKind
from aifund.domain.enums import Timeframe
from aifund.engine import __main__ as main
from aifund.engine.detectors import detector_factory, load_hypothesis
from aifund.persistence.db import make_engine, unit_of_work
from aifund.persistence.migrations import pending_migration
from aifund.persistence.repositories.system import AuditLogRepository
from aifund.strategies.base import TfRoles
from aifund.strategies.dsl_detector import DslDetector
from tests.fakes.fake_mt5 import FakeMT5
from tests.unit.research.test_signals import flat
from tests.unit.risk.test_stops import XAU

EXAMPLE = PROJECT_ROOT / "config" / "trading.example.yaml"
ROLES = TfRoles(Timeframe.M15, Timeframe.H1, (Timeframe.H4,))


def settings(tmp_path: Path, **over: Any) -> Any:
    base = dict(
        CONFIG_PATH=EXAMPLE,
        DATABASE_URL=f"sqlite:///{tmp_path / 'x.db'}",
        BROKER=BrokerKind.SIM,
        DEEPSEEK_API_KEY=None,
        TELEGRAM_BOT_TOKEN=None,
        TELEGRAM_CHAT_ID=None,
        HEALTHCHECKS_URL=None,
        MT5_LOGIN=None,
        MT5_PASSWORD=None,
        MT5_SERVER=None,
        MT5_PATH=None,
        MT5_PORTABLE=False,
    )
    return SimpleNamespace(**{**base, **over})


def baseline_config(tmp_path: Path, **engine: Any) -> Path:
    data = yaml.safe_load(EXAMPLE.read_text())
    data["strategy"].update(analyst_enabled=False, baseline_enabled=True)
    data["engine"].update(engine)
    path = tmp_path / "trading.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


# ---------------------------------------------------------------- prepare


def test_prepare_refuses_a_missing_config_paper_and_an_old_schema(
    tmp_path: Path, db_url: str, engine: Any
) -> None:
    with pytest.raises(main.StartupError, match="trading config"):
        main.prepare(settings(tmp_path, CONFIG_PATH=tmp_path / "missing.yaml"), db_url)
    with pytest.raises(main.StartupError, match="PAPER is not available"):
        main.prepare(settings(tmp_path, CONFIG_PATH=baseline_config(tmp_path, mode="PAPER")), db_url)
    empty = f"sqlite:///{tmp_path / 'empty.db'}"
    with pytest.raises(main.StartupError, match="alembic upgrade head"):
        main.prepare(settings(tmp_path), empty)


def test_prepare_records_the_config_and_reads_the_live_confirmation(
    tmp_path: Path, db_url: str, factory: sessionmaker[Session], clock: FakeClock
) -> None:
    loaded, _factory, live = main.prepare(settings(tmp_path), db_url)
    assert (loaded.config.engine.magic, live) == (26092801, False)
    with unit_of_work(factory) as s:
        AuditLogRepository(s, clock).record(actor="op", action="CONFIRM_LIVE")
    assert main.prepare(settings(tmp_path), db_url)[2] is True


def test_the_migration_check(db_url: str, engine: Any, tmp_path: Path) -> None:
    assert pending_migration(engine, db_url) is None
    empty = f"sqlite:///{tmp_path / 'empty.db'}"
    assert "revision none" in (pending_migration(make_engine(empty), empty) or "")


# ---------------------------------------------------------------- notifier and analyst


async def test_notifier_and_analyst_wiring(
    tmp_path: Path, factory: sessionmaker[Session], clock: FakeClock
) -> None:
    cfg = load_trading_config(EXAMPLE).config
    async with httpx.AsyncClient() as client:
        assert isinstance(main.notifier_for(settings(tmp_path), cfg, client, clock), LogNotifier)
        on = cfg.model_copy(update={"alerts": cfg.alerts.model_copy(update={"telegram": True})})
        tg = settings(tmp_path, TELEGRAM_BOT_TOKEN=SecretStr("1:x"), TELEGRAM_CHAT_ID="42")
        assert isinstance(main.notifier_for(tg, on, client, clock), FanOut)
        with pytest.raises(main.StartupError, match="DEEPSEEK_API_KEY"):
            main.analyst_for(settings(tmp_path), cfg, factory, clock, LogNotifier(), client)
        keyed = settings(tmp_path, DEEPSEEK_API_KEY=SecretStr("k"))
        assert isinstance(main.analyst_for(keyed, cfg, factory, clock, LogNotifier(), client), Analyst)
        off = cfg.model_copy(
            update={
                "strategy": cfg.strategy.model_copy(
                    update={"analyst_enabled": False, "baseline_enabled": True}
                )
            }
        )
        assert main.analyst_for(keyed, off, factory, clock, LogNotifier(), client) is None


# ---------------------------------------------------------------- SIM and MT5 runs


@pytest.fixture
def export(tmp_path: Path) -> Path:
    root = tmp_path / "history"
    hs.write_specs(root, {"XAUUSD": XAU})
    for tf in (Timeframe.M1, Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1):
        hs.write_bars(root, "XAUUSD", tf, flat(tf, days=3))
    start = datetime(2026, 3, 2, tzinfo=UTC)
    hs.write_manifest(
        root,
        hs.Manifest(
            exported_at=start + timedelta(days=3),
            server="Test",
            account_trade_mode="DEMO",
            server_offset_minutes=0,
            start=start,
            end=start + timedelta(days=3),
            rows={"XAUUSD": {"M15": 288}},
        ),
    )
    return root


async def test_sim_runs_the_whole_engine_over_an_export(
    tmp_path: Path, export: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    s = settings(tmp_path, CONFIG_PATH=baseline_config(tmp_path))
    loaded = load_trading_config(s.CONFIG_PATH)
    args = SimpleNamespace(history=str(export), days=0.05)
    assert await main.run_sim(s, loaded, args) == 0
    out = capsys.readouterr().out
    assert "final state RUNNING" in out and "'heartbeat': (" in out  # noqa: PT018


async def test_mt5_runs_boots_and_stops_on_the_fake_terminal(
    tmp_path: Path, db_url: str, engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeMT5(tick_advance_ms=500)
    import aifund.adapters.mt5.gateway as gateway

    real = gateway.MT5Gateway
    monkeypatch.setattr(
        gateway, "MT5Gateway", lambda creds, clock: real(creds, clock=clock, module_loader=lambda: fake)
    )
    monkeypatch.setattr(main, "_on_signals", lambda stop: stop.set())  # stop at once
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    s = settings(
        tmp_path,
        BROKER=BrokerKind.MT5,
        CONFIG_PATH=baseline_config(tmp_path, mode="DEMO", account_label="acc"),
        MT5_LOGIN=12345678,
        MT5_PASSWORD=SecretStr("x"),
        MT5_SERVER="Broker-Demo",
        HEALTHCHECKS_URL=SecretStr("https://hc-ping.com/u"),
    )
    loaded, factory, live = main.prepare(s, db_url)
    assert await main.run_mt5(s, loaded, factory, live) == 0  # boots (refused: no E1 evidence on DEMO), stops
    with pytest.raises(main.StartupError, match="MT5_LOGIN"):
        await main.run_mt5(settings(tmp_path, BROKER=BrokerKind.MT5), loaded, factory, live)


async def test_amain_reports_why_it_did_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main, "configure_logging", lambda *a, **kw: None)
    monkeypatch.setattr(main, "register_settings_secrets", lambda _s: None)
    for broker in (BrokerKind.SIM, BrokerKind.MT5):
        monkeypatch.setattr(
            main,
            "Settings",
            lambda b=broker: settings(tmp_path, BROKER=b, CONFIG_PATH=tmp_path / "none.yaml"),
        )
        assert await main.amain([]) == 2
    assert capsys.readouterr().err.count("engine not started: trading config") == 2


def test_main_exits_with_the_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "amain", lambda argv: asyncio.sleep(0, result=2))
    with pytest.raises(SystemExit) as done:
        main.main()
    assert done.value.code == 2


# ---------------------------------------------------------------- detectors


def card(tmp_path: Path, **over: Any) -> Path:
    hypothesis = {
        "id": "H-1",
        "version": 1,
        "setup_tag": "rsi_dip",
        "long": {"all": [{"feature": "m15.rsi14", "op": "<", "value": 30}]},
        "invalidation": {"type": "atr", "k": 1.0},
        "mechanism": "Oversold dips mean-revert.",
    }
    data = {"id": "H-1", "status": "APPROVED", "hypothesis": hypothesis, **over}
    (tmp_path / "H-1.yaml").write_text(yaml.safe_dump(data))
    return tmp_path


@pytest.mark.parametrize(
    ("over", "error"),
    [({"status": "DRAFT"}, "set status: APPROVED"), ({"hypothesis": None}, "invalid hypothesis")],
)
def test_approved_dsl_playbooks_only(tmp_path: Path, over: dict[str, Any], error: str) -> None:
    with pytest.raises(ConfigError, match=error):
        load_hypothesis(card(tmp_path, **over), "H-1")


def test_the_detector_factory(tmp_path: Path) -> None:
    cfg = load_trading_config(EXAMPLE).config
    both = cfg.model_copy(
        update={"strategy": cfg.strategy.model_copy(update={"detectors": ["mtf_trend_pullback", "H-1"]})}
    )
    built = detector_factory(both, card(tmp_path))(cfg.symbols[0], ROLES)
    assert [d.playbook_id for d in built] == ["mtf_trend_pullback", "H-1"]
    assert isinstance(built[1], DslDetector)
    (tmp_path / "H-2.yaml").write_text(yaml.safe_dump({"id": "H-2"}))
    for missing, error in (("nope", "neither built in"), ("H-2", "no DSL hypothesis")):
        bad = cfg.model_copy(update={"strategy": cfg.strategy.model_copy(update={"detectors": [missing]})})
        with pytest.raises(ConfigError, match=error):
            detector_factory(bad, tmp_path)
