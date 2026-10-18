"""Property test da origin explícita e extensível — Estágio 4 / Operacionalização (task 5.5).

# Feature: pipeline-seis-stages, Property 13: Origem explícita e extensível nodes
# Estágios 3, 4 e 5. Parte Estágio 4 (Operacionalização): a implementação de
# curation ``CurationOperationalizer`` sempre carimba ``curadoria_humana`` em
# ``ProducedOperationalization.origin`` (e em ``EnvironmentParameters.origin``); a
# costura ``OperationalizerByOrigin`` é satisfeita estruturalmente
# (``@runtime_checkable`` Protocol); e um stub ``gerado_ia`` também satisfaz a
# seam — proving that the ``gerado_ia`` origin fits without redesign (without changing
# a assinatura nem o artefato ``ProducedOperationalization``).

Validates: Requirements 8.6, 16.2, 16.4

Esta é uma property test de *feature* (``pipeline-seis-stages`` é uma feature,
não um bugfix): a propriedade é esperada **valer** sobre a implementação atual.
Foca a parte **Estágio 4** da Property 13 — a costura de operacionalização
(:class:`~app.services.environment.operationalizer.OperationalizerByOrigin` /
:class:`~app.services.environment.operationalizer.CurationOperationalizer`).
As partes dos Estágios 3 e 5 são cobertas pelas tasks 4.4 e 7.5.

A Property 13 (lado Estágio 4) afirma, para qualquer input:

1. **Carimbo de curation** (Req. 8.6, 16.2) — ``CurationOperationalizer.origin()``
   devolve exatamente ``TranslationSource.HUMAN_CURATION`` e **toda**
   ``ProducedOperationalization`` que ``operationalize`` devolve carrega
   ``origin == TranslationSource.HUMAN_CURATION`` — e o mesmo vale para o
   artefato ``EnvironmentParameters`` embutido — qualquer que sejam os nós
   curados do DAG em disco e qualquer que seja o ``DagView`` passado.
2. **Conformidade estrutural do Protocol** (Req. 8.6, 16.2) — a instância de
   curation satisfaz o ``@runtime_checkable`` ``OperationalizerByOrigin`` por
   *duck typing* estrutural (``isinstance`` é ``True``), sem herança explícita.
3. **Extensibility without redesign** (Req. 16.2, 16.4) — a ``gerado_ia`` stub
   que apenas expõe ``origin()`` + ``operationalize(campaign, dag)`` (sem herdar de
   ``OperationalizerByOrigin``) também satisfaz o Protocol e produz uma
   ``ProducedOperationalization`` **sem** alterar a assinatura da costura nem o
   artefato produzido — exatamente a extensão por origin exigida por Req.
   16.2/16.4. O valor ``gerado_ia`` já pertence à enum ``DagOrigin`` (persistida
   como VARCHAR+CHECK que o aceita), logo persistir um artefato ``gerado_ia`` no
   futuro não exige alteração de esquema (Req. 16.4).

Estratégia / isolation
-----------------------
Reusa os semeadores de ``tests/test_operationalizer_stage4.py``
(``_seed_case``, ``_stub_dag``, o mapeamento IP→container): tudo é puro/in-memory
— nenhum acesso à rede, nenhuma IA. Cada exemplo semeia um campaign curated em um
diretório de dados **temporário** com o layout ``<data_dir>/{api,dag}/`` e injeta
um ``CaseService`` apontado para ele. Hypothesis varia os nós curados do DAG
(``attacker_commands``/``target_vuln_host_*``/``host_type``/``network_context``)
e o ``DagView`` para asserir que o carimbo de origin **não depende** de nenhum
deles. Hypothesis roda ≥100 iterações.
"""

from __future__ import annotations

import inspect

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.models.enums import DagOrigin, TranslationSource
from app.services.campaign_service import CaseService
from app.services.dag.dag_service import DagView
from app.services.environment.operationalization import EnvironmentParameters
from app.services.environment.operationalizer import (
    CurationOperationalizer,
    ProducedOperationalization,
    OperationalizerByOrigin,
)

# Reuso dos semeadores da suíte de exemplo do Estágio 4 (sem rede, sem IA) — a
# mesma fonte das fixtures pedidas pela task 5.5.
from tests.test_operationalizer_stage4 import (
    _INTERNAL_IP,
    _SLUG,
    _seed_case,
    _stub_dag,
)


# ---------------------------------------------------------------------------
# Stub ``gerado_ia`` — satisfaz ``OperationalizerByOrigin`` sem herança
# explícita. Modela a origin futura (IA) que CRIA/parametriza os commands e
# encaixa na costura por duck typing estrutural, carimbando ``gerado_ia`` (Req.
# 16.2, 16.4). A spec NÃO implementa o agente de IA (Req. 16.5); este stub existe
# apenas para provar a extensibilidade da costura.
# ---------------------------------------------------------------------------


