"""Specialist analysts (roadmap 8.3, docs/01 §5): one LLM analyst per playbook family.

A specialist is the analyst (``agents/analyst.py``) narrowed to one family of setups — trend continuation,
volatility breakout, reversal (``committee.families`` maps setup tags to families) — with its own prompt
(``specialist_v1.j2``: the family's brief first, then the same data the analyst sees). It sees only the
candidates of its family, so it can only take one of those; every validation and fallback of docs/03 §7.3
applies unchanged. A specialist without a candidate of its family is not asked (HOLD, no call).

Each specialist's calls are recorded in ``llm_calls`` as agent ``specialist_<family>``, so cost and usage show
per specialist. The committee (``agents/portfolio_manager.py``) combines their proposals.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from aifund.agents.analyst import Analyst, AnalystInput, AnalystResult, RecordParse, Verdict
from aifund.agents.prompting import PromptLibrary
from aifund.config.trading_config import LLMConfig
from aifund.domain.decision import SetupCandidate
from aifund.domain.enums import ReasonCode
from aifund.ports.llm import LLMPort

PROMPT = "specialist"
BRIEFS = {
    "trend": "trend continuation: entries in the direction of an established higher-timeframe trend, after a "
    "pullback has reset momentum. You look for trend health and whether the pullback is corrective, not a "
    "turn.",
    "breakout": "volatility breakouts: contraction resolving into expansion. You look for real expansion "
    "(range, close near the extreme, volume) and a trend or catalyst behind it, not a drift through a level.",
    "reversal": "reversals and fades: false breakouts and range extremes that fail. You look for exhaustion "
    "and rejection at the level, and you respect a trend strong enough to run through it.",
}


def brief(family: str) -> str:
    return BRIEFS.get(family, f"the {family} setups listed below.")


class Specialist(Analyst):
    def __init__(
        self,
        family: str,
        setup_tags: Iterable[str],
        llm: LLMPort,
        cfg: LLMConfig,
        *,
        playbooks: Path,
        prompts: PromptLibrary | None = None,
        record_parse: RecordParse | None = None,
        version: int = 1,
    ) -> None:
        super().__init__(
            llm, cfg, playbooks=playbooks, prompts=prompts, record_parse=record_parse, version=version,
            prompt=PROMPT, agent=f"specialist_{family}",
        )  # fmt: skip
        self.family = family
        self.setup_tags = frozenset(setup_tags)

    def relevant(self, candidates: Iterable[SetupCandidate]) -> list[SetupCandidate]:
        return [c for c in candidates if c.setup_tag in self.setup_tags]

    def context(self, inp: AnalystInput) -> dict[str, Any]:
        return {**super().context(inp), "family": self.family, "brief": brief(self.family)}

    async def analyse(self, inp: AnalystInput) -> AnalystResult:
        mine = self.relevant(inp.candidates)
        if not mine:
            return AnalystResult(
                Verdict.HOLD, reason=ReasonCode.ANALYST_HOLD, detail=f"no {self.family} candidate",
                prompt_version=f"{PROMPT}_v{self._version}",
            )  # fmt: skip
        return await super().analyse(replace(inp, candidates=mine))
