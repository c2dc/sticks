"""REST endpoints for casos and estágios (pipeline de cinco Estágios).

Wires the curated-case / stage endpoints from the design's "Backend — endpoints
REST renumerados (1..5)" section to the existing services. Handlers are kept
thin: each one constructs the relevant service with the *request-scoped*
SQLAlchemy session (via the ``get_db`` dependency) and delegates, returning the
services' already-typed Pydantic results as the response models.

Os Estágios seguem o esquema renumerado 1..5 (design `pipeline-cinco-estagios`):

====  ===========================  ==========================================
Nº    Estágio                      Serviço / costura
====  ===========================  ==========================================
1     Modelagem Estrutural         ``StageService.read_stage1`` / ``run_stage1``
2     Geração do DAG               ``DagService`` (``/dags``, ``/dags/{id}``,
                                    ``escolher-dag``; auto-conclusão curada)
3     Operacionalização            ``StageService.assemble_stage2`` + concluir
4     Provisionamento do Ambiente  ``EnvironmentStageService`` /
                                    ``CuradoriaProvisionadorDeAmbiente``
5     Execução da Campanha         ``StageService.assemble_stage3`` + Saída
====  ===========================  ==========================================

Mapeamento a partir do esquema anterior (spec `dag-estagio-multi-origem`, 1..4):
Geração do DAG era o Estágio 1 (agora 2), Modelagem era o 2 (agora 1), Tradução
Curada era o 3 (agora Operacionalização, mesmo número 3) e a Emulação era o 4
(agora dividida: Provisionamento=4 + Execução=5).

Endpoints implementados aqui:

* ``GET  /api/casos`` — os 8 casos curados com estado por Estágio (5 Estágios) e
  progresso agregado; isolamento de erro por Caso (Req. 5.1, 5.6).
* ``GET  /api/casos/{caso}`` — detalhe do Caso com estado dos 5 Estágios.
* ``GET  /api/casos/{caso}/estagio/1`` — Modelagem Estrutural (read-only).
* ``POST /api/casos/{caso}/estagio/1/executar`` — dispara a modelagem.
* ``GET  /api/casos/{caso}/estagio/2`` — Geração do DAG (estado + DAGs).
* ``GET  /api/casos/{caso}/dags`` — DAGs persistidos (backfill curado).
* ``GET  /api/casos/{caso}/dags/{dag_id}`` — DAG persistido (grafo + Mermaid).
* ``POST /api/casos/{caso}/estagio/2/escolher-dag`` — escolhe o DAG, conclui o 2.
* ``GET  /api/casos/{caso}/estagio/3`` — Operacionalização.
* ``POST /api/casos/{caso}/estagio/3/concluir`` — conclui a Operacionalização.
* ``GET  /api/casos/{caso}/estagio/4`` — Provisionamento (topologia + isolamento).
* ``GET  /api/casos/{caso}/estagio/5`` — Execução (Entrada + Saída acumulada).
* ``GET  /api/casos/{caso}/operacao`` — resultado por Ability + agregado.

O endpoint legado ``GET /api/casos/{caso}/dag`` (DagView somente-leitura) é
mantido inalterado. Os endpoints de emulação preview/execute do Estágio 5 são
adicionados na tarefa 8.2.

Conclusão automática de Estágios curados (Req. 9.1–9.4)
-------------------------------------------------------
Casos curados pré-concluem os Estágios 2 (DAG) e 4 (Ambiente) assim que o Estágio
anterior conclui. A auto-conclusão é disparada nas leituras de estado que o
wizard consome — ``GET /api/casos`` e ``GET /api/casos/{caso}`` — e também ao
abrir a tela do respectivo Estágio (``GET .../estagio/2`` e ``.../estagio/4``),
delegando às costuras ``DagService.auto_concluir_estagio2`` e
``EnvironmentStageService.auto_concluir_estagio4``. Ambas respeitam o gate
sequencial, são idempotentes/transacionais e **não** concluem sem o material
curado (DAG derivável / ambiente verificado isolado), de modo que repetir a
leitura nunca duplica estado nem conclui indevidamente.

_Requisitos: 8.7, 1.4, 5.4, 9.1, 9.2, 9.3, 9.4_
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.case_service import (
    AbilityData,
    AdversaryData,
    CaseLoadError,
    CaseService,
)
from app.models.enums import TranslationSource
from app.services.dag.dag_service import (
    DagDetalhe,
    DagResumo,
    DagService,
    DagView,
    EscolhaDagFalha,
)
from app.services.environment.stage_service import EnvironmentStageService
from app.services.environment.topologia_builder import HostView, NetworkView
from app.services.session.session_state_service import (
    OperationResultView,
    SessionStateService,
)
from app.services.stage.stage_service import (
    AggregateProgress,
    CaseStageStates,
    Stage1Result,
    Stage2Result,
    Stage3Input,
    StageService,
)

router = APIRouter(prefix="/api", tags=["casos"])


# ---------------------------------------------------------------------------
# Response models specific to the casos listing / detail endpoints
# ---------------------------------------------------------------------------


class CaseListItem(BaseModel):
    """One curated case in ``GET /api/casos`` (Req. 5.1, 5.6).

    Carries the case identity plus its per-stage state (``estados`` from
    :class:`CaseStageStates`). When the case failed to load (missing/malformed
    files, Req. 5.6) ``erro`` is populated with the descriptive per-case error
    and ``estados`` is ``None`` — the case is still listed so one bad case never
    drops the rest.
    """

    id: str
    nome: str
    origem_traducao: Optional[TranslationSource] = None
    estados: Optional[CaseStageStates] = None
    erro: Optional[CaseLoadError] = None


class CasesResponse(BaseModel):
    """``GET /api/casos`` payload — the 8 cases + aggregate progress (Req. 5.1)."""

    casos: list[CaseListItem] = Field(default_factory=list)
    progresso: AggregateProgress


class CaseExecutorView(BaseModel):
    """A concrete executor (command) of a curated Ability (case detail)."""

    name: Optional[str] = None
    platform: Optional[str] = None
    command: Optional[str] = None


class CaseAbilityView(BaseModel):
    """One curated Ability in the case-detail response (Req. 3.2)."""

    ability_id: str
    name: Optional[str] = None
    tactic: Optional[str] = None
    technique_name: Optional[str] = None
    technique_id: Optional[str] = None
    description: Optional[str] = None
    executors: list[CaseExecutorView] = Field(default_factory=list)


class CaseAdversaryView(BaseModel):
    """The curated Adversary in the case-detail response (Req. 3.5)."""

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    atomic_ordering: list[str] = Field(default_factory=list)


class CaseDetailResponse(BaseModel):
    """``GET /api/casos/{caso}`` payload — case detail.

    Abilities, Adversary, ordering and translation source. ``atomic_ordering``
    mirrors the adversary's ordering (empty when there is no Adversary).
    ``estados`` carries the per-stage state (same :class:`CaseStageStates` shape
    as ``GET /api/casos``, cobrindo os 5 Estágios) so the wizard can tell which
    stages are ``concluido`` and enable "Próximo"/Avançar.
    """

    id: str
    nome: str
    origem_traducao: TranslationSource
    estados: CaseStageStates
    abilities: list[CaseAbilityView] = Field(default_factory=list)
    adversary: Optional[CaseAdversaryView] = None
    atomic_ordering: list[str] = Field(default_factory=list)


class Stage2DagResponse(BaseModel):
    """``GET /api/casos/{caso}/estagio/2`` payload — Geração do DAG (Req. 8.7, 9.1).

    No esquema de cinco Estágios, a Geração do DAG é o **Estágio 2** (derivado da
    Modelagem Estrutural, Estágio 1). A tela do Estágio 2 lista os DAGs
    persistidos da Campanha e reflete o estado por Estágio (para habilitar o
    avanço). Para um Caso curado cujo Estágio 1 já concluiu, abrir esta tela
    dispara a auto-conclusão do Estágio 2 (``DagService.auto_concluir_estagio2``,
    idempotente, respeitando o gate), de modo que o ``estados`` devolvido já
    reflete o Estágio 2 ``concluido`` quando há DAG curado derivável; sem DAG
    derivável, o Estágio 2 permanece não concluído e o avanço fica bloqueado.
    """

    caso_id: str
    estados: CaseStageStates
    dags: list[DagResumo] = Field(default_factory=list)


class Stage4ProvisionamentoResponse(BaseModel):
    """``GET /api/casos/{caso}/estagio/4`` payload — Provisionamento (Req. 5.4, 5.6).

    No esquema de cinco Estágios, o Provisionamento do Ambiente é o **Estágio 4**
    (parte da antiga Emulação). A tela exibe a topologia do laboratório contido
    (hosts, redes internas, Mermaid) e o estado de isolamento. Read-only: monta a
    topologia e **verifica** o isolamento via
    :class:`~app.services.environment.CuradoriaProvisionadorDeAmbiente` sem
    iniciar/criar nada.

    Attributes:
        caso_id: Slug do Caso.
        nome: Nome de exibição do Caso.
        isolado: ``True`` sse todos os contêineres-alvo estão isolados — o
            ambiente está pronto para a Execução (Estágio 5).
        isolamento_indisponivel: ``True`` quando o isolamento **não pôde** ser
            verificado (Docker/daemon indisponível); a topologia ainda é exibida,
            mas o Estágio 4 não conclui e o Estágio 5 fica bloqueado (fail-closed).
        motivo: Razão pt-BR quando o ambiente não está isolado/verificado, para a
            tela exibir; ``None`` quando isolado.
        hosts: Hosts do laboratório (nome, container, papel, IPs fixos).
        redes: Redes internas (``internal: true``) e seus hosts.
        mermaid: Diagrama Mermaid da topologia (entregue direto ao
            ``MermaidDiagram`` do frontend).
        compose_file: Caminho do ``docker-compose.yml`` do laboratório (exibição).
        estados: Estado por Estágio (5 Estágios), já refletindo a auto-conclusão
            do Estágio 4 quando o ambiente é verificado isolado e o Estágio 3
            está concluído.
    """

    caso_id: str
    nome: str
    isolado: bool
    isolamento_indisponivel: bool = False
    motivo: Optional[str] = None
    hosts: list[HostView] = Field(default_factory=list)
    redes: list[NetworkView] = Field(default_factory=list)
    mermaid: str = ""
    compose_file: Optional[str] = None
    estados: CaseStageStates


class Stage5Response(BaseModel):
    """``GET /api/casos/{caso}/estagio/5`` payload — Execução (Req. 1.3, 1.7).

    No esquema de cinco Estágios, a Execução da Campanha é o **Estágio 5** (parte
    da antiga Emulação). Entrada = Adversary + curated Abilities
    (:class:`Stage3Input`, montada sem efeito colateral); Saída acumulada = the
    per-Ability operation results + aggregate persisted so far for the case
    (from :class:`SessionStateService.get_operation_results`). A execução em si
    não é disparada aqui (isso é a tarefa 8.2).
    """

    entrada: Stage3Input
    saida: list[OperationResultView] = Field(default_factory=list)


class OperationResponse(BaseModel):
    """``GET /api/casos/{caso}/operacao`` payload (Req. 4.1, 4.5).

    Per-Ability result + aggregate for every Operation of the case.
    """

    caso_id: str
    operacoes: list[OperationResultView] = Field(default_factory=list)


class DagListResponse(BaseModel):
    """``GET /api/casos/{caso}/dags`` payload — DAGs persistidos da Campanha (Req. 4.8).

    Espelha o contrato do design: o ``caso_id`` da Campanha e a lista ``dags`` de
    :class:`DagResumo` (``id``, ``origem``, ``identificador``, ``rotulo``,
    ``criado_em``) — sem o ``conteudo`` gráfico. A listagem é precedida de um
    backfill idempotente dos DAGs curados (``sincronizar_dags_curados``), de modo
    que um Caso curado sempre exibe ao menos o seu DAG de curadoria.
    """

    caso_id: str
    dags: list[DagResumo] = Field(default_factory=list)


class EscolherDagRequest(BaseModel):
    """Corpo do ``POST /api/casos/{caso}/estagio/2/escolher-dag`` (Req. 6.3).

    Carrega o ``dag_id`` do DAG persistido a ser escolhido para a execução. A
    escolha conclui o Estágio 2 (Geração do DAG) e libera o Estágio 3.
    """

    dag_id: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _known_slug_or_404(slug: str, case_service: CaseService) -> None:
    """Raise 404 when ``slug`` is not one of the 8 curated cases."""
    if slug not in case_service.list_cases():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Caso desconhecido: {slug!r}.",
        )


def _auto_concluir_curados(db: Session, case_service: CaseService, caso: str) -> None:
    """Dispara a auto-conclusão dos Estágios curados 2 (DAG) e 4 (Ambiente).

    Casos curados pré-concluem os Estágios 2 e 4 assim que o Estágio anterior
    conclui (Req. 9.1–9.4). Esta função é o ponto único onde as leituras de estado
    (``GET /api/casos`` / ``GET /api/casos/{caso}``) disparam ambas as
    auto-conclusões, mantendo a UI coerente sem exigir um passo explícito. Ambas
    as costuras:

    * respeitam o gate sequencial (Estágio 1 ``concluido`` para o 2; Estágio 3
      ``concluido`` para o 4) — nada ocorre enquanto o gate não liberar;
    * são idempotentes e transacionais (repetir a leitura não duplica estado);
    * **não** concluem sem o material curado (DAG derivável / ambiente verificado
      isolado), de modo que um Caso sem material permanece não concluído e o
      avanço fica bloqueado.

    A ordem importa: concluir o Estágio 2 pode liberar o 3 (via escolha/curadoria)
    e, em cadeia, habilitar o 4; aqui disparamos as duas de forma independente —
    cada chamada relê o estado atual, então na leitura seguinte o encadeamento se
    estabiliza. Erros de persistência/verificação são sinalizados nos resultados
    tipados das costuras (não levantam), então esta função é segura em qualquer
    caminho de leitura.
    """
    DagService(session=db, case_service=case_service).auto_concluir_estagio2(caso)
    EnvironmentStageService(db).auto_concluir_estagio4(caso)


def _ability_view(a: AbilityData) -> CaseAbilityView:
    return CaseAbilityView(
        ability_id=a.ability_id,
        name=a.name,
        tactic=a.tactic,
        technique_name=a.technique_name,
        technique_id=a.technique_id,
        description=a.description,
        executors=[
            CaseExecutorView(name=e.name, platform=e.platform, command=e.command)
            for e in a.executors
        ],
    )


def _adversary_view(adv: AdversaryData) -> CaseAdversaryView:
    return CaseAdversaryView(
        id=adv.id,
        name=adv.name,
        description=adv.description,
        atomic_ordering=list(adv.atomic_ordering),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/casos",
    response_model=CasesResponse,
    summary="Lista os 8 casos curados com estado por estágio e progresso agregado",
)
def listar_casos(db: Session = Depends(get_db)) -> CasesResponse:
    """List the 8 curated cases with per-stage state and aggregate progress.

    Uses :class:`CaseService` (per-case error isolation, Req. 5.6),
    :meth:`StageService.get_stage_states` (per-stage state, Req. 5.3) and
    :meth:`StageService.aggregate_progress` (Req. 5.5). A case that fails to load
    is reported with its descriptive error and still listed — one bad case never
    drops the rest (Req. 5.1, 5.6).

    Para cada Caso carregável, dispara a auto-conclusão dos Estágios curados 2
    (DAG) e 4 (Ambiente) antes de ler o estado, de modo que a listagem já reflita
    o pré-preenchimento curado (Req. 9.1–9.4). As costuras respeitam o gate,
    são idempotentes e não concluem sem material curado.
    """
    case_service = CaseService()
    stage_service = StageService(db, case_service=case_service)

    catalog = case_service.load_all_cases_safe()
    display_names = case_service.list_cases()

    casos: list[CaseListItem] = []
    for slug, nome in display_names.items():
        if slug in catalog.errors:
            casos.append(
                CaseListItem(id=slug, nome=nome, erro=catalog.errors[slug])
            )
            continue
        case = catalog.cases.get(slug)
        # Auto-conclusão dos Estágios curados 2 e 4 (idempotente, respeita o gate).
        _auto_concluir_curados(db, case_service, slug)
        casos.append(
            CaseListItem(
                id=slug,
                nome=nome,
                origem_traducao=(
                    case.origem_traducao if case else TranslationSource.HUMAN_CURATION
                ),
                estados=stage_service.get_stage_states(slug),
            )
        )

    progresso = stage_service.aggregate_progress()
    return CasesResponse(casos=casos, progresso=progresso)


@router.get(
    "/casos/{caso}",
    response_model=CaseDetailResponse,
    summary="Detalha um caso (Abilities, Adversary, ordenação, origem de tradução)",
)
def detalhar_caso(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> CaseDetailResponse:
    """Return a curated case's Abilities, Adversary, ordering and origin.

    A missing/malformed case surfaces a descriptive 422 error via the per-case
    load result (Req. 5.6). An unknown slug is a 404.

    Dispara a auto-conclusão dos Estágios curados 2 e 4 antes de ler o estado,
    de modo que o ``estados`` devolvido reflita o pré-preenchimento curado
    (Req. 9.1–9.4) — idempotente e respeitando o gate sequencial.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    result = case_service.try_load_case(caso)
    if not result.ok or result.case is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(result.error.detail if result.error else f"Falha ao ler {caso!r}."),
        )

    case = result.case
    # Auto-conclusão dos Estágios curados 2 e 4 antes de reler o estado por
    # Estágio, para que a UI veja o pré-preenchimento curado (Req. 9.1–9.4).
    _auto_concluir_curados(db, case_service, caso)
    estados = StageService(db, case_service=case_service).get_stage_states(caso)
    return CaseDetailResponse(
        id=case.id,
        nome=case.nome,
        origem_traducao=case.origem_traducao,
        estados=estados,
        abilities=[_ability_view(a) for a in case.abilities],
        adversary=_adversary_view(case.adversary) if case.adversary else None,
        atomic_ordering=list(case.atomic_ordering),
    )


