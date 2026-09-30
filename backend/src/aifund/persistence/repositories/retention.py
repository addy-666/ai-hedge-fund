"""Data retention (docs/06 §8, roadmap 9.5): what is thinned, and when.

- ``events`` older than 30 days are deleted (the live feed), except the rollout gates' evidence
  (``EVIDENCE_EVENTS``: restarts, positions found without a stop, ledger mismatches — a 4-week L2 period
  needs all of them, docs/06 §10);
- ``llm_calls`` older than 180 days lose the full text (``messages``, ``response_text``); the prompt hash,
  model, tokens, cost, latency, validity and the parsed result stay;
- everything else is kept forever: it is the learning dataset.

Run only after a verified backup (``scripts/backup_db.py`` does it): nothing is lost that is not backed up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, null, update
from sqlalchemy.orm import Session

from aifund.persistence.tables import EventRow, LLMCallRow

EVENTS_DAYS = 30
LLM_TEXT_DAYS = 180
EVIDENCE_EVENTS = ("engine.state", "position.sl_missing", "ledger.mismatch")


@dataclass(frozen=True)
class RetentionResult:
    events_deleted: int
    llm_calls_truncated: int


class RetentionRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def apply(
        self, now: datetime, *, events_days: int = EVENTS_DAYS, llm_text_days: int = LLM_TEXT_DAYS
    ) -> RetentionResult:
        events = self._s.execute(
            delete(EventRow).where(
                EventRow.ts < now - timedelta(days=events_days), EventRow.type.not_in(EVIDENCE_EVENTS)
            )
        )
        calls = self._s.execute(
            update(LLMCallRow)
            .where(
                LLMCallRow.created_at < now - timedelta(days=llm_text_days),
                (LLMCallRow.messages.is_not(None)) | (LLMCallRow.response_text.is_not(None)),
            )
            .values(messages=null(), response_text=None)  # SQL NULL: a JSON None would store 'null'
        )
        return RetentionResult(events.rowcount or 0, calls.rowcount or 0)  # type: ignore[attr-defined]
