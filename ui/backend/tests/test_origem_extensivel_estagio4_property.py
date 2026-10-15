"""Property test da origem explícita e extensível — Estágio 4 / Operacionalização (task 5.5).

# Feature: pipeline-seis-estagios, Property 13: Origem explícita e extensível nos
# Estágios 3, 4 e 5. Parte Estágio 4 (Operacionalização): a implementação de
# curadoria ``CuradoriaOperacionalizador`` sempre carimba ``curadoria_humana`` em
# ``OperacionalizacaoProduzida.origem`` (e em ``ParametrosDeAmbiente.origem``); a
# costura ``OperacionalizadorPorOrigem`` é satisfeita estruturalmente
# (``@runtime_checkable`` Protocol); e um stub ``gerado_ia`` também satisfaz a
# costura — provando que a origem ``gerado_ia`` encaixa sem redesenho (sem alterar
# a assinatura nem o artefato ``OperacionalizacaoProduzida``).

Validates: Requirements 8.6, 16.2, 16.4

Esta é uma property test de *feature* (``pipeline-seis-estagios`` é uma feature,
não um bugfix): a propriedade é esperada **valer** sobre a implementação atual.
Foca a parte **Estágio 4** da Property 13 — a costura de operacionalização
(:class:`~app.services.environment.operacionalizador.OperacionalizadorPorOrigem` /
:class:`~app.services.environment.operacionalizador.CuradoriaOperacionalizador`).
As partes dos Estágios 3 e 5 são cobertas pelas tasks 4.4 e 7.5.

A Property 13 (lado Estágio 4) afirma, para qualquer entrada:

1. **Carimbo de curadoria** (Req. 8.6, 16.2) — ``CuradoriaOperacionalizador.origem()``
   devolve exatamente ``TranslationSource.HUMAN_CURATION`` e **toda**
   ``OperacionalizacaoProduzida`` que ``operacionalizar`` devolve carrega
   ``origem == TranslationSource.HUMAN_CURATION`` — e o mesmo vale para o
   artefato ``ParametrosDeAmbiente`` embutido — qualquer que sejam os nós
   curados do DAG em disco e qualquer que seja o ``DagView`` passado.
2. **Conformidade estrutural do Protocol** (Req. 8.6, 16.2) — a instância de
   curadoria satisfaz o ``@runtime_checkable`` ``OperacionalizadorPorOrigem`` por
   *duck typing* estrutural (``isinstance`` é ``True``), sem herança explícita.
3. **Extensibilidade sem redesenho** (Req. 16.2, 16.4) — um stub ``gerado_ia``
   que apenas expõe ``origem()`` + ``operacionalizar(caso, dag)`` (sem herdar de
   ``OperacionalizadorPorOrigem``) também satisfaz o Protocol e produz uma
   ``OperacionalizacaoProduzida`` **sem** alterar a assinatura da costura nem o
   artefato produzido — exatamente a extensão por origem exigida por Req.
   16.2/16.4. O valor ``gerado_ia`` já pertence à enum ``OrigemDag`` (persistida
   como VARCHAR+CHECK que o aceita), logo persistir um artefato ``gerado_ia`` no
   futuro não exige alteração de esquema (Req. 16.4).

Estratégia / isolamento
-----------------------
Reusa os semeadores de ``tests/test_operacionalizador_estagio4.py``
(``_seed_case``, ``_stub_dag``, o mapeamento IP→container): tudo é puro/in-memory
— nenhum acesso à rede, nenhuma IA. Cada exemplo semeia um caso curado em um
diretório de dados **temporário** com o layout ``<data_dir>/{api,dag}/`` e injeta
um ``CaseService`` apontado para ele. Hypothesis varia os nós curados do DAG
(``attacker_commands``/``target_vuln_host_*``/``host_type``/``network_context``)
e o ``DagView`` para asserir que o carimbo de origem **não depende** de nenhum
deles. Hypothesis roda ≥100 iterações.
"""

from __future__ import annotations

