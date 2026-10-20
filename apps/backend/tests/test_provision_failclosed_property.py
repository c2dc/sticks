"""Property test — a failure never leaves a target with egress (task 8.1).

# Feature: stage5-target-provisioning, Property 4: Falha nunca deixa um alvo com
# egresso. Independentemente do ponto em que a orquestração 5.1/5.2 de
# ``provision`` falhe, o Estágio 5 termina NÃO concluído (``ready is False``) com
# o ``failed_step`` correto, e o estado final de CADA host-alvo é seguro — isolado
# (sem a rede de egresso) ou desprovisionado —, NUNCA deixando um alvo anexado à
# rede de egresso. O invariante de contenção (nunca egresso + rede de ataque
# juntos) vale ao longo de TODA a história de mutações.

Validates: Requirements 8.3, 8.4

Esta é uma property test de *feature* (``stage5-target-provisioning`` é uma
feature, não um bugfix): a propriedade deve **valer** sobre a orquestração atual
de :meth:`~app.services.environment.provisioner.CurationEnvironmentProvisioner.provision`,
que chama ``_fail_safe``/``_fail_safe_all`` em TODO caminho de erro para encerrar
a janela de egresso — nenhum caminho de erro retorna com um alvo ainda anexado à
``EGRESS_NETWORK``.

Nenhum Docker, rede pública ou Caldera real é tocado. Como na property de
idempotência (``tests/test_provision_idempotent_property.py``), os seams
(``EgressWindow``, ``EgressProber``, ``TargetSetup``) e o ``ComposeExecutor`` são
**fakes STATEFUL** que compartilham um ÚNICO modelo mutável de estado de rede — um
mapa ``container -> conjunto de redes anexadas``. Isso permite asserir o estado de
rede FINAL de cada host.

Pontos de injeção de falha (um por exemplo do Hypothesis, aplicado a um host
escolhido):

* ``open_fail`` — ``EgressWindow.open`` reprova (``opened=False``) → ``build_online``;
* ``online_apply_fail`` — ``TargetSetup.apply(ONLINE)`` com ``code != 0`` →
  ``build_online`` (a janela já foi fechada acima + fail-safe);
* ``close_fail`` — ``EgressWindow.close`` reprova (``closed=False``) →
  ``encerrar_egresso``;
* ``reisolar_fail`` — ``ComposeExecutor.attach_network`` (reisolar) com
  ``code != 0`` → ``reisolar``;
* ``probe_present`` — ``EgressProber`` reporta egresso presente → ``verificar_egresso``;
* ``probe_inconclusive`` — ``EgressProber`` reporta sondagem inconclusiva (tratada
  como egresso presente, fail-closed) → ``verificar_egresso``;
* ``offline_apply_fail`` — ``TargetSetup.apply(OFFLINE)`` com ``code != 0`` →
  ``setup_offline``;
* ``isolation_reproves`` — o portão final de isolamento reprova → ``verificar_isolamento``;
* ``unexpected_exception`` — uma exceção inesperada escapa dentro da 5.1 (o
  ``except`` chama ``_fail_safe_all``) → ``build_online``.

IMPORTANTE — por que o estado final é observável: a produção ``_fail_safe`` fecha
a janela chamando ``self._get_egress_window().close(container, ...)``. Logo o fake
``EgressWindow.close`` DETACHA ``EGRESS_NETWORK`` do estado compartilhado, de modo
que o efeito do fail-safe é observável no estado final de rede. Quando a falha
ocorre ANTES de qualquer janela abrir para um host (ou DEPOIS que todas foram
fechadas), esse host nunca teve egresso — igualmente seguro.

Para cada exemplo o teste assere, após ``provision`` retornar:

* **Estágio 5 não concluído** — ``environment.ready is False`` com o
  ``failed_step`` esperado para o ponto de falha injetado (Req. 8.1 em apoio);
* **Nenhum alvo com egresso** — o estado final de rede de TODO host NÃO contém
  ``EGRESS_NETWORK`` (Req. 8.3): cada host é isolado (apenas redes de ataque, ou
  vazio) ou desprovisionado, nunca deixado com egresso;
* **Estado final seguro** — cada host está num dos dois estados seguros: isolado
  (sem egresso) ou desprovisionado (Req. 8.4);
* **Invariante de contenção em TODA a história** — em nenhum snapshot um contêiner
  teve a rede de egresso e uma rede de ataque anexadas ao mesmo tempo (Req. 8.4).

_Requirements: 8.3, 8.4_
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


# The failure-injection points, each mapped to the ``failed_step`` the current
# orchestration must report. The keys are the ``failpoint`` the generator draws.
_FAILPOINTS: dict[str, str] = {
    "open_fail": "build_online",
    "online_apply_fail": "build_online",
    "close_fail": "encerrar_egresso",
    "reisolar_fail": "reisolar",
    "probe_present": "verificar_egresso",
    "probe_inconclusive": "verificar_egresso",
    "offline_apply_fail": "setup_offline",
    "isolation_reproves": "verificar_isolamento",
    "unexpected_exception": "build_online",
}


def _resolver(containers: list[str]) -> list[str]:
    return [_HOST_CONTAINERS.get(c, c) for c in containers]


# ---------------------------------------------------------------------------
# Shared, stateful network model — the single source of truth both the window
# fake and the compose fake mutate (mirrors the idempotency property's model). It
# snapshots the full state after EVERY mutation so the containment invariant can
# be checked across the whole history, and exposes the per-container FINAL state
# so the test can assert no target is left attached to EGRESS_NETWORK.
# ---------------------------------------------------------------------------


@dataclass
class _NetworkState:
    """``container -> set of attached networks``, with a per-mutation history."""

    attached: dict[str, set[str]] = field(default_factory=dict)
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
    network — this is exactly what the production ``_fail_safe`` invokes
    (``EgressWindow.close``), so a fail-safe on ANY error path clears egress from
    the shared state and the effect is observable in the final state.

    When ``fail_host`` is set it injects a failure for that single host:
    ``open`` returns ``opened=False`` (``open_fail``) or ``close`` returns
    ``closed=False`` (``close_fail``). A failed ``close`` STILL detaches egress
    from the model (the real daemon close is attempted; only the *reported*
    success is false) — the subsequent ``_fail_safe`` close is an idempotent no-op,
    and either way no egress remains.
    """

    state: _NetworkState
    failpoint: str = ""
    fail_host: str = ""

    def open(
        self, container: str, attack_networks: list[str], *, on_log
    ) -> EgressWindowResult:
        if self.failpoint == "open_fail" and container == self.fail_host:
            # Reproves BEFORE any attach — the host never gets egress; the
            # orchestration still runs the fail-safe close (idempotent no-op here).
            return EgressWindowResult(
                container=container,
                opened=False,
                reason=f"falha simulada ao abrir egresso de {container}",
            )
        for network in attack_networks:
            self.state.detach(container, network)
        self.state.attach(container, EGRESS_NETWORK)
        return EgressWindowResult(container=container, opened=True)

    def close(self, container: str, *, on_log) -> EgressWindowResult:
        # ALWAYS detach egress from the shared state — this is the daemon-level
        # effect the production close (and the fail-safe that calls it) relies on
        # to leave no target with egress. The injected ``close_fail`` only reports
        # a failed close; the egress is swept regardless.
        self.state.detach(container, EGRESS_NETWORK)
        if self.failpoint == "close_fail" and container == self.fail_host:
            return EgressWindowResult(
                container=container,
                closed=False,
                reason=f"falha simulada ao encerrar egresso de {container}",
            )
        return EgressWindowResult(container=container, closed=True)


