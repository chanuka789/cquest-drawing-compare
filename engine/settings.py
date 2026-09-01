"""Application configuration.

Values come from the environment, or from a `.env` file in the project root
during development. In a packaged build there is no `.env`, so every field
must have a usable default.

Environment variables
---------------------
CQDC_DEV        1 = development (UI served by the Vite dev server)
                0 or unset = production (UI served from ui/dist)
CQDC_LOG_LEVEL  DEBUG | INFO | WARNING | ERROR
CQDC_APP_DATA   Overrides the app data directory. Used by the test suite so
                tests never write into the real %LOCALAPPDATA%.
ANTHROPIC_API_KEY  Optional. Not used before Phase 7.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from engine import __version__

LogLevel = Literal["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Runtime configuration for the engine and the desktop shell."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    app_name: str = "C-Quest Drawing Compare"
    version: str = __version__

    dev_mode: bool = Field(
        default=False,
        validation_alias=AliasChoices("CQDC_DEV", "dev_mode"),
        description="True when the UI is loaded from the Vite dev server.",
    )
    log_level: LogLevel = Field(
        default="INFO",
        validation_alias=AliasChoices("CQDC_LOG_LEVEL", "log_level"),
    )
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "anthropic_api_key"),
        description="Optional. Cloud AI stays disabled while this is empty.",
    )

    # The Vite dev server. Only trusted as a CORS origin while dev_mode is on.
    dev_server_url: str = "http://localhost:5173"

    @field_validator("anthropic_api_key", mode="after")
    @classmethod
    def _blank_key_is_no_key(cls, value: str | None) -> str | None:
        """An empty or whitespace-only key means offline, not a broken key."""
        if value is None or not value.strip():
            return None
        return value

    @property
    def ai_available(self) -> bool:
        """Cloud AI needs a key. Offline mode is the default and always works."""
        return bool(self.anthropic_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, read once and cached."""
    return Settings()
