"""Costura de provisionamento do ambiente: ``ProvisionadorDeAmbiente`` + curadoria.

Este módulo define o **ponto de extensão** do Estágio 4 (Provisionamento do
Ambiente). Separa *como* o ambiente de um Caso é preparado/verificado (hoje por
curadoria sobre o laboratório estático do ``docker-compose``, amanhã por um
agente de IA que **cria** contêineres/VMs/redes dinamicamente) do *resto* do
sistema (auto-conclusão do Estágio 4, exibição da topologia), de modo que uma
implementação de IA possa ser encaixada depois **sem** alterar esta assinatura
(Req. 5.1, 10.2).

``ProvisionadorDeAmbiente``
    Um :class:`typing.Protocol` ``@runtime_checkable`` com uma única operação —
    ``preparar(caso) -> ResultadoProvisionamento`` — que prepara/verifica o
    ambiente de um Caso e devolve um resultado tipado carregando: se o ambiente
    está **pronto/isolado**, os veredictos de isolamento por contêiner e a
    **topologia** (hosts/redes/mermaid) para exibição. A implementação de
    curadoria apenas **verifica e expõe** (read-only); a futura
    ``AiProvisionadorDeAmbiente`` **criará** o ambiente sob a mesma assinatura.

``ResultadoProvisionamento``
    O resultado tipado de uma preparação/verificação: ``pronto``/``isolado``, o
    :class:`~app.services.containment.IsolationResult` com os veredictos por
    contêiner e a :class:`~app.services.environment.topologia_builder.TopologyResponse`
    com a topologia do ambiente contido.

``CuradoriaProvisionadorDeAmbiente``
    A implementação de curadoria desta iteração. **NÃO** inicia, cria ou modifica
    nada (read-only); **sem rede e sem IA**. Monta a topologia a partir do mapa
    estático do laboratório (reusando
    :mod:`app.services.environment.topologia_builder`, sem duplicar o mapa) e
    verifica o isolamento via :func:`app.services.containment.inspect_isolation`
    com um :class:`~app.services.containment.ContainerInspector` **injetável** (um
    inspector falso nos testes, o adaptador real do Docker em produção). Verifica
    exatamente os contêineres que a Execução (Estágio 5) irá atacar.

Como no restante dos pacotes de serviço (``dag``/``stage``), a costura é um
``@runtime_checkable Protocol`` e as dependências externas (inspector, Caso) são
**injetadas**, com os defaults de produção resolvidos preguiçosamente.

_Requisitos: 5.1, 5.4, 5.5, 5.7, 10.2_
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from app.services.case_service import CaseService
from app.services.containment import (
    ContainerInspector,
    IsolationResult,
    inspect_isolation,
)
from app.services.environment.topologia_builder import (
    TopologyResponse,
    compose_path,
    construir_hosts,
    construir_redes,
    container_names,
    montar_mermaid,
)


@dataclass(frozen=True)
class ResultadoProvisionamento:
    """Resultado tipado de preparar/verificar o ambiente de um Caso (Estágio 4).

    Carrega tudo que a auto-conclusão do Estágio 4 (task 4.2) e a tela de
    Provisionamento (``EnvironmentStageView``) precisam para decidir e exibir:

    Attributes:
        caso_id: *Slug* do Caso cujo ambiente foi preparado/verificado.
        pronto: ``True`` sse o ambiente está **pronto para a Execução** — isto é,
            verificado como isolado. Para a curadoria, ``pronto == isolado`` (ela
            não cria nada; só pode declarar pronto um ambiente já isolado). Para
            uma futura IA que cria o ambiente, ``pronto`` pode englobar também o
            provisionamento efetivo, mantendo a mesma assinatura.
        isolado: ``True`` sse **todos** os contêineres-alvo estão isolados
            (``isolamento.passed``). É o sinal de contenção do Req. 5 — se
            ``False``, o Estágio 4 não conclui e o Estágio 5 fica bloqueado.
        isolamento: O :class:`~app.services.containment.IsolationResult` com os
            veredictos por contêiner (razões de falha para exibição/erro).
        topologia: A :class:`~app.services.environment.topologia_builder.TopologyResponse`
            do ambiente contido (hosts, redes internas, Mermaid), para a tela do
            Estágio 4 — exibida mesmo quando ``isolado`` é ``False`` (a razão
            acompanha os veredictos).
    """

    caso_id: str
    pronto: bool
    isolado: bool
    isolamento: IsolationResult
    topologia: TopologyResponse


@runtime_checkable
class ProvisionadorDeAmbiente(Protocol):
    """Costura de extensão do Provisionamento do Ambiente (Estágio 4) — Req. 5.1, 10.2.

    Um ``Protocol`` ``@runtime_checkable`` com uma única responsabilidade:
    preparar/verificar o ambiente de um Caso e devolver um
    :class:`ResultadoProvisionamento`. Qualquer objeto que exponha
    ``preparar(caso)`` com esta assinatura satisfaz o protocolo — nenhuma herança
    explícita é exigida (duck typing estrutural).

    Hoje existe uma implementação de curadoria
    (:class:`CuradoriaProvisionadorDeAmbiente`) que apenas **verifica** o
    isolamento do laboratório estático e **expõe** a topologia (read-only, sem
    rede/IA). Uma implementação de IA (``AiProvisionadorDeAmbiente``) que de fato
    **cria** contêineres/VMs/redes pode ser adicionada depois **sem** alterar esta
    assinatura: ela também devolve um ``ResultadoProvisionamento`` (com a
    topologia que provisionou e os veredictos de isolamento do ambiente criado),
    de modo que trocar curadoria↔IA é uma questão de injeção, não de redesenho
    (Req. 10.2).
    """

    def preparar(self, caso: str) -> ResultadoProvisionamento:
        """Prepara/verifica o ambiente do *slug* ``caso`` e devolve o resultado.

        A implementação de curadoria **não cria nada** — apenas verifica o
        isolamento do ambiente existente e expõe a topologia. A futura
        implementação de IA pode criar o ambiente antes de verificá-lo, sob a
        mesma assinatura.
        """
        ...


class CuradoriaProvisionadorDeAmbiente:
    """Implementação de curadoria da costura :class:`ProvisionadorDeAmbiente` (Req. 5.4, 5.5, 5.7).

    Esta iteração **não provisiona dinamicamente**: o ambiente é o laboratório
    estático do ``docker/docker-compose.yml``. A curadoria apenas:

    1. **Expõe a topologia** do ambiente contido — hosts, redes ``internal: true``
       e diagrama Mermaid — montada a partir do mapa estático reaproveitado de
       :mod:`app.services.environment.topologia_builder` (a mesma fonte de verdade
       de IPs/subnets do pré-voo de contenção, **sem duplicar** o mapa).
    2. **Verifica o isolamento** dos contêineres-alvo via
       :func:`app.services.containment.inspect_isolation`, com um
       :class:`~app.services.containment.ContainerInspector` **injetável** (um
       inspector falso nos testes — sem daemon Docker; o adaptador real em
       produção quando ``None``). Verifica exatamente os contêineres que a
       Execução (Estágio 5) irá atacar (:func:`...topologia_builder.container_names`).

    **Garantia read-only / sem rede / sem IA:** nenhum caminho desta implementação
    inicia, cria ou modifica contêineres/redes, acessa a rede ou invoca IA — toda
    a inspeção Docker é read-only e fica atrás do inspector injetado (Req. 5.5).

    Satisfaz o :class:`ProvisionadorDeAmbiente` estruturalmente (expõe
    ``preparar``), sem herança explícita — exatamente o ponto da costura.

    _Requisitos: 5.4, 5.5, 5.7_
    """

    def __init__(
        self,
        *,
        inspector: Optional[ContainerInspector] = None,
        case_service: Optional[CaseService] = None,
    ) -> None:
        """Monta o provisionador de curadoria com dependências injetáveis.

        Args:
            inspector: Inspector Docker **read-only** usado na verificação de
                isolamento. ``None`` em produção (o adaptador real é construído
                preguiçosamente por :func:`inspect_isolation` apenas quando a
                verificação roda); os testes injetam um inspector falso para não
                tocar em nenhum daemon.
            case_service: :class:`CaseService` para resolver o nome de exibição do
                Caso e validar o *slug*. Default: uma instância de produção.
        """
        self._inspector = inspector
        self._case_service: CaseService = (
            case_service if case_service is not None else CaseService()
        )

    @property
    def case_service(self) -> CaseService:
        """O :class:`CaseService` usado para resolver nome/validação do Caso."""
        return self._case_service

    def _nome_do_caso(self, caso: str) -> str:
        """Nome de exibição do Caso para a topologia (fallback: o próprio slug)."""
        return self._case_service.list_cases().get(caso, caso)

    def preparar(self, caso: str) -> ResultadoProvisionamento:
        """Verifica o isolamento do ambiente contido e expõe sua topologia (read-only).

        Monta a topologia estática (hosts/redes/Mermaid) e verifica o isolamento
        dos contêineres-alvo (os mesmos que a Execução atacará) via
        :func:`inspect_isolation` com o inspector injetado. **Não cria/inicia
        nada.** O ambiente é declarado ``pronto`` sse ``isolado`` — a curadoria
        não provisiona, então só pode aprovar um ambiente já isolado (Req. 5.4,
        5.6); se não estiver isolado, ``isolado=False`` e a topologia ainda é
        devolvida com os veredictos (a tela exibe a razão e o Estágio 5 fica
        bloqueado).

        Não acessa a rede e não invoca IA em nenhum caminho (Req. 5.5, 5.7).
        """
        hosts = construir_hosts()
        redes = construir_redes()

        containers = container_names()
        isolamento = inspect_isolation(containers, self._inspector)

        topologia = TopologyResponse(
            caso_id=caso,
            nome=self._nome_do_caso(caso),
            isolado=isolamento.passed,
            hosts=hosts,
            redes=redes,
            mermaid=montar_mermaid(hosts, redes),
            compose_file=compose_path(),
        )

        return ResultadoProvisionamento(
            caso_id=caso,
            pronto=isolamento.passed,
            isolado=isolamento.passed,
            isolamento=isolamento,
            topologia=topologia,
        )


__all__ = [
    "ResultadoProvisionamento",
    "ProvisionadorDeAmbiente",
    "CuradoriaProvisionadorDeAmbiente",
]
