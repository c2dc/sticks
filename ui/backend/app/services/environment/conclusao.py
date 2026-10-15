"""ConclusaoEstagio6Service — desprovisionamento + conclusão do Estágio 6 (task 8.4).

O Estágio 6 (Execução + medição + relatório) tem três fases já construídas por
tarefas anteriores: a **execução** reusa o ``OperationRunner`` com os quatro
gates (task 8.2, :class:`~app.services.environment.execucao.ExecucaoStageService`);
a **medição + relatório** deriva as métricas de suficiência procedural e
consolida o :class:`~app.services.environment.relatorio.RelatorioExecucao` (task
8.3, :class:`~app.services.environment.medicao.MedicaoService`); e o Estágio 5
(task 7.x) definiu a costura :class:`~app.services.environment.provisionador.ProvisionadorDeAmbiente`
com o método ``desprovisionar``.

Esta tarefa é a **costura de conclusão** que amarra as três fases na ordem exigida
pelo design (Req. 10.8, 10.9, 12):

1. **Executa** o Estágio 6 reusando o ``ExecucaoStageService`` com a **conclusão
   deferida** (``concluir=False``) — a conclusão do Estágio **não** acontece já na
   execução; é esta costura que decide quando concluir (Req. 10.9).
2. **Mede e gera o relatório** a partir dos resultados da execução + o DAG curado
   (via o ``MedicaoService``), em sucesso **ou** falha das técnicas.
3. **SEMPRE desprovisiona** o ambiente da campanha via
   :meth:`ProvisionadorDeAmbiente.desprovisionar` — em sucesso **ou** falha das
   técnicas, mesmo quando a execução foi recusada por um gate ou abortada (Req.
   10.8, 12.1). O teardown remove os contêineres criados (Req. 12.2); uma falha
   de teardown é reportada identificando os **recursos remanescentes** (Req.
   12.3), sem derrubar o fluxo.
4. **Conclui o Estágio 6** pela via única de conclusão (``persist_stage_completion``)
   **somente** quando (a) a execução concluiu de fato (``COMPLETED`` + ``FINISHED``),
   (b) o ambiente foi desprovisionado e (c) os resultados + o relatório estão
   persistidos (Req. 10.9). Se qualquer condição falha, o Estágio **não** conclui.

O relatório carimba ``ambiente_desprovisionado`` com o resultado real do teardown
(Req. 12), de modo que a Saída do Estágio 6 reflita se o ambiente foi de fato
limpo. A persistência do :class:`RelatorioExecucao` é delegada a um
**persistidor injetável** (``relatorio_sink``) — a tabela ``relatorios_execucao``
é a task 10.1/10.2; aqui a costura só exige que *algum* sink confirme a
persistência antes de concluir, e os testes injetam um sink em memória.

Como no restante do pacote, as dependências externas (o provisionador, o sink de
relatório, o serviço de execução/medição) são **injetadas**, de modo que os
testes substituam o provisionador por um *fake* (um mock que registra o teardown)
sem Caldera nem Docker reais.

_Requisitos: 10.8, 10.9, 12.1, 12.2, 12.3_
"""

from __future__ import annotations

import datetime as dt
from typing import Callable, Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.services.case_service import CaseData, CaseService
from app.services.environment.execucao import (
    ExecucaoResult,
    ExecucaoStageService,
    RunnerFactory,
)
from app.services.environment.medicao import MedicaoService, ResultadoAbility
from app.services.environment.operacionalizacao import ParametrosDeAmbiente
from app.services.environment.provisionador import (
    AmbienteProvisionado,
    DesprovisionamentoResult,
    ProvisionadorDeAmbiente,
)
from app.services.environment.relatorio import RelatorioExecucao
from app.services.stage.stage_service import Stage3AbilityResult


#: Um persistidor do :class:`RelatorioExecucao` da execução. A tabela
#: ``relatorios_execucao`` é a task 10.x; esta costura só exige que *algum* sink
#: confirme a persistência (``True``) antes de concluir o Estágio 6 (Req. 10.9).
#: Injetável para que os testes usem um sink em memória sem a tabela.
RelatorioSink = Callable[[RelatorioExecucao], bool]


