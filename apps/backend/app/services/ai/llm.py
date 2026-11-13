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

from app.services.ai.config import SepinfSettings, get_sepinf_settings

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from langchain_openai import ChatOpenAI

__all__ = ["get_sepinf_settings", "make_chat_model"]


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


def make_chat_model(*, temperature: float = 0.0) -> ChatOpenAI:
    """Lazy factory for the SEPINF chat model (OpenAI-compatible).

    Resolved lazily: no network call happens until the first agent node actually
    invokes the returned model, and the ``langchain_openai`` import is deferred
    to this call so importing the module touches no network and does not require
    LangChain (Requirement 7.4).

    The ``base_url``, ``model`` and ``api_key`` are read from settings and passed
    through unchanged (Requirement 7.1). The API key is never hardcoded
    (Requirement 7.2). If ``SEPINF_API_KEY``, ``SEPINF_BASE_URL`` or
    ``SEPINF_MODEL_ID`` is absent or empty, a :class:`SepinfConfigError` is
    raised that names only the missing setting and never exposes the key
    (Requirements 7.3, 7.5, 7.6).

    Args:
        temperature: Sampling temperature for the chat model. Defaults to 0.0
            for deterministic planning.

    Returns:
        A configured ``ChatOpenAI`` instance pointed at the SEPINF endpoint.
    """
    settings = get_sepinf_settings()
    _require(settings)

    # Imported lazily so module import performs no I/O and does not hard-require
    # the LangChain dependency (Requirement 7.4).
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model_id,
        temperature=temperature,
    )
