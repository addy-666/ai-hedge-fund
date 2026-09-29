# AGENTS.md — rules for AI coding agents working in this repo

You are building an autonomous trading system that places real orders. Correctness beats speed. Read this file
fully before every task, then `docs/00_INDEX.md` and the docs referenced by your task in `docs/07_ROADMAP.md`.

## Workflow

1. Work on exactly one roadmap task at a time (`docs/07_ROADMAP.md`). Do not start the next task unasked.
2. Before coding: list files to create/modify and any spec ambiguity. If the spec is wrong or ambiguous, stop and
   ask, or propose a spec edit in the same change — never silently diverge.
3. Tests first for money-path code (`risk/`, `execution/`, `reconcile/`, `rules/engine.py`).
4. Finish with: all tests green, the coverage gate, `ruff check`, `ruff format --check`, `mypy`, `lint-imports` clean. Update
   `docs/PROGRESS.md` (what was built, deviations, follow-ups).
5. Never edit files in `legacy/` (reference only) or anything in the TRADING BRAIN vault's `raw/` folder.

## Commands (keep this list accurate)

Working now (verified at the end of Phase 0; Python 3.12 via uv):

```bash
cd backend && uv sync --python 3.12                # install incl. dev tools (macOS: no MT5 extra needed)
cd backend && uv sync --python 3.12 --extra mt5    # Windows VPS only: adds the MetaTrader5 wheel
cd backend && uv run pytest -q
cd backend && uv run pytest -q --cov --cov-report=json && uv run python scripts/coverage_gate.py   # CI gate: money path 100%
cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports
cd backend && uv run alembic upgrade head          # DATABASE_URL, default <repo>/data/aifund.db
cd backend && uv run alembic revision --autogenerate -m "<change>"   # then review the generated file
cd backend && uv run python scripts/mt5_smoke.py   # Windows + MT5 terminal only: READ-ONLY checks on a demo account
cd backend && uv run python scripts/export_history.py --months 15  # Windows: bars -> <repo>/data/history (Parquet)
cd backend && uv run python scripts/replay.py --from 2026-06-15 --to 2026-09-27   # full stack on exported history (SimBroker); --snapshot-minutes 1 for engine cadence
cd backend && uv run python scripts/research.py baseline   # edge study: grid, walk-forward, gate E1 (docs/09); --spend-holdout once
cd backend && uv run python scripts/research.py dsl --file ideas.json   # operator entry hypotheses (docs/09 §5) through the research loop
cd backend && uv run python scripts/research.py llm --rounds 1   # LLM researcher proposes, the loop judges (DEEPSEEK_API_KEY, llm.pricing, migrated DB)
cd backend && uv run python scripts/spread_report.py   # spread vs ATR per symbol: evidence for the spread gates
cd backend && uv run python scripts/capture_deals.py  # Windows, DEMO: record deal history -> tests/fixtures/mt5_deals (READ-ONLY)
```

Not yet available (the phase that adds each is in brackets; update this list when it lands):

```bash
cd backend && BROKER=sim uv run python -m aifund.engine        # [Phase 5] engine against SimBroker + replay
cd backend && uv run uvicorn aifund.api.app:app --reload        # [Phase 6] API
cd frontend && npm ci && npm run dev | npm run build | npm test  # [Phase 6]
cd frontend && npm run gen:api                                  # [Phase 6] regenerate OpenAPI types
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

- `.env`, `CONFIG_PATH` and relative SQLite paths resolve against the repo root (`config/settings.py`
  `PROJECT_ROOT`), so commands behave the same from the repo root or `backend/`. Keep `.env` at the repo root.

- Python 3.12, type hints everywhere, Pydantic v2 models for all boundaries, `Decimal` for money/volume.
- Package `aifund` under `backend/src/`. Layering per `docs/01_ARCHITECTURE.md` §12 (enforced by import-linter).
- Reason codes, close reasons, statuses are enums in `domain/` — never string literals scattered in code.
- Log with structlog, include correlation ids (`decision_id`, `intent_id`, `position_id`).
- DB access only via repositories in `persistence/repositories/`; schema changes only via Alembic.
  Repositories are synchronous; async code calls them through `asyncio.to_thread` in short transactions.
- Money/price columns use `DecimalText`, timestamps `UtcDateTime` (never plain `Numeric`/`DateTime` —
  SQLite would round money through binary float). Add a table → add a migration → the drift test must pass.
- Only `aifund.risk` may import `aifund.domain._issuance`; the executor rejects intents that were not issued
  (import-linter enforces this).
- Frontend: TypeScript strict; API types only from generated `openapi.d.ts`.
