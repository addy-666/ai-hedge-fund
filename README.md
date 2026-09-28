# AI Fund OS

Autonomous multi-agent trading system for MetaTrader 5: DeepSeek LLM agents propose trades, a deterministic
risk/execution core disposes, and a statistically validated self-learning loop turns losing patterns into
versioned, reversible rules. Operated through a FastAPI + React dashboard.

**Status: under construction (Phase 0). Not for live trading.** See `docs/PROGRESS.md`.

| Where | What |
|---|---|
| `docs/00_INDEX.md` | Start here: specification index, key decisions, reality checks |
| `docs/07_ROADMAP.md` | Phased build plan with acceptance criteria |
| `AGENTS.md` | Rules for AI coding agents (and humans) working in this repo |
| `backend/` | Python engine and API (`aifund` package) |
| `frontend/` | React dashboard |
| `config/` | Trading configuration (no secrets) and playbook cards |
| `mql5/` | Guardian EA (independent safety net) |
| `deploy/windows/` | Windows VPS deployment scripts |
| `legacy/` | Original prototype, reference only |

Nothing in this repository is investment advice.
