# 04 — Self-Learning Auditor Loop

Goal: find setups that **reliably lose**, turn them into machine-evaluable rules, prove them on data the
discovery step did not see, trial them in shadow mode, enforce them deterministically, show them to the
analyst as lessons, and retire them when the evidence fades.

```text
closed trades + virtual trades (entry features, outcome in R)
      │
      ├─► Trade Reviewer (LLM, per trade)   → mistake tags, thesis verdict, lesson text
      ├─► Pattern Miner (statistics)        → ranked loss clusters with effect sizes + CIs
      ▼
Auditor (LLM, periodic)                     → candidate rules in DSL + hypotheses + qualitative lessons
      ▼
Rule Validator (statistics)                 → discovery/holdout test, coverage, dedup → pass/fail
      ▼
SHADOW  (evaluated & logged, not enforced)  → shadow evidence from real + virtual trades
      ▼
ACTIVE  (penalty | risk_scale | block)      → Rule Engine at decision time + lessons in prompt
      ▼
Re-validation every 30 days / expiry 90 days → keep | RETIRED
```

---

## 1. Design rules

1. **Rules condition only on entry-time information** (`available_at_entry=True` features and proposal fields).
   Outcome, MAE/MFE, reviewer tags are *explanations*, never conditions.
2. **Wins and losses are both analysed.** A condition present in 80% of losses is meaningless if it is present in
   80% of wins. Everything is measured as *expectancy in R of matched vs unmatched trades*.
3. **The LLM proposes hypotheses; statistics decide.** The auditor LLM can only reference registry features,
   and its rules are rejected unless they pass the validator.
4. **Penalty only (v1).** Rules can lower confidence, scale risk down, or block. No "boost" rules — positive
   rules are the fastest way to overfit and increase risk.
5. **Versioned, reversible, explainable.** Every decision records the rulebook version and matched rules.
6. **Bounded influence.** Max total penalty, max active rules, max coverage per rule.

---

## 2. Trade Reviewer (LLM, per closed trade) — `agents/reviewer.py`

Input: entry snapshot (compact), analyst thesis & key risks, lessons shown, rules matched, SL/TP plan,
outcome (R, close reason), MAE/MFE in R, bars held, a compact path summary (R at 25/50/75/100% of holding
time), and the trigger-TF candles during the trade.

Output (strict schema):

```python
class TradeReview(BaseModel):
    tags: list[MistakeTag] = Field(max_length=4)
    thesis_verdict: Literal["CORRECT", "WRONG", "UNCLEAR"]
    execution_quality: int = Field(ge=1, le=5)
    lesson: str = Field(max_length=400)
```

`MistakeTag` fixed taxonomy (extend only via spec change):
`COUNTER_HTF_TREND`, `CHASED_EXTENSION`, `LOW_VOLATILITY_CHOP`, `VOLATILITY_SPIKE`, `NEWS_EVENT`,
`STOP_TOO_TIGHT`, `STOP_TOO_WIDE`, `TARGET_TOO_AMBITIOUS`, `LATE_SESSION_ENTRY`, `RANGE_MIDDLE_ENTRY`,
`FAILED_BREAKOUT`, `THESIS_INVALIDATED_EARLY`, `SPREAD_OR_COST_DRAG`, `GOOD_TRADE_BAD_OUTCOME`,
`GOOD_TRADE_GOOD_OUTCOME`, `LUCKY_WIN`.

`GOOD_TRADE_BAD_OUTCOME` matters: it stops the auditor from "learning" from variance. Reviews also run on
winners (so `LUCKY_WIN` can be detected).

---

## 3. Pattern Miner (deterministic) — `rules/miner.py`

The miner sees only the DISCOVERY split (the oldest `1 − holdout_fraction` of the window by entry time); the
holdout is the validator's. A scope holding exactly the samples of a broader one is not tested twice.

Dataset: closed real trades + closed virtual trades in the last `window_days`, each with its entry snapshot
(same `feature_set_version` family), `r_multiple`, symbol, direction, setup_tag, `virtual` flag.

Algorithm:

