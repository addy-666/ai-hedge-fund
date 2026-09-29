"""Edge research on exported history (docs/09; roadmap R.4). Measures, never trades: nothing here can send an order.

    cd backend
    uv run python scripts/research.py baseline                   # grid + walk-forward + gate E1 (holdout untouched)
    uv run python scripts/research.py baseline --spend-holdout   # ...and, only if the walk-forward passes, the
                                                                 #    ONE holdout evaluation this hypothesis gets

``baseline`` studies the ``mtf_trend_pullback`` detector over a small DECLARED parameter grid (declared here, in
code review, not tuned after seeing results). The grid is one hypothesis: its parameters are chosen inside the
walk-forward, so the out-of-sample result already pays for the choice. Every run is appended to the trial
ledger (``<repo>/data/research/ledger.jsonl``); the report is printed and saved as JSON next to it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aifund.adapters import history_store as hs
from aifund.config.loader import load_trading_config
from aifund.config.settings import PROJECT_ROOT, Settings
from aifund.config.trading_config import ProfileConfig, TradingConfig
from aifund.domain.enums import Timeframe
from aifund.research.gates import e1_holdout_checks, e1_walk_forward_checks, passed, throughput
from aifund.research.history import History
from aifund.research.ledger import Ledger, Origin, Split, digest
from aifund.research.signals import CostModel, SignalOutcome, StudySpec, run_study
from aifund.research.stats import summarize
from aifund.research.walkforward import choose, walk_forward
from aifund.strategies.mtf_trend_pullback import MtfTrendPullback, PullbackParams

GRID: dict[str, PullbackParams] = {  # the first is the default (the vault note's own values)
    "default": PullbackParams(),
    "oversold_20": PullbackParams(oversold=20.0),
    "oversold_40": PullbackParams(oversold=40.0),
    "no_candle": PullbackParams(require_candle=False),
    "zone_0.5": PullbackParams(value_zone_atr=0.5),
}


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("study", choices=["baseline"])
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
    history: History, cfg: TradingConfig, profile: ProfileConfig, symbols: list[str], params: PullbackParams,
    start: datetime, end: datetime, costs: CostModel,
) -> tuple[list[SignalOutcome], dict[str, Any]]:  # fmt: skip
    outcomes: list[SignalOutcome] = []
    meta: dict[str, Any] = {}
    for sym in cfg.symbols:
        if sym.broker not in symbols:
            continue
        spec = StudySpec.from_config(cfg, sym, profile=profile, costs=costs)
        result = run_study(history, spec, [MtfTrendPullback(spec.roles, params)], start, end)
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


def main(argv: list[str]) -> int:
    args = parse(argv)
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
    start = manifest.start.replace(second=0, microsecond=0)
    end = manifest.end.replace(second=0, microsecond=0)
    holdout_start = end - (end - start) * float(rc.holdout_fraction)
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
    for name, params in GRID.items():
        print(f"study {name} ...", flush=True)
        outcomes, meta = study_all(history, cfg, profile, symbols, params, start, holdout_start, costs)
        variants[name] = outcomes
        s = summarize([o.r_net for o in outcomes], times=[o.entry_time for o in outcomes])
        hypothesis = {
            **base,
            "detector": "mtf_trend_pullback",
            "version": MtfTrendPullback.version,
            "params": asdict(params),
        }
        ledger.record(hypothesis=hypothesis, symbols=symbols, start=start, end=holdout_start, split=Split.IN_SAMPLE,
                      origin=Origin.GRID, data_fingerprint=fingerprint, summary=s, now=now)  # fmt: skip
        report["variants"][name] = {"in_sample": s.render(), "per_symbol": meta}
        print(f"  in-sample (diagnostic only): {s.render()}", flush=True)

    wf = walk_forward(
        variants, start=start, end=holdout_start, folds=rc.folds, min_train_signals=rc.min_train_signals
    )
    family = {**base, "family": "mtf_trend_pullback", "version": MtfTrendPullback.version,
              "grid": {n: asdict(p) for n, p in GRID.items()}, "selection": f"walk-forward best mean, min {rc.min_train_signals}"}  # fmt: skip
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
        elif ledger.holdout_spent(family):
            print("holdout NOT spent: this hypothesis already had its one look")
        else:
            chosen, _ = choose(
                variants, start=start, before=holdout_start, min_train_signals=rc.min_train_signals
            )
            outcomes, meta = study_all(
                history, cfg, profile, symbols, GRID[chosen], holdout_start, end, costs
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

    out = Path(args.out) / f"report_baseline_{now:%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"report: {out}\nledger: {ledger.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