@dataclass
class _StatefulComposeExecutor:
    """Fake ``ComposeExecutor`` — ``up``/``down`` + idempotent net/exec surface.

    ``attach_network``/``detach_network`` drive the SAME shared state model (the
    ``reisolar`` step reattaches the attack networks; the fail-safe path detaches
    egress through the window). Exposing the extended surface makes
    ``_substages_supported`` return True so the real 5.1/5.2 orchestration runs.

    When ``failpoint == "reisolar_fail"`` and the reattach targets ``fail_host``,
    ``attach_network`` returns ``code != 0`` WITHOUT mutating the state, so the
    reisolar step fails and triggers ``_fail_safe_all`` (which closes every
    window). ``down`` detaches EGRESS_NETWORK from every container it knows, so a
    deprovision path (should the orchestration choose it) is also egress-safe.
    """

    state: _NetworkState
    failpoint: str = ""
    fail_host: str = ""

    def up(self, compose_file: str, *, on_log) -> CommandResult:
        on_log(LogLine(step="subir_conteineres", stream="stdout", text="up"))
        return CommandResult(code=0, lines=[])

    def down(self, compose_file: str, *, on_log) -> CommandResult:
        for container in list(self.state.attached):
            self.state.detach(container, EGRESS_NETWORK)
        on_log(LogLine(step="desprovisionar", stream="stdout", text="down"))
        return CommandResult(code=0, lines=[])

    def attach_network(self, container: str, network: str, *, on_log, ip=None) -> CommandResult:
        if self.failpoint == "reisolar_fail" and container == self.fail_host:
            # Reattach reproves — do NOT mutate the state (the attach failed).
            return CommandResult(code=1, lines=[])
        return CommandResult(code=self.state.attach(container, network), lines=[])

    def detach_network(self, container: str, network: str, *, on_log) -> CommandResult:
        return CommandResult(code=self.state.detach(container, network), lines=[])

    def exec_in(self, container: str, argv: list[str], *, on_log) -> CommandResult:
        return CommandResult(code=0, lines=[])


