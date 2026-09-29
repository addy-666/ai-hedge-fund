# AI Fund OS — Build Specification & Roadmap

An autonomous, 24/7, multi-agent trading system on MetaTrader 5, driven by DeepSeek LLM agents, with a
self-learning auditor loop and a React operations dashboard.

This folder is the **single source of truth** for the AI coding agent that builds the system. The agent
should read `AGENTS.md` (repo root) first, then the docs relevant to the phase it is working on.

| # | Document | What it answers | Read when |
|---|----------|-----------------|-----------|
| 00 | `00_INDEX.md` | Orientation, key decisions, reality checks | Always |
| 01 | `01_ARCHITECTURE.md` | Components, agents, processes, loops, deployment, stack, repo layout | Always |
| 02 | `02_DATA_MODEL.md` | DB tables, state machines, feature registry, config schema | Phases 0, 3, 7 |
| 03 | `03_TRADING_CORE.md` | Decision pipeline, MTF features, ATR sizing, duplicate/reversal guards, execution, reconciliation | Phases 1–5 |
| 04 | `04_LEARNING_LOOP.md` | Trade reviewer, pattern miner, LLM auditor, rule DSL, validation, shadow mode, rule injection | Phase 7 |
| 05 | `05_API_AND_DASHBOARD.md` | REST/WebSocket contract, auth, dashboard pages | Phase 6 |
| 06 | `06_OPERATIONS_AND_SECURITY.md` | Windows VPS, process supervision, alerts, backups, runbooks, rollout ladder | Phases 5, 9 |
| 07 | `07_ROADMAP.md` | Phased task list with acceptance criteria and go-live gates | Always |
| 08 | `08_PROTOTYPE_AUDIT.md` | Defects in the current prototype and where the new design fixes each | Phase 0 |
| 09 | `09_RESEARCH.md` | Signal studies, walk-forward, trial ledger, LLM researcher, evidence gates E1/E2/G-LLM | Phase R, before enabling any strategy outside SIM |

---

## 1. How to drive the AI coding agent

1. **One phase at a time, one task at a time.** Each task in `07_ROADMAP.md` is sized to be one
   pull-request-sized change with its own tests. Do not ask the agent to "build the whole system".
2. **Kickoff prompt template** (paste per task):

   ```text
   Read AGENTS.md and docs/07_ROADMAP.md. We are on Phase <N>, task <N.M>: "<title>".
   Also read the spec sections referenced by that task. Implement only this task.
   Definition of done = the task's acceptance criteria + all tests green + ruff + mypy clean.
   Before coding, list the files you will create/modify and any spec ambiguity you found.
   When done, update docs/PROGRESS.md with what was built and any deviations from spec.
   ```
3. **Spec changes go through the docs.** If the agent (or you) find the spec wrong, update the doc in
   the same change and note it in `docs/PROGRESS.md` → "Decisions log". Code and spec must not drift.
4. **Review money-path code yourself.** Anything under `risk/`, `execution/`, `reconcile/` gets a human
   read-through before it touches a demo account, regardless of test coverage.

---

## 2. Key architectural decisions (summary)

| Decision | Choice | Why |
|----------|--------|-----|
| Who decides trades | LLM agents **propose**; deterministic code (Risk Manager) **disposes** | LLM output is non-deterministic and unverifiable; money math must be testable |
| What the LLM may output | Direction, confidence, setup tag, invalidation level, thesis | Never lot size. Stops are clamped to ATR bands by code |
| Where penalties are applied | Deterministic rule engine, after the LLM | The LLM is told about lessons but **not** asked to self-penalise (avoids double-counting and silent non-compliance) |
| Learned rule format | Structured JSON DSL over a whitelisted feature registry | Free-text rules cannot be evaluated, validated or retired |
| Rule promotion | Statistical validation → shadow mode → active → periodic re-validation → retire | Small samples of losses are mostly noise; unvalidated rules overfit |
| MT5 access | One gateway thread owns the `MetaTrader5` module | The Python API is not thread-safe and is Windows-only |
| Dev on macOS | Ports & adapters: `SimBroker` + bar replay implement the same `BrokerPort` | ~80% of the system can be built and tested without MT5 |
| Runtime host | One Windows VPS (MT5 terminal + engine + API + SQLite) | Fewest moving parts that can run MT5 24/7 |
| Processes | `engine` and `api` are separate processes sharing the DB | A dashboard crash must never stop risk management |
| DB | SQLite (WAL) via SQLAlchemy 2 + Alembic | Single-host, single-writer; zero ops. Swap to Postgres is a URL change |
| Orchestration framework | Plain asyncio + explicit pipeline; **no LangChain/LangGraph** | Auditable, debuggable, fewer dependency breaks |
| Access | Dashboard reachable only over Tailscale, with login | A public trading control panel is an account-takeover risk |
| Independent safety net | MQL5 "Guardian EA" in the terminal enforcing hard equity limits | Protects the account even if Python is dead |
| When a strategy may trade | Only after evidence gates: E1 research-validated (walk-forward + whole-ledger FDR + single-use holdout), E2 forward-confirmed; the analyst only after G-LLM (paired uplift over the baseline net of cost) | Safety ≠ edge; the gates make the absence of an edge visible before money is at risk (ADR 0001) |
| What LLMs do in research | Generate hypotheses in a machine-evaluable DSL; statistics decide; holdout results never reach the LLM | LLMs are good at proposing ideas and bad at judging their own; replaying history to an LLM measures memory |

---

## 3. Reality checks (read before investing months)

- **An LLM has no demonstrated edge by default.** This architecture makes the system *safe, observable
  and self-correcting*; it does not make it profitable. Profitability has to be proven forward, on demo,
  with enough trades to mean something (see go-live gates in `07_ROADMAP.md`).
- **LLM backtests are contaminated.** Models were trained on historical price data and news. Replaying
  2023 bars to an LLM measures memory, not skill. Use replay only to test *plumbing*; measure *edge* with
  forward paper/demo trading after the model's training cutoff.
- **The learning loop learns noise if you let it.** 3 losses sharing "low volume" is not a pattern. The
  validation thresholds in `04_LEARNING_LOOP.md` exist to stop the system from eating its own tail.
- **Blocked trades produce no evidence.** Once a rule blocks a setup, you never see how it would have
  done — so the system tracks *virtual trades* for blocked signals, otherwise rules can never be retired.
- **Costs matter.** Spread + commission + swap on M15 gold/indices can exceed a thin edge. All P&L in this
  system is measured net of costs and in R-multiples.
- **Start on a demo account.** The rollout ladder (sim → paper → demo → live-micro → live) is not
  optional. Nothing in these docs is investment advice; it is engineering guidance for building the tool.

---

## 4. Relationship to the TRADING BRAIN vault

The Obsidian vault (`../TRADING BRAIN`) is the knowledge layer; this project is the execution layer.

- **Vault → Engine:** strategy notes with `automation_potential: high` are curated into *playbook cards*
  (`config/playbooks/*.yaml`) that ground the analyst agents. The engine never reads the vault at runtime
  (it runs on a Windows VPS); cards are compiled and committed.
- **Engine → Vault:** weekly audit reports and learned-rule summaries are exported as `type: review`
  notes into `wiki/reviews/`, following the vault's `SCHEMA.md` (YAML frontmatter, wikilinks). The engine
  never writes to `raw/`.

This closes the vault's own loop: LEARN → EXTRACT → … → JOURNAL → IMPROVE → AUTOMATE.
