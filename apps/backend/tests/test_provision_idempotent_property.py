"""Property test — re-execution idempotency of ``provision`` (task 8.2).

# Feature: stage5-target-provisioning, Property 6: Idempotência da re-execução.
# Re-executar ``provision`` sobre um ambiente JÁ provisionado converge ao mesmo
# estado final — sem duplicar recursos de rede e sem violar o invariante de
# contenção —, e ambas as execuções terminam ``ready``.

Validates: Requirements 7.1, 7.2, 7.3, 7.4

Esta é uma property test de *feature* (``stage5-target-provisioning`` é uma
feature, não um bugfix): a propriedade deve **valer** sobre a orquestração atual
de :meth:`~app.services.environment.provisioner.CurationEnvironmentProvisioner.provision`.

Nenhum Docker, rede pública ou Caldera real é tocado. Os seams novos
(``EgressWindow``, ``EgressProber``, ``TargetSetup``) e o ``ComposeExecutor``
(usado pelo ``reisolar`` via ``attach_network`` e pelo fail-safe via
``detach_network``) são **fakes** que compartilham um ÚNICO modelo mutável de
estado de rede — um mapa ``container -> conjunto de redes anexadas``. Os fakes
modelam a idempotência real do daemon:

* ``attach_network`` a uma rede já anexada é um **no-op** (não duplica, não erra);
* ``detach_network`` de uma rede já ausente é um **no-op** (não erra);
* ``exec`` de setup (online/offline) é idempotente (``useradd || true``,
  ``pgrep || start``, ``cat >`` sobrescreve) — aqui, sempre ``code == 0``.

O :class:`_NetworkState` guarda, além do estado atual por contêiner, um histórico
de **snapshots** tirado após CADA mutação (attach/detach), de ambas as execuções.
Como o invariante de contenção (nunca egresso + rede de ataque juntos num mesmo
instante) deve valer ao longo de TODA a re-execução, as asserções varrem o
histórico inteiro — não só o estado final.

O Hypothesis roteiriza quais hosts-alvo participam (``nginx``/``db``) e a ordem em
que aparecem; para cada caso:

1. roda ``provision`` UMA vez sobre os fakes (estado "fresco") e tira o estado
   final por contêiner;
2. roda ``provision`` DE NOVO sobre os MESMOS fakes (agora "já provisionados") e
   tira o estado final de novo;

e assere:

* **Idempotência de estado** — o conjunto de redes anexadas por contêiner após a
  2ª execução é **igual** ao de após a 1ª (converge; nada acumulado) — Req. 7.1.
* **Sem duplicação de recursos** — o estado é um *conjunto* por contêiner; o
  modelo nunca registra a mesma rede duas vezes, e o número de redes anexadas por
  host não cresce entre as execuções — Req. 7.2, 7.3.
* **Ambas prontas** — ``ready`` é ``True`` nas duas execuções (a re-execução
  reaproveita o pré-requisito já presente e reaplica o setup idempotente,
  concluindo de novo) — Req. 7.4.
* **Invariante preservado em AMBAS as execuções** — em nenhum snapshot do
  histórico (1ª + 2ª) um contêiner tem a rede de egresso e uma rede de ataque
  anexadas ao mesmo tempo.

_Requirements: 7.1, 7.2, 7.3, 7.4_
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.campaign_service import CaseService
from app.services.containment.isolation import (
    ContainerNetworkInfo,
    ContainerNotFound,
    NetworkInfo,
    NetworkNotFound,
)
from app.services.environment.compose_executor import CommandResult, LogLine
from app.services.environment.egress import (
    EGRESS_NETWORK,
    EgressVerdict,
    EgressWindowResult,
    attack_networks_for,
)
from app.services.environment.operationalization import (
    EnvironmentParameters,
    TargetHost,
)
from app.services.environment.provisioner import CurationEnvironmentProvisioner
from app.services.environment.target_setup import ClassifiedCommand, SetupPhase
from app.services.environment.topology_builder import HOST_CONTAINERS as _HOST_CONTAINERS


_SLUG = "shadowray"
_INTERNAL_NET = "caldera-kali-network"
_ATTACK_NETS = frozenset(attack_networks_for([]))


def _resolver(containers: list[str]) -> list[str]:
    return [_HOST_CONTAINERS.get(c, c) for c in containers]


# ---------------------------------------------------------------------------
# Shared, stateful network model — the single source of truth both the window
# fake and the compose fake mutate. It models the Docker daemon's idempotency:
# attaching an already-attached network is a no-op, detaching an already-absent
# one is a no-op, and the per-container state is a *set* (no duplicate resources).
# After EVERY mutation it snapshots the full state so the containment invariant
# can be checked across the whole history of BOTH provision runs.
# ---------------------------------------------------------------------------


@dataclass
class _NetworkState:
    """``container -> set of attached networks``, with a per-mutation history."""

    attached: dict[str, set[str]] = field(default_factory=dict)
    # One snapshot (deep copy) of ``attached`` taken after each attach/detach.
    history: list[dict[str, frozenset[str]]] = field(default_factory=list)

    def _snapshot(self) -> None:
        self.history.append(
            {c: frozenset(nets) for c, nets in self.attached.items()}
        )

    def attach(self, container: str, network: str) -> int:
        """Attach ``network`` to ``container`` — idempotent no-op if already there."""
        self.attached.setdefault(container, set()).add(network)
        self._snapshot()
        return 0

    def detach(self, container: str, network: str) -> int:
        """Detach ``network`` from ``container`` — idempotent no-op if absent."""
        self.attached.setdefault(container, set()).discard(network)
        self._snapshot()
        return 0

    def final_of(self, container: str) -> frozenset[str]:
        """The current attached-network set of ``container`` (empty if unknown)."""
        return frozenset(self.attached.get(container, set()))


@dataclass
class _StatefulEgressWindow:
    """Fake ``EgressWindow`` driving the shared :class:`_NetworkState`.

    ``open`` mirrors the production order: detach every attack network FIRST, then
    attach the egress network — never both at once. ``close`` detaches the egress
    network. Both are idempotent through the state model, so a re-run over an
    already-provisioned environment converges without error.
    """

    state: _NetworkState

    def open(
        self, container: str, attack_networks: list[str], *, on_log
    ) -> EgressWindowResult:
        on_log(
            LogLine(step="build_online", stream="info", text="open", substage="5.1")
        )
        for network in attack_networks:
            self.state.detach(container, network)
        self.state.attach(container, EGRESS_NETWORK)
        return EgressWindowResult(container=container, opened=True)

    def close(self, container: str, *, on_log) -> EgressWindowResult:
        on_log(
            LogLine(
                step="encerrar_egresso", stream="info", text="close", substage="5.1"
            )
        )
        self.state.detach(container, EGRESS_NETWORK)
        return EgressWindowResult(container=container, closed=True)


@dataclass
class _StatefulComposeExecutor:
    """Fake ``ComposeExecutor`` — ``up``/``down`` + idempotent net/exec surface.

    ``attach_network``/``detach_network`` drive the SAME shared state model (the
    ``reisolar`` step reattaches the attack networks; the fail-safe path would
    detach egress). ``exec_in`` models an idempotent setup command (always
    ``code == 0``). Exposing the extended surface makes ``_substages_supported``
    return True so the real 5.1/5.2 orchestration runs.
    """

    state: _NetworkState

    def up(self, compose_file: str, *, on_log) -> CommandResult:
        on_log(LogLine(step="subir_conteineres", stream="stdout", text="up"))
        return CommandResult(code=0, lines=[])

    def down(self, compose_file: str, *, on_log) -> CommandResult:
        on_log(LogLine(step="desprovisionar", stream="stdout", text="down"))
        return CommandResult(code=0, lines=[])

    def attach_network(self, container: str, network: str, *, on_log, ip=None) -> CommandResult:
        return CommandResult(code=self.state.attach(container, network), lines=[])

    def detach_network(self, container: str, network: str, *, on_log) -> CommandResult:
        return CommandResult(code=self.state.detach(container, network), lines=[])

    def exec_in(self, container: str, argv: list[str], *, on_log) -> CommandResult:
        return CommandResult(code=0, lines=[])


@dataclass
class _StatefulTargetSetup:
    """Fake ``TargetSetup`` — curated setup is idempotent, so always succeeds.

    Records a per-(container, phase) application count so the test can assert that
    a re-run reapplies the setup (idempotent reapplication) rather than skipping or
    duplicating resources — the OFFLINE/ONLINE commands themselves are modelled as
    idempotent (``useradd || true``, ``cat >`` overwrites, ``pgrep || start``).
    """

    applied: list[tuple[str, SetupPhase]] = field(default_factory=list)

    def apply(
        self,
        container: str,
        commands: list[ClassifiedCommand],
        phase: SetupPhase,
        *,
        on_log,
        best_effort: bool = False,
    ) -> CommandResult:
        self.applied.append((container, phase))
        return CommandResult(code=0, lines=[])


@dataclass
class _StatefulEgressProber:
    """Fake ``EgressProber`` — egress always positively verified as lost.

    Re-isolation (``reisolar``) reattaches the attack networks and detaches egress,
    so on both runs the probe conclusively reports egress lost (approves 5.2).
    """

    def probe(self, container: str, *, on_log) -> EgressVerdict:
        return EgressVerdict(container=container, egress_lost=True, conclusive=True)


@dataclass
class _FakeCaldera:
    """In-memory Caldera loader (no network); counts loads for idempotency checks."""

    calls: list[str] = field(default_factory=list)

    def load_abilities(self, abilities: list[Mapping[str, Any]]) -> list[Any]:
        self.calls.append("load_abilities")
        return list(abilities)

    def load_adversary(self, adversary: Mapping[str, Any]) -> Any:
        self.calls.append("load_adversary")
        return adversary


@dataclass
class _FakeInspector:
    """In-memory read-only container/network inspector (no Docker daemon).

    Every target is isolated (attached to an ``internal: true`` network), so the
    final isolation gate approves on BOTH runs — this inspector is read-only and
    independent of the mutable ``_NetworkState`` (the gate verifies the resolved
    containment topology, not the transient 5.1/5.2 attaches).
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


