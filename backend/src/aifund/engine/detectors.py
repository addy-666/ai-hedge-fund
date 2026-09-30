"""Which setup detectors the engine runs (``strategy.detectors``; roadmap 5.6).

An id is either a built-in detector (``mtf_trend_pullback``, ``nr7_breakout``, ``failure_test_2b``,
``sr_fade_range``) or a playbook card in
``config/playbooks/<id>.yaml`` that carries a DSL ``hypothesis`` and ``status: APPROVED`` — a research
draft (docs/09 §6) the operator approved by copying it there and changing its status. Anything else is a
configuration error at startup. Outside SIM every detector still needs E1 evidence (``config/evidence.py``),
checked by the pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from aifund.config.loader import ConfigError
from aifund.config.playbooks import CardStatus, PlaybookError, parse_card
from aifund.config.trading_config import SymbolConfig, TradingConfig
from aifund.strategies.base import SetupDetector, TfRoles
from aifund.strategies.dsl_detector import DslDetector, EntryHypothesis
from aifund.strategies.failure_test_2b import FailureTest2B
from aifund.strategies.mtf_trend_pullback import MtfTrendPullback
from aifund.strategies.nr7_breakout import Nr7Breakout
from aifund.strategies.sr_fade_range import SrFadeRange

BUILT_IN: dict[str, Callable[[TfRoles], SetupDetector]] = {
    "mtf_trend_pullback": MtfTrendPullback,
    "nr7_breakout": Nr7Breakout,
    "failure_test_2b": FailureTest2B,
    "sr_fade_range": SrFadeRange,
}
Factory = Callable[[SymbolConfig, TfRoles], list[SetupDetector]]


def load_hypothesis(playbooks: Path, detector_id: str) -> EntryHypothesis:
    path = playbooks / f"{detector_id}.yaml"
    if not path.is_file():
        raise ConfigError(
            f"strategy.detectors: {detector_id!r} is neither built in nor a playbook in {playbooks}"
        )
    try:
        card = parse_card(path)
    except PlaybookError as exc:
        raise ConfigError(f"playbook {exc}") from exc
    if card.hypothesis is None:
        raise ConfigError(f"playbook {path.name} has no DSL hypothesis: it cannot run as a detector")
    if card.status is not CardStatus.APPROVED:
        raise ConfigError(f"playbook {path.name} is {card.status.value}: set status: APPROVED")
    try:
        return EntryHypothesis.model_validate(card.hypothesis)
    except ValidationError as exc:
        raise ConfigError(f"playbook {path.name}: invalid hypothesis: {exc.errors()[0]['msg']}") from exc


def detector_factory(cfg: TradingConfig, playbooks: Path) -> Factory:
    """Validate every configured id now (fail at startup, not on the first bar) and return the factory."""
    hypotheses = {d: load_hypothesis(playbooks, d) for d in cfg.strategy.detectors if d not in BUILT_IN}

    def build(_sym: SymbolConfig, roles: TfRoles) -> list[SetupDetector]:
        return [
            BUILT_IN[d](roles) if d in BUILT_IN else DslDetector(hypotheses[d], roles)
            for d in cfg.strategy.detectors
        ]

    return build
