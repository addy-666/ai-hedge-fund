"""Playbook compiler (roadmap 8.1, docs/00 §4): TRADING BRAIN strategy notes → DRAFT playbook cards.

Which notes: every ``wiki/strategies/*.md`` whose frontmatter says ``type: strategy`` and
``automation_potential: high``, plus any note named explicitly (``--note name`` or ``--note name=card_id``;
the notes our detectors cite carry no ``automation_potential`` field, so they are named). Notes are read from
``wiki/`` only — never ``raw/``.

What: a DRAFT card (``config/playbooks.py`` schema) with the note's summary, the numbered entry conditions of
its rules section (long side; the short side when the note has one), its stop and target lines, the first
target in R when the note states one, the claimed performance as UNVALIDATED book claims, and the source
wikilink. Extraction from free-form Markdown is a heuristic, so everything it could not find is a ``TODO``
placeholder plus a curation note. A draft is never written into ``config/playbooks/``: the operator curates it
(checks every rule against the note, maps timeframes onto the profile roles, removes the TODOs and curation
notes), sets ``status: APPROVED`` and copies it there. The engine never reads the vault.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from aifund.config.playbooks import TODO, CardStatus, Playbook

STRATEGIES = Path("wiki") / "strategies"
FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
NUMBERED = re.compile(r"^\s*(\d+)\.\s+(.*)$")
BOX = re.compile(r"[│┌┐└┘├┤┬┴┼─═║╔╗╚╝╠╣╦╩╬▲▼►◄╲╱╳←→↑↓]+")
R_MULTIPLE = re.compile(r"\+?(\d+(?:\.\d+)?)\s*R\b")
RR_RATIO = re.compile(r"\b(\d+(?:\.\d+)?)\s*:\s*1\b")
RULES_HEADING = re.compile(
    r"rule|definition|condition|setup|logic|trigger|entry|checklist|long|bull|spring|buy", re.IGNORECASE
)
SHORT_HEADING = re.compile(r"short|bear|upthrust|sell", re.IGNORECASE)
CODE_FENCE = re.compile(r"^\s*```\s*([A-Za-z0-9_+-]*)")
PROSE_FENCES = {"", "text", "txt"}  # the notes draw their rule tables in plain fences: read those as text
UNSAFE = re.compile(
    r"no stop[ -]loss|zero fixed stop|without (?:a )?stop|martingale|\bgrid\b|pyramid|hedg", re.IGNORECASE
)
ALWAYS = [
    "Check every rule against the note and map its timeframes onto the profile roles (trend filter / setup / "
    "trigger).",
    "A card only describes the setup: it trades only through a detector (strategies/) or an APPROVED DSL "
    "hypothesis, and outside SIM only with E1 evidence (docs/09 §7).",
]


class CompileError(Exception):
    """A named note that does not exist, or an output directory the compiler may not write to."""


@dataclass(frozen=True)
class Note:
    stem: str
    path: Path
    frontmatter: dict[str, Any]
    body: str

    @property
    def eligible(self) -> bool:
        fm = self.frontmatter
        return fm.get("type") == "strategy" and str(fm.get("automation_potential", "")).lower() == "high"


def read_note(path: Path) -> Note:
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    frontmatter: dict[str, Any] = {}
    if match:
        loaded = yaml.safe_load(match.group(1))
        frontmatter = loaded if isinstance(loaded, dict) else {}
    return Note(path.stem, path, frontmatter, text[match.end() :] if match else text)


def _find(vault: Path, name: str) -> Path:
    wiki = vault / "wiki"
    preferred = wiki / "strategies" / f"{name}.md"
    if preferred.is_file():
        return preferred
    found = sorted(wiki.rglob(f"{name}.md"))
    if not found:
        raise CompileError(f"no note {name}.md under {wiki}")
    return found[0]


def select(vault: Path, named: list[str]) -> list[tuple[Note, str]]:
    """(note, card id): the eligible strategy notes, then the named ones (``name`` or ``name=card_id``)."""
    picked: dict[str, tuple[Note, str]] = {}
    for path in sorted((vault / STRATEGIES).glob("*.md")):
        note = read_note(path)
        if note.eligible:
            picked[note.stem] = (note, note.stem)
    for item in named:
        name, _, card_id = item.partition("=")
        note = read_note(_find(vault, name.strip()))
        picked[note.stem] = (note, card_id.strip() or note.stem)
    return list(picked.values())


# ---------------------------------------------------------------------------------------------- extraction


def clean(text: str) -> str:
    """Markdown and box-drawing noise out: ``[[a|b]]`` → b, ``[[a]]`` → a, bold/italics/code marks dropped."""
    text = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)
    text = BOX.sub(" ", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"^[\s|>*•-]+", "", text)
    return " ".join(text.replace("|", " ").split())


def sections(body: str) -> list[tuple[str, list[str]]]:
    """(heading, lines) in order; text before the first heading has the heading ""."""
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in body.splitlines():
        heading = HEADING.match(line)
        if heading:
            out.append((clean(heading.group(2)), []))
        else:
            out[-1][1].append(line)
    return out


def summary_of(body: str) -> str | None:
    for heading, lines in sections(body):
        if heading.lower().startswith("related"):
            continue
        paragraph: list[str] = []
        for line in [*lines, ""]:
            if line.strip() and not line.lstrip().startswith(("```", "|", ">", "-", "*", "$", "!")):
                paragraph.append(line.strip())
            elif paragraph:
                text = clean(" ".join(paragraph))
                if len(text) >= 40:
                    return text[:800]
                paragraph = []
    return None


def _prose(lines: list[str]) -> list[str]:
    """The lines outside program code (python, mql5, pine...): plain and ``text`` fences count as prose."""
    out, fence = [], None
    for line in lines:
        match = CODE_FENCE.match(line)
        if match:
            fence = None if fence is not None else match.group(1).lower()
            continue
        if fence is None or fence in PROSE_FENCES:
            out.append(line)
    return out


def _numbered(lines: list[str]) -> list[str]:
    items = []
    for line in _prose(lines):
        match = NUMBERED.match(line)
        if match:
            text = clean(match.group(2))
            if text:
                items.append(text)
    return items


def rules_of(body: str) -> tuple[list[str], list[str]]:
    """(long-side conditions, short-side conditions): numbered items of the rules sections."""
    long: list[str] = []
    short: list[str] = []
    for heading, lines in sections(body):
        if not RULES_HEADING.search(heading) and not SHORT_HEADING.search(heading):
            continue
        items = _numbered(lines)
        if SHORT_HEADING.search(heading):
            short = short or items
        elif RULES_HEADING.search(heading):
            long = long or items
    return long, short


def line_about(body: str, *words: str) -> str | None:
    """The first list item, table row or sentence that mentions a word (the most specific word first)."""
    for word in words:
        found = _line_about(body, word)
        if found is not None:
            return found
    return None


def _line_about(body: str, word: str) -> str | None:
    for heading, lines in sections(body):
        if heading.lower().startswith("related"):
            continue
        for line in _prose(lines):
            if word.lower() not in line.lower() or re.fullmatch(r"\s*[-*]?\s*\[\[[^\]]*\]\]\s*", line):
                continue
            text = clean(NUMBERED.sub(r"\2", line))
            if len(text) >= 15:
                return text[:300]
    return None


def typical_r(text: str | None) -> Decimal | None:
    if not text:
        return None
    match = RR_RATIO.search(text) or R_MULTIPLE.search(text)
    return Decimal(match.group(1)) if match and Decimal(match.group(1)) > 0 else None


def compile_note(note: Note, card_id: str) -> Playbook:
    fm, notes = note.frontmatter, list(ALWAYS)
    summary = summary_of(note.body)
    long, short = rules_of(note.body)
    stop = line_about(note.body, "stop loss", "initial stop", "invalidat", "stop")
    target = line_about(note.body, "first target", "target 1", "target")
    if summary is None:
        notes.append("No summary paragraph found.")
    if not long:
        notes.append("No numbered entry conditions found in a rules/definition/setup section.")
    if not short:
        notes.append("No short-side section: confirm the short rules mirror the long ones.")
    if stop is None or target is None:
        notes.append("Stop or target line not found.")
    if UNSAFE.search(note.body):
        notes.append(
            "WARNING: the note describes grids, hedging, pyramiding or trading without a stop. The engine "
            "trades one position per signal with a server-side stop: adapt the idea or reject the card."
        )
    claimed = {k: str(fm[k]) for k in ("win_rate", "profit_factor") if fm.get(k) is not None}
    tag = re.sub(r"[^a-z0-9_]", "_", card_id.lower())[:40].strip("_")
    return Playbook(
        id=card_id,
        setup_tag=tag if len(tag) >= 3 else f"{tag}_setup",
        status=CardStatus.DRAFT,
        source=f"[[{note.stem}]]",
        summary=summary or f"{TODO}: summary of {fm.get('title', note.stem)}",
        long_rules=long or [f"{TODO}: entry conditions (long side)"],
        short_rules=short or f"{TODO}: short side (mirror image of the long rules?)",
        stop=stop or f"{TODO}: invalidation / stop",
        target=target or f"{TODO}: target",
        typical_r=typical_r(target),
        claimed_performance={**claimed, "status": "UNVALIDATED"} if claimed else None,
        validation_status=str(fm["validation_status"]) if fm.get("validation_status") else None,
        automation_potential=str(fm["automation_potential"]) if fm.get("automation_potential") else None,
        curation_notes=notes,
    )


def write_drafts(
    cards: list[Playbook], out: Path, *, approved_dir: Path, force: bool = False
) -> tuple[list[Path], list[Path]]:
    """Write each card to ``out/<id>.yaml``: (written, skipped because the file exists and not ``force``)."""
    if out.resolve() == approved_dir.resolve():
        raise CompileError(f"drafts never go into {approved_dir}: the operator approves them into it")
    out.mkdir(parents=True, exist_ok=True)
    written, skipped = [], []
    for card in cards:
        path = out / f"{card.id}.yaml"
        if path.exists() and not force:
            skipped.append(path)
            continue
        header = (
            f"# DRAFT compiled from {card.source}: curate, set status: APPROVED, move to config/playbooks/\n"
        )
        path.write_text(header + card.to_yaml(), encoding="utf-8")
        written.append(path)
    return written, skipped
