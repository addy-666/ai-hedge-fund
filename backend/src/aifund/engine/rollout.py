"""Rollout ladder and its exit gates (docs/06 §10, roadmap 9.6-9.7), evaluated from the engine's own records.

The level follows the configuration: SIM → L0, DEMO with ``strategy.dry_run`` → L1 (the PAPER stand-in),
DEMO → L2, LIVE with ``risk_per_trade_pct`` ≤ 0.25 → L3 (live-micro), LIVE above that → L4. The period of a
level starts at the operator's sign-off that entered it (``audit_log`` ROLLOUT_SIGNOFF), else at the first
trade.

Exit gates of L2 (DEMO, docs/06 §10), each measured, never self-reported:

- ≥ 28 days and ≥ 100 closed trades in the period;
- 0 duplicate orders (no decision and no position with two filled OPEN intents);
- 0 unreconciled or lost trades and every close's net P&L equal to MT5's: the nightly ledger check
  (``job.verify_ledger``) passed within the last 36 h, and no intent is UNKNOWN;
- 0 positions without a stop-loss (``position.sl_missing`` events);
- every closed trade has its decision and feature snapshot;
- the kill switch was tested (a FLATTEN_ALL command completed) and a restart with open positions happened.

L3 (live-micro) adds: risk per trade ≤ 0.25%; live costs per lot within 20% of the DEMO period's; expectancy
net of costs ≥ 0 over the period (its 90% CI shown, not required above 0). L0 and L1 exits are operator
judgements (the scenario suite and replays run in CI; PAPER has no fills) and show as MANUAL.

Signing off records the operator's decision in ``audit_log``; it is refused unless every measured gate passes.
Changing the mode in the configuration stays the operator's separate step. Moving DOWN never needs this.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from aifund.config.trading_config import TradingConfig
from aifund.domain.enums import Mode
from aifund.domain.rollout import ClosedTrade, RolloutFacts
from aifund.stats import summarize

SIGNOFF = "ROLLOUT_SIGNOFF"
MICRO_RISK_PCT = Decimal("0.25")
LEDGER_FRESH = timedelta(hours=36)


class Level(StrEnum):
    L0 = "L0"  # SIM
    L1 = "L1"  # PAPER (DEMO + dry run)
    L2 = "L2"  # DEMO, real orders on a demo account
    L3 = "L3"  # LIVE-MICRO
    L4 = "L4"  # LIVE


NAMES = {Level.L0: "SIM", Level.L1: "PAPER", Level.L2: "DEMO", Level.L3: "LIVE-MICRO", Level.L4: "LIVE"}
NEXT = {Level.L0: Level.L1, Level.L1: Level.L2, Level.L2: Level.L3, Level.L3: Level.L4}


class GateStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - a gate verdict, not a password
    FAIL = "FAIL"
    MANUAL = "MANUAL"  # an operator judgement, recorded with the sign-off note


@dataclass(frozen=True)
class Gate:
    name: str
    status: GateStatus
    value: str
    need: str


@dataclass(frozen=True)
class Report:
    level: Level
    period_start: datetime | None
    gates: list[Gate] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.level in NEXT and all(g.status is not GateStatus.FAIL for g in self.gates)


def level_of(cfg: TradingConfig) -> Level:
    mode = cfg.engine.mode
    if mode is Mode.SIM:
        return Level.L0
    if mode in (Mode.DEMO, Mode.PAPER):
        return Level.L1 if cfg.strategy.dry_run or mode is Mode.PAPER else Level.L2
    return Level.L3 if cfg.risk.risk_per_trade_pct <= MICRO_RISK_PCT else Level.L4


def _gate(name: str, ok: bool, value: str, need: str) -> Gate:
    return Gate(name, GateStatus.PASS if ok else GateStatus.FAIL, value, need)


def _per_lot(trades: Sequence[ClosedTrade]) -> Decimal | None:
    volume = sum((t.volume for t in trades), Decimal(0))
    return -sum((t.costs for t in trades), Decimal(0)) / volume if volume > 0 else None


def demo_gates(f: RolloutFacts, now: datetime) -> list[Gate]:
    days = (now - f.period_start).total_seconds() / 86400 if f.period_start else 0.0
    fresh = f.ledger_at is not None and now - f.ledger_at <= LEDGER_FRESH
    ledger_ok = f.ledger_status == "ok" and fresh
    ledger = f"{f.ledger_status or 'never ran'}" + (
        f" at {f.ledger_at:%Y-%m-%d %H:%M} UTC" if f.ledger_at else ""
    )
    return [
        _gate("duration", days >= 28, f"{days:.1f} days", "≥ 28 days"),
        _gate("closed_trades", len(f.trades) >= 100, str(len(f.trades)), "≥ 100"),
        _gate("duplicate_orders", f.duplicate_opens == 0, str(f.duplicate_opens), "0"),
        _gate("unreconciled_or_lost", ledger_ok and f.unknown_intents == 0,
              f"ledger check {ledger}; {f.unknown_intents} UNKNOWN intents",
              "nightly ledger check ok within 36 h, 0 UNKNOWN"),
        _gate("positions_without_sl", f.sl_missing == 0, str(f.sl_missing), "0"),
        _gate("net_pnl_matches_mt5", ledger_ok, f"ledger check {ledger}", "0 differences (automated diff)"),
        _gate("trades_with_context", f.without_context == 0, f"{f.without_context} without decision/snapshot",
              "every closed trade"),
        _gate("kill_switch_tested", f.flatten_tested > 0, f"{f.flatten_tested} FLATTEN_ALL done", "≥ 1"),
        _gate("restart_with_open_positions", f.restarts_with_open > 0, f"{f.restarts_with_open} restarts",
              "≥ 1"),
    ]  # fmt: skip


def live_micro_gates(f: RolloutFacts, cfg: TradingConfig, now: datetime) -> list[Gate]:
    live, demo = _per_lot(f.trades), _per_lot(f.baseline_trades)
    if live is None or demo is None:
        costs = _gate("costs_vs_demo", False, "no trades to compare", "within 20% of DEMO per lot")
    else:
        drift = abs(live - demo) / demo if demo != 0 else (Decimal(0) if live == 0 else Decimal(1))
        costs = _gate("costs_vs_demo", drift <= Decimal("0.2"),
                      f"{live:.2f} vs {demo:.2f} per lot ({drift:.0%})",
                      "within 20% of DEMO per lot")  # fmt: skip
    rs = [t.r for t in f.trades if t.r is not None]
    if rs:
        s = summarize(rs, times=None)
        ci = "n/a" if s.ci_low is None else f"[{s.ci_low:+.3f}, {s.ci_high:+.3f}]"
        expectancy = _gate("expectancy_net", s.mean >= 0, f"{s.mean:+.3f}R, 90% CI {ci} (n={s.n})", "≥ 0R")
    else:
        expectancy = _gate("expectancy_net", False, "no closed trades", "≥ 0R")
    risk = cfg.risk.risk_per_trade_pct
    return [
        *demo_gates(f, now),
        _gate("micro_risk", risk <= MICRO_RISK_PCT, f"{risk}% per trade", f"≤ {MICRO_RISK_PCT}%"),
        costs,
        expectancy,
    ]


def evaluate(cfg: TradingConfig, facts: RolloutFacts, now: datetime) -> Report:
    level = level_of(cfg)
    if level is Level.L0:
        gates = [Gate("scenario_suite", GateStatus.MANUAL, "CI: tests + nightly chaos (100 seeds)", "green"),
                 Gate("replay_3_months", GateStatus.MANUAL, "scripts/replay.py on 3 months, all symbols",
                      "0 invariant violations")]  # fmt: skip
    elif level is Level.L1:
        gates = [Gate("paper_1_2_weeks", GateStatus.MANUAL, "uptime, funnel, LLM cost, no stale-data trades",
                      "99.5% uptime, sane funnel, within budget")]  # fmt: skip
    elif level is Level.L2:
        gates = demo_gates(facts, now)
        if cfg.engine.demo_orders_without_evidence:  # roadmap 10.11: said, not blocking (L2 tests plumbing)
            gates.append(Gate("evidence_override", GateStatus.MANUAL,
                              "ON: DEMO orders without E1 / G-LLM evidence",
                              "L3 trades only strategies with E1 + E2: turn it off first"))  # fmt: skip
    elif level is Level.L3:
        gates = live_micro_gates(facts, cfg, now)
    else:
        gates = [Gate("monthly_review", GateStatus.MANUAL, "rulebook, drawdown, LLM cost vs P&L",
                      "risk +0.25 pp per month at most")]  # fmt: skip
    return Report(level, facts.period_start, gates)
