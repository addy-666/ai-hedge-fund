from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from aifund.config.loader import ConfigError, load_trading_config, parse_trading_config
from aifund.config.trading_config import TradingConfig
from aifund.domain.enums import Mode, ReversalMode, Timeframe

EXAMPLE = Path(__file__).resolve().parents[4] / "config" / "trading.example.yaml"


@pytest.fixture(scope="module")
def base() -> dict[str, Any]:
    return yaml.safe_load(EXAMPLE.read_text())


def _mutated(base: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    raw = copy.deepcopy(base)
    node: Any = raw
    keys = path.split(".")
    for key in keys[:-1]:
        node = node[int(key)] if isinstance(node, list) else node[key]
    last = keys[-1]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value
    return raw


# ---------------------------------------------------------------- the example file


def test_example_config_loads_with_exact_decimals() -> None:
    loaded = load_trading_config(EXAMPLE)
    cfg = loaded.config
    assert cfg.engine.mode is Mode.SIM
    assert cfg.risk.risk_per_trade_pct == Decimal("0.5")
    assert cfg.llm.temperature == Decimal("0.1")  # not 0.1000000000000000055511151231257827
    assert cfg.risk.limits.max_notional_leverage == Decimal("10")
    assert cfg.risk.guards.reversal_mode is ReversalMode.CLOSE_ONLY
    assert cfg.profiles["intraday_m15"].trigger_tf is Timeframe.M15
    assert cfg.symbol("XAUUSD").broker == "XAUUSDm"
    assert len(loaded.sha256) == 64


def test_placeholder_models_are_allowed_in_sim(base: dict[str, Any]) -> None:
    TradingConfig.model_validate(base)


# ---------------------------------------------------------------- validators


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        ("engine.mode", "LIVE", "allow_live"),
        ("engine.mode", "DEMO", "placeholder"),
        ("engine.magic", 0, "greater than 0"),
        ("engine.trading_day_boundary_utc", "25:00", "HH:MM"),
        ("risk.stops.k_sl_min", 2.0, "k_sl_min <= k_sl_default <= k_sl_max"),
        ("risk.stops.k_sl_max", 1.2, "k_sl_min <= k_sl_default <= k_sl_max"),
        ("risk.stops.rr_min", 2.5, "rr_min <= rr_default <= rr_max"),
        ("risk.stops.rr_min", 0.5, "rr_min must be >= 1"),
        ("risk.risk_per_trade_pct", 1.5, "risk_per_trade_pct must be <= max_risk_per_trade_pct"),
        ("risk.risk_per_trade_pct", 0, "greater than 0"),
        ("risk.risk_per_trade_pct", 150, "less than or equal to 100"),
        ("risk.max_risk_per_trade_pct", 4.0, "max_risk_per_trade_pct must be <= limits.daily_loss_limit_pct"),
        ("risk.confidence_threshold", 101, "less than or equal to 100"),
        ("risk.confidence_risk_scaling.at_threshold", 1.0, None),  # equal is fine
        ("risk.confidence_risk_scaling.at_90", 0.4, "at_threshold must be <= at_90"),
        ("risk.limits.daily_loss_limit_pct", 7.0, "daily_loss_limit_pct <= weekly_loss_limit_pct"),
        ("risk.limits.weekly_loss_limit_pct", 12.0, "weekly_loss_limit_pct <= max_drawdown_pct"),
        ("risk.limits.max_bucket_heat_pct", 4.0, "max_bucket_heat_pct must be <= max_portfolio_heat_pct"),
        ("risk.limits.max_notional_leverage", 0, "greater than 0"),
        ("risk.max_margin_utilisation", 1.5, "less than or equal to 1"),
        ("risk.min_lot_overshoot_pct", -1, "greater than or equal to 0"),
        ("risk.commission_per_lot_roundtrip", -3, None),
        ("risk.guards.reversal_mode", "flip", "ignore"),
        ("symbols.0.profile", "does_not_exist", "undefined profiles"),
        ("symbols.1.canonical", "XAUUSD", "duplicate canonical"),
        ("symbols.1.broker", "XAUUSDm", "duplicate broker"),
        ("symbols.0.max_spread_to_atr", 0, "greater than 0"),
        ("symbols.0.asset_class", "stocks", None),
        ("profiles.intraday_m15.setup_tf", "M5", "setup_tf must be a higher timeframe"),
        ("profiles.intraday_m15.context_tfs", ["H1", "D1"], "must be higher than setup_tf"),
        ("profiles.intraday_m15.context_tfs", ["D1", "D1"], "duplicates"),
        ("profiles.intraday_m15.bars_per_tf", 299, "greater than or equal to 300"),
        ("strategy.analyst_enabled", False, "enable analyst_enabled and/or baseline_enabled"),
        ("llm.base_url", "http://api.deepseek.com", "should match pattern"),
        ("learning.min_matches_holdout", 50, "min_matches_holdout must be <= min_matches_total"),
        ("learning.review_after_days", 200, "review_after_days must be <= expire_after_days"),
        ("position_management.flatten_friday_utc", "8pm", "HH:MM"),
    ],
)
def test_invalid_values_are_rejected(
    base: dict[str, Any], path: str, value: Any, expected: str | None
) -> None:
    raw = _mutated(base, path, value)
    if expected is None and path == "risk.confidence_risk_scaling.at_threshold":
        TradingConfig.model_validate(raw)  # boundary case: valid
        return
    with pytest.raises(ValidationError) as exc:
        TradingConfig.model_validate(raw)
    if expected is not None:
        assert expected in str(exc.value)