@router.get(
    "/casos/{caso}/estagio/1",
    response_model=Stage1Result,
    summary="Entrada/Processamento/Saída do Estágio 1 (Modelagem Estrutural)",
)
def obter_estagio1(
    caso: str = Path(..., description="Slug do caso curado"),
    versao: Optional[str] = Query(
        None, description="Versão da base STIX selecionada no fluxo (ex.: 18.0)"
    ),
    db: Session = Depends(get_db),
) -> Stage1Result:
    """Return the Stage-1 (Modelagem Estrutural) result — READ-ONLY (Req. 1.3, 1.7).

    No esquema de cinco Estágios, a Modelagem Estrutural é o **Estágio 1**.
    Delegates to :meth:`StageService.read_stage1`, which reflects the PERSISTED
    state WITHOUT executing or persisting anything: opening the stage screen must
    never run the modeling nor mark it ``concluido``. The modeling runs only via
    ``POST /casos/{caso}/estagio/1/executar``.

    O query param opcional ``versao`` seleciona a base STIX usada para preencher a
    Entrada ``campanha_stix``; é repassado ao :class:`StageService`.
    Retrocompatível: sem ``versao`` o serviço aplica o fallback informativo
    (``versao_ausente``) sem levantar erro HTTP (Req. 5.2, 5.4, 3.2).
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)
    return StageService(db, case_service=case_service).read_stage1(caso, versao=versao)


@router.post(
    "/casos/{caso}/estagio/1/executar",
    response_model=Stage1Result,
    summary="Dispara a modelagem estrutural (Estágio 1)",
)
def executar_estagio1(
    caso: str = Path(..., description="Slug do caso curado"),
    versao: Optional[str] = Query(
        None, description="Versão da base STIX selecionada no fluxo (ex.: 18.0)"
    ),
    db: Session = Depends(get_db),
) -> Stage1Result:
    """Trigger Stage-1 (Modelagem Estrutural) structural modeling (Req. 1.3, 1.7).

    No esquema de cinco Estágios, a Modelagem Estrutural é o **Estágio 1**.
    Delegates to :meth:`StageService.run_stage1`, which persists the extracted
    elements and marks the stage ``concluido`` on success (or ``erro`` with the
    failing step on failure — reported in the typed result, not as HTTP error).

    O query param opcional ``versao`` seleciona a base STIX usada para preencher a
    Entrada ``campanha_stix``; é repassado ao :class:`StageService`.
    Retrocompatível: sem ``versao`` o serviço aplica o fallback informativo
    (``versao_ausente``) sem levantar erro HTTP (Req. 5.2, 5.4, 3.2).
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)
    return StageService(db, case_service=case_service).run_stage1(caso, versao=versao)


