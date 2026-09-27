"""Migration, pragmas and column-type round-trips against a real SQLite file."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session, sessionmaker

from aifund.domain.enums import EngineState, Mode
from aifund.persistence.db import json_dumps, make_engine
from aifund.persistence.tables import Base, EngineStateRow, EventRow, HeartbeatRow

from .conftest import T0, alembic_config

EXPECTED_TABLES = {
    "audit_log", "audit_runs", "calibration_models", "commands", "config_versions", "deals", "decisions",
    "engine_state", "equity_snapshots", "events", "feature_snapshots", "heartbeats", "llm_calls",
    "order_intents", "rule_evaluations", "rulebook_versions", "rules", "symbols", "trade_reviews", "trades",
    "virtual_trades",
}  # fmt: skip


def test_migration_creates_every_table_and_matches_the_models(engine: Engine) -> None:
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == EXPECTED_TABLES
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models and migrations drifted: {diff}"


def test_migration_downgrades_to_empty(db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    eng = make_engine(db_url)
    assert set(inspect(eng).get_table_names()) <= {"alembic_version"}
    eng.dispose()


def test_sqlite_pragmas(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000


@pytest.mark.parametrize(
    "value",
    ["0.1", "2345.67", "12345678901234.123456789", "-8.2", "0.00000001", "1E-10", "0"],
)
def test_decimal_round_trip_is_exact(factory: sessionmaker[Session], value: str) -> None:
    with factory.begin() as s:
        s.add(
            EngineStateRow(
                account_id="a",
                state=EngineState.STOPPED,
                mode=Mode.SIM,
                peak_equity=Decimal(value),
                updated_at=T0,
            )
        )
    with factory() as s:
        row = s.get(EngineStateRow, "a")
        assert row is not None
        assert row.peak_equity == Decimal(value)
        assert isinstance(row.peak_equity, Decimal)


def test_decimal_columns_reject_floats_and_non_finite(factory: sessionmaker[Session]) -> None:
    for bad in (0.1, Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(StatementError), factory.begin() as s:
            s.add(
                EngineStateRow(
                    account_id="a", state=EngineState.STOPPED, mode=Mode.SIM, peak_equity=bad, updated_at=T0
                )
            )


def test_utc_round_trip_and_naive_rejected(factory: sessionmaker[Session]) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    moment = datetime(2026, 9, 28, 14, 30, 15, 123456, tzinfo=ist)
    with factory.begin() as s:
        s.add(HeartbeatRow(component="engine", last_beat_at=moment, status="ok"))
    with factory() as s:
        row = s.get(HeartbeatRow, "engine")
        assert row is not None
        assert row.last_beat_at == moment
        assert row.last_beat_at.tzinfo is UTC
    with pytest.raises(StatementError, match="naive datetime"), factory.begin() as s:
        s.add(HeartbeatRow(component="x", last_beat_at=datetime(2026, 1, 1), status="ok"))


def test_utc_text_sorts_chronologically(factory: sessionmaker[Session]) -> None:
    times = [T0 + timedelta(microseconds=n) for n in (999_999, 1, 10, 100_000)]
    with factory.begin() as s:
        for t in times:
            s.add(EventRow(ts=t, type="x", severity="info"))
    with factory() as s:
        rows = s.execute(text("SELECT ts FROM events ORDER BY ts")).scalars().all()
    assert rows == sorted(rows)
    assert len(rows) == 4


def test_json_payloads_keep_decimals_exact(factory: sessionmaker[Session]) -> None:
    with factory.begin() as s:
        s.add(EventRow(ts=T0, type="x", severity="info", payload={"pnl": Decimal("-8.20"), "at": T0}))
    with factory() as s:
        payload = s.execute(text("SELECT payload FROM events")).scalar_one()
    assert payload == '{"pnl":"-8.20","at":"2026-09-28T09:00:00+00:00"}'
    with pytest.raises(ValueError, match="Out of range float values"):
        json_dumps({"x": float("nan")})


def test_enum_check_constraints_are_enforced(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="CHECK constraint failed"), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO engine_state (account_id, state, mode, rulebook_version, updated_at) "
                "VALUES ('a', 'YOLO', 'SIM', 0, '2026-09-28T09:00:00.000000Z')"
            )
        )


def test_foreign_keys_are_enforced(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO engine_state "
                "(account_id, state, mode, rulebook_version, updated_at, config_version_id) "
                "VALUES ('a', 'STOPPED', 'SIM', 0, '2026-09-28T09:00:00.000000Z', 'NOPE')"
            )
        )
