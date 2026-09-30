"""Playbook compiler (roadmap 8.1): vault strategy notes → schema-valid DRAFT cards, never into config/."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from aifund.config.playbooks import TODO, CardStatus, Playbook, parse_card
from aifund.config.settings import PROJECT_ROOT
from aifund.vault import playbook_compiler as pc

SCRIPT = PROJECT_ROOT / "backend" / "scripts" / "compile_playbooks.py"

NR7 = """---
title: "NR7 Breakout"
type: strategy
win_rate: 55-65%
profit_factor: 2.0-2.8
---

# NR7 Breakout

The **NR7 breakout** exploits the transition from [[volatility|volatility compression]] to expansion in the
direction of the dominant trend.

## 1. Setup Identification

1. **Trend Filter**: EMA50 > EMA200 on the daily chart.
2. **NR7 Condition**: the narrowest range of the last 7 bars.

```text
│ **3. Initial Stop** │ Place the stop below the NR7 low - 0.25 ATR │
│ **5. First Target** │ Scale out at +2.0R │
```

```python
# 1. not a rule: code
stop = low - atr
```

## Short setup (bearish)

1. Mirror: EMA50 < EMA200.
2. Break of the NR7 low.

## Related Notes
- [[stop_placement_notes]]
"""

EA = """---
type: strategy
automation_potential: high
validation_status: untested
---

# Grid EA

Tiny.

- no stop loss on any position; the basket averages down in a grid.
"""

CONCEPT = """---
type: concept
automation_potential: high
---
# A concept

