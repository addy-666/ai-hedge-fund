"""Research loop (roadmap R.6, docs/09 §6): hypotheses in, evidence out — the LLM never judges its own ideas.

For a batch of entry hypotheses (from the LLM researcher, the operator, or a playbook):

1. each is studied over the PRE-holdout window and judged by an anchored walk-forward (a grid of one: a DSL
   hypothesis has no free parameters left), and the walk-forward trial is appended to the ledger;
2. only then Benjamini-Hochberg runs over the WHOLE ledger (every hypothesis ever tested, this batch too);
3. a hypothesis that passes the walk-forward part of gate E1 (incl. BH) gets its ONE holdout evaluation;
4. one that also passes the holdout part is RESEARCH-VALIDATED: an evidence record (``config/evidence``,
   gate E1_PASSED) and a DRAFT playbook card for the operator are written. Orders still need E2 (forward).

The brief for the LLM comes from the ledger WITHOUT holdout trials and from descriptive tables of signals
that entered before the holdout starts; both restrictions are enforced here and again by the agent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import TypeAdapter

from aifund.agents.prompting import PlaybookCard
from aifund.agents.researcher import LedgerLine, ResearchBrief, Researcher, ResearcherResult, Table
from aifund.config.evidence import EvidenceGate, EvidenceRecord, profile_key, write_evidence
from aifund.config.trading_config import ResearchConfig
from aifund.market.conditions import Condition, describe
from aifund.research.describe import TABLES, breakdown
from aifund.research.gates import GateCheck, e1_holdout_checks, e1_walk_forward_checks, passed
from aifund.research.history import History
from aifund.research.ledger import Ledger, Origin, Split, Trial, digest
from aifund.research.signals import SignalOutcome, StudySpec, run_study
from aifund.research.stats import Summary, summarize
from aifund.research.walkforward import WalkForwardResult, walk_forward
from aifund.strategies.base import TfRoles
from aifund.strategies.dsl_detector import DslDetector, EntryHypothesis

MAX_LEDGER_LINES = 60


class Evaluator(Protocol):
    """Studies one hypothesis over [start, end) and returns its signal outcomes (``research/signals.py``)."""

    symbols: list[str]
    roles: TfRoles

    def outcomes(
        self, hypothesis: EntryHypothesis, start: datetime, end: datetime
    ) -> list[SignalOutcome]: ...


@dataclass(frozen=True)
class Window:
    start: datetime
    holdout_start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not self.start < self.holdout_start < self.end:
            raise ValueError("need start < holdout_start < end")


@dataclass
class Evaluation:
    hypothesis: EntryHypothesis
    origin: Origin
    hypothesis_id: str
    walk_forward: WalkForwardResult
    trial: Trial
    checks: list[GateCheck] = field(default_factory=list)
    holdout: Summary | None = None
    holdout_checks: list[GateCheck] = field(default_factory=list)
    validated: bool = False
    note: str = ""
    evidence: Path | None = None
    card: Path | None = None


@dataclass
class RunReport:
    proposal: ResearcherResult
    evaluations: list[Evaluation]


def describe_hypothesis(doc: Mapping[str, Any]) -> str:
    """A ledger document as one line for the researcher's prompt (never the raw JSON)."""
    if doc.get("kind") == "dsl":
        b = doc["behaviour"]
        parts = [str(b["setup_tag"])]
        for side in ("long", "short"):
            if b.get(side) is not None:
                parts.append(f"{side.upper()} {describe(_condition(b[side]))}")
        inv, tgt = b["invalidation"], b.get("target")
        parts.append(
            f"stop {inv['k']:g}xATR({inv['tf']})" + (f" target {tgt['rr']:g}R" if tgt else " no target")
        )
        return " | ".join(parts)
    if "family" in doc:
        grid = len(doc.get("grid", {}))
        return f"{doc['family']} v{doc.get('version', '?')} grid of {grid} (walk-forward choice)"
    if "detector" in doc:
        params = ", ".join(f"{k}={v}" for k, v in sorted(doc.get("params", {}).items()))
        return f"{doc['detector']} v{doc.get('version', '?')} ({params})"
    return digest(doc)


def _condition(data: Mapping[str, Any]) -> Condition:
    return _CONDITION.validate_python(data)


_CONDITION: TypeAdapter[Condition] = TypeAdapter(Condition)


