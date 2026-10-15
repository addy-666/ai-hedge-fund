# ruff: noqa: S608  -- the only interpolated values are the integer --days and fixed SQL fragments
"""Why is the engine not trading, what are the LLM errors, what has the learning loop done? READ-ONLY.

    cd backend
    uv run python scripts/diagnose.py                       # the live DB (DATABASE_URL), last 14 days
    uv run python scripts/diagnose.py --days 30 > ../diagnose.txt
    uv run python scripts/diagnose.py --db ../backups/aifund-20261015-2130.db.gz

Prints one plain-text report: the order-relevant config flags (no secrets: the YAML holds none, .env is not
read), the engine state and boot events, decision outcomes and their reasons, order intents and trades, virtual
trades per arm with their R, LLM calls per agent with the error texts grouped, the learning loop (audits, rules,
calibration, reviews), the research ledger and evidence files, and the warnings/errors in the engine log.
The database is opened read-only and copied into memory first (SQLite backup API: consistent with the WAL).
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from aifund.config.settings import PROJECT_ROOT, Settings


def section(title: str) -> None:
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")


def table(rows: list[tuple[Any, ...]], head: tuple[str, ...]) -> None:
    if not rows:
        print("  (none)")
        return
    text = [[("" if v is None else str(v)) for v in r] for r in [head, *rows]]
    widths = [min(max(len(r[i]) for r in text), 70) for i in range(len(head))]
    for n, r in enumerate(text):
        print("  " + "  ".join(v[:70].ljust(w) for v, w in zip(r, widths, strict=True)))
        if n == 0:
            print("  " + "  ".join("-" * w for w in widths))


def open_db(path: Path) -> sqlite3.Connection:
    """An in-memory copy of ``path`` (``.gz`` allowed); the file itself is opened read-only."""
    mem = sqlite3.connect(":memory:")
    if path.suffix == ".gz":
        mem.deserialize(gzip.decompress(path.read_bytes()))
        return mem
    src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    src.backup(mem)
    src.close()
    return mem


def q(con: sqlite3.Connection, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    return con.execute(sql, args).fetchall()


def normalise(error: str) -> str:
    """Group error texts that differ only in numbers, ids or quoted values."""
    text = re.sub(r"\d+(\.\d+)?", "#", error)
    text = re.sub(r"'[^']{12,}'", "'…'", text)
    return text[:160]


# ---------------------------------------------------------------- sections


def config_report(path: Path) -> None:
    section(f"CONFIG  {path}")
    if not path.is_file():
        print("  config file not found")
        return
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    eng, strat = cfg.get("engine", {}), cfg.get("strategy", {})
    risk, llm, learning = cfg.get("risk", {}), cfg.get("llm", {}), cfg.get("learning", {})
    flags = [
        ("engine.mode", eng.get("mode")),
        ("engine.demo_orders_without_evidence", eng.get("demo_orders_without_evidence")),
        ("strategy.analyst_enabled", strat.get("analyst_enabled")),
        ("strategy.baseline_enabled", strat.get("baseline_enabled")),
        ("strategy.analyst_orders", strat.get("analyst_orders")),
        ("strategy.dry_run", strat.get("dry_run")),
        ("strategy.analyst_prompt_version", strat.get("analyst_prompt_version")),
        ("strategy.challenger_prompt_version", strat.get("challenger_prompt_version")),
        ("committee.mode", cfg.get("committee", {}).get("mode")),
        ("risk.confidence_threshold", risk.get("confidence_threshold")),
        ("risk.news.enabled", risk.get("news", {}).get("enabled")),
        ("risk.limits", json.dumps(risk.get("limits"), default=str)[:200]),
        ("llm.analyst_model", llm.get("analyst_model")),
        ("llm.auditor_model", llm.get("auditor_model")),
        ("llm.pricing models", sorted((llm.get("pricing") or {}).keys())),
        ("llm.daily_budget_usd", llm.get("daily_budget_usd")),
        ("llm.timeout_s / auditor_timeout_s", f"{llm.get('timeout_s')} / {llm.get('auditor_timeout_s')}"),
        ("learning.enabled", learning.get("enabled")),
        ("learning.calibration.activation", (learning.get("calibration") or {}).get("activation")),
        ("symbols", [s.get("broker") for s in cfg.get("symbols", [])]),
    ]
    table(flags, ("key", "value"))
    mode = str(eng.get("mode"))
    sends = not strat.get("dry_run") and (
        strat.get("baseline_enabled") or (strat.get("analyst_enabled") and strat.get("analyst_orders"))
    )
    print()
    if mode in ("SIM", "PAPER"):
        print(f"  ORDERS: none — engine.mode is {mode}")
    elif not sends:
        print(
            "  ORDERS: none can be sent with these flags. The pipeline sends orders only when strategy.dry_run is\n"
            "  false AND (strategy.baseline_enabled OR (strategy.analyst_enabled AND strategy.analyst_orders)).\n"
            "  With the analyst on, analyst_orders off and the baseline off, every decision ends SHADOW and only\n"
            "  virtual trades are recorded. engine.demo_orders_without_evidence waives the E1/G-LLM start-up checks\n"
            "  but does not switch analyst_orders on."
        )
    else:
        print(f"  ORDERS: possible in {mode} with these flags (then look at the decision outcomes below)")


def engine_report(con: sqlite3.Connection, days: int) -> None:
    section("ENGINE STATE, HEARTBEATS, KEY EVENTS")
    table(q(con, "SELECT account_id, state, mode, halt_reason, rulebook_version, updated_at FROM engine_state"),
          ("account", "state", "mode", "halt_reason", "rulebook", "updated_at"))  # fmt: skip
    print()
    table(q(con, "SELECT component, status, last_beat_at FROM heartbeats ORDER BY component"),
          ("component", "status", "last_beat_at"))  # fmt: skip
    print("\n  events by type and severity (window):")
    table(
        q(con, f"SELECT type, severity, COUNT(*), MAX(ts) FROM events WHERE ts >= datetime('now', '-{days} days') "
               "GROUP BY type, severity ORDER BY COUNT(*) DESC LIMIT 40"),
        ("type", "severity", "n", "last"),
    )  # fmt: skip
    print("\n  the 25 newest WARN/ERROR/CRITICAL events:")
    rows = q(con, "SELECT ts, type, severity, payload FROM events WHERE severity IN ('WARN','WARNING','ERROR','CRITICAL') "
                  "ORDER BY seq DESC LIMIT 25")  # fmt: skip
    table([(ts, t, s, (p or "")[:200]) for ts, t, s, p in rows], ("ts", "type", "severity", "payload"))
    print("\n  commands (newest 15):")
    table(q(con, "SELECT created_at, type, status, substr(COALESCE(result,''),1,120) FROM commands "
                 "ORDER BY created_at DESC LIMIT 15"), ("created_at", "type", "status", "result"))  # fmt: skip


def decisions_report(con: sqlite3.Connection, days: int) -> None:
    window = f"created_at >= datetime('now', '-{days} days')"
    section(f"DECISIONS (last {days} days)")
    table(q(con, f"SELECT symbol, COUNT(*), MIN(created_at), MAX(created_at) FROM decisions WHERE {window} GROUP BY symbol"),
          ("symbol", "n", "first", "last"))  # fmt: skip
    print("\n  outcome x reason:")
    table(q(con, f"SELECT outcome, COALESCE(reason_code,'-'), COUNT(*) FROM decisions WHERE {window} "
                 "GROUP BY outcome, reason_code ORDER BY COUNT(*) DESC"), ("outcome", "reason", "n"))  # fmt: skip
    print("\n  most common details per outcome (numbers folded):")
    rows = q(
        con, f"SELECT outcome, reason_detail FROM decisions WHERE {window} AND outcome NOT IN ('NO_SETUP')"
    )
    grouped = Counter((o, normalise(d or "")) for o, d in rows)
    table([(o, n, d) for (o, d), n in grouped.most_common(30)], ("outcome", "n", "detail"))
    print("\n  stage reached when the outcome is ERROR:")
    table(q(con, f"SELECT stage_reached, COUNT(*) FROM decisions WHERE {window} AND outcome='ERROR' GROUP BY stage_reached"),
          ("stage", "n"))  # fmt: skip
    print("\n  confidence of decisions that got as far as a confidence (final vs threshold):")
    table(q(con, f"SELECT outcome, COUNT(*), ROUND(AVG(llm_confidence),1), ROUND(AVG(final_confidence),1), "
                 f"MAX(final_confidence), ROUND(AVG(penalty_points),1) FROM decisions WHERE {window} "
                 "AND llm_confidence IS NOT NULL GROUP BY outcome"),
          ("outcome", "n", "avg llm conf", "avg final", "max final", "avg penalty"))  # fmt: skip


def orders_report(con: sqlite3.Connection) -> None:
    section("ORDER INTENTS AND TRADES (all time)")
    table(q(con, "SELECT kind, status, COUNT(*), MAX(created_at) FROM order_intents GROUP BY kind, status"),
          ("kind", "status", "n", "last"))  # fmt: skip
    print()
    cols = {r[1] for r in q(con, "PRAGMA table_info(trades)")}
    r_col = "r_multiple" if "r_multiple" in cols else "NULL"
    table(q(con, f"SELECT symbol, COUNT(*), ROUND(AVG({r_col}),3) FROM trades GROUP BY symbol"),
          ("symbol", "trades", "avg R"))  # fmt: skip


def virtual_report(con: sqlite3.Connection, days: int) -> None:
    window = f"created_at >= datetime('now', '-{days} days')"
    finished = "status IN ('CLOSED','EXPIRED')"
    section(f"VIRTUAL TRADES (last {days} days; R per finished trade)")
    table(q(con, f"SELECT arm, status, COUNT(*) FROM virtual_trades WHERE {window} GROUP BY arm, status ORDER BY arm"),
          ("arm", "status", "n"))  # fmt: skip
    print("\n  per arm (finished):")
    table(q(con, f"SELECT arm, COUNT(*), ROUND(AVG(CAST(r_multiple AS REAL)),3), ROUND(SUM(CAST(r_multiple AS REAL)),2), "
                 f"ROUND(AVG(CAST(r_multiple AS REAL) > 0),3) FROM virtual_trades WHERE {window} AND {finished} GROUP BY arm"),
          ("arm", "n", "mean R", "total R", "win rate"))  # fmt: skip
    print("\n  per arm x setup x symbol (finished, n >= 3):")
    table(q(con, f"SELECT arm, setup_tag, symbol, COUNT(*), ROUND(AVG(CAST(r_multiple AS REAL)),3), "
                 f"ROUND(AVG(CAST(r_multiple AS REAL) > 0),3) FROM virtual_trades WHERE {window} AND {finished} "
                 "GROUP BY arm, setup_tag, symbol HAVING COUNT(*) >= 3 ORDER BY arm, AVG(CAST(r_multiple AS REAL))"),
          ("arm", "setup", "symbol", "n", "mean R", "win rate"))  # fmt: skip
    print("\n  BLOCKED arm by what blocked it (finished):")
    table(q(con, f"SELECT blocked_by, COUNT(*), ROUND(AVG(CAST(r_multiple AS REAL)),3) FROM virtual_trades "
                 f"WHERE {window} AND {finished} AND arm='BLOCKED' GROUP BY blocked_by ORDER BY COUNT(*) DESC"),
          ("blocked_by", "n", "mean R"))  # fmt: skip
    print("\n  exits (finished):")
    table(q(con, f"SELECT arm, exit_reason, COUNT(*), ROUND(AVG(CAST(r_multiple AS REAL)),3) FROM virtual_trades "
                 f"WHERE {window} AND {finished} GROUP BY arm, exit_reason ORDER BY arm"),
          ("arm", "exit", "n", "mean R"))  # fmt: skip


def llm_report(con: sqlite3.Connection, days: int) -> None:
    window = f"created_at >= datetime('now', '-{days} days')"
    section(f"LLM CALLS (last {days} days) — 'Errors' on the Agents page = calls with valid = 0")
    table(q(con, f"SELECT agent, model, COUNT(*), SUM(valid=0), ROUND(SUM(CAST(cost_usd AS REAL)),4), "
                 f"CAST(AVG(latency_ms) AS INT), MAX(latency_ms) FROM llm_calls WHERE {window} GROUP BY agent, model"),
          ("agent", "model", "calls", "invalid", "cost $", "avg ms", "max ms"))  # fmt: skip
    print("\n  invalid calls by agent and error text (numbers folded):")
    rows = q(
        con, f"SELECT agent, COALESCE(error, '(no error text)') FROM llm_calls WHERE {window} AND valid=0"
    )
    grouped = Counter((a, normalise(e)) for a, e in rows)
    table([(a, n, e) for (a, e), n in grouped.most_common(30)], ("agent", "n", "error"))
    print("\n  per day:")
    table(q(con, f"SELECT date(created_at), COUNT(*), SUM(valid=0), ROUND(SUM(CAST(cost_usd AS REAL)),4) FROM llm_calls "
                 f"WHERE {window} GROUP BY date(created_at) ORDER BY 1"), ("day", "calls", "invalid", "cost $"))  # fmt: skip
    print("\n  three newest invalid calls, with the start of the model's reply:")
    for agent, model, error, text, ts in q(
        con, "SELECT agent, model, error, response_text, created_at FROM llm_calls WHERE valid=0 "
             "ORDER BY created_at DESC LIMIT 3"
    ):  # fmt: skip
        print(
            f"  - {ts} {agent} {model}\n    error: {(error or '')[:400]}\n    reply: {(text or '')[:400]!r}"
        )
    print("\n  decisions that ended INVALID (analyst unusable) by reason:")
    table(q(con, f"SELECT reason_code, COUNT(*) FROM decisions WHERE {window} AND outcome='INVALID' GROUP BY reason_code"),
          ("reason", "n"))  # fmt: skip


def learning_report(con: sqlite3.Connection) -> None:
    section("LEARNING LOOP (all time)")
    print("  audit runs (newest 12):")
    rows = q(con, "SELECT created_at, trigger, status, n_trades, n_virtual, candidates, validation, finished_at "
                  "FROM audit_runs ORDER BY created_at DESC LIMIT 12")  # fmt: skip
    out = []
    for created, trig, status, nt, nv, cands, val, fin in rows:
        n_c = len(json.loads(cands)) if cands else 0
        v = json.loads(val) if val else {}
        passed = (
            sum(1 for x in v.values() if isinstance(x, dict) and x.get("passed"))
            if isinstance(v, dict)
            else "?"
        )
        out.append((created, trig, status, nt, nv, n_c, passed, fin))
    table(out, ("created", "trigger", "status", "real", "virtual", "candidates", "passed", "finished"))
    print("\n  rules by status and origin:")
    table(
        q(con, "SELECT status, origin, COUNT(*) FROM rules GROUP BY status, origin"),
        ("status", "origin", "n"),
    )
    print("\n  live rules (ACTIVE/SHADOW):")
    rows = q(con, "SELECT rule_id, version, status, dsl, created_at FROM rules WHERE status IN ('ACTIVE','SHADOW') "
                  "ORDER BY status, created_at DESC LIMIT 25")  # fmt: skip
    table([(r, v, s, (d or "")[:90], c) for r, v, s, d, c in rows], ("rule", "v", "status", "dsl", "created"))
    print("\n  newest rejections (why candidates failed the validator):")
    rows = q(
        con,
        "SELECT rule_id, evidence, created_at FROM rules WHERE status='REJECTED' ORDER BY created_at DESC LIMIT 10",
    )
    table([(r, (e or "")[:140], c) for r, e, c in rows], ("rule", "evidence", "created"))
    print("\n  calibration models:")
    table(q(con, "SELECT version, source, status, n_samples, brier_before, brier_after, created_at FROM calibration_models "
                 "ORDER BY created_at DESC LIMIT 10"),
          ("v", "source", "status", "n", "brier before", "after", "created"))  # fmt: skip
    print(f"\n  trade reviews: {q(con, 'SELECT COUNT(*) FROM trade_reviews')[0][0]}")


def research_report() -> None:
    section("RESEARCH LEDGER AND EVIDENCE")
    ledger = PROJECT_ROOT / "data" / "research" / "ledger.jsonl"
    if not ledger.is_file():
        print(f"  no ledger at {ledger}: research has never run on this machine")
    else:
        lines = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines() if x.strip()]
        print(
            f"  {len(lines)} trials; newest: {max((str(x.get('created_at', '')) for x in lines), default='?')}"
        )
        print(f"  by origin: {dict(Counter(str(x.get('origin')) for x in lines))}")
        print(f"  by split: {dict(Counter(str(x.get('split')) for x in lines))}")
        wf = sorted(
            (x for x in lines if x.get("split") == "walk_forward"), key=lambda x: -float(x.get("mean") or 0)
        )
        print("\n  best walk-forward trials (out-of-sample mean R):")
        table(
            [(str(x.get("created_at", ""))[:10], x.get("origin"), json.dumps(x.get("hypothesis"))[:60], x.get("n"),
              round(float(x.get("mean") or 0), 3), round(float(x.get("p_value") or 1), 4)) for x in wf[:10]],
            ("date", "origin", "hypothesis", "n", "mean R", "p"),
        )  # fmt: skip
    evidence = PROJECT_ROOT / "config" / "evidence"
    files = sorted(p.name for p in evidence.glob("*.json")) if evidence.is_dir() else []
    print(f"  evidence files in {evidence}: {files or 'none'}")
    reports = sorted((PROJECT_ROOT / "data" / "research").glob("*.json"))
    print(f"  research reports: {len(reports)}; newest: {reports[-1].name if reports else '-'}")


def log_report(days: int) -> None:
    path = PROJECT_ROOT / "logs" / "engine.jsonl"
    section(f"ENGINE LOG WARNINGS/ERRORS  {path} (today's file only)")
    if not path.is_file():
        print("  not found")
        return
    counts: Counter[tuple[str, str]] = Counter()
    sample: dict[tuple[str, str], str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        level = str(rec.get("level", ""))
        if level not in ("warning", "error", "critical"):
            continue
        key = (level, str(rec.get("event")))
        counts[key] += 1
        sample[key] = json.dumps(
            {k: v for k, v in rec.items() if k not in ("component", "logger")}, default=str
        )[:300]
    table(
        [(lvl, ev, n, sample[(lvl, ev)]) for (lvl, ev), n in counts.most_common(30)],
        ("level", "event", "n", "last"),
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", help="a .db or .db.gz (default: DATABASE_URL)")
    p.add_argument("--days", type=int, default=14)
    args = p.parse_args()
    settings = Settings()
    db = Path(args.db) if args.db else Path(settings.DATABASE_URL.removeprefix("sqlite:///"))
    print(f"diagnose: db={db}  days={args.days}")
    config_report(settings.CONFIG_PATH)
    con = open_db(db)
    for fn in (engine_report, decisions_report, virtual_report, llm_report):
        try:
            fn(con, args.days)
        except sqlite3.Error as exc:
            print(f"  ({fn.__name__} failed: {exc})")
    for fn0 in (orders_report, learning_report):
        try:
            fn0(con)
        except sqlite3.Error as exc:
            print(f"  ({fn0.__name__} failed: {exc})")
    research_report()
    log_report(args.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
