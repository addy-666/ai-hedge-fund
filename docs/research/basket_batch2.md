# Basket batch 2 — pre-registration (roadmap 11.5)

Written and committed **before any of these hypotheses was run** (2026-10-16). Every rule, parameter and pass
mark below is fixed here; nothing is tuned after the results. A hypothesis that fails is reported and skipped,
not re-specified.

## Data

Everything there is on the Mac, joined into one research root by `scripts/import_workshop_m1.py`:

| Source | Symbols | Range | Used for |
|---|---|---|---|
| Workshop M1 bars built from Vantage ticks (bid OHLC + mean spread per minute) | NAS100.r, XAUUSD, BTCUSD, EURUSD, GER40.r, Nikkei225 | 2025-10-01 → 2026-09-30 | price paths (M1), every timeframe of GER40/Nikkei225/EURUSD |
| Operator "max history" export (`export_history.py`) | NAS100.r, XAUUSD, BTCUSD | M5–H4 from 2025-07-06, D1 from 2022 | signal timeframes; paths before 2025-10-01 (M5) |

Server clock: Vantage = New York + 7 h. Costs: the research config (`commission_per_lot` 0, `slippage_points` 0);
the spread is in every fill and every exit through the bars' spread.

## Pass marks (all hypotheses)

A hypothesis **is added to the basket** (registered in `research/basket/study.py`, verdict in PROGRESS) when, over
all the data it can be run on, net of costs:

1. mean R > 0 **and** the 90% bootstrap CI's lower bound > 0 (`stats.summarize`), and
2. at least 60% of 5 equal time segments have a positive mean (E1's stability check, `folds + 1` = 5).

Cross-asset variants must also show that the filter itself adds something:

3. mean R of the base trades the filter keeps − mean R of those it drops > 0, with the 90% bootstrap CI of the
   difference above 0.

Otherwise it is skipped. Passing here is **not** gate E1: nothing reaches the engine without E1 (≥ 200 signals on
data no search saw, BH over the whole ledger), which is reported alongside and stays required (Part B).

## H1 — SR break and retest (`sr_break_retest` v1.0, NAS100.r)

The break-and-retest side of the operator's `srea` EA (SR_H1_EA v1.00), with the EA's own default numbers. The EA
itself enters on the first close beyond a level; this hypothesis waits for price to come back to the broken level
and hold it. New hypothesis: no data has been searched for it, so every bar is out-of-sample.

- **Levels (H1, as the EA):** fractal swings with 3 bars either side (a high ≥ the 3 older highs and > the 3 newer;
  lows mirrored), known at the close of the 3rd bar after the swing. A new level within 10 index points of a kept
  one replaces it (the more recent wins); the 12 most recent are kept.
- **Break (M5):** a closed bar with body ≥ 30% of its range, bullish, previous M5 close ≤ L and close > L + 3
  (bearish mirrored). The level is then broken up; one watch per level, a newer break replaces it.
- **Failed break:** any later M5 close below L − 3 (above L + 3 after a down-break) before the retest ends the watch.
- **Retest (M5):** within the 12 bars after the break bar, the first closed bar that is bullish with body ≥ 30%,
  low ≤ L + 8 (the zone), low ≥ L − 15 (max penetration) and close > L (mirrored for a down-break). One per break.
- **Trade:** market entry at the next M5 open (at most 5 minutes late); stop at min(retest low, L) − 5 (mirrored),
  at least 0.5 (the broker's stops level), skipped when wider than 60; target 1.5R; no break-even or trailing
  (fixed R so the result is in R); a level traded rests 12 M5 bars; signal bars with spread > 5 are skipped;
  one position at a time; at most 5 entries per server day; all hours.

## H2 — Index opening S1 + BOS (`index_opening_s1` v1.0, GER40.r and Nikkei225)

The workshop's IndexOpeningPlaybook v1.0.0 setup S1 with its BOS filter, rule for rule from
`replay_index_playbook.py` (`s1_bos=True`, S2/S3 off, one S1 fill per day).

- Cash open in exchange time (Frankfurt 09:00, Tokyo 09:00) converted to server time with each zone's own DST.
  No bar exactly at the open → no trade that day; fewer than 70 M1 bars in [open − 60, open + 400) → none.
- Signal on one of the first 5 closed M1 bars from the open: body ≥ 85% of the range; range between 0.03% and
  0.50% of the close and ≥ 3 × spread; close beyond the high (bull) / low (bear) of the 15 bars before it.
- Buy stop at the signal bar's high + spread, stop at its low (sell stop at the low, stop at high + spread); skipped
  when the spread > 25% of the risk or when the next bar opens more than 0.3R past the entry. The order lives 5
  bars and never past open + 8 min, is cancelled when the stop side trades first, and a newer signal replaces it.
- Target 1.5R; whatever is open at open + 330 min is closed.

**Caveat declared now:** the only GER40/Nikkei225 data is the year the workshop chose S1 + BOS on (out of ~30
variants). Its result here is in-sample; it is reported as such and E1 needs the long export.

## H3 — EURUSD agreement filters (cross-asset variants of the 11.4 ports)

Base strategies: the 11.4 ports with a positive mean on both the clean window and the workshop year —
`fib_pullback` (XAUUSD), `sweep_pin_follow` (XAUUSD), `sweep_rejection_follow` (BTCUSD), `ict_dmss` C1
(NAS100.r). `candle_streak` (negative on the clean window) and `spike_fade` (an M1 shock fade; the dollar's
direction has no stated role) are not tested.

One hypothesis per base strategy, one rule, no parameter: at the signal's decision time take the last CLOSED
EURUSD H1 bar and the close 24 H1 bars before it. A BUY is kept only when EURUSD rose over those 24 bars (dollar
weaker), a SELL only when it fell. If the last closed EURUSD bar is older than 4 hours (EURUSD shut, e.g. BTC at
the weekend) the signal is dropped.

USDJPY and XAGUSD agreement (also named in 11.5) cannot be run: there is no history for them on the Mac.
EURUSD exists only from 2025-10-01, so these run on the base strategies' searched year; pass mark 3 (the uplift)
is what judges the filter.
