"""Process settings from environment / .env: secrets and host-specific paths only (docs/02 §4).

Everything that shapes trading behaviour lives in the YAML trading config instead, so it can be
versioned and audited.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BrokerKind(StrEnum):
    MT5 = "mt5"
    SIM = "sim"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
        env_ignore_empty=True,  # "MT5_LOGIN=" in .env means unset, not ""
    )

    BROKER: BrokerKind = BrokerKind.SIM
    CONFIG_PATH: Path = Path("config/trading.yaml")
    DATABASE_URL: str = "sqlite:///data/aifund.db"

    # --- LLM
    DEEPSEEK_API_KEY: SecretStr | None = None

    # --- MetaTrader 5 (required when BROKER=mt5)
    MT5_LOGIN: int | None = Field(default=None, gt=0)
    MT5_PASSWORD: SecretStr | None = None
    MT5_SERVER: str | None = None
    MT5_PATH: Path | None = None

    # --- Alerts
    TELEGRAM_BOT_TOKEN: SecretStr | None = None
    TELEGRAM_CHAT_ID: str | None = None
    HEALTHCHECKS_URL: SecretStr | None = None

    # --- API
    API_SECRET_KEY: SecretStr | None = None
    ADMIN_PASSWORD_HASH: SecretStr | None = None

    @model_validator(mode="after")
    def _mt5_requires_explicit_account(self) -> Self:
        # Never fall back to "whatever account the terminal is logged into" (prototype audit Q87):
        # the engine must know exactly which account it may trade, and verifies it at startup.
        if self.BROKER is BrokerKind.MT5:
            missing = [
                name
                for name, value in (
                    ("MT5_LOGIN", self.MT5_LOGIN),
                    ("MT5_PASSWORD", self.MT5_PASSWORD),
                    ("MT5_SERVER", self.MT5_SERVER),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"BROKER=mt5 requires {', '.join(missing)}")
        return self
