"""Risk critic (roadmap 8.3, docs/01 §5): the committee's devil's advocate, one LLM call per decision.

It reads the best specialist proposal with the same bar data the specialists saw and answers with a
``Critique``: at most five objections, each HIGH / MEDIUM / LOW, and a summary. The critic never changes a
direction, a level or a size; the portfolio manager turns its objections into confidence penalties
(docs/03 §8). The reply is validated like the analyst's (strict schema, one repair call); a provider failure
or a second invalid reply leaves no critique, and the committee then holds (fail closed).

Calls are recorded in ``llm_calls`` as agent ``critic`` with the decision id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aifund.agents.analyst import AnalystInput, RecordParse
from aifund.agents.prompting import (
    PromptLibrary,
    atr_candles,
    candidate_lines,
    feature_table,
    fmt,
    load_playbook,
    position_line,
)
from aifund.config.trading_config import LLMConfig
from aifund.domain.decision import Critique, TradeProposal
from aifund.domain.enums import ReasonCode
from aifund.ports.llm import (
    LLMBudgetExhausted,
    LLMCircuitOpen,
    LLMError,
    LLMInvalidOutput,
    LLMMessage,
    LLMPort,
    LLMRequest,
    LLMResponse,
)

PROMPT = "critic"


@dataclass(frozen=True)
class CriticInput:
    bar: AnalystInput  # the same market data the specialists saw
    proposal: TradeProposal
    proposer: str  # the specialist's family


@dataclass
class CriticResult:
    critique: Critique | None = None
    reason: ReasonCode | None = None  # why there is no critique
    detail: str = ""
    model: str | None = None
    cost_usd: Decimal = Decimal(0)
    call_ids: list[str] = field(default_factory=list)
    prompt_version: str = ""


class Critic:
    def __init__(
        self,
        llm: LLMPort,
        cfg: LLMConfig,
        *,
        playbooks: Path,
        prompts: PromptLibrary | None = None,
        record_parse: RecordParse | None = None,
        version: int = 1,
    ) -> None:
        self._llm = llm
        self._cfg = cfg
        self._playbooks = playbooks
        self._prompts = prompts or PromptLibrary()
        self._record_parse = record_parse
        self._version = version

    def context(self, inp: CriticInput) -> dict[str, Any]:
        bar, p = inp.bar, inp.proposal
        taken = [c for c in bar.candidates if c.setup_tag == p.setup_tag]
        return dict(
            symbol=bar.symbol,
            trigger_tf=bar.roles.trigger.value,
            setup_tf=bar.roles.setup.value,
            context_tfs=",".join(tf.value for tf in bar.roles.context),
            bar_closed=f"{bar.snapshot.bar_time + _length(bar):%Y-%m-%d %H:%M}",
            bid=fmt(bar.tick.bid),
            ask=fmt(bar.tick.ask),
            spread_points=bar.spread_points,
            feature_table=feature_table(bar.snapshot),
            atr=fmt(bar.trigger_atr),
            candles=atr_candles(bar.trigger_bars, bar.trigger_atr),
            candidates=candidate_lines(taken),
            playbooks=[load_playbook(self._playbooks, pid) for pid in sorted({c.playbook_id for c in taken})],
            lessons=bar.lessons,
            proposer=inp.proposer,
            direction=p.direction.value,
            confidence=p.confidence,
            setup_tag=p.setup_tag,
            invalidation=fmt(p.invalidation_price),
            target=fmt(p.target_price),
            thesis=p.thesis,
            key_risks="; ".join(p.key_risks) or "none",
            position=position_line(bar.position),
            portfolio=bar.portfolio,
        )

    async def critique(self, inp: CriticInput) -> CriticResult:
        rendered = self._prompts.render(PROMPT, self._version, self.context(inp))
        result = CriticResult(prompt_version=f"{PROMPT}_v{self._version}")
        messages = list(rendered.messages)
        problem = ""
        for attempt in (1, 2):  # the answer, then at most one repair
            if attempt == 2:
                messages.append(
                    LLMMessage(
                        role="user",
                        content=f"Your previous reply was rejected: {problem}. Reply again with only the "
                        "corrected JSON object, exactly in the required format.",
                    )
                )
            try:
                response = await self._llm.complete(self._request(inp, messages))
            except LLMInvalidOutput as exc:
                problem = f"it was not a single JSON object ({exc})"
                continue
            except LLMBudgetExhausted as exc:
                return self._none(result, ReasonCode.LLM_BUDGET_EXHAUSTED, str(exc))
            except LLMCircuitOpen as exc:
                return self._none(result, ReasonCode.LLM_UNAVAILABLE, str(exc))
            except LLMError as exc:
                return self._none(result, ReasonCode.LLM_ERROR, str(exc))
            self._account(result, response)
            try:
                critique = Critique.model_validate_json(response.text)
            except ValidationError as exc:
                problem = "; ".join(
                    f"{'.'.join(str(x) for x in e['loc']) or 'reply'}: {e['msg']}" for e in exc.errors()[:6]
                )[:500]
                await self._record(response, None, False, f"schema: {problem}")
                messages.append(LLMMessage(role="assistant", content=response.text))
                continue
            await self._record(response, critique.model_dump(mode="json"), True, None)
            result.critique = critique
            return result
        return self._none(result, ReasonCode.LLM_INVALID_OUTPUT, problem)

    def _request(self, inp: CriticInput, messages: list[LLMMessage]) -> LLMRequest:
        return LLMRequest(
            agent=PROMPT,
            model=self._cfg.analyst_model,
            messages=messages,
            temperature=self._cfg.temperature,
            timeout_s=self._cfg.timeout_s,
            prompt_template=PROMPT,
            prompt_version=str(self._version),
            decision_id=inp.bar.decision_id,
        )

    def _none(self, result: CriticResult, reason: ReasonCode, detail: str) -> CriticResult:
        result.reason, result.detail = reason, detail[:500]
        return result

    def _account(self, result: CriticResult, response: LLMResponse) -> None:
        result.model = response.model
        result.cost_usd += response.cost_usd or Decimal(0)
        if response.call_id is not None:
            result.call_ids.append(response.call_id)

    async def _record(
        self, response: LLMResponse, parsed: dict[str, Any] | None, valid: bool, error: str | None
    ) -> None:
        if self._record_parse is not None and response.call_id is not None:
            await self._record_parse(response.call_id, parsed, valid, error)


def _length(bar: AnalystInput) -> timedelta:
    return timedelta(minutes=bar.roles.trigger.minutes)
