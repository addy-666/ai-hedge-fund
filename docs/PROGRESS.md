# Progress Log

Updated by the coding agent at the end of every task.

## Current position

- Phase: 2 — Risk & execution (deterministic baseline strategy)
- Next task: 2.6 Risk Manager
- Rollout level: none (pre-L0). No code path places orders yet (SimBroker can, but nothing calls it).
- Tests: 343 passing + 1 Windows-only (MQL5 constant cross-check against the real MetaTrader5 package).
- Windows run 2026-09-28 (VantageMarkets-Demo, hedging, 1:500, server UTC+3): smoke checks passed; history
  exported. Replay of the real export builds valid snapshots for XAUUSD/EURUSD/BTCUSD.
- Findings from real data: (1) the terminal's first M15 request for EURUSD returned 2024 bars (stale local
  cache) → gateway now retries and raises HistoryNotSynced; (2) a 6-month export has only ~126 D1 bars, so
  D1-context snapshots are refused (need 300) → export `--months 15` for replay; (3) EURUSD spread ≈ 26% of
  M15 ATR on this account vs `max_spread_to_atr: 0.10` — decide in Phase 2 (raw-spread account, higher
  timeframe for EURUSD, or drop it).

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

| 2026-09-28 | 1.1 MT5 gateway | Single worker thread, timeouts, reconnect/backoff, account identity check, startup checks, UTC conversion, pure mapping, read-only smoke script | Offset estimation requires a tick that advances between two samples (a stale tick N×15 min old silently gave a wrong offset — found by a test) | Run smoke script on Windows |
| 2026-09-28 | 1.2 History export | Parquet store (exact round-trip via digit quantisation), chunked export of closed bars, Windows script | — | Run export on Windows |
| 2026-09-28 | 1.3 SimBroker + ReplayFeed | Clock-driven closed-bar feed; MT5-shaped hedging broker with SL/TP walk, partial closes, MT5 retcodes, fault injection | Broker-neutral BrokerError/BrokerUnavailable added to the port | — |
| 2026-09-28 | 1.4 Indicators | EMA, Wilder RSI/ATR/ADX, BB width, MACD hist, z-score, percentile rank, confirmed swings, NR7 | ATR is Wilder's (differs from MT5's SMA-based iATR, documented) | — |
| 2026-09-28 | 1.6 Regime | VOLATILE > TREND > QUIET > RANGE, UNKNOWN on missing input | Done before 1.5 (builder needs it) | — |
| 2026-09-28 | 1.5 Features | Registry (single source of names), validated snapshot builder, idempotent persistence | Mid-rank percentiles and a z-score noise floor (both bugs found while hand-deriving expected values); PDH/PDL moved to ctx; `ctx.htf_trend_score` + `prop.htf_alignment` | — |
| 2026-09-28 | 1.7 Bar clock | One event per closed bar, grace, no retroactive trading after downtime, stale-feed reporting, cursor from decisions table | — | Engine wiring in Phase 5 |

| 2026-09-28 | 2.1 mtf_trend_pullback | Detector faithful to the vault note (rules mapped to D1/H4 → H1 → M15), playbook card with book claims marked UNVALIDATED, feature set v2 (stochastics, EMA50 value-zone distances, close) | Engulfing approximated by a strong body; stochastic threshold 30 (note's code) not 20 (note's text) | On real Vantage data it fires ~0.5/week (XAUUSD) and ~1.5/week (BTCUSD): too few trades for statistical evaluation from a few months of replay |

| 2026-09-28 | 2.2 Stops | ATR-clamped SL from invalidation, RR-banded TP, tick rounding, distances re-derived from rounded levels; property tests | TP rounds away when rounding toward entry would break rr_min (edge case caught by a hand test) | — |

| 2026-09-28 | 2.3 Sizing | Pure Decimal sizing from broker loss-per-lot; confidence/volatility/drawdown/rule factors only scale down; floor to step; reject instead of upsizing to min lot; margin cap; full worksheet | Margin breach rejects (no downsizing to fit) | — |

| 2026-09-28 | 2.4 Limits & exposure | Daily/weekly loss and drawdown breaches (most severe first); max positions, portfolio heat, bucket heat, notional leverage; UTC trading-day/week boundaries | Trading week starts at the Sunday boundary | — |

| 2026-09-28 | 2.5 Guards | Pure duplicate layers (idempotency, in-flight, foreign, same direction, per-symbol cap, cooldown, daily cap) and reversal rules (mode, extra confidence, min hold, daily cap, flip-flop lock); the guard only decides, the executor closes-and-verifies | Per-symbol asyncio lock lives in the pipeline (2.8) | — |

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
| 2026-09-28 | Tests derive expected values by hand, never from memory | A remembered "published" RSI value was wrong; hand derivation caught three real bugs (ADX warm-up, percentile ties, z-score noise) | — |
| 2026-09-28 | `market` is its own layer below `risk`/`rules`/`strategies` | Detectors read market features; siblings in one import-linter layer may not import each other | 01 |
| 2026-09-28 | Executable intents only via `domain._issuance`, import-restricted to `aifund.risk`; copies are never executable | Structural guarantee that LLM output cannot reach the broker without the Risk Manager | AGENTS.md |

## Operational measurements (filled during P4.7, P5.8, soak)

| Metric | Value | Date |
|--------|-------|------|
| LLM cost / day | | |
| Analyst latency p50 / p95 | | |
| Invalid LLM output rate | | |
| Decisions / day, LLM calls / day | | |
| Engine uptime | | |
