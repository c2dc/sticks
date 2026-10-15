"""REST endpoints for casos and estágios (pipeline de seis Estágios).

Wires the curated-case / stage endpoints from the design's "Backend — endpoints
REST (1..6)" section to the existing services. Handlers are kept thin: each one
constructs the relevant service with the *request-scoped* SQLAlchemy session
(via the ``get_db`` dependency) and delegates, returning the services'
already-typed Pydantic results as the response models.

Os Estágios seguem o esquema de seis Estágios (design `pipeline-seis-estagios`):

====  ===============================  ======================================
Nº    Estágio                          Serviço / costura
====  ===============================  ======================================
1     Seleção da base STIX             ``StageService.read_stage1`` /
                                       ``run_stage1``
2     Seleção da campanha              ``Estagio2Service`` (lista + extração)
3     Geração do DAG                   ``DagService`` (``/dags``,
                                       ``/dags/{id}``, ``escolher-dag``;
                                       auto-conclusão curada)
4     Operacionalização                ``StageService`` (Abilities +
                                       parâmetros de ambiente)
5     Criação do ambiente de           ``EnvironmentStageService`` /
      simulação                        ``CuradoriaProvisionadorDeAmbiente``
6     Execução + medição + relatório   ``ExecucaoStageService`` /
                                       ``MedicaoService`` /
                                       ``ConclusaoEstagio6Service``
====  ===============================  ======================================

Endpoints implementados aqui:

* ``GET  /api/casos`` — os casos curados com estado por Estágio (6 Estágios) e
  progresso agregado; isolamento de erro por Caso (Req. 5.1, 5.6).
* ``GET  /api/casos/{caso}`` — detalhe do Caso com estado dos 6 Estágios.
* ``GET  /api/casos/{caso}/estagio/1`` — Seleção da base STIX (read-only).
* ``POST /api/casos/{caso}/estagio/1/executar`` — dispara a modelagem.
* ``GET  /api/casos/{caso}/estagio/2`` — Seleção da campanha (lista + atributos).
* ``POST /api/casos/{caso}/estagio/2/selecionar`` — seleciona campanha, extrai.
* ``GET  /api/casos/{caso}/estagio/3`` — Geração do DAG (DagView/Mermaid).
* ``GET  /api/casos/{caso}/dags`` — DAGs persistidos (backfill curado).
* ``GET  /api/casos/{caso}/dags/{dag_id}`` — DAG persistido (grafo + Mermaid).
* ``POST /api/casos/{caso}/estagio/3/escolher-dag`` — escolhe o DAG, conclui o 3.
* ``POST /api/casos/{caso}/estagio/3/concluir`` — (transitório) conclui a Geração do DAG.
* ``GET  /api/casos/{caso}/estagio/4`` — Operacionalização (Abilities + parâmetros).
* ``POST /api/casos/{caso}/estagio/4/concluir`` — conclui a Operacionalização.
* ``GET  /api/casos/{caso}/estagio/5`` — Criação do ambiente (topologia + isolamento).
* ``POST /api/casos/{caso}/estagio/5/provisionar`` — garante/inicia o provisionamento
  (idempotente); delega à costura ativa ``EnvironmentStageService.provisionar_estagio5``.
* ``GET  /api/casos/{caso}/estagio/5/logs`` — buffer corrente de linhas de log do
  provisionamento (read-only; recuperação do terminal ao vivo — Req. 2.5).
* ``GET  /api/casos/{caso}/estagio/6`` — Execução (Entrada + Saída acumulada).
* ``POST /api/casos/{caso}/estagio/6/execucao/preview`` — pré-visualiza a execução.
* ``POST /api/casos/{caso}/estagio/6/execucao`` — executa, mede e conclui (desprovisiona).
* ``GET  /api/casos/{caso}/relatorio`` — relatório de execução do Estágio 6.
* ``GET  /api/casos/{caso}/operacao`` — resultado por Ability + agregado.

O endpoint legado ``GET /api/casos/{caso}/dag`` (DagView somente-leitura) é
mantido inalterado.

Conclusão automática de Estágios curados (Req. 9.1-9.4)
-------------------------------------------------------
Casos curados pré-concluem os Estágios 3 (DAG) e 5 (Ambiente) assim que o Estágio
anterior conclui. A auto-conclusão é disparada nas leituras de estado que o
wizard consome — ``GET /api/casos`` e ``GET /api/casos/{caso}`` — e também ao
abrir a tela do respectivo Estágio (``GET .../estagio/3`` e ``.../estagio/5``),
delegando às costuras ``DagService.auto_concluir_estagio2`` (opera sobre
``ESTAGIO_GERACAO_DAG == 3``, nome histórico) e
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
from app.models.enums import OperationState, TranslationSource
# Dependências de injeção + modelos/mapeamento do preview reusados do router de
# emulação (task 11.2): o Estágio 6 (Execução) reusa a mesma *factory* de runner
# (cliente Caldera + verificador de isolamento injetáveis) e a mesma apresentação
# do preview, sem duplicá-los. ``api.emulacao`` é um módulo-folha (não importa
# ``api.casos``), então o import não cria ciclo.
from app.api.emulacao import (
    EmulationPreviewResponse,
    RunnerFactory,
    _preview_view,
    get_runner_factory,
)
from app.services.caldera import CalderaApiError, CalderaUnavailable
from app.services.environment.conclusao import (
    ConclusaoEstagio6Result,
    ConclusaoEstagio6Service,
)
from app.services.environment.execucao import ExecucaoStageService
from app.services.environment.medicao import MedicaoService, ResultadoAbility
from app.services.environment.log_buffer import (
    ProvisioningLogBuffer,
    get_log_buffer,
)
from app.services.environment.provisionador import (
    AmbienteProvisionado,
    CuradoriaProvisionadorDeAmbiente,
)
from app.services.environment.saude import ResumoSaude
from app.services.environment.relatorio import RelatorioExecucao
from app.services.operation.runner import RunOutcome
from app.services.dag.dag_service import (
    DagDetalhe,
    DagResumo,
    DagService,
    DagView,
    EscolhaDagFalha,
)
from app.services.environment.stage_service import EnvironmentStageService
from app.services.environment.topologia_builder import HostView, NetworkView
# Importa a costura de operacionalização (Estágio 4) e os seus DTOs direto dos
# submódulos, não do pacote ``app.services.environment``: há um ciclo de import
# conhecido ao reexportar pela raiz do pacote, então os submódulos são a fonte
# direta (mesmo precedente do Estágio 2 em ``app.services.stix``).
from app.services.environment.operacionalizacao import ParametrosDeAmbiente
from app.services.environment.operacionalizador import (
    CuradoriaOperacionalizador,
    OperacionalizacaoProduzida,
)
from app.services.session.session_state_service import (
    OperationResultView,
    SessionStateService,
)
from app.services.stage.stage_service import (
    ESTAGIO_GERACAO_DAG,
    ESTAGIO_OPERACIONALIZACAO,
    AggregateProgress,
    CaseStageStates,
    LinhaLogView,
    Stage1Result,
    Stage2Output,
    Stage3AbilityResult,
    Stage3Input,
    StageService,
)
from app.services.stix.estagio1_service import (
    Estagio1Service,
    ResultadoEstagio1,
)
# Importa o serviço do Estágio 2 diretamente do submódulo (não do pacote
# ``app.services.stix``): há um ciclo de import conhecido ao reexportar o
# serviço pela raiz do pacote, então o submódulo é a fonte direta.
from app.services.stix.estagio2_service import (
    CampanhaListada,
    Estagio2Service,
    ResultadoSelecaoCampanha,
)
from app.services.stix.stix_service import (
    BaseStixInfo,
    StixDownloadError,
    StixFileError,
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
    as ``GET /api/casos``, cobrindo os 6 Estágios) so the wizard can tell which
    stages are ``concluido`` and enable "Próximo"/Avançar.
    """

    id: str
    nome: str
    origem_traducao: TranslationSource
    estados: CaseStageStates
    abilities: list[CaseAbilityView] = Field(default_factory=list)
    adversary: Optional[CaseAdversaryView] = None
    atomic_ordering: list[str] = Field(default_factory=list)


class Estagio2Response(BaseModel):
    """``GET /api/casos/{caso}/estagio/2`` payload — Seleção da campanha (Req. 6.2, 6.4).

    No esquema de **seis** Estágios, o Estágio 2 é a "Seleção da campanha": a tela
    lista as campanhas contidas no bundle selecionado no Estágio 1 e exibe os
    atributos extraídos da campanha quando ela já foi selecionada (Req. 6.2, 6.4).
    A Geração do DAG passou a ser o Estágio 3 (a renumeração da rota do DAG é
    feita na tarefa 4.1).

    Esta visão é **read-only**: monta a listagem via
    :meth:`Estagio2Service.listar_campanhas` e reflete o estado da seleção via
    :meth:`Estagio2Service.ler` (identidade/aliases + técnicas, derivados sem
    persistir) — abrir a tela nunca executa a extração nem conclui o Estágio.

    Attributes:
        caso_id: Slug do Caso/campanha selecionado.
        versao: Versão do bundle STIX (Entrada do Estágio 1/2, Req. 6.1).
        campanhas: Campanhas do bundle, cada uma com ``curada``/``slug_curado``/
            ``selecionavel`` (as 8 curadas são selecionáveis na versão 18, Req.
            6.2, 6.5).
        selecao: A visão read-only da seleção corrente — identidade/aliases,
            técnicas extraídas e o veredito de conclusão (``concluido``); as
            técnicas só aparecem quando o Estágio já foi concluído (Req. 6.3,
            6.4, 6.6).
    """

    caso_id: str
    versao: Optional[str] = None
    campanhas: list[CampanhaListada] = Field(default_factory=list)
    selecao: ResultadoSelecaoCampanha


