# 03 — Trading Core Specification

Covers: MT5 gateway, bar-close triggering, features, pre-flight gates, setup detection, the Analyst contract,
confidence pipeline, ATR-based stops and sizing, portfolio limits, duplicate and reversal guards, execution,
position management, reconciliation and trade enrichment.

---

## 1. MT5 Gateway (`adapters/mt5/gateway.py`)

- A single dedicated thread imports `MetaTrader5` and processes a request queue; the asyncio side awaits
  futures. All requests carry a timeout (default 10 s; `order_send` 15 s).
- `initialize(path=MT5_PATH, login, password, server, timeout=60_000, portable=True)` on start; on any call
  returning `None` with a connection-type `last_error()`, the gateway runs `shutdown()` + re-`initialize()`
  with exponential backoff (1, 2, 4 … 60 s) and reports `DISCONNECTED` to the Supervisor.
- **Startup checks** (fail → engine stays `STOPPED` with reason):
  - `terminal_info().connected` and `terminal_info().trade_allowed` (AutoTrading button on).
  - `account_info().trade_allowed`, `trade_expert`.
  - `account_info().margin_mode` — the design assumes **hedging** (`ACCOUNT_MARGIN_MODE_RETAIL_HEDGING`). On a
    netting account the reversal logic in §9 must use the netting branch; refuse to start if
    `reversal_mode=close_and_reverse` and the mode is unsupported.
  - `account_info().trade_mode` matches configured mode (DEMO vs REAL).
  - Every configured symbol: `symbol_select(sym, True)` and a `symbol_info` cached into `symbols`.
- **Server time → UTC.** MT5 bar/tick/deal times are broker-server wall-clock seconds. Determine the offset at
  startup and hourly: `offset = round_to_15min(latest_tick.time − utc_now())`, using only a symbol whose tick
  time *advanced* between two samples a few seconds apart (a live feed). A stale tick cannot be used: if its
  age is near a multiple of 15 minutes it would silently shift the offset. Persist the offset; alert if it
  changes (DST); with no live symbol (weekend) keep the persisted value. The gateway converts every timestamp it returns.
- **Typed mapping:** return `Bar`, `Tick`, `SymbolSpec`, `Position`, `PendingOrder`, `Deal`, `AccountInfo`,
  `OrderResult` domain objects. Never leak `mt5` namedtuples past the adapter.
- **Constants:** reference MT5 constants by name from the module (`mt5.TRADE_RETCODE_DONE`, …). Where the
  Python module lacks a constant (e.g. symbol filling flags), define it in `adapters/mt5/constants.py` with a
  comment citing the MQL5 documentation value.

The `SimBroker` implements the same `BrokerPort`: fills at bid/ask of the replay tick/bar with a configurable
spread/slippage/commission model, triggers SL/TP intrabar (if both are touched in one bar → assume SL first),
generates MT5-shaped deals (IN/OUT, reasons SL/TP/EXPERT), supports partial closes, and can inject faults
(`None` results, requotes, disconnects) for tests.

---

## 2. Bar-close triggering (`market/bar_clock.py`)

- Every 2 s, per symbol: read the last 2 bars of the trigger TF via `copy_rates_from_pos(sym, tf, 0, 2)`.
  Index 0 of the result (the most recent) is the **forming** bar; the bar before it is the last closed bar.
- When the last closed bar's open time > the last processed bar time for (symbol, tf) → emit `BarClosed`.
- **Only closed bars are ever used for features.** When fetching history for features use
  `copy_rates_from_pos(sym, tf, 1, bars_per_tf)` (start at position 1 to skip the forming bar).
- Delay: wait `bar_close_grace_s` (default 3 s) after the boundary before fetching, so the broker's final tick
  of the bar is in.
- Stale guard: if the newest closed bar is older than `2 × tf` (market closed, feed stall) → no trigger; if the
  symbol is expected open (per session config) → heartbeat detail `STALE_FEED` and alert after 5 min.
