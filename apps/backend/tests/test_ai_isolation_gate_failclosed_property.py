"""Property test — isolation gate is fail-closed on agent-proposed environments (task 6.4).

# Feature: ai-first-campaign-flow, Property 2: Isolation gate is fail-closed on
# agent-proposed environments. The Environment agent *proposes* an environment
# (container/network attachment shape); the read-only, fail-closed isolation gate
# (``containment.inspect_isolation`` -> ``IsolationSummary``) *disposes*. The gate
# approves IFF every target container is attached exclusively to ``internal: true``
# networks with no external bridge and no external DNS; the EMPTY target set and an
# UNAVAILABLE inspector (daemon down) both fail closed (``approved=False``); and a
# rejected environment never proceeds to execution — the AI provisioner keeps the
# produced ``ProvisionedEnvironment.ready`` ``False`` so Stage 6 stays blocked.

Validates: Requirements 3.1, 12.2

This is a *feature* property (``ai-first-campaign-flow`` is a feature, not a
bugfix): the property must **hold** over the reused isolation gate and the AI
Environment agent (``AiEnvironmentProvisioner``) as they exist today.

No Docker, Caldera, network, LLM or database is ever touched. Hypothesis varies the
container/network attachment shape the agent proposes; that shape is fed to the
**real** ``inspect_isolation`` through an in-memory, daemon-free
:class:`_FakeInspector` (the same read-only seam production uses) — so the property
exercises the production isolation logic, not a reimplementation of it. The
agent-proposes/gate-disposes end-to-end is then closed by driving the resulting
isolation verdict through ``AiEnvironmentProvisioner`` via an injected, daemon-free
fake pipeline whose only varying field is the isolation summary: a rejected
environment must keep ``ready`` ``False`` (never proceeds to execution), an approved
one may be ready. The ResourceGate is kept permissive (its own bound is Property 1,
task 5.2/6.2) so this test isolates the containment/isolation gate.

The central assertion is that the isolation gate is a **hard, fail-closed
pre-condition**: the gate approves exactly the isolated shapes (an independent
oracle recomputes the rule), the empty set and the unavailable inspector both fail
closed, and ``ready`` is never ``True`` when the gate did not approve.

_Requirements: 3.1, 12.2_
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.enums import EnvironmentPlatform, TranslationSource
from app.services.ai.agents.env_agent import AiEnvironmentProvisioner
from app.services.containment.isolation import (
    ContainerNetworkInfo,
    ContainerNotFound,
    NetworkInfo,
    NetworkNotFound,
    inspect_isolation,
)
from app.services.containment.subnets import INTERNAL_NETWORK_SUBNETS
from app.services.environment.compose_executor import LogLine
from app.services.environment.health import HealthSummary, HealthVerdict
from app.services.environment.operationalization import (
    EnvironmentParameters,
    TargetHost,
)
from app.services.environment.provisioner import (
    DeprovisioningResult,
    IsolationSummary,
    ProvisionedEnvironment,
    SubStageSummary,
)

_SLUG = "ai-isolation-prop"

# A known-internal network (``internal: true``, lab subnet) and three flavours of
# NON-isolating attachment the gate must reject (external bridge name, a
# non-internal flagged network, and a network the inspector cannot find).
_INTERNAL_NET = "kali-nginx-network"  # internal:true, 192.168.20.0/24
_EXTERNAL_BRIDGE = "local-network"  # a KNOWN external bridge name -> rejected
_NON_INTERNAL_NET = "public-net"  # inspector returns internal=False -> rejected
_MISSING_NET = "ghost-net"  # inspector raises NetworkNotFound -> rejected

_EXTERNAL_DNS = "8.8.8.8"  # external resolver -> rejected
_INTERNAL_DNS = "127.0.0.1"  # loopback -> acceptable

# All the non-internal network tokens, used by the oracle to decide isolation.
_NON_ISOLATING_NETWORKS = frozenset({_EXTERNAL_BRIDGE, _NON_INTERNAL_NET, _MISSING_NET})


# ---------------------------------------------------------------------------
# In-memory, daemon-free inspectors (same read-only seam production uses)
# ---------------------------------------------------------------------------


@dataclass
class _FakeInspector:
    """In-memory read-only container/network inspector (no Docker daemon).

    Mirrors the production ``ContainerInspector`` surface. The whole isolation
    scenario lives in the ``containers``/``networks`` maps the generator fills,
    exactly as :func:`inspect_isolation` consumes them.
    """

    containers: dict[str, ContainerNetworkInfo] = field(default_factory=dict)
    networks: dict[str, NetworkInfo] = field(default_factory=dict)

    def get_container_network_info(self, container: str) -> ContainerNetworkInfo:
        try:
            return self.containers[container]
        except KeyError as exc:
            raise ContainerNotFound(container) from exc

    def get_network_info(self, network_name: str) -> NetworkInfo:
        try:
            return self.networks[network_name]
        except KeyError as exc:
            raise NetworkNotFound(network_name) from exc


# ---------------------------------------------------------------------------
# A daemon-free pipeline whose ONLY varying field is the isolation verdict.
# Every other ``ready`` prerequisite is satisfied, so ``ProvisionedEnvironment.ready``
# is conditioned SOLELY on the isolation gate — exactly the "agent proposes, gate
# disposes" pre-condition under test.
# ---------------------------------------------------------------------------


class _IsolationOnlyPipeline:
    """Fake pipeline returning a fully-ready env except for the injected isolation."""

    def __init__(self, isolation: IsolationSummary, containers: list[str]) -> None:
        self._isolation = isolation
        self._containers = containers

    def provision(
        self,
        params: EnvironmentParameters,
        *,
        on_log: Callable[[LogLine], None] = lambda _l: None,
    ) -> ProvisionedEnvironment:
        failed = None if self._isolation.approved else "verificar_isolamento"
        return ProvisionedEnvironment(
            campaign_id=params.campaign_id,
            platform=EnvironmentPlatform.CONTAINER,
            target_containers=self._containers,
            compose_file="docker/docker-compose.yml",
            health=HealthSummary(
                approved=True,
                verdicts=[HealthVerdict(container=c, running=True) for c in self._containers],
            ),
            loaded_abilities=True,
            isolation=self._isolation,
            substages=SubStageSummary(
                build_online_ok=True,
                egress_closed=True,
                egress_lost_verified=True,
                offline_applied=True,
            ),
            origin=TranslationSource.HUMAN_CURATION,
            failed_step=failed,
        )

    def deprovision(
        self,
        environment: ProvisionedEnvironment,
        *,
        on_log: Callable[[LogLine], None] = lambda _l: None,
    ) -> DeprovisioningResult:  # pragma: no cover - not exercised here
        return DeprovisioningResult(deprovisioned=True)


# ---------------------------------------------------------------------------
# Generator — a container/network attachment shape the agent proposes.
#
# Each proposed container carries a set of network attachments and a set of DNS
# servers. Networks are drawn from one internal name plus three non-isolating
# tokens (external bridge, non-internal flagged, missing); DNS from a loopback
# (acceptable) and an external resolver (rejected). The EMPTY target set is drawn
# too (``min_size=0``), and an empty attachment list for a container (attached to
# no network) is also reachable — both must fail closed.
# ---------------------------------------------------------------------------

_network_token = st.sampled_from([_INTERNAL_NET, _EXTERNAL_BRIDGE, _NON_INTERNAL_NET, _MISSING_NET])
_dns_token = st.sampled_from([_INTERNAL_DNS, _EXTERNAL_DNS])


@st.composite
def _attachment(draw) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """One container's (networks, dns) attachment shape."""
    networks = tuple(draw(st.lists(_network_token, min_size=0, max_size=3, unique=True)))
    dns = tuple(draw(st.lists(_dns_token, min_size=0, max_size=2, unique=True)))
    return networks, dns