```text
baseline = mean R of all trades in scope (also per symbol/direction/setup_tag scopes)
for each scope in [ALL, per symbol, per setup_tag, per (symbol, direction), per (setup_tag, direction)]:
    for each feature f in registry (entry-time only):
        numeric   → quantile bins (quintiles, computed on the discovery split only)
        bool/cat  → each value
        compute for bin b: n, win_rate, mean_R, sum_R; complement stats
        effect = mean_R(b) − mean_R(not b)
    for the top-10 single-feature bins by |effect| (n ≥ min_matches_total/2):
        try pairwise conjunctions with other top bins (depth ≤ 2 in the miner; the auditor may add a 3rd)
    keep bins with effect ≤ −min_effect_r and n ≥ min_matches_total
    bootstrap 2,000× → 90% CI of mean_R(b) and of effect
    p-value: permutation test of effect — the exact permutation variance of the difference with a normal tail
             and a continuity correction (1,000 Monte Carlo permutations cannot resolve the p ≤ q/m that BH
             needs over thousands of bins; PROGRESS decisions log 2026-09-30)
apply Benjamini–Hochberg across ALL tested bins at q = fdr_q
output: top 20 surviving clusters, plus the 10 strongest non-surviving ones marked "weak"
```

Also emits descriptive tables for the auditor and dashboard: expectancy by symbol, setup_tag, session,
regime, htf_alignment, confidence bucket, reviewer tag frequency among losses vs wins.

