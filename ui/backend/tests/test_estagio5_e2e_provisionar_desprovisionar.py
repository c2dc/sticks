"""Teste end-to-end (fakes) do laço provisionar → executar → desprovisionar (task 10.2).

Diferente de ``tests/test_conclusao_estagio6.py`` — que isola a costura de
conclusão com um ``_RecordingProvisioner`` *fake* no nível de costura —, este
teste exercita a **cadeia completa de costuras** do ShadowRay com **o
provisionador REAL** (:class:`CuradoriaProvisionadorDeAmbiente`) ligado a
**todos** os colaboradores *fakes* (sem Docker, sem Caldera, sem rede). Assim o
pipeline **real** de ``provisionar`` **e** o teardown **real** de
``desprovisionar`` (``compose down``) rodam ponta a ponta:

1. **Provisionar (ShadowRay)** — ``FakeComposeExecutor`` sobe o laboratório
   (``codigo_subir=0``), ``_healthy_state_inspector`` aprova a saúde,
   ``FakeCaldera`` carrega as Abilities ordenadas e ``_isolated_inspector``
   aprova o isolamento. Os **três pré-requisitos** passam ⇒ ``ambiente.pronto``.
2. **Executar (mock do runner)** — a costura de conclusão do Estágio 6
   (:class:`ConclusaoEstagio6Service`) roda a execução com um ``_FixedRunner``
   (``OperationRunResult`` fixo ``COMPLETED`` + ``FINISHED``), mede/gera o
   relatório num ``_MemorySink`` em memória.
3. **Desprovisionar** — a **mesma** instância real do provisionador executa o
   ``desprovisionar`` (teardown via o mesmo ``FakeComposeExecutor``):
   - **sucesso** (``codigo_derrubar=0``): sem ``recursos_remanescentes``,
     relatório marca ``ambiente_desprovisionado=True`` e o Estágio 6 conclui;
   - **falha** (``codigo_derrubar=1``): ``desprovisionado=False`` reportando os
     ``recursos_remanescentes`` (os ``containers_alvo`` do ambiente), relatório
     marca ``ambiente_desprovisionado=False`` e o Estágio 6 **não** conclui.

Reusa os *fakes*/helpers já existentes nos módulos irmãos — ``FakeComposeExecutor``,
``FakeCaldera``, ``_isolated_inspector``, ``_healthy_state_inspector``,
``_seed_case``, ``_params`` e o *slug* ``shadowray`` de
``tests/test_provisionador_estagio5.py``; e ``_FixedRunner``, ``_MemorySink``,
``_completed_result`` + a *fixture* de sessão SQLite efêmera de
``tests/test_conclusao_estagio6.py`` — para não reimplementar a andaimaria. Nada
externo é tocado: sem subprocess, sem daemon Docker, sem Caldera, sem rede, sem
IA.

_Requisitos: 8.1, 8.2, 8.4_
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from sqlalchemy import select

from app.models.domain import StageRun
from app.models.enums import StageState, TranslationSource
from app.services.case_service import CaseService
from app.services.environment.conclusao import ConclusaoEstagio6Service
from app.services.environment.operacionalizacao import PlataformaAmbiente
from app.services.environment.provisionador import CuradoriaProvisionadorDeAmbiente
from app.services.operation.runner import OperationRunResult, RunOutcome
from app.services.stage.stage_service import ESTAGIO_EXECUCAO

# Reuso dos *fakes*/helpers do teste de ``provisionar`` (sem redefinir).
from tests.test_provisionador_estagio5 import (
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

# Reuso do runner/sink *fakes* e do helper de resultado da costura de conclusão.
# A *fixture* ``session`` (SQLite efêmera) e ``data_dir`` são autodescobertas
# pelo pytest a partir do módulo irmão quando importadas aqui.
from tests.test_conclusao_estagio6 import (  # noqa: F401 — fixtures via import
    _FixedRunner,
    _MemorySink,
    _completed_result,
    data_dir,
    session,
)


_CONTAINERS = [_C1, _C2]


# ---------------------------------------------------------------------------
# Montagem compartilhada: o provisionador REAL com TODOS os colaboradores fakes
# ---------------------------------------------------------------------------


def _provisionador_real(
    data_dir: Path, compose: FakeComposeExecutor
) -> CuradoriaProvisionadorDeAmbiente:
    """Monta a :class:`CuradoriaProvisionadorDeAmbiente` REAL com os fakes.

    Semeia o Caso curado ``shadowray`` no ``data_dir`` e injeta o ``compose``
    *fake* (subir/derrubar), o inspector de saúde (todos saudáveis), o inspector
    de rede (todos isolados) e o Caldera *fake* — de modo que o pipeline real de
    ``provisionar`` e o teardown real de ``desprovisionar`` rodem sem tocar Docker,
    Caldera ou rede.
    """
    _seed_case(data_dir, _SLUG)
    return CuradoriaProvisionadorDeAmbiente(
        inspector=_isolated_inspector(_CONTAINERS),
        case_service=CaseService(data_dir=data_dir),
        caldera=FakeCaldera(),
        compose_executor=compose,
        state_inspector=_healthy_state_inspector(_CONTAINERS),
    )


def _servico_conclusao(
    session: Session,
    data_dir: Path,
    *,
    provisionador: CuradoriaProvisionadorDeAmbiente,
    runner_result: OperationRunResult,
    sink: _MemorySink,
) -> ConclusaoEstagio6Service:
    """Monta a costura de conclusão do Estágio 6 com o provisionador REAL.

    O ``runner_factory`` devolve o ``_FixedRunner`` (mock do runner — um
    ``OperationRunResult`` fixo), o ``provisionador`` é a **mesma** instância
    real usada no ``provisionar`` (de modo que o teardown atinja o mesmo
    ``FakeComposeExecutor``), e o ``relatorio_sink`` é o ``_MemorySink`` em
    memória.
    """
    return ConclusaoEstagio6Service(
        session,
        runner_factory=lambda _s: _FixedRunner(result=runner_result),  # type: ignore[arg-type,return-value]
        provisionador=provisionador,
        relatorio_sink=sink,
        case_service=CaseService(data_dir=data_dir),
    )


# ---------------------------------------------------------------------------
# CENÁRIO A — caminho feliz: provisionar → três pré-requisitos ok → executar →
# desprovisionar com SUCESSO → Estágio 6 concluído (Req. 8.1, 8.2).
# ---------------------------------------------------------------------------


def test_e2e_provisionar_executar_desprovisionar_sucesso(
    session: Session, data_dir: Path
) -> None:
    """Laço completo: compose up (provisão) + compose down (teardown) ok.

    O provisionador REAL sobe o laboratório, confirma os três pré-requisitos e
    conclui; a costura de conclusão executa o mock do runner (COMPLETED) e
    desprovisiona de fato pelo ``compose down`` do mesmo ``FakeComposeExecutor``.

    _Requisitos: 8.1, 8.2_
    """
    compose = FakeComposeExecutor(codigo_subir=0, codigo_derrubar=0)
    prov = _provisionador_real(data_dir, compose)

    # --- Provisionar (ShadowRay): os TRÊS pré-requisitos passam.
    ambiente = prov.provisionar(_params(_CONTAINERS))
    assert ambiente.pronto is True
    assert ambiente.saude.aprovado is True  # pré-req. 1: contêineres saudáveis
    assert ambiente.abilities_carregadas is True  # pré-req. 2: Abilities no Caldera
    assert ambiente.isolamento.aprovado is True  # pré-req. 3: isolamento aprovado
    assert ambiente.plataforma is PlataformaAmbiente.CONTAINER
    assert ambiente.origem is TranslationSource.HUMAN_CURATION
    assert ambiente.containers_alvo == _resolver(_CONTAINERS)
    # O ``provisionar`` real subiu o laboratório via o ``FakeComposeExecutor``.
    assert compose.subir_calls == [ambiente.compose_file]
    assert compose.derrubar_calls == []  # o teardown ainda não ocorreu

    # --- Executar (mock do runner) + desprovisionar com a MESMA instância real.
    sink = _MemorySink(ok=True)
    servico = _servico_conclusao(
        session,
        data_dir,
        provisionador=prov,
        runner_result=_completed_result(),
        sink=sink,
    )
    result = servico.executar_e_concluir(_SLUG, confirmado=True, ambiente=ambiente)

    # O mock do runner rodou uma única vez (COMPLETED projetado na execução).
    assert result.execucao.resultado is RunOutcome.COMPLETED
    # O teardown REAL foi exercitado (``compose down`` via o mesmo fake).
    assert compose.derrubar_calls == [ambiente.compose_file]
    # Desprovisionamento bem-sucedido, SEM recursos remanescentes (Req. 8.1, 8.2).
    assert result.desprovisionamento.desprovisionado is True
    assert result.desprovisionamento.recursos_remanescentes == []
    assert result.desprovisionamento.origem is TranslationSource.HUMAN_CURATION
    # O relatório reflete o teardown e o Estágio 6 concluiu.
    assert result.relatorio.ambiente_desprovisionado is True
    assert result.relatorio_persistido is True
    assert result.concluido is True

    estado = (
        session.execute(
            select(StageRun)
            .where(StageRun.caso_id == _SLUG)
            .where(StageRun.estagio == ESTAGIO_EXECUCAO)
        )
        .scalars()
        .first()
    )
    assert estado is not None and estado.estado is StageState.COMPLETED

    # Forma Req. 8.5: UMA campanha — um ``subir`` (provisão) e um ``derrubar``
    # (teardown) no mesmo laboratório curado.
    assert len(compose.subir_calls) == 1
    assert len(compose.derrubar_calls) == 1


# ---------------------------------------------------------------------------
# CENÁRIO B — falha de teardown: provisão ok, mas ``compose down`` falha →
# recursos remanescentes reportados → Estágio 6 NÃO conclui (Req. 8.4).
# ---------------------------------------------------------------------------


def test_e2e_falha_de_teardown_reporta_recursos_remanescentes(
    session: Session, data_dir: Path
) -> None:
    """Compose up ok, compose down falha → recursos remanescentes + não conclui.

    O ``FakeComposeExecutor`` sobe o laboratório (``codigo_subir=0``) mas falha ao
    derrubar (``codigo_derrubar=1``). O ``desprovisionar`` real mapeia a falha em
    ``desprovisionado=False`` com os ``containers_alvo`` como
    ``recursos_remanescentes``; o relatório marca
    ``ambiente_desprovisionado=False`` e, mesmo com a execução COMPLETED, o
    Estágio 6 **não** conclui.

    _Requisitos: 8.4_
    """
    compose = FakeComposeExecutor(
        codigo_subir=0,
        codigo_derrubar=1,
        linhas_derrubar=[("stderr", "Error response from daemon: conflict")],
    )
    prov = _provisionador_real(data_dir, compose)

    # --- Provisionar: a subida ok, o ambiente fica pronto.
    ambiente = prov.provisionar(_params(_CONTAINERS))
    assert ambiente.pronto is True
    assert compose.subir_calls == [ambiente.compose_file]

    # --- Executar (mock do runner COMPLETED) + teardown que FALHA.
    sink = _MemorySink(ok=True)
    servico = _servico_conclusao(
        session,
        data_dir,
        provisionador=prov,
        runner_result=_completed_result(),
        sink=sink,
    )
    result = servico.executar_e_concluir(_SLUG, confirmado=True, ambiente=ambiente)

    # O teardown REAL foi tentado (``compose down`` via o mesmo fake).
    assert compose.derrubar_calls == [ambiente.compose_file]
    # Desprovisionamento FALHOU reportando os recursos remanescentes (Req. 8.4).
    assert result.desprovisionamento.desprovisionado is False
    assert result.desprovisionamento.recursos_remanescentes == ambiente.containers_alvo
    assert result.desprovisionamento.mensagem  # causa legível derivada do stderr
    # O relatório reflete que o ambiente NÃO foi desprovisionado.
    assert result.relatorio.ambiente_desprovisionado is False
    # Mesmo com execução COMPLETED, a falha de teardown bloqueia a conclusão.
    assert result.concluido is False

    estado = (
        session.execute(
            select(StageRun)
            .where(StageRun.caso_id == _SLUG)
            .where(StageRun.estagio == ESTAGIO_EXECUCAO)
        )
        .scalars()
        .first()
    )
    # O Estágio 6 NÃO foi concluído (nenhuma transição COMPLETED persistida).
    assert estado is None or estado.estado is not StageState.COMPLETED
