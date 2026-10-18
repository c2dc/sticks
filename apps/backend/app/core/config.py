"""Backend configuration.

Design goals for this task (1.1):

* Reuse the values from the existing ``sticks/config/config.py`` (notably
  ``CALDERA_URL`` and the Caldera API key used as the ``KEY`` header) *without
  duplicating* them — they are read from the sticks module at runtime.
* The project standardizes on **PostgreSQL only** (no SQLite). The default
  ``database_url`` points at the containerized Postgres published on
  ``localhost:5432`` (service ``sticks-postgres``), so native host dev works
  out of the box. Override with ``PIPELINE_UI_DATABASE_URL`` for other
  environments; the compose ``backend`` service sets it to ``postgres:5432``.
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


def _clamp(value: float, lower: float, upper: float) -> float:
    """Clamp ``value`` into the inclusive range ``[lower, upper]``."""
    return max(lower, min(value, upper))


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

    # PostgreSQL is the ONLY database the project uses. Native dev points at the
    # containerized Postgres published on localhost:5432 (service
    # sticks-postgres). Override with PIPELINE_UI_DATABASE_URL in other
    # environments; the compose `backend` service overrides the host to
    # `postgres:5432`.
    database_url: str = "postgresql+psycopg://pipeline_ui:pipeline_ui@localhost:5432/pipeline_ui"

    # Optional override of the Caldera URL for local/mocked testing. When unset
    # the value comes from the existing sticks config (single source of truth).
    caldera_url_override: Optional[str] = None

    # ---- Stage 6 operation collection settings (Req. 3.1) --------------------

    # Seconds between polls while collecting an Operation's state. The runner
    # clamps the effective interval to the inclusive range 1..10 where the value
    # is consumed (see ``operation_poll_interval_clamped``). Default: 2.0s.
    operation_poll_interval_seconds: float = 2.0

    # Total collection timeout (seconds) counted from the start of the Operation.
    # When no terminal state is observed within this budget the runner marks the
    # Operation ``INCOMPLETE_TIMEOUT`` and preserves partial results (Req. 3.2).
    # The default is configurable via PIPELINE_UI_OPERATION_COLLECTION_TIMEOUT_SECONDS.
    operation_collection_timeout_seconds: float = 600.0

    # Maximum interval (seconds) between periodic isolation re-checks performed by
    # the runner **during** execution (Req. 6.3): the containment gate is
    # re-verified at least this often while the Operation runs. The requirement
    # caps the interval at 30s, so the value is clamped to the inclusive range
    # 0..30 where it is consumed (see ``isolation_recheck_interval_clamped``).
    # Default: 30.0s (the requirement's maximum).
    isolation_recheck_interval_seconds: float = 30.0

    @property
    def operation_poll_interval_clamped(self) -> float:
        """Poll interval clamped to the inclusive range 1..10 seconds (Req. 3.1).

        The raw ``operation_poll_interval_seconds`` may be configured to any
        value; consumers (the ``OperationRunner`` collection loop) must use the
        clamped value so the effective interval between polls is always
        ``clamp(value, 1, 10)``.
        """
        return _clamp(self.operation_poll_interval_seconds, 1.0, 10.0)

    @property
    def isolation_recheck_interval_clamped(self) -> float:
        """Isolation re-check interval clamped to 0..30 seconds (Req. 6.3).

        The runner re-verifies container isolation during execution at least
        every ``isolation_recheck_interval`` seconds; the requirement caps the
        interval at 30s, so consumers use the clamped value to guarantee the
        effective re-check interval never exceeds 30 seconds.
        """
        return _clamp(self.isolation_recheck_interval_seconds, 0.0, 30.0)

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
