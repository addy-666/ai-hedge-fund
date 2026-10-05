"""LLM researcher (roadmap R.6, docs/09 §6): proposes entry hypotheses in the DSL; never judges them.

Input (``ResearchBrief``): the entry-usable features of the profile with their mirrors, the playbook cards,
the trial ledger restricted to in-sample / walk-forward results, and descriptive in-sample tables. A holdout
row in the brief is a programming error (the agent refuses the brief): results on the holdout must never
reach the model, or the holdout stops being out of sample.

Output: at most ``max_hypotheses`` ``EntryHypothesis`` objects. Each proposed item is validated on its own
(strict schema, registry features, ranges, mirrors, profile timeframes), so one bad idea does not sink the
rest. If the reply is not a JSON object with a ``hypotheses`` list, or any item is invalid, ONE repair call
quotes the problems; whatever is still invalid is dropped with its reason (``rejected``). The engine assigns
ids from the behaviour hash (``H-<sha>``), so the same idea proposed twice is the same hypothesis.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from aifund.agents.prompting import PlaybookCard, PromptLibrary
from aifund.config.trading_config import LLMConfig
from aifund.market import feature_registry as reg
from aifund.market.feature_registry import CATEGORY_MIRROR, FeatureSource, FeatureSpec, MirrorKind, tf_prefix
from aifund.ports.llm import LLMError, LLMInvalidOutput, LLMMessage, LLMPort, LLMRequest, LLMResponse
from aifund.strategies.base import TfRoles
from aifund.strategies.dsl_detector import MAX_PREDICATES, DslDetector, EntryHypothesis, entry_feature

PROMPT = "researcher"
HOLDOUT = "holdout"
RecordParse = Callable[[str, dict[str, Any] | None, bool, str | None], Awaitable[None]]


@dataclass(frozen=True)
class LedgerLine:
    hypothesis: str  # a compact description (conditions and levels), not the raw ledger document
    split: str  # in_sample | walk_forward (never holdout)
    n: int
    mean: float
    ci_low: float | None
    ci_high: float | None
    p_value: float


@dataclass(frozen=True)
class Table:
    title: str
    rows: list[str]


@dataclass(frozen=True)
class ResearchBrief:
    run_id: str
    roles: TfRoles
    symbols: list[str]
    playbooks: list[PlaybookCard]
    ledger: list[LedgerLine]
    tables: list[Table] = field(default_factory=list)
    instruments: list[str] = field(default_factory=list)  # cross-asset slugs with data (roadmap 10.3)


@dataclass(frozen=True)
class Rejected:
    round: int  # 1: first reply, 2: the repair
    item: Any
    reason: str


@dataclass
class ResearcherResult:
    hypotheses: list[EntryHypothesis] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    error: str | None = None  # provider failure: nothing proposed
    model: str | None = None
    cost_usd: Decimal = Decimal(0)
    call_ids: list[str] = field(default_factory=list)
    prompt_version: str = ""


def _mirror_text(spec: FeatureSpec, prefix: str = "") -> str:
    m = spec.mirror
    if m is None:
        return "none"
    partner = m.partner.removeprefix(prefix) if m.partner else None
    if m.kind is MirrorKind.CATEGORY:
        cats = spec.categories or ()
        kind = ", ".join(f"{a}<->{b}" for a, b in CATEGORY_MIRROR.items() if a < b and a in cats)
    else:
        kind = {
            MirrorKind.SAME: "same",
            MirrorKind.NEGATE: "-value",
            MirrorKind.COMPLEMENT: "100 - value",
            MirrorKind.NOT: "not",
        }[m.kind]
    return f"{partner} ({kind})" if partner else kind


def _line(spec: FeatureSpec, name: str, prefix: str = "") -> dict[str, str]:
    dtype = spec.dtype.value if spec.categories is None else "|".join(spec.categories)
    description = spec.description.split(": ", 1)[-1] if prefix else spec.description
    return dict(
        name=name, dtype=dtype, unit=spec.unit, description=description, mirror=_mirror_text(spec, prefix)
    )


def feature_lines(roles: TfRoles) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(per-timeframe base features, context features) usable in an entry hypothesis for this profile."""
    prefix = tf_prefix(roles.trigger) + "."
    tf_rows = [
        _line(s, s.name.removeprefix(prefix), prefix)
        for s in reg.tf_feature_specs(roles.trigger)
        if entry_feature(s) is None
    ]
    profile = {roles.trigger, roles.setup, *roles.context}
    ctx_rows = [
        _line(s, s.name)
        for s in reg.CTX_FEATURES
        if s.source is FeatureSource.CONTEXT
        and entry_feature(s) is None
        and reg.NEEDS_TIMEFRAME.get(s.name, roles.trigger) in profile
    ]
    return tf_rows, ctx_rows


def xa_feature_lines() -> list[dict[str, str]]:
    """The cross-asset features as ``xa.<instrument>.<base>`` rows (researcher_v2, roadmap 10.3)."""
    rows = []
    for base, *_ in reg.XA_FEATURES:
        spec = reg.xa_spec("instrument", base)
        rows.append(_line(spec, f"xa.<instrument>.{base}", prefix="xa."))
    return rows


def _num(x: float | None) -> str:
    return "na" if x is None else f"{x:+.3f}"


