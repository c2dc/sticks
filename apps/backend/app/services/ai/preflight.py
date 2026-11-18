"""Preflight: verify that a language-model profile can be used before a run starts.

The checks and their codes follow the design table (follow-up 48). The result
carries only codes and numbers: no interface text, no response body and no API
key. The frontend translates the codes.

Results are cached in memory per profile for :data:`CACHE_TTL_SECONDS` using a
monotonic clock; ``force=True`` bypasses the cache and a profile entry can be
dropped with :func:`invalidate_preflight_cache`.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from app.services.ai.config import get_sepinf_settings
from app.services.ai.embedder import OllamaEmbedderSettings
from app.services.ai.hardware import (
    MEMORY_HEADROOM_FACTOR,
    MemoryFit,
    classify_memory_fit,
    read_gpu_memory,
    read_ram_available_bytes,
)
from app.services.ai.llm_profiles import (
    LlmProfile,
    ProfileKind,
    read_api_key,
)
from app.services.ai.ollama_client import ollama_base_url

__all__ = [
    "CACHE_TTL_SECONDS",
    "PreflightCheck",
    "PreflightResult",
    "invalidate_preflight_cache",
    "run_preflight",
]

CACHE_TTL_SECONDS = 30.0
_REACHABILITY_TIMEOUT_SECONDS = 5.0
_OLLAMA_QUERY_TIMEOUT_SECONDS = 2.0

CheckStatus = Literal["pass", "warn", "fail", "skipped"]
PreflightStatus = Literal["ok", "warning", "blocked"]


class PreflightCheck(BaseModel):
    """One check of a preflight run."""

    name: str
    code: str
    status: CheckStatus
    detail: dict[str, Any] | None = None


class PreflightResult(BaseModel):
    """Outcome of a preflight run for one profile."""

    profile_id: str
    model_id: str
    kind: str
    status: PreflightStatus
    checked_at: datetime
    checks: list[PreflightCheck]
    blocking_codes: list[str]
    warning_codes: list[str]
    hints: list[str]


_cache: dict[str, tuple[float, PreflightResult]] = {}
_cache_lock = threading.Lock()


def invalidate_preflight_cache(profile_id: str | None = None) -> None:
    """Drop the cached result of ``profile_id``, or every cached result when ``None``."""
    with _cache_lock:
        if profile_id is None:
            _cache.clear()
        else:
            _cache.pop(profile_id, None)


def _get_json(client: httpx.Client, url: str) -> dict[str, Any] | None:
    """GET ``url`` and return the JSON object, or ``None`` on any failure."""
    try:
        response = client.get(url, timeout=_OLLAMA_QUERY_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _tag_entries(body: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Return the model entries of an ``/api/tags`` body, or ``None`` without a body."""
    if body is None:
        return None
    return [m for m in body.get("models") or [] if isinstance(m, dict)]


