"""Auditor agent (roadmap 7.5, docs/04 §4): turns mined loss clusters into candidate rules; never judges them.

Input (``AuditBrief``): the rule-usable features seen in the window with their observed ranges, the miner's
surviving and weak clusters, the descriptive tables, reviewer-tag frequencies, a few trade narratives and the
current rules. Output: at most 5 candidates (DSL + mechanism + cited clusters), retire suggestions, a lessons
report and strategy-level findings.

Every candidate is checked on its own — the DSL against the registry, the scope against the known symbols and
setups, thresholds against the observed ranges, and at least one citation of a SURVIVING cluster (a weak or
unknown id is not evidence). If anything was rejected, ONE repair call quotes the problems; what is still
invalid is dropped with its reason. The action is advisory: the validator sets the final one.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aifund.agents.prompting import PromptLibrary
from aifund.config.trading_config import LLMConfig
from aifund.market import feature_registry as reg
from aifund.ports.llm import LLMError, LLMInvalidOutput, LLMMessage, LLMPort, LLMRequest, LLMResponse
from aifund.rules import dsl
from aifund.rules.miner import MinerResult

PROMPT = "auditor"
MAX_CANDIDATES = 5
PLACEHOLDER_ID = "R-0000"  # the lifecycle assigns the real id
RecordParse = Callable[[str, dict[str, Any] | None, bool, str | None], Awaitable[None]]


class _Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: dict[str, Any] = Field(default_factory=dict)
    conditions: dict[str, Any]
    action: dict[str, Any]
    hypothesis: str = Field(min_length=10, max_length=600)
    cited_clusters: list[str] = Field(min_length=1, max_length=5)


class RetireSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    reasoning: str = Field(max_length=1000)


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_rules: list[Any] = Field(default_factory=list)  # each item is judged on its own
    retire_suggestions: list[RetireSuggestion] = Field(default_factory=list)
    lessons_markdown: str = Field(default="", max_length=4000)
    strategy_level_findings: list[str] = Field(default_factory=list, max_length=10)


@dataclass(frozen=True)
class FeatureLine:
    name: str
    lo: float | None
    hi: float | None


@dataclass(frozen=True)
class RuleLine:
    id: str
    status: str
    text: str
    evidence: str


@dataclass(frozen=True)
class AuditBrief:
    run_id: str
    miner: MinerResult
    features: Sequence[FeatureLine]
    symbols: Sequence[str]
    setup_tags: Sequence[str]
    window: str
    n_virtual: int
    narratives: Sequence[str] = ()
    rules: Sequence[RuleLine] = ()
    max_conditions: int = 3

    @property
    def observed(self) -> dict[str, tuple[float, float]]:
        return {f.name: (f.lo, f.hi) for f in self.features if f.lo is not None and f.hi is not None}


@dataclass(frozen=True)
class Candidate:
    rule: dsl.Rule  # rule_id is the placeholder until the lifecycle registers it
    cited: tuple[str, ...]


@dataclass(frozen=True)
class Rejected:
    round: int
    item: Any
    reason: str


@dataclass
class AuditorResult:
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    retire_suggestions: list[RetireSuggestion] = field(default_factory=list)
    lessons_markdown: str = ""
    findings: list[str] = field(default_factory=list)
    error: str | None = None
    model: str | None = None
    cost_usd: Decimal = Decimal(0)
    call_ids: list[str] = field(default_factory=list)
    prompt_version: str = ""


def _num(x: float) -> str:
    return f"{x:+.3f}"


def _cluster_row(c: Mapping[str, Any]) -> dict[str, Any]:
    ci = c.get("ci_mean")
    return dict(
        id=c["id"], text=c["text"], n=c["n"], n_virtual=c["n_virtual"], win=f"{c['win_rate']:.0%}",
        mean=_num(c["mean_r"]), rest=_num(c["mean_r_rest"]), effect=_num(c["effect"]),
        ci="n/a" if ci is None else f"[{_num(ci[0])}, {_num(ci[1])}]", p=f"{c['p_value']:.4f}",
    )  # fmt: skip


class Auditor:
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

    def context(self, brief: AuditBrief) -> dict[str, Any]:
        m = brief.miner.to_json()
        features = []
        for f in brief.features:
            spec = reg.get(f.name)
            dtype = spec.dtype.value if spec.categories is None else "|".join(spec.categories)
            rng = "-" if f.lo is None or f.hi is None else f"{f.lo:g}..{f.hi:g}"
            features.append(
                dict(name=f.name, dtype=dtype, unit=spec.unit, description=spec.description, range=rng)
            )
        tables = [
            (name, [dict(key=r["key"], n=r["n"], win=f"{r['win_rate']:.0%}", mean=_num(r["mean_r"]),
                         total=f"{r['total_r']:+.2f}") for r in rows])
            for name, rows in m["tables"].items()
        ]  # fmt: skip
        return dict(
            max_candidates=MAX_CANDIDATES,
            max_conditions=brief.max_conditions,
            window=brief.window,
            n_samples=m["n_samples"],
            n_virtual=brief.n_virtual,
            n_discovery=m["n_discovery"],
            baseline=_num(m["baseline_mean_r"]),
            symbols=", ".join(brief.symbols) or "none",
            setup_tags=", ".join(brief.setup_tags) or "none",
            features=features,
            clusters=[_cluster_row(c) for c in m["clusters"]],
            weak=[_cluster_row(c) for c in m["weak"]],
            tables=tables,
            tags=sorted(m["tag_frequency"].items()),
            narratives=list(brief.narratives),
            rules=[dict(id=r.id, status=r.status, text=r.text, evidence=r.evidence) for r in brief.rules],
        )

    async def audit(self, brief: AuditBrief) -> AuditorResult:
        rendered = self._prompts.render(PROMPT, self._version, self.context(brief))
        result = AuditorResult(prompt_version=f"{PROMPT}_v{self._version}")
        messages = list(rendered.messages)
        surviving = {c.id for c in brief.miner.clusters}
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
                problems = self._collect(response.text, attempt, brief, surviving, result, seen)
                await self._record(response, problems)
                messages.append(LLMMessage(role="assistant", content=response.text))
            if not problems or len(result.candidates) >= MAX_CANDIDATES:
                break
            if attempt == 1:
                messages.append(
                    LLMMessage(
                        role="user",
                        content="Some of your reply was rejected:\n- "
                        + "\n- ".join(problems)
                        + "\nReply again with the SAME JSON object shape, containing corrected versions of "
                        "ONLY the rejected candidate rules (or an empty list to drop them).",
                    )
                )
        return result

    def _collect(
        self,
        text: str,
        attempt: int,
        brief: AuditBrief,
        surviving: set[str],
        result: AuditorResult,
        seen: set[str],
    ) -> list[str]:
        try:
            out = _Output.model_validate_json(text)
        except ValidationError as exc:
            reason = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:4])
            result.rejected.append(Rejected(attempt, text[:2000], reason))
            return [reason[:500]]
        if out.lessons_markdown:  # a repair reply may leave the lessons out: keep the first ones
            result.lessons_markdown = out.lessons_markdown
        known = {r.rule_id for r in result.retire_suggestions}
        result.retire_suggestions += [r for r in out.retire_suggestions if r.rule_id not in known]
        result.findings += [f for f in out.strategy_level_findings if f not in result.findings]
        problems = []
        for i, item in enumerate(out.candidate_rules, start=1):
            if len(result.candidates) >= MAX_CANDIDATES:
                result.rejected.append(
                    Rejected(attempt, item, f"over the limit of {MAX_CANDIDATES} candidates")
                )
                continue
            parsed = _parse(item, brief, surviving)
            if isinstance(parsed, str):
                result.rejected.append(Rejected(attempt, item, parsed))
                problems.append(f"candidate {i}: {parsed}")
                continue
            sha = dsl.dsl_sha256(parsed.rule)
            if sha not in seen:
                seen.add(sha)
                result.candidates.append(parsed)
        return problems

    def _request(self, brief: AuditBrief, messages: list[LLMMessage]) -> LLMRequest:
        return LLMRequest(
            agent=PROMPT,
            model=self._cfg.auditor_model,
            messages=messages,
            temperature=self._cfg.temperature,
            timeout_s=self._cfg.auditor_timeout_s,
            prompt_template=PROMPT,
            prompt_version=str(self._version),
            audit_run_id=brief.run_id,
        )

    def _account(self, result: AuditorResult, response: LLMResponse) -> None:
        result.model = response.model
        result.cost_usd += response.cost_usd or Decimal(0)
        if response.call_id is not None:
            result.call_ids.append(response.call_id)

    async def _record(self, response: LLMResponse, problems: Sequence[str]) -> None:
        if self._record_parse is not None and response.call_id is not None:
            error = "; ".join(problems)[:500] if problems else None
            parsed = None
            with contextlib.suppress(ValueError):
                parsed = json.loads(response.text)
            await self._record_parse(
                response.call_id, parsed if isinstance(parsed, dict) else None, not problems, error
            )


def _parse(item: Any, brief: AuditBrief, surviving: set[str]) -> Candidate | str:
    if not isinstance(item, dict):
        return "a candidate must be a JSON object"
    try:
        c = _Candidate.model_validate(item)
    except ValidationError as exc:
        return "; ".join(
            f"{'.'.join(str(x) for x in e['loc']) or 'candidate'}: {e['msg']}" for e in exc.errors()[:4]
        )[:500]
    cited = tuple(dict.fromkeys(c.cited_clusters))
    unknown = [x for x in cited if x not in surviving]
    if unknown:
        return (
            f"cited_clusters {unknown} are not surviving miner clusters: cite the evidence the rule rests on"
        )
    try:
        rule = dsl.parse(
            {
                "rule_id": PLACEHOLDER_ID,
                "scope": c.scope,
                "conditions": c.conditions,
                "action": c.action,
                "hypothesis": c.hypothesis,
                "cited_clusters": list(cited),
            }
        )
        dsl.validate(
            rule,
            max_conditions=brief.max_conditions,
            symbols=brief.symbols,
            setup_tags=brief.setup_tags,
            observed=brief.observed,
        )
    except dsl.RuleError as exc:
        return str(exc)[:500]
    return Candidate(rule, cited)