Concepts are never compiled unless named, and they have no rules to extract here at all.
"""


def vault(tmp_path: Path) -> Path:
    root = tmp_path / "TRADING BRAIN"
    (root / "wiki" / "strategies").mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir()
    (root / "raw").mkdir()
    (root / "SCHEMA.md").write_text("schema", encoding="utf-8")
    (root / "wiki" / "strategies" / "nr7_note.md").write_text(NR7, encoding="utf-8")
    (root / "wiki" / "strategies" / "grid_ea.md").write_text(EA, encoding="utf-8")
    (root / "wiki" / "strategies" / "plain.md").write_text("no frontmatter at all\n", encoding="utf-8")
    (root / "wiki" / "concepts" / "a_concept.md").write_text(CONCEPT, encoding="utf-8")
    return root


def test_selection_eligible_notes_plus_named(tmp_path: Path) -> None:
    root = vault(tmp_path)
    assert [cid for _, cid in pc.select(root, [])] == ["grid_ea"]
    picked = pc.select(root, ["nr7_note=nr7_breakout", "a_concept"])
    assert [cid for _, cid in picked] == ["grid_ea", "nr7_breakout", "a_concept"]
    with pytest.raises(pc.CompileError, match=r"no note missing\.md"):
        pc.select(root, ["missing"])


def test_a_note_compiles_into_a_draft(tmp_path: Path) -> None:
    note = pc.read_note(vault(tmp_path) / "wiki" / "strategies" / "nr7_note.md")
    card = pc.compile_note(note, "nr7_breakout")
    assert card.status is CardStatus.DRAFT
    assert card.source == "[[nr7_note]]"
    assert card.summary.startswith("The NR7 breakout exploits the transition from volatility compression")
    assert card.long_rules == [
        "Trend Filter: EMA50 > EMA200 on the daily chart.",
        "NR7 Condition: the narrowest range of the last 7 bars.",
    ]
    assert card.short_rules == ["Mirror: EMA50 < EMA200.", "Break of the NR7 low."]
    assert card.stop == "3. Initial Stop Place the stop below the NR7 low - 0.25 ATR"
    assert card.target == "5. First Target Scale out at +2.0R"
    assert card.typical_r == 2
    assert card.claimed_performance == {
        "win_rate": "55-65%",
        "profit_factor": "2.0-2.8",
        "status": "UNVALIDATED",
    }
    assert card.curation_notes == pc.ALWAYS


def test_what_is_not_found_becomes_todo(tmp_path: Path) -> None:
    root = vault(tmp_path)
    ea = pc.compile_note(pc.read_note(root / "wiki" / "strategies" / "grid_ea.md"), "grid_ea")
    assert ea.summary.startswith(TODO)
    assert ea.long_rules[0].startswith(TODO)
    assert ea.short_rules.startswith(TODO)  # type: ignore[union-attr]
    assert ea.target.startswith(TODO)  # type: ignore[union-attr]
    assert ea.stop == "no stop loss on any position; the basket averages down in a grid."
    assert ea.validation_status == "untested"
    assert ea.automation_potential == "high"
    assert any(n.startswith("WARNING") for n in ea.curation_notes)
    assert len(ea.curation_notes) == len(pc.ALWAYS) + 5
    short_id = pc.compile_note(pc.read_note(root / "wiki" / "strategies" / "plain.md"), "ab")
    assert short_id.setup_tag == "ab_setup"
    assert short_id.claimed_performance is None


@pytest.mark.parametrize(
    ("text", "r"),
    [("+1.5R to +2.0R", 1.5), ("2:1 R:R minimum", 2), ("the opposite wall", None), (None, None)],
)
def test_typical_r(text: str | None, r: float | None) -> None:
    assert pc.typical_r(text) == (None if r is None else pytest.approx(r))


def test_drafts_are_written_once_and_never_into_config(tmp_path: Path) -> None:
    cards = [pc.compile_note(n, cid) for n, cid in pc.select(vault(tmp_path), ["nr7_note"])]
    out, approved = tmp_path / "drafts", tmp_path / "config"
    written, skipped = pc.write_drafts(cards, out, approved_dir=approved)
    assert [p.name for p in written] == ["grid_ea.yaml", "nr7_note.yaml"]
    assert skipped == []
    assert parse_card(out / "nr7_note.yaml").status is CardStatus.DRAFT
    header = (out / "nr7_note.yaml").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("# DRAFT compiled from [[nr7_note]]")
    written, skipped = pc.write_drafts(cards, out, approved_dir=approved)
    assert written == []
    assert len(skipped) == 2
    written, _ = pc.write_drafts(cards, out, approved_dir=approved, force=True)
    assert len(written) == 2
    with pytest.raises(pc.CompileError, match="drafts never go into"):
        pc.write_drafts(cards, approved, approved_dir=approved)


# ---------------------------------------------------------------- the script


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("compile_playbooks", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_script(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script, root, out = load_script(), vault(tmp_path), tmp_path / "drafts"
    assert script.main(["--vault", str(root), "--note", "nr7_note=nr7_breakout", "--out", str(out)]) == 0
    assert "2 drafts written" in capsys.readouterr().out
    assert sorted(p.name for p in out.iterdir()) == ["grid_ea.yaml", "nr7_breakout.yaml"]
    assert script.main(["--vault", str(root), "--out", str(out)]) == 0
    assert "0 drafts written" in capsys.readouterr().out
    assert script.main(["--vault", str(root), "--note", "nope", "--out", str(out)]) == 2
    assert "no note nope.md" in capsys.readouterr().err
    assert script.main(["--vault", str(tmp_path), "--out", str(out)]) == 2  # not a vault
    with pytest.raises(SystemExit):
        script.main([])  # neither --vault nor --check


def test_script_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = load_script()
    assert script.main(["--check"]) == 0  # the committed cards
    good = Playbook.model_validate(
        {"id": "a", "setup_tag": "abc", "summary": "A long enough summary.", "long_rules": ["x"],
         "short_rules": "none", "stop": "a stop", "target": "a target"}
    )  # fmt: skip
    (tmp_path / "a.yaml").write_text(good.to_yaml(), encoding="utf-8")
    (tmp_path / "b.yaml").write_text(yaml.safe_dump({"id": "b"}), encoding="utf-8")
    assert script.main(["--check", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "INVALID b.yaml" in captured.err
    assert "2 cards checked" in captured.out
