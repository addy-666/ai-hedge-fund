"""scripts/engine_command.py queues an operator command like the API would and reports the answer."""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.domain.enums import CommandStatus
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.system import CommandRepository
from aifund.persistence.tables import AuditLogRow, CommandRow

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "engine_command.py"


def load() -> Any:
    spec = importlib.util.spec_from_file_location("engine_command_script", SCRIPT)
    assert spec is not None and spec.loader is not None  # noqa: PT018
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_command_is_queued_audited_and_answered(
    factory: sessionmaker[Session], clock: FakeClock, db_url: str, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    script = load()
    monkeypatch.setattr(script, "Settings", lambda: SimpleNamespace(DATABASE_URL=db_url))
    assert script.main(["PAUSE", "--wait", "0.2"]) == 1  # no engine running
    assert "no answer" in capsys.readouterr().out
    with factory() as s:
        (queued,) = s.scalars(select(CommandRow)).all()
        (audit,) = s.scalars(select(AuditLogRow)).all()
    assert (queued.type, queued.status, audit.action) == ("PAUSE", CommandStatus.PENDING, "COMMAND_PAUSE")

    def engine() -> None:  # answers the next command, as the poller would
        time.sleep(0.3)
        with unit_of_work(factory) as s:
            repo = CommandRepository(s, clock)
            while (row := repo.claim_next()) is not None:
                repo.finish(row.id, ok=row.type == "PAUSE", result={"state": "PAUSED"})

    worker = threading.Thread(target=engine)
    worker.start()
    assert script.main(["CLOSE_POSITION", "--payload", '{"position_id": 7}', "--wait", "5"]) == 1
    worker.join()
    assert 'FAILED: {"state": "PAUSED"}' in capsys.readouterr().out
