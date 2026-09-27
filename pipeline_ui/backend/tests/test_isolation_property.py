"""Property-based test for container isolation (task 4.5) — security core.

# Feature: pipeline-ui, Property 2: Emulação só prossegue com todos os
# containers-alvo isolados. Para qualquer conjunto de containers-alvo com
# configurações de rede arbitrárias, o pre-flight aprova a Operação se e somente
# se todos os containers-alvo estão conectados exclusivamente a redes internas
# (sem rede bridge externa e sem DNS externo); se algum não estiver isolado, a
# Operação aborta sem executar nenhum comando, identificando o container que
# falhou.

Validates: Requisitos 6.3, 6.4

This test drives :func:`verify_isolation` through a FAKE
:class:`ContainerInspector` that serves fabricated
:class:`ContainerNetworkInfo` / :class:`NetworkInfo` from Hypothesis-generated
data. NO real Docker daemon is touched — everything is pure in-memory, so it is
safe on the Windows dev environment.

The generators produce, per container:
- networks attached only to ``internal: true`` lab networks with no external DNS
  (which SHOULD be isolated), and
- containers attached to ``local-network`` / ``bridge`` / ``host``, or to
  ``internal: false`` networks, or to internal-flagged networks whose declared
  subnets fall outside the known ranges, and/or with an external DNS such as
  ``8.8.8.8`` (which SHOULD NOT be isolated).

An INDEPENDENT oracle (``_expected_isolated``) recomputes isolation directly from
the generated data — the assertions never trust the implementation to decide
what "isolated" means. We assert:

* ``result.passed`` is True IFF every generated container is isolated per the
  oracle;
* when any container is not isolated, ``result.passed`` is False, the offending
  container(s) match the oracle exactly, and each offending verdict carries at
  least one human-readable reason (Req. 6.4 — identify the failing container).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.containment.isolation import (
    ContainerNetworkInfo,
    ContainerNotFound,
    NetworkInfo,
    NetworkNotFound,
    verify_isolation,
)
from app.services.containment.subnets import (
    INTERNAL_NETWORK_SUBNETS,
    is_internal_subnet,
)

# Names the implementation always treats as external bridges (mirrors the module
# under test — kept here so the oracle is independent of the module constant).
KNOWN_EXTERNAL_NETWORK_NAMES = frozenset({"local-network", "bridge", "host"})


# ---------------------------------------------------------------------------
# Fake, in-memory ContainerInspector (NO real Docker daemon)
# ---------------------------------------------------------------------------


@dataclass
class FakeInspector:
    """In-memory :class:`ContainerInspector` serving fabricated configs.

    Read-only, like the real adapter: it just looks up dictionaries populated
    from generated data. Unknown containers/networks raise the same lookup
    errors the real SDK adapter raises, so the fail-closed paths are exercised.
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
# Generated-network model + strategies
# ---------------------------------------------------------------------------

# Real internal lab networks (internal: true, subnet in a known range).
_INTERNAL_NETWORK_NAMES = tuple(INTERNAL_NETWORK_SUBNETS.keys())

# DNS pools: internal/loopback are acceptable; the external pool must break
# isolation (mirrors the "no external DNS" rule).
_ACCEPTABLE_DNS = ("127.0.0.1", "172.20.0.10", "172.21.0.10", "172.22.0.10")
_EXTERNAL_DNS = ("8.8.8.8", "1.1.1.1", "9.9.9.9", "resolver.example.com")


@dataclass(frozen=True)
class GenNetwork:
    """A generated network definition the fake inspector can serve."""

    name: str
    internal: bool
    subnets: tuple[str, ...]


def _internal_lab_networks() -> st.SearchStrategy[GenNetwork]:
    """A genuinely-internal lab network: internal=True with a known subnet."""
    return st.sampled_from(_INTERNAL_NETWORK_NAMES).map(
        lambda name: GenNetwork(
            name=name,
            internal=True,
            subnets=(INTERNAL_NETWORK_SUBNETS[name],),
        )
    )


def _bad_internal_flagged_networks() -> st.SearchStrategy[GenNetwork]:
    """internal=True but with a subnet OUTSIDE the known internal ranges.

    Exercises the design's cross-check: an internal-flagged network whose IPAM
    subnet is not one of the lab ranges must NOT be trusted.
    """
    return st.builds(
        lambda idx, subnet: GenNetwork(
            name=f"suspicious-net-{idx}", internal=True, subnets=(subnet,)
        ),
        st.integers(min_value=0, max_value=5),
        st.sampled_from(("10.0.0.0/24", "192.168.5.0/24", "203.0.113.0/24")),
    )


def _non_internal_networks() -> st.SearchStrategy[GenNetwork]:
    """A custom network with internal=False (external reachability)."""
    return st.builds(
        lambda idx: GenNetwork(name=f"external-net-{idx}", internal=False, subnets=()),
        st.integers(min_value=0, max_value=5),
    )


# Custom (non-bridge-named) networks: mix of good internal, bad internal-flagged,
# and plainly non-internal. These get registered in the inspector's network map.
_custom_networks = st.one_of(
    _internal_lab_networks(),
    _bad_internal_flagged_networks(),
    _non_internal_networks(),
)


@dataclass(frozen=True)
class GenContainer:
    """A generated container: its attachments (by name) and DNS servers.

    ``custom_networks`` are attachments resolvable via the inspector's network
    map. ``bridge_names`` are attachments to the always-external names
    (local-network/bridge/host) that are NOT registered as networks (the
    implementation short-circuits on the name before any lookup).
    """

    name: str
    custom_networks: tuple[GenNetwork, ...]
    bridge_names: tuple[str, ...]
    dns: tuple[str, ...]