class _FakeStateInspector:
    """In-memory read-only state/health inspector (always running+healthy)."""

    def __init__(self, containers: list[str]) -> None:
        from app.services.environment.health import ContainerStateInfo

        self._states = {
            c: ContainerStateInfo(name=c, running=True, health="healthy")
            for c in containers
        }

    def get_container_state(self, container: str):
        from app.services.environment.health import ContainerStateNotFound

        try:
            return self._states[container]
        except KeyError as exc:
            raise ContainerStateNotFound(container) from exc


def _isolated_inspector(containers: list[str]) -> _FakeInspector:
    insp = _FakeInspector()
    insp.networks[_INTERNAL_NET] = NetworkInfo(
        name=_INTERNAL_NET, internal=True, subnets=()
    )
    for c in containers:
        insp.containers[c] = ContainerNetworkInfo(
            name=c, network_names=(_INTERNAL_NET,), dns=()
        )
    return insp


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_case(data_dir: Path, slug: str) -> None:
    abilities = [
        {
            "ability_id": f"{slug}-ab-1",
            "name": "first",
            "technique_id": "T1190",
            "executors": [{"name": "sh", "platform": "linux", "command": "echo 1"}],
        },
    ]
    _write_json(data_dir / "api" / f"{slug}_dag-ability.json", abilities)
    _write_json(
        data_dir / "api" / f"{slug}_dag-adversary.json",
        {
            "id": f"{slug}-adv",
            "name": "Seed Adversary",
            "description": "seed",
            "atomic_ordering": [f"{slug}-ab-1"],
        },
    )
    _write_json(
        data_dir / "dag" / f"{slug}_dag.json",
        {
            "campaign_name": "Seed",
            "structural_nodes": [
                {"node_id": "n0", "node_index": 0, "technique_id": "T1190"}
            ],
            "metadata": {"total_techniques": 1},
        },
    )


