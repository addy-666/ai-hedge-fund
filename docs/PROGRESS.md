# Progress Log

Updated by the coding agent at the end of every task.

## Current position

- Phase: 3 — Reconciliation & ledger
- Next task: 3.4 Virtual trade tracker (3.2's real demo deal capture still open, see PR #6)
- Rollout level: L0 (SIM). The full stack trades the SimBroker in replay; nothing is wired to MT5 order sending outside tests yet.
- Tests: 555 passing + 1 Windows-only (MQL5 constant cross-check against the real MetaTrader5 package).
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

| 2026-09-28 | 2.6 Risk Manager | Sole issuer of executable intents: loss limits → threshold → guards → price drift → ATR stops → broker-calculated sizing → exposure → intent; reversals issue only a close intent (own idempotency key); broker errors fail closed | Notional per lot = broker margin × account leverage | — |

| 2026-09-28 | 2.7 Executor | Persist-before-act state machine (SENT committed before order_send), filling selection, order_check, bounded retries for requote-type codes only, PAUSE codes, UNKNOWN resolution by comment then structural match, startup recovery; crash at each of 5 stages → zero duplicates, zero lost trades | PENDING→REJECTED and RETRYING→REJECTED on recovery (a crash between retries would otherwise have crashed recovery itself — found in review); post-fill SL repair/adjust belongs to the position manager (2.9), which issues MODIFY_SLTP intents | — |

| 2026-09-28 | 2.8 Baseline pipeline | Pre-flight → snapshot → detector → deterministic decision (confidence 70) → Risk Manager → executor; one decision row per bar written at the start and completed at the end; per-symbol lock; replay harness with per-step invariants; scenario test (synthetic, all guards fire) and `scripts/replay.py` | Decision row inserted first (intents reference it; a crash leaves its stage); indirect imports of `_issuance` allowed (calling the Risk Manager is the intended path) | Real-data replay (Vantage, 2026-06-15 → 09-27): 16,833 bar events, 0 invariant violations, 13 trades (3 TP / 9 SL / 1 open), −1.25% — far too few trades to judge edge. M1 export coverage starts 17 Jun (XAU) / 21 Jul (BTC). BTC's fixed ~$17 spread exceeds 10% of M15 ATR in ~56% of bars. |

| 2026-09-28 | 2.9 Position manager | Friday flatten → time stop → missing-SL repair (close if already crossed) → slippage re-alignment → break-even → ATR trail; tighten-only, stop/freeze-level aware, per-minute idempotent intents; engine loop; hooked into replay | Lives in `risk/` (only the risk layer issues intents) | Real replay: 14 trades, 1 time stop, 1 Friday flatten, 0 violations, −0.95% |

| 2026-09-29 | 3.1 Reconciler | One trade per engine-magic position: OPEN from its FILLED intent (provenance from the decision), ORPHAN_OPEN with an alert otherwise; SL/TP/volume tracked, partial closes mirror their deals; a gone position closes from its position-scoped deals once exits cover its volume, else one critical "vanished" alert after 5 min; `reconcile/pnl.py` sums every deal (commission, swap, fee, partials; VWAP exit; R to 4 dp); close reasons from DEAL_REASON plus the closing intent's recorded reason; events for opened/orphan/partial/closed/vanished; reconciler in the replay harness with a ledger-vs-deals invariant. Scenarios: SL, TP, manual close, partial then engine close, stop-out, reversal, orphan, engine down (trade never seen open), crash mid-send, vanished, idempotency, broker down | Reads the DB before the broker and DEFERS symbols with an in-flight intent instead of resolving UNKNOWN intents itself (only the executor moves intents); also settles FILLED OPEN intents that never got a trade (the spec's loop missed a position closing between two cycles); closing intents carry `close_reason` (migration 6db010216cae; `trades.close_reason` is now a checked enum) because TIME_STOP and CLOSE share an intent kind; orphans get no R/outcome; P&L aggregation (3.2) built here | Real-data replay (Vantage 2026-06-15 → 09-27): 17 positions → 17 CLOSED trades, net matches deals to the cent, reasons SL 10 / TP 3 / TIME_STOP 3 / FLATTEN 1, 0 violations. Scan deal history for orphans that open AND close while the engine is down (never seen by positions_get) → verify_ledger (3.6). Tests use a placeholder account login since the repo is public |

| 2026-09-29 | 3.3 Enrichment | `reconcile/enrichment.py`: MAE/MFE in price and in R (initial stop distance; mae_r ≤ 0 ≤ mfe_r), bars held, holding minutes, entry/exit slippage; runs once per closed trade (`enriched_at`, migration 7e2eb290adbf), waits for the closing minute's bar, retries a day for missing history; notifier summary; `MarketDataPort.bars_range` (ReplayFeed returns closed bars only); replay invariant: every exit lies within its trade's own excursions | Excursions use only minutes held in FULL plus the exit fills: the stop-out minute of the first test dipped to 4130 while the trade left at its 4138.35 stop, which would have reported -1.70R instead of -1.00R. SimBroker now stamps SL/TP fills in the minute they happen (was the bar's end), as MT5 does; the executor records slippage for closes too | Real replay: 32/32 trades enriched, 0 violations; mean MAE -0.77R / MFE +0.92R; 4 of 19 losers had reached +1R first (7 reached +0.5R) — a break-even rule to test once there are enough trades. Trade Reviewer enqueue deferred to Phase 7 |

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
| 2026-09-28 | Spread gates set from measured data (operator decision: Vantage spreads are the best available) | BTC fixed ~$16.94 spread: `max_spread_to_atr` 0.10 → 0.30 (blocks quietest ~7% of M15 bars instead of 56%) plus a 2,500-point blow-out cap; gold unchanged (never trips); NAS100 added provisionally. At 0.30 a quiet-market BTC trade can start ≈ −0.2R in spread | config example, AGENTS.md |
| 2026-09-28 | NAS100 calibrated: broker symbol `NAS100.r`, `max_spread_to_atr` 0.15, 400-point cap | Vantage NAS100 spread is a fixed 180 points; spread/ATR p50 0.039, p99 0.083, so the gate never blocks a normal bar. Its D1 history (127 bars) is still below the 300 bars the profile needs: re-export with `--months 15` | config example |
| 2026-09-28 | Executable intents only via `domain._issuance`, import-restricted to `aifund.risk`; copies are never executable | Structural guarantee that LLM output cannot reach the broker without the Risk Manager | AGENTS.md |

## Operational measurements (filled during P4.7, P5.8, soak)

| Metric | Value | Date |
|--------|-------|------|
| LLM cost / day | | |
| Analyst latency p50 / p95 | | |
| Invalid LLM output rate | | |
| Decisions / day, LLM calls / day | | |
| Engine uptime | | |
