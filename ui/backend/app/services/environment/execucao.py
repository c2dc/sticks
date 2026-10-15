"""ExecucaoStageService — costura da Execução do Estágio 6 (task 8.2).

O Estágio 6 (Execução + medição + relatório) do pipeline de seis Estágios
**reusa integralmente** o :class:`~app.services.operation.runner.OperationRunner`
já construído (o antigo Estágio 5 "Execução da Campanha" do esquema de cinco
Estágios), sem reimplementar nada da sua lógica de segurança. Este módulo é a
**costura** *session-bound* que amarra o runner ao número de Estágio novo
(:data:`~app.services.stage.stage_service.ESTAGIO_EXECUCAO` ``= 6``), expondo o
**preview** como pré-condição visível e marcando a conclusão do Estágio 6 pela
única via de conclusão (:meth:`SessionStateService.persist_stage_completion`).

É a contraparte, para a Execução, da auto-conclusão do ambiente
(:class:`~app.services.environment.stage_service.EnvironmentStageService`): mesma
costura de *service com ``Session`` injetada*, mesma dependência externa
**injetável** (aqui o ``OperationRunner`` via uma pequena *factory*, nos moldes de
``api/emulacao.py``), e mesma via de persistência da conclusão.

Os quatro gates — REUSO total do ``OperationRunner``
----------------------------------------------------
A :meth:`ExecucaoStageService.executar` apenas delega a
:meth:`OperationRunner.run`, que já aplica, **na ordem do design** (Req. 13.1), os
quatro gates antes de iniciar qualquer Operação:

1. **Confirmação explícita** (Req. 13.2) — a Operação inicia *se e somente se*
   ``confirmado is True``; caso contrário nada é executado e o estado volta a
   ``NOT_STARTED`` (``RunOutcome.NOT_CONFIRMED``).
2. **Contenção de destinos** (``validate_abilities``, Req. 13.3, 13.4, 13.5) — se
   algum comando tem destino externo, a Operação é **recusada** e não inicia; a
   violação identifica a Ability/comando (``RunOutcome.CONTAINMENT_REFUSED``).
3. **Isolamento dos containers-alvo** (read-only, Req. 14.4, 14.5) — se algum
   container-alvo não está isolado, a Operação é **abortada** e não inicia
   (``RunOutcome.ISOLATION_FAILED``).
4. **Disponibilidade do Caldera** (Req. 10.1) — ``ensure_available`` levanta
   :class:`~app.services.caldera.CalderaUnavailable` quando o Caldera não responde
   em 10s; nenhuma Operação é criada antes disso.

**Execução ÚNICA** no caso curado (Req. 10.3): o runner conduz a Operação uma só
vez — **sem retry automático** (não-objetivo, Req. 18.3). Esta costura **não**
adiciona nenhum laço de repetição; chama ``run(...)`` exatamente uma vez por
solicitação de execução.

**Persistência por comando** (Req. 10.2): o próprio runner persiste um
:class:`~app.models.domain.AbilityResult` por Ability e grava uma linha de
:class:`~app.models.domain.AuditLogEntry` por comando executado (via o
``AuditLogger`` ligado à mesma ``Session``). Esta costura não duplica essa
persistência — apenas repassa a ``Session`` ao runner.

**Preview como pré-condição visível** (Req. 13.6, 13.7): antes de qualquer
execução, :meth:`ExecucaoStageService.previsualizar` expõe
:func:`~app.services.containment.preview_abilities` — a lista completa de comandos
concretos com o container-alvo de cada e a sinalização de destino externo. É a
mesma apresentação que o modal de confirmação renderiza *antes* da confirmação;
o preview **apresenta**, não **executa** nem cria Operação.

Conclusão do Estágio 6
----------------------
Somente quando a execução conclui de fato (``RunOutcome.COMPLETED`` **e**
``OperationState.FINISHED``) o Estágio 6 (:data:`ESTAGIO_EXECUCAO`) é marcado
``concluido`` pela via única de conclusão
(:meth:`SessionStateService.persist_stage_completion`, transacional e
idempotente). ``ABORTED`` (falha de trilha de auditoria, Req. 10.2),
``NOT_CONFIRMED`` e as recusas por gate **não** concluem o Estágio. Uma falha ao
persistir a conclusão **não** derruba a execução já realizada — ela é um sinal
tipado no resultado.

.. note::
    O **desprovisionamento** ao final (Req. 10.8, 12) e a **medição** de
    suficiência procedural + o relatório (Req. 15) são costurados pelas tarefas
    8.3/8.4 sobre esta mesma costura; aqui a conclusão reflete apenas a execução
    (os endpoints REST são a tarefa 8.5). Esta tarefa confirma/wira a costura de
    Execução do Estágio 6 reusando o ``OperationRunner`` com os quatro gates.

_Requisitos: 10.1, 10.2, 10.3, 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 14.4,
14.5, 18.3_
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.enums import AbilityResultStatus, OperationState
from app.services.caldera import CalderaUnavailable
from app.services.case_service import AbilityData, CaseData, CaseService
from app.services.containment import EmulationPreview, preview_abilities
from app.services.operation.runner import (
    OperationRunner,
    OperationRunResult,
    RunOutcome,
)
from app.services.session.session_state_service import SessionStateService
from app.services.stage.stage_service import ESTAGIO_EXECUCAO


# ---------------------------------------------------------------------------
# Factory do runner (injeção — mesmo padrão de ``api/emulacao.py``)
# ---------------------------------------------------------------------------

#: Uma *factory* que constrói um :class:`OperationRunner` ligado a uma ``Session``
#: de requisição. Injetada para que esta costura permaneça fina e os testes
#: substituam toda a construção (cliente Caldera + verificador de isolamento) por
#: uma única dependência — sem Caldera/Docker reais (mesmo contrato que o
#: ``RunnerFactory`` de ``api/emulacao.py``).
RunnerFactory = Callable[[Session], OperationRunner]


# ---------------------------------------------------------------------------
# Adaptador de Abilities para o runner (executors como dict serializável)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RunnerAbility:
    """Visão da Ability curada que o runner/containment/preview consomem.

    O :class:`OperationRunner` monta o payload ``POST /api/v2/abilities`` do
    Caldera direto de ``ability.executors``, que precisa ser JSON-serializável. A
    :class:`~app.services.case_service.AbilityData` carrega modelos Pydantic
    :class:`Executor`, então este adaptador expõe a mesma superfície *duck-typed*
    (``ability_id``/``name``/``executors``) com ``executors`` como dicts planos
    ``{name, platform, command}``. Reusa exatamente a mesma projeção de
    ``api/emulacao.py`` para manter a carga coesa.
    """

    ability_id: str
    name: Optional[str]
    tactic: Optional[str]
    technique_name: Optional[str]
    technique_id: Optional[str]
    description: Optional[str]
    executors: tuple[dict[str, object], ...]


def _runner_abilities(abilities: list[AbilityData]) -> list[_RunnerAbility]:
    """Adapta :class:`AbilityData` curadas para abilities do runner.

    Converte cada :class:`Executor` Pydantic em dict plano para que o construtor
    de payload do Caldera no runner possa JSON-serializá-lo, preservando ordem e
    identidade.
    """
    return [
        _RunnerAbility(
            ability_id=a.ability_id,
            name=a.name,
            tactic=a.tactic,
            technique_name=a.technique_name,
            technique_id=a.technique_id,
            description=a.description,
            executors=tuple(e.model_dump() for e in a.executors),
        )
        for a in abilities
    ]


def _adversary_payload(case: CaseData) -> Optional[dict[str, object]]:
    """Monta o payload ``POST /api/v2/adversaries`` (v2 usa ``adversary_id``).

    Carregar o Adversary — com a sua ``atomic_ordering`` — antes da Operação é o
    que dá à Operação uma cadeia não vazia para executar na ordem do DAG (Req.
    10.3). Sem Adversary curado devolve ``None`` (a carga de Abilities ainda
    ocorre).
    """
    adversary = case.adversary
    if adversary is None:
        return None
    return {
        "adversary_id": adversary.id,
        "name": adversary.name or adversary.id,
        "description": adversary.description or "",
        "atomic_ordering": list(adversary.atomic_ordering),
    }


# ---------------------------------------------------------------------------
# Resultado tipado da execução do Estágio 6
# ---------------------------------------------------------------------------


class ExecucaoAbilityResult(BaseModel):
    """Resultado por Ability de uma execução do Estágio 6.

    Projeção serializável do
    :class:`~app.services.operation.runner.AbilityRunResult` que o runner produz e
    persiste, para compor o resultado tipado e os contratos JSON pt-BR.

    Attributes:
        ability_id: A Ability executada (``None`` quando o Caldera não associou o
            link a uma ability).
        status: O :class:`~app.models.enums.AbilityResultStatus` derivado.
        comando: O comando concreto executado (decodificado), se conhecido.
        container_destino: O rótulo do container-alvo usado na auditoria.
        saida: A saída (stdout/stderr) do comando, se coletada.
    """

    ability_id: Optional[str] = None
    status: AbilityResultStatus = AbilityResultStatus.PENDING
    comando: Optional[str] = None
    container_destino: str
    saida: Optional[str] = None


class ExecucaoResult(BaseModel):
    """Resultado tipado de :meth:`ExecucaoStageService.executar` (Estágio 6).

    Em vez de levantar exceções pelos caminhos esperados, a execução devolve este
    resultado tipado (mesmo contrato de ``ProvisionamentoAutoResult`` /
    ``StageCompletionResult``): ``iniciada`` indica se a Operação chegou a
    executar; ``resultado`` é o :class:`RunOutcome` do runner (confirmação,
    contenção, isolamento, disponibilidade ou execução/aborto); ``concluido``
    indica se o Estágio 6 passou a ``concluido`` por esta chamada.

    Attributes:
        caso_id: O Caso cuja execução foi solicitada.
        iniciada: ``True`` sse a Operação realmente iniciou na Caldera
            (``COMPLETED``/``ABORTED``).
        resultado: O :class:`RunOutcome` do runner (porquê terminou assim).
        estado: O :class:`OperationState` final persistido da Operação.
        operacao_id: O ``Operation.id`` local, quando uma Operação foi criada.
        caldera_operacao_id: O id da Operação na Caldera, quando criada.
        total_sucesso: Agregado de Abilities em ``sucesso`` (Req. 10.4 fonte).
        total_falha: Agregado de Abilities em ``falha``.
        resultados: Resultados por Ability, na ordem de execução.
        concluido: ``True`` sse o Estágio 6 foi marcado ``concluido`` por esta
            execução (somente ``COMPLETED`` + ``FINISHED``).
        conclusao_persistida: ``True`` sse a conclusão do Estágio 6 foi de fato
            salva; ``False`` se o ``commit`` da conclusão falhou (execução
            preservada, estado anterior do Estágio preservado).
        violacoes: Descrições das violações de contenção, quando recusada.
        containers_nao_isolados: Containers reprovados no isolamento, quando
            abortada por isolamento.
        mensagem: Texto pt-BR legível do resultado (resumo/razão), vindo do
            runner.
    """

    caso_id: str
    iniciada: bool = False
    resultado: RunOutcome = RunOutcome.NOT_CONFIRMED
    estado: OperationState = OperationState.NOT_STARTED
    operacao_id: Optional[int] = None
    caldera_operacao_id: Optional[str] = None
    total_sucesso: int = 0
    total_falha: int = 0
    resultados: list[ExecucaoAbilityResult] = Field(default_factory=list)
    concluido: bool = False
    conclusao_persistida: bool = False
    violacoes: list[str] = Field(default_factory=list)
    containers_nao_isolados: list[str] = Field(default_factory=list)
    mensagem: str = ""


# ---------------------------------------------------------------------------
# A costura session-bound da Execução do Estágio 6
# ---------------------------------------------------------------------------


class ExecucaoStageService:
    """Costura da Execução do Estágio 6 reusando o ``OperationRunner`` (task 8.2).

    Mesmo molde dos demais serviços *session-bound*
    (:class:`~app.services.environment.stage_service.EnvironmentStageService` /
    :class:`~app.services.dag.dag_service.DagService`): recebe uma
    :class:`~sqlalchemy.orm.Session` injetada e constrói o
    :class:`OperationRunner` através de uma *factory* **injetável**
    (``runner_factory``), de modo que os testes substituam toda a construção
    (cliente Caldera + verificador de isolamento) por *fakes* — sem Caldera nem
    Docker reais —, exatamente como ``api/emulacao.py`` faz. O :class:`CaseService`
    (resolução/leitura do Caso curado) também é injetável.

    A costura **não** reimplementa nenhum dos quatro gates nem a persistência por
    comando: ela delega a :meth:`OperationRunner.run` (fonte única da aplicação
    dos gates, da execução única e da auditoria por comando) e apenas:

    * expõe o **preview** como pré-condição visível (:meth:`previsualizar`);
    * chama ``run(...)`` **uma única vez** por solicitação (sem retry — Req. 18.3);
    * marca o Estágio 6 ``concluido`` pela via única de conclusão quando — e
      somente quando — a execução conclui (``COMPLETED`` + ``FINISHED``).

    _Requisitos: 10.1, 10.2, 10.3, 13.1, 13.2, 13.3, 13.6, 14.4, 14.5, 18.3_
    """

    def __init__(
        self,
        session: Session,
        *,
        runner_factory: RunnerFactory,
        case_service: Optional[CaseService] = None,
    ) -> None:
        """Monta a costura da Execução do Estágio 6.

        Args:
            session: :class:`~sqlalchemy.orm.Session` aberta, injetada pelo
                chamador. O serviço não a abre nem fecha; o ``OperationRunner``
                faz ``commit`` das transições/agregação e a conclusão do Estágio
                é persistida pela via única (``persist_stage_completion``).
            runner_factory: *Factory* ``(session) -> OperationRunner`` que constrói
                o runner ligado à ``Session`` da requisição. Injetada para manter
                a costura fina e permitir *override* total nos testes (cliente
                Caldera + verificador de isolamento), sem Caldera/Docker reais.
            case_service: :class:`CaseService` para resolver/ler o Caso curado
                (Abilities + Adversary). Default: uma instância de produção.
        """
        self._session = session
        self._runner_factory = runner_factory
        self._case_service: CaseService = (
            case_service if case_service is not None else CaseService()
        )

    @property
    def case_service(self) -> CaseService:
        """O :class:`CaseService` usado para resolver/ler o Caso curado."""
        return self._case_service

    # -- preview (pré-condição visível, Req. 13.6, 13.7) ---------------------

    def previsualizar(self, caso: str) -> EmulationPreview:
        """Expõe o preview dos comandos do Caso — pré-condição da execução.

        Constrói :func:`~app.services.containment.preview_abilities` sobre as
        Abilities curadas do Caso: a lista completa de comandos concretos com o
        container-alvo de cada e a sinalização de destino externo (Req. 13.6,
        13.7). **Nada é executado** e **nenhuma Operação é criada** — é a
        apresentação que o modal de confirmação mostra *antes* de qualquer
        confirmação. Destinos externos são **sinalizados**, não recusados aqui; a
        recusa é um dos gates da :meth:`executar` (via o runner).
        """
        case = self._load_case(caso)
        return preview_abilities(case.abilities)

    # -- execução (REUSO total do OperationRunner, Req. 13.1) ----------------

    def executar(
        self, caso: str, *, confirmado: bool, concluir: bool = True
    ) -> ExecucaoResult:
        """Executa o Estágio 6 do Caso ``caso`` reusando o ``OperationRunner``.

        Resolve as Abilities + o Adversary curados do Caso, constrói o
        :class:`OperationRunner` ligado à ``Session`` da requisição (via a
        ``runner_factory`` injetável) e chama :meth:`OperationRunner.run`
        **exatamente uma vez** (execução única, sem retry — Req. 10.3, 18.3). O
        runner aplica os quatro gates na ordem do design (confirmação → contenção
        → isolamento → disponibilidade, Req. 13.1), persiste um ``AbilityResult``
        por Ability e uma linha de ``AuditLogEntry`` por comando (Req. 10.2).

        Esta costura **não** reimplementa nenhum gate nem a auditoria; apenas
        projeta o :class:`OperationRunResult` tipado do runner num
        :class:`ExecucaoResult` e, **somente** quando a execução conclui de fato
        (``COMPLETED`` + ``FINISHED``), marca o Estágio 6
        (:data:`ESTAGIO_EXECUCAO`) ``concluido`` pela via única de conclusão
        (:meth:`SessionStateService.persist_stage_completion`). Uma falha ao
        persistir a conclusão **não** derruba a execução já realizada — é um sinal
        tipado em ``conclusao_persistida=False``.

        A :class:`~app.services.caldera.CalderaUnavailable` do gate 4 é deixada
        **propagar** (o chamador/endpoint a mapeia para HTTP 503, Req. 10.1):
        nenhuma Operação é criada antes do gate passar, então o estado permanece
        intocado.

        Quando ``concluir`` é ``False`` a execução roda normalmente mas a
        **conclusão do Estágio 6 é deferida** ao chamador — é o ponto que a
        costura de conclusão do Estágio 6 (:class:`ConclusaoEstagio6Service`,
        task 8.4) usa para só concluir **depois** de medir, gerar o relatório e
        **desprovisionar** o ambiente (Req. 10.9). O default ``True`` preserva o
        comportamento histórico (execução + conclusão imediata).
        """
        case = self._load_case(caso)
        adversary_id = case.adversary.id if case.adversary else ""

        runner = self._runner_factory(self._session)
        # Execução ÚNICA — chamada uma só vez, sem laço de retry (Req. 10.3, 18.3).
        result = runner.run(
            caso_id=caso,
            abilities=_runner_abilities(case.abilities),
            adversary_id=adversary_id,
            confirmado=confirmado,
            adversary_payload=_adversary_payload(case),
        )
        return self._to_result(caso, result, concluir=concluir)

    def executou_com_sucesso(self, result: ExecucaoResult) -> bool:
        """``True`` sse a execução de ``result`` concluiu de fato (COMPLETED + FINISHED).

        Critério único de "a execução concluiu" (Req. 10.9) reusado pela costura
        de conclusão do Estágio 6: só uma execução ``COMPLETED`` + ``FINISHED``
        habilita a conclusão (após o desprovisionamento). ``ABORTED`` /
        ``NOT_CONFIRMED`` / recusa por gate **não** habilitam a conclusão.
        """
        return (
            result.resultado is RunOutcome.COMPLETED
            and result.estado is OperationState.FINISHED
        )

    def concluir_estagio6(self, caso: str) -> bool:
        """Marca o Estágio 6 ``concluido`` pela via única; devolve se persistiu.

        Delega a :meth:`SessionStateService.persist_stage_completion`
        (transacional e idempotente). Chamada pela costura de conclusão (task
        8.4) **somente depois** de a execução concluir e o ambiente ser
        desprovisionado (Req. 10.9). Uma falha de persistência devolve ``False``
        sem derrubar a execução já realizada.
        """
        completion = SessionStateService(self._session).persist_stage_completion(
            caso, ESTAGIO_EXECUCAO
        )
        return completion.saved

    # -- projeção + conclusão ------------------------------------------------

    def _to_result(
        self, caso: str, result: OperationRunResult, *, concluir: bool = True
    ) -> ExecucaoResult:
        """Projeta o resultado do runner e conclui o Estágio 6 em caso de sucesso.

        Quando ``concluir`` é ``False`` a projeção é feita mas a conclusão é
        **deferida** (``concluido``/``conclusao_persistida`` permanecem ``False``);
        o chamador conclui depois do desprovisionamento (Req. 10.9).
        """
        resultados = [
            ExecucaoAbilityResult(
                ability_id=ar.ability_id,
                status=ar.status,
                comando=ar.command,
                container_destino=ar.container_destino,
                saida=ar.output,
            )
            for ar in result.ability_results
        ]
        violacoes = (
            result.containment.describe_violations()
            if result.outcome is RunOutcome.CONTAINMENT_REFUSED and result.containment
            else []
        )
        containers_nao_isolados = (
            [v.container for v in result.isolation.offending]
            if result.outcome is RunOutcome.ISOLATION_FAILED and result.isolation
            else []
        )

        concluido = False
        conclusao_persistida = False
        # Conclusão SOMENTE quando a execução conclui de fato (COMPLETED +
        # FINISHED). ABORTED/NOT_CONFIRMED/contenção/isolamento NÃO concluem.
        # Quando ``concluir`` é False a conclusão é deferida ao chamador (a
        # costura da task 8.4 só conclui após o desprovisionamento — Req. 10.9).
        if (
            concluir
            and result.outcome is RunOutcome.COMPLETED
            and result.state is OperationState.FINISHED
        ):
            completion = SessionStateService(self._session).persist_stage_completion(
                caso, ESTAGIO_EXECUCAO
            )
            conclusao_persistida = completion.saved
            # Uma falha de persistência NÃO derruba a execução já realizada; o
            # Estágio não é marcado concluído, mas o resultado da execução é
            # devolvido integralmente (estado anterior do Estágio preservado).
            concluido = completion.saved

        return ExecucaoResult(
            caso_id=caso,
            iniciada=result.started,
            resultado=result.outcome,
            estado=result.state,
            operacao_id=result.operation_id,
            caldera_operacao_id=result.caldera_operation_id,
            total_sucesso=result.total_sucesso,
            total_falha=result.total_falha,
            resultados=resultados,
            concluido=concluido,
            conclusao_persistida=conclusao_persistida,
            violacoes=list(violacoes),
            containers_nao_isolados=containers_nao_isolados,
            mensagem=result.message,
        )

    # -- helpers -------------------------------------------------------------

    def _load_case(self, caso: str) -> CaseData:
        """Resolve e lê o Caso curado ``caso``, levantando por caminhos inválidos.

        Um *slug* fora do catálogo curado é um :class:`KeyError`; um arquivo de
        Caso ausente/malformado é um :class:`ValueError` descritivo — a mesma
        distinção que a camada de API mapeia para 404/422 (os endpoints são a
        tarefa 8.5). O :class:`CaseData` devolvido expõe as Abilities e o
        Adversary que a execução precisa.
        """
        if caso not in self._case_service.list_cases():
            raise KeyError(f"Caso desconhecido: {caso!r}.")
        resultado = self._case_service.try_load_case(caso)
        if not resultado.ok or resultado.case is None:
            detalhe = (
                resultado.error.detail
                if resultado.error
                else f"Falha ao ler o Caso {caso!r}."
            )
            raise ValueError(detalhe)
        return resultado.case


__all__ = [
    "RunnerFactory",
    "ExecucaoAbilityResult",
    "ExecucaoResult",
    "ExecucaoStageService",
    "CalderaUnavailable",
]
