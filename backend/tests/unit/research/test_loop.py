"""Research loop (roadmap R.6, docs/09 §6) on synthetic worlds with FakeLLM.

The synthetic evaluator runs the REAL ``DslDetector`` over seeded random feature snapshots, one every 2.4 h
for 400 days (300 before the holdout). Each signal wins +2R or loses -1R. In the PLANTED world a LONG with
RSI < 30 inside an H4 uptrend wins 55% of the time (+0.65R); every other signal wins 1/3 (0R). In the NOISE
world every signal wins 1/3. The researcher proposes the planted hypothesis and four SHORT-only noise ideas.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
import yaml

from aifund.adapters.llm.fake_llm import FakeLLM
from aifund.agents.researcher import Researcher
from aifund.config.evidence import EvidenceGate, load_evidence
from aifund.config.trading_config import LLMConfig, ResearchConfig
from aifund.domain.decision import FeatureSnapshot
from aifund.domain.enums import CloseReason, Direction, Timeframe, VirtualStatus
from aifund.ports.llm import LLMRequest
from aifund.research.describe import Probe
from aifund.research.ledger import Ledger, Origin, Split
from aifund.research.loop import HistoryEvaluator, ResearchLoop, Window, describe_hypothesis
from aifund.research.signals import SignalOutcome, run_study
from aifund.stats import summarize
from aifund.strategies.base import TfRoles
from aifund.strategies.dsl_detector import DslDetector, EntryHypothesis
from tests.unit.research import test_signals as sig

ROLES = TfRoles(trigger=Timeframe.M15, setup=Timeframe.H1, context=(Timeframe.H4,))
T0 = datetime(2025, 1, 6, tzinfo=UTC)
WINDOW = Window(start=T0, holdout_start=T0 + timedelta(days=300), end=T0 + timedelta(days=400))
CFG = ResearchConfig()
NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
SESSIONS = ("ASIA", "LONDON", "OVERLAP", "NY", "OFF")


class Synthetic:
    roles = ROLES

    def __init__(
        self, *, planted: bool, seed: int = 8, events: int = 4000, edge_until: datetime | None = None
    ) -> None:
        self.edge_until = edge_until
        rng = random.Random(seed)
        self.symbols = ["XAUUSD"]
        step = (WINDOW.end - WINDOW.start) / events
        self.planted = planted
        self.events = [
            (
                WINDOW.start + i * step,
                {
                    "m15.close": 100.0, "m15.atr14": 1.0, "h1.atr14": 2.0,
                    "m15.rsi14": rng.uniform(0, 100), "h4.ema50_above_ema200": rng.random() < 0.5,
                    "m15.adx14": rng.uniform(5, 60), "ctx.session": rng.choice(SESSIONS),
                    "ctx.htf_trend_score": rng.choice((-2, -1, 0, 1, 2)), "ctx.regime": "RANGE",
                },
                rng.random(),
            )
            for i in range(events)
        ]  # fmt: skip
        self.calls: list[tuple[str, datetime, datetime]] = []

    def win_rate(self, when: datetime, f: dict[str, Any], d: Direction) -> float:
        live = self.planted and (self.edge_until is None or when < self.edge_until)
        edge = live and d is Direction.LONG and f["m15.rsi14"] < 30 and f["h4.ema50_above_ema200"]
        return 0.55 if edge else 1 / 3

    def outcomes(self, h: EntryHypothesis, start: datetime, end: datetime) -> list[SignalOutcome]:
        self.calls.append((h.id, start, end))
        det, out = DslDetector(h, ROLES), []
        for when, features, u in self.events:
            if not start <= when < end:
                continue
            snap = FeatureSnapshot(
                symbol="XAUUSD", trigger_tf=Timeframe.M15, bar_time=when, feature_set_version=2,
                features=features, bars_ref={Timeframe.M15: when},
            )  # fmt: skip
            for c in det.detect(snap):
                out.append(
                    outcome(
                        when,
                        c.direction_hint,
                        D(2) if u < self.win_rate(when, features, c.direction_hint) else D(-1),
                        features,
                    )
                )
        return out


def outcome(when: datetime, d: Direction, r: D, features: dict[str, Any]) -> SignalOutcome:
    return SignalOutcome(
        symbol="XAUUSD", bar_time=when, entry_time=when + timedelta(minutes=15), direction=d,
        setup_tag="x_tag", detector="x@1", status=VirtualStatus.CLOSED,
        exit_reason=CloseReason.TP if r > 0 else CloseReason.SL, exit_time=when + timedelta(hours=1),
        entry_price=D(100), sl_distance=D(1), tp_distance=D(2), r_gross=r, r_net=r, mae_r=None, mfe_r=None,
        resolution=Timeframe.M1, features=features,
    )  # fmt: skip


def dsl(tag: str, long: Any = None, short: Any = None) -> dict[str, Any]:
    return {
        "setup_tag": tag, "long": long, "short": short,
        "invalidation": {"type": "atr", "tf": "trigger", "k": 1.0}, "target": {"type": "rr", "rr": 2.0},
        "mechanism": f"{tag}: an idea with a stated mechanism.",
    }  # fmt: skip


PLANTED = dsl(
    "oversold_in_uptrend",
    long={"all": [{"feature": "m15.rsi14", "op": "<", "value": 30},
                  {"feature": "h4.ema50_above_ema200", "op": "==", "value": True}]},
)  # fmt: skip
NOISE = [
    dsl("adx_short", short={"all": [{"feature": "m15.adx14", "op": ">", "value": 30}]}),
    dsl("london_short", short={"all": [{"feature": "ctx.session", "op": "==", "value": "LONDON"}]}),
    dsl("overbought_short", short={"all": [{"feature": "m15.rsi14", "op": ">", "value": 60}]}),
    dsl("downtrend_short", short={"all": [{"feature": "h4.ema50_above_ema200", "op": "==", "value": False}]}),
]
INVALID = dsl("broken_idea", long={"all": [{"feature": "m15.rsi_14", "op": "<", "value": 30}]})


def make_loop(tmp_path: Path, evaluator: Any) -> ResearchLoop:
    return ResearchLoop(
        evaluator, Ledger(tmp_path / "ledger.jsonl"), CFG, WINDOW, fingerprint="fp-1",
        evidence_dir=tmp_path / "evidence", drafts_dir=tmp_path / "drafts", now=lambda: NOW,
    )  # fmt: skip


async def run(tmp_path: Path, evaluator: Any, reply: dict[str, Any], **kw: Any):  # type: ignore[no-untyped-def]
    llm = FakeLLM([reply])
    loop = make_loop(tmp_path, evaluator)
    researcher = Researcher(llm, LLMConfig(analyst_model="m", auditor_model="m"), max_hypotheses=5)
    report = await loop.run(researcher, run_id="R-1", playbooks=[], **kw)
    return report, loop, llm


async def test_a_planted_edge_is_validated_and_noise_is_not(tmp_path: Path) -> None:
    world = Synthetic(planted=True)
    report, loop, _ = await run(tmp_path, world, {"hypotheses": [PLANTED, INVALID, *NOISE]})

    assert [r.reason.split(": ")[-1] for r in report.proposal.rejected][:1] == [
        "unknown feature 'm15.rsi_14'"
    ]
    by_tag = {e.hypothesis.setup_tag: e for e in report.evaluations}
    assert set(by_tag) == {
        "oversold_in_uptrend",
        "adx_short",
        "london_short",
        "overbought_short",
        "downtrend_short",
    }
    planted = by_tag["oversold_in_uptrend"]
    assert planted.validated, [c.detail for c in planted.checks + planted.holdout_checks if not c.passed]
    assert planted.walk_forward.summary.mean > 0.4 and planted.holdout is not None  # noqa: PT018
    assert [e.hypothesis.setup_tag for e in report.evaluations if e.validated] == ["oversold_in_uptrend"]

    # the holdout was looked at exactly once, and only for what passed the walk-forward part
    holdouts = [t for t in loop.ledger.trials() if t.split is Split.HOLDOUT]
    assert [t.hypothesis_id for t in holdouts] == [planted.hypothesis_id]
    looked = [c for c in world.calls if c[1] == WINDOW.holdout_start]
    assert [c[0] for c in looked] == [planted.hypothesis.id]
    assert all(t.origin is Origin.LLM for t in loop.ledger.trials())

    (record,) = load_evidence(tmp_path / "evidence")
    assert (record.detector, record.gate, record.params_sha256) == (
        planted.hypothesis.id,
        EvidenceGate.E1_PASSED,
        planted.hypothesis.params_sha256(),
    )
    assert record.trials == [planted.trial.trial_id, holdouts[0].trial_id]
    assert EntryHypothesis.model_validate(record.hypothesis).params_sha256() == record.params_sha256
    card = yaml.safe_load((tmp_path / "drafts" / f"{planted.hypothesis.id}.yaml").read_text())
    assert (card["status"], card["origin"], card["evidence"]) == ("DRAFT", "llm", record.filename)
    assert card["long_rules"] == ["m15.rsi14 < 30 AND h4.ema50_above_ema200 == true"]


async def test_noise_yields_nothing_and_keeps_the_holdout_clean(tmp_path: Path) -> None:
    """Seed-dependent by nature: with seed 7 this very noise world hands the planted hypothesis's subset a
    z = 2.9 run (40.6% wins where 33.3% is true) that also survives the holdout — the gate's nominal false-
    positive rate at work, and the reason E2 forward confirmation exists. Calibration over many seeds is
    measured in test_stats (R.2); this test pins one ordinary seed."""
    report, loop, _ = await run(tmp_path, Synthetic(planted=False), {"hypotheses": [PLANTED, *NOISE]})
    assert len(report.evaluations) == 5
    assert not any(e.validated for e in report.evaluations)
    assert all(e.note.startswith("failed the walk-forward part") for e in report.evaluations)
    assert not [t for t in loop.ledger.trials() if t.split is Split.HOLDOUT]
    assert load_evidence(tmp_path / "evidence") == []


async def test_holdout_results_never_reach_a_prompt(tmp_path: Path) -> None:
    world = Synthetic(planted=True)
    await run(tmp_path, world, {"hypotheses": [PLANTED, *NOISE]})  # spends the planted hypothesis's holdout
    loop = make_loop(tmp_path, world)
    sentinel = summarize([D("7.777")] * 4321)
    loop.ledger.record(
        hypothesis={"kind": "dsl", "behaviour": {"setup_tag": "sentinel_holdout_only"}}, symbols=["XAUUSD"],
        start=WINDOW.holdout_start, end=WINDOW.end, split=Split.HOLDOUT, origin=Origin.OPERATOR,
        data_fingerprint="fp-1", summary=sentinel, now=NOW,
    )  # fmt: skip
    assert len([t for t in loop.ledger.trials() if t.split is Split.HOLDOUT]) == 2
    probe = [
        outcome(WINDOW.start + timedelta(days=d), Direction.LONG, D(1), {"ctx.session": "ASIA"})
        for d in range(3)
    ]

    prompts: list[str] = []

    def reply(request: LLMRequest) -> dict[str, Any]:
        prompts.append("\n".join(m.content for m in request.messages))
        return {"hypotheses": [PLANTED, *NOISE]}

    researcher = Researcher(
        FakeLLM([reply]), LLMConfig(analyst_model="m", auditor_model="m"), max_hypotheses=5
    )
    report = await loop.run(researcher, run_id="R-2", playbooks=[], probe=probe)

    (text,) = prompts
    assert "sentinel_holdout_only" not in text and "4321" not in text and "7.777" not in text  # noqa: PT018
    assert "| holdout |" not in text  # no holdout row at all, the planted hypothesis's spent one included
    assert "oversold_in_uptrend | LONG m15.rsi14 < 30 AND h4.ema50_above_ema200 == true" in text
    assert "by session (UTC)\n  ASIA: n=3 mean +1.000R win 100%" in text
    planted = next(e for e in report.evaluations if e.hypothesis.setup_tag == "oversold_in_uptrend")
    assert planted.note == "holdout already spent on this hypothesis"  # a second look is never taken


def test_descriptive_tables_refuse_holdout_signals(tmp_path: Path) -> None:
    loop = make_loop(tmp_path, Synthetic(planted=False, events=10))
    late = outcome(WINDOW.holdout_start, Direction.LONG, D(1), {})
    with pytest.raises(ValueError, match="holdout window"):
        loop.tables([late])
    assert loop.tables([]) == []


async def test_operator_hypotheses_and_no_holdout_on_request(tmp_path: Path) -> None:
    loop = make_loop(tmp_path, Synthetic(planted=True))
    h = EntryHypothesis.model_validate({**PLANTED, "id": "OP-1", "version": 1})
    (e,) = loop.evaluate([h], Origin.OPERATOR, spend_holdout=False)
    assert passed_wf(e)
    assert e.note == "passed the walk-forward part; holdout not requested"
    assert [t.origin for t in loop.ledger.trials()] == [Origin.OPERATOR]


def passed_wf(e: Any) -> bool:
    return all(c.passed for c in e.checks)


def test_ledger_documents_are_described_in_one_line() -> None:
    assert describe_hypothesis(
        {"family": "mtf_trend_pullback", "version": "1", "grid": {"a": {}, "b": {}}}
    ) == ("mtf_trend_pullback v1 grid of 2 (walk-forward choice)")
    assert describe_hypothesis(
        {"detector": "mtf_trend_pullback", "version": "1", "params": {"oversold": 20.0}}
    ) == ("mtf_trend_pullback v1 (oversold=20.0)")
    short = {"any": [{"feature": "m15.adx14", "op": ">", "value": 30}]}
    doc = {"kind": "dsl", "behaviour": {**PLANTED, "short": short, "target": None}}
    assert describe_hypothesis(doc) == (
        "oversold_in_uptrend | LONG m15.rsi14 < 30 AND h4.ema50_above_ema200 == true | SHORT m15.adx14 > 30"
        " | stop 1xATR(trigger) no target"
    )
    assert len(describe_hypothesis({"something": "else"})) == 16


def test_the_window_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="start < holdout_start < end"):
        Window(start=T0, holdout_start=T0, end=T0 + timedelta(days=1))


def test_the_history_evaluator_runs_the_signal_study(tmp_path: Path) -> None:
    h = EntryHypothesis.model_validate(
        {**dsl("doji_probe", long={"all": [{"feature": "m15.candle_dir", "op": "==", "value": "DOJI"}]}),
         "id": "OP-2", "version": 1}
    )  # fmt: skip
    history, spec = sig.world(), sig.spec()
    evaluator = HistoryEvaluator(history, [spec])
    end = sig.START + timedelta(hours=6)
    got = evaluator.outcomes(h, sig.START, end)
    assert got
    assert got == run_study(history, spec, [DslDetector(h, spec.roles)], sig.START, end).outcomes
    assert (evaluator.symbols, evaluator.roles) == (["XAUUSD"], sig.ROLES)
    with pytest.raises(ValueError, match="same profile"):
        HistoryEvaluator(history, [])


def test_the_probe_alternates_direction_bar_by_bar() -> None:
    def snap(minutes: int) -> FeatureSnapshot:
        t = T0 + timedelta(minutes=minutes)
        return FeatureSnapshot(symbol="X", trigger_tf=Timeframe.M15, bar_time=t, feature_set_version=2,
                               features={}, bars_ref={Timeframe.M15: t})  # fmt: skip

    dirs = [Probe().detect(snap(15 * i))[0].direction_hint for i in range(4)]
    assert dirs == [Direction.LONG, Direction.SHORT, Direction.LONG, Direction.SHORT]
    assert json.dumps([d.value for d in dirs])  # plain values


async def test_an_edge_that_stops_at_the_holdout_fails_there(tmp_path: Path) -> None:
    world = Synthetic(planted=True, edge_until=WINDOW.holdout_start)
    loop = make_loop(tmp_path, world)
    h = EntryHypothesis.model_validate({**PLANTED, "id": "OP-3", "version": 1})
    (e,) = loop.evaluate([h], Origin.OPERATOR)
    assert passed_wf(e)
    assert (e.validated, e.note) == (False, "failed the holdout")
    assert e.holdout is not None
    assert e.holdout.mean < 0.3
    assert load_evidence(tmp_path / "evidence") == []


def test_probe_fingerprint_and_alignment_labels() -> None:
    from aifund.research.describe import alignment

    assert len(Probe().params_sha256()) == 64
    long = outcome(T0, Direction.LONG, D(1), {"ctx.htf_trend_score": 2})
    short = outcome(T0, Direction.SHORT, D(1), {"ctx.htf_trend_score": 2})
    flat = outcome(T0, Direction.LONG, D(1), {"ctx.htf_trend_score": 0})
    unknown = outcome(T0, Direction.LONG, D(1), {"ctx.htf_trend_score": None})
    assert [alignment(o) for o in (long, short, flat, unknown)] == [
        "with the HTF trend", "against it", "HTF mixed", "unknown",
    ]  # fmt: skip