@dataclass
class _StatefulTargetSetup:
    """Fake ``TargetSetup`` — curated setup, with a per-phase failure injection.

    Normally returns ``code == 0`` (idempotent setup). When ``failpoint`` selects a
    phase for ``fail_host`` it injects a failure:
    * ``online_apply_fail`` — ONLINE phase of ``fail_host`` returns ``code != 0``;
    * ``offline_apply_fail`` — OFFLINE phase of ``fail_host`` returns ``code != 0``;
    * ``unexpected_exception`` — ONLINE phase of ``fail_host`` RAISES, so the
      exception escapes the per-host 5.1 loop and the orchestration's ``except``
      runs ``_fail_safe_all`` (closing every window).
    """

    failpoint: str = ""
    fail_host: str = ""

    def apply(
        self,
        container: str,
        commands: list[ClassifiedCommand],
        phase: SetupPhase,
        *,
        on_log,
        best_effort: bool = False,
    ) -> CommandResult:
        if container == self.fail_host:
            if self.failpoint == "online_apply_fail" and phase is SetupPhase.ONLINE:
                return CommandResult(code=1, lines=[])
            if self.failpoint == "offline_apply_fail" and phase is SetupPhase.OFFLINE:
                return CommandResult(code=1, lines=[])
            if self.failpoint == "unexpected_exception" and phase is SetupPhase.ONLINE:
                raise RuntimeError(f"falha inesperada simulada em {container}")
        return CommandResult(code=0, lines=[])


@dataclass
class _StatefulEgressProber:
    """Fake ``EgressProber`` — egress loss positively verified, with injection.

    Normally reports egress conclusively lost (approves 5.2). When ``failpoint``
    targets ``fail_host``:
    * ``probe_present`` — reports egress STILL present (``egress_lost=False``);
    * ``probe_inconclusive`` — reports an inconclusive probe (``conclusive=False``),
      which ``egress_lost`` treats as egress present (fail-closed).
    Either reproves ``verificar_egresso`` without applying any offline setup.
    """

    failpoint: str = ""
    fail_host: str = ""

    def probe(self, container: str, *, on_log) -> EgressVerdict:
        if container == self.fail_host:
            if self.failpoint == "probe_present":
                return EgressVerdict(
                    container=container, egress_lost=False, conclusive=True,
                    reason="egresso ainda presente (simulado)",
                )
            if self.failpoint == "probe_inconclusive":
                return EgressVerdict(
                    container=container, egress_lost=True, conclusive=False,
                    reason="sondagem inconclusiva (simulado)",
                )
        return EgressVerdict(container=container, egress_lost=True, conclusive=True)


@dataclass
class _FakeCaldera:
    """In-memory Caldera loader (no network); never fails for these paths."""

    def load_abilities(self, abilities: list[Mapping[str, Any]]) -> list[Any]:
        return list(abilities)

    def load_adversary(self, adversary: Mapping[str, Any]) -> Any:
        return adversary


