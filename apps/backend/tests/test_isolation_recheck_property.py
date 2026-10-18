"""Property-based test for the Stage 6 isolation gate aggregation (task 4.3).

# Feature: stage6-execution-redesign, Property 12: Isolamento é "isolado" sse
# todos os alvos satisfazem a regra

**Validates: Requirements 6.1**

Requirement 6.1 states that the Stage 6 isolation gate verifies that *every*
target container is on an ``internal: true`` network with no route to the host
or the internet, and that the aggregate verdict "isolado" (``IsolationResult.
passed is True``) SHALL require that **all** targets satisfy the rule.

This test drives :func:`inspect_isolation` (the OperationRunner pre-flight entry
point) through a FAKE :class:`ContainerInspector` injected via its ``inspector``
argument, so NO real Docker daemon is ever touched (safe on Windows dev). The
property asserted is the *biconditional* plus the *fail-closed* behaviour:

* ``result.passed`` is True **iff** every target container is isolated per an
  independent oracle computed directly from the generated data;
* an **empty** target set yields ``passed=False`` (nothing proven isolated, so
  fail-closed rather than vacuously approving);
* an **unknown / non-verifiable** target — a container the inspector cannot
  resolve (``ContainerNotFound``) or one attached to a network the inspector
  cannot resolve (``NetworkNotFound``) — forces the aggregate to NOT be isolated
  (we never approve what we cannot prove isolated).

The oracle (``_expected_isolated``) recomputes isolation from the generated
model WITHOUT trusting the implementation to define "isolated".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.containment.isolation import (
    ContainerInspector,
    ContainerNetworkInfo,
    ContainerNotFound,
    NetworkInfo,
    NetworkNotFound,
    inspect_isolation,
)
from app.services.containment.subnets import (
    INTERNAL_NETWORK_SUBNETS,
    is_internal_subnet,
)

# Names the implementation always treats as external bridges (mirrors the module
# under test — kept independent so the oracle does not import the constant).
KNOWN_EXTERNAL_NETWORK_NAMES = frozenset({"local-network", "bridge", "host"})


# ---------------------------------------------------------------------------
# Fake, in-memory ContainerInspector (NO real Docker daemon)
# ---------------------------------------------------------------------------


@dataclass
class FakeInspector:
    """In-memory :class:`ContainerInspector` serving fabricated configs.

    Read-only, like the real adapter: it just looks up dictionaries populated
    from generated data. Unknown containers/networks raise the same lookup
    errors the real SDK adapter raises, so the fail-closed (non-verifiable)
    paths are exercised exactly as in production.
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
# Generated model + strategies
# ---------------------------------------------------------------------------

_INTERNAL_NETWORK_NAMES = tuple(INTERNAL_NETWORK_SUBNETS.keys())

_ACCEPTABLE_DNS = ("127.0.0.1", "192.168.10.10", "192.168.20.20", "192.168.30.30")
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
    """internal=True but with a subnet OUTSIDE the known internal ranges."""
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


_custom_networks = st.one_of(
    _internal_lab_networks(),
    _bad_internal_flagged_networks(),
    _non_internal_networks(),
)


@dataclass(frozen=True)
class GenContainer:
    """A generated target container and how the fake inspector should treat it.

    Attributes:
        name: Container name/id.
        custom_networks: Attachments resolvable via the inspector's network map.
        bridge_names: Attachments to always-external names
            (local-network/bridge/host), NOT registered as networks.
        dns: DNS servers configured on the container.
        missing_container: When True the inspector does NOT register the
            container, so resolving it raises ``ContainerNotFound`` — a
            non-verifiable target (fail-closed).
        missing_network: When True at least one custom network is left
            unregistered, so resolving it raises ``NetworkNotFound`` — a
            non-verifiable attachment (fail-closed).
    """

    name: str
    custom_networks: tuple[GenNetwork, ...]
    bridge_names: tuple[str, ...]
    dns: tuple[str, ...]
    missing_container: bool
    missing_network: bool


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
        # Rarely drop the container / a network to exercise the non-verifiable
        # fail-closed paths without overwhelming the "verifiable" cases.
        missing_container=st.booleans(),
        missing_network=st.booleans(),
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


