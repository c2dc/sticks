"""Property-based test: Stage 5 ↔ Stage 6 isolation-verdict equivalence.

# Feature: stage6-execution-redesign, Property 13: Equivalência da verificação
# de isolamento entre Estágios 5 e 6. Para qualquer estado de ambiente, aplicar
# a verificação de isolamento sobre o mesmo conjunto de contêineres-alvo com a
# mesma regra produz o MESMO veredito ``isolado`` no Estágio 5 e no Estágio 6.

**Validates: Requirements 6.6**

Requirement 6.6 mandates that the Stage 6 isolation gate use the **same target
container set and the same rule** as the Stage 5 environment check, so that for
the same environment state both produce an *equivalent* ``isolado`` verdict.

The design satisfies this by routing BOTH stages through the single shared
function :func:`app.services.containment.inspect_isolation`:

* **Stage 5** — :class:`EnvironmentStageService._check_isolation` calls
  ``inspect_isolation(containers, self._inspector)`` (and the curation
  provisioner / topology builder do the same).
* **Stage 6** — :class:`OperationRunner` defaults its injectable
  ``isolation_verifier`` to :func:`inspect_isolation` and invokes it as
  ``self._verify_isolation(containers, self._inspector)`` in its pre-flight
  gate (and in the periodic re-check during execution).

This test first *confirms the routing* (both stages resolve to the same
``inspect_isolation`` callable), then drives arbitrary environment states
through a FAKE :class:`ContainerInspector` (no real Docker daemon) and asserts
that the aggregate ``passed`` verdict obtained the Stage-5 way equals the one
obtained the Stage-6 way for the same inspector + container set.

The generators / fake inspector mirror the existing isolation property test
(``test_isolation_property.py``), keeping the input space identical to the one
the shared rule is already exercised over.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.containment.isolation import (
    ContainerNetworkInfo,
    ContainerNotFound,
    IsolationResult,
    NetworkInfo,
    NetworkNotFound,
    inspect_isolation,
)
from app.services.containment.subnets import INTERNAL_NETWORK_SUBNETS

# The two production entry points under test. Stage 6 defaults its verifier to
# ``inspect_isolation`` on the runner; Stage 5 performs the environment isolation
# check in the curation provisioner (``_check_isolation`` / topology), which
# imports ``inspect_isolation`` into its own module. Importing both modules lets
# us assert the routing identity below.
from app.services.environment import provisioner as stage5_module
from app.services.operation.runner import OperationRunner

KNOWN_EXTERNAL_NETWORK_NAMES = frozenset({"local-network", "bridge", "host"})


# ---------------------------------------------------------------------------
# Fake, in-memory ContainerInspector (NO real Docker daemon)
# ---------------------------------------------------------------------------


@dataclass
class FakeInspector:
    """In-memory :class:`ContainerInspector` serving fabricated configs.

    Read-only lookups into dictionaries populated from generated data; unknown
    containers/networks raise the same lookup errors the real SDK adapter does,
    so fail-closed paths are exercised identically for both stages.
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
# Generated-network model + strategies (mirrors test_isolation_property.py)
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
    """A generated container: its attachments (by name) and DNS servers."""

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


def _build_inspector(containers: list[GenContainer]) -> FakeInspector:
    """Populate a FakeInspector from generated containers (bridges unregistered)."""
    inspector = FakeInspector()
    for c in containers:
        network_names = tuple(c.bridge_names) + tuple(
            n.name for n in c.custom_networks
        )
        inspector.containers[c.name] = ContainerNetworkInfo(
            name=c.name, network_names=network_names, dns=c.dns
        )
        for net in c.custom_networks:
            inspector.networks[net.name] = NetworkInfo(
                name=net.name, internal=net.internal, subnets=net.subnets
            )
    return inspector


# ---------------------------------------------------------------------------
# Stage-5 and Stage-6 isolation-check adapters (both route to inspect_isolation)
# ---------------------------------------------------------------------------


def _stage5_isolation_check(
    containers: list[str], inspector: FakeInspector
) -> IsolationResult:
    """Apply isolation the way Stage 5 does.

    Stage 5's :class:`CurationEnvironmentProvisioner` (``_check_isolation`` and
    the ``topology`` build) calls the module-level ``inspect_isolation`` imported
    into the provisioner module. Using that very symbol keeps the test honest
    about the routing rather than re-importing a private copy.
    """
    return stage5_module.inspect_isolation(containers, inspector)


