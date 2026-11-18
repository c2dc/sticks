"""Language-model profiles: the work endpoint and local Ollama models.

A profile identifies where the agents send their chat requests. The default is
the work profile built from the ``SEPINF_*`` settings. A local profile points at
a model served by Ollama through its OpenAI-compatible ``/v1`` endpoint.

Secrets note: a profile never stores a key value, only the name of the
environment variable that holds it. :func:`read_api_key` is the single place
that resolves the value, and it never logs or returns it through an API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dotenv import dotenv_values

from app.services.ai.config import get_sepinf_settings
from app.services.ai.embedder import OllamaEmbedderSettings
from app.services.ai.hardware import (
    MemoryFit,
    classify_memory_fit,
    read_gpu_memory,
    read_ram_available_bytes,
)
from app.services.ai.ollama_client import list_loaded_models, list_ollama_models

__all__ = [
    "OLLAMA_PROFILE_PREFIX",
    "WORK_PROFILE_ID",
    "LlmProfile",
    "ProfileEntry",
    "ProfileKind",
    "ProfileListing",
    "build_ollama_profile",
    "build_work_profile",
    "find_profile",
    "list_profiles",
    "read_api_key",
]

WORK_PROFILE_ID = "work"
OLLAMA_PROFILE_PREFIX = "ollama:"

# Ollama ignores the key, but the OpenAI client requires a non-empty one.
_OLLAMA_PLACEHOLDER_KEY = "ollama"


class ProfileKind(StrEnum):
    """Kind of endpoint a profile points at."""

    OPENAI_COMPATIBLE = "openai_compatible"
    OLLAMA_LOCAL = "ollama_local"


@dataclass(frozen=True)
class LlmProfile:
    """A selectable model profile (never holds a key value)."""

    id: str
    kind: ProfileKind
    base_url: str
    model_id: str
    api_key_env: str | None

    def __repr__(self) -> str:
        """Represent the profile without any key value."""
        return (
            f"LlmProfile(id={self.id!r}, kind={self.kind.value!r}, "
            f"base_url={self.base_url!r}, model_id={self.model_id!r}, "
            f"api_key_env={self.api_key_env!r})"
        )

    __str__ = __repr__


def build_work_profile() -> LlmProfile:
    """Build the work profile from the ``SEPINF_*`` settings."""
    settings = get_sepinf_settings()
    return LlmProfile(
        id=WORK_PROFILE_ID,
        kind=ProfileKind.OPENAI_COMPATIBLE,
        base_url=settings.base_url,
        model_id=settings.model_id,
        api_key_env="SEPINF_API_KEY",
    )


def build_ollama_profile(name: str) -> LlmProfile:
    """Build the local profile for the Ollama model ``name``."""
    base_url = OllamaEmbedderSettings().base_url.rstrip("/")
    return LlmProfile(
        id=f"{OLLAMA_PROFILE_PREFIX}{name}",
        kind=ProfileKind.OLLAMA_LOCAL,
        base_url=f"{base_url}/v1",
        model_id=name,
        api_key_env=None,
    )


def find_profile(profile_id: str | None) -> LlmProfile | None:
    """Return the profile for ``profile_id`` or ``None`` when it is not valid.

    ``None`` and ``"work"`` resolve to the work profile; ``ollama:<name>``
    resolves to a local profile. Whether the local model is installed is checked
    by the preflight, not here.
    """
    if profile_id is None or profile_id == WORK_PROFILE_ID:
        return build_work_profile()
    if profile_id.startswith(OLLAMA_PROFILE_PREFIX):
        name = profile_id[len(OLLAMA_PROFILE_PREFIX) :]
        if name:
            return build_ollama_profile(name)
    return None


def _root_env_path() -> Path:
    """Return the repository-root ``.env`` path."""
    # parents: [ai] [services] [app] [backend] [apps] [repo-root]
    return Path(__file__).resolve().parents[5] / ".env"


def read_api_key(profile: LlmProfile) -> str:
    """Resolve the key for ``profile`` from the environment, then the root ``.env``.

    Returns an empty string when the variable is not set anywhere. The value must
    never be logged or returned by an API.
    """
    if profile.api_key_env is None:
        return _OLLAMA_PLACEHOLDER_KEY
    value = os.environ.get(profile.api_key_env)
    if value:
        return value
    return dotenv_values(_root_env_path()).get(profile.api_key_env) or ""


LOCAL_DISCOVERY_OK = "ok"
LOCAL_DISCOVERY_OLLAMA_DOWN = "ollama_down"


@dataclass(frozen=True)
class ProfileEntry:
    """A listed profile with its size and memory fit (local profiles only)."""

    profile: LlmProfile
    size_bytes: int | None = None
    memory_fit: MemoryFit | None = None


@dataclass(frozen=True)
class ProfileListing:
    """Result of :func:`list_profiles`."""

    profiles: list[ProfileEntry]
    local_discovery: str


def list_profiles() -> ProfileListing:
    """List the work profile and one local profile per installed chat model.

    Cloud models (with ``remote_host``) and models whose capabilities lack
    ``completion`` are skipped. A model without a capabilities list is kept.
    When the Ollama daemon does not answer, only the work profile is listed and
    ``local_discovery`` is ``ollama_down``.
    """
    entries = [ProfileEntry(profile=build_work_profile())]
    installed = list_ollama_models()
    if installed is None:
        return ProfileListing(profiles=entries, local_discovery=LOCAL_DISCOVERY_OLLAMA_DOWN)

    chat_models = [
        model
        for model in installed
        if model.remote_host is None
        and (model.capabilities is None or "completion" in model.capabilities)
    ]
    if chat_models:
        gpu = read_gpu_memory()
        gpu_free = gpu.free_bytes if gpu is not None else None
        ram_free = read_ram_available_bytes()
        loaded = list_loaded_models() or []
        in_vram = {m.name for m in loaded if m.size_bytes > 0 and m.size_vram_bytes >= m.size_bytes}
        for model in chat_models:
            fit = classify_memory_fit(
                model.size_bytes, gpu_free, ram_free, loaded_in_vram=model.name in in_vram
            )
            entries.append(
                ProfileEntry(
                    profile=build_ollama_profile(model.name),
                    size_bytes=model.size_bytes or None,
                    memory_fit=fit,
                )
            )
    return ProfileListing(profiles=entries, local_discovery=LOCAL_DISCOVERY_OK)