def _registered_network_names(containers: list["GenContainer"]) -> set[str]:
    """Network names that WILL be resolvable in the inspector (global view).

    A network name is registered by any container that owns it and is NOT
    skipping it. Mirrors Docker reality: a network name is a GLOBAL unique key,
    so an attachment is resolvable iff *some* source registered that name. A
    container flagged ``missing_network`` skips only the FIRST DISTINCT custom
    network name it owns; the same name registered by another container (or by a
    later distinct-name position) is still globally resolvable.
    """
    registered: set[str] = set()
    for c in containers:
        skipped = _skipped_network_name(c)
        for net in c.custom_networks:
            if net.name == skipped:
                continue
            registered.add(net.name)
    return registered


def _skipped_network_name(container: "GenContainer") -> str | None:
    """The single network name this container does NOT register, if any.

    When ``missing_network`` is set and the container has custom networks, it
    skips registering its FIRST DISTINCT custom network name (so that specific
    name is non-verifiable *unless another source registers it*). Returns
    ``None`` when nothing is skipped.
    """
    if not (container.missing_network and container.custom_networks):
        return None
    return container.custom_networks[0].name


def _expected_isolated(
    container: GenContainer,
    registered_names: set[str] | None = None,
) -> bool:
    """Expected per-container verdict computed directly from the model.

    Mirrors the Requirement 6.1 rule WITHOUT calling the implementation. A
    container is isolated iff it is verifiable and attached only to internal
    networks (with internal subnets when declared) with no external DNS. Any
    non-verifiable condition (missing container, or an attached network name that
    NO source registered) is NOT isolated (fail-closed).

    ``registered_names`` is the GLOBAL set of resolvable network names (from
    :func:`_registered_network_names`). When ``None`` (single-container helper
    use) it is derived from this container alone, honouring its own skip.
    """
    # Non-verifiable: the inspector cannot resolve the container at all.
    if container.missing_container:
        return False

    if registered_names is None:
        registered_names = _registered_network_names([container])

    # Production reads the container's attached network NAMES (dict keys →
    # deduplicated). Dedupe here the same way, preserving first-seen order.
    attached_names: list[str] = []
    for name in list(container.bridge_names) + [n.name for n in container.custom_networks]:
        if name not in attached_names:
            attached_names.append(name)

    # Rule 1: attached to at least one network.
    if not attached_names:
        return False

    # Rule 2a: no known external bridge name.
    if any(name in KNOWN_EXTERNAL_NETWORK_NAMES for name in attached_names):
        return False

    # Map custom-network name -> its definition (first occurrence wins, matching
    # how the inspector registers a single NetworkInfo per unique name).
    net_by_name: dict[str, GenNetwork] = {}
    for net in container.custom_networks:
        net_by_name.setdefault(net.name, net)

    for name in attached_names:
        net = net_by_name.get(name)
        if net is None:
            # A bridge name already handled above; anything else is unexpected.
            continue
        # Non-verifiable: this network name is not registered by ANY source.
        if name not in registered_names:
            return False
        # Rule 2b: internal, and (if subnets declared) an internal subnet.
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
    """Populate a FakeInspector consistent with the global registration view.

    Containers are registered unless flagged ``missing_container``. Networks are
    registered from the GLOBAL set computed by :func:`_registered_network_names`,
    so a name skipped by one container but owned by another stays resolvable —
    exactly as Docker treats a network name as a global unique key.
    """
    inspector = FakeInspector()
    registered_names = _registered_network_names(containers)
    # One NetworkInfo per unique registered name (first occurrence wins).
    net_defs: dict[str, GenNetwork] = {}
    for c in containers:
        for net in c.custom_networks:
            net_defs.setdefault(net.name, net)

    for c in containers:
        if not c.missing_container:
            # Attached names as the inspector/Docker would expose them (dict keys
            # → deduplicated, preserving first-seen order).
            attached: list[str] = []
            for name in tuple(c.bridge_names) + tuple(n.name for n in c.custom_networks):
                if name not in attached:
                    attached.append(name)
            inspector.containers[c.name] = ContainerNetworkInfo(
                name=c.name, network_names=tuple(attached), dns=c.dns
            )

    for name in registered_names:
        net = net_defs[name]
        inspector.networks[name] = NetworkInfo(
            name=name, internal=net.internal, subnets=net.subnets
        )
    return inspector


