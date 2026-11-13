"""Production :class:`~app.services.ai.memory.Embedder` backed by Ollama.

``OllamaEmbedder`` calls ``POST {OLLAMA_BASE_URL}/api/embed`` with
``{"model": OLLAMA_EMBED_MODEL, "input": text}``, validates that the returned
vector has ``embedding_dim`` entries (768 for ``nomic-embed-text``) and raises a
clear :class:`EmbedderError` otherwise. The request timeout defaults to 120 s (a cold
model can take a while to load) and is configurable through
``OLLAMA_EMBED_TIMEOUT`` (seconds) or the ``timeout`` argument; a missing,
non-numeric or non-positive value falls back to the default. There is no retry
of its own, because :class:`~app.services.ai.memory.ExperienceMemory` already
retries storage three times.

Error codes (``EmbedderError.code``):

* ``embedder_unavailable``: Ollama is unreachable, timed out, answered with an
  HTTP error or returned an unusable body.
* ``embedding_dim_mismatch``: the vector width differs from ``embedding_dim``.

Settings come from the process environment, then the repository-root ``.env``.
Importing this module performs no I/O; the HTTP client is injectable so tests
need no network.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

EMBEDDER_UNAVAILABLE = "embedder_unavailable"
EMBEDDING_DIM_MISMATCH = "embedding_dim_mismatch"

_DEFAULT_TIMEOUT_SECONDS = 120.0


def _resolve_timeout(value: Any) -> float:
    """Return ``value`` as a positive finite float, else the 120 s default."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(seconds) or seconds <= 0:
        return _DEFAULT_TIMEOUT_SECONDS
    return seconds


class EmbedderError(RuntimeError):
    """Raised by :class:`OllamaEmbedder`; ``code`` is a stable machine cause."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class OllamaEmbedderSettings(BaseSettings):
    """Ollama embedding settings read from the root ``.env``."""

    model_config = SettingsConfigDict(
        # parents: [ai] [services] [app] [backend] [apps] [repo-root]
        env_file=str(Path(__file__).resolve().parents[5] / ".env"),
        extra="ignore",
        populate_by_name=True,
    )

    base_url: str = Field(default="http://127.0.0.1:11434", alias="OLLAMA_BASE_URL")
    model: str = Field(default="nomic-embed-text", alias="OLLAMA_EMBED_MODEL")
    embedding_dim: int = Field(default=768, alias="AI_EMBEDDING_DIM")
    # Kept as text so an invalid value never raises; see ``_resolve_timeout``.
    timeout: str = Field(default="", alias="OLLAMA_EMBED_TIMEOUT")


class OllamaEmbedder:
    """Embed text through Ollama's ``/api/embed`` endpoint."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        embedding_dim: int | None = None,
        client: httpx.Client | None = None,
        timeout: float | None = None,
    ) -> None:
        settings = OllamaEmbedderSettings()
        self._base_url = (base_url or settings.base_url).rstrip("/")
        self._model = model or settings.model
        self._embedding_dim = embedding_dim if embedding_dim is not None else settings.embedding_dim
        self._client = client
        self._timeout = _resolve_timeout(timeout if timeout is not None else settings.timeout)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per text using ONE ``/api/embed`` call (``input`` list)."""
        if not texts:
            return []
        body = self._post({"model": self._model, "input": list(texts)})
        embeddings = body.get("embeddings") if isinstance(body, dict) else None
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise EmbedderError(
                EMBEDDER_UNAVAILABLE,
                "Ollama response does not hold one vector per input.",
            )
        vectors = [self._extract_vector({"embeddings": [item]}) for item in embeddings]
        for vector in vectors:
            if len(vector) != self._embedding_dim:
                raise EmbedderError(
                    EMBEDDING_DIM_MISMATCH,
                    f"expected {self._embedding_dim} dimensions, got {len(vector)}.",
                )
        return vectors

    def embed(self, text: str) -> list[float]:
        """Return the embedding vector for ``text`` or raise :class:`EmbedderError`."""
        body = self._post({"model": self._model, "input": text})
        vector = self._extract_vector(body)
        if len(vector) != self._embedding_dim:
            raise EmbedderError(
                EMBEDDING_DIM_MISMATCH,
                f"expected {self._embedding_dim} dimensions, got {len(vector)}.",
            )
        return vector

    def _post(self, payload: dict[str, Any]) -> Any:
        """POST ``payload`` to ``/api/embed`` and return the decoded JSON body."""
        url = f"{self._base_url}/api/embed"
        try:
            if self._client is not None:
                response = self._client.post(url, json=payload, timeout=self._timeout)
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(url, json=payload)
            response.raise_for_status()
            body: Any = response.json()
        except httpx.HTTPError as exc:
            raise EmbedderError(
                EMBEDDER_UNAVAILABLE, f"Ollama request failed ({type(exc).__name__})."
            ) from exc
        except ValueError as exc:
            raise EmbedderError(
                EMBEDDER_UNAVAILABLE, "Ollama returned a non-JSON response."
            ) from exc
        return body

    @staticmethod
    def _extract_vector(body: Any) -> list[float]:
        embeddings = body.get("embeddings") if isinstance(body, dict) else None
        if (
            not isinstance(embeddings, list)
            or not embeddings
            or not isinstance(embeddings[0], list)
        ):
            raise EmbedderError(EMBEDDER_UNAVAILABLE, "Ollama response has no 'embeddings' vector.")
        try:
            return [float(value) for value in embeddings[0]]
        except (TypeError, ValueError) as exc:
            raise EmbedderError(
                EMBEDDER_UNAVAILABLE, "Ollama vector contains non-numeric values."
            ) from exc
