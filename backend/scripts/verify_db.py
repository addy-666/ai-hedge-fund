"""Verify a database or a backup (roadmap 9.5, docs/06 §8 restore drill). READ-ONLY on the file given.

    cd backend
    uv run python scripts/verify_db.py                                   # the newest <repo>/backups/*.db.gz
    uv run python scripts/verify_db.py ../backups/aifund-20261005-2130.db.gz
    uv run python scripts/verify_db.py ../data/aifund.db

The file is copied (a ``.gz`` decompressed) into a temporary directory and checked there: SQLite
``integrity_check`` and ``foreign_key_check``, the schema revision against the code's Alembic head, the ledger's
own consistency (``persistence/repositories/consistency.py``) and the row count of every table.
Exit 0 = restorable and consistent (warnings allowed), 1 = errors, 2 = unreadable.

Restore (docs/runbooks/install.md): stop the engine and the API, copy the verified file (decompressed) to the
``DATABASE_URL`` path, start them again; the engine reconciles against the broker at startup.
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from aifund.config.settings import PROJECT_ROOT
from aifund.persistence.db import make_engine, make_session_factory
from aifund.persistence.migrations import pending_migration
from aifund.persistence.repositories.consistency import ConsistencyRepository

BACKUPS = PROJECT_ROOT / "backups"


def newest(directory: Path) -> Path | None:
    files = sorted(directory.glob("aifund-*.db.gz"))
    return files[-1] if files else None


def working_copy(source: Path, into: Path) -> Path:
    target = into / "verify.db"
    if source.suffix == ".gz":
        with gzip.open(source, "rb") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    else:
        shutil.copyfile(source, target)
    return target


def verify(path: Path) -> tuple[int, list[str]]:
    """(exit code, report lines)."""
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        try:
            db = working_copy(path, Path(tmp))
            con = sqlite3.connect(db)
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            fk = con.execute("PRAGMA foreign_key_check").fetchall()
            con.close()
        except (OSError, sqlite3.DatabaseError, EOFError) as exc:
            return 2, [f"UNREADABLE {path.name}: {exc}"]
        errors = 0
        if integrity != "ok":
            errors += 1
            lines.append(f"ERROR integrity_check: {integrity}")
        for row in fk[:20]:
            errors += 1
            lines.append(f"ERROR foreign key: {row}")
        url = f"sqlite:///{db}"
        engine = make_engine(url)
        try:
            problem = pending_migration(engine, url)
            if problem is not None:
                errors += 1
                lines.append(f"ERROR schema: {problem}")
            else:
                with make_session_factory(engine)() as s:
                    repo = ConsistencyRepository(s)
                    for f in repo.findings():
                        errors += f.level == "error"
                        lines.append(f"{f.level.upper()} {f.check}: {f.detail}")
                    counts = repo.counts()
                lines.append("rows: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v))
        finally:
            engine.dispose()
    lines.insert(0, f"{path.name}: {'OK' if not errors else f'{errors} error(s)'}")
    return (1 if errors else 0), lines


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("path", nargs="?", type=Path, help=f"a .db or .db.gz (default: the newest in {BACKUPS})")
    args = p.parse_args(argv)
    path = args.path or newest(BACKUPS)
    if path is None or not path.is_file():
        print(f"nothing to verify: {path or BACKUPS}", file=sys.stderr)
        return 2
    code, lines = verify(path)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
