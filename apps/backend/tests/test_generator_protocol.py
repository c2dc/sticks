"""Testes de exemplo de conformidade com o Protocol ``FlowGenerator`` (task 3.4).

Estes exemplos verificam, por *duck typing* estrutural em tempo de execução
(``@runtime_checkable``), o contrato da **costura de geração de attack flow** sob a nova
assinatura de duas vias ``gerar(campaign, modeling)`` do esquema de cinco estágios,
em que o attack flow (Estágio 2) é derivado da Modelagem Estrutural (Estágio 1)
(Req. 3.1, 10.1):

1. Um *stub* ``gerado_ia`` — que apenas expõe ``origin()`` e ``gerar(campaign,
   modeling)`` **sem** herdar de ``FlowGenerator`` — também satisfaz o Protocol,
   provando que uma origin futura (IA), que **recebe a Modelagem** para produzir
   the graph, fits the seam without redesign (Req. 3.1, 10.1).
2. Um objeto que **não** expõe ``origin``/``gerar`` NÃO satisfaz o Protocol,
   confirmando que a verificação estrutural de fato exige a assinatura.

Nota: ``@runtime_checkable`` verifica apenas os **nomes** dos métodos, então os
``isinstance`` passam independentemente da aridade; ainda assim o stub adota a
assinatura de duas vias para modelar o contrato real — o teste comprova que
``gerar("x", StructuralModeling())`` pode ser chamado sem erro e devolve uma
lista de ``ProducedFlow`` com ``origin = gerado_ia``.

São asserções de exemplo (não property tests): não há geração de entradas, nem
I/O, nem rede — apenas a verificação estrutural do Protocol.

# Feature: pipeline-cinco-stages
# Validates: Requirements 3.1, 10.1
"""

from __future__ import annotations

from app.models.enums import FlowOrigin
from app.services.flow.generator import (
    FlowGenerator,
    ProducedFlow,
    StructuralModeling,
)


class _StubGeradoIa:
    """Stub ``gerado_ia`` que satisfaz ``FlowGenerator`` sem herança explícita.

    Expõe apenas ``origin()`` e ``gerar(campaign, modeling)`` com as assinaturas da
    nova costura, carimbando ``FlowOrigin.AI_GENERATED`` — exatamente o ponto da
    costura: uma origin futura **recebe a Modelagem** (Estágio 1) e encaixa por
    structural *duck typing*, without redesign (Req. 3.1, 10.1).
    """

    def origin(self) -> FlowOrigin:
        return FlowOrigin.AI_GENERATED

    def generate(self, campaign: str, modeling: StructuralModeling) -> list[ProducedFlow]:
        # Uma implementação real de IA derivaria o grafo de ``modeling``; o stub
        # apenas comprova que a assinatura de duas vias é honrada e devolve um
        # ProducedFlow com origin gerado_ia.
        _ = modeling
        return [
            ProducedFlow(
                origin=FlowOrigin.AI_GENERATED,
                identifier=campaign,
                label=None,
                content={"structural_nodes": [], "edges": []},
            )
        ]


class _NaoConforma:
    """Objeto que NÃO expõe ``origin``/``gerar`` — não satisfaz o Protocol."""

    def outra_coisa(self) -> None:  # pragma: no cover - nunca chamado
        ...


def test_stub_ai_generated_also_satisfies_the_protocol() -> None:
    """Um stub ``gerado_ia`` satisfaz o Protocol sem herança — extensível (Req. 3.1, 10.1)."""
    stub = _StubGeradoIa()

    assert isinstance(stub, FlowGenerator)
    assert stub.origin() == FlowOrigin.AI_GENERATED


def test_stub_ai_generated_generate_receives_modeling_and_produces_ai_generated() -> None:
    """O stub ``gerado_ia`` aceita a Modelagem e produz ``ProducedFlow`` gerado_ia (Req. 3.1, 10.1)."""
    stub = _StubGeradoIa()

    produzidos = stub.generate("x", StructuralModeling())

    assert isinstance(produzidos, list)
    assert all(isinstance(flow, ProducedFlow) for flow in produzidos)
    assert all(flow.origin == FlowOrigin.AI_GENERATED for flow in produzidos)


def test_object_without_origin_and_generate_does_not_satisfy_the_protocol() -> None:
    """Um objeto sem ``origin``/``gerar`` NÃO satisfaz o Protocol (Req. 3.1)."""
    assert not isinstance(_NaoConforma(), FlowGenerator)
    assert not isinstance(object(), FlowGenerator)
