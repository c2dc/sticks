"""Backend configuration.

Design goals for this task (1.1):

* Every value comes from environment variables (``PIPELINE_UI_*``) with sane
  defaults, including the Caldera URL and the API key used as the ``KEY`` header.
* The project standardizes on **PostgreSQL only** (no SQLite). The default
  ``database_url`` points at the containerized Postgres published on
  ``localhost:5432`` (service ``sticks-postgres``), so native host dev works
  out of the box. Override with ``PIPELINE_UI_DATABASE_URL`` for other
  environments; the compose ``backend`` service sets it to ``postgres:5432``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

def _clamp(value: float, lower: float, upper: float) -> float:
    """Clamp ``value`` into the inclusive range ``[lower, upper]``."""
    return max(lower, min(value, upper))


class Settings(BaseSettings):
    """Backend settings.

    All values are overridable through ``PIPELINE_UI_*`` environment variables.
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

    # Caldera C2 connection. Override with PIPELINE_UI_CALDERA_URL /
    # PIPELINE_UI_CALDERA_KEY (the compose lab publishes Caldera on 8888). The
    # key is sent as the ``KEY`` header of the Caldera v2 API (red API key).
    caldera_url: str = "http://localhost:8888"
    caldera_key: str = "ADMIN123"

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

    @property
    def caldera_headers(self) -> dict[str, str]:
        """Default headers for talking to the Caldera v2 API."""
        return {"KEY": self.caldera_key}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (used as a FastAPI dependency)."""
    return Settings()