- Processed-bar cursors are persisted (via `decisions`), so a restart does not re-decide already-processed bars;
  bars missed during downtime are **not** traded retroactively (only the latest closed bar is considered).

---

## 3. Decision pipeline (`engine/pipeline.py`)

Executed per `BarClosed(symbol, trigger_tf, bar_time)` under a **per-symbol asyncio lock**. Each stage either
passes or terminates the decision with an outcome + reason code. Every run writes exactly one `decisions` row.

| # | Stage | Terminal outcome if it stops here |
|---|-------|-----------------------------------|
| 1 | Pre-flight gates (§4) | `SKIPPED` |
| 2 | Feature snapshot (§5) | `ERROR` (`STALE_DATA`, `INSUFFICIENT_BARS`) |
| 3 | Setup detection (§6) | `NO_SETUP` (if `require_setup`) |
| 4 | Analyst LLM (§7) | `HOLD` (analyst chose no trade) or `INVALID` (LLM error / unreadable output) |
| 5 | Rule engine (`04_LEARNING_LOOP.md` §7) | `RULE_BLOCKED` |
| 6 | Portfolio manager / confidence (§8) | `BELOW_THRESHOLD` |
| 7 | Guards + limits + sizing (§9–§11) | `RISK_REJECTED` |
| 8 | Execution (§12) | `ORDERED` (fill details on intent) or `RISK_REJECTED` (`BROKER_REJECTED`) |

Any uncaught exception inside the pipeline → outcome `ERROR`, no order, full traceback in logs, error budget
decremented. Stages 5–7 outcomes with a directional proposal spawn a **virtual trade** (§14.4).

Budget: the whole pipeline must finish within `min(60 s, 0.25 × trigger TF)`; otherwise abandon with
`TIMEOUT` (never place an order based on a stale snapshot). Before execution, re-read the tick and abort if
price moved more than `0.5 × ATR` from the snapshot close.

---

## 4. Pre-flight gates (cheap, deterministic, before any LLM call)

In order; first failure wins:

1. Engine state is `RUNNING` and mode permits orders (`PAPER` routes to SimBroker).
2. Symbol not locked by a non-terminal intent (`INTENT_IN_FLIGHT`) and not in an `UNKNOWN` resolution.
3. Market open: last tick age < 60 s; weekend rule (`trade_weekends`); broker `trade_mode` allows trading.
4. Spread: `spread_points ≤ max_spread_points` and `spread_price / ATR(trigger) ≤ max_spread_to_atr`.
5. News blackout: no HIGH-impact event for the symbol's currencies within [−before, +after] minutes.
6. Loss limits not breached (daily / weekly / drawdown) — normally already reflected in engine state.
7. Symbol cooldown (after close / after loss) and flip-flop lock not active.
8. Daily per-symbol trade count < `max_trades_per_symbol_per_day`.
9. If the symbol already has an engine position **and** `reversal_mode == ignore` → `POSITION_EXISTS`
   (saves the LLM call; with `close_only`/`close_and_reverse` the pipeline continues so an exit can be
   evaluated).
10. LLM circuit breaker closed and daily budget remaining.

News data source: MT5's economic calendar is only accessible from MQL5, so the Guardian EA exports upcoming
events hourly to `Common\Files\aifund\calendar.csv` (§ `06`). Until that exists, `news.enabled=false` and the
feature `ctx.minutes_to_next_high_impact_news` is null.

---

## 5. Feature snapshot (`market/features.py`)

- Fetch `bars_per_tf` closed bars for each TF in the symbol's profile.
- Validate: enough bars for EMA200 + 100-bar percentile windows; no gaps larger than expected for the asset's
  sessions; last bar time matches the trigger.
- Compute features from the registry (`02_DATA_MODEL.md` §3) in a thread pool. Indicator definitions:
  - EMA: standard `α = 2/(n+1)`, seeded with SMA of first n.
  - RSI / ATR / ADX: **Wilder** smoothing (`α = 1/n`). ATR uses true range with previous close.
  - Percentile rank: fraction of the last 100 values ≤ current value.
  - Swings: 5-bar fractals (2 left, 2 right) — the most recent confirmed swing only (no lookahead: a fractal is
    confirmed 2 bars after its pivot).
