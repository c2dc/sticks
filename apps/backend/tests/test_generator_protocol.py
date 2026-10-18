"""Testes de exemplo de conformidade com o Protocol ``DagGenerator`` (task 3.4).

Estes exemplos verificam, por *duck typing* estrutural em tempo de execução
(``@runtime_checkable``), o contrato da **costura de geração de DAG** sob a nova
assinatura de duas vias ``gerar(campaign, modeling)`` do esquema de cinco estágios,
em que o DAG (Estágio 2) é derivado da Modelagem Estrutural (Estágio 1)
(Req. 3.1, 10.1):

1. A implementação de curation :class:`CurationDagGenerator` satisfaz o
   :class:`DagGenerator` (``isinstance`` é ``True``) e ``origin()`` carimba
   exatamente ``DagOrigin.HUMAN_CURATION``.
2. Um *stub* ``gerado_ia`` — que apenas expõe ``origin()`` e ``gerar(campaign,
   modeling)`` **sem** herdar de ``DagGenerator`` — também satisfaz o Protocol,
   provando que uma origin futura (IA), que **recebe a Modelagem** para produzir
   o grafo, encaixa na costura sem redesenho (Req. 3.1, 10.1).
3. Um objeto que **não** expõe ``origin``/``gerar`` NÃO satisfaz o Protocol,
   confirmando que a verificação estrutural de fato exige a assinatura.

Nota: ``@runtime_checkable`` verifica apenas os **nomes** dos métodos, então os
``isinstance`` passam independentemente da aridade; ainda assim o stub adota a
assinatura de duas vias para modelar o contrato real — o teste comprova que
``gerar("x", StructuralModeling())`` pode ser chamado sem erro e devolve uma
lista de ``ProducedDag`` com ``origin = gerado_ia``.

São asserções de exemplo (não property tests): não há geração de entradas, nem
I/O, nem rede — apenas a verificação estrutural do Protocol.

# Feature: pipeline-cinco-stages
# Validates: Requirements 3.1, 10.1
"""

from __future__ import annotations

from app.models.enums import DagOrigin
from app.services.dag.gerador import (
    CurationDagGenerator,
    ProducedDag,
    DagGenerator,
    StructuralModeling,
)


class _StubGeradoIa:
    """Stub ``gerado_ia`` que satisfaz ``DagGenerator`` sem herança explícita.

    Expõe apenas ``origin()`` e ``gerar(campaign, modeling)`` com as assinaturas da
    nova costura, carimbando ``DagOrigin.AI_GENERATED`` — exatamente o ponto da
    costura: uma origin futura **recebe a Modelagem** (Estágio 1) e encaixa por
    *duck typing* estrutural, sem redesenho (Req. 3.1, 10.1).
    """

    def origin(self) -> DagOrigin:
        return DagOrigin.AI_GENERATED

    def generate(
        self, campaign: str, modeling: StructuralModeling
    ) -> list[ProducedDag]:
        # Uma implementação real de IA derivaria o grafo de ``modeling``; o stub
        # apenas comprova que a assinatura de duas vias é honrada e devolve um
        # ProducedDag com origin gerado_ia.
        _ = modeling
        return [
            ProducedDag(
                origin=DagOrigin.AI_GENERATED,
                identifier=campaign,
                label=None,
                content={"structural_nodes": [], "edges": []},
            )
        ]


class _NaoConforma:
    """Objeto que NÃO expõe ``origin``/``gerar`` — não satisfaz o Protocol."""

    def outra_coisa(self) -> None:  # pragma: no cover - nunca chamado
        ...


def test_curadoria_satisfaz_o_protocol_gerador_de_dag() -> None:
    """``CurationDagGenerator`` satisfaz ``DagGenerator`` via isinstance (Req. 3.1)."""
    gerador = CurationDagGenerator()

    assert isinstance(gerador, DagGenerator)


def test_curadoria_origem_devolve_curadoria_humana() -> None:
    """``origin()`` da curation carimba exatamente ``HUMAN_CURATION`` (Req. 3.1)."""
    gerador = CurationDagGenerator()

    assert gerador.origin() == DagOrigin.HUMAN_CURATION


def test_stub_gerado_ia_tambem_satisfaz_o_protocol() -> None:
    """Um stub ``gerado_ia`` satisfaz o Protocol sem herança — extensível (Req. 3.1, 10.1)."""
    stub = _StubGeradoIa()

    assert isinstance(stub, DagGenerator)
    assert stub.origin() == DagOrigin.AI_GENERATED


def test_stub_gerado_ia_gerar_recebe_modelagem_e_produz_gerado_ia() -> None:
    """O stub ``gerado_ia`` aceita a Modelagem e produz ``ProducedDag`` gerado_ia (Req. 3.1, 10.1)."""
    stub = _StubGeradoIa()

    produzidos = stub.generate("x", StructuralModeling())

    assert isinstance(produzidos, list)
    assert all(isinstance(dag, ProducedDag) for dag in produzidos)
    assert all(dag.origin == DagOrigin.AI_GENERATED for dag in produzidos)


def test_objeto_sem_origem_e_gerar_nao_satisfaz_o_protocol() -> None:
    """Um objeto sem ``origin``/``gerar`` NÃO satisfaz o Protocol (Req. 3.1)."""
    assert not isinstance(_NaoConforma(), DagGenerator)
    assert not isinstance(object(), DagGenerator)
