"""Read-only queries to the local Ollama daemon (installed and loaded models).

Every function returns ``None`` when the daemon does not answer, so callers can
tell "daemon down" apart from "no models installed" (an empty list).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.services.ai.embedder import OllamaEmbedderSettings

__all__ = [
    "LoadedModel",
    "OllamaModel",
    "list_loaded_models",
    "list_ollama_models",
    "ollama_base_url",
]


@dataclass(frozen=True)
class OllamaModel:
    """An installed Ollama model as reported by ``/api/tags``."""

    name: str
    size_bytes: int
    remote_host: str | None
    capabilities: tuple[str, ...] | None


@dataclass(frozen=True)
class LoadedModel:
    """A model currently loaded in memory, as reported by ``/api/ps``."""

    name: str
    size_bytes: int
    size_vram_bytes: int


def ollama_base_url() -> str:
    """Return the Ollama base URL (``OLLAMA_BASE_URL``) without a trailing slash."""
    return OllamaEmbedderSettings().base_url.rstrip("/")


def _get_json(path: str, timeout: float) -> dict[str, Any] | None:
    """GET ``path`` on the daemon and return the JSON object, or ``None`` on any failure."""
    try:
        response = httpx.get(f"{ollama_base_url()}{path}", timeout=timeout)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _as_int(value: Any) -> int:
    """Return ``value`` as a non-negative int, else 0."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(int(value), 0)


def list_ollama_models(timeout: float = 2.0) -> list[OllamaModel] | None:
    """Return the installed models, or ``None`` when the daemon does not answer."""
    body = _get_json("/api/tags", timeout)
    if body is None:
        return None
    models: list[OllamaModel] = []
    for item in body.get("models") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        raw_caps = item.get("capabilities")
        capabilities = tuple(str(cap) for cap in raw_caps) if isinstance(raw_caps, list) else None
        remote_host = item.get("remote_host")
        models.append(
            OllamaModel(
                name=item["name"],
                size_bytes=_as_int(item.get("size")),
                remote_host=remote_host if isinstance(remote_host, str) and remote_host else None,
                capabilities=capabilities,
            )
        )
    return models


def list_loaded_models(timeout: float = 2.0) -> list[LoadedModel] | None:
    """Return the models loaded in memory, or ``None`` when the daemon does not answer."""
    body = _get_json("/api/ps", timeout)
    if body is None:
        return None
    loaded: list[LoadedModel] = []
    for item in body.get("models") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        loaded.append(
            LoadedModel(
                name=item["name"],
                size_bytes=_as_int(item.get("size")),
                size_vram_bytes=_as_int(item.get("size_vram")),
            )
        )
    return loaded
