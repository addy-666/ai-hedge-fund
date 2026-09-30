"""The committee (roadmap 8.4, docs/03 §8): specialists per setup family, a ballot, the critic, a decision.

1. Every specialist with a candidate of its family is asked, concurrently (``agents/specialists.py``).
2. Fail closed: if any asked specialist has no readable answer (INVALID), the committee holds with its reason
   — its view is unknown, and it might have disagreed.
3. The specialists that proposed a trade vote (``portfolio_manager.ballot``): none → COMMITTEE_HOLD, opposite
   directions → COMMITTEE_SPLIT, else one direction at the weighted mean confidence.
4. The risk critic argues against the most confident proposal; no critique → CRITIC_UNAVAILABLE (hold).
5. The portfolio manager (the committee's own instance and calibrator) subtracts the critic's penalty and
   applies the learned rules exactly as for the analyst.

The committee only decides; the pipeline records the result (``record()``) and, in shadow mode, a
SHADOW_COMMITTEE virtual trade. Its LLM spend is summed per decision and logged (``committee.cost``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog

from aifund.agents.analyst import AnalystInput, AnalystResult, Verdict
from aifund.agents.critic import Critic, CriticInput, CriticResult
from aifund.agents.portfolio_manager import (
    Ballot,
    PortfolioDecision,
    PortfolioManager,
    Vote,
    ballot,
    critic_penalty,
)
from aifund.agents.specialists import Specialist
from aifund.config.trading_config import CommitteeConfig
from aifund.domain.enums import Direction, ReasonCode
from aifund.rules.engine import RuleVerdict

log = structlog.get_logger(__name__)
RuleFn = Callable[[Direction, str, int], RuleVerdict]


@dataclass
class Deliberation:
    decision: PortfolioDecision | None = None  # None: the committee holds
    reason: ReasonCode | None = None
    detail: str = ""
    specialists: dict[str, AnalystResult] = field(default_factory=dict)
    ballot: Ballot | None = None
    critic: CriticResult | None = None
    critic_penalty: int = 0
    cost_usd: Decimal = Decimal(0)

    @property
    def calls(self) -> int:
        results: list[Any] = [*self.specialists.values(), *([self.critic] if self.critic else [])]
        return sum(len(r.call_ids) for r in results)

    def record(self) -> dict[str, Any]:
        """The ``committee`` block of the decision's proposal JSON."""
        d = self.decision.decision if self.decision is not None else None
        critique = self.critic.critique if self.critic is not None else None
        return {
            "specialists": {
                family: {
                    "verdict": r.verdict.value,
                    "direction": r.proposal.direction.value if r.proposal else None,
                    "confidence": r.proposal.confidence if r.proposal else None,
                    "setup_tag": r.proposal.setup_tag if r.proposal else None,
                    "thesis": r.proposal.thesis if r.proposal else None,
                    "reason": r.reason.value if r.reason else None,
                    "detail": r.detail,
                    "prompt_version": r.prompt_version,
                }
                for family, r in self.specialists.items()
            },
            "combined": self.ballot.combined if self.ballot else None,
            "proposer": self.ballot.best.family if self.ballot else None,
            "critique": critique.model_dump(mode="json") if critique else None,
            "critic_penalty": self.critic_penalty,
            "direction": d.direction.value if d else None,
            "setup_tag": d.setup_tag if d else None,
            "confidence": d.llm_confidence if d else None,
            "calibrated_confidence": d.calibrated_confidence if d else None,
            "penalty_points": d.penalty_points if d else None,
            "final_confidence": d.final_confidence if d else None,
            "blocked_by": self.decision.blocked_by if self.decision else None,
            "rules_matched": list(self.decision.rules_matched) if self.decision else [],
            "reason": self.reason.value if self.reason else None,
            "detail": self.detail,
            "cost_usd": str(self.cost_usd),
            "calls": self.calls,
        }


class Committee:
    def __init__(
        self,
        specialists: Sequence[Specialist],
        critic: Critic,
        cfg: CommitteeConfig,
        *,
        portfolio: PortfolioManager | None = None,
    ) -> None:
        self._specialists = list(specialists)
        self._critic = critic
        self._cfg = cfg
        self._portfolio = portfolio or PortfolioManager()

    async def deliberate(self, inp: AnalystInput, rules: RuleFn) -> Deliberation:
        out = Deliberation()
        try:
            await self._deliberate(inp, rules, out)
        finally:
            costs = [r.cost_usd for r in out.specialists.values()]
            out.cost_usd = sum(costs, out.critic.cost_usd if out.critic else Decimal(0))
            log.info(
                "committee.cost", decision_id=inp.decision_id, cost_usd=str(out.cost_usd), calls=out.calls,
                outcome=out.reason.value if out.reason else "DECIDED",
            )  # fmt: skip
        return out

    async def _deliberate(self, inp: AnalystInput, rules: RuleFn, out: Deliberation) -> None:
        asked = [s for s in self._specialists if s.relevant(inp.candidates)]
        results = await asyncio.gather(*(s.analyse(inp) for s in asked))
        out.specialists = {s.family: r for s, r in zip(asked, results, strict=True)}
        invalid = [(f, r) for f, r in out.specialists.items() if r.verdict is Verdict.INVALID]
        if invalid:
            family, bad = invalid[0]
            out.reason, out.detail = bad.reason, f"{family} specialist: {bad.detail}"[:500]
            return
        votes = [
            Vote(family, self._cfg.weight(family), r.proposal)
            for family, r in out.specialists.items()
            if r.verdict is Verdict.PROPOSAL and r.proposal is not None
        ]
        result = ballot(votes)
        if isinstance(result, ReasonCode):
            out.reason = result
            out.detail = (
                ", ".join(
                    f"{f} {r.proposal.direction.value if r.proposal else r.verdict.value}"
                    for f, r in out.specialists.items()
                )
                or "no specialist has a candidate of its family"
            )
            return
        out.ballot = result
        out.critic = await self._critic.critique(CriticInput(inp, result.best.proposal, result.best.family))
        critique = out.critic.critique
        if critique is None:
            reason = out.critic.reason.value if out.critic.reason else "none"
            out.reason, out.detail = ReasonCode.CRITIC_UNAVAILABLE, f"{reason}: {out.critic.detail}"[:500]
            return
        out.critic_penalty = critic_penalty(critique, self._cfg.critic_penalty)
        raw = min(max(result.combined - out.critic_penalty, 0), 100)
        verdict = rules(result.direction, result.best.proposal.setup_tag, raw)
        out.decision = self._portfolio.decide_committee(
            decision_id=inp.decision_id, symbol=inp.symbol, ballot=result, penalty=out.critic_penalty,
            rules=verdict,
        )  # fmt: skip
