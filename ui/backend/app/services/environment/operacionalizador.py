"""Costura de operacionalização: ``OperacionalizadorPorOrigem`` + curadoria.

Este módulo define o **ponto de extensão por origem** do Estágio 4
(Operacionalização) no pipeline de seis Estágios. Separa *como* um Caso é
operacionalizado (hoje por curadoria, lendo os artefatos curados de disco;
amanhã por um agente de IA) do *resto* do sistema, de modo que uma implementação
``gerado_ia`` possa ser encaixada depois **sem** alterar esta assinatura (Req.
8.6, 16.2, 16.4), seguindo o precedente de ``GeradorDeDag`` (Estágio 3) e
``ProvisionadorDeAmbiente`` (Estágio 5).

Saída dupla (dois artefatos tipados **separados** — Req. 8.4, 8.7):

``OperacionalizacaoProduzida``
    O resultado tipado (Pydantic) do Estágio 4 — carrega **as Abilities
    ordenadas** (reuso de :meth:`StageService.assemble_stage2` → ``Stage2Output``,
    com ``atomic_ordering`` e ``origem_traducao``) e **os
    ``ParametrosDeAmbiente``** derivados dos nós do DAG curado. Os dois artefatos
    ficam em campos distintos, não misturados.

``OperacionalizadorPorOrigem``
    Um :class:`typing.Protocol` ``@runtime_checkable`` com duas operações —
    ``origem() -> TranslationSource`` e
    ``operacionalizar(caso, dag) -> OperacionalizacaoProduzida``. Qualquer objeto
    que exponha as duas assinaturas satisfaz o protocolo (duck typing
    estrutural), sem herança explícita.

``CuradoriaOperacionalizador``
    A implementação de curadoria desta iteração. Reusa
    :meth:`StageService.assemble_stage2`/``CaseService`` para montar as Abilities
    ordenadas (``atomic_ordering``, ``origem_traducao=curadoria_humana``) e
    **deriva** os :class:`~app.services.environment.operacionalizacao.ParametrosDeAmbiente`
    dos nós do DAG curado: ``attacker_commands`` → ``parse_command`` → destinos
    internos → ``container_for_ip`` resolve ``endereco``/``container``;
    ``target_vuln_host_a``/``_b`` → ``vulnerabilidades``; ``source_host_type``/
    ``target_host_type`` → ``host_type``; ``network_context`` → notas; serviços
    inferidos dos comandos. A plataforma é sempre ``CONTAINER`` nesta spec (Req.
    17.1). **Sem rede e sem IA** em nenhum caminho (Req. 11.3, 11.4).

_Requisitos: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 11.3, 11.4, 16.2_
"""

from __future__ import annotations

import re
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.models.enums import TranslationSource
from app.services.case_service import CaseData, CaseService, DagNode
from app.services.containment import container_for_ip, is_internal_ip, parse_command
from app.services.dag.dag_service import DagView
from app.services.environment.operacionalizacao import (
    HostAlvo,
    ParametrosDeAmbiente,
    PlataformaAmbiente,
    ServicoRequerido,
)
from app.services.stage.stage_service import (
    Stage2Output,
    StageService,
    _ability_view,
    _adversary_view,
)


# ---------------------------------------------------------------------------
# DTO tipado da Saída do Estágio 4 (task 5.2)
# ---------------------------------------------------------------------------


