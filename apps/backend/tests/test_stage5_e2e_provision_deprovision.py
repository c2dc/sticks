"""Teste end-to-end (fakes) do laço provision → execute → deprovision (task 10.2).

Diferente de ``tests/test_completion_stage6.py`` — que isola a costura de
conclusão com um ``_RecordingProvisioner`` *fake* no nível de costura —, este
teste exercita a **cadeia completa de costuras** do ShadowRay com **o
provisioner REAL** (:class:`CurationEnvironmentProvisioner`) ligado a
**todos** os colaboradores *fakes* (sem Docker, sem Caldera, sem rede). Assim o
pipeline **real** de ``provision`` **e** o teardown **real** de
``deprovision`` (``compose down``) rodam ponta a ponta:

1. **Provision (ShadowRay)** — ``FakeComposeExecutor`` brings up the lab
   (``codigo_subir=0``), ``_healthy_state_inspector`` aprova a saúde,
   ``FakeCaldera`` carrega as Abilities ordenadas e ``_isolated_inspector``
   aprova o isolation. Os **três pré-requisitos** passam ⇒ ``environment.ready``.
2. **Executar (mock do runner)** — a costura de conclusão do Estágio 6
   (:class:`CompletionStage6Service`) roda a execução com um ``_FixedRunner``
   (``OperationRunResult`` fixo ``COMPLETED`` + ``FINISHED``), mede/gera o
   relatório num ``_MemorySink`` em memória.
3. **Deprovision** — the **same** real provisioner instance executes the
   ``deprovision`` (teardown via o mesmo ``FakeComposeExecutor``):
   - **success** (``codigo_derrubar=0``): sem ``remaining_resources``,
     relatório marca ``environment_deprovisioned=True`` e o Estágio 6 conclui;
   - **failure** (``codigo_derrubar=1``): ``deprovisioned=False`` reportando os
     ``remaining_resources`` (os ``target_containers`` do environment), relatório
     marca ``environment_deprovisioned=False`` e o Estágio 6 **não** conclui.

Reusa os *fakes*/helpers já existentes nodes módulos irmãos — ``FakeComposeExecutor``,
``FakeCaldera``, ``_isolated_inspector``, ``_healthy_state_inspector``,
``_seed_case``, ``_params`` e o *slug* ``shadowray`` de
``tests/test_provisioner_stage5.py``; e ``_FixedRunner``, ``_MemorySink``,
``_completed_result`` + a *fixture* de sessão SQLite efêmera de
``tests/test_completion_stage6.py`` — para não reimplementar a andaimaria. Nada
external é tocado: sem subprocess, sem daemon Docker, sem Caldera, sem rede, sem
IA.

_Requirements: 8.1, 8.2, 8.4_
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from sqlalchemy import select

from app.models.domain import StageRun
from app.models.enums import StageState, TranslationSource
from app.services.campaign_service import CaseService
from app.services.environment.completion import CompletionStage6Service
from app.services.environment.operationalization import EnvironmentPlatform
from app.services.environment.provisioner import CurationEnvironmentProvisioner
from app.services.operation.runner import OperationRunResult, RunOutcome
from app.services.stage.stage_service import STAGE_EXECUTION

# Reuso dos *fakes*/helpers do teste de ``provision`` (sem redefinir).
from tests.test_provisioner_stage5 import (
    _C1,
    _C2,
    _resolver,
    _SLUG,
    FakeCaldera,
    FakeComposeExecutor,
    _healthy_state_inspector,
    _isolated_inspector,
    _params,
    _seed_case,
)

# Reuso do runner/sink *fakes* e do helper de result da costura de conclusão.
# A *fixture* ``session`` (SQLite efêmera) e ``data_dir`` são autodescobertas
# pelo pytest a partir do módulo irmão quando importadas aqui.
from tests.test_completion_stage6 import (  # noqa: F401 — fixtures via import
    _FixedRunner,
    _MemorySink,
    _completed_result,
    data_dir,
    session,
)


_CONTAINERS = [_C1, _C2]


# ---------------------------------------------------------------------------
# Montagem compartilhada: o provisioner REAL com TODOS os colaboradores fakes
# ---------------------------------------------------------------------------


def _real_provisioner(
    data_dir: Path, compose: FakeComposeExecutor
) -> CurationEnvironmentProvisioner:
    """Monta a :class:`CurationEnvironmentProvisioner` REAL com os fakes.

    Semeia o campaign curated ``shadowray`` no ``data_dir`` e injeta o ``compose``
    *fake* (subir/derrubar), o inspector de saúde (todos saudáveis), o inspector
    de rede (todos isolados) e o Caldera *fake* — de modo que o pipeline real de
    ``provision`` e o teardown real de ``deprovision`` rodem sem tocar Docker,
    Caldera ou rede.
    """
    _seed_case(data_dir, _SLUG)
    return CurationEnvironmentProvisioner(
        inspector=_isolated_inspector(_CONTAINERS),
        case_service=CaseService(data_dir=data_dir),
        caldera=FakeCaldera(),
        compose_executor=compose,
        state_inspector=_healthy_state_inspector(_CONTAINERS),
    )


def _completion_service(
    session: Session,
    data_dir: Path,
    *,
    provisioner: CurationEnvironmentProvisioner,
    runner_result: OperationRunResult,
    sink: _MemorySink,
) -> CompletionStage6Service:
    """Monta a costura de conclusão do Estágio 6 com o provisioner REAL.

    O ``runner_factory`` devolve o ``_FixedRunner`` (mock do runner — um
    ``OperationRunResult`` fixo), o ``provisioner`` é a **mesma** instância
    real usada no ``provision`` (de modo que o teardown atinja o mesmo
    ``FakeComposeExecutor``), e o ``report_sink`` é o ``_MemorySink`` em
    memória.
    """
    return CompletionStage6Service(
        session,
        runner_factory=lambda _s: _FixedRunner(result=runner_result),  # type: ignore[arg-type,return-value]
        provisioner=provisioner,
        report_sink=sink,
        case_service=CaseService(data_dir=data_dir),
    )


# ---------------------------------------------------------------------------
# CENÁRIO A — caminho feliz: provision → três pré-requisitos ok → execute →
# deprovision com SUCESSO → Estágio 6 concluído (Req. 8.1, 8.2).
# ---------------------------------------------------------------------------


def test_e2e_provision_execute_deprovision_success(
    session: Session, data_dir: Path
) -> None:
    """Laço completo: compose up (provisão) + compose down (teardown) ok.

    O provisioner REAL sobe o laboratório, confirma os três pré-requisitos e
    conclui; a costura de conclusão executa o mock do runner (COMPLETED) e
    desprovisiona de fato pelo ``compose down`` do mesmo ``FakeComposeExecutor``.

    _Requirements: 8.1, 8.2_
    """
    compose = FakeComposeExecutor(codigo_subir=0, codigo_derrubar=0)
    prov = _real_provisioner(data_dir, compose)

    # --- Provision (ShadowRay): the THREE prerequisites pass.
    environment = prov.provision(_params(_CONTAINERS))
    assert environment.ready is True
    assert environment.health.approved is True  # pré-req. 1: contêineres saudáveis
    assert environment.loaded_abilities is True  # pré-req. 2: Abilities no Caldera
    assert environment.isolation.approved is True  # pré-req. 3: isolation approved
    assert environment.platform is EnvironmentPlatform.CONTAINER
    assert environment.origin is TranslationSource.HUMAN_CURATION
    assert environment.target_containers == _resolver(_CONTAINERS)
    # O ``provision`` real subiu o laboratório via o ``FakeComposeExecutor``.
    assert compose.up_calls == [environment.compose_file]
    assert compose.down_calls == []  # o teardown ainda não ocorreu

    # --- Executar (mock do runner) + deprovision com a MESMA instância real.
    sink = _MemorySink(ok=True)
    service = _completion_service(
        session,
        data_dir,
        provisioner=prov,
        runner_result=_completed_result(),
        sink=sink,
    )
    result = service.execute_and_complete(_SLUG, confirmed=True, environment=environment)

    # O mock do runner rodou uma única vez (COMPLETED projetado na execução).
    assert result.execution.result is RunOutcome.COMPLETED
    # O teardown REAL foi exercitado (``compose down`` via o mesmo fake).
    assert compose.down_calls == [environment.compose_file]
    # Successful deprovisioning, WITH NO remaining resources (Req. 8.1, 8.2).
    assert result.deprovisioning.deprovisioned is True
    assert result.deprovisioning.remaining_resources == []
    assert result.deprovisioning.origin is TranslationSource.HUMAN_CURATION
    # O relatório reflete o teardown e o Estágio 6 concluiu.
    assert result.report.environment_deprovisioned is True
    assert result.report_persisted is True
    assert result.completed is True

    state = (
        session.execute(
            select(StageRun)
            .where(StageRun.campaign_id == _SLUG)
            .where(StageRun.stage == STAGE_EXECUTION)
        )
        .scalars()
        .first()
    )
    assert state is not None and state.state is StageState.COMPLETED

    # Forma Req. 8.5: UMA campaign — um ``subir`` (provisão) e um ``derrubar``
    # (teardown) no mesmo laboratório curated.
    assert len(compose.up_calls) == 1
    assert len(compose.down_calls) == 1


# ---------------------------------------------------------------------------
# CENÁRIO B — failure de teardown: provisão ok, mas ``compose down`` failure →
# recursos remanescentes reportados → Estágio 6 NÃO conclui (Req. 8.4).
# ---------------------------------------------------------------------------


def test_e2e_teardown_failure_reports_remaining_resources(
    session: Session, data_dir: Path
) -> None:
    """Compose up ok, compose down failure → recursos remanescentes + não conclui.

    O ``FakeComposeExecutor`` sobe o laboratório (``codigo_subir=0``) mas failure ao
    derrubar (``codigo_derrubar=1``). O ``deprovision`` real mapeia a failure em
    ``deprovisioned=False`` com os ``target_containers`` como
    ``remaining_resources``; o relatório marca
    ``environment_deprovisioned=False`` e, mesmo com a execução COMPLETED, o
    Estágio 6 **não** conclui.

    _Requirements: 8.4_
    """
    compose = FakeComposeExecutor(
        codigo_subir=0,
        codigo_derrubar=1,
        linhas_derrubar=[("stderr", "Error response from daemon: conflict")],
    )
    prov = _real_provisioner(data_dir, compose)

    # --- Provision: the bring-up ok, the environment becomes ready.
    environment = prov.provision(_params(_CONTAINERS))
    assert environment.ready is True
    assert compose.up_calls == [environment.compose_file]

    # --- Executar (mock do runner COMPLETED) + teardown que FALHA.
    sink = _MemorySink(ok=True)
    service = _completion_service(
        session,
        data_dir,
        provisioner=prov,
        runner_result=_completed_result(),
        sink=sink,
    )
    result = service.execute_and_complete(_SLUG, confirmed=True, environment=environment)

    # O teardown REAL foi tentado (``compose down`` via o mesmo fake).
    assert compose.down_calls == [environment.compose_file]
    # Deprovisioning FAILED reporting the remaining resources (Req. 8.4).
    assert result.deprovisioning.deprovisioned is False
    assert result.deprovisioning.remaining_resources == environment.target_containers
    assert result.deprovisioning.message  # causa legível derivada do stderr
    # O relatório reflete que o environment NÃO foi deprovisioned.
    assert result.report.environment_deprovisioned is False
    # Mesmo com execução COMPLETED, a failure de teardown bloqueia a conclusão.
    assert result.completed is False

    state = (
        session.execute(
            select(StageRun)
            .where(StageRun.campaign_id == _SLUG)
            .where(StageRun.stage == STAGE_EXECUTION)
        )
        .scalars()
        .first()
    )
    # O Estágio 6 NÃO foi concluído (nenhuma transição COMPLETED persistida).
    assert state is None or state.state is not StageState.COMPLETED
