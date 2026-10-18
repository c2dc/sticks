"""Example tests for ``CalderaClient.list_agents`` (task 2.3).

These are **example** (not property) tests that pin the current pointing/routes
behaviour of :meth:`CalderaClient.list_agents` against a **mocked** Caldera via
:class:`httpx.MockTransport` — *no real Caldera and no real network* are used, in
line with the spec's "no real emulation during development" constraint. They
complement the property test in task 2.2 (agent-health release) and the broader
contract suite in ``test_caldera_integration.py``.

What is verified here (Req. 4.4 / 4.5):

* **Current route** — ``list_agents`` consumes ``GET /api/v2/agents`` with the
  ``KEY: ADMIN123`` header, i.e. the version/route of the running Caldera API
  (Req. 4.4), optionally filtering by the running environment's group name.
* **Pointing divergence maps to a typed error (Req. 4.5)** — when the configured
  route/host does not match the running environment, Caldera either answers with
  an **error status** (e.g. 404 for an unknown route) or the **host does not
  resolve**. Both are surfaced as the typed :class:`CalderaApiError` /
  :class:`CalderaUnavailable` the Stage 5 gate maps to a "pointing divergence"
  block (rather than leaking a raw ``httpx`` error).
* **Orphan detection** — a payload carrying ``alive=false`` is parsed into an
  :class:`AgentRef` with ``alive is False`` (and ``alive=true`` into
  ``alive is True``), so the Stage 5 health gate can detect orphan/dead agents.

_Requirements: 4.4, 4.5_
"""

from __future__ import annotations

import httpx
import pytest

from app.services.caldera import (
    AGENTS_PATH,
    AgentRef,
    CalderaApiError,
    CalderaClient,
    CalderaUnavailable,
)


class RouterHandler:
    """A MockTransport handler that routes by ``(method, path)``.

    Records every request it sees (so tests can assert on the route + headers)
    and dispatches to a registered responder, returning its
    :class:`httpx.Response`. Unregistered routes raise, surfacing an unexpected
    call clearly. Mirrors the helper used by ``test_caldera_integration.py``.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: dict[tuple[str, str], object] = {}

    def route(self, method: str, path: str, responder) -> "RouterHandler":
        self._routes[(method.upper(), path)] = responder
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method.upper(), request.url.path)
        responder = self._routes.get(key)
        if responder is None:
            raise AssertionError(
                f"unexpected request: {request.method} {request.url.path}"
            )
        if callable(responder):
            return responder(request)
        return responder


def _client(handler) -> CalderaClient:
    """Build a CalderaClient backed by a MockTransport around ``handler``."""
    return CalderaClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Current route / contract — GET /api/v2/agents (Req. 4.4)
# ---------------------------------------------------------------------------


def test_list_agents_uses_current_v2_route_with_key_header() -> None:
    agents = [
        {"paw": "paw-1", "group": "red", "alive": True, "host": "kali"},
        {"paw": "paw-2", "group": "red", "alive": True, "host": "kali"},
    ]
    handler = RouterHandler().route(
        "GET", AGENTS_PATH, httpx.Response(200, json=agents)
    )

    with _client(handler) as client:
        refs = client.list_agents()

    # Contract: a single GET to the current v2 agents route with the KEY header.
    assert len(handler.requests) == 1
    req = handler.requests[0]
    assert req.method == "GET"
    assert req.url.path == AGENTS_PATH == "/api/v2/agents"
    assert req.headers.get("KEY") == "ADMIN123"

    assert [r.paw for r in refs] == ["paw-1", "paw-2"]
    assert all(isinstance(r, AgentRef) for r in refs)


def test_list_agents_filters_by_running_environment_group() -> None:
    agents = [
        {"paw": "paw-red", "group": "red", "alive": True},
        {"paw": "paw-blue", "group": "blue", "alive": True},
    ]
    handler = RouterHandler().route(
        "GET", AGENTS_PATH, httpx.Response(200, json=agents)
    )

    with _client(handler) as client:
        red = client.list_agents(group="red")

    # Only agents of the expected (running environment) group are returned.
    assert [r.paw for r in red] == ["paw-red"]


# ---------------------------------------------------------------------------
# Pointing divergence -> typed error (Req. 4.5)
# ---------------------------------------------------------------------------


def test_list_agents_404_route_maps_to_api_error() -> None:
    # A route that does not match the running Caldera API answers 404; this must
    # surface as the typed CalderaApiError the Stage 5 gate maps to a "pointing
    # divergence" block (Req. 4.5).
    handler = RouterHandler().route(
        "GET", AGENTS_PATH, httpx.Response(404, text="not found")
    )

    with _client(handler) as client:
        with pytest.raises(CalderaApiError) as excinfo:
            client.list_agents()

    assert excinfo.value.status_code == 404
    assert excinfo.value.method == "GET"
    assert excinfo.value.path == AGENTS_PATH
    assert "not found" in excinfo.value.body


def test_list_agents_non_resolvable_host_maps_to_unavailable() -> None:
    # A non-resolvable host / connection failure (configured pointing does not
    # match the running environment) must surface as the typed CalderaUnavailable
    # rather than leaking a raw httpx error (Req. 4.5).
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known", request=request)

    with _client(handler) as client:
        with pytest.raises(CalderaUnavailable) as excinfo:
            client.list_agents()

    assert excinfo.value.url == "http://localhost:8888"
    assert excinfo.value.timeout == 10.0


# ---------------------------------------------------------------------------
# Orphan detection — alive=false (Req. 4.4)
# ---------------------------------------------------------------------------


def test_list_agents_parses_alive_false_as_orphan() -> None:
    # Reproduces the observed condition: two agents on host "kali", one orphan
    # (alive=false) left behind by a rebuilt image. The orphan must be parsed
    # with alive=False so the Stage 5 health gate can detect it.
    agents = [
        {"paw": "paw-alive", "group": "red", "alive": True, "host": "kali"},
        {"paw": "paw-orphan", "group": "red", "alive": False, "host": "kali"},
    ]
    handler = RouterHandler().route(
        "GET", AGENTS_PATH, httpx.Response(200, json=agents)
    )

    with _client(handler) as client:
        refs = client.list_agents(group="red")

    by_paw = {r.paw: r for r in refs}
    assert by_paw["paw-alive"].alive is True
    assert by_paw["paw-orphan"].alive is False

    # The orphan is detectable: at least one agent is reported as not alive.
    orphans = [r for r in refs if not r.alive]
    assert [r.paw for r in orphans] == ["paw-orphan"]


def test_list_agents_missing_alive_defaults_to_not_alive() -> None:
    # A payload without an explicit ``alive`` field is treated as not alive
    # (conservative default), so it is never mistaken for a healthy agent.
    handler = RouterHandler().route(
        "GET", AGENTS_PATH, httpx.Response(200, json=[{"paw": "paw-x", "group": "red"}])
    )

    with _client(handler) as client:
        refs = client.list_agents()

    assert refs[0].alive is False