@router.get(
    "/casos/{caso}/estagio/2",
    response_model=Stage2DagResponse,
    summary="Geração do DAG (Estágio 2): estado por Estágio + DAGs persistidos",
)
def obter_estagio2(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> Stage2DagResponse:
    """Return the Stage-2 (Geração do DAG) view (Req. 8.7, 9.1).

    No esquema de cinco Estágios, a Geração do DAG é o **Estágio 2** (derivado da
    Modelagem Estrutural, Estágio 1 — antigo Estágio 1 do esquema 1..4). A tela
    lista os DAGs persistidos da Campanha e o estado por Estágio.

    Para um Caso curado cujo Estágio 1 concluiu, dispara a auto-conclusão do
    Estágio 2 (:meth:`DagService.auto_concluir_estagio2`): roda o backfill curado
    (sem rede/IA) e, havendo DAG derivável, marca o Estágio 2 ``concluido`` —
    idempotente e respeitando o gate. Sem DAG derivável o Estágio 2 permanece não
    concluído (botão "Gerar DAG" desabilitado, avanço bloqueado). Devolve a
    listagem resultante dos DAGs e o estado por Estágio já atualizado.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    dag_service = DagService(session=db, case_service=case_service)
    dag_service.auto_concluir_estagio2(caso)
    dags = dag_service.listar_dags(caso)
    estados = StageService(db, case_service=case_service).get_stage_states(caso)
    return Stage2DagResponse(caso_id=caso, estados=estados, dags=dags)


@router.get(
    "/casos/{caso}/estagio/3",
    response_model=Stage2Result,
    summary="Entrada (técnicas abstratas) e Saída (Abilities operacionais) do Estágio 3",
)
def obter_estagio3(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> Stage2Result:
    """Return the Stage-3 (Operacionalização) assembly (Req. 1.3, 1.7).

    No esquema de cinco Estágios, a Operacionalização é o **Estágio 3** (renome da
    antiga "Tradução Curada", que já era o Estágio 3 no esquema 1..4). Entrada =
    abstract techniques from the Modelagem Estrutural; Saída = operational
    Abilities/ordering/Adversary with the explicit ``origem_traducao`` (preservado
    como precedente de extensibilidade). Read-only (``assemble_stage2`` monta sem
    efeito colateral).
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)
    return StageService(db, case_service=case_service).assemble_stage2(caso)