class ConclusaoEstagio6Result(BaseModel):
    """Resultado tipado da conclusão do Estágio 6 — execução + relatório + teardown.

    Projeção serializável do fluxo completo do Estágio 6, para a camada de API
    (task 8.5) e para os testes. Carrega o resultado da execução, o relatório
    consolidado, o resultado do desprovisionamento e se o Estágio foi de fato
    concluído.

    Attributes:
        caso_id: O *slug* do Caso cujo Estágio 6 foi executado.
        execucao: O :class:`~app.services.environment.execucao.ExecucaoResult` da
            execução (com a conclusão deferida — ``execucao.concluido`` é sempre
            ``False`` aqui; a conclusão real é ``concluido`` abaixo).
        relatorio: O :class:`RelatorioExecucao` consolidado (métricas + resultados
            + flag de desprovisionamento), gerado em sucesso **ou** falha das
            técnicas (Req. 10.6, 10.7).
        desprovisionamento: O :class:`DesprovisionamentoResult` do teardown —
            **sempre** executado (Req. 10.8, 12.1); em falha identifica os
            recursos remanescentes (Req. 12.3).
        relatorio_persistido: ``True`` sse o relatório foi persistido pelo sink
            (pré-condição da conclusão, Req. 10.9).
        concluido: ``True`` sse o Estágio 6 foi marcado ``concluido`` por esta
            chamada — isto é, a execução concluiu, o ambiente foi desprovisionado
            e os resultados + relatório estão persistidos (Req. 10.9).
    """

    caso_id: str
    execucao: ExecucaoResult
    relatorio: RelatorioExecucao
    desprovisionamento: DesprovisionamentoResult
    relatorio_persistido: bool = False
    concluido: bool = False