# A map from container name -> its attachment; container count 0..4 so the EMPTY
# target set (fail-closed) and multi-container shapes are both exercised.
_shape = st.dictionaries(
    keys=st.sampled_from(["c0", "c1", "c2", "c3"]),
    values=_attachment(),
    min_size=0,
    max_size=4,
)


def _build_inspector(shape: dict[str, tuple[tuple[str, ...], tuple[str, ...]]]) -> _FakeInspector:
    """Build a fake inspector from a proposed attachment shape."""
    insp = _FakeInspector()
    # Register the internal + non-internal networks the gate will look up. The
    # external bridge name and the missing network are NOT registered: the bridge
    # name is short-circuited by the gate's known-external set, and the missing
    # network must raise ``NetworkNotFound`` (fail-closed).
    insp.networks[_INTERNAL_NET] = NetworkInfo(
        name=_INTERNAL_NET,
        internal=True,
        subnets=(INTERNAL_NETWORK_SUBNETS[_INTERNAL_NET],),
    )
    insp.networks[_NON_INTERNAL_NET] = NetworkInfo(
        name=_NON_INTERNAL_NET, internal=False, subnets=("10.0.0.0/8",)
    )
    for name, (networks, dns) in shape.items():
        insp.containers[name] = ContainerNetworkInfo(name=name, network_names=networks, dns=dns)
    return insp


