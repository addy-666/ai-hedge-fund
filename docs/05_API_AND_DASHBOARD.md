# 05 — API & Dashboard

## 1. API principles

- FastAPI app in its own process; **reads** from the DB, **writes** only `commands`, `config_versions`,
  `audit_log`, rule approval commands and auth/session data. It never talks to MT5 or DeepSeek.
- All responses are Pydantic models → OpenAPI → `openapi-typescript` → frontend types (generated in CI; a stale
  generated file fails the build).
- Pagination: cursor-based (`?cursor=<ulid>&limit=50`). Filters as query params. Times in UTC ISO-8601.
- Mutations return `202 Accepted` with a `command_id` when they are executed by the engine; the UI follows the
  command via the WebSocket (`command.updated`) or `GET /api/commands/{id}`.

## 2. Authentication & authorisation

- Single operator account (v1). Password hash (argon2) in `.env` (`ADMIN_PASSWORD_HASH`); optional TOTP.
- Session: HttpOnly, Secure, SameSite=Strict cookie with a signed session id; 12 h expiry; CSRF token header
  for mutations.
- **Re-auth required** (password within the last 5 minutes) for: switching to LIVE, raising risk parameters,
  REARM after drawdown halt, force-activating rules, approving `block` rules.
- `FLATTEN_ALL` and `PAUSE` are deliberately *not* re-auth gated (safety actions must be fast).
- Rate limit login (5/min). Log every mutation in `audit_log` with IP and before/after.
- Bind to the Tailscale interface / `127.0.0.1` behind it; never `0.0.0.0` on a public interface. CORS: the
  dashboard origin only.

## 3. Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/login`, `/api/auth/logout`, `/api/auth/reauth` | Session management |
| GET | `/api/me` | Current user, re-auth freshness, the session's CSRF token (a reloaded page needs it) |
| GET | `/api/health` | Unauthenticated liveness (no data) |
| GET | `/api/system` | Engine state & mode, heartbeats with ages, MT5 connection, server-time offset, LLM circuit state, budget used, error counts, versions (config, rulebook, prompts, feature set) |
| POST | `/api/engine/commands` | `{type: START\|PAUSE\|RESUME\|STOP\|REARM\|FLATTEN_ALL\|SET_MODE, payload}` |
| GET | `/api/commands/{id}` | Command status/result |
| GET | `/api/account` | Balance, equity, margin, day P&L, drawdown, open risk/heat, limits and headroom |
| GET | `/api/equity?from&to&granularity` | Equity curve from `equity_snapshots` |
| GET | `/api/positions` | Open engine + orphan positions with live P&L (from last reconcile), SL/TP, R so far |
| POST | `/api/positions/{position_id}/close` | Command `CLOSE_POSITION` |
| GET | `/api/trades?symbol&setup&outcome&from&to&cursor` | Closed trades list |
| GET | `/api/trades/{id}` | Full trade dossier: decision, snapshot, LLM calls (prompt/response), rules matched, risk worksheet, deals, review, MAE/MFE, chart bars with markers |
| GET | `/api/decisions?symbol&outcome&reason&from&to&cursor` | Decision feed (incl. SKIPPED/HOLD) |
| GET | `/api/decisions/{id}` | Decision dossier |
| GET | `/api/virtual-trades?...` | Counterfactuals |
| GET | `/api/rules?status` / `/api/rules/{rule_id}` | Rules with versions and evidence; matched decisions |
| POST | `/api/rules/{rule_id}/approve\|reject\|retire` | Commands; `approve?force=true` activates a CANDIDATE without validation (audit-logged); approving a block or forcing needs re-auth (§2) |
| POST | `/api/rules` | Operator-authored rule (DSL) → CANDIDATE |
| GET | `/api/rulebook/versions` / `/api/rulebook/versions/{v}/diff` | Rulebook history and diffs |
| GET | `/api/audits` / `/api/audits/{id}` | Audit runs with miner output, candidates, validation results, report |
| POST | `/api/audits/run` | Command `RUN_AUDIT` |
| GET | `/api/analytics/summary?from&to` | Net P&L, R, expectancy, win rate, profit factor, max DD, Sharpe (daily), trades/day, avg costs |
| GET | `/api/analytics/breakdown?dim=symbol\|setup_tag\|session\|regime\|direction\|confidence_bucket\|rulebook_version` | Expectancy tables |
| GET | `/api/analytics/calibration` | Reliability bins, Brier history, active calibration model |
| GET | `/api/analytics/costs` | Spread/commission/swap/slippage totals and per trade |
| GET | `/api/llm/usage?from&to` | Calls, tokens, cache hit rate, cost, latency p50/p95, error rate per agent/model |
| GET | `/api/bars?symbol&tf&from&to` | Bars for charts, from `bar_cache` (the pipeline upserts the closed bars it reads; the API never asks MT5) |
| GET | `/api/config` / PUT `/api/config` | Current YAML + schema; PUT validates, versions, audit-logs, sends `RELOAD_CONFIG` |
| GET | `/api/config/versions` | History and diffs |
| GET | `/api/exports/vault` / `/api/exports/vault/{name}` | Weekly vault review files |
| GET | `/api/features` | Rule-usable registry features (name, type, unit, categories, bounds) for the rule editor |
| GET | `/api/logs?level&component&since` | Tail of structured logs (read-only) |
| WS | `/api/ws` | Live events (below) |

