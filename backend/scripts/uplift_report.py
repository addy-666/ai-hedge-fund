"""Gate G-LLM report (docs/09 §7, roadmap R.9): does the analyst beat the baseline, net of its cost?

    cd backend
    uv run python scripts/uplift_report.py                       # paired shadows from the engine database
    uv run python scripts/uplift_report.py --risk-usd 50         # the money one trade risks (default: latest
                                                                 #   equity x risk.risk_per_trade_pct)
    uv run python scripts/uplift_report.py --sign-off "operator name"   # only if the gate PASSED: writes the
                                                                 #   sign-off that lets strategy.analyst_orders on

Reads the shadow virtual trades the pipeline records while the analyst runs (every bar with a candidate gets
one for the baseline's decision and, when it proposed a trade, one for the analyst's). Sends nothing.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from aifund.adapters.clock import SystemClock
from aifund.config.evidence import GLlmSignoff, write_g_llm
from aifund.config.loader import load_trading_config
from aifund.config.settings import PROJECT_ROOT, Settings
from aifund.persistence.db import make_engine, make_session_factory, unit_of_work
from aifund.persistence.repositories.equity import EquitySnapshotRepository
from aifund.persistence.repositories.virtual import VirtualTradeRepository
from aifund.research.uplift import Pair, uplift


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default="", help="account id in the database (default: the engine's label)")
    p.add_argument("--risk-usd", type=Decimal, default=None)
    p.add_argument("--sign-off", default=None, help="operator name: record the sign-off if the gate passed")
    p.add_argument("--evidence", default=str(PROJECT_ROOT / "config" / "evidence"))
    args = p.parse_args(argv)

    settings = Settings()
    example = PROJECT_ROOT / "config" / "trading.example.yaml"
    cfg = load_trading_config(settings.CONFIG_PATH if settings.CONFIG_PATH.is_file() else example).config
    account = args.account or cfg.engine.account_label
    factory = make_session_factory(make_engine(settings.DATABASE_URL))
    clock = SystemClock()
    with unit_of_work(factory) as s:
        pairs = VirtualTradeRepository(s, clock).shadow_pairs(account)
        latest = EquitySnapshotRepository(s).latest(account)
    risk_usd = args.risk_usd
    if risk_usd is None:
        if latest is None:
            print("no equity snapshot to size one trade's risk: pass --risk-usd", file=sys.stderr)
            return 2
        risk_usd = latest.equity * cfg.risk.risk_per_trade_pct / 100
    report = uplift(
        [Pair(p.baseline_r, p.analyst_r, p.cost_usd) for p in pairs], risk_usd=risk_usd, cfg=cfg.research
    )
    prompt_version = f"analyst_v{cfg.strategy.analyst_prompt_version}"
    print(f"G-LLM for {prompt_version} on {cfg.llm.analyst_model} (one trade risks {risk_usd:.2f}):")
    print(f"  {report.render()}")
    for c in report.checks:
        print(f"  [{'x' if c.passed else ' '}] {c.name}: {c.detail}")
    print(f"gate G-LLM: {'PASSED' if report.passed else 'NOT PASSED: the analyst stays in shadow'}")
    if args.sign_off:
        if not report.passed:
            print("sign-off refused: the gate did not pass", file=sys.stderr)
            return 1
        assert report.uplift.ci_low is not None and report.uplift.ci_high is not None  # noqa: PT018
        path = write_g_llm(
            Path(args.evidence),
            GLlmSignoff(
                prompt_version=prompt_version, model=cfg.llm.analyst_model, n=report.n,
                mean_uplift_r=report.uplift.mean, ci90=(report.uplift.ci_low, report.uplift.ci_high),
                signed_off_by=args.sign_off, signed_off_at=datetime.now(UTC),
            ),
        )  # fmt: skip
        print(f"signed off: {path} (strategy.analyst_orders may now be enabled)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
