"""Playbook card schema (roadmap 8.1, docs/00 §4): ``config/playbooks/<id>.yaml``.

A card describes one setup in words a trader (and the analyst prompt) can read: what the idea is, the
entry conditions per side, where it is wrong (the stop), where it pays (the target, the typical R), and where
it came from (the vault note it cites, or the research run that validated it). Cards arrive three ways:

- compiled from a TRADING BRAIN strategy note (``scripts/compile_playbooks.py``) as a DRAFT to curate;
- written by the research loop for a validated DSL hypothesis (docs/09 §6), also as a DRAFT;
- by hand.

Only ``status: APPROVED`` cards belong in ``config/playbooks/``; a DRAFT may still hold ``TODO`` placeholders,
an APPROVED card may not. The schema lives in ``config`` because the compiler (vault), the prompts (agents),
the detectors (engine) and the research loop all read it.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

WIKILINK = r"^\[\[[^\[\]|#]+\]\]$"
TODO = "TODO"


class CardStatus(StrEnum):
    DRAFT = "DRAFT"  # compiled or research-written; waits for the operator
    APPROVED = "APPROVED"  # curated by the operator; may ground prompts and (with a hypothesis) run


class PlaybookError(Exception):
    """A card file that is missing, unreadable or fails the schema (the message names the file)."""


class Playbook(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,59}$")
    setup_tag: str = Field(pattern=r"^[a-z0-9_]{3,40}$")  # SetupCandidate's pattern
    version: int = Field(default=1, ge=1)
    status: CardStatus = CardStatus.DRAFT
    source: str | None = Field(default=None, pattern=WIKILINK)  # the vault note, e.g. "[[nr7_note]]"
    origin: str | None = None  # research cards: who proposed the hypothesis (llm / operator / playbook)
    summary: str = Field(min_length=10, max_length=800)
    mechanism: str | None = None
    timeframe_roles: dict[str, str] | None = None
    long_rules: list[str] = Field(min_length=1)  # the entry conditions (long side)
    short_rules: str | list[str]  # "Mirror image of the long rules.", "none", or the conditions
    stop: str = Field(min_length=2)  # the invalidation
    target: str = Field(min_length=2)
    typical_r: Decimal | None = Field(default=None, gt=0, le=20)  # the first target in R, as planned
    approximations: list[str] = Field(default_factory=list)
    claimed_performance: dict[str, str] | None = None
    validation_status: str | None = None
    automation_potential: str | None = None
    hypothesis: dict[str, Any] | None = None  # a DSL entry hypothesis: the card can run as a detector
    evidence: str | None = None  # the evidence record file (research cards)
    curation_notes: list[str] = Field(default_factory=list)  # what the compiler could not extract

    @field_validator("long_rules")
    @classmethod
    def _no_empty_rules(cls, rules: list[str]) -> list[str]:
        if any(not r.strip() for r in rules):
            raise ValueError("empty rule")
        return rules

    @model_validator(mode="after")
    def _approved_is_curated(self) -> Playbook:
        if self.status is CardStatus.APPROVED:
            if TODO in yaml.safe_dump(self.model_dump(mode="json", exclude={"curation_notes"})):
                raise ValueError(f"an APPROVED card may not contain {TODO} placeholders")
            if self.curation_notes:
                raise ValueError("an APPROVED card has no curation_notes left (resolve and delete them)")
        return self

    def to_yaml(self) -> str:
        data = self.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
        if not data.get("approximations"):
            data.pop("approximations", None)
        if not data.get("curation_notes"):
            data.pop("curation_notes", None)
        if self.typical_r is not None:
            data["typical_r"] = float(self.typical_r)
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)


def parse_card(path: Path) -> Playbook:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PlaybookError(f"{path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise PlaybookError(f"{path.name}: not a YAML mapping")
    try:
        card = Playbook.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(x) for x in first["loc"]) or "card"
        raise PlaybookError(f"{path.name}: {where}: {first['msg']}") from exc
    if card.id != path.stem:
        raise PlaybookError(f"{path.name}: id {card.id!r} does not match the file name")
    return card


def load_card(directory: Path, playbook_id: str) -> Playbook:
    path = directory / f"{playbook_id}.yaml"
    if not path.is_file():
        raise PlaybookError(f"no playbook card {path.name} in {directory}")
    return parse_card(path)


def check_directory(directory: Path) -> list[str]:
    """Every card in ``directory`` against the schema: the problems found (empty = all valid)."""
    problems = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            parse_card(path)
        except PlaybookError as exc:
            problems.append(str(exc))
    return problems