class SelecionarCampanhaRequest(BaseModel):
    """Corpo do ``POST /api/casos/{caso}/estagio/2/selecionar`` (Req. 6.3).

    Carrega o ``slug`` curado da campanha escolhida na lista e, opcionalmente, a
    ``versao`` do bundle STIX selecionada no Estágio 1 (Entrada do Estágio 2,
    Req. 6.1). Quando ``versao`` é omitida, aplica-se a versão curada padrão.
    """

    slug: str
    versao: Optional[str] = None


class Estagio4Response(BaseModel):
    """``GET /api/casos/{caso}/estagio/4`` payload — Operacionalização (Req. 8.4, 8.7).

    No esquema de **seis** Estágios, o Estágio 4 é a "Operacionalização": a Saída
    são **dois artefatos tipados separados** (Req. 8.4, 8.7) — as **Abilities
    ordenadas pelo DAG** (reuso de ``Stage2*``/``CaseService``) e os
    **``ParametrosDeAmbiente``** derivados dos nós do DAG curado (hosts/serviços/
    ``network_context``, destinados ao Estágio 5). A Entrada é o DAG (Saída do
    Estágio 3).

    Esta visão é **read-only**: monta os dois artefatos via
    :class:`~app.services.environment.operacionalizador.CuradoriaOperacionalizador`
    (que reusa ``assemble_stage2`` e deriva os parâmetros de forma pura, **sem
    rede e sem IA** — Req. 11.3, 11.4) e reflete o estado por Estágio; abrir a
    tela nunca conclui o Estágio (a conclusão é o ``POST .../concluir``).

    Attributes:
        caso_id: *Slug* do Caso/campanha.
        abilities: 1º artefato — as Abilities ordenadas (``Stage2Output`` com
            ``atomic_ordering`` e ``origem_traducao``); ``None`` quando a
            operacionalização está suprimida (sem técnica a montante — Req. 8.1).
        parametros: 2º artefato — os :class:`ParametrosDeAmbiente` derivados dos
            nós curados (hosts/serviços/``network_context``), destinados ao
            Estágio 5 (Req. 8.3, 8.4).
        origem: Origem da operacionalização (``curadoria_humana`` hoje; extensível
            a ``gerado_ia`` sem redesenho — Req. 8.6, 16.2, 16.4).
        ambos_produzidos: ``True`` sse **ambos** os artefatos foram produzidos — o
            critério de conclusão do Estágio 4 (Req. 8.8). A conclusão em si é o
            ``POST .../estagio/4/concluir``.
        estados: Estado por Estágio (6 Estágios), para o wizard derivar o
            bloqueio sequencial.
    """

    caso_id: str
    abilities: Optional[Stage2Output] = None
    parametros: ParametrosDeAmbiente
    origem: TranslationSource = TranslationSource.HUMAN_CURATION
    ambos_produzidos: bool = False
    estados: CaseStageStates


