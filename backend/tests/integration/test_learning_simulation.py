"""End-to-end learning simulation (roadmap 7.11, docs/04): deterministic, on a moving clock.

A stream of signals (12 a day) passes through the REAL rule engine the pipeline uses (``RulebookCache`` over
the database): every match becomes a ``rule_evaluations`` row, a signal an ACTIVE block rule stops becomes a
BLOCKED virtual trade, any other becomes a trade. The planted condition — longs above RSI 40 in the NY
session — loses 90% of the time until day 45, then behaves like everything else. Every night the learning
loop runs (nightly audit with a FakeLLM auditor, lifecycle); the operator approves what waits for approval.

Expected story: the loop discovers the pattern, validates it, shadows it, the operator approves the block,
it goes ACTIVE and blocks; when the condition stops losing, the blocked signals' virtual trades show it, the
reviews fail twice and the rule is RETIRED.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.llm.fake_llm import FakeLLM
from aifund.agents.auditor import Auditor
from aifund.config.loader import load_trading_config
from aifund.config.trading_config import LLMConfig
from aifund.domain.enums import CommandType, Direction, RuleStatus, Side, VirtualArm
from aifund.engine.learning import Learning
from aifund.engine.rulebook import RulebookCache
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.learning import RuleEvaluationRepository, RuleRepository
from aifund.persistence.tables import RuleRow
from aifund.ports.llm import LLMRequest
from aifund.rules.dsl import RuleContext, proposal_features
from aifund.rules.miner import MinerConfig
from tests.fakes.learning import seed_outcome

pytestmark = pytest.mark.scenario
CONFIG = Path(__file__).resolve().parents[3] / "config" / "trading.example.yaml"
PER_DAY, HISTORY_DAYS, DAYS, CHANGE_DAY = 12, 40, 140, 45
SESSIONS = ("ASIA", "LONDON", "OVERLAP", "NY")
PLANTED = {
    "scope": {"directions": ["LONG"]},
    "conditions": {
        "all": [
            {"feature": "ctx.session", "op": "==", "value": "NY"},
            {"feature": "m15.rsi14", "op": ">", "value": 40},
        ]
    },
}


class Notices:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def notify(self, severity: Any, title: str, body: str = "") -> None:
        self.sent.append(title)


def auditor_answer(request: LLMRequest) -> dict[str, Any]:
    """Proposes the planted mechanism, citing the strongest surviving cluster the prompt shows."""
    cluster = next(
        (line.split(" | ")[0] for line in request.messages[1].content.splitlines() if line.startswith("C-")),
        None,
    )
    rules = [
        {
            **PLANTED,
            "action": {"type": "penalty", "points": 10},
            "cited_clusters": [cluster],
            "hypothesis": "NY-session longs buy into the US open's liquidity sweep and get stopped.",
        }
    ]
    return {
        "candidate_rules": rules if cluster else [],
        "lessons_markdown": "",
        "strategy_level_findings": [],
    }


class Market:
    """Signals and their outcomes; the planted condition loses until ``CHANGE_DAY``."""

    def __init__(self) -> None:
        self.rng = np.random.default_rng(2026)

    def signal(self, day: int) -> tuple[Direction, dict[str, Any], float]:
        rsi = float(self.rng.uniform(5, 95))
        session = SESSIONS[int(self.rng.integers(0, 4))]
        direction = Direction.LONG if self.rng.random() < 0.7 else Direction.SHORT
        features = {
            "m15.rsi14": round(rsi, 2),
            "h1.adx14": round(float(self.rng.uniform(10, 50)), 2),
            "m15.nr7": bool(self.rng.random() < 0.2),
            "ctx.session": session,
        }
        planted = direction is Direction.LONG and session == "NY" and rsi > 40 and day < CHANGE_DAY
        win = self.rng.random() > 0.9 if planted else self.rng.random() < 0.45
        return direction, features, 1.5 if win else -1.0


def trade(
    factory: sessionmaker[Session], clock: FakeClock, cache: RulebookCache, day: int, market: Market
) -> str | None:
    """One signal through the rule engine as the pipeline applies it; returns the blocking rule, if any."""
    direction, features, r = market.signal(day)
    engine = cache.current()
    ctx = RuleContext(
        "XAUUSD",
        direction,
        "mtf_trend_pullback",
        "M15",
        {**features, **proposal_features(direction, "mtf_trend_pullback", 70, features)},
    )
    verdict = engine.evaluate(ctx)
    with unit_of_work(factory) as s:
        decision_id, _ = seed_outcome(
            s,
            when=clock.now(),
            r=r,
            features=features,
            side=Side.BUY if direction is Direction.LONG else Side.SELL,
            virtual=verdict.blocked_by is not None,
            arm=VirtualArm.BLOCKED,
        )
        RuleEvaluationRepository(s).add_many(
            decision_id,
            [
                {
                    "rule_id": e.rule_id,
                    "rule_version": e.rule_version,
                    "mode": e.mode,
                    "matched": e.matched,
                    "action_applied": e.action_applied,
                }
                for e in verdict.evaluations
            ],
        )
    return verdict.blocked_by


def statuses(factory: sessionmaker[Session]) -> list[tuple[str, int, RuleStatus]]:
    with factory() as s:
        rows = s.scalars(select(RuleRow).order_by(RuleRow.rule_id, RuleRow.version)).all()
        return [(r.rule_id, r.version, r.status) for r in rows]


async def test_the_loop_discovers_enforces_and_retires_a_planted_pattern(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    cfg = load_trading_config(CONFIG).config
    cfg = cfg.model_copy(update={"learning": cfg.learning.model_copy(update={"window_days": 60})})
    notices = Notices()
    llm = FakeLLM([auditor_answer])
    loop = Learning(
        cfg,
        factory,
        clock,
        notices,
        auditor=Auditor(llm, LLMConfig(analyst_model="a", auditor_model="b")),
        miner_config=MinerConfig(resamples=200, min_matches_total=20, holdout_fraction=0.3),
    )
    cache = RulebookCache(factory, clock, max_total_penalty=cfg.learning.max_total_penalty)
    market = Market()
    step = timedelta(hours=24 / PER_DAY)
    for _ in range(HISTORY_DAYS * PER_DAY):  # history before the loop starts (no rules yet)
        trade(factory, clock, cache, 0, market)
        clock.advance(step.total_seconds())

    story: dict[str, int] = {}
    blocked_before = blocked_after = 0
    for day in range(1, DAYS + 1):
        for _ in range(PER_DAY):
            blocked = trade(factory, clock, cache, day, market)
            if blocked is not None:
                if day < CHANGE_DAY:
                    blocked_before += 1
                else:
                    blocked_after += 1
            clock.advance(step.total_seconds())
        await (
            loop.run_once()
        )  # 09:00 each day: past the 00:30 schedule → the nightly audit, then the lifecycle
        await loop.lifecycle(force=True)
        for rule_id, _, status in statuses(factory):
            story.setdefault(f"{rule_id} {status.value}", day)
        with factory() as s:
            waiting = [
                r
                for r in RuleRepository(s, clock).with_status(RuleStatus.SHADOW)
                if (r.evidence or {}).get("awaiting_approval")
            ]
        for r in waiting:  # the operator approves what the loop asks for
            await loop.handle(CommandType.APPROVE_RULE, {"rule_id": r.rule_id, "version": r.version})

    first = min(k.split()[0] for k in story)
    assert story[f"{first} SHADOW"] < story[f"{first} ACTIVE"] < CHANGE_DAY, story
    assert story[f"{first} ACTIVE"] < story[f"{first} RETIRED"], story
    assert story[f"{first} RETIRED"] > CHANGE_DAY + 30, story  # needs two failed reviews on the new regime
    assert blocked_before > 0 and blocked_after > 0  # noqa: PT018 - it blocked, and kept measuring via virtuals
    with factory() as s:
        retired = RuleRepository(s, clock).require(first, 1)
    assert "failed re-validation twice" in (retired.retire_reason or "")
    assert {f"Rule {first}v1 ACTIVE", f"Rule {first}v1 RETIRED"} <= set(notices.sent)