def _container_strategy() -> st.SearchStrategy[GenContainer]:
    return st.builds(
        GenContainer,
        name=st.uuids().map(lambda u: f"container-{u.hex[:8]}"),
        custom_networks=st.lists(_custom_networks, min_size=0, max_size=3).map(tuple),
        bridge_names=st.lists(
            st.sampled_from(sorted(KNOWN_EXTERNAL_NETWORK_NAMES)),
            min_size=0,
            max_size=2,
        ).map(tuple),
        dns=st.lists(
            st.sampled_from(_ACCEPTABLE_DNS + _EXTERNAL_DNS), min_size=0, max_size=3
        ).map(tuple),
    )


def _unique_containers() -> st.SearchStrategy[list[GenContainer]]:
    return st.lists(
        _container_strategy(),
        min_size=1,
        max_size=5,
        unique_by=lambda c: c.name,
    )


# ---------------------------------------------------------------------------
# Independent oracle — recomputes isolation from the generated data
# ---------------------------------------------------------------------------


def _expected_isolated(container: GenContainer) -> bool:
    """Compute the expected isolation verdict directly from generated data.

    Mirrors the Property 2 definition WITHOUT calling the implementation:
    a container is isolated iff it has at least one attachment, no attachment to
    a known external bridge name, every custom network is internal=True with an
    internal subnet (or no declared subnet), and no external DNS.
    """
    all_attachments = list(container.bridge_names) + [
        n.name for n in container.custom_networks
    ]
    # Rule 1: attached to at least one network.
    if not all_attachments:
        return False

    # Rule 2a: no known external bridge name.
    if container.bridge_names:
        return False

    # Rule 2b: every custom network internal, and (if it declares subnets)
    # at least one subnet must be internal.
    for net in container.custom_networks:
        if not net.internal:
            return False
        if net.subnets and not any(is_internal_subnet(cidr) for cidr in net.subnets):
            return False

    # Rule 3: no external DNS.
    for server in container.dns:
        if server in _EXTERNAL_DNS:
            return False

    return True


def _build_inspector(containers: list[GenContainer]) -> FakeInspector:
    """Populate a FakeInspector from generated containers (bridges stay unregistered)."""
    inspector = FakeInspector()
    for c in containers:
        network_names = tuple(c.bridge_names) + tuple(
            n.name for n in c.custom_networks
        )
        inspector.containers[c.name] = ContainerNetworkInfo(
            name=c.name, network_names=network_names, dns=c.dns
        )
        for net in c.custom_networks:
            # Later duplicates with the same name just overwrite with an
            # equivalent definition; sampled internal nets are deterministic.
            inspector.networks[net.name] = NetworkInfo(
                name=net.name, internal=net.internal, subnets=net.subnets
            )
    return inspector


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(containers=_unique_containers())
def test_property2_isolation_passes_iff_all_isolated(
    containers: list[GenContainer],
) -> None:
    """verify_isolation approves IFF every target container is isolated.

    # Feature: pipeline-ui, Property 2: Emulação só prossegue com todos os
    # containers-alvo isolados.
    Validates: Requisitos 6.3, 6.4
    """
    inspector = _build_inspector(containers)
    names = [c.name for c in containers]

    result = verify_isolation(names, inspector)

    # Independent oracle: expected per-container and overall verdicts.
    expected_isolated = {c.name: _expected_isolated(c) for c in containers}
    expected_all = all(expected_isolated.values())

    # Overall pass IFF every generated container is isolated by the definition.
    assert result.passed is expected_all

    # Per-container verdicts agree with the oracle, in order.
    assert [v.container for v in result.verdicts] == names
    for verdict in result.verdicts:
        assert verdict.isolated is expected_isolated[verdict.container]
        if verdict.isolated:
            assert verdict.reasons == ()
        else:
            # Req. 6.4: a failing container must be identified with a reason.
            assert verdict.reasons != ()

    # When not all isolated, the operation aborts and offending set == oracle.
    if not expected_all:
        assert result.passed is False
        offending_names = {v.container for v in result.offending}
        expected_offending = {
            name for name, ok in expected_isolated.items() if not ok
        }
        assert offending_names == expected_offending
        assert offending_names  # at least one offender identified
        for verdict in result.offending:
            assert verdict.reasons != ()
        # The 409-style summary names the containment failure.
        assert "não satisfeita" in result.summary()
    else:
        assert result.passed is True
        assert result.offending == ()


def _isolated_container_strategy() -> st.SearchStrategy[GenContainer]:
    """Build a container guaranteed to be isolated by construction.

    At least one genuinely-internal lab network, no bridge names, and only
    acceptable (loopback/internal) DNS servers.
    """
    return st.builds(
        lambda name, nets, dns: GenContainer(
            name=name, custom_networks=tuple(nets), bridge_names=(), dns=tuple(dns)
        ),
        st.uuids().map(lambda u: f"iso-{u.hex[:8]}"),
        st.lists(_internal_lab_networks(), min_size=1, max_size=3),
        st.lists(st.sampled_from(_ACCEPTABLE_DNS), min_size=0, max_size=3),
    )


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(
    containers=st.lists(
        _isolated_container_strategy(),
        min_size=1,
        max_size=4,
        unique_by=lambda c: c.name,
    )
)
def test_property2_all_isolated_containers_are_approved(
    containers: list[GenContainer],
) -> None:
    """A set built to be fully isolated is always approved (targeted direction).

    # Feature: pipeline-ui, Property 2: Emulação só prossegue com todos os
    # containers-alvo isolados.
    Validates: Requisitos 6.3, 6.4
    """
    # Oracle sanity: everything built here is isolated by construction.
    assert all(_expected_isolated(c) for c in containers)

    inspector = _build_inspector(containers)
    result = verify_isolation([c.name for c in containers], inspector)
    assert result.passed is True
    assert result.offending == ()
