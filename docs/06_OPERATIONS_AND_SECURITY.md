# 06 — Operations, Security, Rollout

## 1. Host

- **Windows Server 2022 VPS**, 4 vCPU / 8 GB RAM / 80 GB SSD, in a region close to the broker's trade server
  (check ping to the MT5 server from the terminal's connection menu).
- Dedicated local user `aifund` with **auto-logon**. MT5 is a GUI application: run it in this interactive
  session, not as a session-0 service. When leaving RDP, *disconnect*; never *sign out*.
- Windows Update: set active hours and schedule restarts in the weekend window when FX/metals/indices are
  closed; crypto symbols are paused by the engine during the maintenance window (`FLATTEN`/PAUSE task).
- Time: Windows Time service syncing to NTP (`w32tm /resync`); engine alerts when clock drift > 2 s.
- Install: Python (version matching the `MetaTrader5` wheel), `uv`, Git, MT5 terminal (broker build) in
  **portable** mode under `C:\aifund\mt5\`, Tailscale.

## 2. Process supervision

| Task (Task Scheduler, at logon of `aifund`, restart on failure every 1 min, unlimited) | Command |
|---|---|
| `aifund-mt5` | `C:\aifund\mt5\terminal64.exe /portable` |
| `aifund-engine` | `deploy\windows\run_engine.ps1` → `uv run python -m aifund.engine` (waits for terminal) |
| `aifund-api` | `deploy\windows\run_api.ps1` → `uv run uvicorn aifund.api.app:app --host <tailscale-ip> --port 8000` |
| `aifund-backup` | Daily 21:30 UTC: `deploy\windows\backup.ps1` |

- `run_engine.ps1` loops: start engine; on exit, log exit code, wait with backoff (5 s → 5 min), restart.
- Engine start always lands in `PAUSED` after an unclean exit; auto-resume to `RUNNING` only if
  `engine.auto_resume_after_crash: true` **and** the reconciler passes **and** no HALT is active.
- External dead-man switch: engine pings `HEALTHCHECKS_URL` every 60 s; healthchecks.io alerts (Telegram/email)
  after 3 missed pings. This catches "the whole VPS is dead", which nothing on the VPS can report.

## 3. MT5 terminal configuration

- Tools → Options → Expert Advisors: **Allow algorithmic trading** on; disable "Disable algorithmic trading when
  the account has been changed / profile changed" only if you understand the implication.
- Log in once manually and save the password so the terminal reconnects after restarts.
- Keep only the needed symbols in Market Watch (reduces load); the engine `symbol_select`s its own.
- Attach **Guardian EA** to one chart (any symbol).

## 4. Guardian EA (MQL5, `mql5/GuardianEA.mq5`) — independent safety net

Runs inside the terminal, independent of Python. Every second (`OnTimer`):

1. **Hard daily loss limit** (default 1 percentage point wider than the engine's soft limit, e.g. 4%): if
   equity ≤ day-start equity × (1 − limit) → close all positions with the engine magic, write
   `Common\Files\aifund\halt.flag` with reason. The engine checks this flag before every order and every 5 s,
   and moves to `HALTED`.
2. **Hard max drawdown** from stored peak (e.g. 12%) → same action.
3. **Python heartbeat watch**: engine writes `Common\Files\aifund\heartbeat.txt` every 10 s. If stale for more
   than `N` minutes (default 10) → ensure every engine position has an SL (attach `k × ATR` if missing); if
   configured, close positions older than their time stop. Alerts via push notification (`SendNotification`)
   to the MT5 mobile app.
4. **Economic calendar export** (hourly): `CalendarValueHistory` for the next 7 days, HIGH/MEDIUM impact, for
   the currencies of configured symbols → `Common\Files\aifund\calendar.csv` (UTC times). Feeds the news gate.

The engine resolves the `Common\Files` path via `terminal_info().commondata_path`.

## 5. Secrets

- `.env` on the VPS only, NTFS ACL restricted to `aifund` and Administrators. Never committed; `.env.example`
  documents keys.
- Logs: structlog processor redacts keys matching `*key*`, `*password*`, `*token*`, `*secret*`.
- The DeepSeek key gets a spend limit on the provider side where available, in addition to the engine budget.
- Rotation runbook: new key in `.env` → `RELOAD_CONFIG` is not enough for secrets → restart engine in a quiet
  period (PAUSE first).

## 6. Security checklist

- [ ] Dashboard/API reachable only via Tailscale (Windows firewall blocks 8000 on public NIC).
- [ ] RDP restricted (Tailscale only, or IP allowlist) with strong password + NLA.
- [ ] Login rate-limited; session cookies Secure/HttpOnly/SameSite=Strict; CSRF on mutations.
- [ ] Re-auth for LIVE, risk increases, drawdown re-arm, block-rule approval.
- [ ] No endpoint can place an arbitrary order; the only trading mutations are close/pause/flatten commands.
- [ ] Prompt-injection surface: LLM prompts contain only numeric market data, curated playbooks and the engine's
      own rules — no external free text (news headlines, web content) in v1. If headlines are added later,
      they go in a clearly delimited data block and cannot change the output schema or bypass the Risk Manager.
- [ ] Dependency audit (`pip-audit`, `npm audit`) in CI.
- [ ] Backups encrypted at rest in the remote store.

## 7. Observability

- **Logs:** JSON lines, `logs/engine.jsonl`, `logs/api.jsonl`, rotated daily, 30 days retained. Always include
  `component`, `symbol`, `decision_id`, `intent_id`, `position_id` where applicable.
- **Alerts (Telegram):**
  - critical: HALTED, FLATTEN executed, orphan position, UNKNOWN intent unresolved, position without SL that
    could not be fixed, MT5 disconnected > 60 s, engine restart, Guardian halt flag, backup failed.
  - warn: order rejected (unexpected retcode), LLM circuit open, budget 80%, stale feed on an open market,
    clock drift, rule needs approval.
  - info: trade opened/closed (compact), daily summary (P&L, R, trades, rule changes, LLM cost).
- **Metrics** exposed on `/api/system` (and optionally Prometheus `/metrics` later): loop latencies, decision
  funnel counts, LLM latency/cost/error, gateway queue depth, reconcile lag.

## 8. Backups & data retention

- Nightly SQLite online backup (`sqlite3.Connection.backup`) to `backups/aifund-YYYYMMDD.db`, compressed,
  uploaded with `rclone` to cloud storage; keep 30 daily + 12 monthly.
- Backup before every Alembic migration (the migration script does it automatically).
- Monthly restore test on the Mac (open the DB, run `scripts/verify_db.py` consistency checks).
- Retention: `events` 30 days; `llm_calls.messages` full text 180 days then truncated (keep hashes/metadata);
  everything else forever (it is the learning dataset).

## 9. Runbooks (short form — expand in `docs/runbooks/` as incidents happen)

| Situation | Steps |
|---|---|
| Engine HALTED by daily limit | Review the day in Journal → do nothing; engine re-arms to PAUSED at the next trading day boundary; RESUME manually |
| HALTED by drawdown | PAUSE thinking: review Learning Lab + analytics; lower risk or stop; REARM requires re-auth |
| Orphan position alert | Identify source (manual trade? restart race?). Decide: keep (engine manages SL only) or close from dashboard |
| UNKNOWN intent not resolved | Check MT5 terminal Journal/History for the comment code; close duplicates manually; mark intent resolved via admin script; file a bug with logs |
| MT5 disconnected | Check VPS network/broker status; restart `aifund-mt5` task; engine resumes after reconcile |
| LLM outage | Nothing: engine HOLDs. Optional: switch to deterministic baseline strategy mode (`strategy.analyst_enabled=false`, `strategy.baseline_enabled=true`) |
| Bad rule suspected | Retire it in Learning Lab; rulebook version increments; decisions after that show the change |
| Moving to a new VPS | PAUSE → FLATTEN (or let positions close) → STOP → backup DB → restore on new host → START in PAUSED → reconcile → RESUME |

## 10. Rollout ladder & gates

| Level | Environment | Minimum duration | Exit gate (all required) |
|---|---|---|---|
| L0 SIM | Mac, SimBroker + replay | until tests green | Scenario suite green (restart/idempotency/fault injection); replay of 3 months × all symbols with zero invariant violations |
| L1 PAPER | VPS, live MT5 data, SimBroker fills | 1–2 weeks | 99.5% uptime; decision funnel sane; LLM cost within budget; no stale-data trades |
| L2 DEMO | VPS, demo account real orders | ≥ 4 weeks and ≥ 100 closed trades | 0 duplicate orders; 0 unreconciled/lost trades; 0 positions without SL > 10 s; all closes have correct net P&L vs MT5 history (automated diff); every closed trade has snapshot + decision; kill switch tested; restart during open positions tested |
| L3 LIVE-MICRO | Live account, `risk_per_trade_pct ≤ 0.25`, minimum lots | ≥ 4 weeks | Same as L2 + live costs within 20% of demo assumptions; expectancy net of costs ≥ 0 over the period (with honest CI shown) |
| L4 LIVE | Gradual risk increases (e.g. +0.25 pp per month max) | ongoing | Monthly review: rulebook changes, drawdown vs expectations, LLM cost vs P&L |

Moving up a level is an operator decision recorded in `audit_log`. Moving *down* (e.g. LIVE → DEMO) after a
serious incident is always allowed and never requires re-auth.
