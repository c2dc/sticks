"""Testes de exemplo de conformidade com o Protocol ``GeradorDeDag`` (task 3.4).

Estes exemplos verificam, por *duck typing* estrutural em tempo de execução
(``@runtime_checkable``), o contrato da **costura de geração de DAG** sob a nova
assinatura de duas vias ``gerar(caso, modelagem)`` do esquema de cinco estágios,
em que o DAG (Estágio 2) é derivado da Modelagem Estrutural (Estágio 1)
(Req. 3.1, 10.1):

1. A implementação de curadoria :class:`CuradoriaGeradorDeDag` satisfaz o
   :class:`GeradorDeDag` (``isinstance`` é ``True``) e ``origem()`` carimba
   exatamente ``OrigemDag.CURADORIA_HUMANA``.
2. Um *stub* ``gerado_ia`` — que apenas expõe ``origem()`` e ``gerar(caso,
   modelagem)`` **sem** herdar de ``GeradorDeDag`` — também satisfaz o Protocol,
   provando que uma origem futura (IA), que **recebe a Modelagem** para produzir
   o grafo, encaixa na costura sem redesenho (Req. 3.1, 10.1).
3. Um objeto que **não** expõe ``origem``/``gerar`` NÃO satisfaz o Protocol,
   confirmando que a verificação estrutural de fato exige a assinatura.

Nota: ``@runtime_checkable`` verifica apenas os **nomes** dos métodos, então os
``isinstance`` passam independentemente da aridade; ainda assim o stub adota a
assinatura de duas vias para modelar o contrato real — o teste comprova que
``gerar("x", ModelagemEstrutural())`` pode ser chamado sem erro e devolve uma
lista de ``DagProduzido`` com ``origem = gerado_ia``.

São asserções de exemplo (não property tests): não há geração de entradas, nem
I/O, nem rede — apenas a verificação estrutural do Protocol.

# Feature: pipeline-cinco-estagios
# Validates: Requirements 3.1, 10.1
"""

from __future__ import annotations

from app.models.enums import OrigemDag
from app.services.dag.gerador import (
    CuradoriaGeradorDeDag,
    DagProduzido,
    GeradorDeDag,
    ModelagemEstrutural,
)


class _StubGeradoIa:
    """Stub ``gerado_ia`` que satisfaz ``GeradorDeDag`` sem herança explícita.

    Expõe apenas ``origem()`` e ``gerar(caso, modelagem)`` com as assinaturas da
    nova costura, carimbando ``OrigemDag.GERADO_IA`` — exatamente o ponto da
    costura: uma origem futura **recebe a Modelagem** (Estágio 1) e encaixa por
    *duck typing* estrutural, sem redesenho (Req. 3.1, 10.1).
    """

    def origem(self) -> OrigemDag:
        return OrigemDag.GERADO_IA

    def gerar(
        self, caso: str, modelagem: ModelagemEstrutural
    ) -> list[DagProduzido]:
        # Uma implementação real de IA derivaria o grafo de ``modelagem``; o stub
        # apenas comprova que a assinatura de duas vias é honrada e devolve um
        # DagProduzido com origem gerado_ia.
        _ = modelagem
        return [
            DagProduzido(
                origem=OrigemDag.GERADO_IA,
                identificador=caso,
                rotulo=None,
                conteudo={"structural_nodes": [], "arestas": []},
            )
        ]


class _NaoConforma:
    """Objeto que NÃO expõe ``origem``/``gerar`` — não satisfaz o Protocol."""

    def outra_coisa(self) -> None:  # pragma: no cover - nunca chamado
        ...


def test_curadoria_satisfaz_o_protocol_gerador_de_dag() -> None:
    """``CuradoriaGeradorDeDag`` satisfaz ``GeradorDeDag`` via isinstance (Req. 3.1)."""
    gerador = CuradoriaGeradorDeDag()

    assert isinstance(gerador, GeradorDeDag)


def test_curadoria_origem_devolve_curadoria_humana() -> None:
    """``origem()`` da curadoria carimba exatamente ``CURADORIA_HUMANA`` (Req. 3.1)."""
    gerador = CuradoriaGeradorDeDag()

    assert gerador.origem() == OrigemDag.CURADORIA_HUMANA


def test_stub_gerado_ia_tambem_satisfaz_o_protocol() -> None:
    """Um stub ``gerado_ia`` satisfaz o Protocol sem herança — extensível (Req. 3.1, 10.1)."""
    stub = _StubGeradoIa()

    assert isinstance(stub, GeradorDeDag)
    assert stub.origem() == OrigemDag.GERADO_IA


def test_stub_gerado_ia_gerar_recebe_modelagem_e_produz_gerado_ia() -> None:
    """O stub ``gerado_ia`` aceita a Modelagem e produz ``DagProduzido`` gerado_ia (Req. 3.1, 10.1)."""
    stub = _StubGeradoIa()

    produzidos = stub.gerar("x", ModelagemEstrutural())

    assert isinstance(produzidos, list)
    assert all(isinstance(dag, DagProduzido) for dag in produzidos)
    assert all(dag.origem == OrigemDag.GERADO_IA for dag in produzidos)


def test_objeto_sem_origem_e_gerar_nao_satisfaz_o_protocol() -> None:
    """Um objeto sem ``origem``/``gerar`` NÃO satisfaz o Protocol (Req. 3.1)."""
    assert not isinstance(_NaoConforma(), GeradorDeDag)
    assert not isinstance(object(), GeradorDeDag)