- Persist the snapshot before calling the LLM. The snapshot is immutable afterwards.
- Unit tests compare each indicator with hand-verified reference series (fixtures) to 1e-8.

---

## 6. Setup detectors (`strategies/`)

Deterministic modules, each implementing:

```python
class SetupDetector(Protocol):
    setup_tag: str            # e.g. "mtf_trend_pullback"
    playbook_id: str          # links to config/playbooks/<id>.yaml
    def detect(self, snap: FeatureSnapshot) -> list[SetupCandidate]: ...
```

`SetupCandidate(setup_tag, direction_hint, key_levels: dict, strength: 0..1, notes)`.

Initial detectors (derived from vault playbooks with `automation_potential: high`; each must cite its vault
note in the playbook card):

| setup_tag | Idea (summary) | Vault source note |
|---|---|---|
| `mtf_trend_pullback` | Context+setup TF trend aligned; trigger TF pulls back to EMA20/50 zone with RSI reset; resumption candle | `wiki/strategies/high_probability_multi_timeframe_trend_pullback.md`, `triple_screen_trading_system.md` |
| `nr7_breakout` | NR7 / volatility contraction on setup TF; break of range with rel volume > 1.2 | `high_probability_nr7_volatility_breakout.md` |
| `failure_test_2b` | Sweep of prior swing high/low that closes back inside; context not strongly trending against | `sperandeo_2b_reversal_playbook.md`, `grimes_failure_test_and_false_breakout_playbook.md` |
| `sr_fade_range` | RANGE regime; test of range extreme with rejection wick | `high_probability_support_resistance_fade.md` |

Phase 2 ships only `mtf_trend_pullback` (also used as the **deterministic baseline strategy** that trades
without the LLM, to prove the plumbing). Others are added in Phase 8.

---

## 7. Analyst agent contract (`agents/analyst.py`)

### 7.1 Input (rendered into a versioned Jinja prompt, `prompts/analyst_v1.j2`)

Prompt order is **stable prefix first** (maximises DeepSeek context-cache hits): system role → output schema →
playbook cards for the candidate setups → lessons (rules in scope) → then the variable part: symbol, TF roles,
compact feature table per TF, last 20 trigger-TF candles (OHLC in ATR-normalised form + raw close), setup
candidates, open position on the symbol (if any), portfolio context.

Lessons block wording (fixed):

```text
LESSONS FROM OUR OWN TRADE HISTORY (validated statistics).
The risk engine automatically applies the listed penalties/blocks. Do NOT lower your confidence to account
for them yourself. Use them only to reconsider whether your thesis depends on a condition listed here.
- [R-0042] LONG mtf_trend_pullback on XAUUSD when h1.rsi14 > 70 AND m15.atr14_pct_rank100 >= 0.9:
  23 trades, win 26% vs 48% baseline, expectancy -0.41R vs +0.12R.
```

### 7.2 Output schema (strict Pydantic; DeepSeek JSON output mode)

```python
class TradeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: Literal["LONG", "SHORT", "NONE"]
    confidence: int = Field(ge=0, le=100)          # raw market read
    setup_tag: SetupTag | Literal["none"]           # must be one of the detected candidates or "none"
    invalidation_price: float | None                 # where the thesis is wrong (becomes SL input)
    target_price: float | None                       # optional; clamped to RR band
    thesis: str = Field(max_length=600)
    key_risks: list[str] = Field(max_length=5)
    lessons_considered: list[str] = []               # rule ids the model thinks are relevant (logged only)
    time_horizon_bars: int | None = Field(default=None, ge=1, le=200)
```

### 7.3 Validation & fallbacks

1. JSON parse + schema validation. On failure: one repair call with the validation error message; then INVALID
   (`LLM_INVALID_OUTPUT`).
