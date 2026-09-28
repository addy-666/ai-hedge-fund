# Progress Log

Updated by the coding agent at the end of every task.

## Current position

- Phase: 1 — Market data & features
- Next task: 1.1 MT5 gateway
- Rollout level: none (pre-L0). Nothing in this repo can place an order yet.
- Tests: 189 passing (unit + integration against a migrated SQLite file). CI defined but not yet run
  (repo not pushed at the time of writing).

## Completed tasks

| Date | Task | Summary | Deviations from spec | Follow-ups |
|------|------|---------|----------------------|------------|
| 2026-09-28 | 0.1 Repo restructure | Prototype moved to `legacy/` unchanged; target layout created | — | — |
| 2026-09-28 | 0.2 Backend tooling | uv project, Python 3.12, ruff, mypy (strict on money-path packages), pytest, import-linter with 4 contracts verified against planted violations | Deps added only when first used (not the full §11 list up front) | — |
| 2026-09-28 | 0.3 CI | GitHub Actions: ubuntu (UTC), ubuntu (TZ=Asia/Kolkata), windows installing the MetaTrader5 wheel | Non-UTC run added (borrowed from TradingAgents) | Verify first run after push |
| 2026-09-28 | 0.4 Config | `Settings` (.env, SecretStr) and `TradingConfig` (YAML, extra keys forbidden, frozen, cross-field validators); loader parses floats as Decimal and rejects duplicate YAML keys; example config | `BROKER=mt5` requires explicit login/password/server; `limits.max_notional_leverage` added | — |
| 2026-09-28 | 0.5 Domain | Enums, Decimal helpers, market/broker models, strict `TradeProposal`, `OrderIntent` with issuance token, idempotency key, intent state machine | `DecisionOutcome.INVALID` added; `LEVERAGE_CAP` reason | — |
| 2026-09-28 | 0.6 Ports | Broker, market-data, LLM, clock, notifier Protocols; SystemClock, FakeClock, NullNotifier; `OrderRequest` | `LLMResponse.model` records the model the provider reports | — |
| 2026-09-28 | 0.7 Persistence | 21 tables, `DecimalText`/`UtcDateTime` types, WAL pragmas, initial Alembic migration with zero-drift test, repositories for intents and control tables | FK cycles removed (`decisions.intent_id`, `audit_runs.llm_call_id`); `llm_calls.model_reported`, `equity_snapshots.open_notional` added | Repositories for trades, deals, decisions, rules etc. arrive with the phases that use them |
| 2026-09-28 | 0.8 Logging | structlog JSON lines, daily-rotated files, redaction by key and by registered secret value (incl. exception text), correlation ids | Module is `aifund/observability.py` | — |
| 2026-09-28 | 0.9 AGENTS/PROGRESS | Every "working now" command in AGENTS.md executed; not-yet-available commands marked with their phase | `make_engine` creates the SQLite parent directory | — |

| 2026-09-28 | PR #1 review fixes | Self-review found 10 issues, all fixed: idempotency key normalised to UTC; log redaction by key segment (token counts no longer hidden); settings paths resolve from the repo root; events.seq uses SQLite AUTOINCREMENT; intent distances must match SL/TP; non-finite YAML numbers -> ConfigError; no scientific notation from rounding; config `latest()` ordered by time; bars_per_tf >= 300; market/strategies import contract | Initial migration edited in place (never deployed) | — |

## Decisions log

| Date | Decision | Reason | Docs updated |
|------|----------|--------|--------------|
| 2026-09-28 | Rebuild rather than refactor the prototype; prototype moves to `legacy/` | See `08_PROTOTYPE_AUDIT.md` | 00–08 |
| 2026-09-28 | Python 3.12 | `MetaTrader5` 5.0.6231 ships cp312 Windows wheels; the prototype's pinned 5.0.45 stops at cp311 | AGENTS.md |
| 2026-09-28 | Money in SQLite stored as exact decimal text (`DecimalText`), native NUMERIC on Postgres | SQLAlchemy `Numeric` on SQLite round-trips through binary float | AGENTS.md, persistence/types.py |
| 2026-09-28 | Synchronous repositories, called via `asyncio.to_thread` | Simpler, fully testable; SQLite has a single writer anyway | AGENTS.md |
| 2026-09-28 | `INVALID` decision outcome separate from `HOLD` | Failures must not pollute HOLD stats or calibration (audit Q100b; TradingAgents' REVIEW sentinel) | 02, 03 |
| 2026-09-28 | Notional-leverage cap independent of broker margin | High-leverage accounts make margin checks meaningless (audit Q100c) | 02, 03 |
| 2026-09-28 | Explicit MT5 account required | Prototype traded whatever account the terminal was logged into when MT5_LOGIN was empty (audit Q87) | 02 |
| 2026-09-28 | Executable intents only via `domain._issuance`, import-restricted to `aifund.risk`; copies are never executable | Structural guarantee that LLM output cannot reach the broker without the Risk Manager | AGENTS.md |

## Operational measurements (filled during P4.7, P5.8, soak)

| Metric | Value | Date |
|--------|-------|------|
| LLM cost / day | | |
| Analyst latency p50 / p95 | | |
| Invalid LLM output rate | | |
| Decisions / day, LLM calls / day | | |
| Engine uptime | | |