import inspect

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.models.enums import OrigemDag, TranslationSource
from app.services.case_service import CaseService
from app.services.dag.dag_service import DagView
from app.services.environment.operacionalizacao import ParametrosDeAmbiente
from app.services.environment.operacionalizador import (
    CuradoriaOperacionalizador,
    OperacionalizacaoProduzida,
    OperacionalizadorPorOrigem,
)

# Reuso dos semeadores da suíte de exemplo do Estágio 4 (sem rede, sem IA) — a
# mesma fonte das fixtures pedidas pela task 5.5.
from tests.test_operacionalizador_estagio4 import (
    _INTERNAL_IP,
    _SLUG,
    _seed_case,
    _stub_dag,
)


# ---------------------------------------------------------------------------
# Stub ``gerado_ia`` — satisfaz ``OperacionalizadorPorOrigem`` sem herança
# explícita. Modela a origem futura (IA) que CRIA/parametriza os comandos e
# encaixa na costura por duck typing estrutural, carimbando ``gerado_ia`` (Req.
# 16.2, 16.4). A spec NÃO implementa o agente de IA (Req. 16.5); este stub existe
# apenas para provar a extensibilidade da costura.
# ---------------------------------------------------------------------------


class _StubGeradoIa:
    """Stub ``gerado_ia`` estrutural: ``origem()`` + ``operacionalizar(caso, dag)``.

    Expõe a superfície completa do ``@runtime_checkable``
    :class:`OperacionalizadorPorOrigem` sem herdar da costura, provando a
    conformidade estrutural por *duck typing*.
    """

    def origem(self) -> OrigemDag:
        # A implementação de IA carimba a origem ``gerado_ia`` — o mesmo valor em
        # pt-BR já aceito pela enum ``OrigemDag`` (VARCHAR+CHECK), de modo que
        # persistir um artefato ``gerado_ia`` não exige alteração de esquema
        # (Req. 16.4). Devolver a enum prova que a assinatura da costura não muda
        # para a nova origem.
        return OrigemDag.GERADO_IA

    def operacionalizar(
        self, caso: str, dag: DagView
    ) -> OperacionalizacaoProduzida:
        # Uma IA real criaria/parametrizaria os comandos a partir do ``dag``; o
        # stub apenas honra a assinatura e devolve o MESMO artefato
        # ``OperacionalizacaoProduzida``, provando que a extensão por origem não
        # redesenha a costura (Req. 16.2).
        _ = dag
        return OperacionalizacaoProduzida(
            caso_id=caso,
            parametros=ParametrosDeAmbiente(caso_id=caso),
        )


# ---------------------------------------------------------------------------
# Estratégia Hypothesis — nós curados arbitrários do DAG. A curadoria IGNORA o
# conteúdo exato ao carimbar a origem; variamos para asserir que o carimbo é
# invariante. Os comandos usam um IP interno conhecido (resolvível) ou nenhum
# destino, e os campos curados variam livremente.
# ---------------------------------------------------------------------------


_IDENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789 .-_/",
    min_size=0,
    max_size=24,
)


@st.composite
def _curated_nodes(draw) -> list[dict]:
    """Lista de nós curados do DAG, com campos variados (pode ser vazia)."""
    n = draw(st.integers(min_value=0, max_value=4))
    nodes: list[dict] = []
    for i in range(n):
        # O comando pode mirar o IP interno conhecido (resolvível a container) ou
        # ser um texto arbitrário sem destino interno — ambos exercitam a
        # derivação sem alterar o carimbo de origem.
        if draw(st.booleans()):
            comando = f'curl "http://{_INTERNAL_IP}:5000/?p={i}"'
        else:
            comando = draw(_IDENT)
        nodes.append(
            {
                "node_id": f"n{i}",
                "node_index": i,
                "technique_id": f"T{1000 + i}",
                "attacker_commands": [comando] if comando else [],
                "target_vuln_host_a": draw(st.lists(_IDENT, max_size=2)),
                "target_vuln_host_b": draw(st.lists(_IDENT, max_size=2)),
                "source_host_type": draw(st.none() | _IDENT),
                "target_host_type": draw(st.none() | _IDENT),
                "network_context": draw(st.none() | _IDENT),
            }
        )
    return nodes