def _int_or_zero(value: Any) -> int:
    """Return ``value`` as a non-negative int, else 0."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(int(value), 0)


def _embedder_present(entries: list[dict[str, Any]] | None) -> bool:
    """Return whether ``OLLAMA_EMBED_MODEL`` is installed (with or without ``:latest``)."""
    if entries is None:
        return False
    wanted = OllamaEmbedderSettings().model
    accepted = {wanted, f"{wanted}:latest"}
    if wanted.endswith(":latest"):
        accepted.add(wanted.removesuffix(":latest"))
    return any(entry.get("name") in accepted for entry in entries)


def _check_reachability(client: httpx.Client, profile: LlmProfile) -> PreflightCheck:
    """Check that the OpenAI-compatible server answers at all (any HTTP status counts)."""
    if not (profile.base_url and profile.model_id and read_api_key(profile)):
        return PreflightCheck(name="reachability", code="not_configured", status="fail")
    try:
        client.get(
            f"{profile.base_url.rstrip('/')}/models",
            timeout=httpx.Timeout(_REACHABILITY_TIMEOUT_SECONDS),
        )
    except httpx.HTTPError:
        return PreflightCheck(name="reachability", code="unreachable", status="fail")
    return PreflightCheck(name="reachability", code="reachable", status="pass")


def _check_response(client: httpx.Client, profile: LlmProfile) -> PreflightCheck:
    """Send a one-token chat request and require a 2xx answer."""
    timeout = get_sepinf_settings().llm_preflight_response_timeout_seconds
    key = read_api_key(profile)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    payload = {
        "model": profile.model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    try:
        response = client.post(
            f"{profile.base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
            timeout=httpx.Timeout(timeout),
        )
    except httpx.HTTPError:
        return PreflightCheck(name="response", code="no_response", status="fail")
    if response.is_success:
        return PreflightCheck(name="response", code="responds", status="pass")
    return PreflightCheck(name="response", code="no_response", status="fail")


def _check_memory(
    client: httpx.Client, profile: LlmProfile, entry: dict[str, Any] | None
) -> PreflightCheck:
    """Compare the local model size with the free GPU and RAM."""
    size_bytes = _int_or_zero(entry.get("size")) if entry is not None else 0
    gpu = read_gpu_memory()
    gpu_free = gpu.free_bytes if gpu is not None else None
    ram_free = read_ram_available_bytes()
    loaded_body = _get_json(client, f"{ollama_base_url()}/api/ps")
    loaded_in_vram = False
    for item in (loaded_body or {}).get("models") or []:
        if isinstance(item, dict) and item.get("name") == profile.model_id:
            size = _int_or_zero(item.get("size"))
            loaded_in_vram = size > 0 and _int_or_zero(item.get("size_vram")) >= size
    fit = classify_memory_fit(size_bytes, gpu_free, ram_free, loaded_in_vram=loaded_in_vram)
    detail: dict[str, Any] | None = None
    if size_bytes > 0:
        detail = {
            "model_size_bytes": size_bytes,
            "required_bytes": int(size_bytes * MEMORY_HEADROOM_FACTOR),
            "gpu_free_bytes": gpu_free,
            "ram_free_bytes": ram_free,
            "headroom_factor": MEMORY_HEADROOM_FACTOR,
        }
    status: CheckStatus
    if fit is MemoryFit.FITS_GPU:
        status = "pass"
    elif fit is MemoryFit.TOO_LARGE:
        status = "fail"
    else:
        status = "warn"
    return PreflightCheck(name="memory", code=fit.value, status=status, detail=detail)


def _skipped(name: str) -> PreflightCheck:
    return PreflightCheck(name=name, code="skipped", status="skipped")


def _run_checks(client: httpx.Client, profile: LlmProfile) -> list[PreflightCheck]:
    """Run every check for ``profile`` and return them in display order."""
    embedder_entries = _tag_entries(_get_json(client, f"{ollama_base_url()}/api/tags"))
    embedder = (
        PreflightCheck(name="embedder", code="embedder_available", status="pass")
        if _embedder_present(embedder_entries)
        else PreflightCheck(name="embedder", code="embedder_missing", status="warn")
    )

    if profile.kind is ProfileKind.OPENAI_COMPATIBLE:
        reachability = _check_reachability(client, profile)
        response = (
            _check_response(client, profile)
            if reachability.status == "pass"
            else _skipped("response")
        )
        memory = PreflightCheck(name="memory", code="not_applicable", status="skipped")
        return [reachability, response, memory, embedder]

    # Local profile: one /api/tags call answers both the daemon and the model checks.
    entries = embedder_entries
    if entries is None:
        return [
            PreflightCheck(name="daemon", code="daemon_down", status="fail"),
            _skipped("model"),
            _skipped("response"),
            _skipped("memory"),
            embedder,
        ]
    daemon = PreflightCheck(name="daemon", code="daemon_running", status="pass")
    entry = next((e for e in entries if e.get("name") == profile.model_id), None)
    if entry is None:
        return [
            daemon,
            PreflightCheck(name="model", code="model_missing", status="fail"),
            _skipped("response"),
            _skipped("memory"),
            embedder,
        ]
    model = PreflightCheck(name="model", code="model_present", status="pass")
    memory = _check_memory(client, profile, entry)
    response = _skipped("response") if memory.status == "fail" else _check_response(client, profile)
    return [daemon, model, response, memory, embedder]


def _aggregate(profile: LlmProfile, checks: list[PreflightCheck]) -> PreflightResult:
    """Build the result with the overall status, code lists and hints."""
    blocking = [c.code for c in checks if c.status == "fail"]
    warnings = [c.code for c in checks if c.status == "warn"]
    status: PreflightStatus = "blocked" if blocking else ("warning" if warnings else "ok")
    hints = (
        ["vpn"]
        if profile.kind is ProfileKind.OPENAI_COMPATIBLE and "unreachable" in blocking
        else []
    )
    return PreflightResult(
        profile_id=profile.id,
        model_id=profile.model_id,
        kind=profile.kind.value,
        status=status,
        checked_at=datetime.now(UTC),
        checks=checks,
        blocking_codes=blocking,
        warning_codes=warnings,
        hints=hints,
    )


def run_preflight(
    profile: LlmProfile, *, force: bool = False, client: httpx.Client | None = None
) -> PreflightResult:
    """Run (or reuse from the 30 s cache) the preflight for ``profile``."""
    if not force:
        with _cache_lock:
            cached = _cache.get(profile.id)
        if cached is not None and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1].model_copy(deep=True)

    owns_client = client is None
    http = client if client is not None else httpx.Client()
    try:
        result = _aggregate(profile, _run_checks(http, profile))
    finally:
        if owns_client:
            http.close()

    with _cache_lock:
        _cache[profile.id] = (time.monotonic(), result)
    return result.model_copy(deep=True)
