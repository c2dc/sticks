"""Availability-check tests for the CalderaClient (task 6.1).

These focused unit tests verify the scaffolding + 10s availability probe with an
injected :class:`httpx.MockTransport`, so **no real Caldera** is contacted:

1. A timely 200 response => ``available`` result.
2. A simulated timeout (``httpx.TimeoutException``) => ``unavailable`` result and
   :meth:`ensure_available` raises the typed :class:`CalderaUnavailable`.
3. A non-success status (503) => ``unavailable``.
4. The configured request timeout is the Req. 4.6 mandated **10 seconds**, and
   the client carries the reused base URL + ``KEY`` header.

The broader contract/integration suite (endpoints, headers, payloads) is task
6.3; this file stays minimal and only proves the 6.1 behaviour.

_Requisitos: 4.6_
"""

from __future__ import annotations

import httpx
import pytest

from app.services.caldera import (
    DEFAULT_TIMEOUT_SECONDS,
    CalderaClient,
    CalderaUnavailable,
)


def _client_with_handler(handler) -> CalderaClient:
    """Build a CalderaClient whose httpx client is backed by a MockTransport."""
    transport = httpx.MockTransport(handler)
    return CalderaClient(transport=transport)


def test_timely_200_response_is_available() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # The probe hits the Caldera v2 health path with the KEY header.
        assert request.url.path == "/api/v2/health"
        assert request.headers.get("KEY") == "ADMIN123"
        return httpx.Response(200, json={"status": "ok"})

    with _client_with_handler(handler) as client:
        result = client.check_availability()

    assert result.available is True
    assert result.status_code == 200
    assert result.timed_out is False
    assert result.url == "http://localhost:8888"


def test_timeout_signals_unavailable_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=request)

    with _client_with_handler(handler) as client:
        result = client.check_availability()

    assert result.available is False
    assert result.timed_out is True
    assert result.status_code is None


def test_ensure_available_raises_typed_error_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=request)

    with _client_with_handler(handler) as client:
        with pytest.raises(CalderaUnavailable) as excinfo:
            client.ensure_available()

    # Carries the URL and the 10s timeout for a precise 503 message.
    assert excinfo.value.url == "http://localhost:8888"
    assert excinfo.value.timeout == 10.0


def test_non_success_status_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with _client_with_handler(handler) as client:
        result = client.check_availability()

    assert result.available is False
    assert result.status_code == 503
    assert result.timed_out is False


def test_connection_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with _client_with_handler(handler) as client:
        result = client.check_availability()

    assert result.available is False
    assert result.timed_out is False
    assert result.status_code is None


def test_configured_timeout_is_ten_seconds() -> None:
    client = CalderaClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    assert DEFAULT_TIMEOUT_SECONDS == 10.0
    assert client.timeout_seconds == 10.0
    # The explicit httpx.Timeout applied to every request is 10s on all phases.
    timeout = client.timeout
    assert timeout.connect == 10.0
    assert timeout.read == 10.0
    assert timeout.write == 10.0
    assert timeout.pool == 10.0


def test_base_url_and_key_header_reused_from_config() -> None:
    client = CalderaClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    assert client.base_url == "http://localhost:8888"
    assert client.headers == {"KEY": "ADMIN123"}


def test_client_and_transport_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError):
        CalderaClient(
            client=httpx.Client(),
            transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        )