@router.get(
    "/casos/{caso}/estagio/4",
    response_model=Stage4ProvisionamentoResponse,
    summary="Provisionamento do Ambiente (Estágio 4): topologia + isolamento",
)
def obter_estagio4(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> Stage4ProvisionamentoResponse:
    """Return the Stage-4 (Provisionamento do Ambiente) view (Req. 5.4, 5.6).

    No esquema de cinco Estágios, o Provisionamento do Ambiente é o **Estágio 4**
    (parte da antiga Emulação). A tela exibe a topologia do laboratório contido
    (hosts, redes internas, Mermaid) e o estado de isolamento.

    Fluxo: dispara a auto-conclusão do Estágio 4
    (:meth:`EnvironmentStageService.auto_concluir_estagio4`) — que respeita o gate
    (Estágio 3 ``concluido``), exige o material curado de ambiente e **verifica**
    o isolamento, concluindo o Estágio 4 somente quando isolado (idempotente). O
    resultado tipado traz a topologia e os sinais de isolamento, que mapeamos para
    o payload da tela; mesmo quando não isolado (ou não verificável), a topologia é
    exibida com a razão e o Estágio 5 permanece bloqueado (Req. 5.6). O estado por
    Estágio devolvido já reflete a (não-)conclusão do Estágio 4.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    env_service = EnvironmentStageService(db)
    auto = env_service.auto_concluir_estagio4(caso)
    # A topologia (sempre exibível) e os veredictos de isolamento vêm do
    # provisionador de curadoria; `preparar` é read-only (não cria/inicia nada).
    resultado = env_service.provisionador.preparar(caso)
    topo = resultado.topologia

    estados = StageService(db, case_service=case_service).get_stage_states(caso)
    return Stage4ProvisionamentoResponse(
        caso_id=caso,
        nome=topo.nome,
        isolado=resultado.isolado,
        isolamento_indisponivel=auto.isolamento_indisponivel,
        motivo=(None if resultado.isolado else (auto.mensagem or resultado.isolamento.summary())),
        hosts=topo.hosts,
        redes=topo.redes,
        mermaid=topo.mermaid,
        compose_file=topo.compose_file,
        estados=estados,
    )


@router.get(
    "/casos/{caso}/estagio/5",
    response_model=Stage5Response,
    summary="Entrada (Adversary + Abilities) e Saída acumulada do Estágio 5 (Execução)",
)
def obter_estagio5(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> Stage5Response:
    """Return the Stage-5 (Execução) Entrada + accumulated Saída (Req. 1.3, 1.7).

    No esquema de cinco Estágios, a Execução da Campanha é o **Estágio 5** (parte
    da antiga Emulação; antigo Estágio 4 do esquema 1..4). Entrada = Adversary +
    curated Abilities (assembled, no side effects). Saída acumulada = the
    per-Ability operation results persisted so far for the case. Execution itself
    is not triggered here (that is task 8.2).
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    stage_service = StageService(db, case_service=case_service)
    entrada = stage_service.assemble_stage3(caso)
    saida = SessionStateService(db).get_operation_results(caso)
    return Stage5Response(entrada=entrada, saida=saida)


@router.get(
    "/casos/{caso}/operacao",
    response_model=OperationResponse,
    summary="Resultado por Ability e agregado da Operação",
)
def obter_operacao(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> OperationResponse:
    """Return the per-Ability results + aggregate for the case (Req. 4.1, 4.5).

    Delegates to :meth:`SessionStateService.get_operation_results`.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)
    operacoes = SessionStateService(db).get_operation_results(caso)
    return OperationResponse(caso_id=caso, operacoes=operacoes)


@router.get(
    "/casos/{caso}/dag",
    response_model=DagView,
    summary="DAG estrutural do caso (nós, arestas e Mermaid) ou estado de erro",
)
def obter_dag(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> DagView:
    """Return the structural DAG view for a curated case (Req. 5.2, 5.3, 6.x).

    READ-ONLY and side-effect free: mirrors the invariant of
    :func:`obter_estagio1` / ``topologia.obter_topologia`` — opening the DAG
    screen only reads ``{slug}_dag.json`` through :class:`DagService`, never
    executes anything, performs no Docker/network access and invokes **no AI
    service** on any path (Req. 9.1, 9.2).

    An unknown slug is a 404, consistent with every other ``casos.py`` route
    (:func:`_known_slug_or_404` checks the 8 curated cases from
    ``CaseService.list_cases``). The "passa-direto" non-curated stub in
    :meth:`DagService.obter_dag` is intended for future STIX campaigns that are
    not among the curated 8; this endpoint is keyed by a curated-case ``{caso}``
    slug, so a slug outside the 8 is unknown here.

    A curated case whose DAG is merely missing/malformed is NOT a 404: the
    :class:`DagView` itself carries ``curado=True``/``pode_avancar=False``/``erro``
    so the UI can show the error state (Req. 5.3, 6.4). The handler stays thin —
    :meth:`DagService.obter_dag` returns a typed value (never raises), so the
    ``DagView`` fields express curado/stub/erro and FastAPI serializes it
    directly; no translation to the ``{"detail": ...}`` shape is needed.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)
    return DagService(case_service=case_service).obter_dag(caso)


# ---------------------------------------------------------------------------
# Estágio 2 — Geração do DAG: listagem e escolha de DAGs persistidos
# (Req. 4.8, 6.1, 6.2, 6.3). Handlers finos: constroem o DagService com a sessão
# do request (get_db) e delegam, mapeando o resultado tipado de escolher_dag para
# o código HTTP correspondente.
# ---------------------------------------------------------------------------


@router.get(
    "/casos/{caso}/dags",
    response_model=DagListResponse,
    summary="Lista os DAGs persistidos de uma Campanha (Estágio 2)",
)
def listar_dags(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> DagListResponse:
    """List the persisted DAGs of a Campanha (Req. 4.8, 6.1).

    Precede a listagem de um backfill idempotente dos DAGs curados
    (:meth:`DagService.sincronizar_dags_curados`), que lê ``data/dag/{slug}_dag.json``
    via ``CaseService`` — **sem rede e sem IA** (Req. 5.5, 9.1) — e persiste 1 DAG
    ``curadoria_humana`` por Caso curado se ainda não existir. Rodar o backfill
    "preguiçosamente" ao listar garante que um Caso curado sempre exibe ao menos o
    seu DAG, sem exigir um passo de sincronização explícito, e, por ser
    idempotente, não duplica em acessos repetidos. ``sincronizar_dags_curados`` já
    devolve a listagem resultante (:class:`DagResumo`), então o handler apenas a
    repassa.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)
    dags = DagService(session=db, case_service=case_service).sincronizar_dags_curados(
        caso
    )
    return DagListResponse(caso_id=caso, dags=dags)


@router.get(
    "/casos/{caso}/dags/{dag_id}",
    response_model=DagDetalhe,
    summary="Grafo + Mermaid de um DAG persistido (Estágio 2)",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "dag_id inexistente ou não pertencente ao Caso."
        },
    },
)
def obter_dag_persistido(
    caso: str = Path(..., description="Slug do caso curado"),
    dag_id: int = Path(..., description="Id do DAG persistido"),
    db: Session = Depends(get_db),
) -> DagDetalhe:
    """Return the graph + Mermaid of a persisted DAG (Req. 4.4).

    Delega a :meth:`DagService.obter_dag_persistido`, que reconstrói o grafo a
    partir do ``Dag.conteudo`` persistido e monta o Mermaid de forma consistente
    com :func:`obter_dag`. Um ``dag_id`` inexistente ou alheio ao Caso resulta em
    404 Not Found (o serviço devolve ``None``). O handler permanece fino: valida
    o slug curado (:func:`_known_slug_or_404`) e mapeia a ausência para 404.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    detalhe = DagService(session=db, case_service=case_service).obter_dag_persistido(
        caso, dag_id
    )
    if detalhe is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "motivo": "dag_nao_encontrado",
                "mensagem": (
                    "O DAG solicitado não existe ou não pertence a esta Campanha."
                ),
            },
        )
    return detalhe


