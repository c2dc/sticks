"""MedicaoService — medição de suficiência procedural do Estágio 6 (task 8.3).

Este módulo é a camada de **medição** da Saída do Estágio 6 (Execução + medição
+ relatório). A execução em si **reusa** o
:class:`~app.services.operation.runner.OperationRunner` (costurado pela task 8.2
em :class:`~app.services.environment.execucao.ExecucaoStageService`), que
persiste um :class:`~app.models.domain.AbilityResult` por Ability; esta tarefa
**deriva** as :class:`~app.services.environment.relatorio.MetricasSuficiencia` a
partir desses resultados por Ability **mais o DAG curado** e consolida tudo num
:class:`~app.services.environment.relatorio.RelatorioExecucao` (Req. 10.4–10.7,
15.1–15.7).

O que é derivado, e como
------------------------
A unidade de medição é a **técnica do DAG** (um nó estrutural com ``technique_id``;
Req. 15.1, 15.3). Os resultados de execução vêm por **Ability**; cada Ability
curada carrega o seu ``technique_id`` (:class:`~app.services.case_service.AbilityData`),
então a ponte é ``ability_id`` → ``technique_id`` → nó do DAG:

* **total_tecnicas_dag** — o número de técnicas **distintas** do DAG (nós com
  ``technique_id``, de-duplicados por ``technique_id`` na ordem dos nós), o
  denominador de ``taxa_sucesso`` e ``cobertura_procedural`` (Req. 15.1, 15.3).
* **tecnicas_sucesso** — quantas técnicas do DAG tiveram ao menos um resultado de
  Ability em ``sucesso`` (uma técnica pode mapear mais de uma Ability).
* **taxa_sucesso** — ``tecnicas_sucesso / total_tecnicas_dag`` ∈ ``[0, 1]``
  (``0.0`` quando o DAG não tem técnicas, para evitar divisão por zero — Req.
  15.1).
* **cobertura_procedural** — a proporção **efetivamente executável**: as técnicas
  do DAG que foram **tentadas** (algum resultado que saiu de ``pendente``), sobre
  o total — ∈ ``[0, 1]`` (Req. 15.3).
* **nos_falhos** — as técnicas cujo status consolidado é ``falha`` (falhou e não
  teve nenhum sucesso), na ordem do DAG, cada uma identificada pelo ``node_id`` do
  nó representante e anotada com a **etapa do DAG** onde a falha ocorreu (Req.
  15.2).
* **tempo por técnica / total** (Req. 15.4) e **telemetria por técnica** (Req.
  15.5), consolidados dos resultados por Ability quando disponíveis.
* **veredito_sucesso** — o veredito geral: ``True`` sse o DAG não é vazio,
  **todas** as suas técnicas tiveram sucesso e nenhum nó falhou (Req. 15.6, 10.5).

A medição é **pura e determinística** — sem rede, Docker ou IA. Opera sobre
estruturas em memória (os resultados por Ability + o DAG + o mapa
``ability_id`` → técnica), de modo que os testes a exercitem sem banco. Para o
caminho de produção, :meth:`MedicaoService.medir_do_caso` resolve o DAG curado e
o mapa de técnicas a partir de um :class:`~app.services.case_service.CaseData`.

_Requisitos: 10.4, 10.5, 10.6, 10.7, 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7_
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from app.models.enums import AbilityResultStatus
from app.services.case_service import CaseData, DagData, DagNode
from app.services.environment.relatorio import (
    MetricasSuficiencia,
    MetricaTecnica,
    RelatorioExecucao,
)
from app.services.stage.stage_service import Stage3AbilityResult


# ---------------------------------------------------------------------------
# Entrada normalizada da medição (um resultado por Ability executada)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultadoAbility:
    """A entrada normalizada da medição — um resultado por Ability executada.

    Camada fina e neutra sobre as várias fontes de resultado por Ability do
    sistema (o :class:`~app.services.stage.stage_service.Stage3AbilityResult`
    histórico ou o
    :class:`~app.services.environment.execucao.ExecucaoAbilityResult` da costura
    da task 8.2). A medição consome **só** o que precisa, mantendo-se desacoplada
    da forma concreta do resultado.

    Attributes:
        ability_id: A Ability executada (a ponte para a técnica via o mapa
            ``ability_id`` → ``technique_id``).
        status: O :class:`~app.models.enums.AbilityResultStatus` final.
        tempo_execucao_s: Tempo de execução da Ability em segundos, quando
            medido (Req. 15.4); ``None`` quando indisponível.
        telemetria: Sinais de telemetria observados (processo/rede/auth/arquivo)
            durante a execução (Req. 15.5).
    """

    ability_id: Optional[str] = None
    status: AbilityResultStatus = AbilityResultStatus.PENDING
    tempo_execucao_s: Optional[float] = None
    telemetria: tuple[str, ...] = ()

    @classmethod
    def de_stage3(cls, resultado: Stage3AbilityResult) -> "ResultadoAbility":
        """Normaliza um :class:`Stage3AbilityResult` (sem tempo/telemetria)."""
        return cls(ability_id=resultado.ability_id, status=resultado.status)

    @classmethod
    def de_execucao(cls, resultado: object) -> "ResultadoAbility":
        """Normaliza um resultado *duck-typed* (``ability_id``/``status``).

        Aceita qualquer objeto que exponha ``ability_id`` e ``status`` — por
        exemplo o
        :class:`~app.services.environment.execucao.ExecucaoAbilityResult` da
        costura da task 8.2 — e os campos opcionais ``tempo_execucao_s`` e
        ``telemetria`` quando presentes.
        """
        telemetria = getattr(resultado, "telemetria", ()) or ()
        return cls(
            ability_id=getattr(resultado, "ability_id", None),
            status=getattr(resultado, "status", AbilityResultStatus.PENDING),
            tempo_execucao_s=getattr(resultado, "tempo_execucao_s", None),
            telemetria=tuple(telemetria),
        )


# ---------------------------------------------------------------------------
# Técnica do DAG (unidade de medição) — nó estrutural com technique_id
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TecnicaDag:
    """Uma técnica distinta do DAG — a unidade de medição (Req. 15.1, 15.3).

    Attributes:
        technique_id: O ``technique_id`` da técnica.
        node_id: O ``node_id`` do nó representante (o 1º nó dessa técnica), para
            anotar a :class:`MetricaTecnica` e os ``nos_falhos``.
        etapa: A etapa do DAG (``node_type`` → ``tactic`` → ``node_index``) usada
            para anotar onde uma falha ocorreu (Req. 15.2).
    """

    technique_id: str
    node_id: Optional[str] = None
    etapa: Optional[str] = None


def _etapa_do_no(no: DagNode) -> Optional[str]:
    """Deriva a 'etapa do DAG' de um nó, para anotar a falha (Req. 15.2).

    Prefere ``node_type`` (a fase estrutural), cai para ``tactic`` (a tática
    ATT&CK) e, por fim, para o índice do nó. ``None`` quando nenhum está presente.
    """
    if no.node_type:
        return no.node_type
    if no.tactic:
        return no.tactic
    if no.node_index is not None:
        return f"no_{no.node_index}"
    return None


def _tecnicas_do_dag(dag: Optional[DagData]) -> list[_TecnicaDag]:
    """Extrai as técnicas **distintas** do DAG, preservando a ordem dos nós.

    Uma técnica é um nó estrutural com ``technique_id`` (a mesma definição de
    :func:`app.services.stage.stage_service._iter_technique_nodes`),
    de-duplicada por ``technique_id`` — o 1º nó de cada técnica é o representante
    (fornece ``node_id`` e a etapa). Preserva a ordem dos nós para uma Saída
    determinística.
    """
    if dag is None:
        return []
    tecnicas: list[_TecnicaDag] = []
    vistas: set[str] = set()
    for no in dag.structural_nodes:
        tid = no.technique_id
        if not tid or tid in vistas:
            continue
        vistas.add(tid)
        tecnicas.append(
            _TecnicaDag(technique_id=tid, node_id=no.node_id, etapa=_etapa_do_no(no))
        )
    return tecnicas


# ---------------------------------------------------------------------------
# Agregado interno dos resultados por técnica
# ---------------------------------------------------------------------------


@dataclass
class _AgregadoTecnica:
    """Acumulador mutável dos resultados por técnica durante a medição."""

    tentada: bool = False
    sucesso: bool = False
    falha: bool = False
    tempo_s: Optional[float] = None
    telemetria: list[str] = field(default_factory=list)

    def acumular(self, resultado: ResultadoAbility) -> None:
        """Acumula um resultado por Ability nesta técnica."""
        if resultado.status is not AbilityResultStatus.PENDING:
            self.tentada = True
        if resultado.status is AbilityResultStatus.SUCCESS:
            self.sucesso = True
        if resultado.status is AbilityResultStatus.FAILURE:
            self.falha = True
        if resultado.tempo_execucao_s is not None:
            self.tempo_s = (self.tempo_s or 0.0) + resultado.tempo_execucao_s
        for sinal in resultado.telemetria:
            if sinal not in self.telemetria:
                self.telemetria.append(sinal)

    @property
    def status(self) -> AbilityResultStatus:
        """O status consolidado da técnica (sucesso > falha > tentada > pendente).

        Uma técnica em ``sucesso`` tem ao menos uma Ability em sucesso; só é
        ``falha`` quando falhou e não teve nenhum sucesso; ``em_execucao`` quando
        foi tentada mas não concluiu; ``pendente`` quando nunca foi tentada.
        """
        if self.sucesso:
            return AbilityResultStatus.SUCCESS
        if self.falha:
            return AbilityResultStatus.FAILURE
        if self.tentada:
            return AbilityResultStatus.RUNNING
        return AbilityResultStatus.PENDING


# ---------------------------------------------------------------------------
# O serviço de medição (puro, sem rede/Docker/IA)
# ---------------------------------------------------------------------------


class MedicaoService:
    """Deriva :class:`MetricasSuficiencia` e consolida em :class:`RelatorioExecucao`.

    A medição é **pura e determinística**: dada a lista de resultados por Ability,
    o DAG curado e o mapa ``ability_id`` → ``technique_id``, produz as métricas de
    suficiência procedural (Req. 15) sem tocar rede, Docker ou IA. Os pontos de
    entrada:

    * :meth:`medir` — a derivação pura, sobre estruturas em memória (resultados +
      DAG + mapa); o alvo dos testes e da composição.
    * :meth:`consolidar` — empacota as métricas (sem recomputar) num
      :class:`RelatorioExecucao` com os resultados reusados.
    * :meth:`medir_do_caso` / :meth:`relatorio_do_caso` — a conveniência de
      produção que extrai o DAG curado e o mapa de técnicas de um
      :class:`~app.services.case_service.CaseData` e delega aos acima.

    Nenhuma dependência de ``Session`` nem construção externa — o serviço é um
    agrupador de funções puras; instanciá-lo é opcional (os métodos são de
    instância só para manter o estilo *service* do pacote).

    _Requisitos: 10.4, 10.5, 10.6, 10.7, 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7_
    """

    def medir(
        self,
        *,
        resultados: Sequence[ResultadoAbility],
        dag: Optional[DagData],
        tecnica_por_ability: Mapping[str, Optional[str]],
    ) -> MetricasSuficiencia:
        """Deriva as :class:`MetricasSuficiencia` dos resultados + o DAG (Req. 15).

        Args:
            resultados: Os resultados por Ability da execução (normalizados em
                :class:`ResultadoAbility`), na ordem de execução.
            dag: O DAG curado do Caso (Saída do Estágio 3); define o universo de
                técnicas (o denominador). ``None``/vazio ⇒ métricas zeradas.
            tecnica_por_ability: O mapa ``ability_id`` → ``technique_id`` das
                Abilities curadas (a ponte resultado → técnica). Uma Ability sem
                técnica mapeada, ou mapeada a uma técnica FORA do DAG, não
                contribui para nenhuma técnica do DAG. ``ability_id`` ausente é
                ignorado.

        Returns:
            As :class:`MetricasSuficiencia` consolidadas — total/sucesso/taxa,
            cobertura procedural ∈ ``[0, 1]``, tempo total, a lista por técnica
            (uma por técnica distinta do DAG, na ordem), os nós falhos (com etapa)
            e o veredito geral.
        """
        tecnicas = _tecnicas_do_dag(dag)
        # Agregador por technique_id, inicializado com todas as técnicas do DAG
        # (técnicas não tentadas permanecem 'pendente'/não cobertas).
        agregados: dict[str, _AgregadoTecnica] = {
            t.technique_id: _AgregadoTecnica() for t in tecnicas
        }
        tecnicas_do_dag: set[str] = set(agregados)

        for resultado in resultados:
            ability_id = resultado.ability_id
            if ability_id is None:
                continue
            tecnica = tecnica_por_ability.get(ability_id)
            # Só contam resultados cujas Abilities mapeiam para uma técnica do DAG.
            if tecnica is None or tecnica not in tecnicas_do_dag:
                continue
            agregados[tecnica].acumular(resultado)

        por_tecnica: list[MetricaTecnica] = []
        nos_falhos: list[str] = []
        tecnicas_sucesso = 0
        tecnicas_tentadas = 0
        tempo_total: Optional[float] = None

        for tecnica in tecnicas:
            agregado = agregados[tecnica.technique_id]
            status = agregado.status
            if agregado.sucesso:
                tecnicas_sucesso += 1
            if agregado.tentada:
                tecnicas_tentadas += 1
            if agregado.tempo_s is not None:
                tempo_total = (tempo_total or 0.0) + agregado.tempo_s

            falhou = status is AbilityResultStatus.FAILURE
            etapa_falha = tecnica.etapa if falhou else None
            if falhou:
                nos_falhos.append(tecnica.node_id or tecnica.technique_id)

            por_tecnica.append(
                MetricaTecnica(
                    technique_id=tecnica.technique_id,
                    node_id=tecnica.node_id,
                    status=status,
                    etapa_falha=etapa_falha,
                    tempo_execucao_s=agregado.tempo_s,
                    telemetria=list(agregado.telemetria),
                )
            )

        total = len(tecnicas)
        taxa_sucesso = (tecnicas_sucesso / total) if total else 0.0
        cobertura = (tecnicas_tentadas / total) if total else 0.0
        # Veredito geral: DAG não vazio, TODAS as técnicas em sucesso e nenhum nó
        # falho (Req. 15.6, 10.5). Um DAG vazio nunca é um sucesso.
        veredito = total > 0 and tecnicas_sucesso == total and not nos_falhos

        return MetricasSuficiencia(
            total_tecnicas_dag=total,
            tecnicas_sucesso=tecnicas_sucesso,
            taxa_sucesso=taxa_sucesso,
            cobertura_procedural=cobertura,
            tempo_total_s=tempo_total,
            por_tecnica=por_tecnica,
            nos_falhos=nos_falhos,
            veredito_sucesso=veredito,
        )

    def consolidar(
        self,
        *,
        caso_id: str,
        metricas: MetricasSuficiencia,
        resultados: Optional[Sequence[Stage3AbilityResult]] = None,
        ambiente_desprovisionado: bool = False,
        gerado_em: Optional[dt.datetime] = None,
    ) -> RelatorioExecucao:
        """Consolida as métricas + os resultados num :class:`RelatorioExecucao`.

        A Saída do Estágio 6 (Req. 10.6, 10.7, 15.7): junta as
        :class:`MetricasSuficiencia` medidas (preservadas na íntegra, **sem**
        recomputar), os
        :class:`~app.services.stage.stage_service.Stage3AbilityResult` reusados da
        execução, o flag de desprovisionamento do ambiente (Req. 12, preenchido
        pela task 8.4) e o instante de geração.

        Args:
            caso_id: O *slug* do Caso cuja execução originou o relatório.
            metricas: As métricas derivadas por :meth:`medir`.
            resultados: Os resultados por Ability reusados do Estágio histórico de
                execução (``Stage3*``); default: vazio.
            ambiente_desprovisionado: ``True`` sse o ambiente já foi
                desprovisionado (preenchido no fim do Estágio 6 — Req. 12).
            gerado_em: O instante de geração; default: agora (UTC).
        """
        return RelatorioExecucao(
            caso_id=caso_id,
            metricas=metricas,
            resultados=list(resultados or []),
            ambiente_desprovisionado=ambiente_desprovisionado,
            gerado_em=gerado_em or dt.datetime.now(dt.timezone.utc),
        )

    def medir_do_caso(
        self,
        *,
        caso: CaseData,
        resultados: Sequence[ResultadoAbility],
    ) -> MetricasSuficiencia:
        """Conveniência de produção: mede a partir de um :class:`CaseData`.

        Extrai o DAG curado (``caso.dag``) e o mapa ``ability_id`` →
        ``technique_id`` das Abilities curadas (``caso.abilities``) e delega a
        :meth:`medir`. Mantém a derivação em um só lugar — a camada de API (task
        8.5) só precisa resolver o Caso e passar os resultados da execução.
        """
        tecnica_por_ability: dict[str, Optional[str]] = {
            a.ability_id: a.technique_id for a in caso.abilities
        }
        return self.medir(
            resultados=resultados,
            dag=caso.dag,
            tecnica_por_ability=tecnica_por_ability,
        )

    def relatorio_do_caso(
        self,
        *,
        caso: CaseData,
        resultados_ability: Sequence[ResultadoAbility],
        resultados_stage3: Sequence[Stage3AbilityResult],
        ambiente_desprovisionado: bool = False,
        gerado_em: Optional[dt.datetime] = None,
    ) -> RelatorioExecucao:
        """Mede e consolida num só passo — a Saída completa do Estágio 6.

        Combina :meth:`medir_do_caso` e :meth:`consolidar`: deriva as métricas do
        Caso + os resultados por Ability e empacota o relatório com os
        ``Stage3AbilityResult`` reusados. É o ponto de composição que a camada de
        API/costura usa para produzir o :class:`RelatorioExecucao` final.
        """
        metricas = self.medir_do_caso(caso=caso, resultados=resultados_ability)
        return self.consolidar(
            caso_id=caso.id,
            metricas=metricas,
            resultados=resultados_stage3,
            ambiente_desprovisionado=ambiente_desprovisionado,
            gerado_em=gerado_em,
        )


__all__ = [
    "ResultadoAbility",
    "MedicaoService",
]
