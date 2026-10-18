"""Property-based test for the agent-health advance gate (task 2.2).

# Feature: stage6-execution-redesign, Property 15: Liberação de avanço por saúde de agentes

*Para qualquer* lista de agentes do Caldera, o avanço do Estágio 5 para o Estágio 6
é liberado se e somente se existe pelo menos um agente com ``alive=true`` no grupo
esperado; a presença de agentes órfãos (``alive=false``) é sinalizada e, na ausência
de qualquer ``alive=true``, o avanço é bloqueado.

**Validates: Requirements 4.1, 4.3**

Esta propriedade é exercida sobre :meth:`CalderaClient.list_agents`, alimentado por
listas de agentes geradas pelo Hypothesis e servidas por um
:class:`httpx.MockTransport` em ``GET /api/v2/agents`` — *sem Caldera real e sem rede*,
em linha com a restrição de "nenhuma emulação real durante o desenvolvimento".

A decisão de liberação do gate de saúde (Req. 4.1/4.3) é derivada do que
``list_agents(group)`` retorna:

* **released** sse ≥ 1 agente com ``alive=true`` no grupo esperado (4.1);
* **orphans** = os agentes ``alive=false`` do grupo esperado, sempre sinalizados (4.3);
* ausência de qualquer ``alive=true`` no grupo ⇒ avanço bloqueado (4.3).

Um **oráculo independente** (:func:`_expected_release`) recomputa a liberação e os
órfãos diretamente dos dados gerados, de modo que as asserções nunca confiam na
implementação para decidir o que "liberar" significa.
"""

from __future__ import annotations

import json

import httpx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.caldera.client import (
    AGENTS_PATH,
    AgentRef,
    CalderaClient,
)

# The group the Stage 5 health gate expects agents to be registered under.
EXPECTED_GROUP = "red"

# A small pool of group names so generated agents land in/out of the expected
# group with meaningful frequency (and ``None`` exercises the "no group" case).
_GROUPS = st.sampled_from([EXPECTED_GROUP, "blue", "misc", None])


# ---------------------------------------------------------------------------
# Generators: arbitrary agent payloads (as the v2 API returns them)
# ---------------------------------------------------------------------------


@st.composite
def _agent_payload(draw: st.DrawFn) -> dict[str, object]:
    """Generate one ``GET /api/v2/agents`` item with varied ``alive``/``group``."""
    paw = draw(st.text(alphabet="abcdef0123456789", min_size=3, max_size=8))
    group = draw(_GROUPS)
    alive = draw(st.booleans())
    payload: dict[str, object] = {"paw": paw, "alive": alive}
    # ``group`` may be absent entirely (None) to exercise the missing-field path.
    if group is not None:
        payload["group"] = group
    # Optional, realistic extras that must not affect the release decision.
    if draw(st.booleans()):
        payload["host"] = draw(st.sampled_from(["kali", "attacker", "ubuntu"]))
    if draw(st.booleans()):
        payload["last_seen"] = draw(st.sampled_from(["2024-01-01T00:00:00Z", ""]))
    return payload


_agent_lists = st.lists(_agent_payload(), min_size=0, max_size=12)


# ---------------------------------------------------------------------------
# Independent oracle (recomputed straight from the generated payloads)
# ---------------------------------------------------------------------------


def _expected_release(
    payloads: list[dict[str, object]], group: str
) -> tuple[bool, list[str]]:
    """Recompute (released, orphan_paws) from the raw generated agent list.

    Release is granted iff at least one agent in ``group`` has ``alive=true``.
    Orphans are the ``alive=false`` agents in ``group`` (signalled regardless of
    the release decision).
    """
    in_group = [p for p in payloads if p.get("group") == group]
    released = any(bool(p.get("alive", False)) for p in in_group)
    orphans = sorted(
        str(p.get("paw", "")) for p in in_group if not bool(p.get("alive", False))
    )
    return released, orphans


# ---------------------------------------------------------------------------
# Gate decision derived from the client's typed result (the code under test)
# ---------------------------------------------------------------------------


def _release_from_agents(agents: list[AgentRef]) -> tuple[bool, list[str]]:
    """Derive (released, orphan_paws) from ``list_agents(group)`` output.

    ``agents`` is already filtered to the expected group by ``list_agents``.
    """
    released = any(agent.alive for agent in agents)
    orphans = sorted(agent.paw for agent in agents if not agent.alive)
    return released, orphans


# ---------------------------------------------------------------------------
# MockTransport serving the generated agent list from GET /api/v2/agents
# ---------------------------------------------------------------------------


def _client_serving(payloads: list[dict[str, object]]) -> CalderaClient:
    """Build a CalderaClient whose ``GET /api/v2/agents`` returns ``payloads``."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == AGENTS_PATH
        return httpx.Response(200, json=payloads)

    return CalderaClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Property 15
# ---------------------------------------------------------------------------


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(payloads=_agent_lists)
def test_advance_released_iff_alive_agent_in_group(
    payloads: list[dict[str, object]],
) -> None:
    """Release iff ≥1 alive in the expected group; orphans always signalled.

    # Feature: stage6-execution-redesign, Property 15: Liberação de avanço por saúde de agentes
    **Validates: Requirements 4.1, 4.3**
    """
    expected_released, expected_orphans = _expected_release(payloads, EXPECTED_GROUP)

    with _client_serving(payloads) as client:
        agents = client.list_agents(EXPECTED_GROUP)

    # list_agents must return only the expected-group agents (and all of them).
    assert all(agent.group == EXPECTED_GROUP for agent in agents)
    assert len(agents) == len([p for p in payloads if p.get("group") == EXPECTED_GROUP])

    released, orphans = _release_from_agents(agents)

    # Core biconditional (Req. 4.1): released iff ≥1 alive in the expected group.
    assert released == expected_released

    # Orphans (alive=false in group) are signalled regardless of release (Req. 4.3).
    assert orphans == expected_orphans

    # Absence of any alive=true in the group blocks the advance (Req. 4.3).
    if not any(agent.alive for agent in agents):
        assert released is False


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(payloads=_agent_lists)
def test_release_ignores_agents_outside_expected_group(
    payloads: list[dict[str, object]],
) -> None:
    """Agents in other groups never grant release for the expected group.

    # Feature: stage6-execution-redesign, Property 15: Liberação de avanço por saúde de agentes
    **Validates: Requirements 4.1, 4.3**
    """
    with _client_serving(payloads) as client:
        agents = client.list_agents(EXPECTED_GROUP)

    released, _ = _release_from_agents(agents)

    # If no in-group agent is alive, release stays blocked even when other
    # groups have alive agents — proving group scoping of the biconditional.
    in_group_alive = any(
        bool(p.get("alive", False))
        for p in payloads
        if p.get("group") == EXPECTED_GROUP
    )
    assert released == in_group_alive
    # Confirm the payload round-trips as JSON (serving-shape sanity).
    assert json.loads(json.dumps(payloads)) == payloads