Cross-asset features (roadmap 10.5, `02` §3) are ordinary registry features to the miner, the validator, the
auditor and the rule engine: a loss cluster can live in another market ("gold longs while EURUSD's 24-bar
move is below −1σ"), and its rule references `xa.eurusd.ret24_z`. When that market is shut the feature is
null: the sample does not fall in the bin, and at decision time the rule does not match (it cannot judge, so
it neither penalises nor blocks). Rules still only reduce risk.

---

## 4. Auditor agent (LLM) — `agents/auditor.py`

Triggers: nightly at `audit_schedule_utc`; or `audit_min_new_trades` new closed trades since the last run
(respecting `audit_cooldown_hours`); or manual (`RUN_AUDIT` command).

Input:

- Feature registry (names, types, units, descriptions) — the only vocabulary allowed in conditions.
- Miner output (surviving + weak clusters with n, win rate, mean R, CI, p/q values).
- Descriptive tables; reviewer tag frequencies (losses vs wins).
- 10–20 sampled trade narratives (worst R, plus a few wins in the same clusters for contrast).
- Current active and shadow rules (to avoid duplicates and to suggest merges/retirements).

Output schema:

```python
class AuditorOutput(BaseModel):
    candidate_rules: list[CandidateRule] = Field(max_length=5)
    retire_suggestions: list[RetireSuggestion] = []     # rule ids + reasoning (validator still decides)
    lessons_markdown: str = Field(max_length=4000)      # qualitative; goes to report + vault, never enforced
    strategy_level_findings: list[str] = []             # e.g. "setup X negative overall" → human attention
```

Each `CandidateRule` = DSL (§5) + `hypothesis` (a *mechanism*: why would this lose?) + `cited_clusters`
(miner cluster ids it is based on). Candidates without a cited SURVIVING cluster are discarded (no invented
patterns; a weak cluster is context, not evidence). A candidate already live under the same scope and
condition is not registered again. Without an LLM (no API key) the surviving clusters are the candidates.

Model: the auditor may use a slower reasoning model (`llm.auditor_model`) with `auditor_timeout_s`.

---

## 5. Rule DSL — `rules/dsl.py`

```json
{
  "rule_id": "R-0042",
  "version": 1,
  "scope": {
    "symbols": ["XAUUSD"],              // canonical; [] = all
    "directions": ["LONG"],             // [] = both
    "setup_tags": ["mtf_trend_pullback"],
    "trigger_tfs": []
  },
  "conditions": {
    "all": [
      {"feature": "h1.rsi14", "op": ">", "value": 70},
      {"feature": "m15.atr14_pct_rank100", "op": ">=", "value": 0.9}
    ]
  },
  "action": {"type": "penalty", "points": 20},
  "hypothesis": "Late-trend longs into volatility expansion get stopped by mean reversion spikes.",
  "cited_clusters": ["C-2026-09-28-07"]
}
```

- `conditions`: `all` (AND) of up to `max_rule_conditions` predicates; `any` supported one level deep only.
- `op`: `<`, `<=`, `>`, `>=`, `==`, `!=`, `in`, `not_in`, `between` (`value: [lo, hi]`).
- `action.type`: `penalty` (`points` 5–30), `risk_scale` (`factor` 0.25–0.75), `block`.
- `prop.*` features describe the candidate trade (direction, setup, raw confidence, HTF alignment);
  `prop.sl_atr_multiple` and `prop.rr_target` are refused: they exist only after the stop is planned, which is
  after the rules run.
- Validation (Pydantic): feature exists in registry with `available_at_entry=True`; value type matches dtype;
  category values valid; numeric thresholds within the feature's observed range; scope values valid.
- Evaluation is pure: `evaluate(rule, features: dict, proposal) -> bool`. A missing feature → rule does not
  match (and is counted in `rule_evaluations` as `missing_feature` for diagnostics).
- Canonical form (sorted keys, normalised numbers) → `dsl_sha256` for dedup.

Operators may also author rules by hand in the dashboard (origin `OPERATOR`); they go through the same
validator and shadow path unless the operator explicitly force-activates (audit-logged).

---

## 6. Rule Validator — `rules/validator.py`

Split the window by **time**: oldest `1 − holdout_fraction` = discovery, newest `holdout_fraction` = holdout.
(The miner only saw discovery data for the bin edges; the validator re-uses the exact rule thresholds.)

A candidate **passes** only if all hold:

| Check | Default |
|---|---|
| Matches in whole window | ≥ `min_matches_total` (20) |
| Matches in holdout | ≥ `min_matches_holdout` (6) |
| Effect (mean R matched − mean R unmatched) in discovery | ≤ −`min_effect_r` (−0.30 R) |
| Effect in holdout | same sign, ≤ −0.5 × `min_effect_r` |
| 90% bootstrap CI upper bound of mean R (matched, whole window) | < 0 |
| Coverage (share of trades matched) | ≤ `max_rule_coverage` (30%) — above that it is a *strategy-level finding*, escalated to the operator, not a rule |
| Counterfactual | Removing matched trades improves total R in holdout **and** does not remove > 25% of gross winning R |
| Complexity | ≤ `max_rule_conditions` predicates |
| Dedup | Jaccard similarity of matched-trade sets with any active/shadow rule < 0.8 (else: propose as new version of that rule, keep the stronger) |
| Virtual share | If > 50% of matched trades are virtual, require n thresholds × 1.5 (virtual fills are approximations) |

**Action severity** is set by the validator (the auditor's suggestion is advisory):

```text
mean_R_matched ≤ −0.6 and CI upper < −0.2 and n ≥ 30   → block        (always requires operator approval)
mean_R_matched ≤ −0.3                                   → risk_scale 0.5 (approval required)
otherwise                                               → penalty = clamp(round(−effect × 40), 5, 30)
```

Validation output (all numbers) is stored in `rules.evidence` and shown in the dashboard's Learning Lab.

---

## 7. Rule Engine (decision time) — `rules/engine.py`

```text
# applies to whichever decision is taken — the deterministic baseline as much as the analyst — and to both
# G-LLM shadow arms, so the comparison stays fair
in_scope   = rules whose scope matches (symbol, direction, setup_tag, trigger_tf)
matched    = [r for r in in_scope if evaluate(r, snapshot.features, proposal)]
active     = [r for r in matched if r.status == ACTIVE]
shadow     = [r for r in matched if r.status == SHADOW]      # logged only

if any(r.action.type == "block" for r in active):   → RULE_BLOCKED (virtual trade created)
penalty     = min(Σ r.points for penalty rules in active, max_total_penalty)
risk_factor = max(Π r.factor for risk_scale rules in active, 0.25)
log rule_evaluations for active and shadow matches
```

**Lessons shown in the prompt** (before the LLM call): rules in scope for the symbol and candidate setups whose
conditions are *currently matched or within 10% of their thresholds*, top 8 by severity, in the fixed wording
from `03_TRADING_CORE.md` §7.1. The LLM is told the engine applies penalties itself; it uses lessons only to
reconsider its thesis. This avoids double-penalising and makes enforcement independent of LLM compliance.
Discrepancies between `lessons_considered` (LLM) and actual matches are logged — frequent mismatches point to
confusing feature definitions.

---

## 8. Lifecycle — `rules/lifecycle.py`

| Transition | Condition |
|---|---|
| CANDIDATE → SHADOW | Validator pass |
| CANDIDATE → REJECTED | Validator fail (reason and numbers kept — rejected ideas are knowledge too) |
| SHADOW → ACTIVE | ≥ `shadow_min_matches` live matches (real + virtual) **and** their mean R < 0 **and** (penalty ≤ `auto_promote_max_penalty` **or** operator approval) |
| SHADOW → REJECTED | `shadow_max_days` elapsed without enough matches (re-proposable later), or shadow mean R ≥ +0.1 |
| ACTIVE → ACTIVE | Re-validation at `review_at` on rolling window incl. virtual trades of blocked signals passes → `review_at += review_after_days` |
| ACTIVE → RETIRED | Re-validation fails twice in a row; or `expires_at` reached without renewal; or operator retires; or `max_active_rules` exceeded (weakest effect retires) |

Every transition: new `rulebook_versions` row, `events` entry, Telegram notice for ACTIVE/RETIRED/block
approvals needed.

Why virtual trades matter here: once a rule blocks or down-weights a setup, the real-trade sample for that
setup dries up. Virtual trades keep measuring what the blocked setup *would* have done, so a rule whose
premise stops being true can be retired instead of blocking good trades forever.

---

## 9. Confidence calibration — `rules/calibration.py`

- Collect (raw LLM confidence, outcome WIN/LOSS, R) for real + virtual trades.
- Until ≥ 150 samples: identity mapping (dashboard shows reliability chart only).
- Afterwards, weekly: fit isotonic regression of win probability on confidence using time-ordered 70/30 split;
  activate only if Brier score on the 30% improves by ≥ 5%. Map to 0–100 scale.
- The threshold then operates on calibrated confidence. Reliability diagram and Brier history in dashboard.

Implementation (8.5): `rules/calibration.py` (pure: pool-adjacent-violators isotonic fit — no scikit-learn
dependency for 20 lines —, Brier, reliability bins), `engine/calibration.py` (weekly fit per source, the
pipeline's cache), `persistence/repositories/calibration.py` (samples). Per **source**: the analyst (raw
confidence of its proposals, decided or in shadow; outcome = its real trade, else its SHADOW_ANALYST virtual
trade, else — when it decided and was blocked — the blocked signal's virtual trade) and the committee
(confidence after the critic; SHADOW_COMMITTEE). Win = net R > 0. The map interpolates between fitted points
and, **outside the fitted confidence range, may only lower a confidence**. `learning.calibration.activation`
(operator decision 2026-09-30): `approve` (default) stores a passing fit as a CANDIDATE and notifies; the
operator activates it (Analytics page, re-auth, or `APPROVE_CALIBRATION {version}`); `auto` activates at once.
A fit that misses the 5% stores REJECTED (Brier history); fewer than `min_samples` stores nothing. Activation
retires the source's previous model; `FIT_CALIBRATION` fits now. Weekly at `fit_weekday` / `fit_utc`.

This answers the question "does the LLM's 80 actually mean anything?" with data.

---

## 10. Outputs to humans and the vault

- **Audit report** per run (Markdown, stored in `audit_runs.lessons_md`): miner tables, candidates with
  pass/fail reasons, lifecycle changes, strategy-level findings.
- **Weekly review export** to the vault (`vault/review_exporter.py`) → `TRADING BRAIN/wiki/reviews/
  ai-fund-review-YYYY-Www.md` with frontmatter:

  ```yaml
  ---
  title: "AI Fund Weekly Review 2026-W40"
  type: review
  created: 2026-10-05
  tags: [review/ai-fund, system/learning-loop]
  sources: ["[[ai_fund_engine]]"]
  ---
  ```

  Contents: P&L in R by setup/symbol, active/retired rules with evidence, top lessons, links to the relevant
  strategy notes (`[[high_probability_multi_timeframe_trend_pullback]]`). The engine host cannot see the Mac
  vault directly, so the exporter writes into `exports/vault/` and the API serves it; a Mac-side script
  (`scripts/pull_vault_reviews.py`) downloads new reports into the vault. Never write to `raw/`.

---

## 11. Guardrails summary (why this will not eat its own tail)

| Risk | Mitigation |
|---|---|
| Learning from noise (tiny samples) | n thresholds, bootstrap CIs, permutation tests, FDR control |
| Overfitting thresholds | Time-based holdout; miner bins fixed on discovery only; complexity ≤ 3 |
| Lookahead leakage | Only `available_at_entry` features in conditions |
| Rules that never die | Virtual trades + scheduled re-validation + expiry |
| Rulebook bloat / conflicting rules | Dedup by matched-set Jaccard; `max_active_rules`; `max_total_penalty` |
| LLM ignoring lessons | Deterministic enforcement; LLM-side lessons are advisory |
| LLM inventing patterns | Candidates must cite miner clusters; validator is the only gate |
| Auto-escalating risk | No boost rules; risk_scale ≤ 1; blocks need approval |
| Silent drift in behaviour | Rulebook version on every decision; dashboard diff between versions |
