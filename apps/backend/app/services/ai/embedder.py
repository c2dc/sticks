"""Production :class:`~app.services.ai.memory.Embedder` backed by Ollama.

``OllamaEmbedder`` calls ``POST {OLLAMA_BASE_URL}/api/embed`` with
``{"model": OLLAMA_EMBED_MODEL, "input": text}``, validates that the returned
vector has ``embedding_dim`` entries (768 for ``nomic-embed-text``) and raises a
clear :class:`EmbedderError` otherwise. It has a short timeout and no retry of
its own, because :class:`~app.services.ai.memory.ExperienceMemory` already
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

from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

EMBEDDER_UNAVAILABLE = "embedder_unavailable"
EMBEDDING_DIM_MISMATCH = "embedding_dim_mismatch"

_DEFAULT_TIMEOUT_SECONDS = 15.0


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


class OllamaEmbedder:
    """Embed text through Ollama's ``/api/embed`` endpoint."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        embedding_dim: Optional[int] = None,
        client: Optional[httpx.Client] = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        settings = OllamaEmbedderSettings()
        self._base_url = (base_url or settings.base_url).rstrip("/")
        self._model = model or settings.model
        self._embedding_dim = (
            embedding_dim if embedding_dim is not None else settings.embedding_dim
        )
        self._client = client
        self._timeout = timeout

    def embed(self, text: str) -> list[float]:
        """Return the embedding vector for ``text`` or raise :class:`EmbedderError`."""
        url = f"{self._base_url}/api/embed"
        payload = {"model": self._model, "input": text}
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

        vector = self._extract_vector(body)
        if len(vector) != self._embedding_dim:
            raise EmbedderError(
                EMBEDDING_DIM_MISMATCH,
                f"expected {self._embedding_dim} dimensions, got {len(vector)}.",
            )
        return vector

    @staticmethod
    def _extract_vector(body: Any) -> list[float]:
        embeddings = body.get("embeddings") if isinstance(body, dict) else None
        if (
            not isinstance(embeddings, list)
            or not embeddings
            or not isinstance(embeddings[0], list)
        ):
            raise EmbedderError(
                EMBEDDER_UNAVAILABLE, "Ollama response has no 'embeddings' vector."
            )
        try:
            return [float(value) for value in embeddings[0]]
        except (TypeError, ValueError) as exc:
            raise EmbedderError(
                EMBEDDER_UNAVAILABLE, "Ollama vector contains non-numeric values."
            ) from exc
