"""Analyst agent (roadmap 4.4, docs/03 §7): one LLM read of a closed bar, validated into a TradeProposal.

The LLM only proposes. Validation and fallbacks (docs/03 §7.3):

1. The reply must parse into ``TradeProposal`` (strict schema, extra keys forbidden). If it does not, one
   repair call quotes the validation error; a second failure is INVALID (``LLM_INVALID_OUTPUT``). Provider
   failures are INVALID too: budget exhausted, circuit open (``LLM_UNAVAILABLE``) or any other error.
2. ``direction == NONE`` or ``setup_tag == "none"`` → HOLD (``ANALYST_HOLD``).
3. The proposal must take one of the DETECTED candidates, tag and direction both; otherwise HOLD
   (``SETUP_MISMATCH``): hallucinated or reversed setups never reach the risk engine.
4. An invalidation on the wrong side of the entry price is dropped (the risk engine then uses its default ATR
   stop) and noted; so is a target on the wrong side.
5. The LLM never supplies volume: the schema has no such field and forbids extra keys.

Each call's parsed result and verdict are recorded on its ``llm_calls`` row through ``record_parse``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

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
from aifund.domain.decision import FeatureSnapshot, SetupCandidate, TradeProposal
from aifund.domain.enums import Direction, ReasonCode
from aifund.domain.market import Bar, Position, Tick
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
from aifund.strategies.base import TfRoles

RecordParse = Callable[[str, dict[str, Any] | None, bool, str | None], Awaitable[None]]
PROMPT = "analyst"


class Verdict(StrEnum):
    PROPOSAL = "PROPOSAL"
    HOLD = "HOLD"
    INVALID = "INVALID"


@dataclass(frozen=True)
class AnalystInput:
    decision_id: str
    symbol: str
    snapshot: FeatureSnapshot
    roles: TfRoles
    candidates: list[SetupCandidate]
    trigger_bars: list[Bar]
    trigger_atr: Decimal
    tick: Tick
    spread_points: int
    position: Position | None
    portfolio: str
    lessons: list[str] = field(default_factory=list)


@dataclass
class AnalystResult:
    verdict: Verdict
    proposal: TradeProposal | None = None
    reason: ReasonCode | None = None
    detail: str = ""
    raw: dict[str, Any] | None = None  # what the model answered (the proposal before sanitising)
    model: str | None = None
    cost_usd: Decimal = Decimal(0)
    call_ids: list[str] = field(default_factory=list)
    prompt_version: str = ""
    notes: list[str] = field(default_factory=list)


class Analyst:
    def __init__(
        self,
        llm: LLMPort,
        cfg: LLMConfig,
        *,
        playbooks: Path,
        prompts: PromptLibrary | None = None,
        record_parse: RecordParse | None = None,
        version: int = 1,
        require_setup: bool = True,
    ) -> None:
        self._llm = llm
        self._cfg = cfg
        self._playbooks = playbooks
        self._prompts = prompts or PromptLibrary()
        self._record_parse = record_parse
        self._version = version
        self._require_setup = require_setup

    # ------------------------------------------------------------------ prompt

    def context(self, inp: AnalystInput) -> dict[str, Any]:
        playbook_ids = sorted({c.playbook_id for c in inp.candidates})
        return dict(
            symbol=inp.symbol,
            trigger_tf=inp.roles.trigger.value,
            setup_tf=inp.roles.setup.value,
            context_tfs=",".join(tf.value for tf in inp.roles.context),
            bar_closed=f"{inp.snapshot.bar_time + _tf_length(inp):%Y-%m-%d %H:%M}",
            bid=fmt(inp.tick.bid),
            ask=fmt(inp.tick.ask),
            spread_points=inp.spread_points,
            feature_table=feature_table(inp.snapshot),
            atr=fmt(inp.trigger_atr),
            candles=atr_candles(inp.trigger_bars, inp.trigger_atr),
            candidates=candidate_lines(inp.candidates),
            playbooks=[load_playbook(self._playbooks, pid) for pid in playbook_ids],
            lessons=inp.lessons,
            position=position_line(inp.position),
            portfolio=inp.portfolio,
        )

    # ------------------------------------------------------------------ the call

    async def analyse(self, inp: AnalystInput) -> AnalystResult:
        rendered = self._prompts.render(PROMPT, self._version, self.context(inp))
        result = AnalystResult(Verdict.INVALID, prompt_version=f"{PROMPT}_v{self._version}")
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
                return self._invalid(result, ReasonCode.LLM_BUDGET_EXHAUSTED, str(exc))
            except LLMCircuitOpen as exc:
                return self._invalid(result, ReasonCode.LLM_UNAVAILABLE, str(exc))
            except LLMError as exc:
                return self._invalid(result, ReasonCode.LLM_ERROR, str(exc))
            self._account(result, response)
            try:
                proposal = TradeProposal.model_validate_json(response.text)
            except ValidationError as exc:
                problem = _errors(exc)
                await self._record(response, None, False, f"schema: {problem}")
                messages.append(LLMMessage(role="assistant", content=response.text))
                continue
            await self._record(response, proposal.model_dump(mode="json"), True, None)
            result.raw = proposal.model_dump(mode="json")
            return self._judge(result, proposal, inp)
        return self._invalid(result, ReasonCode.LLM_INVALID_OUTPUT, problem)

    def _request(self, inp: AnalystInput, messages: list[LLMMessage]) -> LLMRequest:
        return LLMRequest(
            agent=PROMPT,
            model=self._cfg.analyst_model,
            messages=messages,
            temperature=self._cfg.temperature,
            timeout_s=self._cfg.timeout_s,
            prompt_template=PROMPT,
            prompt_version=str(self._version),
            decision_id=inp.decision_id,
        )

    # ------------------------------------------------------------------ verdicts

    def _judge(self, result: AnalystResult, p: TradeProposal, inp: AnalystInput) -> AnalystResult:
        if p.direction is Direction.NONE or (p.setup_tag == "none" and self._require_setup):
            result.verdict, result.proposal, result.reason = Verdict.HOLD, p, ReasonCode.ANALYST_HOLD
            result.detail = p.thesis
            return result
        taken = [
            c
            for c in inp.candidates
            if c.setup_tag == p.setup_tag and c.direction_hint in (p.direction, Direction.NONE)
        ]
        if not taken:
            detected = ", ".join(f"{c.setup_tag} {c.direction_hint.value}" for c in inp.candidates) or "none"
            result.verdict, result.proposal, result.reason = Verdict.HOLD, p, ReasonCode.SETUP_MISMATCH
            result.detail = f"proposed {p.setup_tag} {p.direction.value}; detected: {detected}"
            return result
        entry = inp.tick.entry_price(p.direction.to_side())
        sign = 1 if p.direction is Direction.LONG else -1
        update: dict[str, Any] = {}
        if p.invalidation_price is not None and sign * (entry - p.invalidation_price) <= 0:
            update["invalidation_price"] = None
            result.notes.append(f"INVALIDATION_IGNORED: {p.invalidation_price} is not beyond entry {entry}")
        if p.target_price is not None and sign * (p.target_price - entry) <= 0:
            update["target_price"] = None
            result.notes.append(f"TARGET_IGNORED: {p.target_price} is not beyond entry {entry}")
        result.verdict = Verdict.PROPOSAL
        result.proposal = p.model_copy(update=update) if update else p
        result.detail = "; ".join(result.notes)
        return result

    def _invalid(self, result: AnalystResult, reason: ReasonCode, detail: str) -> AnalystResult:
        result.verdict, result.reason, result.detail = Verdict.INVALID, reason, detail[:500]
        return result

    def _account(self, result: AnalystResult, response: LLMResponse) -> None:
        result.model = response.model
        result.cost_usd += response.cost_usd or Decimal(0)
        if response.call_id is not None:
            result.call_ids.append(response.call_id)

    async def _record(
        self, response: LLMResponse, parsed: dict[str, Any] | None, valid: bool, error: str | None
    ) -> None:
        if self._record_parse is not None and response.call_id is not None:
            await self._record_parse(response.call_id, parsed, valid, error)


def _errors(exc: ValidationError) -> str:
    parts = [f"{'.'.join(str(x) for x in e['loc']) or 'reply'}: {e['msg']}" for e in exc.errors()[:6]]
    return "; ".join(parts)[:500]


def _tf_length(inp: AnalystInput) -> timedelta:
    return timedelta(minutes=inp.roles.trigger.minutes)
