"""scripts/research.py end to end on a tiny export: ledger lines, report file, an honest failing gate."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from aifund.adapters import history_store as hs
from aifund.domain.enums import Timeframe
from aifund.research.ledger import Ledger, Split
from tests.unit.research.test_signals import flat
from tests.unit.risk.test_stops import XAU

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "research.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("research_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def export(tmp_path: Path) -> Path:
    root = tmp_path / "history"
    hs.write_specs(root, {"XAUUSD": XAU})
    for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4):
        hs.write_bars(root, "XAUUSD", tf, flat(tf, days=10))
    start = datetime(2026, 3, 2, tzinfo=UTC)
    hs.write_manifest(
        root,
        hs.Manifest(
            exported_at=start + timedelta(days=10),
            server="Test",
            account_trade_mode="DEMO",
            server_offset_minutes=0,
            start=start,
            end=start + timedelta(days=10),
            rows={"XAUUSD": {"M15": 960}},
        ),
    )
    return root


def test_baseline_study_records_every_trial_and_fails_honestly(
    export: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = load()
    out = tmp_path / "research"
    code = script.main(
        ["baseline", "--symbols", "XAUUSD", "--history", str(export), "--out", str(out), "--spend-holdout"]
    )
    assert code == 0
    trials = Ledger(out / "ledger.jsonl").trials()
    assert [t.split for t in trials] == [Split.IN_SAMPLE] * len(script.GRID) + [Split.WALK_FORWARD]
    printed = capsys.readouterr().out
    assert "[ ] oos_signals: 0 out-of-sample signals" in printed
    assert "holdout NOT spent" in printed  # the gate failed, so the holdout stays clean
    (report,) = out.glob("report_baseline_*.json")
    data = json.loads(report.read_text())
    assert data["survives_fdr"] is False
    assert data["holdout"] is None
    assert data["throughput"]["too_slow"] is True