# ---------------------------------------------------------------------------
# Property 12 — the biconditional + fail-closed empty/unknown
# ---------------------------------------------------------------------------


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(containers=_unique_containers())
def test_property12_isolated_iff_all_targets_satisfy_rule(
    containers: list[GenContainer],
) -> None:
    """``inspect_isolation`` passes IFF every target satisfies the rule.

    # Feature: stage6-execution-redesign, Property 12: Isolamento é "isolado"
    # sse todos os alvos satisfazem a regra
    **Validates: Requirements 6.1**
    """
    inspector = _build_inspector(containers)
    names = [c.name for c in containers]

    result = inspect_isolation(names, inspector=inspector)

    registered_names = _registered_network_names(containers)
    expected_isolated = {
        c.name: _expected_isolated(c, registered_names) for c in containers
    }
    expected_all = all(expected_isolated.values())

    # The biconditional: aggregate is "isolado" iff ALL targets satisfy the rule.
    assert result.passed is expected_all

    # Per-container verdicts agree with the oracle, in the given order.
    assert [v.container for v in result.verdicts] == names
    for verdict in result.verdicts:
        assert verdict.isolated is expected_isolated[verdict.container]
        if not verdict.isolated:
            # A failing (incl. non-verifiable) target must be identified.
            assert verdict.reasons != ()

    # If any single target is not isolated, the aggregate must NOT be isolated.
    if not expected_all:
        assert result.passed is False
        offending = {v.container for v in result.offending}
        assert offending == {n for n, ok in expected_isolated.items() if not ok}
        assert offending  # at least one offender identified


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(
    bad_index=st.integers(min_value=0),
    containers=st.lists(
        _container_strategy(), min_size=1, max_size=4, unique_by=lambda c: c.name
    ),
)
def test_property12_single_bad_target_breaks_the_aggregate(
    bad_index: int, containers: list[GenContainer]
) -> None:
    """One non-verifiable target is enough to make the aggregate NOT isolated.

    Builds an otherwise all-isolated set, then forces exactly one target to be
    non-verifiable (missing container) — the aggregate must fail-closed.

    # Feature: stage6-execution-redesign, Property 12: Isolamento é "isolado"
    # sse todos os alvos satisfazem a regra
    **Validates: Requirements 6.1**
    """
    # Rebuild each container as isolated-by-construction (one internal lab net,
    # no bridges, acceptable DNS, fully verifiable).
    base = [
        GenContainer(
            name=c.name,
            custom_networks=(
                GenNetwork(
                    name=_INTERNAL_NETWORK_NAMES[0],
                    internal=True,
                    subnets=(INTERNAL_NETWORK_SUBNETS[_INTERNAL_NETWORK_NAMES[0]],),
                ),
            ),
            bridge_names=(),
            dns=(),
            missing_container=False,
            missing_network=False,
        )
        for c in containers
    ]
    # Sanity: the base set is fully isolated.
    assert all(_expected_isolated(c) for c in base)

    inspector = _build_inspector(base)
    names = [c.name for c in base]
    assert inspect_isolation(names, inspector=inspector).passed is True

    # Now poison exactly one target: make it non-verifiable.
    victim = names[bad_index % len(names)]
    poisoned_inspector = _build_inspector(base)
    del poisoned_inspector.containers[victim]  # -> ContainerNotFound (fail-closed)

    result = inspect_isolation(names, inspector=poisoned_inspector)
    assert result.passed is False
    offending = {v.container for v in result.offending}
    assert victim in offending
    victim_verdict = next(v for v in result.verdicts if v.container == victim)
    assert victim_verdict.isolated is False
    assert victim_verdict.reasons != ()


def test_property12_empty_target_set_is_fail_closed() -> None:
    """An empty target set is NOT isolated (fail-closed, not vacuously true).

    # Feature: stage6-execution-redesign, Property 12: Isolamento é "isolado"
    # sse todos os alvos satisfazem a regra
    **Validates: Requirements 6.1**
    """
    inspector = FakeInspector()
    result = inspect_isolation([], inspector=inspector)
    assert result.passed is False
    assert result.verdicts == ()


def _isolation_inspector_protocol_sanity() -> None:
    """Static sanity: FakeInspector satisfies the ContainerInspector protocol."""
    _check: ContainerInspector = FakeInspector()  # noqa: F841
