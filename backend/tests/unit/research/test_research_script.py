"""scripts/research.py end to end on a tiny export: ledger lines, report file, an honest failing gate."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import SecretStr

from aifund.adapters import history_store as hs
from aifund.adapters.llm.fake_llm import FakeLLM
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


DOJI = {
    "id": "OP-1", "version": 1, "setup_tag": "doji_probe",
    "long": {"all": [{"feature": "m15.candle_dir", "op": "==", "value": "DOJI"}]}, "short": None,
    "invalidation": {"type": "atr", "tf": "trigger", "k": 1.0}, "target": {"type": "rr", "rr": 2.0},
    "mechanism": "A doji on a flat market: a probe that must fail the gate.",
}  # fmt: skip


def args(export: Path, tmp_path: Path, *extra: str) -> list[str]:
    return [*extra, "--symbols", "XAUUSD", "--history", str(export), "--out", str(tmp_path / "research"),
            "--evidence", str(tmp_path / "evidence")]  # fmt: skip


def test_dsl_hypotheses_from_a_file_go_through_the_loop(
    export: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ideas = tmp_path / "ideas.json"
    ideas.write_text(json.dumps([DOJI]))
    script = load()
    assert script.main(args(export, tmp_path, "dsl", "--file", str(ideas), "--spend-holdout")) == 0
    (trial,) = Ledger(tmp_path / "research" / "ledger.jsonl").trials()
    assert (trial.split, trial.origin.value) == (Split.WALK_FORWARD, "operator")
    printed = capsys.readouterr().out
    assert "OP-1 doji_probe" in printed and "failed the walk-forward part of E1" in printed  # noqa: PT018
    assert not (tmp_path / "evidence").exists()
    assert script.main(args(export, tmp_path, "dsl")) == 2  # --file is required


def test_llm_needs_an_api_key(
    export: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = load()
    monkeypatch.setattr(
        script, "Settings", lambda: SimpleNamespace(DEEPSEEK_API_KEY=None, CONFIG_PATH=tmp_path / "none.yaml")
    )
    assert script.main(args(export, tmp_path, "llm")) == 2
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err


def test_llm_rounds_propose_and_judge(
    export: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = load()
    idea = {k: v for k, v in DOJI.items() if k not in ("id", "version")}
    broken = {**idea, "setup_tag": "broken", "long": {"all": [{"feature": "nope", "op": "==", "value": 1}]}}
    fake = FakeLLM([{"hypotheses": [idea, broken]}])
    monkeypatch.setattr(
        script, "Settings",
        lambda: SimpleNamespace(
            DEEPSEEK_API_KEY=SecretStr("k"), DATABASE_URL=f"sqlite:///{tmp_path / 'x.db'}",
            CONFIG_PATH=tmp_path / "none.yaml",
        ),
    )  # fmt: skip
    monkeypatch.setattr(script, "DeepSeekClient", lambda *a, **kw: fake)
    assert script.main(args(export, tmp_path, "llm", "--rounds", "2")) == 0
    printed = capsys.readouterr().out
    assert "round 1: 1 valid, 2 rejected" in printed and "round 2:" in printed  # noqa: PT018
    assert "unknown feature 'nope'" in printed
    prompt = fake.requests[-1].messages[1].content
    assert "doji_probe | LONG m15.candle_dir == DOJI" in prompt  # round 2 sees round 1 in the ledger
    assert "DESCRIPTIVE TABLES" in prompt  # (empty here: 10 days of bars never warm the indicators up)
    trials = Ledger(tmp_path / "research" / "ledger.jsonl").trials()
    assert [t.split for t in trials] == [Split.WALK_FORWARD]  # the same idea twice is one trial
