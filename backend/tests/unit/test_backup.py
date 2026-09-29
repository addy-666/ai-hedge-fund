"""Nightly backup (roadmap 5.7): a consistent copy of a live database, gzip, 30-day + 12-month retention."""

from __future__ import annotations

import gzip
import importlib.util
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backup_db.py"


def load() -> Any:
    spec = importlib.util.spec_from_file_location("backup_db_script", SCRIPT)
    assert spec is not None and spec.loader is not None  # noqa: PT018
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_backup_copies_a_live_database_and_compresses_it(tmp_path: Path) -> None:
    db = tmp_path / "aifund.db"
    live = sqlite3.connect(db)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE t (x INTEGER)")
    live.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    live.commit()  # the engine keeps its connection open
    script = load()
    out = script.backup(db, tmp_path / "backups", datetime(2026, 9, 29, 21, 30, tzinfo=UTC))
    assert out.name == "aifund-20260929-2130.db.gz"
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(out.read_bytes()))
    assert sqlite3.connect(restored).execute("SELECT count(*) FROM t").fetchone() == (100,)
    live.close()


def test_retention_keeps_30_days_and_12_months(tmp_path: Path) -> None:
    script = load()
    start = datetime(2025, 1, 1, 21, 30, tzinfo=UTC)
    for d in range(400):
        for minute in (30, 45):  # two backups a day: only the newest of each day may survive
            (tmp_path / f"aifund-{start + timedelta(days=d):%Y%m%d}-21{minute}.db.gz").write_bytes(b"x")
    removed = script.prune(tmp_path)
    kept = sorted(tmp_path.glob("aifund-*.db.gz"))
    days = {p.name[7:15] for p in kept}
    months = {p.name[7:13] for p in kept}
    assert len(removed) == 800 - len(kept)
    assert all(p.name.endswith("-2145.db.gz") for p in kept)  # the newest of its day
    assert len(months) == 12
    # the last 30 days (5 Jan - 4 Feb 2026) already hold the newest backup of January and February, so the
    # monthly rule adds the last backup of the 10 months before them
    assert len(days) == 30 + 10


def test_main_reports_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = load()
    monkeypatch.setattr(script, "Settings", lambda: SimpleNamespace(DATABASE_URL="postgresql://x/y"))
    assert script.main(["--out", str(tmp_path)]) == 1
    assert "BACKUP FAILED: only SQLite" in capsys.readouterr().err
    db = tmp_path / "a.db"
    sqlite3.connect(db).execute("CREATE TABLE t (x)").connection.commit()
    monkeypatch.setattr(script, "Settings", lambda: SimpleNamespace(DATABASE_URL=f"sqlite:///{db}"))
    assert script.main(["--out", str(tmp_path / "b")]) == 0
    assert "backup:" in capsys.readouterr().out
