"""Load and validate config/trading.yaml.

YAML floats are parsed as Decimal (no binary-float rounding in risk parameters) and duplicate keys are an
error (PyYAML silently keeps the last one otherwise).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from aifund.config.trading_config import TradingConfig


class ConfigError(Exception):
    """The trading configuration is unreadable or invalid. The message lists every problem found."""


class _DecimalSafeLoader(yaml.SafeLoader):
    pass


def _construct_decimal(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> Decimal:
    return Decimal(loader.construct_scalar(node))


def _construct_mapping_no_duplicates(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            raise ConfigError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
        seen.add(key)
    return loader.construct_mapping(node)


_DecimalSafeLoader.add_constructor("tag:yaml.org,2002:float", _construct_decimal)
_DecimalSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_no_duplicates
)


@dataclass(frozen=True)
class LoadedConfig:
    config: TradingConfig
    yaml_text: str
    sha256: str


def parse_trading_config(yaml_text: str, source: str = "<string>") -> LoadedConfig:
    try:
        raw = yaml.load(yaml_text, Loader=_DecimalSafeLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: top level must be a mapping")
    try:
        config = TradingConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{source}: invalid trading config:\n{format_validation_error(exc)}") from exc
    return LoadedConfig(
        config=config,
        yaml_text=yaml_text,
        sha256=hashlib.sha256(yaml_text.encode("utf-8")).hexdigest(),
    )


def load_trading_config(path: Path) -> LoadedConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read trading config {path}: {exc}") from exc
    return parse_trading_config(text, source=str(path))


def format_validation_error(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)
