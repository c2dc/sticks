"""Property test — isolation gate is a fail-closed precondition of completion (task 7.2).

# Feature: stage5-target-provisioning, Property 5: Portão de isolamento
# fail-closed como pré-condição de conclusão. O Estágio 5 só conclui (``ready``
# True, ``failed_step`` None) quando o portão de isolamento (``inspect_isolation``)
# aprova TODOS os contêineres-alvo; se qualquer alvo não está isolado, se a lista
# de alvos é vazia, ou se o daemon Docker está indisponível (inspector levanta
# ``RuntimeError``), o portão reprova (fail-closed) e o Estágio 5 fica não
# concluído com ``failed_step == "verificar_isolamento"`` e ``ready`` False.

Validates: Requirements 4.1, 4.3, 4.5

Esta é uma property test de *feature* (``stage5-target-provisioning`` é uma
feature, não um bugfix): a propriedade deve **valer** sobre a orquestração atual
de :meth:`~app.services.environment.provisioner.CurationEnvironmentProvisioner.provision`.

Nenhum Docker, rede pública ou Caldera real é tocado. As sub-etapas 5.1/5.2 são
costuradas por **fakes** sempre-bem-sucedidos (``EgressWindow``/``TargetSetup``/
``EgressProber`` + ``ComposeExecutor`` com as operações de rede/exec), para que a
orquestração real caminhe por todas as etapas e **alcance** o portão de
isolamento final. O que o Hypothesis varia é o cenário de isolamento, injetado
num ``ContainerInspector`` *fake* (o mesmo seam que a produção usa, só que
read-only e sem daemon):

* **todos isolados** ⇒ ``inspect_isolation`` aprova ⇒ ``provision`` conclui
  (``ready`` True, ``failed_step`` None) — Req. 4.5;
* **ao menos um não isolado** (bridge externa / dns externo / rede não encontrada)
  ⇒ o portão reprova ⇒ ``failed_step == "verificar_isolamento"`` e ``ready`` False
  — Req. 4.1;
* **daemon indisponível** (o inspector levanta ``RuntimeError``) ⇒ o portão
  reprova fail-closed ⇒ ``failed_step == "verificar_isolamento"`` e ``ready`` False
  — Req. 4.3.

Além do varrido por Hypothesis, dois exemplos determinísticos exercem o
fail-closed de **lista vazia** diretamente contra a semântica reusada
(``inspect_isolation([], fake)`` reprova) e o fail-closed de **daemon indisponível**
no caminho do provisionador.

A asserção central é que o portão de isolamento é uma **pré-condição dura** da
conclusão: ``provision`` jamais fica ``ready`` quando o isolamento não aprova.

_Requirements: 4.1, 4.3, 4.5_
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
    inspect_isolation,
)
from app.services.environment.compose_executor import CommandResult, LogLine
from app.services.environment.egress import EgressVerdict, EgressWindowResult
from app.services.environment.operationalization import (
    EnvironmentParameters,
    TargetHost,
)
from app.services.environment.provisioner import CurationEnvironmentProvisioner
from app.services.environment.target_setup import ClassifiedCommand, SetupPhase
from app.services.environment.topology_builder import HOST_CONTAINERS as _HOST_CONTAINERS


_SLUG = "shadowray"
_INTERNAL_NET = "caldera-kali-network"
_EXTERNAL_NET = "local-network"  # a KNOWN external bridge name -> reproves isolation


def _resolver(containers: list[str]) -> list[str]:
    return [_HOST_CONTAINERS.get(c, c) for c in containers]


# ---------------------------------------------------------------------------
# Always-succeeding sub-stage fakes — so the real orchestration reaches the
# final isolation gate for every scripted scenario. None of them touch Docker,
# the public network or Caldera.
# ---------------------------------------------------------------------------


@dataclass
class _OkEgressWindow:
    """Fake ``EgressWindow`` — opening/closing always succeeds (5.1 passes)."""

    def open(self, container: str, attack_networks: list[str], *, on_log) -> EgressWindowResult:
        on_log(LogLine(step="build_online", stream="info", text="open", substage="5.1"))
        return EgressWindowResult(container=container, opened=True)

    def close(self, container: str, *, on_log) -> EgressWindowResult:
        on_log(LogLine(step="encerrar_egresso", stream="info", text="close", substage="5.1"))
        return EgressWindowResult(container=container, closed=True)


@dataclass
class _OkTargetSetup:
    """Fake ``TargetSetup`` — every ONLINE/OFFLINE application succeeds."""

    def apply(
        self,
        container: str,
        commands: list[ClassifiedCommand],
        phase: SetupPhase,
        *,
        on_log,
        best_effort: bool = False,
    ) -> CommandResult:
        return CommandResult(code=0, lines=[])


@dataclass
class _OkEgressProber:
    """Fake ``EgressProber`` — egress always positively verified as lost."""

    def probe(self, container: str, *, on_log) -> EgressVerdict:
        return EgressVerdict(container=container, egress_lost=True, conclusive=True)


@dataclass
class _OkComposeExecutor:
    """Fake ``ComposeExecutor`` — ``up``/``down`` + the extended net/exec surface.

    Exposing ``attach_network``/``detach_network``/``exec_in`` makes
    ``_substages_supported`` return True, so the real 5.1/5.2 orchestration runs
    and the pipeline reaches the final isolation gate.
    """

    def up(self, compose_file: str, *, on_log) -> CommandResult:
        on_log(LogLine(step="subir_conteineres", stream="stdout", text="up"))
        return CommandResult(code=0, lines=[])

    def down(self, compose_file: str, *, on_log) -> CommandResult:
        on_log(LogLine(step="desprovisionar", stream="stdout", text="down"))
        return CommandResult(code=0, lines=[])

    def attach_network(self, container: str, network: str, *, on_log, ip=None) -> CommandResult:
        return CommandResult(code=0, lines=[])

    def detach_network(self, container: str, network: str, *, on_log) -> CommandResult:
        return CommandResult(code=0, lines=[])

    def exec_in(self, container: str, argv: list[str], *, on_log) -> CommandResult:
        return CommandResult(code=0, lines=[])


@dataclass
class _FakeCaldera:
    """In-memory Caldera loader (no network)."""

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

    Mirrors the production ``ContainerInspector`` surface. The isolation scenario
    is encoded entirely in the ``containers``/``networks`` maps the test fills,
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


class _DaemonDownInspector:
    """Fake inspector that simulates an unavailable Docker daemon.

    :func:`inspect_isolation` only guards ``ContainerNotFound``/``NetworkNotFound``;
    a ``RuntimeError`` from the inspector surfaces up to the provisioner's
    ``_check_isolation``, which catches it and reproves fail-closed (Req. 4.3) —
    the same way the real SDK adapter raises ``RuntimeError`` when the daemon does
    not respond.
    """

    def get_container_network_info(self, container: str) -> ContainerNetworkInfo:
        raise RuntimeError("Docker Engine API indisponível")

    def get_network_info(self, network_name: str) -> NetworkInfo:
        raise RuntimeError("Docker Engine API indisponível")


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
    """Inspector where every container is isolated (gate approves)."""
    insp = _FakeInspector()
    insp.networks[_INTERNAL_NET] = NetworkInfo(name=_INTERNAL_NET, internal=True, subnets=())
    for c in containers:
        insp.containers[c] = ContainerNetworkInfo(
            name=c, network_names=(_INTERNAL_NET,), dns=()
        )
    return insp


def _inspector_with_offenders(
    containers: list[str], offenders: set[str]
) -> _FakeInspector:
    """Inspector where ``offenders`` are attached to an external bridge (not isolated)."""
    insp = _FakeInspector()
    insp.networks[_INTERNAL_NET] = NetworkInfo(name=_INTERNAL_NET, internal=True, subnets=())
    for c in containers:
        if c in offenders:
            # KNOWN external bridge name -> classified non-isolated with a reason.
            insp.containers[c] = ContainerNetworkInfo(
                name=c, network_names=(_INTERNAL_NET, _EXTERNAL_NET), dns=()
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


def _params(containers: list[str]) -> EnvironmentParameters:
    hosts = [
        TargetHost(
            label=f"h{i}",
            container=c,
            vulnerabilities=["apt-get install -y nmap", "useradd attacker"],
        )
        for i, c in enumerate(containers)
    ]
    return EnvironmentParameters(campaign_id=_SLUG, hosts=hosts)


def _provisioner(
    data_dir: Path, containers: list[str], inspector: object
) -> CurationEnvironmentProvisioner:
    _seed_case(data_dir, _SLUG)
    return CurationEnvironmentProvisioner(
        inspector=inspector,
        case_service=CaseService(data_dir=data_dir),
        caldera=_FakeCaldera(),
        compose_executor=_OkComposeExecutor(),
        state_inspector=_FakeStateInspector(containers),
        egress_window=_OkEgressWindow(),
        egress_prober=_OkEgressProber(),
        target_setup=_OkTargetSetup(),
    )


# ---------------------------------------------------------------------------
# Generators — hosts and an isolation scenario.
#
# The lab resolves two logical target hosts (``nginx``/``db``), so the host-list
# domain is intentionally small. To keep a rich enough input space for Hypothesis
# to draw many distinct cases, the scenario (``all_isolated`` /
# ``some_non_isolated`` / ``daemon_down``) and a wide offender bitmask vary
# independently; the extra, unreachable mask bits still shrink/vary the drawn
# examples without changing the semantics (offenders are intersected with the
# resolved containers).
# ---------------------------------------------------------------------------

_HOST_CHOICES = ["nginx", "db"]
_hosts_strategy = st.lists(
    st.sampled_from(_HOST_CHOICES), min_size=1, max_size=2, unique=True
)
# Which isolation scenario the gate faces.
_scenario = st.sampled_from(["all_isolated", "some_non_isolated", "daemon_down"])
# A per-host bitmask selecting which hosts are the non-isolated offenders (only
# used by the ``some_non_isolated`` scenario; forced to >= 1 offender there). A
# wide range keeps the generated-example space large.
_offender_mask = st.integers(min_value=0, max_value=2**16)


@settings(max_examples=100, deadline=None)
@given(hosts=_hosts_strategy, scenario=_scenario, offender_mask=_offender_mask)
def test_isolation_gate_is_failclosed_precondition_of_completion(
    hosts: list[str], scenario: str, offender_mask: int
) -> None:
    # Feature: stage5-target-provisioning, Property 5: Portão de isolamento
    # fail-closed como pré-condição de conclusão.
    containers = _resolver(hosts)

    if scenario == "all_isolated":
        inspector: object = _isolated_inspector(containers)
    elif scenario == "daemon_down":
        inspector = _DaemonDownInspector()
    else:  # some_non_isolated
        offenders = {
            c for i, c in enumerate(containers) if (offender_mask >> i) & 1
        }
        if not offenders:  # guarantee at least one non-isolated target
            offenders = {containers[0]}
        inspector = _inspector_with_offenders(containers, offenders)

    tmp = Path(tempfile.mkdtemp(prefix="stage5_isolation_gate_prop_"))
    try:
        prov = _provisioner(tmp, containers, inspector)
        environment = prov.provision(_params(hosts))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if scenario == "all_isolated":
        # Gate approves all -> Stage 5 completes (Req. 4.5). ``ready`` can only be
        # True because the isolation gate (plus the already-ok prerequisites)
        # passed, so completion is strictly conditioned on the gate.
        assert environment.failed_step is None, environment.error_message
        assert environment.isolation.approved is True
        assert environment.ready is True
    else:
        # Gate reproves (a non-isolated target, or the daemon down fail-closed)
        # -> Stage 5 stays incomplete at the isolation gate (Req. 4.1, 4.3).
        assert environment.failed_step == "verificar_isolamento"
        assert environment.isolation.approved is False
        assert environment.ready is False
        # Every verdict backing the failure identifies the container + reason.
        assert environment.isolation.verdicts, "o portão deveria reportar vereditos"
        assert any(not v.isolated for v in environment.isolation.verdicts)
        for verdict in environment.isolation.verdicts:
            if not verdict.isolated:
                assert verdict.reasons, "um alvo não isolado deve ter uma razão"


@settings(max_examples=100, deadline=None)
@given(hosts=_hosts_strategy, offender_pick=st.integers(min_value=0, max_value=2**16))
def test_completion_requires_the_isolation_gate_to_approve_all(
    hosts: list[str], offender_pick: int
) -> None:
    # Feature: stage5-target-provisioning, Property 5: Portão de isolamento
    # fail-closed como pré-condição de conclusão.
    #
    # Reforça a cláusula "pré-condição dura": com TODAS as demais etapas ok (5.1 e
    # 5.2 pelos fakes sempre-ok), basta um ÚNICO alvo não isolado para que o
    # Estágio 5 nunca fique ``ready`` — a conclusão depende do portão aprovar
    # TODOS (Req. 4.1, 4.5). ``offender_pick`` apenas varia QUAL alvo é o não
    # isolado (sempre há ao menos um), mantendo o espaço de exemplos amplo.
    containers = _resolver(hosts)
    offenders = {containers[offender_pick % len(containers)]}  # always >= 1 offender

    tmp = Path(tempfile.mkdtemp(prefix="stage5_isolation_gate_hard_"))
    try:
        prov = _provisioner(
            tmp, containers, _inspector_with_offenders(containers, offenders)
        )
        environment = prov.provision(_params(hosts))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    assert environment.ready is False
    assert environment.failed_step == "verificar_isolamento"
    assert environment.isolation.approved is False


def test_isolation_gate_failclosed_on_empty_container_list() -> None:
    """Fail-closed: an empty target-container list reproves isolation (Req. 4.3).

    Exercises the reused :func:`inspect_isolation` semantics the provisioner's
    gate rests on — an empty list yields ``passed=False`` (nothing proven
    isolated, so fail-closed rather than vacuously approve). The provisioner can
    only complete when the gate approves, so an empty list can never complete
    Stage 5.
    _Requirements: 4.3_
    """
    result = inspect_isolation([], _FakeInspector())
    assert result.passed is False
    assert result.verdicts == ()


def test_isolation_gate_failclosed_when_daemon_unavailable() -> None:
    """Fail-closed: an unavailable daemon (``RuntimeError``) reproves (Req. 4.3).

    The provisioner's ``_check_isolation`` catches the ``RuntimeError`` the real
    adapter raises when the daemon is down and reproves with a per-container
    verdict, keeping Stage 5 incomplete at the isolation gate.
    _Requirements: 4.3_
    """
    containers = _resolver(["nginx", "db"])
    tmp = Path(tempfile.mkdtemp(prefix="stage5_isolation_gate_daemon_"))
    try:
        prov = _provisioner(tmp, containers, _DaemonDownInspector())
        environment = prov.provision(_params(["nginx", "db"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    assert environment.ready is False
    assert environment.failed_step == "verificar_isolamento"
    assert environment.isolation.approved is False
    assert len(environment.isolation.verdicts) == len(containers)
    assert all(not v.isolated for v in environment.isolation.verdicts)
