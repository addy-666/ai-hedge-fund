"""Edge research on exported history (docs/09; roadmap R.4). Measures, never trades: nothing here can send an order.

    cd backend
    uv run python scripts/research.py baseline                   # grid + walk-forward + gate E1 (holdout untouched)
    uv run python scripts/research.py baseline --detector nr7_breakout   # the same for another built-in detector
    uv run python scripts/research.py baseline --spend-holdout   # ...and, only if the walk-forward passes, the
                                                                 #    ONE holdout evaluation this hypothesis gets
    uv run python scripts/research.py dsl --file my_ideas.json   # operator hypotheses (a JSON list, docs/09 §5)
    uv run python scripts/research.py scheduled                  # the weekly run (roadmap 8.7): only on new history
                                                                 #    and >= research.schedule_days apart; --force
    uv run python scripts/research.py llm --rounds 1             # the LLM researcher proposes, the loop judges
                                                                 #    (needs DEEPSEEK_API_KEY, llm.pricing and a
                                                                 #    migrated database for the llm_calls budget)

``dsl`` and ``llm`` go through the research loop (docs/09 §6): walk-forward, ledger, BH over the whole ledger,
and — for survivors, with ``--spend-holdout`` — the single holdout look. A validated hypothesis gets an evidence
record in ``config/evidence`` (gate E1) and a DRAFT playbook card in ``<out>/drafts`` for the operator.

Every study uses ONE holdout window, fixed in ``<out>/holdout.json`` on the first run (the last
``holdout_fraction`` of the export then) and rolled forward only onto ``holdout_roll_days`` of new data
(``research/holdout.py``); the ledger allows one holdout look per hypothesis per window.

``scheduled`` (a weekly Windows task after ``export_history.py --update``): when due, the ``baseline`` study of
every built-in detector in ``strategy.detectors`` and ``research.scheduled_llm_rounds`` researcher rounds (with
a DeepSeek key), all with ``--spend-holdout``; BH always runs over the whole ledger.

``baseline`` studies a built-in detector (``--detector``, default ``mtf_trend_pullback``) over a small DECLARED
parameter grid (declared here, in code review, not tuned after seeing results). The grid is one hypothesis: its parameters are chosen inside the
walk-forward, so the out-of-sample result already pays for the choice. Every run is appended to the trial
ledger (``<repo>/data/research/ledger.jsonl``); the report is printed and saved as JSON next to it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aifund.adapters import history_store as hs
from aifund.adapters.clock import SystemClock
from aifund.adapters.llm.deepseek import DeepSeekClient
from aifund.agents.prompting import load_playbook
from aifund.agents.researcher import Researcher
from aifund.config.loader import load_trading_config
from aifund.config.settings import PROJECT_ROOT, Settings
from aifund.config.trading_config import ProfileConfig, TradingConfig
from aifund.domain.enums import Timeframe
from aifund.persistence.db import make_engine, make_session_factory
from aifund.research.describe import Probe
from aifund.research.gates import e1_holdout_checks, e1_walk_forward_checks, passed, throughput
from aifund.research.history import History
from aifund.research.holdout import HoldoutState, ScheduleState, due, plan
from aifund.research.ledger import Ledger, Origin, Split, digest
from aifund.research.loop import Evaluation, HistoryEvaluator, ResearchLoop, Window
from aifund.research.signals import CostModel, SignalOutcome, StudySpec, run_study
from aifund.research.walkforward import choose, walk_forward
from aifund.stats import summarize
from aifund.strategies.base import SetupDetector, TfRoles
from aifund.strategies.dsl_detector import EntryHypothesis
from aifund.strategies.failure_test_2b import FailureTest2B, FailureTestParams
from aifund.strategies.mtf_trend_pullback import MtfTrendPullback, PullbackParams
from aifund.strategies.nr7_breakout import Nr7Breakout, Nr7Params
from aifund.strategies.sr_fade_range import FadeParams, SrFadeRange

PLAYBOOKS = PROJECT_ROOT / "config" / "playbooks"

GRID: dict[str, PullbackParams] = {  # the first is the default (the vault note's own values)
    "default": PullbackParams(),
    "oversold_20": PullbackParams(oversold=20.0),
    "oversold_40": PullbackParams(oversold=40.0),
    "no_candle": PullbackParams(require_candle=False),
    "zone_0.5": PullbackParams(value_zone_atr=0.5),
}
# per built-in detector: (class, declared grid); the first variant is the playbook card's own values
GRIDS: dict[str, tuple[Any, dict[str, Any]]] = {
    "mtf_trend_pullback": (MtfTrendPullback, GRID),
    "nr7_breakout": (Nr7Breakout, {
        "default": Nr7Params(),
        "volume_1.0": Nr7Params(min_rel_volume=1.0),
        "location_0.5": Nr7Params(location_atr=0.5),
        "target_1.5r": Nr7Params(target_r=1.5),
    }),
    "failure_test_2b": (FailureTest2B, {
        "default": FailureTestParams(),
        "overshoot_0.25": FailureTestParams(max_overshoot_atr=0.25),
        "buffer_0.25": FailureTestParams(stop_buffer_atr=0.25),
        "target_1.5r": FailureTestParams(min_target_r=1.5),
    }),
    "sr_fade_range": (SrFadeRange, {
        "default": FadeParams(),
        "range_3atr": FadeParams(min_range_atr=3.0),
        "oversold_30": FadeParams(oversold=30.0, rsi_oversold=35.0),
        "touch_0.5": FadeParams(touch_atr=0.5),
    }),
}  # fmt: skip


def build(detector: str, roles: TfRoles, params: Any) -> SetupDetector:
    cls, _ = GRIDS[detector]
    built: SetupDetector = cls(roles, params)
    return built


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("study", choices=["baseline", "dsl", "llm", "scheduled"])
    p.add_argument("--force", action="store_true", help="scheduled: run even if not due")
    p.add_argument("--file", help="dsl: a JSON list of entry hypotheses")
    p.add_argument(
        "--detector", default="mtf_trend_pullback", choices=sorted(GRIDS), help="baseline: which one"
    )
    p.add_argument(
        "--rounds", type=int, default=1, help="llm: proposal rounds (each sees the updated ledger)"
    )
    p.add_argument("--evidence", default=str(PROJECT_ROOT / "config" / "evidence"))
    p.add_argument("--symbols", default="", help="comma-separated broker symbols (default: all configured)")
    p.add_argument("--context", default="H4", help="context timeframes, e.g. H4 or H4,D1 (default H4)")
    p.add_argument("--history", default=str(PROJECT_ROOT / "data" / "history"))
    p.add_argument("--out", default=str(PROJECT_ROOT / "data" / "research"))
    p.add_argument(
        "--spend-holdout", action="store_true", help="evaluate the holdout (once) if E1 passes so far"
    )
    return p.parse_args(argv)


def load_config() -> TradingConfig:
    settings = Settings()
    path = (
        settings.CONFIG_PATH
        if settings.CONFIG_PATH.is_file()
        else PROJECT_ROOT / "config" / "trading.example.yaml"
    )
    return load_trading_config(path).config


def study_all(
    history: History, cfg: TradingConfig, profile: ProfileConfig, symbols: list[str], params: Any,
    start: datetime, end: datetime, costs: CostModel, detector: str = "mtf_trend_pullback",
) -> tuple[list[SignalOutcome], dict[str, Any]]:  # fmt: skip
    outcomes: list[SignalOutcome] = []
    meta: dict[str, Any] = {}
    for sym in cfg.symbols:
        if sym.broker not in symbols:
            continue
        spec = StudySpec.from_config(cfg, sym, profile=profile, costs=costs)
        result = run_study(history, spec, [build(detector, spec.roles, params)], start, end)
        outcomes += result.outcomes
        meta[sym.broker] = {
            "trigger_bars": result.trigger_bars,
            "candidates": result.candidates,
            "signals": len(result.outcomes),
            "first_evaluated": result.first_evaluated.isoformat() if result.first_evaluated else None,
            "skipped": dict(result.skipped.most_common()),
            "resolution": sorted({o.resolution.value for o in result.outcomes}),
        }
    return sorted(outcomes, key=lambda o: o.entry_time), meta


def holdout_window(
    args: argparse.Namespace, manifest: hs.Manifest, rc: Any
) -> tuple[datetime, datetime, datetime]:
    """(history start, holdout start, holdout end) from the recorded holdout, rolled forward when due."""
    start = manifest.start.replace(second=0, microsecond=0)
    end = manifest.end.replace(second=0, microsecond=0)
    path = Path(args.out) / "holdout.json"
    state = HoldoutState.load(path)
    window, changed = plan(
        state, data_start=start, data_end=end, fraction=rc.holdout_fraction, roll_days=rc.holdout_roll_days,
        now=datetime.now(UTC),
    )  # fmt: skip
    if changed:
        state.save(path)
        what = "set" if window.generation == 1 else "ROLLED FORWARD (the old holdout joins the walk-forward)"
        print(
            f"holdout {what}: generation {window.generation} [{window.start:%Y-%m-%d}, {window.end:%Y-%m-%d})"
        )
    return start, window.start, window.end


def scheduled(args: argparse.Namespace) -> int:
    cfg = load_config()
    rc = cfg.research
    manifest = hs.read_manifest(Path(args.history))
    fingerprint = digest(
        {"server": manifest.server, "start": manifest.start, "end": manifest.end, "rows": manifest.rows}
    )
    path = Path(args.out) / "schedule.json"
    now = datetime.now(UTC)
    run, why = due(ScheduleState.load(path), fingerprint=fingerprint, now=now, every_days=rc.schedule_days)
    if not run and not args.force:
        print(f"scheduled research not due: {why}")
        return 0
    print(f"scheduled research: {why if run else 'forced'}", flush=True)
    holdout_window(args, manifest, rc)  # roll the holdout before anything is studied
    common = ["--symbols", args.symbols, "--context", args.context, "--history", args.history, "--out", args.out,
              "--evidence", args.evidence, "--spend-holdout"]  # fmt: skip
    codes = []
    for detector in [d for d in cfg.strategy.detectors if d in GRIDS]:
        print(f"\n=== baseline --detector {detector}", flush=True)
        codes.append(main(["baseline", "--detector", detector, *common]))
    if rc.scheduled_llm_rounds and Settings().DEEPSEEK_API_KEY is not None:
        print(f"\n=== llm --rounds {rc.scheduled_llm_rounds}", flush=True)
        codes.append(main(["llm", "--rounds", str(rc.scheduled_llm_rounds), *common]))
    elif rc.scheduled_llm_rounds:
        print("\nno DEEPSEEK_API_KEY: the LLM researcher is skipped")
    ScheduleState(now, fingerprint).save(path)
    return max(codes, default=0)


def main(argv: list[str]) -> int:
    args = parse(argv)
    if args.study == "scheduled":
        return scheduled(args)
    if args.study != "baseline":
        return asyncio.run(loop_main(args))
    cfg = load_config()
    rc = cfg.research
    root = Path(args.history)
    manifest = hs.read_manifest(root)
    available = set(hs.read_specs(root))
    wanted = {s.strip() for s in args.symbols.split(",") if s.strip()}
    symbols = [s.broker for s in cfg.symbols if s.broker in available and (not wanted or s.broker in wanted)]
    context = [Timeframe(tf.strip()) for tf in args.context.split(",")]
    profile = ProfileConfig(trigger_tf=Timeframe.M15, setup_tf=Timeframe.H1, context_tfs=context)
    fingerprint = digest(
        {"server": manifest.server, "start": manifest.start, "end": manifest.end, "rows": manifest.rows}
    )
    print(f"loading {symbols} from {root} ({manifest.start:%Y-%m-%d} -> {manifest.end:%Y-%m-%d})", flush=True)
    history = History.load(root, symbols, [Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1, *context])
    start, holdout_start, end = holdout_window(args, manifest, rc)
    costs = CostModel(commission_per_lot=rc.commission_per_lot, slippage_points=rc.slippage_points)
    ledger = Ledger(Path(args.out) / "ledger.jsonl")
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "symbols": symbols,
        "context": [tf.value for tf in context],
        "trigger": "M15",
        "setup": "H1",
    }

    variants: dict[str, list[SignalOutcome]] = {}
    report: dict[str, Any] = {
        "window": [start.isoformat(), holdout_start.isoformat()],
        "holdout_start": holdout_start.isoformat(),
        "variants": {},
    }
    detector_cls, grid = GRIDS[args.detector]
    for name, params in grid.items():
        print(f"study {args.detector} {name} ...", flush=True)
        outcomes, meta = study_all(
            history, cfg, profile, symbols, params, start, holdout_start, costs, args.detector
        )
        variants[name] = outcomes
        s = summarize([o.r_net for o in outcomes], times=[o.entry_time for o in outcomes])
        hypothesis = {
            **base,
            "detector": args.detector,
            "version": detector_cls.version,
            "params": asdict(params),
        }
        ledger.record(hypothesis=hypothesis, symbols=symbols, start=start, end=holdout_start, split=Split.IN_SAMPLE,
                      origin=Origin.GRID, data_fingerprint=fingerprint, summary=s, now=now)  # fmt: skip
        report["variants"][name] = {"in_sample": s.render(), "per_symbol": meta}
        print(f"  in-sample (diagnostic only): {s.render()}", flush=True)

    wf = walk_forward(
        variants, start=start, end=holdout_start, folds=rc.folds, min_train_signals=rc.min_train_signals
    )
    family = {**base, "family": args.detector, "version": detector_cls.version,
              "grid": {n: asdict(p) for n, p in grid.items()}, "selection": f"walk-forward best mean, min {rc.min_train_signals}"}  # fmt: skip
    ledger.record(hypothesis=family, symbols=symbols, start=start, end=holdout_start, split=Split.WALK_FORWARD,
                  origin=Origin.GRID, data_fingerprint=fingerprint, summary=wf.summary, now=now)  # fmt: skip
    survives = digest(family) in ledger.survivors(float(rc.fdr_q))
    checks = e1_walk_forward_checks(wf, survives_fdr=survives, cfg=rc)
    firsts = [datetime.fromisoformat(m["first_evaluated"]) for v in report["variants"].values()
              for m in v["per_symbol"].values() if m["first_evaluated"]]  # fmt: skip
    tp = throughput(
        len(variants["default"]), min(firsts) if firsts else start, holdout_start, rc, cfg.learning
    )

    print("\nwalk-forward (anchored, parameters chosen in-sample):")
    for f in wf.folds:
        oos = "none" if f.oos_mean is None else f"{f.oos_mean:+.3f}R"
        print(f"  fold {f.index}: {f.test_start:%Y-%m-%d} -> {f.test_end:%Y-%m-%d}  chose {f.chosen:<12} "
              f"in-sample {f.in_sample.n} sig {f.in_sample.mean:+.3f}R | out-of-sample {len(f.out_of_sample)} sig {oos}")  # fmt: skip
    print(f"  OUT-OF-SAMPLE: {wf.summary.render()}; positive folds {wf.positive_fold_share:.0%}")
    print(f"throughput (default variant): {tp.render()}")
    print("gate E1 (walk-forward part):")
    for c in checks:
        print(f"  [{'x' if c.passed else ' '}] {c.name}: {c.detail}")

    report.update(
        walk_forward={
            "summary": wf.summary.render(), "positive_fold_share": wf.positive_fold_share,
            "folds": [{"index": f.index, "test_start": f.test_start.isoformat(), "chosen": f.chosen,
                       "in_sample": f.in_sample.render(), "oos_n": len(f.out_of_sample), "oos_mean": f.oos_mean}
                      for f in wf.folds],
            "monthly": wf.summary.monthly,
        },
        throughput={"signals_per_month": tp.signals_per_month, "months_to": tp.months_to, "too_slow": tp.too_slow},
        e1_walk_forward=[asdict(c) for c in checks], survives_fdr=survives, holdout=None,
    )  # fmt: skip

    if args.spend_holdout:
        if not passed(checks):
            print(
                "holdout NOT spent: the walk-forward part of E1 failed (it stays clean for a better hypothesis)"
            )
        elif ledger.holdout_spent(family, holdout_start, end):
            print("holdout NOT spent: this hypothesis already had its one look at this holdout window")
        else:
            chosen, _ = choose(
                variants, start=start, before=holdout_start, min_train_signals=rc.min_train_signals
            )
            outcomes, meta = study_all(
                history, cfg, profile, symbols, grid[chosen], holdout_start, end, costs, args.detector
            )
            hs_summary = summarize([o.r_net for o in outcomes], times=[o.entry_time for o in outcomes])
            ledger.record(hypothesis=family, symbols=symbols, start=holdout_start, end=end, split=Split.HOLDOUT,
                          origin=Origin.GRID, data_fingerprint=fingerprint, summary=hs_summary, now=now)  # fmt: skip
            hold = e1_holdout_checks(hs_summary, rc)
            print(f"holdout ({chosen}): {hs_summary.render()}")
            for c in hold:
                print(f"  [{'x' if c.passed else ' '}] {c.name}: {c.detail}")
            print(f"gate E1: {'PASSED' if passed(checks + hold) else 'FAILED'}")
            report["holdout"] = {
                "chosen": chosen,
                "summary": hs_summary.render(),
                "checks": [asdict(c) for c in hold],
            }

    out = Path(args.out) / f"report_baseline_{args.detector}_{now:%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"report: {out}\nledger: {ledger.path}")
    return 0


def prepare(args: argparse.Namespace) -> tuple[TradingConfig, History, ResearchLoop]:
    """Config, history and a research loop over the same window and data fingerprint as ``baseline``."""
    cfg = load_config()
    rc = cfg.research
    root = Path(args.history)
    manifest = hs.read_manifest(root)
    available = set(hs.read_specs(root))
    wanted = {s.strip() for s in args.symbols.split(",") if s.strip()}
    symbols = [s for s in cfg.symbols if s.broker in available and (not wanted or s.broker in wanted)]
    context = [Timeframe(tf.strip()) for tf in args.context.split(",")]
    profile = ProfileConfig(trigger_tf=Timeframe.M15, setup_tf=Timeframe.H1, context_tfs=context)
    fingerprint = digest(
        {"server": manifest.server, "start": manifest.start, "end": manifest.end, "rows": manifest.rows}
    )
    history = History.load(
        root, [s.broker for s in symbols], [Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1, *context]
    )
    start, holdout_start, end = holdout_window(args, manifest, rc)
    costs = CostModel(commission_per_lot=rc.commission_per_lot, slippage_points=rc.slippage_points)
    specs = [StudySpec.from_config(cfg, s, profile=profile, costs=costs) for s in symbols]
    loop = ResearchLoop(
        HistoryEvaluator(history, specs), Ledger(Path(args.out) / "ledger.jsonl"), rc,
        Window(start, holdout_start, end), fingerprint=fingerprint,
        evidence_dir=Path(args.evidence), drafts_dir=Path(args.out) / "drafts", now=lambda: datetime.now(UTC),
    )  # fmt: skip
    return cfg, history, loop


def print_evaluations(evaluations: list[Evaluation]) -> None:
    for e in evaluations:
        h = e.hypothesis
        print(f"\n{h.id} {h.setup_tag}: {h.mechanism}")
        print(f"  walk-forward OOS: {e.walk_forward.summary.render()}; positive folds "
              f"{e.walk_forward.positive_fold_share:.0%}")  # fmt: skip
        for c in e.checks + e.holdout_checks:
            print(f"  [{'x' if c.passed else ' '}] {c.name}: {c.detail}")
        print(f"  -> {e.note}" + (f" (evidence {e.evidence}, draft card {e.card})" if e.validated else ""))


async def loop_main(args: argparse.Namespace) -> int:
    cfg, history, loop = prepare(args)
    if args.study == "dsl":
        if not args.file:
            print("dsl needs --file (a JSON list of entry hypotheses)", file=sys.stderr)
            return 2
        docs = json.loads(Path(args.file).read_text(encoding="utf-8"))
        hypotheses = [EntryHypothesis.model_validate(d) for d in (docs if isinstance(docs, list) else [docs])]
        print_evaluations(loop.evaluate(hypotheses, Origin.OPERATOR, spend_holdout=args.spend_holdout))
        return 0

    settings = Settings()
    if settings.DEEPSEEK_API_KEY is None:
        print("llm needs DEEPSEEK_API_KEY in .env", file=sys.stderr)
        return 2
    factory = make_session_factory(make_engine(settings.DATABASE_URL))
    client = DeepSeekClient(
        cfg.llm, settings.DEEPSEEK_API_KEY.get_secret_value(), factory=factory, clock=SystemClock()
    )
    researcher = Researcher(client, cfg.llm, max_hypotheses=cfg.research.max_hypotheses_per_run)
    playbooks = [load_playbook(PLAYBOOKS, p.stem) for p in sorted(PLAYBOOKS.glob("*.yaml"))]
    w = loop.window
    print("probe study for the descriptive tables (pre-holdout only) ...", flush=True)
    evaluator = loop.evaluator
    assert isinstance(evaluator, HistoryEvaluator)
    probe = [
        o
        for spec in evaluator.specs
        for o in run_study(history, spec, [Probe()], w.start, w.holdout_start).outcomes
    ]
    for n in range(1, args.rounds + 1):
        run_id = f"research-{datetime.now(UTC):%Y%m%d%H%M%S}-{n}"
        report = await loop.run(researcher, run_id=run_id, playbooks=playbooks, probe=probe,
                                spend_holdout=args.spend_holdout)  # fmt: skip
        p = report.proposal
        print(f"\nround {n}: {len(p.hypotheses)} valid, {len(p.rejected)} rejected, cost ${p.cost_usd}"
              + (f", provider error: {p.error}" if p.error else ""))  # fmt: skip
        for r in p.rejected:
            print(f"  rejected (round {r.round}): {r.reason}")
        print_evaluations(report.evaluations)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