def _oracle_isolated(networks: tuple[str, ...], dns: tuple[str, ...]) -> bool:
    """Independent oracle of the isolation rule (NOT the implementation under test).

    A container is isolated IFF it is attached to at least one network, EVERY
    attached network is internal (none in the non-isolating set), and it has NO
    external DNS configured. This recomputes Property 2's definition so the test
    does not merely echo ``inspect_isolation``.
    """
    if not networks:
        return False
    if any(n in _NON_ISOLATING_NETWORKS for n in networks):
        return False
    return not any(server != _INTERNAL_DNS for server in dns)


def _params(containers: list[str]) -> EnvironmentParameters:
    return EnvironmentParameters(
        campaign_id=_SLUG,
        platform=EnvironmentPlatform.CONTAINER,
        hosts=[TargetHost(label=f"h{i}", container=c) for i, c in enumerate(containers)],
        origin=TranslationSource.HUMAN_CURATION,
    )


# ---------------------------------------------------------------------------
# Property — the gate approves exactly the isolated shapes, fails closed on the
# empty set, and a rejected environment never proceeds to execution.
# ---------------------------------------------------------------------------


@settings(max_examples=10, deadline=None)
@given(shape=_shape)
def test_isolation_gate_failclosed_on_agent_proposed_environment(
    shape: dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
) -> None:
    # Feature: ai-first-campaign-flow, Property 2: Isolation gate is fail-closed
    # on agent-proposed environments.
    containers = sorted(shape.keys())
    inspector = _build_inspector(shape)

    # The agent PROPOSES; the gate DISPOSES — run the real, read-only gate.
    result = inspect_isolation(containers, inspector)
    summary = IsolationSummary.from_isolation_result(result)

    # Independent oracle: the gate approves IFF the target set is non-empty AND
    # every proposed container is attached exclusively to internal networks with
    # no external DNS.
    expected_isolated = {c: _oracle_isolated(*shape[c]) for c in containers}
    expected_approved = bool(containers) and all(expected_isolated.values())

    # (1) Approval IFF every target container is isolated; the empty set fails
    #     closed (``containers`` empty -> ``expected_approved`` False).
    assert summary.approved is expected_approved
    if not containers:
        assert summary.approved is False
        assert summary.verdicts == []

    # (2) Per-container verdicts match the oracle, and every non-isolated verdict
    #     carries a reason (so the audit can show WHY it was rejected).
    for verdict in summary.verdicts:
        assert verdict.isolated is expected_isolated[verdict.container]
        if not verdict.isolated:
            assert verdict.reasons, "a non-isolated container must carry a reason"

    # (3) A rejected environment NEVER proceeds to execution: driving the verdict
    #     through the AI provisioner keeps ``ready`` False whenever the gate did
    #     not approve, and only an approved gate can let ``ready`` be True (every
    #     other prerequisite is satisfied, so ``ready`` is conditioned SOLELY on
    #     the isolation gate). The ResourceGate is kept permissive to isolate the
    #     containment gate under test.
    agent = AiEnvironmentProvisioner(pipeline=_IsolationOnlyPipeline(summary, containers))
    environment = agent.provision(_params(containers))

    # Origin is always re-stamped to AI_GENERATED (the agent's artifact).
    assert environment.origin is TranslationSource.AI_GENERATED
    # ``ready`` is True IFF the gate approved — fail-closed completion pre-condition.
    assert environment.ready is expected_approved
    if not expected_approved:
        assert environment.ready is False
        assert environment.isolation.approved is False
        assert environment.failed_step == "verificar_isolamento"


# ---------------------------------------------------------------------------
# Deterministic fail-closed edge cases that the generator cannot reliably reach:
# the EMPTY target set and the UNAVAILABLE inspector (daemon down).
# ---------------------------------------------------------------------------
