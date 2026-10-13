"""Costura de geração de DAG: o Protocol ``GeradorDeDag`` e o DTO ``DagProduzido``.

Este módulo define o **ponto de extensão** pelo qual um DAG é produzido para uma
Campanha. A ideia é separar *como* um DAG é gerado (hoje por curadoria humana,
amanhã por um agente de IA) do *resto* do sistema (persistência, listagem,
montagem de Mermaid, escolha no Estágio 1), de modo que uma implementação de IA
possa ser encaixada depois **sem** alterar esta assinatura nem a entidade ``Dag``
(Req. 5.1, 5.3, 9.3).

``GeradorDeDag``
    Um :class:`typing.Protocol` ``@runtime_checkable`` com duas operações:

    * ``origem() -> OrigemDag`` — a origem (``curadoria_humana`` | ``gerado_ia``)
      que a implementação carimba nos DAGs que produz.
    * ``gerar(caso: str, modelagem: ModelagemEstrutural) -> list[DagProduzido]``
      — produz zero ou mais DAGs para o *slug* da Campanha a partir da Modelagem
      Estrutural (Estágio 1); no esquema de cinco estágios o DAG (Estágio 2) é
      derivado da Modelagem (Req. 3.1, 10.1). A implementação de curadoria
      (:class:`CuradoriaGeradorDeDag`, task 4.2) **ignora** ``modelagem`` (o
      grafo curado já existe em disco) e devolve exatamente um ``DagProduzido``
      com ``origem = curadoria_humana``; uma futura implementação de IA
      (``AiGeradorDeDag``, **fora de escopo** — Req. 9) **usaria** ``modelagem``
      para produzir DAGs com ``origem = gerado_ia`` sem redesenho.

``DagProduzido``
    O resultado tipado (Pydantic) de uma geração, pronto para persistência como
    uma entidade ``Dag`` (``origem``, ``identificador``, ``rotulo`` e o
    ``conteudo`` estrutural do grafo). Os campos espelham diretamente as colunas
    da entidade ``Dag`` (Req. 4.1–4.4), de modo que ``DagService``
    (task 5.1) possa persistir um ``DagProduzido`` sem conversão extra.

Como no restante do pacote ``dag``, nada aqui acessa a rede nem invoca IA; a
geração de curadoria lê apenas o disco local via ``CaseService`` (Req. 5.5, 9.1).

_Requisitos: 3.1, 10.1_
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.models.enums import OrigemDag
from app.services.case_service import CaseService, DagData
from app.services.dag.dag_service import DagService
from app.services.stage.stage_service import RelationshipElement, TechniqueElement


class ModelagemEstrutural(BaseModel):
    """A Modelagem Estrutural (Estágio 1) consumida pela geração de DAG (Estágio 2).

    DTO tipado que transporta a saída estrutural do Estágio 1 — no mínimo as
    **técnicas** ATT&CK e as **relações** entre elas — reaproveitando os mesmos
    elementos de :class:`~app.services.stage.stage_service.Stage1Output`
    (:class:`~app.services.stage.stage_service.TechniqueElement` e
    :class:`~app.services.stage.stage_service.RelationshipElement`), de modo que
    o grafo do Estágio 2 seja **derivado da Modelagem** (Req. 3.1, 10.1).

    No esquema de cinco estágios, o DAG (Estágio 2) é derivado da Modelagem
    Estrutural (Estágio 1). Esta é a *entrada* da costura :class:`GeradorDeDag`:
    a implementação de curadoria a **ignora** (o grafo curado já existe em
    disco), enquanto uma futura implementação de IA (``AiGeradorDeDag``, fora de
    escopo — Req. 9) a usará para gerar o grafo sem redesenho da costura.
    """

    techniques: list[TechniqueElement] = Field(default_factory=list)
    relationships: list[RelationshipElement] = Field(default_factory=list)


class DagProduzido(BaseModel):
    """Um DAG produzido por um :class:`GeradorDeDag`, pronto para persistência.

    Espelha as colunas da entidade ``Dag`` (Req. 4.1–4.4), de modo que um
    ``DagProduzido`` possa ser persistido diretamente por ``DagService``
    (task 5.1) sem conversão intermediária:

    * ``origem`` — a :class:`~app.models.enums.OrigemDag` que carimba a procedência
      do DAG (``curadoria_humana`` para a implementação de curadoria;
      ``gerado_ia`` para uma futura implementação de IA — Req. 4.3, 9.3).
    * ``identificador`` — identificador estável do DAG dentro da Campanha (por
      exemplo o *slug* do caso curado), usado para o backfill idempotente
      (task 5.1).
    * ``rotulo`` — rótulo legível opcional exibido na UI (``None`` quando não há
      um rótulo específico).
    * ``conteudo`` — o grafo estrutural no formato
      ``{"structural_nodes": [...], "arestas": [{"de", "para"}]}``: a lista de
      nós estruturais e as arestas direcionadas (chaves pt-BR ``de``/``para``,
      exatamente como o contrato de ``GET /api/casos/{caso}/dag`` e o helper
      ``DagService._derivar_arestas``). É um ``dict`` para casar com a coluna
      ``conteudo`` (JSON) da entidade ``Dag`` e permitir que uma implementação
      de IA produza o mesmo formato sem mudança de esquema (Req. 9.3, 9.4).
    """

    origem: OrigemDag
    identificador: str
    rotulo: Optional[str] = None
    conteudo: dict = Field(default_factory=dict)


@runtime_checkable
class GeradorDeDag(Protocol):
    """Costura de extensão de geração de DAG (Req. 5.1, 5.3).

    Um ``Protocol`` ``@runtime_checkable`` com uma única responsabilidade:
    produzir DAGs para uma Campanha a partir da Modelagem Estrutural (Estágio 1).
    Qualquer objeto que exponha ``origem()`` e ``gerar(caso, modelagem)`` com
    estas assinaturas satisfaz o protocolo — nenhuma herança explícita é exigida
    (duck typing estrutural).

    Hoje existe uma implementação de curadoria
    (:class:`CuradoriaGeradorDeDag`, task 4.2); uma implementação de IA
    (``AiGeradorDeDag``) pode ser adicionada depois **sem** alterar esta
    assinatura nem a entidade ``Dag`` (Req. 5.3, 9.3). ``DagService`` injeta um
    ``GeradorDeDag`` (curadoria por padrão), de modo que trocar a origem é uma
    questão de injeção, não de redesenho.
    """

    def origem(self) -> OrigemDag:
        """A :class:`~app.models.enums.OrigemDag` carimbada nos DAGs produzidos."""
        ...

    def gerar(self, caso: str, modelagem: "ModelagemEstrutural") -> list["DagProduzido"]:
        """Produz os DAGs do *slug* ``caso`` a partir da Modelagem Estrutural.

        ``modelagem`` é a saída do Estágio 1 (técnicas + relações); o DAG do
        Estágio 2 é derivado dela. A implementação de curadoria
        (:class:`CuradoriaGeradorDeDag`) **ignora** ``modelagem`` — o grafo
        curado já existe em disco, e ela devolve exatamente um ``DagProduzido``
        (``origem=curadoria_humana``) lido de ``data/dag/{slug}_dag.json`` (uma
        Campanha sem DAG derivável resulta em lista vazia). A futura
        implementação de IA (``AiGeradorDeDag``) **usará** ``modelagem`` para
        gerar o grafo. Não acessa a rede nem invoca IA (Req. 5.5, 9.1).
        """
        ...


class CuradoriaGeradorDeDag:
    """Implementação de curadoria da costura :class:`GeradorDeDag` (Req. 5.2, 5.4, 5.5).

    Produz o DAG de uma Campanha a partir do material **já curado por humano** em
    ``data/dag/{slug}_dag.json``, carimbando ``origem = curadoria_humana``
    (Req. 5.2, 5.4). A leitura é feita exclusivamente pelo disco local via
    :class:`~app.services.case_service.CaseService`
    (:meth:`~app.services.case_service.CaseService.try_load_case`): **não há
    acesso à rede nem invocação de qualquer serviço de IA** em nenhum caminho
    desta implementação (Req. 5.5, 9.1).

    O ``conteudo`` do :class:`DagProduzido` é montado no mesmo formato do contrato
    de ``GET /api/casos/{caso}/dag`` — ``structural_nodes`` (os nós estruturais
    com a identidade ATT&CK) e ``arestas`` (``{"de", "para"}``) — reaproveitando
    :meth:`DagService._derivar_arestas`, de modo que as arestas persistidas sejam
    sempre consistentes com as que o Mermaid desenha (Req. 4.4).

    Satisfaz o :class:`GeradorDeDag` estruturalmente (``origem()`` + ``gerar()``),
    sem herança explícita — exatamente o ponto da costura (Req. 5.1, 5.3).

    _Requisitos: 5.2, 5.4, 5.5_
    """

    def __init__(self, case_service: Optional[CaseService] = None) -> None:
        # Reaproveita o CaseService para resolução de caminho e parsing tipado;
        # um CaseService customizado pode ser injetado (ex.: testes/fixtures).
        self._case_service: CaseService = (
            case_service if case_service is not None else CaseService()
        )

    @property
    def case_service(self) -> CaseService:
        """O :class:`CaseService` usado para localizar e ler o DAG curado."""
        return self._case_service

    def origem(self) -> OrigemDag:
        """A origem carimbada nos DAGs produzidos: sempre ``curadoria_humana`` (Req. 5.4)."""
        return OrigemDag.CURADORIA_HUMANA

    @staticmethod
    def _montar_conteudo(dag: DagData) -> dict:
        """Monta o ``conteudo`` estrutural persistível a partir de um :class:`DagData`.

        Produz ``{"structural_nodes": [...], "arestas": [{"de", "para"}]}``: um nó
        por ``structural_node`` que carrega um ``node_id`` (com a identidade
        ATT&CK ``technique_id``/``technique_name``/``tactic`` e as relações
        ``parent_nodes``/``child_nodes`` preservadas), e uma aresta por relação
        derivada via :meth:`DagService._derivar_arestas` — a mesma rotina usada
        por ``montar_mermaid``/``obter_dag``, para que as arestas persistidas
        casem exatamente com as do grafo (Req. 4.4).

        Pura: determinística, sem I/O e sem rede.
        """
        structural_nodes = [
            {
                "node_id": node.node_id,
                "technique_id": node.technique_id,
                "technique_name": node.technique_name,
                "tactic": node.tactic,
                "parent_nodes": list(node.parent_nodes),
                "child_nodes": list(node.child_nodes),
            }
            for node in dag.structural_nodes
            if node.node_id is not None
        ]
        arestas = [
            {"de": src, "para": dst} for src, dst in DagService._derivar_arestas(dag)
        ]
        return {"structural_nodes": structural_nodes, "arestas": arestas}

    def gerar(self, caso: str, modelagem: ModelagemEstrutural) -> list[DagProduzido]:
        """Produz o DAG curado do *slug* ``caso`` como uma lista de ``DagProduzido``.

        ``modelagem`` (saída do Estágio 1) é **intencionalmente ignorada** por
        esta implementação de curadoria — o grafo curado já existe em disco, e o
        parâmetro só está aqui para honrar a assinatura da costura
        :class:`GeradorDeDag` (Req. 3.1, 10.1), de modo que uma futura
        implementação de IA (``AiGeradorDeDag``, fora de escopo — Req. 9) a use
        para gerar o grafo sem redesenho da costura.

        Lê ``data/dag/{slug}_dag.json`` via
        :meth:`~app.services.case_service.CaseService.try_load_case` (que **nunca
        levanta** por arquivo ausente/malformado — Req. 5.5) e devolve
        **exatamente um** :class:`DagProduzido` com ``origem = curadoria_humana``
        (Req. 5.2, 5.4). Um Caso cujo DAG curado está ausente/malformado — ou um
        Caso sem DAG — resulta em **lista vazia** (nenhum ``Dag`` inválido é
        produzido para o backfill persistir, Req. 4.5), sem derrubar os demais.

        Não acessa a rede e não invoca IA em nenhum caminho (Req. 5.5, 9.1).
        """
        resultado = self._case_service.try_load_case(caso)

        # DAG curado ausente/malformado (ou Caso sem DAG): nada a produzir — o
        # backfill não deve persistir um Dag inválido (Req. 4.5, 5.5).
        if not resultado.ok or resultado.case is None:
            return []
        dag = resultado.case.dag
        if dag is None:
            return []

        return [
            DagProduzido(
                origem=OrigemDag.CURADORIA_HUMANA,
                identificador=caso,
                rotulo=resultado.case.nome,
                conteudo=self._montar_conteudo(dag),
            )
        ]
