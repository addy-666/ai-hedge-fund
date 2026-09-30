# Go-live runbook (Phase 9, docs/06 §10)

Everything in Phase 9 that runs on the Windows host or needs a human. Do the steps in order; each ends in a
record (PROGRESS.md, `audit_log`, or the System page).

## 1. Install the new scheduled tasks

```powershell
powershell -ExecutionPolicy Bypass -File C:\aifund\ai-hedge-fund\deploy\windows\install_tasks.ps1
```

Adds `aifund-ledger` (nightly ledger check, 9.3) and `aifund-research` (weekly research, 8.7). Test the ledger
alert once: `uv run python scripts/verify_ledger.py --fixture tests/fixtures/mt5_deals/<file>.json --alert`
against a database that lacks one of the fixture's trades → a CRITICAL "Ledger mismatch" alert, the System
page shows `job.verify_ledger` = diff; the next nightly run turns it back to ok.

## 2. Security (9.4, docs/security_review.md)

1. RDP: NLA on, 3389 allowed only from the tailnet (`100.64.0.0/10`), a 16+ character password.
2. Firewall: `New-NetFirewallRule -DisplayName "aifund api deny" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Block`;
   check from a phone off the tailnet that `http://<public-ip>:8000` does not answer.
3. Backups: `rclone config` → a `crypt` remote (e.g. `secret:`) over the cloud remote; user variable
   `AIFUND_BACKUP_REMOTE=secret:aifund`; the crypt password in your password manager.
4. If the dashboard's live feed stops after the upgrade (log `ws.origin_refused`), add
   `API_ALLOWED_ORIGINS=https://<machine>.<tailnet>.ts.net` to `.env` and restart `aifund-api`.

## 3. Guardian EA re-test on the final build (9.1)

1. Recompile `GuardianEA.mq5` (MetaEditor, F7) and re-attach it.
2. Forced drawdown: on the demo, set `InpHardDailyLossPct` just below the current day's loss (or run the
   Strategy Tester scenario of 5.7a) → the EA closes the engine positions, writes `halt.flag`, the engine is
   HALTED within 5 s, REARM is refused until the flag is deleted.
3. Heartbeat loss: with a position open, `Stop-ScheduledTask aifund-engine` (and stop the python process) for
   longer than `InpHeartbeatStaleMinutes`; remove the position's SL by hand first → the EA attaches a k × ATR stop
   and pushes a notification. Start the engine again: it lands PAUSED and reconciles.
4. Record date, build and results in PROGRESS.md.

The engine side of both is covered by the chaos suite (halt flag under kills, restarts keep the cause).

## 4. Backup / restore drill (9.5)

Runbook `install.md` §7, on the Mac, monthly; the first one now. Record it in PROGRESS.md.

## 5. The L2 soak (9.6)

Run DEMO with real orders (`engine.mode: DEMO`, `strategy.dry_run: false`) for at least 4 weeks and 100
closed trades. During it, deliberately: press FLATTEN_ALL once (the kill switch gate) and restart the engine
once while a position is open (the restart gate). The System page → "Rollout gates" shows every gate from the
engine's own records; when all are PASS, sign off L2 → L3 (fresh password, a note). The sign-off is written to
`audit_log`.

## 6. Go-live L3 (9.7) — only with evidence

Only strategies that passed E1 **and** E2 (docs/09 §7) may trade; the analyst only after G-LLM. Then:
`engine.mode: LIVE`, `engine.allow_live: true`, `risk.risk_per_trade_pct: 0.25` (or less), the SET_MODE LIVE
confirmation (re-auth). The System page then tracks the L3 gates (the L2 set plus micro risk, live costs per lot
within 20% of the DEMO period, expectancy net of costs ≥ 0 with its CI). Moving back down is always allowed.