class Stage4ProvisionamentoResponse(BaseModel):
    """``GET /api/casos/{caso}/estagio/5`` payload — Provisionamento (Req. 9.6, 9.7).

    No esquema de seis Estágios, a Criação do ambiente de simulação é o
    **Estágio 5** (antigo Provisionamento). A tela exibe a topologia do laboratório contido
    (hosts, redes internas, Mermaid) e o estado de isolamento. Read-only: monta a
    topologia e **verifica** o isolamento via
    :class:`~app.services.environment.CuradoriaProvisionadorDeAmbiente` sem
    iniciar/criar nada.

    Attributes:
        caso_id: Slug do Caso.
        nome: Nome de exibição do Caso.
        isolado: ``True`` sse todos os contêineres-alvo estão isolados — o
            ambiente está pronto para a Execução (Estágio 6).
        isolamento_indisponivel: ``True`` quando o isolamento **não pôde** ser
            verificado (Docker/daemon indisponível); a topologia ainda é exibida,
            mas o Estágio 5 não conclui e o Estágio 6 fica bloqueado (fail-closed).
        motivo: Razão pt-BR quando o ambiente não está isolado/verificado, para a
            tela exibir; ``None`` quando isolado.
        hosts: Hosts do laboratório (nome, container, papel, IPs fixos).
        redes: Redes internas (``internal: true``) e seus hosts.
        mermaid: Diagrama Mermaid da topologia (entregue direto ao
            ``MermaidDiagram`` do frontend).
        compose_file: Caminho do ``docker-compose.yml`` do laboratório (exibição).
        estados: Estado por Estágio (6 Estágios), já refletindo a auto-conclusão
            do Estágio 5 quando o ambiente é verificado isolado e o Estágio 4
            está concluído.
        saude: Resumo de saúde dos contêineres-alvo
            (:class:`~app.services.environment.saude.ResumoSaude`) — veredito por
            contêiner (``running`` + ``healthy``) e resultado geral, para o
            checklist de pré-requisitos da tela (Req. 5.6, 6.1). É **read-only**:
            vem da Saída :class:`AmbienteProvisionado` **persistida** de um
            provisionamento anterior/corrente, se houver; na ausência de uma
            Saída persistida devolve um :class:`ResumoSaude` vazio (não aprovado)
            — abrir a tela nunca sobe contêineres para medir a saúde.
        logs: Buffer corrente das linhas de log do provisionamento
            (:class:`LinhaLogView` com ``seq`` monotônico), lido do
            :class:`~app.services.environment.log_buffer.ProvisioningLogBuffer`
            compartilhado a nível de processo (Req. 2.5). Reflete o histórico da
            execução corrente/anterior, de modo que uma tela aberta tarde ou uma
            reconexão recuperem o terminal ao vivo (Req. 2.7). Vazio quando não há
            provisionamento registrado para o Caso. A leitura é **read-only** — o
            ``GET`` nunca provisiona (Req. 6.1).
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
    # DELTA (task 6.1): saúde dos contêineres + buffer de logs do provisionamento.
    saude: ResumoSaude = Field(default_factory=ResumoSaude)
    logs: list[LinhaLogView] = Field(default_factory=list)


class Stage5Response(BaseModel):
    """``GET /api/casos/{caso}/estagio/5`` payload — Execução (Req. 1.3, 1.7).

    No esquema de seis Estágios, a Execução + medição + relatório é o
    **Estágio 6** (antiga Execução da Campanha). Entrada = Adversary + curated Abilities
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
    """Corpo do ``POST /api/casos/{caso}/estagio/3/escolher-dag`` (Req. 7.2, 7.3).

    Carrega o ``dag_id`` do DAG persistido a ser escolhido para a execução. No
    esquema de seis Estágios, a Geração do DAG é o Estágio 3, então a escolha
    conclui o Estágio 3 (Geração do DAG) e libera o Estágio 4 (Operacionalização).
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


def _operacionalizar(db: Session, case_service: CaseService, caso: str) -> OperacionalizacaoProduzida:
    """Produz os dois artefatos do Estágio 4 (Operacionalização) para ``caso``.

    Ponto único onde ``GET .../estagio/4`` e ``POST .../estagio/4/concluir``
    montam a Saída dupla do Estágio 4 (Req. 8.4, 8.7), para que a leitura e a
    conclusão apliquem exatamente o mesmo critério. Constrói a costura de
    curadoria (:class:`CuradoriaOperacionalizador`) com o
    :class:`StageService` *session-bound* — assim as Abilities ordenadas são
    montadas pela via canônica (``assemble_stage2``) e os
    :class:`ParametrosDeAmbiente` são derivados dos nós do DAG curado de forma
    pura, **sem rede e sem IA** (Req. 11.3, 11.4).

    A Entrada do Estágio 4 é o DAG (Saída do Estágio 3): lemos a
    :class:`~app.services.dag.dag_service.DagView` curada
    (:meth:`DagService.obter_dag`, read-only) e a passamos a
    :meth:`CuradoriaOperacionalizador.operacionalizar`, que a usa como sinal do
    caminho curado e deriva os parâmetros dos nós curados lidos de disco.
    """
    stage_service = StageService(db, case_service=case_service)
    dag = DagService(session=db, case_service=case_service).obter_dag(caso)
    operacionalizador = CuradoriaOperacionalizador(
        stage_service=stage_service, case_service=case_service
    )
    return operacionalizador.operacionalizar(caso, dag)


def _ambos_artefatos_produzidos(produzida: OperacionalizacaoProduzida) -> bool:
    """Critério de conclusão do Estágio 4: os **dois** artefatos foram produzidos.

    O Estágio 4 conclui quando **ambos** os artefatos tipados existem (Req. 8.8):
    as Abilities ordenadas (presentes e com ao menos uma Ability — não
    ``suppressed``/vazio, Req. 8.1) **e** os :class:`ParametrosDeAmbiente`
    (sempre um artefato tipado derivado dos nós curados).
    """
    abilities = produzida.abilities
    tem_abilities = abilities is not None and abilities.has_abilities
    tem_parametros = produzida.parametros is not None
    return tem_abilities and tem_parametros


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


# ---------------------------------------------------------------------------
# Estágio 1 — Seleção da base STIX (esquema de SEIS Estágios, task 2.3).
#
# No esquema de seis Estágios, o Estágio 1 é a "Seleção da base STIX": o
# Pesquisador escolhe a versão do pacote STIX, o Backend baixa o pacote se ainda
# não houver cópia em disco (único egresso de rede) e apresenta o dashboard do
# bundle (Req. 5.1, 5.2, 5.7, 5.8). Estes handlers são finos: constroem um
# ``Estagio1Service`` (serviço fino que reusa ``StixService``) e delegam,
# devolvendo os resultados tipados do serviço.
#
# "Reusar/alinhar, não duplicar": a listagem de bases (``GET /api/stix/bases``)
# permanece no router ``stix`` — ali ela ganhou a flag ``curada`` do Estágio 1,
# delegando a ``Estagio1Service.listar_bases`` para não duplicar a rota. Aqui
# fica o download do Estágio 1 em ``POST .../baixar`` (o único egresso de rede,
# path distinto do ``/download`` histórico) e o dashboard do bundle em
# ``GET .../estagio/1``. Os demais contratos de inteligência STIX
# (dashboard/campanhas/atores por base) permanecem no router ``stix``.
# ---------------------------------------------------------------------------


@router.post(
    "/stix/bases/{versao}/baixar",
    response_model=BaseStixInfo,
    summary="Estágio 1: baixa o pacote STIX da versão (único egresso de rede)",
    responses={
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Versão desconhecida (rejeitada antes de tocar disco/rede)."
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "Falha de rede/conteúdo/escrita no download."
        },
    },
)
def baixar_base_stix(
    versao: str = Path(..., description="Versão da base ATT&CK Enterprise (ex.: 18.0)"),
) -> BaseStixInfo:
    """Garante o pacote STIX da ``versao`` em disco, baixando-o se ausente (Req. 5.2, 5.3).

    Delega a :meth:`Estagio1Service.baixar_base`: reutiliza o pacote em disco sem
    baixá-lo de novo quando já presente (Req. 5.3); caso contrário, aciona o
    **único egresso de rede** via ``StixService.baixar_base`` (valida ``versao``
    contra ``VERSOES_CONHECIDAS`` **antes** de tocar disco/rede, escrita atômica —
    Req. 5.2). Em falha propaga :class:`StixDownloadError`, mapeada ao contrato
    ``falha_download``: 422 quando a versão é desconhecida (rejeitada no
    pré-voo), 502 para falha de rede/conteúdo/escrita (Req. 5.9), sem deixar
    arquivo parcial em disco.
    """
    try:
        return Estagio1Service().baixar_base(versao)
    except StixDownloadError as exc:
        from app.services.stix.stix_service import VERSOES_CONHECIDAS

        desconhecida = exc.versao is not None and exc.versao not in VERSOES_CONHECIDAS
        codigo = (
            status.HTTP_422_UNPROCESSABLE_ENTITY
            if desconhecida
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(
            status_code=codigo,
            detail={"motivo": "falha_download", "mensagem": exc.detail},
        ) from exc


@router.get(
    "/casos/{caso}/estagio/1",
    response_model=ResultadoEstagio1,
    summary="Estágio 1 (Seleção da base STIX): dashboard do bundle + veredito",
)
def obter_estagio1(
    caso: str = Path(..., description="Slug do caso curado"),
    versao: Optional[str] = Query(
        None, description="Versão da base STIX selecionada no fluxo (ex.: 18.0)"
    ),
    db: Session = Depends(get_db),
) -> ResultadoEstagio1:
    """Retorna o resultado do Estágio 1 "Seleção da base STIX" (Req. 5.1, 5.7, 5.8).

    No esquema de seis Estágios, o Estágio 1 é a Seleção da base STIX. Delega a
    :meth:`Estagio1Service.concluir`, que orquestra: garantir o bundle da
    ``versao`` em disco (baixando-o apenas se ausente — único egresso de rede),
    montar o dashboard do bundle (contagens por tipo) e as campanhas, e derivar o
    veredito de conclusão — ``concluido == True`` **somente quando** o bundle está
    em disco **e** o dashboard foi montado (Req. 5.8, 5.10). A Saída (bundle +
    dashboard + campanhas) é a Entrada do Estágio 2 (Req. 2.2).

    O serviço **nunca** levanta: uma falha de download/leitura é refletida em
    ``concluido == False`` com ``mensagem_erro`` preenchida (Req. 5.9), de modo
    que a tela do Estágio sempre recebe um resultado reportável — por isso o
    handler devolve 200 com o resultado tipado em todos os caminhos.

    Quando ``versao`` é omitida, aplica a versão curada
    (:data:`~app.services.stix.stix_service.VERSAO_CURADA`) como padrão do fluxo,
    de modo que abrir a tela sem seleção explícita parta da base que carrega os 8
    casos curados (Req. 5.5, 5.6). O ``caso`` é validado contra os 8 casos
    curados (404 para slug desconhecido), mantendo a consistência com as demais
    rotas de ``casos.py``.
    """
    from app.services.stix.stix_service import VERSAO_CURADA

    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    versao_efetiva = versao if versao is not None else VERSAO_CURADA
    return Estagio1Service().concluir(versao_efetiva)


@router.post(
    "/casos/{caso}/estagio/1/executar",
    response_model=Stage1Result,
    summary="(Transitório) dispara a modelagem estrutural (futuro Estágio 2)",
)
def executar_estagio1(
    caso: str = Path(..., description="Slug do caso curado"),
    versao: Optional[str] = Query(
        None, description="Versão da base STIX selecionada no fluxo (ex.: 18.0)"
    ),
    db: Session = Depends(get_db),
) -> Stage1Result:
    """Dispara a modelagem estrutural (:meth:`StageService.run_stage1`).

    **Transitório.** No esquema de seis Estágios, a Modelagem Estrutural deixa de
    ser o Estágio 1 (agora "Seleção da base STIX", acima) e passa a servir a
    Seleção da campanha (Estágio 2) conforme o mapeamento número→classe do design
    ("o DTO ``Stage1*`` passa a servir o Estágio 2"). A **renumeração** desta rota
    para ``.../estagio/2/...`` é feita nas tarefas 3.x (que também migram o
    frontend e os testes). Até lá, a execução da modelagem permanece acessível
    aqui para não deixar a Modelagem órfã.

    Delega a :meth:`StageService.run_stage1`, que persiste os elementos extraídos
    e marca o Estágio ``concluido`` em sucesso (ou ``erro`` com a etapa de falha —
    reportado no resultado tipado, não como erro HTTP). O query param opcional
    ``versao`` seleciona a base STIX usada para preencher a Entrada
    ``campanha_stix``; sem ``versao`` o serviço aplica o fallback informativo
    (``versao_ausente``) sem levantar erro HTTP (Req. 5.2, 5.4, 3.2).
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)
    return StageService(db, case_service=case_service).run_stage1(caso, versao=versao)


@router.get(
    "/casos/{caso}/estagio/2",
    response_model=Estagio2Response,
    summary="Seleção da campanha (Estágio 2): campanhas do bundle + atributos extraídos",
    responses={
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Bundle STIX ausente/malformado/inválido."
        },
    },
)
def obter_estagio2(
    caso: str = Path(..., description="Slug do caso curado"),
    versao: Optional[str] = Query(
        None, description="Versão da base STIX selecionada no fluxo (ex.: 18.0)"
    ),
    db: Session = Depends(get_db),
) -> Estagio2Response:
    """Return the Stage-2 (Seleção da campanha) view (Req. 6.2, 6.3, 6.4).

    No esquema de **seis** Estágios, o Estágio 2 é a "Seleção da campanha". Delega
    ao :class:`Estagio2Service` (serviço fino que reusa ``StixService`` para a
    listagem e ``StageService`` para a extração), apresentando:

    * a **lista de campanhas** do bundle selecionado no Estágio 1, cada uma
      marcada com ``curada``/``slug_curado``/``selecionavel``
      (:meth:`Estagio2Service.listar_campanhas`, Req. 6.2, 6.5);
    * os **atributos extraídos** da campanha corrente numa visão **read-only**
      (:meth:`Estagio2Service.ler`): identidade/aliases + técnicas quando o
      Estágio já foi concluído — abrir a tela nunca executa a extração nem
      conclui o Estágio (Req. 6.3, 6.4).

    A Entrada é o bundle STIX selecionado no Estágio 1 (Req. 6.1); quando
    ``versao`` é omitida aplica-se a versão curada
    (:data:`~app.services.stix.stix_service.VERSAO_CURADA`), de modo que abrir a
    tela sem seleção explícita parta da base que carrega os 8 casos curados.

    Um bundle ausente/malformado propaga :class:`StixFileError`, mapeada ao
    contrato ``bundle_invalido`` (422), consistente com o precedente do Estágio 1.
    Um slug de Caso desconhecido é 404.
    """
    from app.services.stix.stix_service import VERSAO_CURADA

    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    versao_efetiva = versao if versao is not None else VERSAO_CURADA
    estagio2 = Estagio2Service(StageService(db, case_service=case_service))

    try:
        campanhas = estagio2.listar_campanhas(versao_efetiva)
    except StixFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"motivo": "bundle_invalido", "mensagem": exc.detail},
        ) from exc

    # Visão read-only da seleção corrente (nunca executa/persiste): o slug do
    # Caso curado é o slug da campanha curada (Req. 6.3, 6.4).
    selecao = estagio2.ler(caso, versao_efetiva)
    return Estagio2Response(
        caso_id=caso,
        versao=versao_efetiva,
        campanhas=campanhas,
        selecao=selecao,
    )


