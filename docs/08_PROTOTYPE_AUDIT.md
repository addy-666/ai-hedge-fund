# 08 — Prototype Audit (legacy/ → new design)

The prototype (`main.py`, `ai_brain.py`, `execution.py`, `memory_store.py`, `auditor.py`, `data_engine.py`)
captures the right *ideas*. The defects below are why the new system is a rebuild and not an incremental
refactor. **Do not port code from `legacy/`; port intent only.** Each item lists where the new design fixes it.

Severity: **C** = can lose money / corrupt records, **H** = wrong behaviour, **M** = robustness / maintainability.

| # | Sev | Prototype defect | Where fixed |
|---|---|---|---|
| 1 | C | Dashboard API binds `0.0.0.0` with **no authentication**; `/api/close` closes any ticket, `/api/control` changes risk | `05` §2, `06` §6 |
| 2 | C | Position sizing clamps **up** to `volume_min` when the computed lot is smaller → risks more than budget | `03` §11 (reject or bounded overshoot) |
| 3 | C | Sizing uses `info.ask` for both BUY and SELL and a tick-value approximation; `round(lots, 2)` can produce a volume that is not a multiple of `volume_step` | `03` §11 (`order_calc_profit`, Decimal floor to step) |
| 4 | C | Reversal closes the opposite position and opens the new one **without verifying the close succeeded** → can end up hedged or double-exposed | `03` §9.2 |
| 5 | C | No daily loss limit, drawdown halt, portfolio heat, correlation cap, max positions, or kill switch | `02` §4 `risk.limits`, `01` §9, `06` §4 |
| 6 | C | `order_send` result not checked for `None` (`res.retcode` → AttributeError); no handling of unknown outcomes; no idempotency → restart or exception can duplicate orders | `03` §12, `02` §2.1 |
| 7 | C | Trade memory in `memory.json` rewritten wholesale; on corruption `load_trade_memory()` returns `[]`, silently discarding history | `02` (SQLite WAL, transactions, backups) |
| 8 | H | "ATR-based sizing" is not ATR-based: the LLM invents SL/TP prices; ATR is only a fallback for invalid geometry | `03` §10–§11 |
| 9 | H | LLM confidence penalty for learned rules is **requested from the LLM** ("reduce confidence by N"), never enforced | `04` §7 |
| 10 | H | Learned rules are free text; appended forever; no dedup, validation, expiry, versioning; generated from as few as 3 trades; only losses analysed (no comparison with wins) | `04` whole document |
| 11 | H | Reconciliation stores `res.order` as the ticket and matches `deal.position_id` to it (usually but not guaranteed equal); P&L = last exit deal `profit` only (ignores commission, swap, fee, partial closes); outcome by sign of profit | `03` §12 (position_id from entry deal), §14.2 |
| 12 | H | Indicators computed on bars including the **forming bar** (`copy_rates_from_pos(..., 0, 250)`) → signals repaint | `03` §2, §5 |
| 13 | H | Scans every 30 s using H1/D1 data → the same bar is re-sent to the LLM ~120×/hour per symbol (cost, and repeated contradictory signals) | `03` §2 (bar-close triggering) |
| 14 | H | Duplicate guard checks all positions on the symbol regardless of magic, ignores pending orders and in-flight requests, has no cooldown | `03` §9.1 |
| 15 | H | Hard-coded `ORDER_FILLING_IOC` (rejected by some symbols/brokers); hard-coded magic `999999` and deviation | `03` §12.1, config |
| 16 | H | No SL/stops-level/freeze-level validation; no `order_check` | `03` §10, §12 |
| 17 | H | Local `datetime.now()` mixed with MT5 server-time epochs; 30-day history window can miss or mis-time deals | `03` §1 (server offset), §14.1 (position-scoped history) |
| 18 | M | Blocking `requests.post` and MT5 calls inside `async def` loop → freezes the event loop and the dashboard | `01` §4 (gateway thread, async LLM client) |
| 19 | M | `requests.post` without timeout → a hung DeepSeek call hangs trading forever | `03` §7, `01` §10 |
| 20 | M | Single-process API + loop: a dashboard bug can take down trading | `01` §4 |
| 21 | M | Engine state (`is_running`, risk %) only in memory; lost on restart; no audit trail of changes | `02` `engine_state`, `audit_log` |
| 22 | M | `config.py` imports `MetaTrader5` at module import → nothing (not even tests) runs on macOS | `01` §11 (optional extra, ports & adapters) |
| 23 | M | `VAULT_PATH` is a macOS path while the runtime is Windows (`.bat`/`.ps1`) | `00` §4, `04` §10 |
| 24 | M | Unused heavy deps (langchain, langgraph, chromadb); `pandas-ta 0.3.14b0` incompatible with current numpy | `01` §11 |
| 25 | M | `@app.on_event("startup")` deprecated; audit runs synchronously inside the trading loop; `AUDIT_TRADE_THRESHOLD` defined but unused | `01` §6 |
| 26 | M | Market context stored per trade lacks a feature schema/version, prompt, raw LLM response, rules applied → losses cannot be explained later | `02` `feature_snapshots`, `decisions`, `llm_calls` |
| 27 | M | No tests | `07` global quality gates |

What to keep from the prototype (as ideas): the self-learning intent, correlated-asset context, confidence
threshold, geometric SL/TP sanity checks, dashboard concept, Windows background scripts (replaced by Task
Scheduler setup in `06` §2).