class OperacionalizacaoProduzida(BaseModel):
    """Saída do Estágio 4 — os **dois** artefatos tipados separados (Req. 8.4, 8.7).

    O Estágio 4 (Operacionalização) tem Saída **dupla**: as Abilities ordenadas
    pelo DAG e os parâmetros de ambiente derivados dos nós curados. Estes ficam
    em campos **distintos** deste resultado, nunca misturados (Req. 8.7):

    Attributes:
        caso_id: *Slug* do Caso operacionalizado.
        abilities: 1º artefato — o :class:`~app.services.stage.stage_service.Stage2Output`
            com as Abilities (comando por executor), a ordem
            (``atomic_ordering``), o Adversary e ``origem_traducao``
            (``curadoria_humana``) — reuso de
            :meth:`~app.services.stage.stage_service.StageService.assemble_stage2`
            (Req. 8.2, 8.5, 8.6). ``None`` quando a operacionalização está
            suprimida (sem técnica a montante — Req. 8.1).
        parametros: 2º artefato — os
            :class:`~app.services.environment.operacionalizacao.ParametrosDeAmbiente`
            derivados dos nós do DAG curado (hosts/serviços/``network_context``),
            destinados ao Estágio 5 (Req. 8.3, 8.4).
        origem: Origem da operacionalização — ``curadoria_humana`` hoje;
            extensível a ``gerado_ia`` sem alterar o modelo (Req. 8.6, 16.2,
            16.4).
    """

    caso_id: str
    abilities: Optional[Stage2Output] = None
    parametros: ParametrosDeAmbiente
    origem: TranslationSource = TranslationSource.HUMAN_CURATION


# ---------------------------------------------------------------------------
# Costura por origem (Protocol) — Estágio 4 (task 5.2)
# ---------------------------------------------------------------------------


@runtime_checkable
class OperacionalizadorPorOrigem(Protocol):
    """Costura do Estágio 4 (Operacionalização) por **origem** — Req. 8.6, 16.2.

    Um ``Protocol`` ``@runtime_checkable`` que define o **ponto de extensão** da
    operacionalização, seguindo o precedente das demais costuras por origem
    (``GeradorDeDag`` no Estágio 3, ``ProvisionadorDeAmbiente`` no Estágio 5):
    cada origem (``curadoria_humana`` hoje, ``gerado_ia`` no futuro) é uma
    implementação intercambiável por injeção, **sem** redesenho (Req. 16.2,
    16.4).

    Qualquer objeto que exponha estas operações satisfaz o protocolo — nenhuma
    herança explícita é exigida (duck typing estrutural):

    - ``origem()`` — a :class:`~app.models.enums.TranslationSource` que esta
      implementação carimba na Saída (``OperacionalizacaoProduzida.origem`` e
      ``ParametrosDeAmbiente.origem``).
    - ``operacionalizar(caso, dag)`` — produz os **dois** artefatos tipados do
      Estágio 4 (Abilities ordenadas + ``ParametrosDeAmbiente``) a partir do
      *slug* do Caso e do DAG (Saída do Estágio 3).

    Hoje existe a implementação de curadoria
    (:class:`CuradoriaOperacionalizador`); uma implementação de IA
    (``AiOperacionalizador``) encaixa depois sob a mesma assinatura (Req. 16.2).
    """

    def origem(self) -> TranslationSource:
        """A :class:`~app.models.enums.TranslationSource` desta implementação."""
        ...

    def operacionalizar(self, caso: str, dag: "DagView") -> "OperacionalizacaoProduzida":
        """Produz os dois artefatos do Estágio 4 (Abilities + ``ParametrosDeAmbiente``).

        ``dag`` é a Saída do Estágio 3 (o DAG escolhido); a implementação de
        curadoria usa a sua presença como sinal do caminho curado e deriva os
        parâmetros dos nós curados lidos de disco (sem rede, sem IA — Req. 11.3,
        11.4).
        """
        ...


# ---------------------------------------------------------------------------
# Heurística pura de inferência de serviços a partir dos comandos (Req. 8.3)
# ---------------------------------------------------------------------------

