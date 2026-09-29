# ADR 0001 — Edge evidence before intelligence

- Status: accepted (operator request 2026-09-29)
- Supersedes: the build order in `07_ROADMAP.md` ("plumbing → LLM → ops → UI → learning")

## Context

Phases 0–4.6 built safe plumbing and an LLM analyst. The system still has no evidence that any strategy
it runs has a positive expectancy net of costs:

- the only detector (`mtf_trend_pullback`) fired ~9 times a month across three symbols in replay, with a
  small negative result — far too few trades to judge anything;
- the walk-forward research harness sat in the Phase 10 backlog;
- no gate required the LLM analyst to beat the deterministic baseline, and the L3 go-live bar was only
  "expectancy ≥ 0";
- the learning loop only removes losing subsets (penalty-only rules). It cannot create an edge, and at the
  measured signal rate its first validated rule was 6–12 months away.

## Decision

1. Insert **Phase R — Edge research & evidence gates** (`09_RESEARCH.md`) before the LLM analyst may trade
   (4.7 onwards) and before Phases 5–9 continue.
2. Research measures **every detector signal** as a counterfactual trade using the production stop planner
   and fill rule, with walk-forward parameter selection, a whole-ledger false-discovery control and a
   single-use holdout.
3. Use LLMs where they can plausibly help without contaminating the evidence: **generating hypotheses** in
   a machine-evaluable DSL, judged only by statistics on data the LLM never saw results for, then confirmed
   forward on data after the model's training cutoff.
4. Add enforceable gates: **E1** (research-validated, enforced by the engine outside SIM), **E2** (forward
   confirmation) and **G-LLM** (the analyst must show paired uplift over the baseline net of its cost).
5. Move the Guardian EA and the news gate into Phase 5 (before the 24/7 demo run), and enforce branch
   coverage in CI (money path to 100%).

## Consequences

- No new infrastructure: pure numpy statistics, JSON-lines ledger, the existing LLM adapter and budget.
- Strategies may be rejected by the gates — that is the intended outcome when there is no edge. The gates
  must not be lowered to let a strategy through; change the hypothesis instead.
- An LLM-proposed strategy is idea-level data-snooped (the model has read about past markets); E2 forward
  confirmation is therefore mandatory, and it costs calendar time proportional to the signal rate.
- Nothing here guarantees profitability. It makes the absence of an edge visible early and cheaply.
