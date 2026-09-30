"""Calibration in the engine (roadmap 8.5): samples per source from the decisions and their trades, the weekly
fit, approve and auto activation, operator commands, and the pipeline's cache."""

from __future__ import annotations

import itertools
import random
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.llm.fake_llm import FakeLLM
from aifund.config.loader import load_trading_config
from aifund.config.trading_config import StrategyConfig, TradingConfig
from aifund.domain.enums import (
    CalibrationStatus,
    CloseReason,
    CommandType,
    DecisionOutcome,
    Side,
    TradeStatus,
    VirtualArm,
    VirtualStatus,
)
from aifund.engine.calibration import Calibration, CalibrationCache, CalibrationError
from aifund.engine.learning import Learning, LearningError
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.calibration import CalibrationRepository, raw_confidence
from aifund.persistence.repositories.decisions import DecisionRepository
from aifund.persistence.tables import CalibrationModelRow, DecisionRow, EventRow, TradeRow, VirtualTradeRow
from aifund.ports.system import Severity
from tests.integration.test_baseline_replay import echo_candidate, market, replay  # noqa: F401

from .conftest import T0

CONFIG = Path(__file__).resolve().parents[3] / "config" / "trading.example.yaml"
IDS = itertools.count()


class Notices:
    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str, str]] = []

    async def notify(self, severity: Severity, title: str, body: str = "") -> None:
        self.sent.append((severity, title, body))


def config(**calibration: Any) -> TradingConfig:
    cfg = load_trading_config(CONFIG).config
    cal = cfg.learning.calibration.model_copy(update=calibration)
    return cfg.model_copy(
        update={
            "learning": cfg.learning.model_copy(update={"calibration": cal}),
            "engine": cfg.engine.model_copy(update={"account_label": "acc"}),
        }
    )


def virtual(s: Session, decision_id: str, arm: VirtualArm, r: str | None, when: Any) -> None:
    s.add(
        VirtualTradeRow(
            id=f"V{next(IDS)}", account_id="acc", decision_id=decision_id, arm=arm, symbol="XAUUSD",
            side=Side.BUY, entry_time=when, sl_distance=D("10"), tp_distance=D("20"),
            expires_at=when + timedelta(hours=3), expire_reason=CloseReason.TIME_STOP,
            status=VirtualStatus.CLOSED if r is not None else VirtualStatus.OPEN, created_at=when,
            entry_price=D("4150"), r_multiple=D(r) if r is not None else None,
        )
    )  # fmt: skip


def trade(s: Session, decision_id: str, r: str, when: Any) -> None:
    s.add(
        TradeRow(
            id=f"T{next(IDS)}", position_id=next(IDS), account_id="acc", symbol="XAUUSD", side=Side.BUY,
            setup_tag="stub", trigger_tf="M15", open_price=D("4150"), volume_opened=D("0.1"),
            initial_sl=D("4140"), created_at=when, updated_at=when, status=TradeStatus.CLOSED, open_time=when,
            volume_open_now=D(0), close_time=when + timedelta(hours=1), r_multiple=D(r),
            decision_id=decision_id,
        )
    )  # fmt: skip


def decision(s: Session, clock: FakeClock, when: Any, proposal: dict[str, Any] | None) -> str:
    return DecisionRepository(s, clock).add(
        account_id="acc", symbol="XAUUSD", trigger_tf="M15", bar_time=when, stage_reached="DECISION",
        outcome=DecisionOutcome.SHADOW, proposal=proposal,
    ).id  # fmt: skip


def analyst(confidence: int, *, decided: bool = False, verdict: str = "PROPOSAL") -> dict[str, Any]:
    body = {"source": "analyst", "verdict": verdict, "confidence": confidence}
    return body if decided else {"source": "baseline", "confidence": 70, "shadow_analyst": body}


def seed_overconfident(factory: sessionmaker[Session], clock: FakeClock, n: int = 400, seed: int = 3) -> None:
    """An analyst that says c and wins (c - 30)% of the time, as G-LLM shadows; plus committee shadows."""
    rng = random.Random(seed)
    with unit_of_work(factory) as s:
        for i in range(n):
            when = T0 - timedelta(days=60) + timedelta(hours=i)
            c = rng.randint(40, 95)
            won = rng.random() < max(0.05, c / 100 - 0.30)
            proposal = {**analyst(c), "committee": {"confidence": c}}
            d = decision(s, clock, when, proposal)
            virtual(s, d, VirtualArm.SHADOW_ANALYST, "1.5" if won else "-1", when)
            virtual(s, d, VirtualArm.SHADOW_COMMITTEE, "1.5" if won else "-1", when)