# Padrões (regex) → descrição legível do serviço/vulnerabilidade a provisionar.
# Puros e determinísticos; aplicados sobre os attacker_commands e os
# target_vuln_host_* dos nós curados. A ordem é estável para uma Saída
# determinística.
_SERVICO_PADROES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bflask\b", re.IGNORECASE), "servidor Flask (HTTP)"),
    (re.compile(r"\bport=\"?(\d+)\"?|:(\d{2,5})\b"), "serviço HTTP em porta exposta"),
    (re.compile(r"\bsshpass\b|\bssh\b", re.IGNORECASE), "acesso SSH"),
    (re.compile(r"\bsudo\b|\bNOPASSWD\b", re.IGNORECASE), "sudo sem senha"),
    (re.compile(r"\bnc\b|\bnetcat\b|/dev/tcp/", re.IGNORECASE), "listener/reverse shell (netcat)"),
    (re.compile(r"\bchmod \+s\b|setuid\(", re.IGNORECASE), "binário SUID (escalonamento)"),
    (re.compile(r"\bdnsmasq\b|\bdig\b|dnsutils", re.IGNORECASE), "serviço DNS"),
    (re.compile(r"docker\.sock", re.IGNORECASE), "socket Docker exposto"),
    (re.compile(r"\bxmrig\b", re.IGNORECASE), "minerador (xmrig)"),
)


def _inferir_servicos(comandos: list[str], tecnica: Optional[str]) -> list[ServicoRequerido]:
    """Infere serviços/vulnerabilidades a provisionar a partir de ``comandos``.

    Pura e determinística: casa cada padrão de :data:`_SERVICO_PADROES` contra o
    texto concatenado dos comandos (``attacker_commands`` + ``target_vuln_host_*``
    de um nó) e devolve um :class:`ServicoRequerido` por padrão único casado,
    carimbando ``tecnica_origem`` com o ``technique_id`` do nó quando disponível
    (Req. 8.3). Não acessa rede nem IA.
    """
    if not comandos:
        return []
    texto = "\n".join(comandos)
    servicos: list[ServicoRequerido] = []
    vistos: set[str] = set()
    for padrao, descricao in _SERVICO_PADROES:
        if descricao in vistos:
            continue
        if padrao.search(texto):
            servicos.append(ServicoRequerido(descricao=descricao, tecnica_origem=tecnica))
            vistos.add(descricao)
    return servicos


