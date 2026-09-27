# AGENTS.md — rules for AI coding agents working in this repo

You are building an autonomous trading system that places real orders. Correctness beats speed. Read this file
fully before every task, then `docs/00_INDEX.md` and the docs referenced by your task in `docs/07_ROADMAP.md`.

## Workflow

1. Work on exactly one roadmap task at a time (`docs/07_ROADMAP.md`). Do not start the next task unasked.
2. Before coding: list files to create/modify and any spec ambiguity. If the spec is wrong or ambiguous, stop and
   ask, or propose a spec edit in the same change — never silently diverge.
3. Tests first for money-path code (`risk/`, `execution/`, `reconcile/`, `rules/engine.py`).
4. Finish with: all tests green, `ruff check`, `ruff format --check`, `mypy`, `lint-imports` clean. Update
   `docs/PROGRESS.md` (what was built, deviations, follow-ups).
5. Never edit files in `legacy/` (reference only) or anything in the TRADING BRAIN vault's `raw/` folder.

## Commands (keep this list accurate)

```bash
cd backend && uv sync                      # install (macOS: without the mt5 extra)
cd backend && uv sync --extra mt5          # Windows VPS only
cd backend && uv run pytest -q
cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run lint-imports
cd backend && uv run alembic upgrade head
cd backend && BROKER=sim uv run python -m aifund.engine        # engine against SimBroker + replay
cd backend && uv run uvicorn aifund.api.app:app --reload        # API
cd frontend && npm ci && npm run dev | npm run build | npm test
cd frontend && npm run gen:api                                  # regenerate OpenAPI types
```

## Invariants — never violate, never "temporarily" bypass

1. **LLM output never reaches the broker directly.** Only `risk/manager.py` constructs `OrderIntent`; only
   `execution/executor.py` calls `BrokerPort.order_send`. `agents/` must not import `execution/` or `adapters/mt5`.
2. **The LLM never decides volume.** Stops/targets from the LLM are clamped to ATR bands (`docs/03` §10).
3. **Volume is floor-rounded to `volume_step` with `Decimal`** and never upsized beyond the overshoot policy.
4. **Persist the `order_intents` row (with its UNIQUE idempotency key) before calling `order_send`.**
   Never auto-retry `order_send` after an unknown outcome — run the UNKNOWN resolver.
5. **Only closed bars feed features** (`copy_rates_from_pos(..., 1, n)`); no lookahead in indicators or rules.
6. **Fail closed:** any exception/timeout/invalid data in the decision path → no new order.
7. **All MT5 calls go through the gateway thread.** Nothing else imports `MetaTrader5`.
8. **UTC everywhere inside the app**; broker server time is converted only in `adapters/mt5`.
9. **Rules may reference only registry features with `available_at_entry=True`.**
10. **Deterministic enforcement** of learned rules in `rules/engine.py`; prompts only *inform* the LLM.
11. **No secrets in code, logs, fixtures or prompts.** Use `.env`; logs go through the redaction processor.
12. **Released prompt versions are immutable**; create `*_v{n+1}.j2`.
13. **No new infrastructure or frameworks** (LangChain, vector DBs, Redis, Celery…) without an ADR in `docs/adr/`.
14. **Unit tests never hit the network or a real broker.** Use `SimBroker`, `FakeLLM`, `FakeClock`.

## Conventions

- Python 3.12, type hints everywhere, Pydantic v2 models for all boundaries, `Decimal` for money/volume.
- Package `aifund` under `backend/src/`. Layering per `docs/01_ARCHITECTURE.md` §12 (enforced by import-linter).
- Reason codes, close reasons, statuses are enums in `domain/` — never string literals scattered in code.
- Log with structlog, include correlation ids (`decision_id`, `intent_id`, `position_id`).
- DB access only via repositories in `persistence/repositories/`; schema changes only via Alembic.
- Frontend: TypeScript strict; API types only from generated `openapi.d.ts`.