@router.post(
    "/casos/{caso}/estagio/2/selecionar",
    response_model=ResultadoSelecaoCampanha,
    summary="Seleciona uma campanha e extrai os seus atributos (conclui o Estágio 2)",
)
def selecionar_campanha(
    corpo: SelecionarCampanhaRequest,
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> ResultadoSelecaoCampanha:
    """Seleciona a campanha ``corpo.slug`` e extrai os seus atributos (Req. 6.3, 6.4, 6.6).

    Delega a :meth:`Estagio2Service.selecionar`, que extrai a identidade/aliases
    (via ``StageService._build_campanha_stix``, defensivo e sem rede nos caminhos
    curados) e as técnicas associadas (extração estrutural do DAG curado), e
    deriva o veredito: ``concluido == True`` **somente quando** a extração teve
    sucesso (Req. 6.6); uma falha de extração (slug inexistente, DAG
    ausente/ilegível) mantém o Estágio não concluído com a causa reportada em
    ``mensagem_erro``/``etapa_falha`` (Req. 6.7).

    O serviço **nunca** levanta por um caso inválido — a falha é refletida no
    resultado tipado —, então o handler devolve 200 com o resultado em todos os
    caminhos de seleção. Quando ``versao`` é omitida no corpo, aplica a versão
    curada padrão (Entrada do Estágio 1/2, Req. 6.1). O slug do Caso na rota é
    validado contra os 8 casos curados (404 para slug desconhecido).
    """
    from app.services.stix.stix_service import VERSAO_CURADA

    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    versao_efetiva = corpo.versao if corpo.versao is not None else VERSAO_CURADA
    estagio2 = Estagio2Service(StageService(db, case_service=case_service))
    return estagio2.selecionar(corpo.slug, versao_efetiva)


@router.get(
    "/casos/{caso}/estagio/3",
    response_model=DagView,
    summary="Geração do DAG (Estágio 3): grafo estrutural + Mermaid do DAG curado",
)
def obter_estagio3(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> DagView:
    """Return the Stage-3 (Geração do DAG) view — DagView/Mermaid (Req. 7.2, 7.3).

    No esquema de **seis** Estágios, a Geração do DAG é o **Estágio 3**
    (``ESTAGIO_GERACAO_DAG == 3``): a Entrada são os atributos da campanha
    selecionada no Estágio 2 e a Saída é o DAG curado pronto, com a ``origem``
    explícita (``curadoria_humana``). A tela do Estágio exibe o DAG como grafo
    (nós + arestas) e o diagrama Mermaid.

    Esta visão é **read-only** e sem efeito colateral de execução: delega a
    :meth:`DagService.obter_dag`, que lê ``data/dag/{slug}_dag.json`` via
    ``CaseService`` — **sem rede e sem IA** em qualquer caminho (Req. 7.4, 11.1).
    Antes de montar a visão, dispara a auto-conclusão idempotente do Estágio 3
    (:meth:`DagService.auto_concluir_estagio2`, cujo nome é histórico mas opera
    sobre ``ESTAGIO_GERACAO_DAG``): respeita o gate sequencial, faz o backfill
    curado e conclui o Estágio 3 somente quando há um DAG derivável, sem duplicar
    estado em leituras repetidas (Req. 7.4, 11.3, 11.4).

    Um slug de Caso desconhecido é 404, consistente com as demais rotas de
    ``casos.py``. Um Caso curado cujo DAG esteja ausente/malformado **não** é 404:
    a própria :class:`DagView` carrega ``curado``/``pode_avancar``/``erro`` para a
    tela exibir o estado de erro (Req. 7.6), e o Estágio 3 permanece não concluído
    reportando a ausência. Os DAGs persistidos (com ``dag_id``) são servidos pelos
    endpoints ``GET .../dags`` e ``GET .../dags/{dag_id}`` (grafo + Mermaid),
    mantidos/reusados abaixo.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)
    # Auto-conclusão idempotente do Estágio 3 (DAG curado): respeita o gate,
    # roda o backfill curado e conclui apenas havendo DAG derivável.
    DagService(session=db, case_service=case_service).auto_concluir_estagio2(caso)
    return DagService(session=db, case_service=case_service).obter_dag(caso)


@router.get(
    "/casos/{caso}/estagio/4",
    response_model=Estagio4Response,
    summary="Operacionalização (Estágio 4): Abilities ordenadas + ParametrosDeAmbiente",
)
def obter_estagio4(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> Estagio4Response:
    """Return the Stage-4 (Operacionalização) view — Abilities + parâmetros (Req. 8.4, 8.7).

    No esquema de **seis** Estágios, o Estágio 4 é a "Operacionalização": a Saída
    são **dois artefatos tipados separados** (Req. 8.4, 8.7) — as **Abilities
    ordenadas pelo DAG** (reuso de ``Stage2*``/``CaseService`` via
    ``assemble_stage2``) e os **``ParametrosDeAmbiente``** derivados dos nós do
    DAG curado (hosts/serviços/``network_context``), destinados ao Estágio 5. A
    Entrada é o DAG (Saída do Estágio 3).

    Delega à costura de curadoria
    (:class:`~app.services.environment.operacionalizador.CuradoriaOperacionalizador`,
    via :func:`_operacionalizar`), que reusa ``assemble_stage2`` para as Abilities
    e **deriva** os parâmetros de forma pura dos nós curados lidos de disco —
    **sem rede e sem IA** (Req. 11.3, 11.4). Esta visão é **read-only** e sem
    efeito colateral de execução: abrir a tela nunca conclui o Estágio (a
    conclusão é o ``POST .../estagio/4/concluir``), apenas expõe os dois artefatos
    e o veredito ``ambos_produzidos`` (Req. 8.8).

    Um slug de Caso desconhecido é 404, consistente com as demais rotas. Um Caso
    curado cuja operacionalização esteja suprimida (sem técnica a montante) ainda
    responde 200 com ``abilities=None`` e ``ambos_produzidos=False`` (Req. 8.1).
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    produzida = _operacionalizar(db, case_service, caso)
    estados = StageService(db, case_service=case_service).get_stage_states(caso)
    return Estagio4Response(
        caso_id=caso,
        abilities=produzida.abilities,
        parametros=produzida.parametros,
        origem=produzida.origem,
        ambos_produzidos=_ambos_artefatos_produzidos(produzida),
        estados=estados,
    )


@router.post(
    "/casos/{caso}/estagio/4/concluir",
    response_model=CaseStageStates,
    summary="Conclui a Operacionalização (Estágio 4) quando ambos os artefatos são produzidos",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": (
                "Estágio 3 não concluído (bloqueio sequencial) ou artefatos "
                "incompletos (Abilities + ParametrosDeAmbiente)."
            )
        },
    },
)
def concluir_estagio4(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> CaseStageStates:
    """Conclui o Estágio 4 (Operacionalização) quando os dois artefatos existem (Req. 8.7, 8.8).

    No esquema de **seis** Estágios, a Operacionalização é o Estágio 4
    (:data:`ESTAGIO_OPERACIONALIZACAO`). A operacionalização é curada (os
    artefatos derivam do material em disco), então "avançar do 4" é, semanticamente,
    "concluir o 4". Duas pré-condições, nesta ordem:

    * **Bloqueio sequencial (Req. 3.x):** só conclui o Estágio 4 se o **Estágio 3**
      (Geração do DAG, :data:`ESTAGIO_GERACAO_DAG`) já estiver ``concluido`` —
      senão 409, sem persistir.
    * **Ambos os artefatos produzidos (Req. 8.8):** monta a Saída dupla via
      :func:`_operacionalizar` e exige que as **Abilities ordenadas** e os
      **``ParametrosDeAmbiente``** estejam ambos presentes
      (:func:`_ambos_artefatos_produzidos`) — senão 409, sem persistir, reportando
      a causa.

    Com ambas satisfeitas, persiste a conclusão do Estágio 4 via
    :meth:`SessionStateService.persist_stage_completion` (atômico; preserva o
    estado anterior em falha → 500) e relê o estado por Estágio (com o 4
    ``concluido`` e o 5 liberado pelo gate). Handler fino: sem reimplementar a
    regra de persistência.
    """
    from app.models.enums import StageState  # local import: evita ciclo no topo

    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    stage_service = StageService(db, case_service=case_service)
    estados = stage_service.get_stage_states(caso)

    # (1) Bloqueio sequencial: o Estágio 4 só conclui se o 3 está concluido.
    estagio3 = next(
        (e for e in estados.estagios if e.estagio == ESTAGIO_GERACAO_DAG), None
    )
    if estagio3 is None or estagio3.estado is not StageState.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "motivo": "bloqueio_sequencial",
                "mensagem": (
                    "O Estágio 4 não pode ser concluído enquanto o Estágio 3 "
                    "não estiver concluído."
                ),
            },
        )

    # (2) Ambos os artefatos produzidos (Abilities ordenadas + ParametrosDeAmbiente).
    produzida = _operacionalizar(db, case_service, caso)
    if not _ambos_artefatos_produzidos(produzida):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "motivo": "artefatos_incompletos",
                "mensagem": (
                    "O Estágio 4 só conclui quando ambos os artefatos são "
                    "produzidos: as Abilities ordenadas e os ParametrosDeAmbiente."
                ),
            },
        )

    resultado = SessionStateService(db).persist_stage_completion(
        caso, ESTAGIO_OPERACIONALIZACAO
    )
    if not resultado.saved:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "motivo": "falha_persistencia",
                "mensagem": resultado.mensagem or "Conclusão não foi salva.",
            },
        )

    # Reler o estado por Estágio já com o 4 concluído (5 liberado pelo gate).
    return stage_service.get_stage_states(caso)