class Researcher:
    def __init__(
        self,
        llm: LLMPort,
        cfg: LLMConfig,
        *,
        max_hypotheses: int,
        prompts: PromptLibrary | None = None,
        record_parse: RecordParse | None = None,
        version: int = 1,
    ) -> None:
        self._llm = llm
        self._cfg = cfg
        self._max = max_hypotheses
        self._prompts = prompts or PromptLibrary()
        self._record_parse = record_parse
        self._version = version

    def context(self, brief: ResearchBrief) -> dict[str, Any]:
        leaked = [line for line in brief.ledger if line.split == HOLDOUT]
        if leaked:
            raise ValueError(
                f"{len(leaked)} holdout result(s) in a research brief: holdouts never reach the LLM"
            )
        roles = brief.roles
        tf_rows, ctx_rows = feature_lines(roles)
        timeframes = [roles.trigger, roles.setup, *roles.context]
        return dict(
            max_predicates=MAX_PREDICATES,
            max_hypotheses=self._max,
            trigger_tf=roles.trigger.value,
            setup_tf=roles.setup.value,
            context_tfs=",".join(tf.value for tf in roles.context),
            symbols=", ".join(brief.symbols),
            prefixes=", ".join(f"{tf_prefix(tf)}." for tf in timeframes),
            tf_features=tf_rows,
            ctx_features=ctx_rows,
            xa_features=xa_feature_lines() if brief.instruments else [],
            instruments=", ".join(brief.instruments),
            playbooks=brief.playbooks,
            ledger=[
                dict(
                    hypothesis=line.hypothesis,
                    split=line.split,
                    n=line.n,
                    mean=_num(line.mean),
                    ci=f"[{_num(line.ci_low)}, {_num(line.ci_high)}]",
                    p=f"{line.p_value:.3f}",
                )
                for line in brief.ledger
            ],
            tables=brief.tables,
        )

    async def propose(self, brief: ResearchBrief) -> ResearcherResult:
        rendered = self._prompts.render(PROMPT, self._version, self.context(brief))
        result = ResearcherResult(prompt_version=f"{PROMPT}_v{self._version}")
        messages = list(rendered.messages)
        seen: set[str] = set()
        for attempt in (1, 2):
            try:
                response = await self._llm.complete(self._request(brief, messages))
            except LLMInvalidOutput as exc:
                problems = [f"the reply was not a single JSON object ({exc})"]
            except LLMError as exc:
                result.error = str(exc)[:500]
                return result
            else:
                self._account(result, response)
                problems = self._collect(response.text, attempt, brief, result, seen)
                await self._record(response, attempt, problems)
                messages.append(LLMMessage(role="assistant", content=response.text))
            if not problems or len(result.hypotheses) >= self._max:
                break
            if attempt == 1:
                messages.append(
                    LLMMessage(
                        role="user",
                        content="Some of your reply was rejected:\n- "
                        + "\n- ".join(problems)
                        + '\nReply again with ONE JSON object {"hypotheses": [...]} containing corrected '
                        "versions of ONLY the rejected hypotheses (or an empty list to drop them).",
                    )
                )
        return result

    def _collect(
        self, text: str, attempt: int, brief: ResearchBrief, result: ResearcherResult, seen: set[str]
    ) -> list[str]:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("hypotheses"), list) or len(data) != 1:
            reason = 'expected exactly {"hypotheses": [...]}'
            result.rejected.append(Rejected(attempt, text[:2000], reason))
            return [reason]
        problems = []
        for i, item in enumerate(data["hypotheses"], start=1):
            if len(result.hypotheses) >= self._max:
                result.rejected.append(Rejected(attempt, item, f"over the limit of {self._max} hypotheses"))
                continue
            parsed = _parse(item, brief.roles, brief.instruments)
            if isinstance(parsed, str):
                result.rejected.append(Rejected(attempt, item, parsed))
                problems.append(f"hypothesis {i}: {parsed}")
                continue
            sha = parsed.params_sha256()
            if sha in seen:  # proposed twice (or re-sent with the repair): one hypothesis, counted once
                continue
            seen.add(sha)
            result.hypotheses.append(parsed)
        return problems

    def _request(self, brief: ResearchBrief, messages: list[LLMMessage]) -> LLMRequest:
        return LLMRequest(
            agent=PROMPT,
            model=self._cfg.auditor_model,  # offline, may be the slower reasoning model
            messages=messages,
            temperature=self._cfg.temperature,
            timeout_s=self._cfg.auditor_timeout_s,
            prompt_template=PROMPT,
            prompt_version=str(self._version),
            audit_run_id=brief.run_id,
        )

    def _account(self, result: ResearcherResult, response: LLMResponse) -> None:
        result.model = response.model
        result.cost_usd += response.cost_usd or Decimal(0)
        if response.call_id is not None:
            result.call_ids.append(response.call_id)

    async def _record(self, response: LLMResponse, attempt: int, problems: Sequence[str]) -> None:
        if self._record_parse is not None and response.call_id is not None:
            error = "; ".join(problems)[:500] if problems else None
            await self._record_parse(response.call_id, None, not problems, error)


def _parse(item: Any, roles: TfRoles, instruments: Sequence[str] = ()) -> EntryHypothesis | str:
    if not isinstance(item, dict):
        return "a hypothesis must be a JSON object"
    body = {k: v for k, v in item.items() if k not in ("id", "version")} | {"id": "H-new", "version": 1}
    try:
        h = EntryHypothesis.model_validate(body)
        DslDetector(h, roles)  # timeframes must belong to the profile
    except ValidationError as exc:
        return "; ".join(
            f"{'.'.join(str(x) for x in e['loc']) or 'hypothesis'}: {e['msg']}" for e in exc.errors()[:4]
        )[:500]
    except ValueError as exc:
        return str(exc)[:500]
    unknown = sorted(h.instruments() - set(instruments))
    if unknown:  # a name the registry accepts, but no data: it could never fire and would only cost FDR
        have = ", ".join(instruments) or "none"
        return f"no cross-asset data for {', '.join(unknown)} (instruments: {have})"
    return h.model_copy(update={"id": f"H-{h.params_sha256()[:10]}"})
