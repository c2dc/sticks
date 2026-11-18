"""Lazy LangChain chat-model factory for the SEPINF OpenAI-compatible endpoint.

Hosts :func:`make_chat_model`, which wires a LangChain ``ChatOpenAI`` client
from the SEPINF settings (task 1.2). The client is constructed lazily on first
call and the ``langchain_openai`` import is performed inside the factory so that
importing this module opens no network connection and does not require the
LangChain dependency at import time (Requirement 7.4).

The ``get_sepinf_settings`` accessor is re-exported here for convenience; its
canonical home is :mod:`app.services.ai.config`.

Secrets note: the API key is read from settings, never hardcoded (Requirement
7.2). Validation failures identify the missing setting by name only and never
include any portion of the key value (Requirements 7.3, 7.5, 7.6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from app.services.ai.config import SepinfSettings, get_sepinf_settings
from app.services.ai.llm_profiles import (
    WORK_PROFILE_ID,
    LlmProfile,
    build_work_profile,
    read_api_key,
)
from app.services.ai.llm_selection import resolve_active_profile

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from langchain_openai import ChatOpenAI

__all__ = ["SepinfConfigError", "get_sepinf_settings", "make_chat_model"]


class SepinfConfigError(RuntimeError):
    """Configuration error for the SEPINF client.

    Raised when a required SEPINF setting is absent or empty at the time the
    client is first resolved. The message identifies the offending setting by
    its environment-variable name only and never contains any key material
    (Requirements 7.5, 7.6).
    """


def _require(settings: SepinfSettings) -> None:
    """Fail fast when a required SEPINF setting is absent or empty.

    Checks ``SEPINF_API_KEY``, ``SEPINF_BASE_URL`` and ``SEPINF_MODEL_ID``. The
    raised error names the missing setting only; the API key value is never read
    into the message, logged, or echoed (Requirements 7.3, 7.5, 7.6).
    """
    # Order chosen so the key check is explicit (Requirement 7.5) while the other
    # two are covered by Requirement 7.6; each message is name-only.
    if not settings.api_key:
        raise SepinfConfigError("SEPINF_API_KEY is not configured; set it in the environment.")
    if not settings.base_url:
        raise SepinfConfigError("SEPINF_BASE_URL is not configured; set it in the environment.")
    if not settings.model_id:
        raise SepinfConfigError("SEPINF_MODEL_ID is not configured; set it in the environment.")


def make_chat_model(*, temperature: float = 0.0, profile: LlmProfile | None = None) -> ChatOpenAI:
    """Lazy factory for the chat model (OpenAI-compatible endpoint).

    Resolved lazily: no network call happens until the first agent node actually
    invokes the returned model, and the ``langchain_openai`` import is deferred
    to this call so importing the module touches no network and does not require
    LangChain (Requirement 7.4).

    The client enforces explicit limits: ``LLM_CONNECT_TIMEOUT_SECONDS`` to
    connect (default 5 s), ``LLM_READ_TIMEOUT_SECONDS`` to wait for the response
    (default 180 s) and no automatic retry, so a failure surfaces once.

    The API key is never hardcoded (Requirement 7.2). If ``SEPINF_API_KEY``,
    ``SEPINF_BASE_URL`` or ``SEPINF_MODEL_ID`` is absent or empty, a
    :class:`SepinfConfigError` is raised that names only the missing setting and
    never exposes the key (Requirements 7.3, 7.5, 7.6).

    Args:
        temperature: Sampling temperature for the chat model. Defaults to 0.0
            for deterministic planning.
        profile: Model profile to use. ``None`` uses the active profile from
            :func:`resolve_active_profile` (frozen run profile, else the selection).

    Returns:
        A configured ``ChatOpenAI`` instance.
    """
    settings = get_sepinf_settings()
    if profile is None:
        profile = resolve_active_profile()
    if profile.id == WORK_PROFILE_ID:
        _require(settings)
        profile = build_work_profile()
        api_key = settings.api_key
    else:
        api_key = read_api_key(profile)

    # Imported lazily so module import performs no I/O and does not hard-require
    # the LangChain dependency (Requirement 7.4).
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        base_url=profile.base_url,
        api_key=api_key,
        model=profile.model_id,
        temperature=temperature,
        timeout=httpx.Timeout(
            connect=settings.llm_connect_timeout_seconds,
            read=settings.llm_read_timeout_seconds,
            write=10.0,
            pool=5.0,
        ),
        max_retries=0,
    )