2. `direction == NONE` or `setup_tag == none` (when `require_setup`) → HOLD.
3. `setup_tag` not among detected candidates → HOLD (`SETUP_MISMATCH`) — prevents hallucinated setups.
4. Invalidation geometry: LONG requires `invalidation < entry_ref`; SHORT requires `>`; otherwise ignore the
   field and use the default ATR stop (logged as `INVALIDATION_IGNORED`).
5. The LLM never supplies volume. Any extra fields → validation error (`extra="forbid"`).

---

## 8. Confidence pipeline (`agents/portfolio_manager.py`)

```text
p_raw        = proposal.confidence                          # 0..100
p_cal        = calibrator(p_raw)                            # identity until calibration model active (04 §9)
penalty      = min(Σ active-rule penalty points, max_total_penalty)
final        = p_cal − penalty
trade if     final ≥ confidence_threshold  (and no BLOCK rule matched)
risk_factor  = Π(rule risk_scale factors) × regime/drawdown scalers   (floor 0.25)
```

With the Phase 8 committee, `p_raw` becomes a weighted combination of specialist proposals that agree on
direction, minus critic penalties (`HIGH` objection = −15, `MEDIUM` = −7); disagreeing directions → HOLD.

---

## 9. Duplicate & reversal guards (`risk/guards.py`)

### 9.1 Duplicate guard — layered, any layer blocks

| Layer | Check | Reason code |
|---|---|---|
| 1. Idempotency | `INSERT order_intents(idempotency_key)`; UNIQUE violation = this bar/direction already acted on (survives restarts) | `DUPLICATE_IDEMPOTENCY` |
| 2. In-process lock | Per-symbol asyncio lock held from pipeline stage 7 through execution resolution | (serialises) |
| 3. In-flight intents | Any non-terminal intent for the symbol | `INTENT_IN_FLIGHT` |
| 4. Broker state | `positions_get(symbol)` + `orders_get(symbol)`: same-direction position/order with our magic | `DUPLICATE_SAME_DIRECTION` |
| 5. Foreign exposure | Position on symbol with another magic (manual trade / other EA) and `block_if_foreign_position_on_symbol` | `FOREIGN_POSITION` |
| 6. Per-symbol cap | Engine positions on symbol ≥ `max_positions_per_symbol` | `MAX_PER_SYMBOL` |
| 7. Cooldown | Within `cooldown_bars_after_close` / `_after_loss` of last close on symbol | `COOLDOWN` |
| 8. Daily cap | Trades today on symbol ≥ `max_trades_per_symbol_per_day` | `DAILY_SYMBOL_CAP` |

Layer 4 uses **live broker state**, not the DB — the DB could be behind.

### 9.2 Reversal guard

Triggered when a position `P` (direction `D`) exists and the final decision is `−D`.

```text
if reversal_mode == "ignore":                 -> REJECT (POSITION_EXISTS)
require final_conf ≥ threshold + reversal_extra_confidence   else REJECT (REVERSAL_WEAK)
require bars_held(P) ≥ reversal_min_hold_bars                 else REJECT (REVERSAL_TOO_EARLY)
require reversals_today(symbol) < max_reversals_per_symbol_per_day else REJECT (REVERSAL_CAP)
flip-flop: if the last `flip_flop_window` directional decisions on the symbol alternate
           -> lock symbol for `flip_flop_lock_bars`, REJECT (FLIP_FLOP)

Execution (never open the new side before the old one is gone):
  1. intent kind=REVERSE_CLOSE for P (full volume) → send → verify:
       positions_get(ticket=P) is empty AND an OUT deal exists for P.position_id
     if verification fails within 10 s → mark UNKNOWN, lock symbol, alert, STOP (do not open)
  2. reconcile P immediately (close_reason = REVERSAL)
  3. if reversal_mode == "close_only": done (decision outcome ORDERED with kind REVERSE_CLOSE)
  4. if "close_and_reverse": re-run stage 7 from scratch with fresh account equity, fresh tick,
     fresh limits (the closed trade's P&L may have changed them) → open new intent
```

