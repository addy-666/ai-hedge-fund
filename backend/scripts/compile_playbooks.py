"""Compile TRADING BRAIN strategy notes into DRAFT playbook cards (roadmap 8.1, docs/00 §4).

Runs on the Mac, which holds the vault:

    cd backend && uv run python scripts/compile_playbooks.py --vault "$HOME/Desktop/TRADING OS/TRADING BRAIN"
    cd backend && uv run python scripts/compile_playbooks.py --vault "<vault>" \\
        --note high_probability_nr7_volatility_breakout=nr7_breakout      # a note by name (-> card id)
    cd backend && uv run python scripts/compile_playbooks.py --check      # validate config/playbooks/*.yaml

Without ``--note`` it compiles every ``type: strategy`` note with ``automation_potential: high``; each
``--note NAME[=CARD_ID]`` adds one (the notes the detectors cite have no ``automation_potential`` field).
Drafts go to ``data/playbooks/drafts/`` (never over an existing draft without ``--force``, never into
``config/playbooks/``). Curate one, set ``status: APPROVED`` and move it to ``config/playbooks/``; ``--check``
then validates it (exit 1 on any invalid card). The vault is only read, and only ``wiki/``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aifund.config.playbooks import check_directory
from aifund.config.settings import PROJECT_ROOT
from aifund.vault.playbook_compiler import CompileError, compile_note, select, write_drafts

APPROVED = PROJECT_ROOT / "config" / "playbooks"
DRAFTS = PROJECT_ROOT / "data" / "playbooks" / "drafts"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vault", type=Path, help="the TRADING BRAIN vault (read only)")
    parser.add_argument("--note", action="append", default=[], help="NAME or NAME=CARD_ID (repeatable)")
    parser.add_argument("--out", type=Path, default=DRAFTS, help=f"draft directory (default {DRAFTS})")
    parser.add_argument("--force", action="store_true", help="overwrite existing drafts")
    parser.add_argument("--check", nargs="?", const=APPROVED, type=Path, metavar="DIR",
                        help=f"validate the cards in DIR (default {APPROVED}) and exit")  # fmt: skip
    args = parser.parse_args(argv)

    if args.check is not None:
        problems = check_directory(args.check)
        for problem in problems:
            print(f"INVALID {problem}", file=sys.stderr)
        print(
            f"{len(list(args.check.glob('*.yaml')))} cards checked in {args.check}, {len(problems)} invalid"
        )
        return 1 if problems else 0
    if args.vault is None:
        parser.error("--vault is required (or use --check)")
    vault = args.vault.expanduser().resolve()
    if not (vault / "SCHEMA.md").is_file() or not (vault / "wiki").is_dir():
        print(f"{vault} does not look like the TRADING BRAIN vault (no SCHEMA.md / wiki/)", file=sys.stderr)
        return 2
    try:
        cards = [compile_note(note, card_id) for note, card_id in select(vault, args.note)]
        written, skipped = write_drafts(cards, args.out, approved_dir=APPROVED, force=args.force)
    except CompileError as exc:
        print(f"compile_playbooks: {exc}", file=sys.stderr)
        return 2
    for card in cards:
        todo = len(card.curation_notes)
        print(f"{card.id:<45} from {card.source}  ({todo} curation notes)")
    print(f"{len(written)} drafts written to {args.out}, {len(skipped)} kept (exist; --force to overwrite)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