def _params(containers: list[str], *, label_salt: int = 0) -> EnvironmentParameters:
    hosts = [
        TargetHost(
            # ``label_salt`` only perturbs the cosmetic ``label`` string — it does
            # NOT affect container resolution, the attack networks or the setup, so
            # it widens Hypothesis' draw space without changing the semantics.
            label=f"h{i}-{label_salt}",
            container=c,
            # A mix of online/offline curated commands so both phases have work.
            vulnerabilities=["apt-get install -y nmap", "useradd attacker"],
        )
        for i, c in enumerate(containers)
    ]
    return EnvironmentParameters(campaign_id=_SLUG, hosts=hosts)


def _assert_invariant_over_history(state: _NetworkState) -> None:
    """No snapshot ever has egress AND an attack network on the same container."""
    for snapshot in state.history:
        for container, nets in snapshot.items():
            if EGRESS_NETWORK in nets:
                offenders = nets & _ATTACK_NETS
                assert not offenders, (
                    f"{container} tinha egresso e rede(s) de ataque juntas "
                    f"{sorted(offenders)} num snapshot: {sorted(nets)}"
                )


# ---------------------------------------------------------------------------
# Generators.
#
# The lab resolves only two logical target hosts (``nginx``/``db``), so the
# host-list domain is intentionally small (4 distinct unique lists). To keep a
# rich enough input space for Hypothesis to run many distinct examples, three
# dimensions vary INDEPENDENTLY and NONE of them changes the property's
# semantics:
#   * ``hosts`` — which targets participate and in which order (the real domain);
#   * ``reruns`` — how many times ``provision`` is re-executed over the SAME
#     fakes (>= 2): the property is that every re-run converges to the 1st run's
#     final state, so varying the count directly exercises idempotency depth;
#   * ``label_salt`` — a wide integer that only perturbs the cosmetic host
#     ``label`` string (never the resolved container, networks or setup), mirroring
#     the ``offender_mask`` precedent of the isolation-gate property to widen the
#     draw space without altering behavior.
# ---------------------------------------------------------------------------