Rationale: on a hedging account an opposite market order without `position=` opens a **second** position
(a hedge), which is never intended here; on netting accounts it would net/flip in one order — the netting
branch must send a close-by-volume first and then the new order, with the same verification.

---

## 10. Stops and targets from ATR (`risk/stops.py`)

```text
ATR        = snapshot[f"{atr_tf}.atr14"]            # trigger TF by default
entry_ref  = ask (LONG) / bid (SHORT) at sizing time
spread     = ask − bid

default_sl_dist = k_sl_default × ATR
if proposal.invalidation_price valid:
    raw = |entry_ref − invalidation| + invalidation_buffer_atr × ATR + spread
    sl_dist = clamp(raw, k_sl_min × ATR, k_sl_max × ATR)
else:
    sl_dist = default_sl_dist

min_dist = (stops_level + 2) × point + spread       # broker minimum distance, with margin
sl_dist  = max(sl_dist, min_dist)

rr = rr_default
if proposal.target_price valid (right side of entry):
    rr = clamp(|target − entry_ref| / sl_dist, rr_min, rr_max)
tp_dist = rr × sl_dist

SL = entry_ref − sl_dist  (LONG) | entry_ref + sl_dist (SHORT)
TP = entry_ref + tp_dist  (LONG) | entry_ref − tp_dist (SHORT)
round SL/TP to tick_size (SL rounded AWAY from entry, TP rounded TOWARD entry)
```

`sl_atr_multiple = sl_dist / ATR` and `rr` are recorded in the decision and are rule-usable features
(`prop.sl_atr_multiple`, `prop.rr_target`).

---

## 11. Position sizing (`risk/sizing.py`) — ATR-volatility based

Because `sl_dist` scales with ATR, a fixed money risk automatically produces **smaller positions in volatile
conditions and larger ones in quiet conditions**. On top of that, explicit scalers reduce risk further in
extreme volatility, drawdown, and low-confidence situations.

```text
equity        = account_info().equity
base_pct      = risk_per_trade_pct
conf_factor   = lerp(conf_scaling.at_threshold → at_90, final_conf between threshold and 90), clamp [0.5, 1.0]
vol_factor    = vol_regime_scaling.factor if atr_pct_rank100 > threshold else 1.0
dd_factor     = drawdown_scaling.factor   if drawdown_pct > threshold else 1.0
rule_factor   = Π rule risk_scale factors
risk_pct      = min(base_pct × conf_factor × vol_factor × dd_factor × rule_factor, max_risk_per_trade_pct)
risk_money    = equity × risk_pct / 100

# loss for 1.0 lot if SL is hit — use the broker's own calculator (handles cross-currency conversion)
loss_per_lot  = |order_calc_profit(order_type, symbol, 1.0, entry_ref, SL)|
comm_per_lot  = configured or learned average round-turn commission per lot (from deals history)
lots_raw      = risk_money / (loss_per_lot + comm_per_lot)

step          = volume_step (as Decimal)
lots          = floor(lots_raw / step) × step       # ALWAYS round DOWN
lots          = min(lots, volume_max, max_lots_per_symbol)
if lots < volume_min:
    if volume_min × (loss_per_lot + comm_per_lot) ≤ risk_money × (1 + min_lot_overshoot_pct/100):
        lots = volume_min
    else: REJECT (RISK_BELOW_MIN_LOT)                # never silently risk more than budget

margin        = order_calc_margin(order_type, symbol, lots, entry_ref)
require margin ≤ free_margin × max_margin_utilisation   else REJECT (MARGIN)

initial_risk_money = lots × (loss_per_lot + comm_per_lot)
```

Portfolio limits (after sizing, using `initial_risk_money`):

