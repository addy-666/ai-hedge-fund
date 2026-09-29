"""Trade reviewer (roadmap 7.3, docs/04 §2): one LLM review per closed trade, winners included.

Input: the entry snapshot, the thesis and key risks (when the analyst decided), the lessons shown and rules
matched at entry, the stop/target plan, the outcome (R, close reason, MAE/MFE, bars held), the R path at
25/50/75/100% of the holding time and the trigger candles around the trade, in R from the entry price.

Output: a strict ``TradeReview`` (tags from the fixed taxonomy, thesis verdict, execution quality, lesson).
One repair call quotes a schema failure; then the review fails (the trade is marked for the operator, never
reviewed by guesswork). Reviews explain; the miner and the validator decide what is learned.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from aifund.agents.prompting import PromptLibrary, feature_table, fmt
from aifund.config.trading_config import LLMConfig
from aifund.domain.decision import FeatureSnapshot, TradeReview
from aifund.domain.enums import Direction
from aifund.domain.market import Bar
from aifund.ports.llm import LLMError, LLMInvalidOutput, LLMMessage, LLMPort, LLMRequest, LLMResponse

PROMPT = "reviewer"
CANDLES_BEFORE = 10  # context bars before the entry
MAX_CANDLES = 60
RecordParse = Callable[[str, dict[str, Any] | None, bool, str | None], Awaitable[None]]


@dataclass(frozen=True)
class ReviewInput:
    trade_id: str
    symbol: str
    direction: Direction
    setup_tag: str | None
    trigger_tf: str
    opened: datetime
    closed: datetime
    entry: Decimal
    stop: Decimal | None
    target: Decimal | None
    atr: Decimal | None  # trigger ATR at entry (the unit when there is no stop)
    r: Decimal
    close_reason: str
    mae_r: Decimal | None
    mfe_r: Decimal | None
    bars_held: int | None
    minutes: int | None
    snapshot: FeatureSnapshot | None  # None: an orphan trade without an engine decision
    candles: Sequence[Bar]  # trigger bars from before the entry to the exit
    thesis: str | None = None
    key_risks: Sequence[str] = ()
    lessons: Sequence[str] = ()
    rules: Sequence[str] = ()


@dataclass
class ReviewResult:
    review: TradeReview | None = None
    error: str | None = None
    model: str | None = None
    cost_usd: Decimal = Decimal(0)
    call_ids: list[str] = field(default_factory=list)
    prompt_version: str = ""


def _unit(inp: ReviewInput) -> Decimal | None:
    """1R in price: the initial stop distance, else the ATR."""
    if inp.stop is not None and inp.stop != inp.entry:
        return abs(inp.entry - inp.stop)
    return inp.atr if inp.atr is not None and inp.atr > 0 else None


def r_candles(inp: ReviewInput) -> list[dict[str, str]]:
    unit = _unit(inp)
    if unit is None:
        return []
    sign = 1 if inp.direction is Direction.LONG else -1
    before = [b for b in inp.candles if b.time < inp.opened][-CANDLES_BEFORE:]
    during = [b for b in inp.candles if inp.opened <= b.time <= inp.closed]
    rows = []
    for b in [*before, *during][:MAX_CANDLES]:

        def r(x: Decimal) -> str:
            return f"{sign * (x - inp.entry) / unit:+.2f}"

        high, low = (b.high, b.low) if sign > 0 else (b.low, b.high)  # "h" = best for the trade
        rows.append(
            dict(
                mark=">" if b is (during[0] if during else None) else " ",
                time=f"{b.time:%m-%d %H:%M}",
                o=r(b.open),
                h=r(high),
                l=r(low),
                c=r(b.close),
            )
        )
    return rows


def r_path(inp: ReviewInput) -> str:
    """R of the close at 25/50/75/100% of the bars held (``na`` without bars or a unit)."""
    unit = _unit(inp)
    during = [b for b in inp.candles if inp.opened <= b.time <= inp.closed]
    if unit is None or not during:
        return "na"
    sign = 1 if inp.direction is Direction.LONG else -1
    points = []
    for q in (0.25, 0.5, 0.75, 1.0):
        b = during[max(0, min(len(during) - 1, round(q * len(during)) - 1))]
        points.append(f"{sign * (b.close - inp.entry) / unit:+.2f}")
    return " / ".join(points)


class Reviewer:
    def __init__(
        self,
        llm: LLMPort,
        cfg: LLMConfig,
        *,
        prompts: PromptLibrary | None = None,
        record_parse: RecordParse | None = None,
        version: int = 1,
    ) -> None:
        self._llm = llm
        self._cfg = cfg
        self._prompts = prompts or PromptLibrary()
        self._record_parse = record_parse
        self._version = version

    def context(self, inp: ReviewInput) -> dict[str, Any]:
        stop_atr = "na"
        if inp.stop is not None and inp.atr:
            stop_atr = f"{abs(inp.entry - inp.stop) / inp.atr:.2f}"
        unit = _unit(inp)
        return dict(
            symbol=inp.symbol,
            direction=inp.direction.value,
            setup_tag=inp.setup_tag or "unknown",
            trigger_tf=inp.trigger_tf,
            opened=f"{inp.opened:%Y-%m-%d %H:%M}",
            closed=f"{inp.closed:%Y-%m-%d %H:%M}",
            entry=fmt(inp.entry),
            stop=fmt(inp.stop),
            stop_atr=stop_atr,
            target=fmt(inp.target),
            risk=fmt(unit) + ("" if inp.stop is not None else " (no stop: 1 ATR)"),
            r=f"{inp.r:+.2f}",
            close_reason=inp.close_reason,
            mae_r=fmt(inp.mae_r),
            mfe_r=fmt(inp.mfe_r),
            bars_held=inp.bars_held if inp.bars_held is not None else "na",
            minutes=inp.minutes if inp.minutes is not None else "na",
            path=r_path(inp),
            thesis=inp.thesis or "none recorded (deterministic setup, no analyst)",
            key_risks="; ".join(inp.key_risks) or "none",
            lessons="; ".join(inp.lessons) or "none",
            rules=", ".join(inp.rules) or "none",
            feature_table=feature_table(inp.snapshot) if inp.snapshot is not None else "(none: orphan trade)",
            candles=r_candles(inp),
        )

    async def review(self, inp: ReviewInput) -> ReviewResult:
        rendered = self._prompts.render(PROMPT, self._version, self.context(inp))
        result = ReviewResult(prompt_version=f"{PROMPT}_v{self._version}")
        messages = list(rendered.messages)
        problem = ""
        for attempt in (1, 2):
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
            except LLMError as exc:
                result.error = str(exc)[:500]
                return result
            self._account(result, response)
            try:
                review = TradeReview.model_validate_json(response.text)
            except ValidationError as exc:
                problem = "; ".join(
                    f"{'.'.join(str(x) for x in e['loc']) or 'reply'}: {e['msg']}" for e in exc.errors()[:6]
                )[:500]
                await self._record(response, None, False, f"schema: {problem}")
                messages.append(LLMMessage(role="assistant", content=response.text))
                continue
            await self._record(response, review.model_dump(mode="json"), True, None)
            result.review = review
            return result
        result.error = f"invalid output: {problem}"
        return result

    def _request(self, inp: ReviewInput, messages: list[LLMMessage]) -> LLMRequest:
        return LLMRequest(
            agent=PROMPT,
            model=self._cfg.analyst_model,  # a cheap per-trade call; the auditor gets the slower model
            messages=messages,
            temperature=self._cfg.temperature,
            timeout_s=self._cfg.timeout_s,
            prompt_template=PROMPT,
            prompt_version=str(self._version),
            trade_id=inp.trade_id,
        )

    def _account(self, result: ReviewResult, response: LLMResponse) -> None:
        result.model = response.model
        result.cost_usd += response.cost_usd or Decimal(0)
        if response.call_id is not None:
            result.call_ids.append(response.call_id)

    async def _record(
        self, response: LLMResponse, parsed: dict[str, Any] | None, valid: bool, error: str | None
    ) -> None:
        if self._record_parse is not None and response.call_id is not None:
            await self._record_parse(response.call_id, parsed, valid, error)
