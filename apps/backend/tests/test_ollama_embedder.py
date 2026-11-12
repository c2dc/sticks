"""Offline tests for OllamaEmbedder (Req. 4.2, 7.2)."""

from __future__ import annotations

import httpx
import pytest

from app.services.ai.embedder import (
    EMBEDDER_UNAVAILABLE,
    EMBEDDING_DIM_MISMATCH,
    EmbedderError,
    OllamaEmbedder,
)
from app.services.ai.memory import AFFINITY_KINDS, Embedder


def _embedder(handler, dim: int = 768) -> OllamaEmbedder:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OllamaEmbedder(
        base_url="http://ollama.test/", model="m", embedding_dim=dim, client=client
    )


def test_embed_posts_to_api_embed_and_returns_vector():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"embeddings": [[0.5] * 768]})

    embedder = _embedder(handler)
    assert isinstance(embedder, Embedder)
    assert embedder.embed("hello") == [0.5] * 768
    assert seen["url"] == "http://ollama.test/api/embed"
    assert b'"model":"m"' in seen["body"].replace(b" ", b"")


def test_dimension_mismatch_raises_clear_error():
    embedder = _embedder(lambda r: httpx.Response(200, json={"embeddings": [[0.1] * 10]}))
    with pytest.raises(EmbedderError) as info:
        embedder.embed("x")
    assert info.value.code == EMBEDDING_DIM_MISMATCH


def test_unreachable_and_http_error_map_to_embedder_unavailable():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    for handler in (refuse, lambda r: httpx.Response(500)):
        with pytest.raises(EmbedderError) as info:
            _embedder(handler).embed("x")
        assert info.value.code == EMBEDDER_UNAVAILABLE


def test_attack_flow_is_an_affinity_kind():
    assert "attack_flow" in AFFINITY_KINDS
