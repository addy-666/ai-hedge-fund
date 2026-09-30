"""Nightly database backup (roadmap 5.7, docs/06 §8): online SQLite backup, gzip, retention, optional upload.

    cd backend
    uv run python scripts/backup_db.py                         # -> <repo>/backups/aifund-YYYYMMDD-HHMM.db.gz
    uv run python scripts/backup_db.py --remote secret:aifund  # ...and `rclone copy` it to an ENCRYPTED remote

Uses sqlite3's online backup API, so it is safe while the engine writes (WAL). Keeps the newest backup of each
of the last 30 days and of each of the last 12 months; older files are deleted. After a verified backup, the
data retention of docs/06 §8 is applied to the live database (``persistence/repositories/retention.py``:
events > 30 days deleted, LLM call text > 180 days truncated); ``--no-retention`` skips it. With ``--alert``
(the nightly task) the result is the ``job.backup`` heartbeat and a failure is a CRITICAL alert.
Exit 0 = backed up, 1 = failed.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from aifund.adapters.clock import SystemClock
from aifund.config.loader import load_trading_config
from aifund.config.settings import PROJECT_ROOT, Settings
from aifund.engine.__main__ import notifier_for
from aifund.persistence.db import make_engine, make_session_factory, unit_of_work
from aifund.persistence.repositories.retention import RetentionRepository, RetentionResult
from aifund.persistence.repositories.system import HeartbeatRepository
from aifund.ports.system import Severity

PATTERN = "aifund-*.db.gz"


def sqlite_path(url: str) -> Path:
    if not url.startswith("sqlite:///"):
        raise ValueError(f"only SQLite databases are backed up by this script, not {url.split(':')[0]}")
    return Path(url.removeprefix("sqlite:///"))


def backup(db: Path, out_dir: Path, now: datetime) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / f"aifund-{now:%Y%m%d-%H%M}.db"
    source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(raw)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    check = sqlite3.connect(raw)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"{raw.name} failed its integrity check")
    finally:
        check.close()
    packed = raw.with_suffix(".db.gz")
    with raw.open("rb") as src, gzip.open(packed, "wb") as dst:
        shutil.copyfileobj(src, dst)
    raw.unlink()
    return packed


def stamp(path: Path) -> datetime:
    return datetime.strptime(path.name.removeprefix("aifund-").removesuffix(".db.gz"), "%Y%m%d-%H%M").replace(
        tzinfo=UTC
    )


def prune(out_dir: Path, *, days: int = 30, months: int = 12) -> list[Path]:
    """Keep the newest backup of each of the last ``days`` days and ``months`` months; delete the rest."""
    files = sorted(out_dir.glob(PATTERN), key=stamp, reverse=True)
    keep: set[Path] = set()
    seen_days: list[str] = []
    seen_months: list[str] = []
    for f in files:
        day, month = f"{stamp(f):%Y%m%d}", f"{stamp(f):%Y%m}"
        if day not in seen_days and len(seen_days) < days:
            seen_days.append(day)
            keep.add(f)
        if month not in seen_months and len(seen_months) < months:
            seen_months.append(month)
            keep.add(f)
    removed = [f for f in files if f not in keep]
    for f in removed:
        f.unlink()
    return removed


def remote_type(remote: str) -> str | None:
    """The rclone backend type of ``remote`` ("name:path"), from ``rclone listremotes --long``."""
    name = remote.split(":", 1)[0] + ":"
    command = ["rclone", "listremotes", "--long"]
    out = subprocess.run(command, check=True, capture_output=True, text=True, timeout=60).stdout  # noqa: S603
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == name:
            return parts[1]
    return None


def check_encrypted(remote: str) -> None:
    """Backups leave the machine only encrypted (docs/06 §6): the remote must be an rclone ``crypt`` remote
    (e.g. ``secret:`` wrapping ``gdrive:aifund``; set it up with ``rclone config``)."""
    kind = remote_type(remote)
    if kind != "crypt":
        raise RuntimeError(
            f"rclone remote {remote.split(':', 1)[0]!r} is {kind or 'not configured'}, not an encrypted "
            "'crypt' remote: backups are only uploaded encrypted (docs/runbooks/install.md)"
        )


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default=str(PROJECT_ROOT / "backups"))
    p.add_argument("--remote", default=None, help="rclone crypt destination, e.g. secret:aifund")
    p.add_argument("--no-retention", action="store_true", help="do not thin old events / LLM call text")
    p.add_argument("--alert", action="store_true", help="record the result and alert on failure")
    args = p.parse_args(argv)
    try:
        if args.remote:
            check_encrypted(args.remote)  # before anything is written: a misconfigured upload fails loudly
        db = sqlite_path(Settings().DATABASE_URL)
        path = backup(db, Path(args.out), datetime.now(UTC))
        removed = prune(Path(args.out))
        print(f"backup: {path} ({path.stat().st_size / 1e6:.1f} MB); pruned {len(removed)}")
        if args.remote:
            subprocess.run(["rclone", "copy", str(path), args.remote], check=True, timeout=600)  # noqa: S603, S607
            print(f"uploaded to {args.remote}")
        detail: dict[str, object] = {"file": path.name, "mb": round(path.stat().st_size / 1e6, 2)}
        if not args.no_retention:
            thinned = retention(Settings().DATABASE_URL)
            print(
                f"retention: {thinned.events_deleted} events deleted, {thinned.llm_calls_truncated} LLM calls truncated"
            )
            detail.update(
                events_deleted=thinned.events_deleted, llm_calls_truncated=thinned.llm_calls_truncated
            )
    except Exception as exc:
        print(f"BACKUP FAILED: {exc}", file=sys.stderr)
        if args.alert:
            record(Settings(), "failed", {"error": str(exc)[:500]}, alert=f"Backup FAILED: {exc}"[:300])
        return 1
    if args.alert:
        record(Settings(), "ok", detail)
    return 0


def retention(url: str) -> RetentionResult:
    factory = make_session_factory(make_engine(url))
    with unit_of_work(factory) as s:
        return RetentionRepository(s).apply(datetime.now(UTC))


def record(settings: Settings, status: str, detail: dict[str, object], alert: str | None = None) -> None:
    """The ``job.backup`` heartbeat (System page) and, for a failure, a CRITICAL alert. Best effort: the
    database may be the very thing that failed."""
    try:
        factory = make_session_factory(make_engine(settings.DATABASE_URL))
        with unit_of_work(factory) as s:
            HeartbeatRepository(s, SystemClock()).beat("job.backup", status, detail)
    except Exception as exc:
        print(f"could not record the backup result: {exc}", file=sys.stderr)
    if alert is None:
        return
    try:
        cfg = load_trading_config(settings.CONFIG_PATH).config
    except Exception:
        cfg = load_trading_config(PROJECT_ROOT / "config" / "trading.example.yaml").config

    async def send() -> None:
        async with httpx.AsyncClient() as client:
            await notifier_for(settings, cfg, client, SystemClock()).notify(Severity.CRITICAL, alert, "")

    asyncio.run(send())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
