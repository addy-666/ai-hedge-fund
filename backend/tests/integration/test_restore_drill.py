"""Backups, retention and the restore drill (roadmap 9.5, docs/06 §8).

A database written by the real stack (a replayed day on the SimBroker) is backed up, verified from the backup
(integrity, foreign keys, schema at head, ledger consistency) and found restorable; a damaged ledger, an old
schema and a truncated file are each caught. Retention thins only what docs/06 §8 allows, only after a backup.
"""

from __future__ import annotations

import gzip
import importlib.util
import sys
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.config.trading_config import StrategyConfig
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.consistency import ConsistencyRepository
from aifund.persistence.repositories.llm import LLMCallRepository
from aifund.persistence.repositories.retention import RetentionRepository
from aifund.persistence.repositories.system import EventRepository
from aifund.persistence.tables import EventRow, HeartbeatRow, LLMCallRow, TradeRow
from aifund.ports.system import Severity
from tests.integration.conftest import T0, alembic_config
from tests.integration.test_baseline_replay import market, replay  # noqa: F401

pytestmark = pytest.mark.scenario
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"{name}_drill", SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def test_a_backup_of_a_trading_database_restores_and_verifies(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
    db_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline = StrategyConfig(analyst_enabled=False, baseline_enabled=True)
    report = await replay(market, factory, strategy=baseline,
                          days=1)  # fmt: skip
    assert report.fills >= 1
    backup, verify = load("backup_db"), load("verify_db")
    packed = backup.backup(Path(db_url.removeprefix("sqlite:///")), tmp_path / "backups", T0)
    code, lines = verify.verify(packed)
    assert code == 0, lines
    assert lines[0] == f"{packed.name}: OK"
    assert any(line.startswith("rows: ") and "trades" in line for line in lines)
    assert verify.main([str(packed)]) == 0
    assert verify.newest(tmp_path / "backups") == packed
    capsys.readouterr()

    # a damaged ledger: a closed trade that lost its decision
    with unit_of_work(factory) as s:
        closed = s.scalars(select(TradeRow).where(TradeRow.close_time.is_not(None))).first()
        assert closed is not None
        s.execute(update(TradeRow).where(TradeRow.id == closed.id).values(decision_id=None))
    damaged = backup.backup(Path(db_url.removeprefix("sqlite:///")), tmp_path / "damaged", T0)
    code, lines = verify.verify(damaged)
    assert code == 1
    assert any(line.startswith("ERROR closed_trade_without_context") for line in lines)

    # a truncated file and an old schema
    cut = tmp_path / "cut.db.gz"
    cut.write_bytes(packed.read_bytes()[: len(packed.read_bytes()) // 3])
    assert verify.verify(cut)[0] == 2
    old = tmp_path / "old.db"
    old.write_bytes(gzip.decompress(packed.read_bytes()))
    command.downgrade(alembic_config(f"sqlite:///{old}"), "-1")
    code, lines = verify.verify(old)
    assert code == 1
    assert any(line.startswith("ERROR schema:") for line in lines)
    assert verify.main([str(tmp_path / "nothing.db")]) == 2


def test_retention_keeps_the_learning_dataset(factory: sessionmaker[Session], clock: FakeClock) -> None:
    now = T0 + timedelta(days=400)
    with unit_of_work(factory) as s:  # oldest first: the fake clock only moves forward
        for days_old, kind in ((200, "llm"), (60, "evidence"), (40, "event"), (30, "llm"), (10, "event")):
            clock.set(now - timedelta(days=days_old))
            if kind == "evidence":  # a restart: the rollout gates need it beyond 30 days
                EventRepository(s, clock).append("engine.state", Severity.INFO, {"age": days_old})
                continue
            if kind == "event":
                EventRepository(s, clock).append("alert", Severity.INFO, {"age": days_old})
                continue
            LLMCallRepository(s, clock).add(
                agent="analyst", model="m", prompt_template="analyst", prompt_version="1",
                prompt_sha256="h" * 64, messages=[{"role": "user", "content": "x"}], response_text="{}",
                parsed={"ok": True}, valid=True,
                prompt_tokens=10, completion_tokens=5, cost_usd=D("0.001"),
            )  # fmt: skip
    with unit_of_work(factory) as s:
        result = RetentionRepository(s).apply(now)
    assert (result.events_deleted, result.llm_calls_truncated) == (1, 1)
    with factory() as s:
        assert [e.payload["age"] for e in s.scalars(select(EventRow)).all()] == [60, 10]
        calls = sorted(s.scalars(select(LLMCallRow)).all(), key=lambda c: c.created_at)
        assert (calls[0].messages, calls[0].response_text, calls[0].parsed) == (None, None, {"ok": True})
        assert (calls[0].prompt_sha256, calls[0].cost_usd) == ("h" * 64, D("0.001"))  # the metadata stays
        assert calls[1].messages is not None
    with unit_of_work(factory) as s:
        again = RetentionRepository(s).apply(now)
    assert (again.events_deleted, again.llm_calls_truncated) == (0, 0)  # nothing twice


def test_the_nightly_backup_records_thins_and_alerts(
    factory: sessionmaker[Session], db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    script = load("backup_db")
    settings = SimpleNamespace(DATABASE_URL=db_url, CONFIG_PATH=tmp_path / "none.yaml")
    monkeypatch.setattr(script, "Settings", lambda: settings)
    sent: list[tuple[Severity, str]] = []

    class Recorder:
        async def notify(self, severity: Severity, title: str, body: str = "") -> None:
            sent.append((severity, title))

    monkeypatch.setattr(script, "notifier_for", lambda *_a: Recorder())
    assert script.main(["--out", str(tmp_path / "b"), "--alert"]) == 0
    assert "retention: 0 events deleted" in capsys.readouterr().out
    with factory() as s:
        beat = s.scalars(select(HeartbeatRow).where(HeartbeatRow.component == "job.backup")).one()
        assert (beat.status, beat.detail["events_deleted"]) == ("ok", 0)
    assert sent == []
    monkeypatch.setattr(script, "backup", lambda *_a: (_ for _ in ()).throw(OSError("disk full")))
    assert script.main(["--out", str(tmp_path / "b"), "--alert"]) == 1
    assert sent == [(Severity.CRITICAL, "Backup FAILED: disk full")]
    with factory() as s:
        beat = s.scalars(select(HeartbeatRow).where(HeartbeatRow.component == "job.backup")).one()
        assert (beat.status, beat.detail["error"]) == ("failed", "disk full")
    settings.DATABASE_URL = "sqlite:////nonexistent/dir/x.db"  # the database itself is gone: still alerts
    assert script.main(["--out", str(tmp_path / "b"), "--alert"]) == 1
    assert len(sent) == 2
    assert "could not record the backup result" in capsys.readouterr().err
    assert ConsistencyRepository  # (imported for the drill above)
