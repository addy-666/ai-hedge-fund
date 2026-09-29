"""Is the database at the latest Alembic revision? The engine will not start on an old schema (5.6)."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

BACKEND = Path(__file__).resolve().parents[3]


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.attributes["url"] = url
    return cfg


def pending_migration(engine: Engine, url: str) -> str | None:
    """None when the schema is at head; otherwise what to do."""
    head = ScriptDirectory.from_config(alembic_config(url)).get_current_head()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    if current == head:
        return None
    return (
        f"database at revision {current or 'none'}, code expects {head}: "
        "run `uv run alembic upgrade head` (after a backup)"
    )