class ConclusaoEstagio6Service:
    """Costura de conclusão do Estágio 6 — teardown sempre + conclusão gated (task 8.4).

    Orquestra as três fases do Estágio 6 na ordem do design, **sem** reimplementar
    nenhuma delas: delega a execução ao
    :class:`~app.services.environment.execucao.ExecucaoStageService` (conclusão
    deferida), a medição/relatório ao
    :class:`~app.services.environment.medicao.MedicaoService`, e o teardown à
    costura :class:`~app.services.environment.provisionador.ProvisionadorDeAmbiente`
    (injetável). A única lógica própria é a **ordem** e o **gate de conclusão**
    (Req. 10.8, 10.9, 12).

    _Requisitos: 10.8, 10.9, 12.1, 12.2, 12.3_
    """

    def __init__(
        self,
        session: Session,
        *,
        runner_factory: RunnerFactory,
        provisionador: ProvisionadorDeAmbiente,
        relatorio_sink: RelatorioSink,
        case_service: Optional[CaseService] = None,
        medicao: Optional[MedicaoService] = None,
    ) -> None:
        """Monta a costura de conclusão do Estágio 6.

        Args:
            session: :class:`~sqlalchemy.orm.Session` aberta, injetada pelo
                chamador — repassada ao ``ExecucaoStageService`` e à via de
                conclusão. O serviço não a abre nem fecha.
            runner_factory: *Factory* ``(session) -> OperationRunner`` repassada ao
                ``ExecucaoStageService`` (os testes injetam um runner falso ou um
                real com *fakes*).
            provisionador: A costura
                :class:`~app.services.environment.provisionador.ProvisionadorDeAmbiente`
                cujo ``desprovisionar`` é **sempre** chamado ao final (Req. 10.8,
                12.1). Os testes injetam um *fake* que registra o teardown.
            relatorio_sink: Persistidor do :class:`RelatorioExecucao`
                (``True`` quando persistiu). Pré-condição da conclusão (Req.
                10.9).
            case_service: :class:`CaseService` para resolver/ler o Caso curado
                (DAG + Abilities para a medição). Default: produção.
            medicao: O :class:`MedicaoService` (puro) usado para medir/consolidar.
                Default: uma instância nova.
        """
        self._session = session
        self._case_service: CaseService = (
            case_service if case_service is not None else CaseService()
        )
        self._execucao = ExecucaoStageService(
            session,
            runner_factory=runner_factory,
            case_service=self._case_service,
        )
        self._provisionador = provisionador
        self._relatorio_sink = relatorio_sink
        self._medicao = medicao if medicao is not None else MedicaoService()

    def executar_e_concluir(
        self,
        caso: str,
        *,
        confirmado: bool,
        ambiente: AmbienteProvisionado,
        gerado_em: Optional[dt.datetime] = None,
    ) -> ConclusaoEstagio6Result:
        """Executa, mede, gera o relatório, **desprovisiona** e conclui (Req. 10.8–10.9, 12).

        O fluxo, na ordem do design:

        1. Executa o Estágio 6 com a **conclusão deferida** (``concluir=False``):
           o ``ExecucaoStageService`` aplica os quatro gates e, em sucesso,
           persiste os ``AbilityResult``/``AuditLogEntry`` por comando — mas
           **não** conclui o Estágio ainda (Req. 10.9).
        2. Mede a suficiência procedural e consolida o :class:`RelatorioExecucao`
           a partir dos resultados da execução + o DAG curado, em sucesso **ou**
           falha das técnicas (Req. 10.4–10.7).
        3. **SEMPRE** desprovisiona o ``ambiente`` via
           :meth:`ProvisionadorDeAmbiente.desprovisionar` — mesmo quando a
           execução foi recusada/abortada ou falhou (Req. 10.8, 12.1); carimba o
           resultado em ``relatorio.ambiente_desprovisionado`` (Req. 12). Uma
           falha de teardown é reportada com os recursos remanescentes (Req.
           12.3) e **não** derruba o fluxo.
        4. Persiste o relatório (via o ``relatorio_sink``) e conclui o Estágio 6
           **somente** quando a execução concluiu, o ambiente foi desprovisionado
           e o relatório foi persistido (Req. 10.9). Caso contrário o Estágio
           **não** conclui.

        Args:
            caso: O *slug* do Caso curado a executar.
            confirmado: Confirmação explícita da execução (gate 1 do runner).
            ambiente: O :class:`AmbienteProvisionado` do Estágio 5 — o alvo do
                desprovisionamento ao final (Req. 12.1, 12.2).
            gerado_em: Instante de geração do relatório; default: agora (UTC).

        Returns:
            O :class:`ConclusaoEstagio6Result` com a execução, o relatório (com a
            flag de desprovisionamento), o resultado do teardown e se o Estágio 6
            foi concluído.
        """
        case = self._load_case(caso)

        # 1) Execução com conclusão DEFERIDA (a conclusão é decidida aqui, após o
        #    desprovisionamento — Req. 10.9).
        execucao = self._execucao.executar(
            caso, confirmado=confirmado, concluir=False
        )

        # 2) Medição + relatório, em sucesso OU falha das técnicas (Req. 10.6, 10.7).
        resultados_ability = [
            ResultadoAbility.de_execucao(r) for r in execucao.resultados
        ]
        resultados_stage3 = [
            Stage3AbilityResult(
                ability_id=r.ability_id or "",
                status=r.status,
                saida_comando=r.saida,
            )
            for r in execucao.resultados
        ]

        # 3) SEMPRE desprovisionar — em sucesso ou falha (Req. 10.8, 12.1). O
        #    teardown nunca derruba o fluxo; uma falha é reportada no resultado.
        desprovisionamento = self._desprovisionar(ambiente)

        relatorio = self._medicao.relatorio_do_caso(
            caso=case,
            resultados_ability=resultados_ability,
            resultados_stage3=resultados_stage3,
            ambiente_desprovisionado=desprovisionamento.desprovisionado,
            gerado_em=gerado_em,
        )

        # 4) Persistir o relatório (pré-condição da conclusão, Req. 10.9).
        relatorio_persistido = bool(self._relatorio_sink(relatorio))

        # Conclusão SOMENTE quando: execução concluiu de fato E ambiente
        # desprovisionado E resultados + relatório persistidos (Req. 10.9).
        concluido = False
        if (
            self._execucao.executou_com_sucesso(execucao)
            and desprovisionamento.desprovisionado
            and relatorio_persistido
        ):
            concluido = self._execucao.concluir_estagio6(caso)

        return ConclusaoEstagio6Result(
            caso_id=caso,
            execucao=execucao,
            relatorio=relatorio,
            desprovisionamento=desprovisionamento,
            relatorio_persistido=relatorio_persistido,
            concluido=concluido,
        )

    # -- helpers -------------------------------------------------------------

    def _desprovisionar(
        self, ambiente: AmbienteProvisionado
    ) -> DesprovisionamentoResult:
        """Chama ``desprovisionar`` de forma defensiva — nunca derruba o fluxo.

        O desprovisionamento é **sempre** tentado (Req. 10.8, 12.1). Uma exceção
        inesperada do provisionador é capturada e virada num resultado de falha
        que identifica os recursos remanescentes (os contêineres-alvo do
        ambiente, Req. 12.3) — a falha é reportada, não propagada, para não
        mascarar a execução já realizada nem impedir o relatório.
        """
        try:
            return self._provisionador.desprovisionar(ambiente)
        except Exception as exc:  # defensivo: teardown nunca derruba o Estágio 6
            return DesprovisionamentoResult(
                desprovisionado=False,
                recursos_remanescentes=list(ambiente.containers_alvo),
                origem=ambiente.origem,
                mensagem=(
                    "Falha ao desprovisionar o ambiente da campanha; recursos "
                    f"permaneceram provisionados: {exc}"
                ),
            )

    def _load_case(self, caso: str) -> CaseData:
        """Resolve e lê o Caso curado ``caso`` (DAG + Abilities para a medição).

        Mesma distinção da costura de execução: *slug* fora do catálogo é
        :class:`KeyError`; Caso ausente/malformado é :class:`ValueError`
        descritivo (a API mapeia para 404/422).
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
    "RelatorioSink",
    "ConclusaoEstagio6Result",
    "ConclusaoEstagio6Service",
]