def test_positions_per_symbol_cannot_exceed_open_positions(base: dict[str, Any]) -> None:
    raw = _mutated(base, "risk.limits.max_open_positions", 2)
    raw["risk"]["limits"]["max_positions_per_symbol"] = 3
    with pytest.raises(ValidationError, match="max_positions_per_symbol must be <= max_open_positions"):
        TradingConfig.model_validate(raw)


def test_live_is_accepted_only_with_allow_live_and_real_models(base: dict[str, Any]) -> None:
    raw = _mutated(base, "engine.mode", "LIVE")
    raw["engine"]["allow_live"] = True
    raw["llm"]["analyst_model"] = raw["llm"]["auditor_model"] = "some-model-id"
    assert TradingConfig.model_validate(raw).engine.mode is Mode.LIVE


@pytest.mark.parametrize("section", ["", "engine", "risk", "risk.limits", "risk.stops", "llm"])
def test_unknown_keys_are_rejected_everywhere(base: dict[str, Any], section: str) -> None:
    raw = copy.deepcopy(base)
    node: Any = raw
    for key in filter(None, section.split(".")):
        node = node[key]
    node["risk_per_trade_pc"] = 0.5  # typo of an existing key
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TradingConfig.model_validate(raw)


def test_config_is_immutable() -> None:
    cfg = load_trading_config(EXAMPLE).config
    with pytest.raises(ValidationError):
        cfg.risk.risk_per_trade_pct = Decimal("50")  # type: ignore[misc]


# ---------------------------------------------------------------- loader


def test_duplicate_yaml_keys_fail_loudly() -> None:
    text = EXAMPLE.read_text().replace(
        "  risk_per_trade_pct: 0.5\n", "  risk_per_trade_pct: 0.5\n  risk_per_trade_pct: 5\n"
    )
    with pytest.raises(ConfigError, match="duplicate key 'risk_per_trade_pct'"):
        parse_trading_config(text)


def test_invalid_yaml_and_non_mapping_raise_config_error() -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        parse_trading_config("engine: [unclosed")
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        parse_trading_config("- a\n- b\n")


def test_error_message_lists_every_problem(base: dict[str, Any]) -> None:
    raw = _mutated(base, "risk.confidence_threshold", 500)
    raw["engine"]["magic"] = -1
    text = yaml.safe_dump(raw)
    with pytest.raises(ConfigError) as exc:
        parse_trading_config(text, source="t.yaml")
    message = str(exc.value)
    assert "t.yaml" in message
    assert "risk.confidence_threshold" in message
    assert "engine.magic" in message


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_trading_config(tmp_path / "nope.yaml")


@pytest.mark.parametrize("value", [".inf", "-.inf", ".nan", ".NaN"])
def test_non_finite_yaml_numbers_raise_config_error(value: str) -> None:
    text = EXAMPLE.read_text().replace("  bar_close_grace_s: 3\n", f"  bar_close_grace_s: {value}\n")
    with pytest.raises(ConfigError, match="non-finite number"):
        parse_trading_config(text)