@router.get(
    "/casos/{caso}/estagio/5",
    response_model=Stage4ProvisionamentoResponse,
    summary="Criação do ambiente (Estágio 5): topologia + isolamento",
)
def obter_estagio5_ambiente(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
    log_buffer: ProvisioningLogBuffer = Depends(get_log_buffer),
) -> Stage4ProvisionamentoResponse:
    """Return the Stage-5 (Criação do ambiente) view (Req. 9.6, 9.7).

    No esquema de **seis** Estágios, a Criação do ambiente de simulação é o
    **Estágio 5** (antigo Provisionamento, que no esquema de cinco Estágios era o
    4). A tela exibe a topologia do laboratório contido (hosts, redes internas,
    Mermaid) e o estado de isolamento.

    Nota (task 7.3): esta rota é a renumeração do antigo ``.../estagio/4`` de
    Provisionamento para ``.../estagio/5``, movida aqui porque o Estágio 4 passou
    a servir a Operacionalização. A costura de provisionamento/isolamento em si é
    de responsabilidade da task 7.3; esta movimentação apenas preserva o endpoint
    sob o número correto do esquema de seis Estágios.

    Fluxo: dispara a auto-conclusão do Estágio 4
    (:meth:`EnvironmentStageService.auto_concluir_estagio4`) — que respeita o gate
    (Estágio 3 ``concluido``), exige o material curado de ambiente e **verifica**
    o isolamento, concluindo o Estágio 4 somente quando isolado (idempotente). O
    resultado tipado traz a topologia e os sinais de isolamento, que mapeamos para
    o payload da tela; mesmo quando não isolado (ou não verificável), a topologia é
    exibida com a razão e o Estágio 5 permanece bloqueado (Req. 5.6). O estado por
    Estágio devolvido já reflete a (não-)conclusão do Estágio 4.

    Saúde + buffer de logs (task 6.1 — Req. 2.5, 5.6, 6.1)
    ------------------------------------------------------
    Além da topologia e do isolamento, a leitura devolve dois sinais do
    provisionamento, **ambos read-only** (abrir a tela nunca sobe contêineres):

    * ``saude`` — o :class:`~app.services.environment.saude.ResumoSaude` da Saída
      :class:`AmbienteProvisionado` **persistida** de um provisionamento
      anterior/corrente (via :meth:`EnvironmentStageService.ambiente_persistido`),
      ou um resumo vazio (não aprovado) quando ainda não há Saída persistida — a
      saúde é medida durante o provisionamento ativo, não nesta leitura (Req. 5.6).
    * ``logs`` — o buffer corrente de linhas de log do provisionamento, lido do
      :class:`~app.services.environment.log_buffer.ProvisioningLogBuffer`
      compartilhado a nível de processo (``get_log_buffer``) — a **mesma**
      instância que o ``EnvironmentStageService`` alimenta e que o canal WebSocket
      lê —, de modo que uma tela aberta tarde ou uma reconexão recuperem o
      terminal ao vivo (Req. 2.5, 2.7). Vazio quando não há provisionamento
      registrado para o Caso.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    env_service = EnvironmentStageService(db)
    auto = env_service.auto_concluir_estagio4(caso)
    # A topologia (sempre exibível) e os veredictos de isolamento vêm do
    # provisionador de curadoria; `preparar` é read-only (não cria/inicia nada).
    resultado = env_service.provisionador.preparar(caso)
    topo = resultado.topologia

    # Saúde: vem da Saída persistida de um provisionamento anterior/corrente, se
    # houver (read-only); na ausência, um resumo vazio (não aprovado). A leitura
    # NÃO provisiona (não chama `provisionar`) — Req. 6.1.
    ambiente = env_service.ambiente_persistido(caso)
    saude = ambiente.saude if ambiente is not None else ResumoSaude()

    # Buffer de logs corrente do Caso, lido do buffer compartilhado (Req. 2.5, 2.7).
    logs = log_buffer.ler(caso)

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
        saude=saude,
        logs=logs,
    )


def get_provisionador() -> CuradoriaProvisionadorDeAmbiente:
    """Dependência FastAPI que fornece o :class:`ProvisionadorDeAmbiente`.

    Compartilhada pela Criação do ambiente (Estágio 5,
    ``POST .../estagio/5/provisionar``) e pela Execução (Estágio 6,
    ``POST .../estagio/6/execucao``). Em produção devolve a implementação de
    curadoria com os defaults de produção (inspector Docker real + cliente
    Caldera reais, construídos preguiçosamente só quando a verificação/carga roda
    — importar nunca abre socket). Os testes **sobrescrevem** esta dependência
    para injetar um provisionador com inspector de isolamento e cliente Caldera
    *fakes*, de modo que os endpoints de provisionamento/execução sejam
    exercitáveis de ponta a ponta sem Caldera nem Docker reais — exatamente como
    :func:`~app.api.emulacao.get_runner_factory` permite para o runner. O mesmo
    provisionador é reusado para o desprovisionamento ao final do Estágio 6
    (Req. 12).
    """
    return CuradoriaProvisionadorDeAmbiente()


class ProvisionarEstagio5Response(BaseModel):
    """``POST /api/casos/{caso}/estagio/5/provisionar`` payload — Criação do ambiente (Req. 1.1, 1.6).

    No esquema de **seis** Estágios, a Criação do ambiente de simulação é o
    **Estágio 5**. Este endpoint é o ponto de **garante/inicia** idempotente do
    provisionamento ativo: delega à costura
    :meth:`EnvironmentStageService.provisionar_estagio5` — a **mesma** pipeline em
    streaming que o início automático do Estágio 5 dispara (Req. 1.1) —, que sobe
    o laboratório (subir contêineres → aguardar saúde → carregar Abilities →
    verificar isolamento), **encaminha cada linha de log ao buffer compartilhado**
    (que o terminal ao vivo da tela lê — Req. 2.2/2.5) e conclui o Estágio 5
    **somente** quando os três pré-requisitos passam (saúde + Abilities +
    isolamento). O serviço é construído com o buffer compartilhado
    (:func:`get_log_buffer`) e o ``provisionador`` injetado (:func:`get_provisionador`),
    de modo que os testes de endpoint continuem injetando *fakes* de
    compose/estado/Caldera/isolamento.

    **Idempotência (Req. 1.6):** quando o ambiente já foi provisionado com sucesso
    para o Caso e permanece provisionado, a costura **reaproveita** a Saída
    persistida (``reaproveitado``) e devolve ``concluido=True`` **sem** subir um
    novo ambiente — chamar o endpoint duas vezes é seguro.

    A Saída exposta é o :class:`AmbienteProvisionado` completo — contêineres-alvo,
    resumo de isolamento (veredito por contêiner + geral), ``abilities_carregadas``,
    ``compose_file`` e ``origem``.

    Attributes:
        ambiente: A Saída tipada :class:`AmbienteProvisionado` do Estágio 5. Em
            todos os caminhos que **rodaram** o provisionamento (sucesso ou falha
            de etapa) a Saída acompanha a resposta; ``None`` apenas no caminho
            defensivo em que o gate sequencial bloqueou (o Estágio 4 não está
            concluído) e o pipeline não rodou.
        concluido: ``True`` sse o Estágio 5 ficou concluído — isto é, os três
            pré-requisitos passaram (``resultado.pronto``) ou um ambiente pronto
            foi reaproveitado (Req. 1.6). Quando ``False``, o Estágio 6 permanece
            bloqueado.
        motivo: Razão pt-BR quando o Estágio 5 **não** conclui — derivada da
            última linha de erro do console da etapa que falhou (consistência tela
            ↔ console — Req. 7.5), do bloqueio do gate ou de falha de persistência;
            ``None`` em sucesso. A tela usa isto para exibir a causa.
        estados: Estado por Estágio (6 Estágios), já refletindo a (não-)conclusão
            do Estágio 5 — a fonte do bloqueio sequencial para o wizard.
    """

    ambiente: Optional[AmbienteProvisionado] = None
    concluido: bool = False
    motivo: Optional[str] = None
    estados: CaseStageStates


@router.post(
    "/casos/{caso}/estagio/5/provisionar",
    response_model=ProvisionarEstagio5Response,
    summary="Estágio 5: provisiona + carrega Abilities + verifica isolamento (não conclui sem isolamento)",
)
def provisionar_estagio5(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
    provisionador: CuradoriaProvisionadorDeAmbiente = Depends(get_provisionador),
    log_buffer: ProvisioningLogBuffer = Depends(get_log_buffer),
) -> ProvisionarEstagio5Response:
    """Garante/inicia o provisionamento ativo do Estágio 5 (idempotente — Req. 1.1, 1.6).

    No esquema de **seis** Estágios, a Criação do ambiente de simulação é o
    **Estágio 5** (``estagio=5``). Este handler é **fino**: delega todo
    o provisionamento à costura ativa
    :meth:`EnvironmentStageService.provisionar_estagio5` — a **mesma** pipeline em
    streaming que o início automático do Estágio 5 dispara (Req. 1.1), de modo que
    o endpoint seja o ponto idempotente de "garante/inicia" e não um fluxo
    paralelo. O serviço é construído com:

    * ``log_buffer`` — o :class:`ProvisioningLogBuffer` **compartilhado a nível de
      processo** (:func:`get_log_buffer`), a mesma instância que o canal WebSocket
      e o ``GET .../estagio/5/logs`` leem, de modo que cada linha emitida pela
      pipeline chegue ao terminal ao vivo da tela (Req. 2.2, 2.5);
    * ``provisionador`` — o :class:`CuradoriaProvisionadorDeAmbiente` injetado via
      :func:`get_provisionador` (os testes o sobrescrevem para injetar *fakes* de
      compose/estado/Caldera/isolamento, exercitando o endpoint sem Docker nem
      Caldera reais);
    * ``params_provider`` — resolve os :class:`ParametrosDeAmbiente` (Entrada do
      Estágio 5) pela **mesma via canônica** que o ``GET .../estagio/4`` usa
      (:func:`_operacionalizar`), **sem rede e sem IA** (Req. 11.3, 11.4), evitando
      recomputar os parâmetros numa via divergente.

    A costura:

    1. **Idempotência (Req. 1.6):** se o ambiente já foi provisionado com sucesso
       e permanece provisionado (``AmbienteProvisionado`` pronto persistido + o
       ``StageRun`` do Estágio 5 já ``concluido``), **reaproveita** a Saída sem
       subir um novo ambiente (``resultado.reaproveitado``) e devolve ``pronto``.
    2. **Pipeline em streaming:** resolve os parâmetros, sobe o laboratório,
       aguarda a saúde, carrega as Abilities (ordem do DAG) e **verifica o
       isolamento** read-only, encaminhando cada linha ao buffer compartilhado.
    3. **Conclusão e ``StageRun`` (Req. 5.5, 6.2, 6.4, 7.1):** a própria costura
       conclui o Estágio 5 (``StageRun`` → ``concluido``/100 e persistência da
       Saída) **apenas** quando os três pré-requisitos passam (``resultado.pronto``);
       em falha de etapa grava ``etapa_falha`` + ``mensagem_erro`` (derivada da
       última linha de erro do console) e mantém o Estágio 6 bloqueado. **Não**
       repersistimos a conclusão aqui — o serviço é o dono dessa transição.

    Mapeamento para a resposta: ``ambiente = resultado.ambiente`` (``None`` só no
    caminho defensivo em que o gate bloqueou), ``concluido = resultado.pronto``,
    ``motivo = resultado.mensagem_erro`` quando não concluído, e ``estados`` relido
    para refletir a (não-)conclusão. Um *slug* de Caso desconhecido é 404,
    consistente com as demais rotas de ``casos.py``.
    """
    from app.models.enums import StageState  # local import: evita ciclo no topo

    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    stage_service = StageService(db, case_service=case_service)

    # Garante que o Estágio 4 (Operacionalização) esteja concluído antes do gate
    # sequencial da costura ativa (Req. 1.1 — o Estágio 5 só inicia com o Estágio 4
    # concluído). A operacionalização dos Casos curados é derivada de forma pura do
    # material em disco; quando os dois artefatos existem, concluímos o Estágio 4
    # de forma idempotente via :meth:`SessionStateService.persist_stage_completion`
    # (upsert absoluto; preserva o estado anterior em falha), de modo que atingir a
    # tela do Estágio 5 (e, portanto, este endpoint) implique o Estágio 4 concluído
    # — exatamente o invariante que o fluxo guiado garante.
    estados = stage_service.get_stage_states(caso)
    estagio4 = next(
        (e for e in estados.estagios if e.estagio == ESTAGIO_OPERACIONALIZACAO), None
    )
    if estagio4 is None or estagio4.estado is not StageState.COMPLETED:
        produzida = _operacionalizar(db, case_service, caso)
        if _ambos_artefatos_produzidos(produzida):
            SessionStateService(db).persist_stage_completion(
                caso, ESTAGIO_OPERACIONALIZACAO
            )

    # Delega à costura ativa: provisionamento em streaming com buffer compartilhado
    # (terminal ao vivo) + provisionador injetado (fakes nos testes). O
    # ``params_provider`` reusa a via canônica do Estágio 4 para resolver a Entrada
    # do Estágio 5 de forma idêntica à do ``GET .../estagio/4`` (sem rede/IA).
    service = EnvironmentStageService(
        db,
        provisionador=provisionador,
        log_buffer=log_buffer,
        params_provider=lambda _c: _operacionalizar(db, case_service, _c).parametros,
    )
    resultado = service.provisionar_estagio5(caso)

    estados = stage_service.get_stage_states(caso)
    return ProvisionarEstagio5Response(
        ambiente=resultado.ambiente,
        concluido=resultado.pronto,
        motivo=(None if resultado.pronto else resultado.mensagem_erro),
        estados=estados,
    )


class Estagio5LogsResponse(BaseModel):
    """``GET /api/casos/{caso}/estagio/5/logs`` payload — buffer corrente de logs (Req. 2.5).

    Devolve o **buffer corrente** de linhas de log do provisionamento do Estágio
    5 do Caso, lido do :class:`~app.services.environment.log_buffer.ProvisioningLogBuffer`
    compartilhado a nível de processo — a **mesma** instância que o
    :class:`EnvironmentStageService` alimenta durante a pipeline (via ``on_log``) e
    que o canal WebSocket lê para o terminal ao vivo. Serve à **recuperação** do
    terminal quando a tela é aberta tarde ou a conexão ao vivo é reestabelecida
    (Req. 2.5, 2.7): o cliente obtém o histórico da execução corrente sem perder
    linhas.

    A leitura é **read-only**: nunca provisiona nem muta o buffer; devolve uma
    cópia das :class:`LinhaLogView` em ordem de ``seq`` monotônico. Vazio quando
    não há provisionamento registrado para o Caso.

    Attributes:
        caso_id: *Slug* do Caso cujo buffer é devolvido.
        logs: As linhas de log correntes (``seq``, ``etapa``, ``fluxo``, ``texto``,
            ``em``), em ordem de ``seq``.
    """

    caso_id: str
    logs: list[LinhaLogView] = Field(default_factory=list)


@router.get(
    "/casos/{caso}/estagio/5/logs",
    response_model=Estagio5LogsResponse,
    summary="Estágio 5: buffer corrente de linhas de log do provisionamento (read-only)",
)
def obter_estagio5_logs(
    caso: str = Path(..., description="Slug do caso curado"),
    log_buffer: ProvisioningLogBuffer = Depends(get_log_buffer),
) -> Estagio5LogsResponse:
    """Devolve o buffer corrente de linhas de log do provisionamento (Req. 2.5, 2.7).

    Handler fino e **read-only**: lê o buffer corrente do Caso do
    :class:`ProvisioningLogBuffer` compartilhado (:func:`get_log_buffer`) — a mesma
    instância que o ``POST .../estagio/5/provisionar`` alimenta via a costura
    ativa e que o canal WebSocket lê para o terminal ao vivo —, de modo que uma
    tela aberta tarde ou uma reconexão recuperem o histórico da execução corrente
    sem perder linhas (Req. 2.5, 2.7). Não provisiona nada. Um *slug* de Caso
    desconhecido é 404, consistente com as demais rotas de ``casos.py``.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)
    return Estagio5LogsResponse(caso_id=caso, logs=log_buffer.ler(caso))