@dataclass
class _FakeInspector:
    """In-memory read-only container/network inspector (no Docker daemon).

    Independent of the mutable ``_NetworkState`` (the final isolation gate verifies
    the resolved containment topology, not the transient 5.1/5.2 attaches). When a
    container is marked ``non_isolated`` it is attached to an external bridge, so
    the gate reproves ``verificar_isolamento``.
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


_EXTERNAL_BRIDGE = "sticks-external-bridge"


def _inspector(containers: list[str], *, non_isolated: str = "") -> _FakeInspector:
    """Build a read-only inspector; ``non_isolated`` makes one host reprove the gate."""
    insp = _FakeInspector()
    insp.networks[_INTERNAL_NET] = NetworkInfo(
        name=_INTERNAL_NET, internal=True, subnets=()
    )
    insp.networks[_EXTERNAL_BRIDGE] = NetworkInfo(
        name=_EXTERNAL_BRIDGE, internal=False, subnets=()
    )
    for c in containers:
        if c == non_isolated:
            insp.containers[c] = ContainerNetworkInfo(
                name=c, network_names=(_INTERNAL_NET, _EXTERNAL_BRIDGE), dns=()
            )
        else:
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
# Three dimensions vary INDEPENDENTLY:
#   * ``hosts`` — which targets participate and in which order (the real domain;
#     the lab resolves only ``nginx``/``db``);
#   * ``failpoint`` — WHERE in the 5.1/5.2 orchestration the injected failure
#     fires (the property's core: every point must stay egress-safe);
#   * ``label_salt`` — a wide integer perturbing only the cosmetic host ``label``,
#     widening the draw space without changing the semantics.
# The failing host is drawn from the participating hosts via ``st.data()`` so it
# is always a real target of the run.
# ---------------------------------------------------------------------------

_HOST_CHOICES = ["nginx", "db"]
_hosts_strategy = st.lists(
    st.sampled_from(_HOST_CHOICES), min_size=1, max_size=2, unique=True
)
_failpoint_strategy = st.sampled_from(sorted(_FAILPOINTS))
_label_salt_strategy = st.integers(min_value=0, max_value=2**16)


@settings(max_examples=100, deadline=None)
@given(
    hosts=_hosts_strategy,
    failpoint=_failpoint_strategy,
    label_salt=_label_salt_strategy,
    data=st.data(),
)
def test_failure_never_leaves_a_target_with_egress(
    hosts: list[str], failpoint: str, label_salt: int, data: st.DataObject
) -> None:
    # Feature: stage5-target-provisioning, Property 4: Falha nunca deixa um alvo com egresso.
    containers = _resolver(hosts)
    # The failing host is one of the participating targets.
    fail_host = data.draw(st.sampled_from(containers), label="fail_host")

    state = _NetworkState()
    window = _StatefulEgressWindow(state, failpoint=failpoint, fail_host=fail_host)
    compose = _StatefulComposeExecutor(state, failpoint=failpoint, fail_host=fail_host)
    setup = _StatefulTargetSetup(failpoint=failpoint, fail_host=fail_host)
    prober = _StatefulEgressProber(failpoint=failpoint, fail_host=fail_host)
    non_isolated = fail_host if failpoint == "isolation_reproves" else ""

    tmp = Path(tempfile.mkdtemp(prefix="stage5_failclosed_prop_"))
    try:
        _seed_case(tmp, _SLUG)
        prov = CurationEnvironmentProvisioner(
            inspector=_inspector(containers, non_isolated=non_isolated),
            case_service=CaseService(data_dir=tmp),
            caldera=_FakeCaldera(),
            compose_executor=compose,
            state_inspector=_FakeStateInspector(containers),
            egress_window=window,
            egress_prober=prober,
            target_setup=setup,
        )
        environment = prov.provision(_params(hosts, label_salt=label_salt))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Stage 5 is NOT complete, with the ``failed_step`` matching the injected point.
    assert environment.ready is False, (
        f"ponto de falha {failpoint!r} não deixou o Estágio 5 incompleto"
    )
    assert environment.failed_step == _FAILPOINTS[failpoint], (
        f"ponto de falha {failpoint!r}: failed_step={environment.failed_step!r} "
        f"(esperado {_FAILPOINTS[failpoint]!r})"
    )
    assert environment.error_message, "a causa da falha deve ser reportada"

    # Req. 8.3 / 8.4: NO host is left with egress — every host ends in a safe final
    # state: isolated (no egress; only attack/internal networks, or empty) or
    # deprovisioned. The egress network is NEVER among a host's final networks.
    for container in containers:
        final = state.final_of(container)
        assert EGRESS_NETWORK not in final, (
            f"{container} ficou com egresso após falha em {failpoint!r}: "
            f"{sorted(final)}"
        )
        # Safe-state shape: the final set is a subset of the attack networks (an
        # isolated/empty host) — egress swept, no stray external network left.
        assert final <= _ATTACK_NETS, (
            f"{container} terminou num estado inseguro após {failpoint!r}: "
            f"{sorted(final)}"
        )

    # The containment invariant held across the WHOLE mutation history: never
    # egress and an attack network on the same container at the same instant.
    _assert_invariant_over_history(state)
