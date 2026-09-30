"""Playbook card schema (roadmap 8.1): every committed card validates; APPROVED cards are fully curated."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from aifund.config.playbooks import (
    CardStatus,
    Playbook,
    PlaybookError,
    check_directory,
    load_card,
    parse_card,
)
from aifund.config.settings import PROJECT_ROOT
from aifund.domain.enums import Timeframe

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "playbooks"


def card(**over: Any) -> dict[str, Any]:
    return {
        "id": "nr7",
        "setup_tag": "nr7_breakout",
        "status": "APPROVED",
        "source": "[[high_probability_nr7_volatility_breakout]]",
        "summary": "Volatility contraction resolves into expansion.",
        "long_rules": ["trend up", "NR7 bar", "break of its high"],
        "short_rules": "Mirror image of the long rules.",
        "stop": "NR7 low - 0.25 ATR",
        "target": "2R",
        "typical_r": 2.0,
        **over,
    }


def test_the_committed_cards_are_valid_and_approved() -> None:
    directory = PROJECT_ROOT / "config" / "playbooks"
    assert check_directory(directory) == []
    for path in directory.glob("*.yaml"):
        assert parse_card(path).status is CardStatus.APPROVED, path.name
    assert check_directory(FIXTURES) == []


@pytest.mark.parametrize(
    ("over", "error"),
    [
        ({"summary": "TODO: summary"}, "TODO"),
        ({"curation_notes": ["check the stop"]}, "curation_notes"),
        ({"source": "a book"}, "source"),
        ({"setup_tag": "NR7"}, "setup_tag"),
        ({"long_rules": []}, "long_rules"),
        ({"long_rules": [" "]}, "empty rule"),
        ({"typical_r": 0}, "typical_r"),
        ({"unknown": 1}, "unknown"),
    ],
)
def test_the_schema_rejects(over: dict[str, Any], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        Playbook.model_validate(card(**over))


def test_a_draft_may_hold_placeholders() -> None:
    draft = Playbook.model_validate(card(status="DRAFT", stop="TODO: stop", curation_notes=["check"]))
    assert draft.status is CardStatus.DRAFT
    text = draft.to_yaml()
    assert "curation_notes" in text
    assert "typical_r: 2.0" in text
    assert "approximations" not in text  # empty lists are left out of the file
    assert Playbook.model_validate(yaml.safe_load(text)) == draft


def test_files(tmp_path: Path) -> None:
    (tmp_path / "nr7.yaml").write_text(yaml.safe_dump(card()), encoding="utf-8")
    assert load_card(tmp_path, "nr7").typical_r == 2
    (tmp_path / "other.yaml").write_text(yaml.safe_dump(card()), encoding="utf-8")
    (tmp_path / "list.yaml").write_text("- a\n", encoding="utf-8")
    (tmp_path / "broken.yaml").write_text("a: [\n", encoding="utf-8")
    (tmp_path / "bad.yaml").write_text(yaml.safe_dump(card(id="bad", stop="")), encoding="utf-8")
    problems = check_directory(tmp_path)
    assert len(problems) == 4
    assert any("other.yaml: id 'nr7' does not match the file name" in p for p in problems)
    assert any("list.yaml: not a YAML mapping" in p for p in problems)
    assert any(p.startswith("broken.yaml:") for p in problems)
    assert any(p.startswith("bad.yaml: stop:") for p in problems)
    with pytest.raises(PlaybookError, match=r"no playbook card missing\.yaml"):
        load_card(tmp_path, "missing")


def test_every_built_in_detector_has_its_card() -> None:
    from aifund.engine.detectors import BUILT_IN
    from aifund.strategies.base import TfRoles

    directory = PROJECT_ROOT / "config" / "playbooks"
    roles = TfRoles(trigger=Timeframe.M15, setup=Timeframe.H1, context=(Timeframe.H4,))
    for detector_id, build in BUILT_IN.items():
        detector = build(roles)
        card = load_card(directory, detector.playbook_id)
        assert card.setup_tag == detector.setup_tag == detector_id
        assert card.source is not None, detector_id  # cites its vault note