@router.get(
    "/casos/{caso}/estagio/6",
    response_model=Stage5Response,
    summary="Entrada (Adversary + Abilities) e Saída acumulada do Estágio 6 (Execução)",
)
def obter_estagio6(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> Stage5Response:
    """Return the Stage-6 (Execução) Entrada + accumulated Saída (Req. 10.x).

    No esquema de **seis** Estágios, a Execução, medição e relatório é o
    **Estágio 6** (antiga Execução, que no esquema de cinco Estágios era o 5).
    Entrada = Adversary + curated Abilities (assembled, no side effects). Saída
    acumulada = the per-Ability operation results persisted so far for the case.
    Execution itself is not triggered here (that is task 8.2/8.5).

    Nota (task 8.5): esta rota é a renumeração do antigo ``.../estagio/5`` de
    Execução para ``.../estagio/6``, movida aqui porque, no esquema de seis
    Estágios, o Estágio 5 passou a ser a Criação do ambiente e o 6 a Execução.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    stage_service = StageService(db, case_service=case_service)
    entrada = stage_service.assemble_stage3(caso)
    saida = SessionStateService(db).get_operation_results(caso)
    return Stage5Response(entrada=entrada, saida=saida)


# ---------------------------------------------------------------------------
# Estágio 6 — Execução, medição e relatório (esquema de SEIS Estágios, task 8.5)
#
# O Estágio 6 (Execução + medição + relatório) **reusa integralmente** o
# ``OperationRunner`` já construído (os quatro gates: confirmação → contenção →
# isolamento → disponibilidade) através das costuras *session-bound* das tarefas
# 8.2/8.3/8.4:
#
# * :class:`ExecucaoStageService` (task 8.2) — preview + execução única reusando
#   o runner, com os gates;
# * :class:`MedicaoService` (task 8.3) — medição de suficiência procedural +
#   relatório;
# * :class:`ConclusaoEstagio6Service` (task 8.4) — ``executar_e_concluir``:
#   execução (conclusão deferida) → medição/relatório → **SEMPRE** desprovisiona →
#   conclui o Estágio 6 apenas quando a execução concluiu, o ambiente foi
#   desprovisionado e o relatório foi persistido.
#
# Esta tarefa (8.5) é a **renumeração** do antigo par de emulação do Estágio 5
# (``api/emulacao.py``: ``.../estagio/5/emulacao/preview`` + ``.../emulacao``)
# para o esquema de seis Estágios, movendo a superfície HTTP de execução para
# ``.../estagio/6/execucao/preview`` + ``.../estagio/6/execucao`` e amarrando-a às
# costuras do Estágio 6 (que acrescentam a medição/relatório e o
# desprovisionamento sempre). As dependências de injeção (``get_runner_factory``,
# cliente Caldera) e a apresentação do preview são **reusadas** de
# ``api/emulacao.py`` — sem duplicação. Mapeamento de resultado para HTTP:
#
#   * ``CONTAINMENT_REFUSED`` (destino externo) / ``ISOLATION_FAILED``
#     (contêiner-alvo não isolado) → **409** com o detalhe ofensor (Req. 13.4,
#     13.5, 14.2, 14.4);
#   * :class:`CalderaUnavailable` (Caldera silencioso em 10s) → **503** (Req.
#     10.1); nada foi executado;
#   * :class:`CalderaApiError` (Caldera respondeu com erro) → **502** (upstream);
#   * ``NOT_CONFIRMED`` (confirmação ausente/falsa) → **200** com corpo indicando
#     que nada iniciou (Req. 13.2);
#   * ``COMPLETED`` / ``ABORTED`` → **200** com o resultado completo (execução +
#     relatório + desprovisionamento).
#
# ``GET /api/casos/{caso}/relatorio`` devolve o :class:`RelatorioExecucao`
# consolidado (métricas + resultados + flags) derivado dos resultados por Ability
# **persistidos** mais o DAG curado, via :class:`MedicaoService` (Req. 15.7).
# ---------------------------------------------------------------------------


class ExecucaoRequest(BaseModel):
    """Corpo do ``POST /api/casos/{caso}/estagio/6/execucao`` (Req. 13.2).

    ``confirmado`` é a confirmação **explícita** do Pesquisador. A Operação inicia
    *se e somente se* este campo for exatamente ``True``; qualquer outro valor
    (ausente/falso/cancelado) significa "não confirmada" e nada é executado — o
    gate 1 do ``OperationRunner`` (Req. 13.2).
    """

    confirmado: bool = Field(
        default=False,
        description=(
            "Confirmação explícita do Pesquisador. A execução só inicia com "
            "confirmado=true; caso contrário nenhum comando é executado."
        ),
    )


def _relatorio_sink_memoria(_relatorio: RelatorioExecucao) -> bool:
    """Persistidor de relatório *no-op* que sempre confirma (pré-tabela, task 10.x).

    A tabela ``relatorios_execucao`` é introduzida pela migração da task 10.x; até
    lá a costura de conclusão do Estágio 6 (:class:`ConclusaoEstagio6Service`)
    exige apenas que *algum* sink confirme a persistência (``True``) para liberar a
    conclusão (Req. 10.9). Este sink confirma sem gravar — o relatório é
    recomputável de forma determinística a partir dos resultados por Ability
    persistidos + o DAG curado (ver :func:`obter_relatorio`), então nenhuma
    informação se perde. Quando a tabela existir, este sink é trocado por um que
    grava a linha de ``relatorios_execucao``, sem alterar os handlers.
    """
    return True


@router.post(
    "/casos/{caso}/estagio/6/execucao/preview",
    response_model=EmulationPreviewResponse,
    summary="Estágio 6: lista de comandos concretos + container-alvo + destino externo (sem executar)",
)
def preview_execucao_estagio6(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> EmulationPreviewResponse:
    """Preview dos comandos do Estágio 6 — pré-condição visível (Req. 13.6, 13.7).

    Expõe a lista completa de comandos concretos com o **container-alvo** de cada
    e a sinalização de **destino externo** (``tem_externo``), **sem executar nada**
    e **sem criar Operação** — é a apresentação que o modal de confirmação mostra
    *antes* de qualquer confirmação. Delega a
    :meth:`ExecucaoStageService.previsualizar` (que reusa
    :func:`~app.services.containment.preview_abilities`) e reaproveita o mapeamento
    de resposta de ``api/emulacao.py`` (:func:`_preview_view`), sem duplicar a
    apresentação.

    Destinos externos são **sinalizados**, não recusados aqui; a recusa é um dos
    gates da execução (:func:`executar_estagio6`, via o runner). Um *slug* de Caso
    desconhecido é 404; um arquivo de Caso ausente/malformado é 422 — a mesma
    distinção que a costura levanta (``KeyError``/``ValueError``).
    """
    case_service = CaseService()
    service = ExecucaoStageService(
        db,
        runner_factory=lambda _s: None,  # type: ignore[arg-type,return-value]
        case_service=case_service,
    )
    try:
        preview = service.previsualizar(caso)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Caso desconhecido: {caso!r}.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return _preview_view(caso, preview)


@router.post(
    "/casos/{caso}/estagio/6/execucao",
    response_model=ConclusaoEstagio6Result,
    summary="Estágio 6: confirmação + execução (gates) + medição + relatório + desprovisionamento",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": (
                "Contenção violada — destino externo (Req. 13.4) ou container-alvo "
                "não isolado (Req. 14.2). Nada foi executado."
            )
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Caldera não respondeu em 10s (Req. 10.1). Nada foi executado."
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "Caldera respondeu com erro ao receber a operação."
        },
    },
)
def executar_estagio6(
    corpo: ExecucaoRequest,
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
    runner_factory: RunnerFactory = Depends(get_runner_factory),
    provisionador: CuradoriaProvisionadorDeAmbiente = Depends(get_provisionador),
) -> ConclusaoEstagio6Result:
    """Executa o Estágio 6 com os quatro gates, mede, gera o relatório e desprovisiona.

    Fluxo (Req. 10.1–10.9, 12, 13):

    1. Deriva os :class:`ParametrosDeAmbiente` do Estágio 4 (via
       :func:`_operacionalizar`) e **provisiona/verifica** o ambiente curado via
       :class:`CuradoriaProvisionadorDeAmbiente`, obtendo o
       :class:`AmbienteProvisionado` — o alvo do desprovisionamento ao final.
    2. Delega a :meth:`ConclusaoEstagio6Service.executar_e_concluir`, que: executa
       **uma única vez** reusando o ``OperationRunner`` com os quatro gates
       (confirmação → contenção → isolamento → disponibilidade, Req. 13.1);
       **mede** a suficiência procedural e gera o :class:`RelatorioExecucao`;
       **SEMPRE** desprovisiona o ambiente (sucesso ou falha, Req. 10.8, 12.1); e
       conclui o Estágio 6 **somente** quando a execução concluiu, o ambiente foi
       desprovisionado e o relatório foi persistido (Req. 10.9).

    O resultado tipado (:class:`ConclusaoEstagio6Result`) carrega a execução, o
    relatório, o desprovisionamento e o veredito de conclusão. O handler apenas
    mapeia os caminhos de **recusa**/indisponibilidade para o HTTP do design:

    * ``CONTAINMENT_REFUSED`` → **409** com as violações (destino externo);
    * ``ISOLATION_FAILED`` → **409** com os contêineres não isolados;
    * :class:`CalderaUnavailable` → **503** (nada iniciou);
    * :class:`CalderaApiError` → **502** (upstream);
    * ``NOT_CONFIRMED`` → **200** (nada iniciou);
    * ``COMPLETED`` / ``ABORTED`` → **200** com o resultado completo.

    Nota de persistência: até a tabela ``relatorios_execucao`` existir (task
    10.x), o relatório é persistido por um sink *no-op* que confirma (o relatório é
    recomputável em ``GET .../relatorio``), de modo que a conclusão não fique
    bloqueada por uma dependência ainda não criada (Req. 10.9).
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    # (1) Parâmetros do Estágio 4 + provisionamento/verificação do ambiente curado.
    # O mesmo provisionador (injetável via get_provisionador) é reusado para o
    # desprovisionamento ao final (Req. 12).
    parametros = _operacionalizar(db, case_service, caso).parametros
    ambiente: AmbienteProvisionado = provisionador.provisionar(parametros)

    service = ConclusaoEstagio6Service(
        db,
        runner_factory=runner_factory,
        provisionador=provisionador,
        relatorio_sink=_relatorio_sink_memoria,
        case_service=case_service,
    )

    try:
        resultado = service.executar_e_concluir(
            caso, confirmado=corpo.confirmado, ambiente=ambiente
        )
    except CalderaUnavailable as exc:
        # Gate 4 (Req. 10.1): Caldera silencioso em 10s → 503. Nada iniciou.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except CalderaApiError as exc:
        # Caldera respondeu, mas recusou/fracassou ao processar o payload → 502.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "motivo": "caldera_api_error",
                "mensagem": str(exc),
                "endpoint": exc.path,
                "status_caldera": exc.status_code,
            },
        ) from exc

    execucao = resultado.execucao

    if execucao.resultado is RunOutcome.CONTAINMENT_REFUSED:
        # Gate 2 (Req. 13.4): destino externo → 409, identificando a violação.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "motivo": "contencao_recusada",
                "mensagem": execucao.mensagem,
                "violacoes": execucao.violacoes,
            },
        )

    if execucao.resultado is RunOutcome.ISOLATION_FAILED:
        # Gate 3 (Req. 14.2): contêiner-alvo não isolado → 409, identificando-o.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "motivo": "isolamento_falhou",
                "mensagem": execucao.mensagem,
                "containers": execucao.containers_nao_isolados,
            },
        )

    # NOT_CONFIRMED (Req. 13.2): nada iniciou — 200. COMPLETED/ABORTED: resultado
    # completo (execução + relatório + desprovisionamento). O desprovisionamento
    # ocorreu em TODOS esses caminhos (Req. 10.8, 12.1).
    return resultado