@router.post(
    "/casos/{caso}/estagio/2/escolher-dag",
    response_model=CaseStageStates,
    summary="Escolhe um DAG para a execução, concluindo o Estágio 2",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "dag_id inexistente ou não pertencente ao Caso."
        },
        status.HTTP_409_CONFLICT: {
            "description": "Nenhum DAG persistido para a Campanha (avanço bloqueado)."
        },
    },
)
def escolher_dag(
    corpo: EscolherDagRequest,
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> CaseStageStates:
    """Escolhe um DAG para a execução, concluindo o Estágio 2 (Req. 6.2, 6.3).

    Delega a :meth:`DagService.escolher_dag`, que grava ``dag_escolhido_id`` no
    ``StageRun`` do Estágio 2 (Geração do DAG) e o conclui (progresso 100),
    liberando o Estágio 3 pelo gate sequencial. O resultado tipado
    (:class:`EscolhaDagResult`) é mapeado para o HTTP correspondente — o handler
    permanece fino, sem reimplementar regra de negócio:

    * **sucesso** → 200 com o estado por Estágio já com o Estágio 2 ``concluido``
      e o 3 liberado (:class:`CaseStageStates`, mesma forma de
      ``GET /api/casos/{caso}``);
    * **``SEM_DAG``** → 409 Conflict (a Campanha não tem nenhum DAG persistido —
      Req. 6.2);
    * **``DAG_NAO_ENCONTRADO``** → 404 Not Found (``dag_id`` inexistente/alheio ao
      Caso — Req. 6.3);
    * **``FALHA_PERSISTENCIA``** → 500 (falha ao persistir; o estado anterior é
      preservado pelo rollback do serviço).

    A mensagem pt-BR do resultado é propagada no ``detail`` do erro.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    dag_service = DagService(session=db, case_service=case_service)
    resultado = dag_service.escolher_dag(caso, corpo.dag_id)

    if resultado.sucesso:
        # Reler o estado por Estágio já com o Estágio 2 concluído e o 3 liberado.
        return StageService(db, case_service=case_service).get_stage_states(caso)

    # Recusa/falha tipada → código HTTP correspondente, com a mensagem pt-BR.
    _FALHA_PARA_STATUS = {
        EscolhaDagFalha.SEM_DAG: status.HTTP_409_CONFLICT,
        EscolhaDagFalha.DAG_NAO_ENCONTRADO: status.HTTP_404_NOT_FOUND,
        EscolhaDagFalha.FALHA_PERSISTENCIA: status.HTTP_500_INTERNAL_SERVER_ERROR,
    }
    codigo = _FALHA_PARA_STATUS.get(
        resultado.falha, status.HTTP_500_INTERNAL_SERVER_ERROR
    )
    raise HTTPException(
        status_code=codigo,
        detail={
            "motivo": resultado.falha.value if resultado.falha else "falha",
            "mensagem": resultado.mensagem or "A escolha do DAG não foi concluída.",
        },
    )


# ---------------------------------------------------------------------------
# Wizard de Execução Guiada — conclusão de estágio e reset de campanha
# (spec: wizard-execucao-guiada). Toda conclusão passa pelo
# SessionStateService.persist_stage_completion (atomicidade + preservação de
# estado já garantidas); o reset limpa o estado persistido para repetir a demo.
# ---------------------------------------------------------------------------


@router.post(
    "/casos/{caso}/estagio/3/concluir",
    response_model=CaseStageStates,
    summary="Conclui o Estágio 3 (Operacionalização) e destrava o Estágio 4",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "Estágio 2 ainda não concluído (bloqueio sequencial)."
        },
    },
)
def concluir_estagio3(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> CaseStageStates:
    """Persiste a conclusão do Estágio 3 (Operacionalização) — Req. 1.3, 1.4, 1.7.

    No esquema de cinco Estágios, a Operacionalização é o Estágio 3 (renome da
    antiga "Tradução Curada"). A operacionalização é curada (os artefatos já
    existem), então "avançar do 3" é, semanticamente, "concluir o 3". Respeita o
    bloqueio sequencial: só conclui o 3 se o **Estágio 2** (Geração do DAG) já
    estiver ``concluido`` (senão 409, sem persistir) — no esquema de cinco
    Estágios o Estágio anterior à Operacionalização é a Geração do DAG (Estágio
    2), de modo que o gate recai sobre o Estágio 2.
    """
    from app.models.enums import StageState  # local import: evita ciclo no topo

    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    stage_service = StageService(db, case_service=case_service)
    estados = stage_service.get_stage_states(caso)

    # Bloqueio sequencial (Req. 1.4): o Estágio 3 só conclui se o 2 está concluido.
    estagio2 = next((e for e in estados.estagios if e.estagio == 2), None)
    if estagio2 is None or estagio2.estado is not StageState.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "motivo": "bloqueio_sequencial",
                "mensagem": (
                    "O Estágio 3 não pode ser concluído enquanto o Estágio 2 "
                    "não estiver concluído."
                ),
            },
        )

    resultado = SessionStateService(db).persist_stage_completion(caso, 3)
    if not resultado.saved:
        # Falha de persistência: estado anterior preservado (Req. 3.6).
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "motivo": "falha_persistencia",
                "mensagem": resultado.mensagem or "Conclusão não foi salva.",
            },
        )

    # Reler o estado por estágio já com o 3 concluído (4 destravado).
    return stage_service.get_stage_states(caso)


@router.post(
    "/casos/{caso}/reset",
    response_model=CaseStageStates,
    summary="Reinicia o estado da campanha (para repetir a demonstração)",
)
def reset_caso(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> CaseStageStates:
    """Remove estágios concluídos e resultados de operação da campanha (Req. 6).

    Em uma única transação, apaga os ``StageRun`` da campanha e as ``Operation``
    (com seus ``AbilityResult`` e registros de auditoria) associadas, voltando a
    campanha ao estado inicial (Estágio 1 ``nao_iniciado``, 2/3/4/5 ``bloqueado``).
    """
    from app.models.domain import (  # local import: evita ciclo no topo
        AbilityResult,
        AuditLogEntry,
        Operation,
        StageRun,
    )

    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    # Ids das operações da campanha, para limpar resultados/auditoria dependentes.
    op_ids = [
        row[0]
        for row in db.query(Operation.id).filter(Operation.caso_id == caso).all()
    ]
    if op_ids:
        db.query(AbilityResult).filter(
            AbilityResult.operacao_id.in_(op_ids)
        ).delete(synchronize_session=False)
        db.query(AuditLogEntry).filter(
            AuditLogEntry.operacao_id.in_(op_ids)
        ).delete(synchronize_session=False)
    db.query(Operation).filter(Operation.caso_id == caso).delete(
        synchronize_session=False
    )
    db.query(StageRun).filter(StageRun.caso_id == caso).delete(
        synchronize_session=False
    )
    db.commit()

    return StageService(db, case_service=case_service).get_stage_states(caso)
