"""The rulebook the pipeline enforces (docs/04 §7-8): the ACTIVE and SHADOW rules, reloaded only when the
rulebook version changes (every lifecycle transition records a new version in the same transaction)."""

from __future__ import annotations

import structlog
from sqlalchemy.orm import Session, sessionmaker

from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.learning import RulebookRepository, RuleRepository
from aifund.persistence.tables import RuleRow
from aifund.ports.system import ClockPort
from aifund.rules import dsl
from aifund.rules.engine import BookRule, Rulebook, RuleEngine

log = structlog.get_logger(__name__)


def rule_of(row: RuleRow) -> dsl.Rule:
    return dsl.parse({**row.dsl, "rule_id": row.rule_id, "version": row.version})


def load(session: Session, clock: ClockPort) -> Rulebook:
    version = RulebookRepository(session, clock).version()
    rules = []
    for row in RuleRepository(session, clock).live():
        try:
            rules.append(BookRule(rule_of(row), row.status, dict(row.evidence or {})))
        except dsl.RuleError as exc:  # a stored rule the current registry refuses: skip it, loudly
            log.error("rulebook.rule_unreadable", rule_id=row.rule_id, version=row.version, error=str(exc))
    return Rulebook(version=version, rules=tuple(rules))


class RulebookCache:
    def __init__(self, factory: sessionmaker[Session], clock: ClockPort, *, max_total_penalty: int) -> None:
        self._factory = factory
        self._clock = clock
        self._max_penalty = max_total_penalty
        self._engine: RuleEngine | None = None

    def current(self) -> RuleEngine:
        """Synchronous (call it through ``asyncio.to_thread``): one cheap query unless the version changed."""
        with unit_of_work(self._factory) as s:
            version = RulebookRepository(s, self._clock).version()
            if self._engine is None or self._engine.version != version:
                self._engine = RuleEngine(load(s, self._clock), max_total_penalty=self._max_penalty)
                log.info("rulebook.loaded", version=version, rules=len(self._engine.rulebook.rules))
        return self._engine