### WebSocket events

Server pushes `{seq, ts, type, severity, payload}`; client resumes with `?since=<seq>` after reconnect.
`?since=-1` starts from now: the first message is `stream.start` with the latest `seq` (the page has just
loaded its data fresh). The socket authenticates with the session cookie (close code 1008 without one).
Implemented so far (Phases 6–7): `engine.state`, `command.updated`, `rule.status_changed`, `trade.*` (opened, orphan, partial_close,
closed, vanished), `risk.limit_breach`; the rest arrive with the phases that produce them.

`engine.state_changed`, `heartbeat`, `decision.created`, `intent.updated`, `trade.opened`, `trade.updated`
(throttled 1/s), `trade.closed`, `rule.status_changed`, `audit.finished`, `command.updated`,
`alert` (severity info/warn/critical), `equity.tick` (throttled 5 s).

## 4. Dashboard (React) — pages

Design: dark, dense, "operations terminal" style; every number links to its evidence. Mobile-friendly for
Overview, Positions and the kill switch.

1. **Overview**
   - Header bar (sticky, all pages): engine state pill, mode badge (SIM/PAPER/DEMO/LIVE in red), heartbeat age,
     MT5 status, LLM status, **PAUSE** and **FLATTEN ALL** buttons (confirm dialog, no re-auth).
   - KPI tiles: equity, day P&L (money and R), drawdown vs limit, portfolio heat vs limit, open positions,
     today's trades, LLM spend vs budget.
   - Equity curve (with rulebook-version change markers), today's decision funnel
     (bars processed → setups → LLM calls → ordered), latest alerts.
2. **Positions** — live table: symbol, side, volume, entry, SL, TP, R now, MAE/MFE so far, age, setup, thesis
   (expand), close button. Mini chart with entry/SL/TP lines.
3. **Decisions** — feed with filters by outcome/reason/symbol. Row expands to the pipeline trace: gates →
   setups → proposal → rules matched (active + shadow) → confidence math → risk worksheet → execution.
   Links to prompt/response.
4. **Trade Journal** — closed trades table + detail dossier: price chart (lightweight-charts) with entry/exit
   markers and SL/TP lines, snapshot features, thesis vs reviewer verdict and tags, costs breakdown, deals.
5. **Learning Lab**
   - Rules board: columns CANDIDATE / SHADOW / ACTIVE / RETIRED / REJECTED; card shows DSL in readable form,
     action, evidence (n, mean R matched vs baseline, CI, holdout), shadow progress, next review date.
   - Rule detail: evidence charts, matched decisions (real and virtual), version history, approve/reject/retire.
   - Audit runs: miner clusters table, auditor candidates with validator verdicts, lessons report.
   - Rule editor for operator-authored rules (DSL form with feature autocomplete from the registry).
   - Rulebook version diff.
6. **Analytics** — breakdown tables/charts by dimension; calibration reliability diagram; costs; real vs virtual
   comparison per setup; expectancy by rulebook version (did learning help?).
7. **Agents & LLM** — per-agent call counts, latency, error rate, cost, cache-hit rate; prompt versions; browse
   individual calls.
8. **Settings** — config editor with schema validation and diff preview; config history; mode switch (re-auth);
   symbol list; risk limits (re-auth to raise).
9. **System** — heartbeats per loop, error budgets, server-time offset, gateway queue depth, disk space,
   backup status, log tail.

Phase 6 built pages 1–4 and 6–9 and Phase 7 the Learning Lab (5); the calibration diagram comes with Phase 8
(8.5). The trade dossier shows the reviewer's verdict, tags and lesson. Engine controls (START / RESUME / STOP / REARM, with re-auth where §2 requires it) are on the
System page. Components are a few own Tailwind primitives (`components/ui.tsx`), not shadcn/ui.

Frontend structure:

```text
frontend/src/
  api/          generated types (openapi.d.ts), client.ts (fetch wrapper with CSRF), queries/*.ts (TanStack)
  ws/           socket.ts (reconnect + since=seq resume), useEvent hooks that invalidate queries
  components/   KpiTile, StatePill, KillSwitch, PriceChart, EquityChart, RuleCard, DslView, DataTable, ...
  pages/        Overview, Positions, Decisions, Journal, LearningLab, Analytics, Agents, Settings, System, Login
  lib/          format (money, R, pips), time (UTC ↔ display TZ)
```

Frontend tests: Vitest + React Testing Library for components with logic (DSL view, confidence math display,
kill switch confirm flow); Playwright smoke test against the API running with SimBroker and seeded data.
