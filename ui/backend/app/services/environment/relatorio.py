"""DTOs de métricas e relatório — Saída do Estágio 6 (Execução + medição + relatório).

Este módulo define os modelos tipados (Pydantic) da medição de suficiência
procedural e do relatório consolidado produzidos ao final do Estágio 6. A
execução em si **reusa** o ``OperationRunner``/``Stage3*`` (que persiste os
``AbilityResult`` por Ability); estes DTOs são a camada de **medição** sobre
esses resultados mais o DAG, consolidada num :class:`RelatorioExecucao`.

- :class:`MetricaTecnica` — a medição por técnica/nó do DAG: status final,
  etapa do DAG onde falhou, tempo de execução e telemetria observada (Req. 15.2,
  15.4, 15.5).
- :class:`MetricasSuficiencia` — as métricas de suficiência procedural
  (PROPOSTA INICIAL, Req. 15.8): total de técnicas do DAG, técnicas em sucesso,
  taxa de sucesso, cobertura procedural, tempo total, a lista por técnica, os
  nós falhos e o veredito geral (Req. 15.1, 15.2, 15.3, 15.4, 15.6).
- :class:`RelatorioExecucao` — a Saída do Estágio 6: as métricas consolidadas,
  os :class:`~app.services.stage.stage_service.Stage3AbilityResult` reusados do
  Estágio histórico de execução, o flag de desprovisionamento do ambiente (Req.
  12) e o instante de geração (Req. 10.6, 10.7, 15.7).

O status das técnicas reusa :class:`~app.models.enums.AbilityResultStatus`
(``pendente``/``em_execucao``/``sucesso``/``falha``), a mesma fonte de verdade do
``Stage3AbilityResult``.

_Requisitos: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.8_
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel, Field

from app.models.enums import AbilityResultStatus
from app.services.stage.stage_service import Stage3AbilityResult


class MetricaTecnica(BaseModel):
    """Medição por técnica/nó do DAG (Req. 15.2, 15.4, 15.5).

    Attributes:
        technique_id: ``technique_id`` da técnica medida, quando disponível.
        node_id: Identificador do nó do DAG correspondente, quando disponível.
        status: Status final da técnica — um de
            :class:`~app.models.enums.AbilityResultStatus`
            (``pendente``/``em_execucao``/``sucesso``/``falha``).
        etapa_falha: Etapa do DAG onde a técnica falhou, quando aplicável
            (Req. 15.2); ``None`` quando não falhou.
        tempo_execucao_s: Tempo de execução da técnica em segundos (Req. 15.4);
            ``None`` quando não medido.
        telemetria: Sinais de telemetria observados (processo/rede/auth/arquivo)
            durante a execução da técnica (Req. 15.5).
    """

    technique_id: Optional[str] = None
    node_id: Optional[str] = None
    status: AbilityResultStatus = AbilityResultStatus.PENDING
    etapa_falha: Optional[str] = None
    tempo_execucao_s: Optional[float] = None
    telemetria: list[str] = Field(default_factory=list)


class MetricasSuficiencia(BaseModel):
    """Métricas de suficiência procedural — PROPOSTA INICIAL (Req. 15.8).

    Attributes:
        total_tecnicas_dag: Total de técnicas do DAG do Caso.
        tecnicas_sucesso: Número de técnicas que terminaram em ``sucesso``.
        taxa_sucesso: ``tecnicas_sucesso / total_tecnicas_dag`` (Req. 15.1).
        cobertura_procedural: Proporção do DAG efetivamente executável, em
            ``[0, 1]`` (Req. 15.3).
        tempo_total_s: Tempo total da execução em segundos (Req. 15.4); ``None``
            quando não medido.
        por_tecnica: A :class:`MetricaTecnica` de cada técnica/nó do DAG.
        nos_falhos: Nós/técnicas que falharam (Req. 15.2).
        veredito_sucesso: Veredito geral da execução (Req. 15.6, 10.5).
    """

    total_tecnicas_dag: int = 0
    tecnicas_sucesso: int = 0
    taxa_sucesso: float = 0.0
    cobertura_procedural: float = 0.0
    tempo_total_s: Optional[float] = None
    por_tecnica: list[MetricaTecnica] = Field(default_factory=list)
    nos_falhos: list[str] = Field(default_factory=list)
    veredito_sucesso: bool = False


class RelatorioExecucao(BaseModel):
    """Saída do Estágio 6 — relatório consolidado (Req. 10.6, 10.7, 15.7).

    Attributes:
        caso_id: *Slug* do Caso cuja execução originou o relatório.
        metricas: As :class:`MetricasSuficiencia` da execução.
        resultados: Os
            :class:`~app.services.stage.stage_service.Stage3AbilityResult`
            por Ability, reusados do Estágio histórico de execução (``Stage3*``).
        ambiente_desprovisionado: ``True`` sse o ambiente da campanha foi
            desprovisionado ao final (Req. 12).
        gerado_em: Instante em que o relatório foi gerado.
    """

    caso_id: str
    metricas: MetricasSuficiencia
    resultados: list[Stage3AbilityResult] = Field(default_factory=list)
    ambiente_desprovisionado: bool = False
    gerado_em: dt.datetime


__all__ = [
    "MetricaTecnica",
    "MetricasSuficiencia",
    "RelatorioExecucao",
]
