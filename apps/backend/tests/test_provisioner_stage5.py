"""Example-based tests for ``CurationEnvironmentProvisioner.provision`` (task 7.2).

The Stage-5 (environment creation) curated provisioner runs ``provision(params)``
as an **ordered pipeline** of steps — ``subir_conteineres`` → ``aguardar_saude`` →
``carregar_abilities`` → ``verificar_isolamento`` (task 3.2) — each emitting
``LogLine`` through ``on_log``; any failing step returns early with ``failed_step``
set and keeps the stage non-concluded (Req. 3.5, 7.1). It also tears the lab down
with ``deprovision`` at the end of Stage 6 (task 3.3). On ``provision`` it:

1. **Sobe o laboratório** via the INJECTED ``ComposeExecutor`` — ``code != 0``
   fails the ``subir_conteineres`` step (Req. 3.5).
2. **Confirma a saúde** of the target containers (read-only) via the INJECTED
   ``ContainerStateInspector`` — an unhealthy/not-running container fails the
   ``aguardar_saude`` step (Req. 5.1).
3. **Load the ordered Abilities + Adversary into Caldera** in DAG order
   (``atomic_ordering``), reusing ``CalderaClient.load_abilities``/``load_adversary``
   through an INJECTED client, setting ``loaded_abilities`` accordingly
   (Req. 4.1). Fail-closed on any Caldera / case error (Req. 4.4).
4. **Verify isolation (read-only)** via ``inspect_isolation`` with an INJECTED
   fake inspector BEFORE concluding (Req. 5.3). Fail-closed: an empty
   container list OR an unavailable Docker daemon reproves.

Everything here is pure/in-memory: a fake ``ComposeExecutor`` (no subprocess, no
daemon), a fake ``ContainerStateInspector`` (no daemon), a fake Caldera loader
(no network, no daemon) and a fake container inspector (no Docker daemon), plus a
temp curated-case ``data_dir`` injected into the ``CaseService``. No real network,
no AI, no public-network access.

_Requirements: 3.5, 4.4, 5.1, 5.3, 5.5, 7.1, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7,
9.8, 9.9, 14.1, 14.2, 14.3, 14.4, 11.1, 11.3, 11.4_
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from app.models.enums import TranslationSource
from app.services.campaign_service import CaseService
from app.services.containment.isolation import (
    ContainerInspector,
    ContainerNetworkInfo,
    ContainerNotFound,
    NetworkInfo,
    NetworkNotFound,
)
from app.services.environment.compose_executor import (
    ComposeExecutor,
    LogLine,
    CommandResult,
)
from app.services.environment.operationalization import (
    TargetHost,
    EnvironmentParameters,
    EnvironmentPlatform,
)
from app.services.environment.provisioner import (
    CurationEnvironmentProvisioner,
    EnvironmentProvisioner,
)
from app.services.environment.health import (
    ContainerStateInfo,
    ContainerStateInspector,
    ContainerStateNotFound,
)


# ---------------------------------------------------------------------------
# Fakes (no network, no Docker daemon, no AI)
# ---------------------------------------------------------------------------


class FakeCaldera:
    """In-memory Caldera loader recording the ordered loads (no network)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.loaded_abilities: list[Mapping[str, Any]] = []
        self.loaded_adversary: Mapping[str, Any] | None = None
        self.calls: list[str] = []

    def load_abilities(self, abilities: list[Mapping[str, Any]]) -> list[Any]:
        self.calls.append("load_abilities")
        if self.fail:
            raise RuntimeError("Caldera indisponível")
        self.loaded_abilities = list(abilities)
        return list(abilities)

    def load_adversary(self, adversary: Mapping[str, Any]) -> Any:
        self.calls.append("load_adversary")
        if self.fail:
            raise RuntimeError("Caldera indisponível")
        self.loaded_adversary = adversary
        return adversary


@dataclass
class FakeInspector:
    """In-memory read-only :class:`ContainerInspector` (no Docker daemon)."""

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


class RaisingInspector:
    """Inspector that simulates an unavailable Docker daemon (fail-closed path)."""

    def get_container_network_info(self, container: str) -> ContainerNetworkInfo:
        raise RuntimeError("Docker Engine API indisponível")

    def get_network_info(self, network_name: str) -> NetworkInfo:
        raise RuntimeError("Docker Engine API indisponível")