- `Σ open initial risk + new ≤ max_portfolio_heat_pct × equity` → else `PORTFOLIO_HEAT`.
- Same for the symbol's `correlation_bucket` with `max_bucket_heat_pct` → else `BUCKET_HEAT`.
- `open positions < max_open_positions` → else `MAX_POSITIONS`.
- `Σ open notional + new notional ≤ equity × max_notional_leverage` → else `LEVERAGE_CAP`. Broker margin
  alone is not a limit on high-leverage accounts.

The full worksheet (every input and intermediate value above) is stored in `decisions.risk_calc`.
**Property-based tests** (hypothesis) must assert for random valid symbol specs and prices: lots is a multiple of
step; lots ≤ volume_max; `initial_risk_money ≤ risk_money × (1 + overshoot)`; SL on the correct side; result is
monotonically non-increasing in ATR.

---

## 12. Execution (`execution/executor.py`)

```text
def execute(intent):
    persist intent PENDING (idempotency key reserved)          # BEFORE touching the broker
    filling = choose_filling(symbol_spec)                      # §12.1
    tick    = symbol_info_tick(symbol); price = ask/bid
    re-derive SL/TP from sl_dist/tp_dist around the fresh price (keeps distances, not absolute levels)
    req = {action: DEAL, symbol, volume, type, price, sl, tp, deviation: max_deviation_points,
           magic, comment: "AF:"+intent.code, type_time: GTC, type_filling: filling}
    chk = order_check(req)   → if not OK: status CHECK_FAILED (retcode, comment); return
    status SENT; res = order_send(req)
    if res is None or timeout:           status UNKNOWN; resolve_unknown(intent); return
    match classify(res.retcode):
        DONE / DONE_PARTIAL:  resolve position_id via history_deals_get(ticket=res.deal).position_id
                              (fallback: positions_get(symbol) filtered by magic+comment)
                              status FILLED; record fill price/volume/slippage; create Trade OPEN
        RETRYABLE (REQUOTE, PRICE_CHANGED, PRICE_OFF): status RETRYING; if attempts < 3 and
                              |new price − snapshot close| < 0.5 ATR → re-price and resend; else REJECTED
        TERMINAL (everything else): status REJECTED; alert on unexpected codes
                              (NO_MONEY, INVALID_STOPS, MARKET_CLOSED, CLIENT_DISABLES_AT → also PAUSE)
    trigger reconciler immediately
```

### 12.1 Filling mode

Read `symbol_info.filling_mode` bit flags (FOK = 1, IOC = 2). Prefer IOC if allowed, else FOK, else RETURN
(for exchange-execution symbols). The prototype hard-coded IOC, which some brokers reject
(`TRADE_RETCODE_INVALID_FILL`).

### 12.2 UNKNOWN resolver

Runs immediately, then every 5 s for up to 2 minutes, and on every startup:

1. Search `positions_get(symbol)` for our magic + comment code (and volume ± step, open time ≥ sent_at − 5 s).
2. Search `history_deals_get(sent_at − 60 s, now)` for deals with our magic and comment code.
3. Found → `FILLED` (link position_id, create Trade). Not found after the grace period → `REJECTED`
   (`UNKNOWN_NOT_EXECUTED`) and alert.

The symbol stays locked while any intent is UNKNOWN. `order_send` is **never** blindly retried.

### 12.3 Post-fill checks

- If the position has no SL (some brokers drop SL on market execution with slippage) → immediately
  `TRADE_ACTION_SLTP` with the planned levels; on failure close the position and alert.
- If actual fill price differs from `price_ref` so that SL distance changed by > 10% → modify SL/TP to restore
  the planned distances (risk stays as sized).

---

## 13. Position management (`risk/position_manager.py`)

Every 5 s over engine-owned open positions:

- **Missing SL repair** (§12.3) — also catches orphans.
- **Break-even** (if `break_even_at_r`): when price ≥ entry + R × sl_dist, move SL to entry + costs.
- **Trailing** (if `trailing.mode == atr`): SL = max(SL, close − k × ATR) on closed trigger bars only.
- **Time stop**: close after `time_stop_bars` trigger bars (`close_reason = TIME_STOP`).
- **Friday flatten**: non-weekend symbols closed at `flatten_friday_utc`.
- **HALTED/FLATTENING** behaviour per engine state table.
- Modifications respect `freeze_level` (no modification when price is within freeze distance of SL/TP).
- Every modification is an `order_intents` row (kind `MODIFY_SLTP`) for auditability.

