"""Which setup detectors the engine runs (``strategy.detectors``; roadmap 5.6).

An id is either a built-in detector (``mtf_trend_pullback``) or a playbook card in
``config/playbooks/<id>.yaml`` that carries a DSL ``hypothesis`` and ``status: APPROVED`` — a research
draft (docs/09 §6) the operator approved by copying it there and changing its status. Anything else is a
configuration error at startup. Outside SIM every detector still needs E1 evidence (``config/evidence.py``),
checked by the pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import ValidationError

from aifund.config.loader import ConfigError
from aifund.config.trading_config import SymbolConfig, TradingConfig
from aifund.strategies.base import SetupDetector, TfRoles
from aifund.strategies.dsl_detector import DslDetector, EntryHypothesis
from aifund.strategies.mtf_trend_pullback import MtfTrendPullback

BUILT_IN: dict[str, Callable[[TfRoles], SetupDetector]] = {"mtf_trend_pullback": MtfTrendPullback}
Factory = Callable[[SymbolConfig, TfRoles], list[SetupDetector]]


def load_hypothesis(playbooks: Path, detector_id: str) -> EntryHypothesis:
    path = playbooks / f"{detector_id}.yaml"
    if not path.is_file():
        raise ConfigError(
            f"strategy.detectors: {detector_id!r} is neither built in nor a playbook in {playbooks}"
        )
    card = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "hypothesis" not in card:
        raise ConfigError(f"playbook {path.name} has no DSL hypothesis: it cannot run as a detector")
    if card.get("status") != "APPROVED":
        raise ConfigError(
            f"playbook {path.name} is {card.get('status', 'not approved')}: set status: APPROVED"
        )
    try:
        return EntryHypothesis.model_validate(card["hypothesis"])
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