def test_raw_confidence_per_source() -> None:
    assert raw_confidence(analyst(72, decided=True), "analyst") == (72, True)
    assert raw_confidence(analyst(64), "analyst") == (64, False)
    assert raw_confidence(analyst(64, verdict="HOLD"), "analyst") is None
    assert raw_confidence({"source": "baseline", "confidence": 70}, "analyst") is None
    assert raw_confidence(None, "analyst") is None
    assert raw_confidence({"committee": {"confidence": 58}}, "committee") == (58, False)
    assert raw_confidence({"committee": {"confidence": None}}, "committee") is None
    assert raw_confidence({"source": "analyst"}, "committee") is None


def test_samples_prefer_the_real_trade(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = T0 - timedelta(days=1)
    with unit_of_work(factory) as s:
        real = decision(s, clock, w, analyst(80, decided=True))
        trade(s, real, "-1", w)
        virtual(s, real, VirtualArm.SHADOW_ANALYST, "2", w)  # the same idea, but the real trade counts
        blocked = decision(s, clock, w + timedelta(minutes=15), analyst(55, decided=True))
        virtual(s, blocked, VirtualArm.BLOCKED, "1", w)  # below the threshold: still measured
        shadow = decision(s, clock, w + timedelta(minutes=30), analyst(70))
        virtual(s, shadow, VirtualArm.SHADOW_ANALYST, "0.5", w)
        baseline_blocked = decision(s, clock, w + timedelta(minutes=45), analyst(60))
        virtual(s, baseline_blocked, VirtualArm.BLOCKED, "1", w)  # the baseline's block: not the analyst's
        running = decision(s, clock, w + timedelta(hours=1), analyst(66))
        virtual(s, running, VirtualArm.SHADOW_ANALYST, None, w)  # unfinished
        baseline = decision(s, clock, w + timedelta(hours=2), {"source": "baseline", "confidence": 70})
        trade(s, baseline, "3", w)
        committee = decision(s, clock, w + timedelta(hours=3), {"committee": {"confidence": 61}})
        virtual(s, committee, VirtualArm.SHADOW_COMMITTEE, "-1", w)
    with factory() as s:
        repo = CalibrationRepository(s, clock)
        got = [(c, r) for c, r, _ in repo.outcomes("acc", "analyst")]
        assert got == [(80, D(-1)), (55, D(1)), (70, D("0.5"))]
        assert [(c, r) for c, r, _ in repo.outcomes("acc", "committee")] == [(61, D(-1))]
        assert repo.outcomes("other", "analyst") == []
        with pytest.raises(ValueError, match="unknown calibration source"):
            repo.outcomes("acc", "baseline")


async def test_approve_mode_waits_for_the_operator(factory: sessionmaker[Session], clock: FakeClock) -> None:
    seed_overconfident(factory, clock)
    notices = Notices()
    cal = Calibration(config(), factory, clock, notices)
    cache = CalibrationCache(factory, clock)
    out = await cal.fit_all("manual")
    assert out["analyst"]["status"] == out["committee"]["status"] == "CANDIDATE"
    assert out["analyst"]["improvement"] >= 0.05
    cache.refresh()
    assert cache.calibrator("analyst")(85) == 85  # nothing active yet
    assert [t for _, t, _ in notices.sent] == [
        "Calibration v1 (analyst) awaits approval", "Calibration v2 (committee) awaits approval",
    ]  # fmt: skip
    # a second fit supersedes the waiting candidate
    await cal.fit_all("manual")
    with factory() as s:
        repo = CalibrationRepository(s, clock)
        assert [(r.version, r.status, r.decided_by) for r in repo.history()] == [
            (4, CalibrationStatus.CANDIDATE, None), (3, CalibrationStatus.CANDIDATE, None),
            (2, CalibrationStatus.REJECTED, "superseded"), (1, CalibrationStatus.REJECTED, "superseded"),
        ]  # fmt: skip
    assert await cal.handle(CommandType.APPROVE_CALIBRATION, {"version": 3}) == {
        "version": 3, "status": "ACTIVE",
    }  # fmt: skip
    cache.refresh()
    assert 40 <= cache.calibrator("analyst")(85) <= 72  # the overconfident 85 (wins ~55%) is marked down
    assert cache.calibrator("committee")(85) == 85
    cache.refresh()  # unchanged: no reload
    await cal.handle(CommandType.REJECT_CALIBRATION, {"version": 4})
    with pytest.raises(CalibrationError, match="not a CANDIDATE"):
        await cal.handle(CommandType.APPROVE_CALIBRATION, {"version": 4})
    with pytest.raises(CalibrationError, match="no calibration model 9"):
        await cal.handle(CommandType.REJECT_CALIBRATION, {"version": 9})
    with pytest.raises(CalibrationError, match="needs a version"):
        await cal.handle(CommandType.APPROVE_CALIBRATION, {})
    with factory() as s:
        events = s.scalars(select(EventRow).where(EventRow.type == "calibration.changed")).all()
    assert [e.payload["what"] for e in events] == ["fitted"] * 4 + ["activated", "rejected"]


async def test_auto_mode_activates_and_retires(factory: sessionmaker[Session], clock: FakeClock) -> None:
    seed_overconfident(factory, clock)
    cal = Calibration(config(activation="auto"), factory, clock, Notices())
    await cal.fit_all("weekly")
    await cal.fit_all("weekly")
    with factory() as s:
        rows = {
            r.version: (r.source, r.status, r.decided_by) for r in CalibrationRepository(s, clock).history()
        }
    assert rows == {
        1: ("analyst", CalibrationStatus.RETIRED, "auto"),
        2: ("committee", CalibrationStatus.RETIRED, "auto"),
        3: ("analyst", CalibrationStatus.ACTIVE, "auto"), 4: ("committee", CalibrationStatus.ACTIVE, "auto"),
    }  # fmt: skip


async def test_no_improvement_is_recorded_and_too_few_is_not(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    rng = random.Random(5)
    with unit_of_work(factory) as s:
        for i in range(300):  # a well-calibrated analyst
            when = T0 - timedelta(days=30) + timedelta(hours=i)
            c = rng.randint(40, 95)
            d = decision(s, clock, when, analyst(c))
            virtual(s, d, VirtualArm.SHADOW_ANALYST, "1" if rng.random() < c / 100 else "-1", when)
    out = await Calibration(config(), factory, clock, Notices()).fit_all("manual")
    assert out["analyst"]["status"] == "REJECTED"
    assert out["committee"] == {"result": "TOO_FEW", "n": 0}
    with factory() as s:
        (row,) = s.scalars(select(CalibrationModelRow)).all()
    assert (row.decided_by, row.details["n_test"], len(row.details["reliability"])) == ("fit", 90, 10)


async def test_the_weekly_schedule(factory: sessionmaker[Session], clock: FakeClock) -> None:
    # T0 is Monday 2026-09-28 09:00 UTC; the fit is due Mondays from 01:00
    cal = Calibration(config(), factory, clock, Notices())
    assert cal.scheduled(T0) == T0.replace(hour=1)
    assert cal.due()
    await cal.fit_all("weekly")  # nothing to fit: nothing stored, but it ran this week
    assert not cal.due()
    clock.advance(days=1)
    assert not cal.due()
    fresh = Calibration(config(), factory, clock, Notices())  # a restart on Tuesday: due once more
    assert fresh.due()
    with unit_of_work(factory) as s:
        CalibrationRepository(s, clock).add(
            source="analyst", method="ISOTONIC", status=CalibrationStatus.REJECTED, params=None, n_samples=1,
        )  # fmt: skip
    assert not fresh.due()  # a model from this week exists
    later = Calibration(config(fit_weekday=3, fit_utc="23:00"), factory, clock, Notices())
    assert not later.due()  # Thursday 23:00 is still ahead


async def test_the_learning_loop_runs_the_fit_and_routes_the_commands(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    seed_overconfident(factory, clock)
    learning = Learning(config(), factory, clock, Notices())
    await learning.run_once()  # Monday after 01:00: the weekly fit
    with factory() as s:
        assert len(CalibrationRepository(s, clock).history()) == 2
    out = await learning.handle(CommandType.APPROVE_CALIBRATION, {"version": 1})
    assert out["status"] == "ACTIVE"
    with pytest.raises(LearningError, match="not a CANDIDATE"):
        await learning.handle(CommandType.APPROVE_CALIBRATION, {"version": 1})
    assert (await learning.handle(CommandType.FIT_CALIBRATION, {}))["trigger"] == "manual"


async def test_the_pipeline_decides_on_the_calibrated_confidence(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
    clock: FakeClock,
) -> None:
    """An ACTIVE analyst model maps the echo analyst's 75 to 65 (between 60 → 50% and 80 → 70%)."""
    with unit_of_work(factory) as s:
        CalibrationRepository(s, clock).add(
            source="analyst", method="ISOTONIC", status=CalibrationStatus.ACTIVE, n_samples=200,
            params={"points": [[60, 0.5], [80, 0.7]]},
        )  # fmt: skip
    llm = FakeLLM([echo_candidate], factory=factory, clock=clock)
    report = await replay(market, factory, strategy=StrategyConfig(analyst_orders=True), llm=llm, days=1)
    assert report.violations == []
    with factory() as s:
        analysed = s.scalars(select(DecisionRow).where(DecisionRow.model == "fake-llm")).all()
    assert analysed
    assert {(d.llm_confidence, d.calibrated_confidence) for d in analysed} == {(75, 65)}
