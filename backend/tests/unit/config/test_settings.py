from __future__ import annotations

import pytest
from pydantic import ValidationError

from aifund.config.settings import BrokerKind, Settings

ENV_KEYS = [
    "BROKER",
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_SERVER",
    "MT5_PATH",
    "DEEPSEEK_API_KEY",
    "DATABASE_URL",
    "CONFIG_PATH",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def make(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg, arg-type]


def test_defaults_to_sim_without_secrets() -> None:
    s = make()
    assert s.BROKER is BrokerKind.SIM
    assert s.MT5_LOGIN is None


def test_mt5_requires_explicit_account() -> None:
    with pytest.raises(ValidationError, match="BROKER=mt5 requires MT5_LOGIN, MT5_PASSWORD, MT5_SERVER"):
        make(BROKER="mt5")


def test_mt5_with_explicit_account_is_accepted() -> None:
    s = make(BROKER="mt5", MT5_LOGIN="12345678", MT5_PASSWORD="pw", MT5_SERVER="Broker-Demo")
    assert s.MT5_LOGIN == 12345678


def test_empty_env_values_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MT5_LOGIN", "")
    monkeypatch.setenv("BROKER", "sim")
    assert Settings(_env_file=None).MT5_LOGIN is None  # type: ignore[call-arg]


def test_secrets_are_masked_in_repr_and_str() -> None:
    s = make(DEEPSEEK_API_KEY="sk-very-secret", MT5_PASSWORD="hunter2")
    assert "sk-very-secret" not in repr(s)
    assert "hunter2" not in str(s)
    assert s.DEEPSEEK_API_KEY is not None
    assert s.DEEPSEEK_API_KEY.get_secret_value() == "sk-very-secret"
