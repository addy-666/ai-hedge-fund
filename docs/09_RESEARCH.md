# 09 — Edge Research & Evidence Gates

The rest of the spec makes the system **safe**; this document is about whether it deserves to trade at all.
Nothing here promises an edge. It builds the machinery that finds out, cheaply and honestly, whether a
strategy — deterministic or LLM-proposed — has one, and it stops the system from trading a strategy that
has not shown one. See ADR `adr/0001-edge-evidence-before-intelligence.md`.

```text
hypothesis (playbook detector, parameter variant, or LLM-proposed entry rule in the DSL)
      ▼
Signal study (research/signals.py)   every candidate on every closed trigger bar → counterfactual trade,
      │                               the LIVE stop planner and the LIVE fill rule, net of spread/costs
      ▼
Statistics + walk-forward            expectancy in R, bootstrap CI, p-value, per-fold stability;
(research/stats.py, walkforward.py)  parameters chosen in-sample, judged only out-of-sample
      ▼
Trial ledger (research/ledger.py)    every hypothesis ever evaluated → Benjamini–Hochberg across ALL of them
      ▼
Locked holdout (single use)          the most recent slice of history, evaluated once per hypothesis
      ▼
RESEARCH-VALIDATED record            → may be enabled in PAPER (evidence gate E1)
      ▼
Forward confirmation (PAPER/DEMO)    shadow virtual trades on live data after the model's training cutoff (E2)
      ▼
Orders (DEMO → LIVE ladder, 06 §10)
```

---

## 1. Principles

1. **Evidence before intelligence.** An LLM layer, a learning loop or a UI cannot rescue a strategy with no
   edge. Each strategy must clear the gates below before it may send orders outside SIM.
2. **Research uses the production maths.** Stops come from `risk/stops.plan_stops` (the ATR clamps and RR
   band), exits from `reconcile/virtual.simulate` → `market/fills.exit_on_bar` (SL first, gaps at the open,
   bid/ask sides), expiry from the configured time stop and the session calendar's pre-close flatten. A
   research number and a live number differ only by what research cannot model (below), never by formula.
3. **Every signal counts.** The study measures each detector candidate as an independent counterfactual trade,
   whatever the portfolio would have done (guards, limits, one-position-per-symbol). That is the *signal's*
   expectancy; the portfolio replay (`scripts/replay.py`) remains the plumbing test. This multiplies the
   sample the statistics see — the baseline fires far too rarely for its trades alone to be evidence.
4. **Parameters are chosen in-sample and judged out-of-sample.** Only concatenated out-of-sample folds are
   reported as the result of a walk-forward. In-sample numbers are diagnostics.
5. **Every trial is counted.** The ledger records every hypothesis evaluated, including failures, so the
   false-discovery control reflects the true search effort (grid points, LLM proposals, manual ideas).
6. **The holdout is spent once.** The most recent `holdout_fraction` of history is locked — a fixed window
   recorded in `data/research/holdout.json` on the first run. A hypothesis may be evaluated on it at most once
   (the ledger enforces it); results on it are never shown to the LLM. The window rolls forward (8.7) only onto
   `research.holdout_roll_days` of NEW data: the next holdout is exactly the bars after the old window, and the
   old holdout joins the walk-forward history; each window allows one look per hypothesis.
7. **LLM-proposed hypotheses are data-snooped at the idea level** (the model has read about what worked in
   the past). In-sample/holdout success is therefore necessary, not sufficient: forward confirmation on data
   after the model's training cutoff (E2) is mandatory before orders.

What research cannot model — and live must confirm: the real spread at the exact fill time (bars carry one
spread per bar), slippage beyond the configured allowance, requotes, swap on positions held past rollover
(the default profile flattens before long closes), liquidity, and the portfolio interactions the study ignores.

## 2. Signal study — `research/signals.py`

Inputs: history root (Parquet, `adapters/history_store.py`), symbols, [start, end), a profile (trigger /
setup / context TFs, `bars_per_tf`), detectors, `StopsConfig`, position-management expiry settings, session
calendars, cost model.

For every closed trigger bar in the window:

