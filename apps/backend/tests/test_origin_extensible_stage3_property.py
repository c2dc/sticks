"""Property test da origin explícita e extensível — Estágio 3 / DAG (task 4.4).

# Feature: pipeline-seis-stages, Property 13: Origem explícita e extensível nodes
# Estágios 3, 4 e 5. Parte Estágio 3 (Geração do DAG): a implementação de
# curation ``CurationDagGenerator`` sempre carimba ``curadoria_humana``; a
# costura ``DagGenerator`` é satisfeita estruturalmente (``@runtime_checkable``
# Protocol); e um stub ``gerado_ia`` também satisfaz a costura — provando que a
# origin ``gerado_ia`` fits without redesign (without changing the signature nor the
# artefato DAG).

Validates: Requirements 7.5, 16.1, 16.4

Esta é uma property test de *feature* (``pipeline-seis-stages`` é uma feature,
não um bugfix): a propriedade é esperada **valer** sobre a implementação atual.
Foca a parte **Estágio 3** da Property 13 — a costura de geração de DAG
(:class:`~app.services.dag.generator.DagGenerator` /
:class:`~app.services.dag.generator.CurationDagGenerator`). As partes dos
Estágios 4 e 5 são cobertas pelas tasks 5.5 e 7.5.

A Property 13 (lado Estágio 3) afirma, para qualquer input:

1. **Carimbo de curation** (Req. 7.5, 16.1) — ``CurationDagGenerator.origin()``
   devolve exatamente ``DagOrigin.HUMAN_CURATION`` e **todo** ``ProducedDag``
   que ela devolve carrega ``origin == DagOrigin.HUMAN_CURATION`` — qualquer
   que seja o DAG curated em disco e qualquer que seja a Modelagem passada
   (ignorada pela curation).
2. **Conformidade estrutural do Protocol** (Req. 16.1) — a instância de
   curation satisfaz o ``@runtime_checkable`` ``DagGenerator`` por *duck typing*
   estrutural (``isinstance`` é ``True``), sem herança explícita.
3. **Extensibility without redesign** (Req. 16.1, 16.4) — a ``gerado_ia`` stub
   que apenas expõe ``origin()`` + ``gerar(campaign, modeling)`` (sem herdar de
   ``DagGenerator``) também satisfaz o Protocol e produz ``ProducedDag`` com
   ``origin == DagOrigin.AI_GENERATED`` **sem** alterar a assinatura da costura nem
   o artefato ``ProducedDag`` — exatamente a extensão por origin exigida por
   Req. 16.1/16.4. O valor ``gerado_ia`` já pertence à enum ``DagOrigin`` (cujo
   persistido é VARCHAR+CHECK que o aceita), logo persistir um DAG ``gerado_ia``
   no futuro não exige alteração de esquema (Req. 16.4).

Estratégia / isolation
-----------------------
Cada exemplo escreve um ``{slug}_dag.json`` curated (acíclico por construção) em
um diretório de dados **temporário** com o layout ``<data_dir>/{api,dag}/`` e
aponta um :class:`~app.services.case_service.CaseService` para ele, exatamente
como os arquivos reais são parseados — SQLite efêmero não é necessário aqui pois
a costura de geração de DAG lê apenas o disco local via ``CaseService`` (nenhum
state de banco é envolvido na parte Estágio 3). A Modelagem é variada por
Hypothesis para exercitar que a curation a ignora. Hypothesis roda ≥100
iterações. Nada aqui toca a rede nem invoca IA.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.models.enums import DagOrigin
from app.services.campaign_service import CURATED_CASES, CaseService
from app.services.dag.generator import (
    CurationDagGenerator,
    ProducedDag,
    DagGenerator,
    StructuralModeling,
)
from app.services.stage.stage_service import RelationshipElement, TechniqueElement


# Um slug de campaign curated real: a geração de curation só produz um DAG para
# slugs curados (``CaseService.try_load_case``).
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guarda: mantém o slug do fixture curated


# ---------------------------------------------------------------------------
# Stub ``gerado_ia`` — satisfaz ``DagGenerator`` sem herança explícita. Modela a
# origin futura (IA) que RECEBE a Modelagem e encaixa na costura por duck typing
# estrutural, carimbando ``DagOrigin.AI_GENERATED`` (Req. 16.1, 16.4). A spec NÃO
# implementa o agente de IA (Req. 16.5); este stub existe apenas para provar a
# extensibilidade da costura.
# ---------------------------------------------------------------------------


class _StubGeradoIa:
    """Stub ``gerado_ia`` estrutural: ``origin()`` + ``gerar(campaign, modeling)``."""

    def origin(self) -> DagOrigin:
        return DagOrigin.AI_GENERATED

    def generate(
        self, campaign: str, modeling: StructuralModeling
    ) -> list[ProducedDag]:
        # Uma implementação real de IA derivaria o grafo de ``modeling``; o stub
        # apenas honra a assinatura de duas vias e carimba gerado_ia.
        _ = modeling
        return [
            ProducedDag(
                origin=DagOrigin.AI_GENERATED,
                identifier=campaign,
                label=None,
                content={"structural_nodes": [], "edges": []},
            )
        ]


# ---------------------------------------------------------------------------
# Construtor de fixture — escreve ability + {slug}_dag.json em um data dir
# temporário com o layout <data_dir>/{api,dag}/, para que CaseService(data_dir=...)
# resolva os arquivos. Sem rede, sem IA.
# ---------------------------------------------------------------------------


def _write_case_fixture(
    data_dir: Path, slug: str, structural_nodes: list[dict]
) -> None:
    api_dir = data_dir / "api"
    dag_dir = data_dir / "dag"
    api_dir.mkdir(parents=True, exist_ok=True)
    dag_dir.mkdir(parents=True, exist_ok=True)

    (api_dir / f"{slug}_dag-ability.json").write_text("[]", encoding="utf-8")

    dag_payload = {
        "campaign_name": slug,
        "metadata": {"validation": {"is_dag": True}},
        "structural_nodes": structural_nodes,
    }
    (dag_dir / f"{slug}_dag.json").write_text(
        json.dumps(dag_payload), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Estratégia Hypothesis — DAG curated acíclico (edges só anterior->posterior).
# ---------------------------------------------------------------------------


@st.composite
def _acyclic_dag(draw) -> list[dict]:
    n = draw(st.integers(min_value=1, max_value=8))
    node_ids = [f"node-{i:02d}" for i in range(n)]
    children: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for i in range(n):
        for j in range(i + 1, n):
            if draw(st.booleans()):
                children[node_ids[i]].append(node_ids[j])
    return [
        {
            "node_id": node_ids[i],
            "node_index": i,
            "technique_id": f"T{1000 + i}",
            "technique_name": f"Technique {i}",
            "tactic": f"tactic-{i}",
            "child_nodes": children[node_ids[i]],
            "parent_nodes": [],
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Estratégia Hypothesis — Modelagem Estrutural arbitrária (pode ser vazia). A
# curation a IGNORA; variamos o conteúdo para asserir que o carimbo de origin
# não depende dela.
# ---------------------------------------------------------------------------


_IDENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    min_size=1,
    max_size=12,
)


@st.composite
def _modelagem(draw) -> StructuralModeling:
    techniques = draw(
        st.lists(
            st.builds(
                TechniqueElement,
                technique_id=_IDENT,
                technique_name=st.none() | _IDENT,
                tactic=st.none() | _IDENT,
                node_id=st.none() | _IDENT,
            ),
            max_size=5,
        )
    )
    relationships = draw(
        st.lists(
            st.builds(
                RelationshipElement,
                source=_IDENT,
                target=_IDENT,
                source_technique=st.none() | _IDENT,
                target_technique=st.none() | _IDENT,
            ),
            max_size=5,
        )
    )
    return StructuralModeling(techniques=techniques, relationships=relationships)


# ===========================================================================
# Property 13 (Estágio 3) — para qualquer DAG curated e qualquer Modelagem:
#   (1) a curation carimba curadoria_humana em origin() e em cada ProducedDag;
#   (2) a curation satisfaz o Protocol DagGenerator estruturalmente;
#   (3) um stub gerado_ia também satisfaz o Protocol e produz origin gerado_ia,
#       with unchanged signature and artifact (extensible without redesign).
# ===========================================================================


# Feature: pipeline-seis-stages, Property 13: Origem explícita e extensível nodes
# Estágios 3, 4 e 5 (parte Estágio 3 / DAG).
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(structural_nodes=_acyclic_dag(), modeling=_modelagem())
def test_property13_stage3_origin_explicit_and_extensible(
    structural_nodes: list[dict],
    modeling: StructuralModeling,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A origin do DAG é explícita (curadoria_humana) e extensível (gerado_ia).

    Validates: Requirements 7.5, 16.1, 16.4
    """
    data_dir = tmp_path_factory.mktemp("origin_extensible_stage3")
    _write_case_fixture(data_dir, _CURATED_SLUG, structural_nodes)

    curation = CurationDagGenerator(case_service=CaseService(data_dir=data_dir))

    # (1) A curation carimba curadoria_humana — em origin() e em CADA
    #     ProducedDag devolvido, independentemente do DAG e da Modelagem
    #     (Req. 7.5, 16.1).
    assert curation.origin() == DagOrigin.HUMAN_CURATION
    produzidos = curation.generate(_CURATED_SLUG, modeling)
    assert len(produzidos) == 1  # DAG curated válido → exatamente um ProducedDag
    assert all(isinstance(p, ProducedDag) for p in produzidos)
    assert all(p.origin == DagOrigin.HUMAN_CURATION for p in produzidos)

    # (2) A curation satisfaz o Protocol @runtime_checkable estruturalmente,
    #     sem herança explícita (Req. 16.1).
    assert isinstance(curation, DagGenerator)

    # (3) Um stub gerado_ia também satisfaz o Protocol e produz origin gerado_ia
    #     — extensibility without redesign: same signature, same artifact
    #     ProducedDag, origin distinta (Req. 16.1, 16.4).
    stub = _StubGeradoIa()
    assert isinstance(stub, DagGenerator)
    assert stub.origin() == DagOrigin.AI_GENERATED
    produzidos_ia = stub.generate(_CURATED_SLUG, modeling)
    assert all(isinstance(p, ProducedDag) for p in produzidos_ia)
    assert all(p.origin == DagOrigin.AI_GENERATED for p in produzidos_ia)

    # A assinatura da costura é a MESMA para ambas as origens — a extensão não
    # redesigns ``generate`` (Req. 16.1): same parameters ``(campaign, modeling)``.
    assert list(inspect.signature(curation.generate).parameters) == ["campaign", "modeling"]
    assert list(inspect.signature(stub.generate).parameters) == ["campaign", "modeling"]

    # ``gerado_ia`` é um valor legítimo da MESMA enum de origin usada pelo DAG
    # (persistida como VARCHAR+CHECK que já o aceita) — persistir um DAG
    # gerado_ia no futuro não exige alteração de esquema (Req. 16.4).
    assert DagOrigin.AI_GENERATED in set(DagOrigin)
    assert DagOrigin.HUMAN_CURATION in set(DagOrigin)