def _stage6_isolation_check(
    containers: list[str], inspector: FakeInspector
) -> IsolationResult:
    """Apply isolation the way Stage 6 does.

    Stage 6's :class:`OperationRunner` defaults its injectable
    ``isolation_verifier`` to ``inspect_isolation`` and invokes it as
    ``verifier(containers, inspector)``. We read that default off the class
    signature so the test tracks the production wiring, not a hardcoded copy.
    """
    verifier = OperationRunner.__init__.__kwdefaults__["isolation_verifier"]
    return verifier(containers, inspector)


# ---------------------------------------------------------------------------
# Routing confirmation (both stages share the single inspect_isolation seam)
# ---------------------------------------------------------------------------


def test_both_stages_route_through_inspect_isolation() -> None:
    """Confirm Stage 5 and Stage 6 both use the shared ``inspect_isolation``.

    **Validates: Requirements 6.6**

    This is the structural half of Property 13: equivalence is only guaranteed
    because there is a *single* implementation of the rule. We assert that the
    symbol Stage 5 uses and the default verifier Stage 6 uses are the exact same
    callable object as :func:`app.services.containment.inspect_isolation`.
    """
    stage6_default = OperationRunner.__init__.__kwdefaults__["isolation_verifier"]
    assert stage5_module.inspect_isolation is inspect_isolation
    assert stage6_default is inspect_isolation


# ---------------------------------------------------------------------------
# Property 13 — equivalence of the aggregate verdict across stages
# ---------------------------------------------------------------------------


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(containers=_unique_containers())
def test_property13_stage5_stage6_isolation_verdict_equivalent(
    containers: list[GenContainer],
) -> None:
    """Stage 5 and Stage 6 agree on the ``isolado`` verdict for any state.

    # Feature: stage6-execution-redesign, Property 13: Equivalência da
    # verificação de isolamento entre Estágios 5 e 6.
    **Validates: Requirements 6.6**

    For an arbitrary environment state (fabricated by the fake inspector) and
    the same target-container set, the aggregate ``passed`` verdict obtained the
    Stage-5 way equals the one obtained the Stage-6 way. The per-container
    verdicts (container, isolated, reasons, external networks/DNS) must match
    too, since both run the identical rule over identical inputs.
    """
    inspector = _build_inspector(containers)
    names = [c.name for c in containers]

    stage5 = _stage5_isolation_check(names, inspector)
    stage6 = _stage6_isolation_check(names, inspector)

    # The headline equivalence Requirement 6.6 demands: same "isolado" verdict.
    assert stage5.passed == stage6.passed

    # Equivalence is structural, not coincidental: the full per-container
    # verdicts match in order (same rule, same containers, same inspector).
    assert stage5.verdicts == stage6.verdicts
    assert stage5.offending == stage6.offending
    assert stage5.summary() == stage6.summary()


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(
    base=_unique_containers(),
    perm_seed=st.integers(min_value=0, max_value=10_000),
)
def test_property13_equivalence_independent_of_check_order(
    base: list[GenContainer],
    perm_seed: int,
) -> None:
    """Equivalence holds regardless of which stage runs first / container order.

    # Feature: stage6-execution-redesign, Property 13: Equivalência da
    # verificação de isolamento entre Estágios 5 e 6.
    **Validates: Requirements 6.6**

    The two stages verify the same *set* of target containers; the aggregate
    ``passed`` verdict is order-independent (every target must be isolated), so
    running Stage 6 first, or over a rotated container list, cannot diverge from
    Stage 5 over the canonical order.
    """
    inspector = _build_inspector(base)
    names = [c.name for c in base]

    # Rotate the container list to model an alternate (but equivalent) ordering.
    if names:
        rot = perm_seed % len(names)
        rotated = names[rot:] + names[:rot]
    else:  # pragma: no cover - generator guarantees min_size>=1
        rotated = names

    # Stage 6 first, over the rotated order; Stage 5 after, over canonical order.
    stage6 = _stage6_isolation_check(rotated, inspector)
    stage5 = _stage5_isolation_check(names, inspector)

    # Aggregate verdict is invariant to order and to which stage ran first.
    assert stage5.passed == stage6.passed
