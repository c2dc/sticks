"""Example-based tests for ``CuradoriaProvisionadorDeAmbiente.provisionar`` (task 7.2).

The Stage-5 (environment creation) curated provisioner runs ``provisionar(params)``
as an **ordered pipeline** of steps — ``subir_conteineres`` → ``aguardar_saude`` →
``carregar_abilities`` → ``verificar_isolamento`` (task 3.2) — each emitting
``LinhaLog`` through ``on_log``; any failing step returns early with ``etapa_falha``
set and keeps the stage non-concluded (Req. 3.5, 7.1). It also tears the lab down
with ``desprovisionar`` at the end of Stage 6 (task 3.3). On ``provisionar`` it:

1. **Sobe o laboratório** via the INJECTED ``ComposeExecutor`` — ``codigo != 0``
   fails the ``subir_conteineres`` step (Req. 3.5).
2. **Confirma a saúde** of the target containers (read-only) via the INJECTED
   ``ContainerStateInspector`` — an unhealthy/not-running container fails the
   ``aguardar_saude`` step (Req. 5.1).
3. **Load the ordered Abilities + Adversary into Caldera** in DAG order
   (``atomic_ordering``), reusing ``CalderaClient.load_abilities``/``load_adversary``
   through an INJECTED client, setting ``abilities_carregadas`` accordingly
   (Req. 4.1). Fail-closed on any Caldera / case error (Req. 4.4).
4. **Verify isolation (read-only)** via ``inspect_isolation`` with an INJECTED
   fake inspector BEFORE concluding (Req. 5.3). Fail-closed: an empty
   container list OR an unavailable Docker daemon reproves.

Everything here is pure/in-memory: a fake ``ComposeExecutor`` (no subprocess, no
daemon), a fake ``ContainerStateInspector`` (no daemon), a fake Caldera loader
(no network, no daemon) and a fake container inspector (no Docker daemon), plus a
temp curated-case ``data_dir`` injected into the ``CaseService``. No real network,
no AI, no public-network access.

_Requisitos: 3.5, 4.4, 5.1, 5.3, 5.5, 7.1, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7,
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
from app.services.case_service import CaseService
from app.services.containment.isolation import (
    ContainerInspector,
    ContainerNetworkInfo,
    ContainerNotFound,
    NetworkInfo,
    NetworkNotFound,
)
from app.services.environment.compose_executor import (
    ComposeExecutor,
    LinhaLog,
    ResultadoComando,
)
from app.services.environment.operacionalizacao import (
    HostAlvo,
    ParametrosDeAmbiente,
    PlataformaAmbiente,
)
from app.services.environment.provisionador import (
    CuradoriaProvisionadorDeAmbiente,
    ProvisionadorDeAmbiente,
)
from app.services.environment.saude import (
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
    empurra as :class:`LinhaLog` configuradas por ``on_log`` (na ordem) e devolve um
    :class:`ResultadoComando` com o ``codigo`` configurado. Injetar este *fake* é o
    que mantém ``provisionar``/``desprovisionar`` **sem acesso à rede pública** nem
    ao daemon Docker nos testes (Req. 9.1, 9.2).

    Attributes:
        codigo_subir: ``codigo`` devolvido por :meth:`subir` (``0`` = sucesso).
        codigo_derrubar: ``codigo`` devolvido por :meth:`derrubar`.
        linhas_subir: linhas ``(fluxo, texto)`` emitidas pelo ``subir``.
        linhas_derrubar: linhas ``(fluxo, texto)`` emitidas pelo ``derrubar``.
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
        self.subir_calls: list[str] = []
        self.derrubar_calls: list[str] = []

    def _executar(
        self,
        compose_file: str,
        *,
        etapa: str,
        codigo: int,
        linhas_spec: list[tuple[str, str]],
        on_log: Callable[[LinhaLog], None],
    ) -> ResultadoComando:
        linhas: list[LinhaLog] = []
        for fluxo, texto in linhas_spec:
            linha = LinhaLog(etapa=etapa, fluxo=fluxo, texto=texto)
            linhas.append(linha)
            on_log(linha)
        return ResultadoComando(codigo=codigo, linhas=linhas)

    def subir(
        self, compose_file: str, *, on_log: Callable[[LinhaLog], None]
    ) -> ResultadoComando:
        self.subir_calls.append(compose_file)
        return self._executar(
            compose_file,
            etapa="subir_conteineres",
            codigo=self.codigo_subir,
            linhas_spec=self._linhas_subir,
            on_log=on_log,
        )

    def derrubar(
        self, compose_file: str, *, on_log: Callable[[LinhaLog], None]
    ) -> ResultadoComando:
        self.derrubar_calls.append(compose_file)
        return self._executar(
            compose_file,
            etapa="desprovisionar",
            codigo=self.codigo_derrubar,
            linhas_spec=self._linhas_derrubar,
            on_log=on_log,
        )


class FakeStateInspector:
    """In-memory read-only :class:`ContainerStateInspector` (no Docker daemon).

    Devolve :class:`ContainerStateInfo` fabricadas por nome e levanta
    :class:`ContainerStateNotFound` para contêiner desconhecido — o contrato
    fail-closed que a etapa ``aguardar_saude`` espera (Req. 5.1). É o *fake* que
    mantém a verificação de saúde sem daemon Docker.
    """

    def __init__(self, estados: dict[str, ContainerStateInfo]) -> None:
        self._estados = dict(estados)
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

# Resolve o nome logico do host (ex.: ``nginx``, ``db``) para o ``container_name``
# real do laboratorio, igual ao que ``CuradoriaProvisionadorDeAmbiente._containers_alvo``
# faz em producao (via ``HOST_CONTAINERS``), de modo que os fakes de saude/isolamento
# conhecam exatamente os nomes que o ``provisionar`` inspeciona.
from app.services.environment.topologia_builder import HOST_CONTAINERS as _HOST_CONTAINERS

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
                # Reversed order on purpose: provisionar must load in THIS order.
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


def _params(containers: list[str] | None = None) -> ParametrosDeAmbiente:
    hosts = [HostAlvo(rotulo=f"h{i}", container=c) for i, c in enumerate(containers or [])]
    return ParametrosDeAmbiente(caso_id=_SLUG, hosts=hosts)


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
    """``CuradoriaProvisionadorDeAmbiente`` satisfies the Stage-5 Protocol."""
    prov = CuradoriaProvisionadorDeAmbiente()
    assert isinstance(prov, ProvisionadorDeAmbiente)
    assert prov.origem() is TranslationSource.HUMAN_CURATION


def test_provisionar_loads_abilities_in_dag_order_and_concludes(data_dir: Path) -> None:
    """Isolated + loaded -> ambiente pronto, abilities in atomic_ordering order.
    _Requisitos: 9.5, 9.6, 9.8, 9.9_"""
    _seed_case(data_dir, _SLUG)
    containers = [_C1, _C2]
    caldera = FakeCaldera()
    prov = CuradoriaProvisionadorDeAmbiente(
        inspector=_isolated_inspector(containers),
        case_service=CaseService(data_dir=data_dir),
        caldera=caldera,
        compose_executor=FakeComposeExecutor(),
        state_inspector=_healthy_state_inspector(containers),
    )

    ambiente = prov.provisionar(_params(containers))

    # Isolation verified (read-only) and approved -> stage can conclude.
    assert ambiente.isolamento.aprovado is True
    assert ambiente.abilities_carregadas is True
    assert ambiente.caso_id == _SLUG
    assert ambiente.plataforma is PlataformaAmbiente.CONTAINER
    assert ambiente.containers_alvo == _resolver(containers)
    assert ambiente.compose_file is not None
    assert ambiente.origem is TranslationSource.HUMAN_CURATION

    # Abilities loaded in the adversary's atomic_ordering order (ab-2 then ab-1).
    assert caldera.calls == ["load_abilities", "load_adversary"]
    loaded_ids = [a["ability_id"] for a in caldera.loaded_abilities]
    assert loaded_ids == [f"{_SLUG}-ab-2", f"{_SLUG}-ab-1"]
    assert caldera.loaded_adversary is not None
    assert caldera.loaded_adversary["adversary_id"] == f"{_SLUG}-adv"


def test_provisionar_not_isolated_does_not_conclude(data_dir: Path) -> None:
    """A container on an external bridge keeps the stage non-concluded (Req. 9.7).
    _Requisitos: 9.6, 9.7, 14.2, 14.4_"""
    _seed_case(data_dir, _SLUG)
    containers = [_C1, _C2]
    insp = _isolated_inspector(containers)
    # Attach C2 to an external bridge -> not isolated.
    _r2 = _resolver([_C2])[0]
    insp.containers[_r2] = ContainerNetworkInfo(
        name=_r2, network_names=(_NET, "local-network"), dns=()
    )
    prov = CuradoriaProvisionadorDeAmbiente(
        inspector=insp,
        case_service=CaseService(data_dir=data_dir),
        caldera=FakeCaldera(),
        compose_executor=FakeComposeExecutor(),
        state_inspector=_healthy_state_inspector(containers),
    )

    ambiente = prov.provisionar(_params(containers))

    assert ambiente.isolamento.aprovado is False
    nao_isolados = ambiente.isolamento.nao_isolados
    assert [v.container for v in nao_isolados] == _resolver([_C2])
    assert nao_isolados[0].razoes  # the reason is reported (Req. 9.7, 14.4)
    # Abilities may have loaded, but the stage still cannot conclude.
    assert ambiente.abilities_carregadas is True


def test_provisionar_empty_containers_is_fail_closed(data_dir: Path) -> None:
    """No resolvable target containers -> fail-closed (not approved).
    _Requisitos: 9.7, 14.4_"""
    _seed_case(data_dir, _SLUG)
    # No hosts resolved AND empty lab fallback is non-empty, so force empty by
    # using an inspector that leaves every lab container unknown (not isolated).
    # To test the *empty list* path directly we stub the resolver via params:
    from app.services.environment.topologia_builder import container_names

    lab_containers = container_names()
    prov = CuradoriaProvisionadorDeAmbiente(
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
    ambiente = prov.provisionar(_params(None))
    assert ambiente.isolamento.aprovado is False


def test_provisionar_daemon_unavailable_is_fail_closed(data_dir: Path) -> None:
    """An unavailable Docker daemon (RuntimeError) reproves fail-closed.
    _Requisitos: 9.7, 14.4_"""
    _seed_case(data_dir, _SLUG)
    containers = [_C1, _C2]
    prov = CuradoriaProvisionadorDeAmbiente(
        inspector=RaisingInspector(),
        case_service=CaseService(data_dir=data_dir),
        caldera=FakeCaldera(),
        compose_executor=FakeComposeExecutor(),
        state_inspector=_healthy_state_inspector(containers),
    )

    ambiente = prov.provisionar(_params(containers))

    assert ambiente.isolamento.aprovado is False
    # Every target container is reported with the daemon-unavailable reason.
    assert [v.container for v in ambiente.isolamento.veredictos] == _resolver(containers)
    for v in ambiente.isolamento.veredictos:
        assert v.isolado is False
        assert any("indispon" in r.lower() for r in v.razoes)


def test_provisionar_caldera_failure_marks_abilities_not_loaded(data_dir: Path) -> None:
    """A Caldera failure keeps ``abilities_carregadas`` False (fail-closed load).
    _Requisitos: 9.5, 9.9_"""
    _seed_case(data_dir, _SLUG)
    containers = [_C1, _C2]
    prov = CuradoriaProvisionadorDeAmbiente(
        inspector=_isolated_inspector(containers),
        case_service=CaseService(data_dir=data_dir),
        caldera=FakeCaldera(fail=True),
        compose_executor=FakeComposeExecutor(),
        state_inspector=_healthy_state_inspector(containers),
    )

    ambiente = prov.provisionar(_params(containers))

    # A carga de Abilities falha ANTES do isolamento no pipeline (task 3.2), então
    # o pipeline retorna cedo em ``carregar_abilities`` e o isolamento não roda.
    assert ambiente.etapa_falha == "carregar_abilities"
    assert ambiente.abilities_carregadas is False
    assert ambiente.isolamento.aprovado is False
    assert ambiente.pronto is False


def test_provisionar_no_abilities_marks_not_loaded(data_dir: Path) -> None:
    """A case with no abilities loads nothing -> ``abilities_carregadas`` False.
    _Requisitos: 9.5, 9.9_"""
    _seed_case(data_dir, _SLUG, with_abilities=False, with_adversary=False)
    containers = [_C1, _C2]
    caldera = FakeCaldera()
    prov = CuradoriaProvisionadorDeAmbiente(
        inspector=_isolated_inspector(containers),
        case_service=CaseService(data_dir=data_dir),
        caldera=caldera,
        compose_executor=FakeComposeExecutor(),
        state_inspector=_healthy_state_inspector(containers),
    )

    ambiente = prov.provisionar(_params(containers))

    assert ambiente.abilities_carregadas is False
    assert caldera.calls == []  # Caldera never touched when there are no abilities


def test_provisionar_without_adversary_still_loads_abilities(data_dir: Path) -> None:
    """No adversary -> abilities still load (load_adversary skipped).
    _Requisitos: 9.5_"""
    _seed_case(data_dir, _SLUG, with_adversary=False)
    containers = [_C1, _C2]
    caldera = FakeCaldera()
    prov = CuradoriaProvisionadorDeAmbiente(
        inspector=_isolated_inspector(containers),
        case_service=CaseService(data_dir=data_dir),
        caldera=caldera,
        compose_executor=FakeComposeExecutor(),
        state_inspector=_healthy_state_inspector(containers),
    )

    ambiente = prov.provisionar(_params(containers))

    assert ambiente.abilities_carregadas is True
    assert caldera.calls == ["load_abilities"]
    # Without atomic_ordering the natural file order is used.
    loaded_ids = [a["ability_id"] for a in caldera.loaded_abilities]
    assert loaded_ids == [f"{_SLUG}-ab-1", f"{_SLUG}-ab-2"]


# ===========================================================================
# Task 3.4 — Pipeline ``provisionar``/``desprovisionar`` com TODOS os colaboradores
# fakes: caminho feliz conclui com os três pré-requisitos; cada etapa falhando
# isoladamente produz o ``etapa_falha`` correto e bloqueia a conclusão; nenhum
# acesso à rede pública (os fakes garantem — sem subprocess, sem daemon Docker,
# sem rede, sem IA).
#
# _Requisitos: 3.5, 4.4, 5.3, 5.5, 7.1, 9.1, 9.2
# ===========================================================================

# Etapas do pipeline na ordem do design (task 3.2), para asserir a sequência das
# linhas e os ``etapa_falha`` possíveis.
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
) -> CuradoriaProvisionadorDeAmbiente:
    """Monta a curadoria com TODOS os colaboradores fakes (sem rede, sem daemon).

    Semeia o caso curado no ``data_dir`` e injeta: um ``ComposeExecutor`` fake
    (sucesso por default), um ``ContainerStateInspector`` fake (todos saudáveis por
    default), um ``ContainerInspector`` fake isolado por default e um ``CalderaLoader``
    fake. Qualquer colaborador pode ser substituído para exercitar a falha de uma
    etapa isolada — as demais permanecem no caminho feliz.
    """
    _seed_case(
        data_dir, _SLUG, with_abilities=with_abilities, with_adversary=with_adversary
    )
    return CuradoriaProvisionadorDeAmbiente(
        inspector=inspector if inspector is not None else _isolated_inspector(containers),
        case_service=CaseService(data_dir=data_dir),
        caldera=caldera if caldera is not None else FakeCaldera(),
        compose_executor=compose if compose is not None else FakeComposeExecutor(),
        state_inspector=state if state is not None else _healthy_state_inspector(containers),
    )


# ---------------------------------------------------------------------------
# 1. Caminho feliz — as quatro etapas passam → pronto, três pré-requisitos ok,
#    on_log recebeu linhas de cada etapa na ordem, sem etapa_falha.
# ---------------------------------------------------------------------------


def test_provisionar_caminho_feliz_conclui_com_tres_prerequisitos(data_dir: Path) -> None:
    """Quatro etapas ok → ``pronto`` com saúde + Abilities + isolamento aprovados.
    _Requisitos: 5.3, 5.5, 7.1_"""
    containers = [_C1, _C2]
    compose = FakeComposeExecutor()
    prov = _curadoria(data_dir, containers, compose=compose)

    linhas: list[LinhaLog] = []
    ambiente = prov.provisionar(_params(containers), on_log=linhas.append)

    # Os três pré-requisitos passaram e o pipeline não teve retorno antecipado.
    assert ambiente.etapa_falha is None
    assert ambiente.mensagem_erro is None
    assert ambiente.saude.aprovado is True
    assert ambiente.abilities_carregadas is True
    assert ambiente.isolamento.aprovado is True
    assert ambiente.pronto is True
    assert ambiente.containers_alvo == _resolver(containers)

    # on_log recebeu ao menos uma linha de CADA etapa, na ordem do pipeline.
    etapas_vistas = [l.etapa for l in linhas]
    for etapa in _ETAPAS:
        assert etapa in etapas_vistas, f"nenhuma linha emitida para {etapa!r}"
    # A primeira ocorrência de cada etapa respeita a ordem subir → saúde →
    # abilities → isolamento.
    primeira_ocorrencia = [etapas_vistas.index(etapa) for etapa in _ETAPAS]
    assert primeira_ocorrencia == sorted(primeira_ocorrencia)

    # O compose foi efetivamente usado (o fake, não o real) com o compose do caso.
    assert compose.subir_calls == [ambiente.compose_file]


# ---------------------------------------------------------------------------
# 2. subir_conteineres falha (compose codigo != 0) → etapa_falha="subir_conteineres",
#    pipeline para (abilities não carregadas, isolamento não verificado).
# ---------------------------------------------------------------------------


def test_provisionar_falha_subir_conteineres_bloqueia_pipeline(data_dir: Path) -> None:
    """``up`` com ``codigo != 0`` → ``etapa_falha='subir_conteineres'`` e para.
    _Requisitos: 3.5, 7.1_"""
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

    ambiente = prov.provisionar(_params(containers))

    assert ambiente.etapa_falha == "subir_conteineres"
    assert ambiente.mensagem_erro is not None and ambiente.mensagem_erro != ""
    assert ambiente.pronto is False
    # O pipeline parou na 1ª etapa: saúde não rodou, Abilities não carregaram,
    # isolamento não foi verificado.
    assert state.consultados == []  # a etapa de saúde nem tocou o inspector
    assert caldera.calls == []  # as Abilities nunca foram carregadas
    assert ambiente.abilities_carregadas is False
    assert ambiente.saude.aprovado is False
    assert ambiente.isolamento.aprovado is False


# ---------------------------------------------------------------------------
# 3. aguardar_saude falha (um contêiner não-running/unhealthy) →
#    etapa_falha="aguardar_saude", pronto=False, para antes de abilities/isolamento.
# ---------------------------------------------------------------------------


def test_provisionar_falha_aguardar_saude_bloqueia_pipeline(data_dir: Path) -> None:
    """Um contêiner ``unhealthy`` → ``etapa_falha='aguardar_saude'`` e para.
    _Requisitos: 5.5, 7.1_"""
    containers = [_C1, _C2]
    # _C2 reporta unhealthy -> a etapa de saúde reprova.
    _r1, _r2 = _resolver([_C1, _C2])
    state = FakeStateInspector(
        {
            _r1: ContainerStateInfo(name=_r1, running=True, health="healthy"),
            _r2: ContainerStateInfo(name=_r2, running=True, health="unhealthy"),
        }
    )
    caldera = FakeCaldera()
    prov = _curadoria(data_dir, containers, state=state, caldera=caldera)

    ambiente = prov.provisionar(_params(containers))

    assert ambiente.etapa_falha == "aguardar_saude"
    assert ambiente.saude.aprovado is False
    assert [v.container for v in ambiente.saude.nao_saudaveis] == _resolver([_C2])
    assert ambiente.mensagem_erro is not None and _resolver([_C2])[0] in ambiente.mensagem_erro
    assert ambiente.pronto is False
    # Parou antes de carregar Abilities e verificar isolamento.
    assert caldera.calls == []
    assert ambiente.abilities_carregadas is False
    assert ambiente.isolamento.aprovado is False


def test_provisionar_falha_saude_container_nao_running(data_dir: Path) -> None:
    """Um contêiner fora de ``running`` também reprova ``aguardar_saude``.
    _Requisitos: 5.5, 7.1_"""
    containers = [_C1, _C2]
    _r1, _r2 = _resolver([_C1, _C2])
    state = FakeStateInspector(
        {
            _r1: ContainerStateInfo(name=_r1, running=True, health="healthy"),
            _r2: ContainerStateInfo(name=_r2, running=False, health=None),
        }
    )
    prov = _curadoria(data_dir, containers, state=state)

    ambiente = prov.provisionar(_params(containers))

    assert ambiente.etapa_falha == "aguardar_saude"
    assert ambiente.pronto is False
    assert [v.container for v in ambiente.saude.nao_saudaveis] == _resolver([_C2])


# ---------------------------------------------------------------------------
# 4. carregar_abilities falha (Caldera levanta) → etapa_falha="carregar_abilities",
#    pronto=False, isolamento não verificado.
# ---------------------------------------------------------------------------


def test_provisionar_falha_carregar_abilities_bloqueia_pipeline(data_dir: Path) -> None:
    """Caldera indisponível → ``etapa_falha='carregar_abilities'`` e para.
    _Requisitos: 4.4, 7.1_"""
    containers = [_C1, _C2]
    caldera = FakeCaldera(fail=True)
    prov = _curadoria(data_dir, containers, caldera=caldera)

    ambiente = prov.provisionar(_params(containers))

    assert ambiente.etapa_falha == "carregar_abilities"
    assert ambiente.abilities_carregadas is False
    assert ambiente.mensagem_erro is not None and ambiente.mensagem_erro != ""
    assert ambiente.pronto is False
    # Saúde passou (pré-requisito anterior) mas isolamento não foi verificado.
    assert ambiente.saude.aprovado is True
    assert ambiente.isolamento.aprovado is False
    # A etapa de fato tentou carregar no Caldera antes de falhar.
    assert caldera.calls == ["load_abilities"]


def test_provisionar_falha_carregar_abilities_caso_sem_abilities(data_dir: Path) -> None:
    """Caso sem Abilities → ``etapa_falha='carregar_abilities'`` (fail-closed).
    _Requisitos: 4.4, 7.1_"""
    containers = [_C1, _C2]
    caldera = FakeCaldera()
    prov = _curadoria(
        data_dir,
        containers,
        caldera=caldera,
        with_abilities=False,
        with_adversary=False,
    )

    ambiente = prov.provisionar(_params(containers))

    assert ambiente.etapa_falha == "carregar_abilities"
    assert ambiente.abilities_carregadas is False
    assert ambiente.pronto is False
    assert caldera.calls == []  # nada a carregar: o Caldera nem foi tocado


# ---------------------------------------------------------------------------
# 5. verificar_isolamento falha (contêiner em rede externa) →
#    etapa_falha="verificar_isolamento", pronto=False (saúde + abilities ok).
# ---------------------------------------------------------------------------


def test_provisionar_falha_verificar_isolamento_bloqueia_conclusao(data_dir: Path) -> None:
    """Contêiner em bridge externo → ``etapa_falha='verificar_isolamento'``.
    _Requisitos: 5.3, 5.5, 7.1_"""
    containers = [_C1, _C2]
    insp = _isolated_inspector(containers)
    # _C2 anexado a um bridge externo -> não isolado -> a etapa reprova.
    _r2 = _resolver([_C2])[0]
    insp.containers[_r2] = ContainerNetworkInfo(
        name=_r2, network_names=(_NET, "local-network"), dns=()
    )
    prov = _curadoria(data_dir, containers, inspector=insp)

    ambiente = prov.provisionar(_params(containers))

    assert ambiente.etapa_falha == "verificar_isolamento"
    assert ambiente.isolamento.aprovado is False
    assert [v.container for v in ambiente.isolamento.nao_isolados] == _resolver([_C2])
    assert ambiente.mensagem_erro is not None and ambiente.mensagem_erro != ""
    assert ambiente.pronto is False
    # Os dois pré-requisitos anteriores passaram (só o isolamento reprovou).
    assert ambiente.saude.aprovado is True
    assert ambiente.abilities_carregadas is True


# ---------------------------------------------------------------------------
# 6. desprovisionar sucesso (compose down codigo=0) → desprovisionado=True,
#    sem recursos remanescentes.
# ---------------------------------------------------------------------------


def test_desprovisionar_sucesso_sem_remanescentes(data_dir: Path) -> None:
    """``down`` com ``codigo=0`` → ``desprovisionado=True`` e sem remanescentes.
    _Requisitos: 9.1_"""
    containers = [_C1, _C2]
    compose = FakeComposeExecutor()
    prov = _curadoria(data_dir, containers, compose=compose)

    ambiente = prov.provisionar(_params(containers))
    assert ambiente.pronto is True

    linhas: list[LinhaLog] = []
    resultado = prov.desprovisionar(ambiente, on_log=linhas.append)

    assert resultado.desprovisionado is True
    assert resultado.recursos_remanescentes == []
    assert resultado.origem is TranslationSource.HUMAN_CURATION
    # O ``down`` do fake foi chamado com o compose do ambiente provisionado.
    assert compose.derrubar_calls == [ambiente.compose_file]
    # on_log recebeu as linhas do teardown.
    assert linhas and all(l.etapa == "desprovisionar" for l in linhas)


# ---------------------------------------------------------------------------
# 7. desprovisionar falha (codigo != 0) → desprovisionado=False,
#    recursos_remanescentes == containers_alvo.
# ---------------------------------------------------------------------------


def test_desprovisionar_falha_reporta_recursos_remanescentes(data_dir: Path) -> None:
    """``down`` com ``codigo != 0`` → ``desprovisionado=False`` com remanescentes.
    _Requisitos: 9.1_"""
    containers = [_C1, _C2]
    compose = FakeComposeExecutor(
        codigo_derrubar=1,
        linhas_derrubar=[("stderr", "Error: failed to remove network")],
    )
    prov = _curadoria(data_dir, containers, compose=compose)

    ambiente = prov.provisionar(_params(containers))
    resultado = prov.desprovisionar(ambiente)

    assert resultado.desprovisionado is False
    assert resultado.recursos_remanescentes == ambiente.containers_alvo == _resolver(containers)
    assert resultado.mensagem is not None and resultado.mensagem != ""


# ---------------------------------------------------------------------------
# 8. Nenhum acesso à rede pública: os fakes são de fato usados (compose/estado/
#    isolamento/caldera), provando que nem subprocess, nem daemon Docker, nem a
#    rede são tocados no caminho feliz nem no teardown.
# ---------------------------------------------------------------------------


def test_pipeline_usa_apenas_fakes_sem_rede_publica(data_dir: Path) -> None:
    """Todo o pipeline + teardown passam só pelos fakes — sem rede/daemon/subprocess.
    _Requisitos: 9.1, 9.2_"""
    containers = [_C1, _C2]
    compose = FakeComposeExecutor()
    state = _healthy_state_inspector(containers)
    caldera = FakeCaldera()
    inspector = _isolated_inspector(containers)
    prov = CuradoriaProvisionadorDeAmbiente(
        inspector=inspector,
        case_service=CaseService(data_dir=data_dir),
        caldera=caldera,
        compose_executor=compose,
        state_inspector=state,
    )
    _seed_case(data_dir, _SLUG)

    ambiente = prov.provisionar(_params(containers))
    prov.desprovisionar(ambiente)

    # Cada colaborador fake foi efetivamente exercitado — nenhum caminho caiu no
    # ``_get_compose_executor``/``inspect_*`` reais (que tocariam subprocess/daemon).
    assert compose.subir_calls == [ambiente.compose_file]
    assert compose.derrubar_calls == [ambiente.compose_file]
    assert state.consultados == _resolver(containers)  # a etapa de saúde usou o fake
    assert caldera.calls == ["load_abilities", "load_adversary"]
    # E o provisionador seguia referenciando os MESMOS fakes injetados (nunca
    # substituídos por um executor/estado real).
    assert prov._compose_executor is compose
    assert prov._state_inspector is state
    assert prov._caldera is caldera
    assert prov._inspector is inspector
