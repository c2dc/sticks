"""Backend configuration.

Design goals for this task (1.1):

* Reuse the values from the existing ``sticks/config/config.py`` (notably
  ``CALDERA_URL`` and the Caldera API key used as the ``KEY`` header) *without
  duplicating* them — they are read from the sticks module at runtime.
* Be forward-compatible with the database configuration that arrives in task
  1.3: default to a local SQLite database in dev, overridable via environment
  variable so PostgreSQL can be selected later without code changes.
* Never modify anything under ``sticks/``.
"""

from __future__ import annotations

from functools import lru_cache
from types import ModuleType
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.sticks_config import load_sticks_config


def get_sticks_config() -> ModuleType:
    """Return the loaded existing sticks config module (cached)."""
    return load_sticks_config()


class Settings(BaseSettings):
    """Backend settings.

    Values that already live in the existing sticks pipeline are *not* redefined
    here. Instead they are exposed through computed properties that read from
    ``sticks/config/config.py`` so there is a single source of truth.
    """

    model_config = SettingsConfigDict(
        env_prefix="PIPELINE_UI_",
        env_file=".env",
        extra="ignore",
    )

    app_name: str = "Pipeline_UI Backend"
    environment: str = "development"

    # Forward-compatible DB config (full setup lands in task 1.3).
    # Dev default: local SQLite file. Override with PIPELINE_UI_DATABASE_URL to
    # point at PostgreSQL in other environments.
    database_url: str = "sqlite:///./pipeline_ui.db"

    # Optional override of the Caldera URL for local/mocked testing. When unset
    # the value comes from the existing sticks config (single source of truth).
    caldera_url_override: Optional[str] = None

    # ---- Reused-from-sticks values (no duplication) --------------------------

    @property
    def caldera_url(self) -> str:
        """Caldera base URL, reused from sticks config (``CALDERA_URL``)."""
        if self.caldera_url_override:
            return self.caldera_url_override
        return getattr(get_sticks_config(), "CALDERA_URL")

    @property
    def caldera_key(self) -> str:
        """Value for the Caldera ``KEY`` header, reused from sticks config.

        The existing pipeline authenticates against Caldera's v2 API using the
        red API key (``CALDERA_API_KEY_RED``) as the ``KEY`` header.
        """
        return getattr(get_sticks_config(), "CALDERA_API_KEY_RED")

    @property
    def caldera_headers(self) -> dict[str, str]:
        """Default headers for talking to the Caldera v2 API."""
        return {"KEY": self.caldera_key}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (used as a FastAPI dependency)."""
    return Settings()
