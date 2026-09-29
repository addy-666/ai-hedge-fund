"""Shadow pairs for gate G-LLM (roadmap R.9): one per decided bar, only once every arm has finished."""

from __future__ import annotations

import importlib.util
import sys
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.config.evidence import load_g_llm
from aifund.domain.enums import CloseReason, DecisionOutcome, Side, VirtualArm, VirtualStatus
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.decisions import DecisionRepository
from aifund.persistence.repositories.virtual import VirtualTradeRepository
from aifund.research.gates import GateCheck
from aifund.research.uplift import UpliftReport

from .conftest import T0

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "uplift_report.py"


def decided(
    factory: sessionmaker[Session],
    clock: FakeClock,
    minutes: int,
    arms: dict[VirtualArm, tuple[VirtualStatus, str | None]],
    cost: str = "0.002",
) -> str:
    with unit_of_work(factory) as s:
        d = DecisionRepository(s, clock).add(
            account_id="acc", symbol="XAUUSD", trigger_tf="M15", bar_time=T0 + timedelta(minutes=minutes),
            stage_reached="DECISION", outcome=DecisionOutcome.SHADOW, model="m", cost_usd=D(cost),
        )  # fmt: skip
        repo = VirtualTradeRepository(s, clock)
        for arm, (status, r) in arms.items():
            v = repo.add_pending(
                account_id="acc", decision_id=d.id, symbol="XAUUSD", side=Side.BUY, setup_tag="stub", arm=arm,
                entry_time=T0 + timedelta(minutes=minutes + 15), sl_distance=D("10"), tp_distance=D("20"),
                expires_at=T0 + timedelta(hours=12), expire_reason=CloseReason.TIME_STOP,
            )  # fmt: skip
            if status is VirtualStatus.NO_ENTRY:
                repo.update(v.id, status)
            elif status is not VirtualStatus.PENDING:
                repo.update(v.id, VirtualStatus.OPEN, entry_price=D("100"))
                if status is not VirtualStatus.OPEN:
                    repo.update(v.id, status, r_multiple=D(r) if r else None, exit_reason=CloseReason.SL)
        return d.id


def test_pairs_count_finished_bars_only(factory: sessionmaker[Session], clock: FakeClock) -> None:
    B, A = VirtualArm.SHADOW_BASELINE, VirtualArm.SHADOW_ANALYST
    both = decided(factory, clock, 0, {B: (VirtualStatus.CLOSED, "-1"), A: (VirtualStatus.CLOSED, "2")})
    held = decided(factory, clock, 15, {B: (VirtualStatus.CLOSED, "1.5")})  # the analyst held: 0R
    missed = decided(
        factory, clock, 30, {B: (VirtualStatus.NO_ENTRY, None), A: (VirtualStatus.EXPIRED, "0.3")}
    )
    decided(factory, clock, 45, {B: (VirtualStatus.CLOSED, "1"), A: (VirtualStatus.OPEN, None)})  # unfinished
    decided(factory, clock, 60, {A: (VirtualStatus.CLOSED, "1")})  # no baseline shadow: not a pair
    decided(factory, clock, 75, {VirtualArm.BLOCKED: (VirtualStatus.CLOSED, "1")})  # not a shadow at all
    with factory() as s:
        pairs = VirtualTradeRepository(s, clock).shadow_pairs("acc")
        assert VirtualTradeRepository(s, clock).shadow_pairs("other") == []
    assert [(p.decision_id, p.baseline_r, p.analyst_r, p.cost_usd) for p in pairs] == [
        (both, D("-1"), D("2"), D("0.002")),
        (held, D("1.5"), D(0), D("0.002")),
        (missed, D(0), D("0.3"), D("0.002")),
    ]


def load() -> Any:
    spec = importlib.util.spec_from_file_location("uplift_report_script", SCRIPT)
    assert spec is not None and spec.loader is not None  # noqa: PT018
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_report_script_refuses_a_sign_off_that_did_not_pass(
    factory: sessionmaker[Session], clock: FakeClock, db_url: str, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    B, A = VirtualArm.SHADOW_BASELINE, VirtualArm.SHADOW_ANALYST
    decided(factory, clock, 0, {B: (VirtualStatus.CLOSED, "-1"), A: (VirtualStatus.CLOSED, "2")})
    script = load()
    monkeypatch.setattr(
        script, "Settings", lambda: SimpleNamespace(DATABASE_URL=db_url, CONFIG_PATH=tmp_path / "none.yaml")
    )
    evidence = tmp_path / "evidence"
    base = ["--account", "acc", "--evidence", str(evidence)]
    assert script.main(base) == 2  # no equity snapshot and no --risk-usd
    assert script.main([*base, "--risk-usd", "50", "--sign-off", "op"]) == 1
    out = capsys.readouterr()
    assert "1 paired bars: baseline -1.000R, analyst +2.000R" in out.out
    assert "NOT PASSED" in out.out and "sign-off refused" in out.err  # noqa: PT018
    assert load_g_llm(evidence) == []

    passing = UpliftReport(n=200, baseline_mean=0.0, analyst_mean=0.2, cost_mean_r=0.01,
                           uplift=SimpleNamespace(mean=0.19, ci_low=0.05, ci_high=0.33, render=lambda: "x"),  # type: ignore[arg-type]
                           checks=[GateCheck("uplift_ci", True, "")])  # fmt: skip
    monkeypatch.setattr(script, "uplift", lambda *a, **kw: passing)
    assert script.main([*base, "--risk-usd", "50", "--sign-off", "op"]) == 0
    (signoff,) = load_g_llm(evidence)
    assert (signoff.prompt_version, signoff.n, signoff.ci90, signoff.signed_off_by) == (
        "analyst_v1",
        200,
        (0.05, 0.33),
        "op",
    )
