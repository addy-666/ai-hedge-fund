"""Prompt system (roadmap 4.3): versioned Jinja templates rendered into LLM messages.

Each template (``prompts/<name>_v<version>.j2``) has a ``system`` block (the stable prefix: role, output
schema, playbook cards, lessons) and a ``user`` block (the per-bar data). Released templates are immutable:
``prompts/released.json`` pins the SHA-256 of each one and a test fails if a released file changes.

The helpers here turn engine objects into compact prompt text: a feature table per timeframe, candles
normalised by ATR (relative to the last close, so the model reads shape, not price level), candidates,
position and portfolio lines. Numbers are formatted deterministically so golden files are stable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import structlog
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from aifund.config.playbooks import load_card
from aifund.domain.decision import FeatureSnapshot, FeatureValue, SetupCandidate
from aifund.domain.market import Bar, Position
from aifund.ports.llm import LLMMessage

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
RELEASED = PROMPTS_DIR / "released.json"
log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RenderedPrompt:
    template: str
    version: str
    messages: list[LLMMessage]

    @property
    def token_estimate(self) -> int:
        return sum(len(m.content) for m in self.messages) // 4  # ~4 characters per token


def template_sha256(path: Path) -> str:
    """Hash of the template text with LF line endings, so a CRLF checkout (Windows) hashes the same."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def released_templates() -> dict[str, str]:
    data: dict[str, str] = json.loads(RELEASED.read_text())
    return data


class PromptLibrary:
    def __init__(self, directory: Path = PROMPTS_DIR) -> None:
        self._env = Environment(
            loader=FileSystemLoader(directory),
            undefined=StrictUndefined,  # a missing variable is an error, never an empty string
            autoescape=False,  # noqa: S701 - plain-text prompts, not HTML
            keep_trailing_newline=False,
            trim_blocks=False,
        )

    def render(self, name: str, version: int, context: Mapping[str, Any]) -> RenderedPrompt:
        template = self._env.get_template(f"{name}_v{version}.j2")
        ctx = template.new_context(dict(context))
        system = "".join(template.blocks["system"](ctx)).strip()
        user = "".join(template.blocks["user"](ctx)).strip()
        rendered = RenderedPrompt(
            name,
            str(version),
            [LLMMessage(role="system", content=system), LLMMessage(role="user", content=user)],
        )
        log.info("prompt.rendered", template=name, version=version, token_estimate=rendered.token_estimate)
        return rendered


# ---------------------------------------------------------------------------------------------- formatting


def fmt(value: FeatureValue | Decimal | float | None) -> str:
    """Deterministic, compact number formatting (6 significant digits, no exponent for normal ranges)."""
    if value is None:
        return "na"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float | Decimal):
        text = format(float(value), ".6g")
        return text if "e" not in text else format(float(value), ".6f").rstrip("0").rstrip(".")
    return str(value)


def feature_table(snapshot: FeatureSnapshot) -> str:
    """One line per timeframe (and one for context): ``M15: atr14=9.67 rsi14=44.1 ...``, keys sorted."""
    groups: dict[str, list[str]] = {}
    for key in sorted(snapshot.features):
        prefix, _, name = key.partition(".")
        groups.setdefault(prefix, []).append(f"{name}={fmt(snapshot.features[key])}")
    order = sorted(groups, key=lambda p: (p in ("ctx", "prop"), p))  # timeframes first, context last
    return "\n".join(f"{p.upper()}: {' '.join(groups[p])}" for p in order)


def atr_candles(bars: Sequence[Bar], atr: Decimal, count: int = 20) -> list[dict[str, str]]:
    """The last ``count`` bars as (x - last close) / ATR, two decimals, plus the raw close."""
    recent = list(bars)[-count:]
    if not recent or atr <= 0:
        return []
    ref = recent[-1].close

    def unit(x: Decimal) -> str:
        return f"{(x - ref) / atr:+.2f}"

    return [
        dict(time=f"{b.time:%m-%d %H:%M}", o=unit(b.open), h=unit(b.high), l=unit(b.low), c=unit(b.close),
             close=fmt(b.close))
        for b in recent
    ]  # fmt: skip


def candidate_lines(candidates: Sequence[SetupCandidate]) -> list[dict[str, str]]:
    return [
        dict(
            setup_tag=c.setup_tag,
            direction=c.direction_hint.value,
            strength=f"{c.strength:.2f}",
            levels=", ".join(f"{k} {fmt(v)}" for k, v in sorted(c.key_levels.items())),
            notes=c.notes,
        )
        for c in candidates
    ]


def position_line(position: Position | None) -> str:
    if position is None:
        return "none"
    return (
        f"{position.side.value} {fmt(position.volume)} lots at {fmt(position.price_open)}, "
        f"SL {fmt(position.sl)}, TP {fmt(position.tp)}, floating {fmt(position.profit)}"
    )


# ---------------------------------------------------------------------------------------------- playbooks


@dataclass(frozen=True)
class PlaybookCard:
    setup_tag: str
    summary: str
    long_rules: tuple[str, ...]
    short_rules: str
    stop: str
    target: str


@cache
def load_playbook(directory: Path, playbook_id: str) -> PlaybookCard:
    """A playbook card (config/playbooks/<id>.yaml, schema-checked): the prompt's description of a setup."""
    card = load_card(directory, playbook_id)
    short = card.short_rules if isinstance(card.short_rules, str) else "; ".join(card.short_rules)
    return PlaybookCard(
        setup_tag=card.setup_tag,
        summary=" ".join(card.summary.split()),
        long_rules=tuple(card.long_rules),
        short_rules=short,
        stop=card.stop,
        target=card.target,
    )
