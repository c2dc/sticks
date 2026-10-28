"""SEPINF settings and configuration access (lazy).

Hosts :class:`SepinfSettings` (pydantic-settings) reading ``SEPINF_BASE_URL``,
``SEPINF_MODEL_ID``, ``SEPINF_API_KEY``, ``AI_MAX_ITERATIONS``,
``AI_EMBEDDING_DIM`` and ``AI_RETRIEVAL_K`` from the **root** ``.env`` (the
repository-level file, not the backend-local one), plus a cached
:func:`get_sepinf_settings` accessor (task 1.2).

No settings are instantiated at import time; configuration is resolved lazily on
first access so importing this module performs no I/O and opens no network
connection (Requirements 1.7, 7.4).

Secrets note: ``SEPINF_API_KEY`` is read from the environment only and is never
hardcoded (Requirement 7.2) and never logged or echoed (Requirement 7.3).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _root_env_file() -> str:
    """Resolve the repository-root ``.env`` path.

    The SEPINF variables live in the repository-root ``.env`` (without the
    ``PIPELINE_UI_`` prefix the backend ``Settings`` uses). This module may be
    imported with any current working directory, so the path is resolved
    relative to this file rather than the CWD:

        apps/backend/app/services/ai/config.py  ->  <repo-root>/.env

    Pydantic-settings silently ignores a missing ``env_file``, so real
    environment variables still take precedence and configuration stays lazy.
    """
    # parents: [ai] [services] [app] [backend] [apps] [repo-root]
    return str(Path(__file__).resolve().parents[5] / ".env")


class SepinfSettings(BaseSettings):
    """SEPINF LLM endpoint settings (OpenAI-compatible). Read from the root ``.env``.

    The three endpoint values are intentionally defaulted to empty strings so
    that an absent variable is indistinguishable from an empty one; the
    :func:`app.services.ai.llm.make_chat_model` factory performs the fail-fast,
    name-only validation at resolution time (Requirements 7.5, 7.6). ``api_key``
    is never logged (Requirement 7.3) and never hardcoded (Requirement 7.2).
    """

    model_config = SettingsConfigDict(
        env_file=_root_env_file(),
        extra="ignore",
    )

    base_url: str = Field(default="", alias="SEPINF_BASE_URL")
    model_id: str = Field(default="", alias="SEPINF_MODEL_ID")
    api_key: str = Field(default="", alias="SEPINF_API_KEY")  # never logged

    # Loop + memory knobs (overridable via env). ``max_iterations`` defaults to 3
    # per Requirement 3.6.
    max_iterations: int = Field(default=3, alias="AI_MAX_ITERATIONS")
    embedding_dim: int = Field(default=1024, alias="AI_EMBEDDING_DIM")
    retrieval_k: int = Field(default=5, alias="AI_RETRIEVAL_K")

    def __repr__(self) -> str:  # pragma: no cover - defensive, avoids key leak
        """Represent settings without ever exposing the API key value.

        Guards against the key appearing in logs or stack traces that render the
        settings object (Requirement 7.3).
        """
        return (
            f"SepinfSettings(base_url={self.base_url!r}, model_id={self.model_id!r}, "
            f"api_key='***', max_iterations={self.max_iterations}, "
            f"embedding_dim={self.embedding_dim}, retrieval_k={self.retrieval_k})"
        )

    __str__ = __repr__


@lru_cache(maxsize=1)
def get_sepinf_settings() -> SepinfSettings:
    """Return a cached :class:`SepinfSettings` instance.

    Resolved lazily on first call; importing this module instantiates nothing.
    """
    return SepinfSettings()