_HOST_CHOICES = ["nginx", "db"]
_hosts_strategy = st.lists(
    st.sampled_from(_HOST_CHOICES), min_size=1, max_size=2, unique=True
)
# Number of provision runs over the same fakes (first + at least one re-run).
_reruns_strategy = st.integers(min_value=2, max_value=6)
# A cosmetic salt for the host labels — widens the example space only.
_label_salt_strategy = st.integers(min_value=0, max_value=2**16)


@settings(max_examples=100, deadline=None)
@given(hosts=_hosts_strategy, reruns=_reruns_strategy, label_salt=_label_salt_strategy)
def test_reexecution_is_idempotent_and_preserves_the_invariant(
    hosts: list[str], reruns: int, label_salt: int
) -> None:
    # Feature: stage5-target-provisioning, Property 6: Idempotência da re-execução.
    containers = _resolver(hosts)
    state = _NetworkState()
    window = _StatefulEgressWindow(state)
    compose = _StatefulComposeExecutor(state)
    setup = _StatefulTargetSetup()
    prober = _StatefulEgressProber()

    tmp = Path(tempfile.mkdtemp(prefix="stage5_idempotent_prop_"))
    try:
        _seed_case(tmp, _SLUG)
        prov = CurationEnvironmentProvisioner(
            inspector=_isolated_inspector(containers),
            case_service=CaseService(data_dir=tmp),
            caldera=_FakeCaldera(),
            compose_executor=compose,
            state_inspector=_FakeStateInspector(containers),
            egress_window=window,
            egress_prober=prober,
            target_setup=setup,
        )

        # Provision ``reruns`` times over the SAME (stateful) fakes: the 1st run is
        # a "fresh" environment, every subsequent run re-executes over an
        # "already provisioned" one. Capture each run's outcome, the per-container
        # final network state, and the history length right after the 1st run.
        outcomes = []
        states_per_run: list[dict[str, frozenset[str]]] = []
        history_after_first = 0
        for i in range(reruns):
            environment = prov.provision(_params(hosts, label_salt=label_salt))
            outcomes.append(environment)
            states_per_run.append({c: state.final_of(c) for c in containers})
            if i == 0:
                history_after_first = len(state.history)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    first_state = states_per_run[0]

    # Every run completes: each re-run reuses the already-present prerequisite and
    # reapplies the idempotent setup, converging to ready again (Req. 7.4).
    for i, environment in enumerate(outcomes):
        assert environment.ready is True, (i, environment.error_message)
        assert environment.failed_step is None

    # Idempotency of state: the attached-network set per container after EVERY
    # re-run equals the one after the 1st — nothing accumulates (Req. 7.1).
    for i, run_state in enumerate(states_per_run[1:], start=1):
        assert run_state == first_state, (
            f"a execução #{i + 1} não convergiu: {first_state} -> {run_state}"
        )

    # No duplicated resources: every target ends re-isolated onto exactly the
    # attack networks IT BELONGS TO (its curated compose topology — e.g. nginx on
    # kali-nginx + nginx-db, NOT caldera-kali), egress swept, identical on every
    # run (Req. 7.2, 7.3). The per-container state is a *set*, so a network is
    # never recorded twice. We derive the expected set from the same source of
    # truth the orchestration uses, so the assertion tracks the real topology
    # rather than a flat "all three networks".
    from app.services.environment.egress import attack_networks_with_ip_for

    for container in containers:
        expected = {net for net, _ip in attack_networks_with_ip_for(container)}
        assert first_state[container] == expected, (
            f"{container} não ficou re-isolado nas suas redes de ataque: "
            f"esperado {sorted(expected)}, obtido {sorted(first_state[container])}"
        )

    # The re-runs genuinely re-executed the mutating orchestration (they produced
    # further snapshots), so idempotency is proven over actual re-work.
    assert len(state.history) > history_after_first, (
        "as re-execuções não exerceram a orquestração mutável (sem novos snapshots)"
    )

    # The setup was reapplied on every run (idempotent reapplication, Req. 7.1):
    # each phase (ONLINE + OFFLINE) ran once per host per run.
    expected_per_run = 2 * len(containers)
    assert len(setup.applied) == reruns * expected_per_run

    # Containment invariant across the WHOLE history of ALL runs: no container is
    # ever attached to egress and an attack network at the same instant.
    _assert_invariant_over_history(state)