- Build the snapshot with `market/features.build_snapshot` from the bars CLOSED at that bar's close time
  (identical inputs to the pipeline; higher-timeframe features are cached by their last closed bar, which
  changes nothing numerically).
- Skip bars the live pipeline would skip on data alone: session closed, inside the no-entries window before a
  long close, spread over the symbol's `max_spread_points` or `max_spread_to_atr` (the bar's own spread
  stands in for the tick).
- For each candidate: `plan_stops` with entry = next trigger bar's open (ask for BUY), the candidate's
  invalidation and target, the stops-timeframe ATR, the symbol spec. Then `simulate` over the finest bars that
  cover the trade (M1 when the export covers the window, else M5 — recorded per signal as `resolution`).
- Costs: spread is inside the fills; `commission_per_lot` and `slippage_points` (applied adversely at entry
  and at stop exits) are converted to R with the signal's own stop distance.

Output: one `SignalOutcome` per candidate — symbol, bar time, direction, setup tag, detector version and
params hash, entry, SL/TP distances, exit reason, R net of costs, MAE/MFE in R, resolution, and the
entry-time feature values (for the pattern miner and the LLM researcher's in-sample summaries).

Determinism: same inputs → identical outcomes (tested). Parity: for a bar the portfolio replay traded, the
study's gross R equals the replay trade's R within one spread (tested on the committed synthetic fixture).

## 3. Statistics & walk-forward — `research/stats.py`, `research/walkforward.py`

Per sample of R values: n, mean (expectancy), median, win rate, profit factor, sum, max drawdown in R
(cumulative in time order), mean R per calendar month, **seeded bootstrap 90% CI of the mean** (2,000
resamples) and a **one-sided p-value** for mean > 0 (bootstrap of the centred sample). Deterministic given
the seed. Pure numpy.

Walk-forward (anchored): the pre-holdout history is cut into `folds` consecutive test windows; for each, the
parameter set with the best in-sample objective (mean R with a minimum-n floor; ties → fewer trades
dropped, then the grid's first) is chosen on all data before the window and evaluated on the window. The
concatenated out-of-sample R series is the result; the report also shows per-fold means and the share of
positive folds. A grid of one point is a plain out-of-sample split.

## 4. Trial ledger — `research/ledger.py`

Append-only JSON lines under `<repo>/data/research/ledger.jsonl` (research artefact, not engine state; it is
copied into the evidence record a strategy is enabled with). One line per evaluation:
`trial_id` (hash of hypothesis + window + data manifest), hypothesis (detector id/version, params, or DSL),
symbols, profile, window, split (`in_sample` | `walk_forward` | `holdout`), n, mean R, CI, p-value, origin
(`grid` | `llm` | `operator`), and created_at. Re-evaluating an identical trial does not add a count.

- **FDR:** Benjamini–Hochberg at `research.fdr_q` over the p-values of *every* distinct hypothesis in the
  ledger (walk-forward split). A hypothesis "survives" only if it survives against the whole ledger.
- **Holdout:** at most one `holdout` line per hypothesis per holdout window (an overlapping window counts as
  the same); a second request is refused.

Scheduled research (8.7, `scripts/research.py scheduled`, Windows task `aifund-research` after
`export_history.py --update`): due when `research.schedule_days` (7) have passed since the last run AND the
export changed (its fingerprint); rolls the holdout when due, runs the `baseline` study of every built-in
detector in `strategy.detectors` and `research.scheduled_llm_rounds` researcher rounds (with a DeepSeek key),
all with the holdout spend enabled; BH always runs over the whole ledger. A replaced or cut export that no
longer covers the recorded holdout stops research (`HoldoutError`) instead of guessing.

## 5. Entry-rule DSL detector — `strategies/dsl_detector.py`

The format hypotheses are written in (by the LLM researcher, the operator, or a playbook). Shares the
condition grammar with the learned-rule DSL (`04` §5: `all`/`any`, the same ops, registry features with
`available_at_entry=True` only, numeric thresholds within the feature's range):

```json
{
  "id": "H-0007", "version": 1, "setup_tag": "h1_trend_m15_rsi_reclaim",
  "long":  {"all": [{"feature": "h4.ema50_above_ema200", "op": "==", "value": true},
                     {"feature": "m15.rsi14", "op": "between", "value": [35, 50]}]},
  "short": "mirror",
  "invalidation": {"type": "atr", "tf": "trigger", "k": 1.5},
  "target": {"type": "rr", "rr": 2.0},
  "mechanism": "Pullbacks in an H4 uptrend that reclaim RSI mid-range resume the trend."
}
```

`"mirror"` builds the SHORT side by the registry's declared mirror of each feature (e.g. `rsi14` → 100 −
value, `ema50_above_ema200` → negation, `dist_*_atr` → sign flip); a feature without a declared mirror makes
`"mirror"` invalid (write the short side explicitly). A missing feature value → no signal.

Implementation notes (R.5):

- The grammar lives in `market/conditions.py` (validation, pure evaluation, canonical form + SHA-256,
  mirroring): `rules` and `strategies` are independent layers and may not import each other, so both use it
  from `market`. Mirrors are declared per feature in `market/feature_registry.py` (`Mirror`: SAME, NEGATE,
  COMPLEMENT (100 − v), NOT, CATEGORY (BULL↔BEAR, TREND_UP↔TREND_DOWN), optionally with a partner feature:
  `low_dist_ema50_atr` ↔ −`high_dist_ema50_atr`, `stoch_cross_up` ↔ `stoch_cross_down`, the wicks, the swing
  distances, PDH/PDL). A test checks every declared mirror against snapshots built on the price-reflected
  market; the only approximate one is `bb_width_pct_rank100` (width / SMA depends on the price level).
- Entry hypotheses may use bar and context features only (not portfolio or proposal fields) and no raw price
  levels (`close`, `atr14`): hypotheses must transfer between symbols and years. At most 6 predicates a side;
  every timeframe referenced must be in the profile.
- Levels are prices: invalidation = trigger close ∓ k·ATR, target = close ± rr·k·ATR; the live stop planner
  measures them from the real entry, adds its buffer and the spread, and clamps them — so the realised stop is
  slightly wider than k·ATR and the realised RR slightly below `rr`, as it would be live.
- `params_sha256` hashes the behaviour (setup tag, both resolved sides, levels), not the prose: a `"mirror"`
  and the same short side written out hash the same.

## 6. LLM researcher — `agents/researcher.py`, loop in `research/loop.py`

The LLM's job in research is **hypothesis generation**, not judgement:

- Input: the feature registry (names, units, descriptions, mirrors), the playbook cards, and a summary of the
  ledger restricted to **in-sample / walk-forward** results (never holdout), including failures, plus
  descriptive in-sample tables (expectancy by session, regime, HTF alignment) from signal studies.
- Output (strict schema, DeepSeek JSON mode): ≤ `research.max_hypotheses_per_run` DSL hypotheses, each with
  a mechanism; invalid ones are dropped with the validation error recorded (one repair attempt, as `03` §7.3).
- The loop evaluates each valid hypothesis by walk-forward over the pre-holdout history, writes the ledger,
  applies BH across the whole ledger, and spends the holdout only on survivors. A hypothesis that also passes
  the holdout gate becomes a **research-validated** record with a DRAFT playbook card for operator approval.
- Budget: the LLM calls go through the same adapter, budget and `llm_calls` accounting as trading calls.

Implementation notes (R.6):

- `agents/researcher.py` + prompt `researcher_v1.j2` (released, hash-pinned). Model `llm.auditor_model`
  with `auditor_timeout_s` (offline work; may be the slower reasoning model). Each proposed item is
  validated on its own (strict `EntryHypothesis`, registry, ranges, profile timeframes); if the reply is
  malformed or any item is invalid, ONE repair call lists the problems and asks for corrected versions of the
  rejected ones only; what is still invalid is dropped with its reason. Ids are assigned by the engine from
  the behaviour hash (`H-<sha[:10]>`), so an idea proposed twice is one hypothesis.
- The holdout never reaches the model, enforced twice: `research/loop.py` builds the ledger summary without
  holdout trials and the descriptive tables only from signals that entered before the holdout start (it
  raises otherwise), and the agent refuses a brief containing a holdout line.
- Descriptive tables come from a PROBE detector (alternating LONG/SHORT on every trigger bar, default stops):
  mean R by direction, session, setup-TF regime and higher-timeframe alignment.
- Features that are always null today (the news-distance features, until 5.7b) and D1-only context features
  in a profile without D1 are neither shown to the model nor accepted in a hypothesis.
- A research-validated hypothesis gets an evidence record (`config/evidence/`, §7) and a DRAFT card
  (`data/research/drafts/<id>.yaml`); the operator approves it into `config/playbooks/`.
- `scripts/research.py dsl --file ideas.json` runs operator hypotheses through the same loop (origin
  `operator`); `scripts/research.py llm --rounds N` runs the researcher (each round sees the updated ledger).

## 7. Evidence gates

| Gate | Where | Pass condition (defaults in `research:` config) |
|---|---|---|
| **E1 research-validated** | before a detector/playbook may run outside SIM | walk-forward OOS: n ≥ `min_oos_signals` (200), mean R > 0, CI lower bound > 0 net of costs, ≥ 60% positive folds; survives BH at q = `fdr_q` (0.10) against the whole ledger; holdout (single use): n ≥ `min_holdout_signals` (60), mean R > 0 |
| **E2 forward confirmation** | before PAPER → DEMO orders for that strategy | ≥ `min_forward_signals` (100) shadow virtual trades on live data dated after the analyst model's training cutoff; forward mean R > 0 and its CI overlaps the research estimate |
| **G-LLM analyst uplift** | before the analyst replaces the baseline for orders | paired comparison over ≥ `min_paired_signals` (150) bars where both decided: analyst mean R − baseline mean R (virtual, same stops) minus LLM cost per signal in R has a CI lower bound > 0; otherwise the analyst stays in shadow |

The engine enforces E1 at startup: outside SIM, every enabled detector must reference an evidence record
(`config/evidence/*.json`, produced by the research loop, hash-checked against the detector's version and
params) with `gate = E1_PASSED` or later. Implementation (R.7): `config/evidence.py` (the record lives in `config` because
the research loop writes it and the engine reads it, and those layers may not import each other). A record
matches a detector on id, version, `params_sha256` (every detector exposes it), a symbol it lists and the
profile (trigger/setup/context) it was researched with; changed parameters make it STALE. The decision
pipeline checks every (symbol, detector) it would run when it is constructed and refuses to start, listing
every problem. SIM is exempt, and so is any configuration that cannot send an order: a **dry run**
(`strategy.dry_run`), or the analyst in shadow with the baseline off (`analyst_orders: false`,
`baseline_enabled: false`) — a shadow run is how a strategy collects forward (E2) evidence. E2 and G-LLM are tracked on the dashboard and signed off in
`audit_log` (operator), like the L2/L3 rollout gates.

Implementation (R.9, G-LLM): while the analyst is enabled, every bar with a candidate records two SHADOW
virtual trades with the same stop planner — `SHADOW_BASELINE` (the strongest candidate at the baseline's
fixed confidence) and, when the analyst proposed a trade that its rules and the threshold let through,
`SHADOW_ANALYST` (virtual_trades.arm; a decision may carry one per arm). Its decisions reach the Risk Manager
only with `strategy.analyst_orders`; outside SIM that needs a sign-off for the exact prompt version and model
(`config/evidence/g_llm_<prompt>_<model>.json`, written by `scripts/uplift_report.py --sign-off NAME` only when
the gate passed; the pipeline refuses to start otherwise). Off, the baseline trades if enabled, else the
decision ends `SHADOW`. The paired report counts a bar once both shadows have finished; an arm that did not
trade earned 0R; the LLM cost is the decision's spend over the money one trade risks.

## 8. Throughput planning

Plan the calendar in **signals and trades, not weeks**. Every research report prints the measured signal
rate per symbol and the implied calendar time to: E1's `min_oos_signals`, E2's `min_forward_signals`, the
L2 gate's 100 closed trades (`06` §10), and the learning loop's first possible rule (`04` §6:
`min_matches_total` matches in one scope at ≤ 30% coverage). A strategy whose implied time to E2 exceeds
`research.max_months_to_evidence` (6) is reported as **too slow to validate**: add symbols, a faster trigger
timeframe, or a different hypothesis — do not lower the gates.