class HistoryEvaluator:
    """The real evaluator: the signal study over exported history, one spec per symbol (same profile)."""

    def __init__(self, history: History, specs: Sequence[StudySpec]) -> None:
        if not specs or len({s.roles for s in specs}) != 1:
            raise ValueError("one or more study specs, all with the same profile")
        self.history = history
        self.specs = list(specs)
        self.symbols = [s.symbol.broker for s in specs]
        self.roles = specs[0].roles

    def outcomes(self, hypothesis: EntryHypothesis, start: datetime, end: datetime) -> list[SignalOutcome]:
        out: list[SignalOutcome] = []
        for spec in self.specs:
            out += run_study(self.history, spec, [DslDetector(hypothesis, spec.roles)], start, end).outcomes
        return sorted(out, key=lambda o: o.entry_time)


class ResearchLoop:
    def __init__(
        self,
        evaluator: Evaluator,
        ledger: Ledger,
        cfg: ResearchConfig,
        window: Window,
        *,
        fingerprint: str,
        evidence_dir: Path,
        drafts_dir: Path,
        now: Callable[[], datetime],
        seed: int = 0,
    ) -> None:
        self.evaluator = evaluator
        self.ledger = ledger
        self.cfg = cfg
        self.window = window
        self.fingerprint = fingerprint
        self.evidence_dir = evidence_dir
        self.drafts_dir = drafts_dir
        self._now = now
        self._seed = seed

    # ------------------------------------------------------------------ what the LLM may see

    def document(self, h: EntryHypothesis) -> dict[str, Any]:
        """The ledger identity of a hypothesis: its behaviour on these symbols with this profile."""
        roles = self.evaluator.roles
        return {
            "kind": "dsl",
            "behaviour": h.behaviour(),
            "symbols": sorted(self.evaluator.symbols),
            "profile": profile_key(roles.trigger, roles.setup, roles.context),
        }

    def ledger_lines(self) -> list[LedgerLine]:
        visible = [t for t in self.ledger.trials() if t.split is not Split.HOLDOUT]
        visible.sort(key=lambda t: (t.split is not Split.WALK_FORWARD, t.created_at))  # walk-forward first
        return [
            LedgerLine(
                hypothesis=describe_hypothesis(t.hypothesis),
                split=t.split.value,
                n=t.n,
                mean=t.mean,
                ci_low=t.ci_low,
                ci_high=t.ci_high,
                p_value=t.p_value,
            )
            for t in visible[:MAX_LEDGER_LINES]
        ]

    def tables(self, probe: Sequence[SignalOutcome]) -> list[Table]:
        before = [o for o in probe if o.entry_time < self.window.holdout_start]
        if len(before) != len(probe):
            raise ValueError("descriptive tables were given signals from the holdout window")
        if not before:
            return []
        return [Table(title, breakdown(before, key)) for title, key in TABLES]

    def brief(
        self, run_id: str, playbooks: list[PlaybookCard], probe: Sequence[SignalOutcome]
    ) -> ResearchBrief:
        return ResearchBrief(
            run_id=run_id,
            roles=self.evaluator.roles,
            symbols=list(self.evaluator.symbols),
            playbooks=playbooks,
            ledger=self.ledger_lines(),
            tables=self.tables(probe),
        )

    # ------------------------------------------------------------------ the loop

    async def run(
        self,
        researcher: Researcher,
        *,
        run_id: str,
        playbooks: list[PlaybookCard],
        probe: Sequence[SignalOutcome] = (),
        spend_holdout: bool = True,
    ) -> RunReport:
        proposal = await researcher.propose(self.brief(run_id, playbooks, probe))
        evaluations = self.evaluate(proposal.hypotheses, Origin.LLM, spend_holdout=spend_holdout)
        return RunReport(proposal, evaluations)

    def evaluate(
        self, hypotheses: Sequence[EntryHypothesis], origin: Origin, *, spend_holdout: bool = True
    ) -> list[Evaluation]:
        w, cfg = self.window, self.cfg
        out: list[Evaluation] = []
        for h in hypotheses:  # 1. walk-forward + ledger for the whole batch first
            outcomes = self.evaluator.outcomes(h, w.start, w.holdout_start)
            wf = walk_forward(
                {"h": outcomes}, start=w.start, end=w.holdout_start, folds=cfg.folds,
                min_train_signals=cfg.min_train_signals, seed=self._seed,
            )  # fmt: skip
            doc = self.document(h)
            trial, _ = self.ledger.record(
                hypothesis=doc, symbols=self.evaluator.symbols, start=w.start, end=w.holdout_start,
                split=Split.WALK_FORWARD, origin=origin, data_fingerprint=self.fingerprint,
                summary=wf.summary, now=self._now(),
            )  # fmt: skip
            out.append(Evaluation(h, origin, trial.hypothesis_id, wf, trial))

        survivors = self.ledger.survivors(float(cfg.fdr_q))  # 2. BH over the WHOLE ledger
        for e in out:
            e.checks = e1_walk_forward_checks(
                e.walk_forward, survives_fdr=e.hypothesis_id in survivors, cfg=cfg
            )
            if not passed(e.checks):
                e.note = "failed the walk-forward part of E1; holdout kept clean"
            elif not spend_holdout:
                e.note = "passed the walk-forward part; holdout not requested"
            elif self.ledger.holdout_spent(self.document(e.hypothesis)):
                e.note = "holdout already spent on this hypothesis"
            else:
                self._holdout(e)  # 3. the one look
        return out

    def _holdout(self, e: Evaluation) -> None:
        w = self.window
        outcomes = self.evaluator.outcomes(e.hypothesis, w.holdout_start, w.end)
        e.holdout = summarize(
            [o.r_net for o in outcomes], times=[o.entry_time for o in outcomes], seed=self._seed
        )
        holdout_trial, _ = self.ledger.record(
            hypothesis=self.document(e.hypothesis), symbols=self.evaluator.symbols, start=w.holdout_start,
            end=w.end, split=Split.HOLDOUT, origin=e.origin, data_fingerprint=self.fingerprint,
            summary=e.holdout, now=self._now(),
        )  # fmt: skip
        e.holdout_checks = e1_holdout_checks(e.holdout, self.cfg)
        e.validated = passed(e.checks + e.holdout_checks)
        e.note = (
            "RESEARCH-VALIDATED (E1 passed): forward confirmation (E2) next"
            if e.validated
            else "failed the holdout"
        )
        if e.validated:  # 4. evidence + a DRAFT card for the operator
            self._write(e, [e.trial.trial_id, holdout_trial.trial_id])

    def _write(self, e: Evaluation, trials: list[str]) -> None:
        h, wf = e.hypothesis, e.walk_forward
        assert e.holdout is not None
        roles = self.evaluator.roles
        record = EvidenceRecord(
            detector=h.id,
            version=str(h.version),
            params_sha256=h.params_sha256(),
            gate=EvidenceGate.E1_PASSED,
            symbols=sorted(self.evaluator.symbols),
            profile=profile_key(roles.trigger, roles.setup, roles.context),
            walk_forward={
                "summary": wf.summary.render(), "n": wf.summary.n, "mean_r": wf.summary.mean,
                "ci90": [wf.summary.ci_low, wf.summary.ci_high], "p_value": wf.summary.p_value,
                "positive_fold_share": wf.positive_fold_share,
            },
            holdout={"summary": e.holdout.render(), "n": e.holdout.n, "mean_r": e.holdout.mean},
            trials=trials,
            hypothesis=h.model_dump(mode="json", by_alias=True),
            created_at=self._now(),
        )  # fmt: skip
        e.evidence = write_evidence(self.evidence_dir, record)
        card = {
            "id": h.id,
            "setup_tag": h.setup_tag,
            "version": h.version,
            "status": "DRAFT",  # an operator approves it into config/playbooks; orders also need E2
            "origin": e.origin.value,
            "summary": h.mechanism,
            "mechanism": h.mechanism,
            "long_rules": [describe(h.long)] if h.long is not None else [],
            "short_rules": describe(h.short_condition) if h.short_condition is not None else "none",
            "stop": f"{h.invalidation.k:g} x ATR({h.invalidation.tf}) beyond the signal bar's close",
            "target": f"{h.target.rr:g} R" if h.target is not None else "none (time stop / trailing)",
            "hypothesis": h.model_dump(mode="json", by_alias=True),
            "evidence": record.filename,
            "validation_status": "research_validated",
        }
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        e.card = self.drafts_dir / f"{h.id}.yaml"
        e.card.write_text(yaml.safe_dump(card, sort_keys=False, allow_unicode=True), encoding="utf-8")