---

## 14. Reconciliation (`reconcile/reconciler.py`)

### 14.1 Algorithm (every 30 s, on startup, after every execution)

```text
broker_positions = positions_get() filtered to magic == engine magic (and all symbols configured)
db_open          = trades where status in (OPEN, ORPHAN_OPEN)

for pos in broker_positions:
    trade = db_open.get(position_id=pos.identifier)
    if trade is None:
        if an UNKNOWN/SENT intent matches (comment code) → resolve it (FILLED) and create Trade
        else → create Trade ORPHAN_OPEN, ensure SL, alert "orphan position"
    else:
        update current_sl/tp, volume_open_now (detect partial closes), floating P&L

for trade in db_open not in broker_positions:
    deals = history_deals_get(position=trade.position_id)          # position-scoped query, no date windows
    if deals has OUT/OUT_BY/INOUT deals covering the full volume:
        upsert all deals into `deals`
        close_trade(trade, deals)
    else:
        retry next cycle; after 5 min → alert "position vanished without closing deals"
```

### 14.2 P&L aggregation (`reconcile/pnl.py`)

```text
gross      = Σ deal.profit over all deals of the position
commission = Σ deal.commission      (entry and exit deals both carry commission on many brokers)
swap       = Σ deal.swap
fee        = Σ deal.fee
net_pnl    = gross + commission + swap + fee
close_price_vwap = Σ(out.price × out.volume) / Σ out.volume
close_time = max(out.time)
close_reason from the last OUT deal's reason:
    DEAL_REASON_SL → SL, DEAL_REASON_TP → TP, DEAL_REASON_SO → STOP_OUT,
    DEAL_REASON_EXPERT → our intent kind (ENGINE / REVERSAL / TIME_STOP / FLATTEN / OPERATOR),
    DEAL_REASON_CLIENT / MOBILE / WEB → MANUAL_EXTERNAL
r_multiple = net_pnl / initial_risk_money
outcome    = WIN if r ≥ +0.1, LOSS if r ≤ −0.1, else BREAKEVEN
```

(The prototype used only the last exit deal's `profit` and its sign — ignoring commission/swap/partials.)

### 14.3 Enrichment (`reconcile/enrichment.py`, async after close)

- Fetch M1 bars from open to close; compute MAE and MFE in price and in R, `bars_held`, `holding_minutes`.
- Slippage in points for entry and exit (vs requested).
- Enqueue the Trade Reviewer (Phase 7), emit `trade.closed` event, send Telegram summary.

### 14.4 Virtual trades (`reconcile/virtual.py`)

For decisions ending in `RULE_BLOCKED`, `BELOW_THRESHOLD` (with a directional proposal) or
`RISK_REJECTED` (guard/limit reasons, not broker errors): create a virtual trade with entry = next trigger bar
open ± half spread, and the SL/TP that stage 10 would have produced. Advance on M1 bars every minute; SL first
if both touched in one bar; expire after `time_stop_bars`. Virtual trades use the same R/MAE/MFE maths and feed
the miner, validator and calibration with a `virtual=true` flag.

---

## 15. Money-path invariants (tested in CI)

1. No function in `agents/` imports from `execution/` or `adapters/mt5`.
2. `Executor.execute` accepts only an `OrderIntent` produced by `RiskManager` (type carries a private
   constructor token / is created only in `risk/manager.py`).
3. Volume is always floor-rounded to step; never exceeds `volume_max`; never upsized beyond overshoot policy.
4. Any exception between stage 1 and stage 8 results in no `order_send` (scenario test injects exceptions at
   every stage).
5. Restart at every step of `execute` (scenario tests kill the engine after each line with the SimBroker)
   produces zero duplicates and zero lost trades.