class FakeComposeExecutor:
    """In-memory :class:`ComposeExecutor` *fake* (no subprocess, no Docker daemon).

    Satisfaz estruturalmente o ``@runtime_checkable`` ``ComposeExecutor`` sem jamais
    tocar o ``docker`` real: cada ``subir``/``derrubar`` apenas registra a chamada,
    empurra as :class:`LogLine` configuradas por ``on_log`` (na ordem) e devolve um
    :class:`CommandResult` com o ``code`` configurado. Injetar este *fake* é o
    que mantém ``provision``/``deprovision`` **sem acesso à rede pública** nem
    ao daemon Docker nodes testes (Req. 9.1, 9.2).

    Attributes:
        codigo_subir: ``code`` devolvido por :meth:`subir` (``0`` = success).
        codigo_derrubar: ``code`` devolvido por :meth:`derrubar`.
        linhas_subir: lines ``(stream, text)`` emitidas pelo ``subir``.
        linhas_derrubar: lines ``(stream, text)`` emitidas pelo ``derrubar``.
    """

    def __init__(
        self,
        *,
        codigo_subir: int = 0,
        codigo_derrubar: int = 0,
        linhas_subir: list[tuple[str, str]] | None = None,
        linhas_derrubar: list[tuple[str, str]] | None = None,
    ) -> None:
        self.codigo_subir = codigo_subir
        self.codigo_derrubar = codigo_derrubar
        self._linhas_subir = linhas_subir or [("stdout", "Container up")]
        self._linhas_derrubar = linhas_derrubar or [("stdout", "Container removed")]
        self.up_calls: list[str] = []
        self.down_calls: list[str] = []

    def _executar(
        self,
        compose_file: str,
        *,
        step: str,
        code: int,
        linhas_spec: list[tuple[str, str]],
        on_log: Callable[[LogLine], None],
    ) -> CommandResult:
        lines: list[LogLine] = []
        for stream, text in linhas_spec:
            line = LogLine(step=step, stream=stream, text=text)
            lines.append(line)
            on_log(line)
        return CommandResult(code=code, lines=lines)

    def up(
        self, compose_file: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        self.up_calls.append(compose_file)
        return self._executar(
            compose_file,
            step="subir_conteineres",
            code=self.codigo_subir,
            linhas_spec=self._linhas_subir,
            on_log=on_log,
        )

    def down(
        self, compose_file: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        self.down_calls.append(compose_file)
        return self._executar(
            compose_file,
            step="deprovision",
            code=self.codigo_derrubar,
            linhas_spec=self._linhas_derrubar,
            on_log=on_log,
        )


class FakeStateInspector:
    """In-memory read-only :class:`ContainerStateInspector` (no Docker daemon).

    Devolve :class:`ContainerStateInfo` fabricadas por name e levanta
    :class:`ContainerStateNotFound` para contêiner desconhecido — o contrato
    fail-closed que a step ``aguardar_saude`` espera (Req. 5.1). É o *fake* que
    mantém a verificação de saúde sem daemon Docker.
    """

    def __init__(self, states: dict[str, ContainerStateInfo]) -> None:
        self._estados = dict(states)
        self.consultados: list[str] = []

    def get_container_state(self, container: str) -> ContainerStateInfo:
        self.consultados.append(container)
        try:
            return self._estados[container]
        except KeyError as exc:
            raise ContainerStateNotFound(container) from exc


def _healthy_state_inspector(containers: list[str]) -> FakeStateInspector:
    """Um :class:`FakeStateInspector` com todos os ``containers`` running+healthy."""
    return FakeStateInspector(
        {
            c: ContainerStateInfo(name=c, running=True, health="healthy")
            for c in _resolver(containers)
        }
    )


# ---------------------------------------------------------------------------
# Curated-case temp fixtures (read-only w.r.t. real data)
# ---------------------------------------------------------------------------

_SLUG = "shadowray"  # a registered curated slug (CURATED_CASES)
_C1 = "nginx"
_C2 = "db"
_NET = "caldera-kali-network"

# Resolve o name logico do host (ex.: ``nginx``, ``db``) para o ``container_name``
# real do laboratorio, igual ao que ``CurationEnvironmentProvisioner._target_containers``
# faz em producao (via ``HOST_CONTAINERS``), de modo que os fakes de health/isolation
# conhecam exatamente os nomes que o ``provision`` inspeciona.
from app.services.environment.topology_builder import HOST_CONTAINERS as _HOST_CONTAINERS

def _resolver(containers: list[str]) -> list[str]:
    return [_HOST_CONTAINERS.get(c, c) for c in containers]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_case(data_dir: Path, slug: str, *, with_adversary: bool = True,
               with_abilities: bool = True) -> None:
    """Seed a minimal curated case with a 2-ability ordered adversary."""
    abilities = (
        [
            {
                "ability_id": f"{slug}-ab-1",
                "name": "first",
                "technique_id": "T1190",
                "executors": [
                    {"name": "sh", "platform": "linux", "command": "echo 1"}
                ],
            },
            {
                "ability_id": f"{slug}-ab-2",
                "name": "second",
                "technique_id": "T1059",
                "executors": [
                    {"name": "sh", "platform": "linux", "command": "echo 2"}
                ],
            },
        ]
        if with_abilities
        else []
    )
    _write_json(data_dir / "api" / f"{slug}_dag-ability.json", abilities)

    if with_adversary:
        _write_json(
            data_dir / "api" / f"{slug}_dag-adversary.json",
            {
                "id": f"{slug}-adv",
                "name": "Seed Adversary",
                "description": "seed",
                # Reversed order on purpose: provision must load in THIS order.
                "atomic_ordering": [f"{slug}-ab-2", f"{slug}-ab-1"],
            },
        )
    else:
        # Remove any adversary so the case has none.
        (data_dir / "api" / f"{slug}_dag-adversary.json").unlink(missing_ok=True)

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


@pytest.fixture()
def data_dir() -> Iterator[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_estagio5_"))
    (tmp / "api").mkdir(parents=True, exist_ok=True)
    (tmp / "dag").mkdir(parents=True, exist_ok=True)
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _params(containers: list[str] | None = None) -> EnvironmentParameters:
    hosts = [TargetHost(label=f"h{i}", container=c) for i, c in enumerate(containers or [])]
    return EnvironmentParameters(campaign_id=_SLUG, hosts=hosts)


def _isolated_inspector(containers: list[str]) -> FakeInspector:
    insp = FakeInspector()
    insp.networks[_NET] = NetworkInfo(name=_NET, internal=True, subnets=())
    for c in _resolver(containers):
        insp.containers[c] = ContainerNetworkInfo(name=c, network_names=(_NET,), dns=())
    return insp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_curadoria_satisfies_the_protocol() -> None:
    """``CurationEnvironmentProvisioner`` satisfies the Stage-5 Protocol."""
    prov = CurationEnvironmentProvisioner()
    assert isinstance(prov, EnvironmentProvisioner)
    assert prov.origin() is TranslationSource.HUMAN_CURATION


def test_provisionar_loads_abilities_in_dag_order_and_concludes(data_dir: Path) -> None:
    """Isolated + loaded -> environment ready, abilities in atomic_ordering order.
    _Requirements: 9.5, 9.6, 9.8, 9.9_"""
    _seed_case(data_dir, _SLUG)
    containers = [_C1, _C2]
    caldera = FakeCaldera()
    prov = CurationEnvironmentProvisioner(
        inspector=_isolated_inspector(containers),
        case_service=CaseService(data_dir=data_dir),
        caldera=caldera,
        compose_executor=FakeComposeExecutor(),
        state_inspector=_healthy_state_inspector(containers),
    )

    environment = prov.provision(_params(containers))

    # Isolation verified (read-only) and approved -> stage can conclude.
    assert environment.isolation.approved is True
    assert environment.loaded_abilities is True
    assert environment.campaign_id == _SLUG
    assert environment.platform is EnvironmentPlatform.CONTAINER
    assert environment.target_containers == _resolver(containers)
    assert environment.compose_file is not None
    assert environment.origin is TranslationSource.HUMAN_CURATION

    # Abilities loaded in the adversary's atomic_ordering order (ab-2 then ab-1).
    assert caldera.calls == ["load_abilities", "load_adversary"]
    loaded_ids = [a["ability_id"] for a in caldera.loaded_abilities]
    assert loaded_ids == [f"{_SLUG}-ab-2", f"{_SLUG}-ab-1"]
    assert caldera.loaded_adversary is not None
    assert caldera.loaded_adversary["adversary_id"] == f"{_SLUG}-adv"


def test_provisionar_not_isolated_does_not_conclude(data_dir: Path) -> None:
    """A container on an external bridge keeps the stage non-concluded (Req. 9.7).
    _Requirements: 9.6, 9.7, 14.2, 14.4_"""
    _seed_case(data_dir, _SLUG)
    containers = [_C1, _C2]
    insp = _isolated_inspector(containers)
    # Attach C2 to an external bridge -> not isolated.
    _r2 = _resolver([_C2])[0]
    insp.containers[_r2] = ContainerNetworkInfo(
        name=_r2, network_names=(_NET, "local-network"), dns=()
    )
    prov = CurationEnvironmentProvisioner(
        inspector=insp,
        case_service=CaseService(data_dir=data_dir),
        caldera=FakeCaldera(),
        compose_executor=FakeComposeExecutor(),
        state_inspector=_healthy_state_inspector(containers),
    )

    environment = prov.provision(_params(containers))

    assert environment.isolation.approved is False
    non_isolated = environment.isolation.non_isolated
    assert [v.container for v in non_isolated] == _resolver([_C2])
    assert non_isolated[0].reasons  # the reason is reported (Req. 9.7, 14.4)
    # Abilities may have loaded, but the stage still cannot conclude.
    assert environment.loaded_abilities is True


def test_provisionar_empty_containers_is_fail_closed(data_dir: Path) -> None:
    """No resolvable target containers -> fail-closed (not approved).
    _Requirements: 9.7, 14.4_"""
    _seed_case(data_dir, _SLUG)
    # No hosts resolved AND empty lab fallback is non-empty, so force empty by
    # using an inspector that leaves every lab container unknown (not isolated).
    # To test the *empty list* path directly we stub the resolver via params:
    from app.services.environment.topology_builder import container_names

    lab_containers = container_names()
    prov = CurationEnvironmentProvisioner(
        inspector=FakeInspector(),  # knows nothing -> all unknown -> not isolated
        case_service=CaseService(data_dir=data_dir),
        caldera=FakeCaldera(),
        compose_executor=FakeComposeExecutor(),
        # The lab fallback containers must pass the health gate so the pipeline
        # reaches the isolation step (where the empty-knowledge inspector reproves).
        state_inspector=_healthy_state_inspector(lab_containers),
    )

    # Params with no hosts fall back to the lab containers; none are known to the
    # empty inspector, so isolation fails closed.
    environment = prov.provision(_params(None))
    assert environment.isolation.approved is False


def test_provisionar_daemon_unavailable_is_fail_closed(data_dir: Path) -> None:
    """An unavailable Docker daemon (RuntimeError) reproves fail-closed.
    _Requirements: 9.7, 14.4_"""
    _seed_case(data_dir, _SLUG)
    containers = [_C1, _C2]
    prov = CurationEnvironmentProvisioner(
        inspector=RaisingInspector(),
        case_service=CaseService(data_dir=data_dir),
        caldera=FakeCaldera(),
        compose_executor=FakeComposeExecutor(),
        state_inspector=_healthy_state_inspector(containers),
    )

    environment = prov.provision(_params(containers))

    assert environment.isolation.approved is False
    # Every target container is reported with the daemon-unavailable reason.
    assert [v.container for v in environment.isolation.verdicts] == _resolver(containers)
    for v in environment.isolation.verdicts:
        assert v.isolated is False
        assert any("indispon" in r.lower() for r in v.reasons)


def test_provisionar_caldera_failure_marks_abilities_not_loaded(data_dir: Path) -> None:
    """A Caldera failure keeps ``loaded_abilities`` False (fail-closed load).
    _Requirements: 9.5, 9.9_"""
    _seed_case(data_dir, _SLUG)
    containers = [_C1, _C2]
    prov = CurationEnvironmentProvisioner(
        inspector=_isolated_inspector(containers),
        case_service=CaseService(data_dir=data_dir),
        caldera=FakeCaldera(fail=True),
        compose_executor=FakeComposeExecutor(),
        state_inspector=_healthy_state_inspector(containers),
    )

    environment = prov.provision(_params(containers))

    # A carga de Abilities failure ANTES do isolation no pipeline (task 3.2), então
    # o pipeline retorna cedo em ``carregar_abilities`` e o isolation não roda.
    assert environment.failed_step == "carregar_abilities"
    assert environment.loaded_abilities is False
    assert environment.isolation.approved is False
    assert environment.ready is False


def test_provisionar_no_abilities_marks_not_loaded(data_dir: Path) -> None:
    """A case with no abilities loads nothing -> ``loaded_abilities`` False.
    _Requirements: 9.5, 9.9_"""
    _seed_case(data_dir, _SLUG, with_abilities=False, with_adversary=False)
    containers = [_C1, _C2]
    caldera = FakeCaldera()
    prov = CurationEnvironmentProvisioner(
        inspector=_isolated_inspector(containers),
        case_service=CaseService(data_dir=data_dir),
        caldera=caldera,
        compose_executor=FakeComposeExecutor(),
        state_inspector=_healthy_state_inspector(containers),
    )

    environment = prov.provision(_params(containers))

    assert environment.loaded_abilities is False
    assert caldera.calls == []  # Caldera never touched when there are no abilities


def test_provisionar_without_adversary_still_loads_abilities(data_dir: Path) -> None:
    """No adversary -> abilities still load (load_adversary skipped).
    _Requirements: 9.5_"""
    _seed_case(data_dir, _SLUG, with_adversary=False)
    containers = [_C1, _C2]
    caldera = FakeCaldera()
    prov = CurationEnvironmentProvisioner(
        inspector=_isolated_inspector(containers),
        case_service=CaseService(data_dir=data_dir),
        caldera=caldera,
        compose_executor=FakeComposeExecutor(),
        state_inspector=_healthy_state_inspector(containers),
    )

    environment = prov.provision(_params(containers))

    assert environment.loaded_abilities is True
    assert caldera.calls == ["load_abilities"]
    # Without atomic_ordering the natural file order is used.
    loaded_ids = [a["ability_id"] for a in caldera.loaded_abilities]
    assert loaded_ids == [f"{_SLUG}-ab-1", f"{_SLUG}-ab-2"]


# ===========================================================================
# Task 3.4 — Pipeline ``provision``/``deprovision`` com TODOS os colaboradores
# fakes: caminho feliz conclui com os três pré-requisitos; cada step falhando
# isoladamente produz o ``failed_step`` correto e bloqueia a conclusão; nenhum
# acesso à rede pública (os fakes garantem — sem subprocess, sem daemon Docker,
# sem rede, sem IA).
#
# _Requirements: 3.5, 4.4, 5.3, 5.5, 7.1, 9.1, 9.2
# ===========================================================================

# Etapas do pipeline na ordem do design (task 3.2), para asserir a sequência das
# lines e os ``failed_step`` possíveis.
_ETAPAS = (
    "subir_conteineres",
    "aguardar_saude",
    "carregar_abilities",
    "verificar_isolamento",
)


def _curadoria(
    data_dir: Path,
    containers: list[str],
    *,
    compose: FakeComposeExecutor | None = None,
    state: ContainerStateInspector | None = None,
    inspector: ContainerInspector | None = None,
    caldera: FakeCaldera | None = None,
    with_abilities: bool = True,
    with_adversary: bool = True,
) -> CurationEnvironmentProvisioner:
    """Monta a curation com TODOS os colaboradores fakes (sem rede, sem daemon).

    Semeia o campaign curated no ``data_dir`` e injeta: um ``ComposeExecutor`` fake
    (success por default), um ``ContainerStateInspector`` fake (todos saudáveis por
    default), um ``ContainerInspector`` fake isolated por default e um ``CalderaLoader``
    fake. Qualquer colaborador pode ser substituído para exercitar a failure de uma
    step isolada — as demais permanecem no caminho feliz.
    """
    _seed_case(
        data_dir, _SLUG, with_abilities=with_abilities, with_adversary=with_adversary
    )
    return CurationEnvironmentProvisioner(
        inspector=inspector if inspector is not None else _isolated_inspector(containers),
        case_service=CaseService(data_dir=data_dir),
        caldera=caldera if caldera is not None else FakeCaldera(),
        compose_executor=compose if compose is not None else FakeComposeExecutor(),
        state_inspector=state if state is not None else _healthy_state_inspector(containers),
    )


# ---------------------------------------------------------------------------
# 1. Caminho feliz — as quatro etapas passam → ready, três pré-requisitos ok,
#    on_log recebeu lines de cada step na ordem, sem failed_step.
# ---------------------------------------------------------------------------


def test_provisionar_caminho_feliz_conclui_com_tres_prerequisitos(data_dir: Path) -> None:
    """Quatro etapas ok → ``ready`` com saúde + Abilities + isolation aprovados.
    _Requirements: 5.3, 5.5, 7.1_"""
    containers = [_C1, _C2]
    compose = FakeComposeExecutor()
    prov = _curadoria(data_dir, containers, compose=compose)

    lines: list[LogLine] = []
    environment = prov.provision(_params(containers), on_log=lines.append)

    # Os três pré-requisitos passaram e o pipeline não teve retorno antecipado.
    assert environment.failed_step is None
    assert environment.error_message is None
    assert environment.health.approved is True
    assert environment.loaded_abilities is True
    assert environment.isolation.approved is True
    assert environment.ready is True
    assert environment.target_containers == _resolver(containers)

    # on_log recebeu ao menos uma line de CADA step, na ordem do pipeline.
    etapas_vistas = [l.step for l in lines]
    for step in _ETAPAS:
        assert step in etapas_vistas, f"nenhuma line emitida para {step!r}"
    # A primeira ocorrência de cada step respeita a ordem subir → saúde →
    # abilities → isolation.
    primeira_ocorrencia = [etapas_vistas.index(step) for step in _ETAPAS]
    assert primeira_ocorrencia == sorted(primeira_ocorrencia)

    # O compose foi efetivamente usado (o fake, não o real) com o compose do campaign.
    assert compose.up_calls == [environment.compose_file]


# ---------------------------------------------------------------------------
# 2. subir_conteineres failure (compose code != 0) → failed_step="subir_conteineres",
#    pipeline para (abilities não carregadas, isolation não verificado).
# ---------------------------------------------------------------------------


def test_provisionar_falha_subir_conteineres_bloqueia_pipeline(data_dir: Path) -> None:
    """``up`` com ``code != 0`` → ``failed_step='subir_conteineres'`` e para.
    _Requirements: 3.5, 7.1_"""
    containers = [_C1, _C2]
    compose = FakeComposeExecutor(
        codigo_subir=1,
        linhas_subir=[("stderr", "Error response from daemon: no such image")],
    )
    caldera = FakeCaldera()
    state = _healthy_state_inspector(containers)
    prov = _curadoria(
        data_dir, containers, compose=compose, state=state, caldera=caldera
    )

    environment = prov.provision(_params(containers))

    assert environment.failed_step == "subir_conteineres"
    assert environment.error_message is not None and environment.error_message != ""
    assert environment.ready is False
    # O pipeline parou na 1ª step: saúde não rodou, Abilities não carregaram,
    # isolation não foi verificado.
    assert state.consultados == []  # a step de saúde nem tocou o inspector
    assert caldera.calls == []  # as Abilities nunca foram carregadas
    assert environment.loaded_abilities is False
    assert environment.health.approved is False
    assert environment.isolation.approved is False


# ---------------------------------------------------------------------------
# 3. aguardar_saude failure (um contêiner não-running/unhealthy) →
#    failed_step="aguardar_saude", ready=False, para antes de abilities/isolation.
# ---------------------------------------------------------------------------


def test_provisionar_falha_aguardar_saude_bloqueia_pipeline(data_dir: Path) -> None:
    """Um contêiner ``unhealthy`` → ``failed_step='aguardar_saude'`` e para.
    _Requirements: 5.5, 7.1_"""
    containers = [_C1, _C2]
    # _C2 reporta unhealthy -> a step de saúde reprova.
    _r1, _r2 = _resolver([_C1, _C2])
    state = FakeStateInspector(
        {
            _r1: ContainerStateInfo(name=_r1, running=True, health="healthy"),
            _r2: ContainerStateInfo(name=_r2, running=True, health="unhealthy"),
        }
    )
    caldera = FakeCaldera()
    prov = _curadoria(data_dir, containers, state=state, caldera=caldera)

    environment = prov.provision(_params(containers))

    assert environment.failed_step == "aguardar_saude"
    assert environment.health.approved is False
    assert [v.container for v in environment.health.unhealthy] == _resolver([_C2])
    assert environment.error_message is not None and _resolver([_C2])[0] in environment.error_message
    assert environment.ready is False
    # Parou antes de carregar Abilities e verificar isolation.
    assert caldera.calls == []
    assert environment.loaded_abilities is False
    assert environment.isolation.approved is False


def test_provisionar_falha_saude_container_nao_running(data_dir: Path) -> None:
    """Um contêiner fora de ``running`` também reprova ``aguardar_saude``.
    _Requirements: 5.5, 7.1_"""
    containers = [_C1, _C2]
    _r1, _r2 = _resolver([_C1, _C2])
    state = FakeStateInspector(
        {
            _r1: ContainerStateInfo(name=_r1, running=True, health="healthy"),
            _r2: ContainerStateInfo(name=_r2, running=False, health=None),
        }
    )
    prov = _curadoria(data_dir, containers, state=state)

    environment = prov.provision(_params(containers))

    assert environment.failed_step == "aguardar_saude"
    assert environment.ready is False
    assert [v.container for v in environment.health.unhealthy] == _resolver([_C2])


# ---------------------------------------------------------------------------
# 4. carregar_abilities failure (Caldera levanta) → failed_step="carregar_abilities",
#    ready=False, isolation não verificado.
# ---------------------------------------------------------------------------


def test_provisionar_falha_carregar_abilities_bloqueia_pipeline(data_dir: Path) -> None:
    """Caldera indisponível → ``failed_step='carregar_abilities'`` e para.
    _Requirements: 4.4, 7.1_"""
    containers = [_C1, _C2]
    caldera = FakeCaldera(fail=True)
    prov = _curadoria(data_dir, containers, caldera=caldera)

    environment = prov.provision(_params(containers))

    assert environment.failed_step == "carregar_abilities"
    assert environment.loaded_abilities is False
    assert environment.error_message is not None and environment.error_message != ""
    assert environment.ready is False
    # Saúde passou (pré-requisito anterior) mas isolation não foi verificado.
    assert environment.health.approved is True
    assert environment.isolation.approved is False
    # A step de fato tentou carregar no Caldera antes de falhar.
    assert caldera.calls == ["load_abilities"]


def test_provisionar_falha_carregar_abilities_caso_sem_abilities(data_dir: Path) -> None:
    """campaign sem Abilities → ``failed_step='carregar_abilities'`` (fail-closed).
    _Requirements: 4.4, 7.1_"""
    containers = [_C1, _C2]
    caldera = FakeCaldera()
    prov = _curadoria(
        data_dir,
        containers,
        caldera=caldera,
        with_abilities=False,
        with_adversary=False,
    )

    environment = prov.provision(_params(containers))

    assert environment.failed_step == "carregar_abilities"
    assert environment.loaded_abilities is False
    assert environment.ready is False
    assert caldera.calls == []  # nada a carregar: o Caldera nem foi tocado


# ---------------------------------------------------------------------------
# 5. verificar_isolamento failure (contêiner em rede externa) →
#    failed_step="verificar_isolamento", ready=False (saúde + abilities ok).
# ---------------------------------------------------------------------------


def test_provisionar_falha_verificar_isolamento_bloqueia_conclusao(data_dir: Path) -> None:
    """Contêiner em bridge external → ``failed_step='verificar_isolamento'``.
    _Requirements: 5.3, 5.5, 7.1_"""
    containers = [_C1, _C2]
    insp = _isolated_inspector(containers)
    # _C2 anexado a um bridge external -> não isolated -> a step reprova.
    _r2 = _resolver([_C2])[0]
    insp.containers[_r2] = ContainerNetworkInfo(
        name=_r2, network_names=(_NET, "local-network"), dns=()
    )
    prov = _curadoria(data_dir, containers, inspector=insp)

    environment = prov.provision(_params(containers))

    assert environment.failed_step == "verificar_isolamento"
    assert environment.isolation.approved is False
    assert [v.container for v in environment.isolation.non_isolated] == _resolver([_C2])
    assert environment.error_message is not None and environment.error_message != ""
    assert environment.ready is False
    # Os dois pré-requisitos anteriores passaram (só o isolation reprovou).
    assert environment.health.approved is True
    assert environment.loaded_abilities is True


# ---------------------------------------------------------------------------
# 6. deprovision success (compose down code=0) → deprovisioned=True,
#    sem recursos remanescentes.
# ---------------------------------------------------------------------------


def test_desprovisionar_sucesso_sem_remanescentes(data_dir: Path) -> None:
    """``down`` com ``code=0`` → ``deprovisioned=True`` e sem remanescentes.
    _Requirements: 9.1_"""
    containers = [_C1, _C2]
    compose = FakeComposeExecutor()
    prov = _curadoria(data_dir, containers, compose=compose)

    environment = prov.provision(_params(containers))
    assert environment.ready is True

    lines: list[LogLine] = []
    result = prov.deprovision(environment, on_log=lines.append)

    assert result.deprovisioned is True
    assert result.remaining_resources == []
    assert result.origin is TranslationSource.HUMAN_CURATION
    # O ``down`` do fake foi chamado com o compose do environment provisionado.
    assert compose.down_calls == [environment.compose_file]
    # on_log recebeu as lines do teardown.
    assert lines and all(l.step == "deprovision" for l in lines)


# ---------------------------------------------------------------------------
# 7. deprovision failure (code != 0) → deprovisioned=False,
#    remaining_resources == target_containers.
# ---------------------------------------------------------------------------


def test_desprovisionar_falha_reporta_recursos_remanescentes(data_dir: Path) -> None:
    """``down`` com ``code != 0`` → ``deprovisioned=False`` com remanescentes.
    _Requirements: 9.1_"""
    containers = [_C1, _C2]
    compose = FakeComposeExecutor(
        codigo_derrubar=1,
        linhas_derrubar=[("stderr", "Error: failed to remove network")],
    )
    prov = _curadoria(data_dir, containers, compose=compose)

    environment = prov.provision(_params(containers))
    result = prov.deprovision(environment)

    assert result.deprovisioned is False
    assert result.remaining_resources == environment.target_containers == _resolver(containers)
    assert result.message is not None and result.message != ""


# ---------------------------------------------------------------------------
# 8. Nenhum acesso à rede pública: os fakes são de fato usados (compose/state/
#    isolation/caldera), provando que nem subprocess, nem daemon Docker, nem a
#    rede são tocados no caminho feliz nem no teardown.
# ---------------------------------------------------------------------------


def test_pipeline_usa_apenas_fakes_sem_rede_publica(data_dir: Path) -> None:
    """Todo o pipeline + teardown passam só pelos fakes — sem rede/daemon/subprocess.
    _Requirements: 9.1, 9.2_"""
    containers = [_C1, _C2]
    compose = FakeComposeExecutor()
    state = _healthy_state_inspector(containers)
    caldera = FakeCaldera()
    inspector = _isolated_inspector(containers)
    prov = CurationEnvironmentProvisioner(
        inspector=inspector,
        case_service=CaseService(data_dir=data_dir),
        caldera=caldera,
        compose_executor=compose,
        state_inspector=state,
    )
    _seed_case(data_dir, _SLUG)

    environment = prov.provision(_params(containers))
    prov.deprovision(environment)

    # Cada colaborador fake foi efetivamente exercitado — nenhum caminho caiu no
    # ``_get_compose_executor``/``inspect_*`` reais (que tocariam subprocess/daemon).
    assert compose.up_calls == [environment.compose_file]
    assert compose.down_calls == [environment.compose_file]
    assert state.consultados == _resolver(containers)  # a step de saúde usou o fake
    assert caldera.calls == ["load_abilities", "load_adversary"]
    # E o provisioner seguia referenciando os MESMOS fakes injetados (nunca
    # substituídos por um executor/state real).
    assert prov._compose_executor is compose
    assert prov._state_inspector is state
    assert prov._caldera is caldera
    assert prov._inspector is inspector
