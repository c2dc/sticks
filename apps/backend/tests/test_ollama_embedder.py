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


def _timeout_of(**kwargs) -> float:
    return OllamaEmbedder(base_url="http://ollama.test", **kwargs)._timeout


def test_default_timeout_is_120_seconds(monkeypatch):
    monkeypatch.delenv("OLLAMA_EMBED_TIMEOUT", raising=False)
    assert _timeout_of() == 120.0


def test_timeout_is_overridable_by_env_and_constructor(monkeypatch):
    monkeypatch.setenv("OLLAMA_EMBED_TIMEOUT", "45.5")
    assert _timeout_of() == 45.5
    assert _timeout_of(timeout=7) == 7.0


@pytest.mark.parametrize("raw", ["abc", "", "0", "-5", "nan", "inf"])
def test_invalid_env_timeout_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv("OLLAMA_EMBED_TIMEOUT", raw)
    assert _timeout_of() == 120.0


@pytest.mark.parametrize("value", [0, -1, float("nan")])
def test_invalid_constructor_timeout_falls_back_to_default(monkeypatch, value):
    monkeypatch.delenv("OLLAMA_EMBED_TIMEOUT", raising=False)
    assert _timeout_of(timeout=value) == 120.0


def test_timeout_is_passed_to_the_http_call(monkeypatch):
    monkeypatch.setenv("OLLAMA_EMBED_TIMEOUT", "33")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json={"embeddings": [[0.5] * 768]})

    _embedder(handler).embed("x")
    assert seen["timeout"]["read"] == 33.0


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.ConnectTimeout])
def test_timeout_exceeded_maps_to_embedder_unavailable(exc):
    def slow(request: httpx.Request) -> httpx.Response:
        raise exc("timed out")

    with pytest.raises(EmbedderError) as info:
        _embedder(slow).embed("x")
    assert info.value.code == EMBEDDER_UNAVAILABLE
