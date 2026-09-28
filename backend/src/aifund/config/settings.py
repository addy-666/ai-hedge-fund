"""Process settings from environment / .env: secrets and host-specific paths only (docs/02 §4).

Everything that shapes trading behaviour lives in the YAML trading config instead, so it can be
versioned and audited.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


def find_project_root() -> Path:
    """Repo root (the directory holding AGENTS.md and backend/). Relative paths in settings resolve here,
    so commands behave the same whether run from the repo root or from backend/.

    Override with AIFUND_HOME for installs outside a checkout.
    """
    if home := os.environ.get("AIFUND_HOME"):
        return Path(home).resolve()
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "AGENTS.md").is_file() and (candidate / "backend").is_dir():
            return candidate
    return Path.cwd()


PROJECT_ROOT = find_project_root()


class BrokerKind(StrEnum):
    MT5 = "mt5"
    SIM = "sim"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
        env_ignore_empty=True,  # "MT5_LOGIN=" in .env means unset, not ""
    )

    BROKER: BrokerKind = BrokerKind.SIM
    CONFIG_PATH: Path = Path("config/trading.yaml")  # relative -> PROJECT_ROOT
    DATABASE_URL: str = "sqlite:///data/aifund.db"  # relative SQLite path -> PROJECT_ROOT

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

    @field_validator("CONFIG_PATH")
    @classmethod
    def _config_path_from_root(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value

    @field_validator("DATABASE_URL")
    @classmethod
    def _sqlite_path_from_root(cls, value: str) -> str:
        url = make_url(value)
        database = url.database
        if (
            url.get_backend_name() != "sqlite"
            or database in (None, "", ":memory:")
            or Path(database).is_absolute()
        ):
            return value
        return url.set(database=str(PROJECT_ROOT / database)).render_as_string(hide_password=False)

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
