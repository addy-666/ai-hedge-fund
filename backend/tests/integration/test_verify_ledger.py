"""Ledger verification (roadmap 5.7c): database trades vs the broker's deals, incl. engine-down orphans."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.domain.enums import DealEntry, DealReason, Side, TradeStatus
from aifund.domain.market import Deal
from aifund.persistence.db import unit_of_work
from aifund.persistence.tables import EventRow, HeartbeatRow, TradeRow
from aifund.ports.system import Severity
from aifund.reconcile.ledger_check import DiffKind, TradeFacts, diff_ledger

MAGIC = 26092801
T0 = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
SINCE = T0 - timedelta(days=1)
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_ledger.py"


def deal(
    ticket: int,
    pid: int,
    entry: DealEntry,
    minutes: int,
    *,
    volume: str = "0.10",
    profit: str = "0",
    commission: str = "0",
    magic: int = MAGIC,
    reason: DealReason = DealReason.EXPERT,
) -> Deal:
    return Deal(
        ticket=ticket,
        order=ticket,
        position_id=pid,
        time=T0 + timedelta(minutes=minutes),
        time_server_epoch=0,
        symbol="XAUUSD",
        side=Side.BUY if entry is DealEntry.IN else Side.SELL,
        entry=entry,
        reason=reason,
        magic=magic,
        volume=D(volume),
        price=D("4150"),
        profit=D(profit),
        commission=D(commission),
    )


def facts(
    pid: int,
    status: TradeStatus = TradeStatus.CLOSED,
    *,
    net: str | None = "19.30",
    volume: str = "0.10",
    minutes: int = 0,
) -> TradeFacts:
    return TradeFacts(
        pid, "XAUUSD", status, T0 + timedelta(minutes=minutes), D(volume), D(net) if net else None
    )


# position 1: recorded correctly; 2: opened and closed while the engine was down; 3: DB says open, broker
# closed; 4: net differs; 5: in the DB only; 6: another magic; 7: entered before the window; 8: an orphan
# the database knows
DEALS = [
    deal(1, 1, DealEntry.IN, 0, commission="-0.35"),
    deal(2, 1, DealEntry.OUT, 60, profit="20.00", commission="-0.35"),
    deal(3, 2, DealEntry.IN, 120),
    deal(4, 2, DealEntry.OUT, 180, profit="-12.00", reason=DealReason.SL),
    deal(5, 3, DealEntry.IN, 200),
    deal(6, 3, DealEntry.OUT, 260, profit="5.00"),
    deal(7, 4, DealEntry.IN, 300),
    deal(8, 4, DealEntry.OUT, 320, profit="7.00"),
    deal(9, 6, DealEntry.IN, 330, magic=1234),
    deal(10, 6, DealEntry.OUT, 340, profit="3.00", magic=1234),
    deal(11, 7, DealEntry.IN, -3000),
    deal(12, 7, DealEntry.OUT, 10, profit="1.00"),
]
TRADES = [
    facts(1),
    facts(3, TradeStatus.OPEN, net=None, minutes=200),
    facts(4, net="9.00", minutes=300),
    facts(5, TradeStatus.OPEN, net=None, minutes=400),
    facts(8, TradeStatus.ORPHAN_CLOSED, minutes=410),
]


def test_every_kind_of_difference_is_found_and_nothing_else() -> None:
    diffs = diff_ledger(TRADES, DEALS, magic=MAGIC, since=SINCE)
    assert [(d.kind, d.position_id) for d in diffs] == [
        (DiffKind.UNRECORDED, 2),
        (DiffKind.STATUS, 3),
        (DiffKind.NET, 4),
        (DiffKind.NOT_AT_BROKER, 5),
    ]
    assert diffs[0].render() == (
        "UNRECORDED     #2 XAUUSD: opened 2026-09-21 10:00 UTC, closed 2026-09-21 11:00 UTC, net -12.00"
    )
    assert diffs[1].detail == "broker closed, database OPEN"
    assert diffs[2].detail == "broker 7.00, database 9.00"


def test_a_clean_ledger_and_open_positions() -> None:
    assert diff_ledger([facts(1)], DEALS[:2], magic=MAGIC, since=SINCE) == []
    still_open = [deal(20, 20, DealEntry.IN, 0)]
    (d,) = diff_ledger([], still_open, magic=MAGIC, since=SINCE)
    assert (d.kind, d.detail) == (DiffKind.UNRECORDED, "opened 2026-09-21 08:00 UTC, still open")
    (v,) = diff_ledger(
        [facts(20, TradeStatus.OPEN, net=None, volume="0.20")], still_open, magic=MAGIC, since=SINCE
    )
    assert (v.kind, v.detail) == (DiffKind.VOLUME, "broker 0.10, database 0.20")


def load() -> Any:
    spec = importlib.util.spec_from_file_location("verify_ledger_script", SCRIPT)
    assert spec is not None and spec.loader is not None  # noqa: PT018
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def raw(d: Deal) -> dict[str, Any]:
    return {
        "ticket": d.ticket,
        "order": d.order,
        "time": int(d.time.timestamp()),
        "time_msc": int(d.time.timestamp()) * 1000,
        "type": 0 if d.side is Side.BUY else 1,
        "entry": {"IN": 0, "OUT": 1}[d.entry.value],
        "magic": d.magic,
        "position_id": d.position_id,
        "reason": 3,
        "volume": float(d.volume),
        "price": float(d.price),
        "commission": float(d.commission),
        "swap": 0.0,
        "profit": float(d.profit),
        "fee": 0.0,
        "symbol": d.symbol,
        "comment": "",
    }


def test_the_script_runs_on_a_fixture(
    factory: sessionmaker[Session],
    db_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = tmp_path / "deals.json"
    fixture.write_text(
        json.dumps(
            {
                "format_version": 1,
                "server": "Example-Demo",
                "currency": "USD",
                "trade_mode": "DEMO",
                "server_offset_minutes": 0,
                "balance": "0",
                "deals": [raw(d) for d in DEALS[:4]],
            }
        )
    )
    with unit_of_work(factory) as s:
        s.add(
            TradeRow(
                id="T1",
                account_id="acc",
                position_id=1,
                symbol="XAUUSD",
                side=Side.BUY,
                status=TradeStatus.CLOSED,
                open_time=T0,
                open_price=D("4150"),
                volume_opened=D("0.10"),
                volume_open_now=D(0),
                net_pnl=D("19.30"),
                created_at=T0,
                updated_at=T0,
            )
        )
    script = load()
    monkeypatch.setattr(
        script, "Settings", lambda: SimpleNamespace(DATABASE_URL=db_url, CONFIG_PATH=tmp_path / "none.yaml")
    )
    assert script.main(["--fixture", str(fixture), "--account", "acc"]) == 1
    out = capsys.readouterr().out
    assert "2 engine positions at the broker" in out
    assert "UNRECORDED     #2" in out
    assert "1 difference(s)" in out
    assert script.main(["--fixture", str(tmp_path / "missing.json")]) == 2


def test_the_nightly_job_records_and_alerts(
    factory: sessionmaker[Session],
    db_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Roadmap 9.3: a difference (or a failure to read the broker) is a CRITICAL alert and a heartbeat."""
    fixture = tmp_path / "deals.json"
    fixture.write_text(json.dumps({
        "format_version": 1, "server": "Example-Demo", "currency": "USD", "trade_mode": "DEMO",
        "server_offset_minutes": 0, "balance": "0", "deals": [raw(d) for d in DEALS[:4]],
    }))  # fmt: skip
    sent: list[tuple[Severity, str, str]] = []

    class Recorder:
        async def notify(self, severity: Severity, title: str, body: str = "") -> None:
            sent.append((severity, title, body))

    script = load()
    monkeypatch.setattr(
        script, "Settings", lambda: SimpleNamespace(DATABASE_URL=db_url, CONFIG_PATH=tmp_path / "none.yaml")
    )
    monkeypatch.setattr(script, "notifier_for", lambda *_a: Recorder())
    assert script.main(["--fixture", str(fixture), "--account", "acc", "--alert"]) == 1  # nothing recorded
    ((severity, title, body),) = sent
    assert (severity, title) == (Severity.CRITICAL, "Ledger mismatch: 2 difference(s)")
    assert "UNRECORDED" in body
    with factory() as s:
        beat = s.scalars(select(HeartbeatRow).where(HeartbeatRow.component == "job.verify_ledger")).one()
        assert (beat.status, beat.detail["positions"], len(beat.detail["differences"])) == ("diff", 2, 2)
        assert [e.type for e in s.scalars(select(EventRow)).all()] == ["ledger.mismatch"]
    assert script.main(["--fixture", str(tmp_path / "missing.json"), "--alert"]) == 2
    assert sent[-1][1] == "Ledger check FAILED"
    empty = tmp_path / "empty.json"
    empty.write_text(fixture.read_text().replace(json.dumps([raw(d) for d in DEALS[:4]]), "[]"))
    sent.clear()
    assert script.main(["--fixture", str(empty), "--account", "acc", "--alert"]) == 0
    assert sent == []
    with factory() as s:
        beat = s.scalars(select(HeartbeatRow).where(HeartbeatRow.component == "job.verify_ledger")).one()
        assert beat.status == "ok"
    capsys.readouterr()
