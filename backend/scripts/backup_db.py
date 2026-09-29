"""Nightly database backup (roadmap 5.7, docs/06 §8): online SQLite backup, gzip, retention, optional upload.

    cd backend
    uv run python scripts/backup_db.py                         # -> <repo>/backups/aifund-YYYYMMDD-HHMM.db.gz
    uv run python scripts/backup_db.py --remote gdrive:aifund  # ...and `rclone copy` it to the remote

Uses sqlite3's online backup API, so it is safe while the engine writes (WAL). Keeps the newest backup of each
of the last 30 days and of each of the last 12 months; older files are deleted. Exit 0 = backed up, 1 = failed
(the Windows task alerts on a non-zero exit through the engine's log).
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from aifund.config.settings import PROJECT_ROOT, Settings

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


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default=str(PROJECT_ROOT / "backups"))
    p.add_argument("--remote", default=None, help="rclone destination, e.g. gdrive:aifund")
    args = p.parse_args(argv)
    try:
        db = sqlite_path(Settings().DATABASE_URL)
        path = backup(db, Path(args.out), datetime.now(UTC))
        removed = prune(Path(args.out))
        print(f"backup: {path} ({path.stat().st_size / 1e6:.1f} MB); pruned {len(removed)}")
        if args.remote:
            subprocess.run(["rclone", "copy", str(path), args.remote], check=True, timeout=600)  # noqa: S603, S607
            print(f"uploaded to {args.remote}")
    except Exception as exc:
        print(f"BACKUP FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