@router.get(
    "/casos/{caso}/relatorio",
    response_model=RelatorioExecucao,
    summary="Estágio 6: relatório de execução + métricas de suficiência procedural",
)
def obter_relatorio(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> RelatorioExecucao:
    """Retorna o relatório consolidado + as métricas do Estágio 6 (Req. 10.7, 15.7).

    Deriva o :class:`RelatorioExecucao` dos resultados por Ability **persistidos**
    da Campanha (:meth:`SessionStateService.get_operation_results`) mais o DAG
    curado, via :class:`MedicaoService` (medição **pura e determinística** — sem
    rede/Docker/IA). As métricas (taxa de sucesso, cobertura procedural, tempo,
    nós falhos, telemetria por técnica e veredito) são recomputadas a cada leitura
    a partir dos resultados persistidos, de modo que o relatório reflita sempre o
    estado atual da execução (Req. 15.1–15.7).

    Os resultados por Ability de **todas** as Operações da Campanha são achatados
    (uma Campanha curada executa uma única Operação; defensivamente consolidamos
    todas) e normalizados em :class:`ResultadoAbility` para a medição e em
    :class:`Stage3AbilityResult` para o relatório. O flag
    ``ambiente_desprovisionado`` reflete o resultado agregado das Operações
    finalizadas: como o ambiente curado é **sempre** desprovisionado ao final de
    uma execução concluída (Req. 10.8, 12.1), ele é ``True`` quando há ao menos uma
    Operação finalizada; caso contrário ``False`` (nenhuma execução concluída
    ainda).

    Um *slug* de Caso desconhecido é 404. Um Caso sem execução ainda devolve 200
    com métricas zeradas (DAG como denominador, zero técnicas em sucesso) — a tela
    de relatório sempre recebe um resultado reportável.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    # Caso ausente/malformado → 422 (consistente com as demais rotas).
    resultado_caso = case_service.try_load_case(caso)
    if not resultado_caso.ok or resultado_caso.case is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                resultado_caso.error.detail
                if resultado_caso.error
                else f"Falha ao ler o Caso {caso!r}."
            ),
        )
    case = resultado_caso.case

    operacoes = SessionStateService(db).get_operation_results(caso)
    resultados_ability: list[ResultadoAbility] = []
    resultados_stage3: list[Stage3AbilityResult] = []
    algum_finalizado = False
    for operacao in operacoes:
        if operacao.estado is OperationState.FINISHED:
            algum_finalizado = True
        for ability in operacao.resultados:
            resultados_ability.append(
                ResultadoAbility(ability_id=ability.ability_id, status=ability.status)
            )
            resultados_stage3.append(
                Stage3AbilityResult(
                    ability_id=ability.ability_id or "",
                    status=ability.status,
                    saida_comando=ability.saida_comando,
                )
            )

    return MedicaoService().relatorio_do_caso(
        caso=case,
        resultados_ability=resultados_ability,
        resultados_stage3=resultados_stage3,
        ambiente_desprovisionado=algum_finalizado,
    )


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
# Estágio 3 — Geração do DAG: listagem e escolha de DAGs persistidos
# (Req. 7.2, 7.3; ``ESTAGIO_GERACAO_DAG == 3`` no esquema de seis Estágios).
# A visão do Estágio é servida por ``GET .../estagio/3`` (DagView/Mermaid, acima);
# estes endpoints mantêm/reusam os DAGs *persistidos* (com ``dag_id``): a lista
# ``GET .../dags``, o detalhe ``GET .../dags/{dag_id}`` e a escolha
# ``POST .../estagio/3/escolher-dag`` (conclui o Estágio 3). Handlers finos:
# constroem o DagService com a sessão do request (get_db) e delegam, mapeando o
# resultado tipado de escolher_dag para o código HTTP correspondente.
# ---------------------------------------------------------------------------


@router.get(
    "/casos/{caso}/dags",
    response_model=DagListResponse,
    summary="Lista os DAGs persistidos de uma Campanha (Estágio 3)",
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
    summary="Grafo + Mermaid de um DAG persistido (Estágio 3)",
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
    "/casos/{caso}/estagio/3/escolher-dag",
    response_model=CaseStageStates,
    summary="Escolhe um DAG para a execução, concluindo o Estágio 3 (Geração do DAG)",
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
    """Escolhe um DAG para a execução, concluindo o Estágio 3 (Req. 7.2, 7.3).

    No esquema de **seis** Estágios, a Geração do DAG é o **Estágio 3**
    (``ESTAGIO_GERACAO_DAG == 3``), então a rota passa a ``.../estagio/3/escolher-dag``,
    alinhada à fonte única do número do Estágio em ``stage_service``. Delega a
    :meth:`DagService.escolher_dag`, que grava ``dag_escolhido_id`` no ``StageRun``
    do Estágio 3 (Geração do DAG) e o conclui (progresso 100), liberando o Estágio
    4 (Operacionalização) pelo gate sequencial. O resultado tipado
    (:class:`EscolhaDagResult`) é mapeado para o HTTP correspondente — o handler
    permanece fino, sem reimplementar regra de negócio:

    * **sucesso** → 200 com o estado por Estágio já com o Estágio 3 ``concluido``
      e o 4 liberado (:class:`CaseStageStates`, mesma forma de
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
        # Reler o estado por Estágio já com o Estágio 3 concluído e o 4 liberado.
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
    summary="Conclui o Estágio 3 (Geração do DAG) e destrava o Estágio 4",
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
    """Persiste a conclusão do Estágio 3 (Geração do DAG) — Req. 1.3, 1.4, 1.7.

    No esquema de seis Estágios, a Geração do DAG é o Estágio 3. O DAG curado já
    existe, então "avançar do 3" é, semanticamente, "concluir o 3". Respeita o
    bloqueio sequencial: só conclui o 3 se o **Estágio 2** (Seleção da campanha)
    já estiver ``concluido`` (senão 409, sem persistir), de modo que o gate recai
    sobre o Estágio 2.
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
