"""Service tests for the Stage 5 agent-health gate (task 8.1 — Req. 4).

Exercises :meth:`~app.services.environment.stage_service.EnvironmentStageService.verify_agents_health`
— the environment check that **belongs to Stage 5** (Req. 5.1) and gates the
advance to Stage 6 based on the Caldera agents of the expected group.

These are **example** tests that pin the four verdicts of the gate against a
**mocked** Caldera via :class:`httpx.MockTransport` — *no real Caldera and no
real network*, in line with the spec's "no real emulation during development"
constraint. The gate is a pure, side-effect-free Caldera read, so no DB session
is needed: the ``EnvironmentStageService`` is built with ``session=None`` and a
``CalderaClient`` injected over a ``MockTransport``.

What is verified here:

* **Release (Req. 4.1)** — ≥ 1 agent ``alive=true`` in the expected group ⇒
  ``released`` with ``cause == NONE``; orphans coexisting are still signalled
  (Req. 4.3) but do not block.
* **No active agent (Req. 4.2)** — group with no agent at all ⇒ blocked with
  ``cause == NO_ACTIVE_AGENT``.
* **Orphan (Req. 4.3)** — group with only ``alive=false`` agents ⇒ blocked with
  ``cause == ORPHAN`` and the orphan paws signalled.
* **Pointing divergence (Req. 4.4/4.5)** — a Caldera API error (e.g. 404 on the
  route) or a non-resolvable host ⇒ blocked with ``cause == POINTING_DIVERGENCE``
  (the typed Caldera errors never leak).
* **Group scoping (Req. 4.1)** — an ``alive=true`` agent in **another** group
  never releases the expected group.
* **Read-only / reusable verdict (Req. 5.1/5.2)** — the verdict is derived with
  a single ``GET /api/v2/agents`` and no mutation, so Stage 6 can consume the
  same result without re-running the check.

_Requirements: 4.1, 4.2, 4.3, 4.4, 4.6, 5.1, 5.2_
"""

from __future__ import annotations

import httpx

from app.services.caldera.client import AGENTS_PATH, CalderaClient
from app.services.environment.stage_service import (
    AgentHealthCause,
    EnvironmentStageService,
)

EXPECTED_GROUP = "red"
CAMPAIGN = "shadowray"


def _service_serving(
    *, status: int = 200, payloads: list[dict[str, object]] | None = None
) -> tuple[EnvironmentStageService, list[httpx.Request]]:
    """Build a service whose injected Caldera serves ``payloads`` on GET agents.

    Returns the service and the recorded requests list so the tests can assert
    the gate issues exactly one read (read-only — Req. 5.1/5.2).
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == AGENTS_PATH
        return httpx.Response(status, json=payloads if payloads is not None else [])

    client = CalderaClient(transport=httpx.MockTransport(handler))
    service = EnvironmentStageService(
        session=None,  # type: ignore[arg-type]  # gate is a pure Caldera read
        caldera_client=client,
        agent_group=EXPECTED_GROUP,
    )
    return service, requests


def test_release_when_alive_agent_in_group() -> None:
    """≥1 alive in the expected group releases the advance (Req. 4.1).

    _Requirements: 4.1_
    """
    service, requests = _service_serving(
        payloads=[{"paw": "aaa", "group": EXPECTED_GROUP, "alive": True}]
    )

    result = service.verify_agents_health(CAMPAIGN)

    assert result.released is True
    assert result.cause is AgentHealthCause.NONE
    assert result.active_agents == 1
    assert result.orphan_paws == []
    assert result.message is None
    assert result.group == EXPECTED_GROUP
    # Read-only: exactly one GET /api/v2/agents, no mutation (Req. 5.1/5.2).
    assert len(requests) == 1


def test_release_signals_coexisting_orphans() -> None:
    """An orphan alongside a live agent is signalled but does not block (Req. 4.3).

    _Requirements: 4.1, 4.3_
    """
    service, _ = _service_serving(
        payloads=[
            {"paw": "live", "group": EXPECTED_GROUP, "alive": True},
            {"paw": "dead", "group": EXPECTED_GROUP, "alive": False},
        ]
    )

    result = service.verify_agents_health(CAMPAIGN)

    assert result.released is True
    assert result.cause is AgentHealthCause.NONE
    assert result.active_agents == 1
    assert result.orphan_paws == ["dead"]


def test_block_no_active_agent() -> None:
    """No agent at all in the group blocks with NO_ACTIVE_AGENT (Req. 4.2).

    _Requirements: 4.2, 4.6_
    """
    service, _ = _service_serving(payloads=[])

    result = service.verify_agents_health(CAMPAIGN)

    assert result.released is False
    assert result.cause is AgentHealthCause.NO_ACTIVE_AGENT
    assert result.active_agents == 0
    assert result.orphan_paws == []
    assert result.message is not None


def test_block_orphan_only() -> None:
    """Only alive=false agents in the group block with ORPHAN (Req. 4.3).

    _Requirements: 4.3, 4.6_
    """
    service, _ = _service_serving(
        payloads=[
            {"paw": "zzz", "group": EXPECTED_GROUP, "alive": False},
            {"paw": "aaa", "group": EXPECTED_GROUP, "alive": False},
        ]
    )

    result = service.verify_agents_health(CAMPAIGN)

    assert result.released is False
    assert result.cause is AgentHealthCause.ORPHAN
    assert result.active_agents == 0
    # Orphans are sorted paws (Req. 4.3).
    assert result.orphan_paws == ["aaa", "zzz"]
    assert result.message is not None


def test_block_pointing_divergence_on_api_error() -> None:
    """A Caldera API error (404) maps to POINTING_DIVERGENCE (Req. 4.4/4.5).

    _Requirements: 4.4, 4.6_
    """
    service, _ = _service_serving(status=404, payloads={"error": "not found"})  # type: ignore[arg-type]

    result = service.verify_agents_health(CAMPAIGN)

    assert result.released is False
    assert result.cause is AgentHealthCause.POINTING_DIVERGENCE
    assert result.active_agents == 0
    assert result.message is not None


def test_block_pointing_divergence_on_unreachable_host() -> None:
    """A non-resolvable host (connection failure) maps to POINTING_DIVERGENCE.

    _Requirements: 4.4, 4.6_
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("host não resolvível", request=request)

    client = CalderaClient(transport=httpx.MockTransport(handler))
    service = EnvironmentStageService(
        session=None,  # type: ignore[arg-type]
        caldera_client=client,
        agent_group=EXPECTED_GROUP,
    )

    result = service.verify_agents_health(CAMPAIGN)

    assert result.released is False
    assert result.cause is AgentHealthCause.POINTING_DIVERGENCE
    assert result.message is not None


def test_release_is_group_scoped() -> None:
    """An alive agent in another group never releases the expected group (Req. 4.1).

    _Requirements: 4.1_
    """
    service, _ = _service_serving(
        payloads=[{"paw": "blue1", "group": "blue", "alive": True}]
    )

    result = service.verify_agents_health(CAMPAIGN)

    assert result.released is False
    assert result.active_agents == 0
    assert result.cause is AgentHealthCause.NO_ACTIVE_AGENT


def test_group_override_argument() -> None:
    """The ``group`` argument overrides the service default (Req. 4.1).

    _Requirements: 4.1_
    """
    service, _ = _service_serving(
        payloads=[{"paw": "blue1", "group": "blue", "alive": True}]
    )

    result = service.verify_agents_health(CAMPAIGN, group="blue")

    assert result.group == "blue"
    assert result.released is True
    assert result.active_agents == 1