class _StubGeradoIa:
    """Stub ``gerado_ia`` estrutural: ``origin()`` + ``operationalize(campaign, dag)``.

    Expõe a superfície completa do ``@runtime_checkable``
    :class:`OperationalizerByOrigin` sem herdar da costura, provando a
    conformidade estrutural por *duck typing*.
    """

    def origin(self) -> DagOrigin:
        # A implementação de IA carimba a origin ``gerado_ia`` — o mesmo valor em
        # pt-BR já aceito pela enum ``DagOrigin`` (VARCHAR+CHECK), de modo que
        # persistir um artefato ``gerado_ia`` não exige alteração de esquema
        # (Req. 16.4). Devolver a enum prova que a assinatura da costura não muda
        # para a nova origin.
        return DagOrigin.AI_GENERATED

    def operationalize(
        self, campaign: str, dag: DagView
    ) -> ProducedOperationalization:
        # Uma IA real criaria/parametrizaria os commands a partir do ``dag``; o
        # stub apenas honra a assinatura e devolve o MESMO artefato
        # ``ProducedOperationalization``, provando que a extensão por origin não
        # redesigns the seam (Req. 16.2).
        _ = dag
        return ProducedOperationalization(
            campaign_id=campaign,
            parameters=EnvironmentParameters(campaign_id=campaign),
        )


# ---------------------------------------------------------------------------
# Estratégia Hypothesis — nós curados arbitrários do DAG. A curation IGNORA o
# conteúdo exato ao carimbar a origin; variamos para asserir que o carimbo é
# invariante. Os commands usam um IP interno conhecido (resolvível) ou nenhum
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
        # O command pode mirar o IP interno conhecido (resolvível a container) ou
        # ser um text arbitrário sem destino interno — ambos exercitam a
        # derivação sem alterar o carimbo de origin.
        if draw(st.booleans()):
            command = f'curl "http://{_INTERNAL_IP}:5000/?p={i}"'
        else:
            command = draw(_IDENT)
        nodes.append(
            {
                "node_id": f"n{i}",
                "node_index": i,
                "technique_id": f"T{1000 + i}",
                "attacker_commands": [command] if command else [],
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
#   (1) a curation carimba curadoria_humana em origin(), na ProducedOperationalization
#       e no EnvironmentParameters embutido;
#   (2) a curation satisfaz o Protocol OperationalizerByOrigin estruturalmente;
#   (3) um stub gerado_ia também satisfaz o Protocol, com assinatura e artefato
#       unchanged (extensible without redesign).
# ===========================================================================


# Feature: pipeline-seis-stages, Property 13: Origem explícita e extensível nodes
# Estágios 3, 4 e 5 (parte Estágio 4 / Operacionalização).
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(nodes=_curated_nodes())
def test_property13_stage4_origin_explicit_and_extensible(
    nodes: list[dict],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A origin da operacionalização é explícita (curadoria_humana) e extensível (gerado_ia).

    Validates: Requirements 8.6, 16.2, 16.4
    """
    data_dir = tmp_path_factory.mktemp("origin_extensible_stage4")
    (data_dir / "api").mkdir(parents=True, exist_ok=True)
    (data_dir / "dag").mkdir(parents=True, exist_ok=True)
    _seed_case(data_dir, _SLUG, structural_nodes=nodes)

    curation = CurationOperationalizer(case_service=CaseService(data_dir=data_dir))

    # (1) A curation carimba curadoria_humana — em origin(), na
    #     ProducedOperationalization devolvida e no EnvironmentParameters embutido,
    #     independentemente dos nós curados e do DagView (Req. 8.6, 16.2).
    assert curation.origin() is TranslationSource.HUMAN_CURATION
    produzida = curation.operationalize(_SLUG, _stub_dag())
    assert isinstance(produzida, ProducedOperationalization)
    assert produzida.campaign_id == _SLUG
    assert produzida.origin is TranslationSource.HUMAN_CURATION
    assert isinstance(produzida.parameters, EnvironmentParameters)
    assert produzida.parameters.origin is TranslationSource.HUMAN_CURATION

    # (2) A curation satisfaz o Protocol @runtime_checkable estruturalmente,
    #     sem herança explícita (Req. 8.6, 16.2).
    assert isinstance(curation, OperationalizerByOrigin)

    # (3) Um stub gerado_ia também satisfaz o Protocol — extensibilidade sem
    #     redesign: same signature, same ProducedOperationalization artifact,
    #     origin distinta (Req. 16.2, 16.4).
    stub = _StubGeradoIa()
    assert isinstance(stub, OperationalizerByOrigin)
    assert stub.origin() is DagOrigin.AI_GENERATED
    produzida_ia = stub.operationalize(_SLUG, _stub_dag())
    assert isinstance(produzida_ia, ProducedOperationalization)
    assert isinstance(produzida_ia.parameters, EnvironmentParameters)

    # A assinatura da costura é a MESMA para ambas as origens — a extensão não
    # redesigns ``operationalize`` (Req. 16.2): same parameters ``(campaign, dag)``.
    assert list(inspect.signature(curation.operationalize).parameters) == ["campaign", "dag"]
    assert list(inspect.signature(stub.operationalize).parameters) == ["campaign", "dag"]

    # ``gerado_ia`` é um valor legítimo da enum de origin persistida como
    # VARCHAR+CHECK que já o aceita — persistir um artefato gerado_ia no futuro
    # não exige alteração de esquema (Req. 16.4).
    assert DagOrigin.AI_GENERATED.value == "gerado_ia"
    assert DagOrigin.AI_GENERATED in set(DagOrigin)