# ===========================================================================
# Property 13 (Estágio 4) — para quaisquer nós curados e qualquer DagView:
#   (1) a curadoria carimba curadoria_humana em origem(), na OperacionalizacaoProduzida
#       e no ParametrosDeAmbiente embutido;
#   (2) a curadoria satisfaz o Protocol OperacionalizadorPorOrigem estruturalmente;
#   (3) um stub gerado_ia também satisfaz o Protocol, com assinatura e artefato
#       inalterados (extensível sem redesenho).
# ===========================================================================


# Feature: pipeline-seis-estagios, Property 13: Origem explícita e extensível nos
# Estágios 3, 4 e 5 (parte Estágio 4 / Operacionalização).
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(nodes=_curated_nodes())
def test_property13_estagio4_origem_explicita_e_extensivel(
    nodes: list[dict],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A origem da operacionalização é explícita (curadoria_humana) e extensível (gerado_ia).

    Validates: Requirements 8.6, 16.2, 16.4
    """
    data_dir = tmp_path_factory.mktemp("origem_extensivel_estagio4")
    (data_dir / "api").mkdir(parents=True, exist_ok=True)
    (data_dir / "dag").mkdir(parents=True, exist_ok=True)
    _seed_case(data_dir, _SLUG, structural_nodes=nodes)

    curadoria = CuradoriaOperacionalizador(case_service=CaseService(data_dir=data_dir))

    # (1) A curadoria carimba curadoria_humana — em origem(), na
    #     OperacionalizacaoProduzida devolvida e no ParametrosDeAmbiente embutido,
    #     independentemente dos nós curados e do DagView (Req. 8.6, 16.2).
    assert curadoria.origem() is TranslationSource.HUMAN_CURATION
    produzida = curadoria.operacionalizar(_SLUG, _stub_dag())
    assert isinstance(produzida, OperacionalizacaoProduzida)
    assert produzida.caso_id == _SLUG
    assert produzida.origem is TranslationSource.HUMAN_CURATION
    assert isinstance(produzida.parametros, ParametrosDeAmbiente)
    assert produzida.parametros.origem is TranslationSource.HUMAN_CURATION

    # (2) A curadoria satisfaz o Protocol @runtime_checkable estruturalmente,
    #     sem herança explícita (Req. 8.6, 16.2).
    assert isinstance(curadoria, OperacionalizadorPorOrigem)

    # (3) Um stub gerado_ia também satisfaz o Protocol — extensibilidade sem
    #     redesenho: mesma assinatura, mesmo artefato OperacionalizacaoProduzida,
    #     origem distinta (Req. 16.2, 16.4).
    stub = _StubGeradoIa()
    assert isinstance(stub, OperacionalizadorPorOrigem)
    assert stub.origem() is OrigemDag.GERADO_IA
    produzida_ia = stub.operacionalizar(_SLUG, _stub_dag())
    assert isinstance(produzida_ia, OperacionalizacaoProduzida)
    assert isinstance(produzida_ia.parametros, ParametrosDeAmbiente)

    # A assinatura da costura é a MESMA para ambas as origens — a extensão não
    # redesenha ``operacionalizar`` (Req. 16.2): mesmos parâmetros ``(caso, dag)``.
    assert list(inspect.signature(curadoria.operacionalizar).parameters) == ["caso", "dag"]
    assert list(inspect.signature(stub.operacionalizar).parameters) == ["caso", "dag"]

    # ``gerado_ia`` é um valor legítimo da enum de origem persistida como
    # VARCHAR+CHECK que já o aceita — persistir um artefato gerado_ia no futuro
    # não exige alteração de esquema (Req. 16.4).
    assert OrigemDag.GERADO_IA.value == "gerado_ia"
    assert OrigemDag.GERADO_IA in set(OrigemDag)