class CuradoriaOperacionalizador:
    """Implementação de curadoria da costura :class:`OperacionalizadorPorOrigem`.

    Produz os **dois** artefatos do Estágio 4 a partir do material **já curado
    por humano**, sem rede e sem IA em nenhum caminho (Req. 11.3, 11.4):

    1. **Abilities ordenadas** — reusa
       :meth:`~app.services.stage.stage_service.StageService.assemble_stage2`
       (``Stage2Output`` com ``atomic_ordering`` e
       ``origem_traducao=curadoria_humana``), a fonte única da operacionalização
       curada (Req. 8.2, 8.5, 8.6).
    2. **``ParametrosDeAmbiente``** — **deriva** dos nós do DAG curado lidos via
       :class:`~app.services.case_service.CaseService` (``try_load_case``,
       defensivo e sem rede): para cada nó,
       ``attacker_commands`` → :func:`~app.services.containment.parse_command` →
       destinos **internos** → :func:`~app.services.containment.container_for_ip`
       resolve ``endereco``/``container``; ``target_vuln_host_a``/``_b`` →
       ``vulnerabilidades`` do host correspondente; ``source_host_type``/
       ``target_host_type`` → ``host_type``; ``network_context`` → nota de rede;
       serviços inferidos dos comandos (:func:`_inferir_servicos`). A plataforma
       é sempre ``CONTAINER`` (Req. 17.1).

    Satisfaz o :class:`OperacionalizadorPorOrigem` estruturalmente — expõe
    ``origem()`` + ``operacionalizar()`` — sem herança explícita, exatamente o
    ponto da costura; carimba ``curadoria_humana`` na Saída (Req. 8.6, 16.2).

    _Requisitos: 8.1, 8.2, 8.3, 8.4, 8.5, 8.7, 8.8, 11.3, 11.4, 16.2_
    """

    def __init__(
        self,
        *,
        stage_service: Optional[StageService] = None,
        case_service: Optional[CaseService] = None,
    ) -> None:
        """Monta o operacionalizador de curadoria com dependências injetáveis.

        Args:
            stage_service: :class:`StageService` *session-bound* opcional usado
                para montar as Abilities ordenadas via
                :meth:`~app.services.stage.stage_service.StageService.assemble_stage2`
                (que persiste o ``StageRun`` do Estágio 1 a montante). A camada de
                API, que já tem a ``Session``, pode injetá-lo para reusar a
                montagem canônica. Quando omitido, as Abilities ordenadas são
                montadas de forma **pura** direto do ``CaseService`` (mesma fonte
                de dados que o ``assemble_stage2`` lê — ``load_case`` → abilities/
                adversary), **sem** persistência — útil para a derivação testável
                sem banco e para chamadas somente de parâmetros.
            case_service: :class:`CaseService` para ler o Caso/DAG curado de
                disco. Default: uma instância de produção (reaproveitada do
                ``stage_service`` quando este for passado, para compartilhar o
                mesmo catálogo).
        """
        self._case_service: CaseService = (
            case_service
            if case_service is not None
            else (
                getattr(stage_service, "_case_service", None) or CaseService()
                if stage_service is not None
                else CaseService()
            )
        )
        self._stage_service: Optional[StageService] = stage_service

    @property
    def case_service(self) -> CaseService:
        """O :class:`CaseService` usado para ler o Caso/DAG curado de disco."""
        return self._case_service

    @property
    def stage_service(self) -> Optional[StageService]:
        """O :class:`StageService` injetado (ou ``None`` no modo puro)."""
        return self._stage_service

    def origem(self) -> TranslationSource:
        """Carimba ``curadoria_humana`` na Saída do Estágio 4 (Req. 8.6, 16.2)."""
        return TranslationSource.HUMAN_CURATION

    # -- Derivação dos ParametrosDeAmbiente (pura, sem rede/IA) --------------

    @staticmethod
    def _resolver_hosts(no: DagNode) -> list[HostAlvo]:
        """Deriva os :class:`HostAlvo` de um nó do DAG curado (Req. 8.3).

        Resolve o(s) host(s)-alvo a partir dos destinos **internos** dos
        ``attacker_commands`` do nó (via :func:`parse_command` →
        :func:`container_for_ip`), associando ``endereco``/``container`` e o
        ``target_host_type`` do nó, e empacotando os comandos
        ``target_vuln_host_a`` como ``vulnerabilidades`` do primeiro host-alvo
        resolvido (e ``target_vuln_host_b`` como um segundo host, quando houver).
        Pura e determinística; sem rede.
        """
        hosts: list[HostAlvo] = []

        # Destinos internos resolvidos dos comandos → host(s)-alvo com endereço
        # e container. Preserva a ordem e remove enderecos duplicados.
        enderecos: list[str] = []
        for comando in no.attacker_commands:
            for dest in parse_command(comando).destinations:
                if is_internal_ip(dest.value) and dest.value not in enderecos:
                    enderecos.append(dest.value)

        vuln_a = list(no.target_vuln_host_a)
        vuln_b = list(no.target_vuln_host_b)

        if enderecos:
            primeiro = enderecos[0]
            hosts.append(
                HostAlvo(
                    rotulo="host_a",
                    endereco=primeiro,
                    container=container_for_ip(primeiro),
                    host_type=no.target_host_type,
                    vulnerabilidades=vuln_a,
                )
            )
            # Destinos internos adicionais viram hosts extra (ex.: ssh aninhado).
            for idx, endereco in enumerate(enderecos[1:], start=1):
                rotulo = "host_b" if idx == 1 else f"host_{chr(ord('a') + idx)}"
                hosts.append(
                    HostAlvo(
                        rotulo=rotulo,
                        endereco=endereco,
                        container=container_for_ip(endereco),
                        host_type=no.target_host_type,
                        vulnerabilidades=vuln_b if idx == 1 else [],
                    )
                )
        elif vuln_a or vuln_b or no.target_host_type is not None:
            # Nenhum destino interno resolvido, mas há sinais de host-alvo:
            # ainda registramos um host_a (endereço/container não resolvidos).
            hosts.append(
                HostAlvo(
                    rotulo="host_a",
                    host_type=no.target_host_type,
                    vulnerabilidades=vuln_a,
                )
            )

        # target_vuln_host_b presente sem destino adicional: segundo host sem
        # endereço resolvido, carregando apenas as vulnerabilidades.
        if vuln_b and all(h.rotulo != "host_b" for h in hosts):
            hosts.append(HostAlvo(rotulo="host_b", vulnerabilidades=vuln_b))

        return hosts

    def _derivar_parametros(self, caso: str) -> ParametrosDeAmbiente:
        """Deriva os :class:`ParametrosDeAmbiente` dos nós do DAG curado (Req. 8.3).

        Lê o Caso curado via :meth:`CaseService.try_load_case` (defensivo, sem
        rede) e percorre os ``structural_nodes`` acumulando hosts (com
        ``endereco``/``container`` resolvidos, ``host_type`` e vulnerabilidades),
        serviços inferidos dos comandos e as notas de ``network_context``. A
        plataforma é sempre ``CONTAINER`` (Req. 17.1) e a origem
        ``curadoria_humana`` (Req. 16.2). Um Caso sem DAG curado resulta em
        parâmetros vazios (sem levantar).
        """
        hosts: list[HostAlvo] = []
        servicos: list[ServicoRequerido] = []
        network_context: list[str] = []
        servicos_vistos: set[tuple[str, Optional[str]]] = set()
        hosts_por_container: dict[Optional[str], HostAlvo] = {}

        resultado = self._case_service.try_load_case(caso)
        dag = (
            resultado.case.dag
            if resultado.ok and resultado.case is not None
            else None
        )
        nos: list[DagNode] = list(dag.structural_nodes) if dag is not None else []

        for no in nos:
            # Notas de rede por nó (Req. 8.3), de-duplicadas preservando a ordem.
            if no.network_context and no.network_context not in network_context:
                network_context.append(no.network_context)

            # Hosts-alvo: unimos por (container, endereco) para não duplicar o
            # mesmo contêiner alvo entre nós, acumulando as vulnerabilidades.
            for host in self._resolver_hosts(no):
                chave = host.container if host.container is not None else host.endereco
                existente = hosts_por_container.get(chave) if chave is not None else None
                if existente is None:
                    hosts.append(host)
                    if chave is not None:
                        hosts_por_container[chave] = host
                else:
                    for vuln in host.vulnerabilidades:
                        if vuln not in existente.vulnerabilidades:
                            existente.vulnerabilidades.append(vuln)
                    if existente.host_type is None and host.host_type is not None:
                        existente.host_type = host.host_type

            # Serviços inferidos dos comandos (attacker + target_vuln_host_*).
            comandos = (
                list(no.attacker_commands)
                + list(no.target_vuln_host_a)
                + list(no.target_vuln_host_b)
            )
            for servico in _inferir_servicos(comandos, no.technique_id):
                chave_s = (servico.descricao, servico.tecnica_origem)
                if chave_s not in servicos_vistos:
                    servicos.append(servico)
                    servicos_vistos.add(chave_s)

        return ParametrosDeAmbiente(
            caso_id=caso,
            plataforma=PlataformaAmbiente.CONTAINER,
            hosts=hosts,
            servicos=servicos,
            network_context=network_context,
            origem=self.origem(),
        )

    # -- Montagem das Abilities ordenadas (1º artefato) ---------------------

    @staticmethod
    def _montar_abilities_puro(case: Optional[CaseData]) -> Optional[Stage2Output]:
        """Monta o :class:`Stage2Output` **puramente** a partir do Caso curado.

        Espelha a Saída de :meth:`StageService.assemble_stage2` — Abilities
        (comando por executor), ordem (``atomic_ordering`` do Adversary, com
        *fallback* na ordem natural), Adversary e ``origem_traducao`` — mas
        **sem** executar/persistir a Modelagem (Estágio 1), de modo a não exigir
        uma ``Session`` (Req. 8.2, 8.5, 8.6). Reusa os mesmos mapeadores puros
        ``_ability_view``/``_adversary_view`` do ``stage_service``.

        Devolve ``None`` quando não há Caso/Abilities (operacionalização
        suprimida — Req. 8.1), espelhando o ``suppressed`` do ``assemble_stage2``.
        """
        if case is None:
            return None
        abilities = list(case.abilities)
        if not abilities:
            return None

        adversary = case.adversary
        ability_views = [_ability_view(a) for a in abilities]
        if adversary is not None:
            ordering = list(adversary.atomic_ordering)
            adversary_view = _adversary_view(adversary)
            adversary_note = None
        else:
            ordering = [a.ability_id for a in abilities]
            adversary_view = None
            adversary_note = (
                "Este caso não possui Adversary associado; não há agrupamento "
                "de Abilities em Adversary."
            )

        return Stage2Output(
            abilities=ability_views,
            ordering=ordering,
            adversary=adversary_view,
            origem_traducao=TranslationSource.HUMAN_CURATION,
            human_curation=True,
            has_abilities=True,
            has_adversary=adversary is not None,
            adversary_note=adversary_note,
        )

    def _montar_abilities(self, caso: str) -> Optional[Stage2Output]:
        """Monta as Abilities ordenadas, reusando ``assemble_stage2`` se possível.

        Com um :class:`StageService` *session-bound* injetado, delega a
        :meth:`StageService.assemble_stage2` (a montagem canônica, Req. 8.2).
        Sem ele (modo puro), monta direto do ``CaseService`` via
        :meth:`_montar_abilities_puro` — mesma fonte de dados, sem persistência.
        """
        if self._stage_service is not None:
            return self._stage_service.assemble_stage2(caso).saida

        resultado = self._case_service.try_load_case(caso)
        case = resultado.case if resultado.ok else None
        return self._montar_abilities_puro(case)

    def operacionalizar(self, caso: str, dag: DagView) -> OperacionalizacaoProduzida:
        """Produz os dois artefatos do Estágio 4 para o *slug* ``caso`` (Req. 8.4, 8.7).

        1. Monta as **Abilities ordenadas** reusando
           :meth:`StageService.assemble_stage2` — ``atomic_ordering`` e
           ``origem_traducao=curadoria_humana`` (Req. 8.2, 8.5, 8.6). Quando a
           operacionalização está suprimida (sem técnica a montante), ``abilities``
           é ``None`` (Req. 8.1).
        2. **Deriva** os :class:`ParametrosDeAmbiente` dos nós do DAG curado
           (Req. 8.3), com a plataforma sempre ``CONTAINER`` (Req. 17.1).

        Os dois artefatos saem em campos **separados** de
        :class:`OperacionalizacaoProduzida` (Req. 8.7). ``dag`` sinaliza o
        caminho curado; a derivação lê os nós curados de disco (sem rede, sem IA
        — Req. 11.3, 11.4). O Estágio 4 conclui quando **ambos** os artefatos são
        produzidos (Req. 8.8).
        """
        abilities = self._montar_abilities(caso)
        parametros = self._derivar_parametros(caso)

        return OperacionalizacaoProduzida(
            caso_id=caso,
            abilities=abilities,
            parametros=parametros,
            origem=self.origem(),
        )


__all__ = [
    "OperacionalizacaoProduzida",
    "OperacionalizadorPorOrigem",
    "CuradoriaOperacionalizador",
]
