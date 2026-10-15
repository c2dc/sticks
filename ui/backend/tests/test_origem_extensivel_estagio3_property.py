"""Property test da origem explícita e extensível — Estágio 3 / DAG (task 4.4).

# Feature: pipeline-seis-estagios, Property 13: Origem explícita e extensível nos
# Estágios 3, 4 e 5. Parte Estágio 3 (Geração do DAG): a implementação de
# curadoria ``CuradoriaGeradorDeDag`` sempre carimba ``curadoria_humana``; a
# costura ``GeradorDeDag`` é satisfeita estruturalmente (``@runtime_checkable``
# Protocol); e um stub ``gerado_ia`` também satisfaz a costura — provando que a
# origem ``gerado_ia`` encaixa sem redesenho (sem alterar a assinatura nem o
# artefato DAG).

Validates: Requirements 7.5, 16.1, 16.4

Esta é uma property test de *feature* (``pipeline-seis-estagios`` é uma feature,
não um bugfix): a propriedade é esperada **valer** sobre a implementação atual.
Foca a parte **Estágio 3** da Property 13 — a costura de geração de DAG
(:class:`~app.services.dag.gerador.GeradorDeDag` /
:class:`~app.services.dag.gerador.CuradoriaGeradorDeDag`). As partes dos
Estágios 4 e 5 são cobertas pelas tasks 5.5 e 7.5.

A Property 13 (lado Estágio 3) afirma, para qualquer entrada:

1. **Carimbo de curadoria** (Req. 7.5, 16.1) — ``CuradoriaGeradorDeDag.origem()``
   devolve exatamente ``OrigemDag.CURADORIA_HUMANA`` e **todo** ``DagProduzido``
   que ela devolve carrega ``origem == OrigemDag.CURADORIA_HUMANA`` — qualquer
   que seja o DAG curado em disco e qualquer que seja a Modelagem passada
   (ignorada pela curadoria).
2. **Conformidade estrutural do Protocol** (Req. 16.1) — a instância de
   curadoria satisfaz o ``@runtime_checkable`` ``GeradorDeDag`` por *duck typing*
   estrutural (``isinstance`` é ``True``), sem herança explícita.
3. **Extensibilidade sem redesenho** (Req. 16.1, 16.4) — um stub ``gerado_ia``
   que apenas expõe ``origem()`` + ``gerar(caso, modelagem)`` (sem herdar de
   ``GeradorDeDag``) também satisfaz o Protocol e produz ``DagProduzido`` com
   ``origem == OrigemDag.GERADO_IA`` **sem** alterar a assinatura da costura nem
   o artefato ``DagProduzido`` — exatamente a extensão por origem exigida por
   Req. 16.1/16.4. O valor ``gerado_ia`` já pertence à enum ``OrigemDag`` (cujo
   persistido é VARCHAR+CHECK que o aceita), logo persistir um DAG ``gerado_ia``
   no futuro não exige alteração de esquema (Req. 16.4).

Estratégia / isolamento
-----------------------
Cada exemplo escreve um ``{slug}_dag.json`` curado (acíclico por construção) em
um diretório de dados **temporário** com o layout ``<data_dir>/{api,dag}/`` e
aponta um :class:`~app.services.case_service.CaseService` para ele, exatamente
como os arquivos reais são parseados — SQLite efêmero não é necessário aqui pois
a costura de geração de DAG lê apenas o disco local via ``CaseService`` (nenhum
estado de banco é envolvido na parte Estágio 3). A Modelagem é variada por
Hypothesis para exercitar que a curadoria a ignora. Hypothesis roda ≥100
iterações. Nada aqui toca a rede nem invoca IA.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.models.enums import OrigemDag
from app.services.case_service import CURATED_CASES, CaseService
from app.services.dag.gerador import (
    CuradoriaGeradorDeDag,
    DagProduzido,
    GeradorDeDag,
    ModelagemEstrutural,
)
from app.services.stage.stage_service import RelationshipElement, TechniqueElement


# Um slug de caso curado real: a geração de curadoria só produz um DAG para
# slugs curados (``CaseService.try_load_case``).
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guarda: mantém o slug do fixture curado


# ---------------------------------------------------------------------------
# Stub ``gerado_ia`` — satisfaz ``GeradorDeDag`` sem herança explícita. Modela a
# origem futura (IA) que RECEBE a Modelagem e encaixa na costura por duck typing
# estrutural, carimbando ``OrigemDag.GERADO_IA`` (Req. 16.1, 16.4). A spec NÃO
# implementa o agente de IA (Req. 16.5); este stub existe apenas para provar a
# extensibilidade da costura.
# ---------------------------------------------------------------------------


class _StubGeradoIa:
    """Stub ``gerado_ia`` estrutural: ``origem()`` + ``gerar(caso, modelagem)``."""

    def origem(self) -> OrigemDag:
        return OrigemDag.GERADO_IA

    def gerar(
        self, caso: str, modelagem: ModelagemEstrutural
    ) -> list[DagProduzido]:
        # Uma implementação real de IA derivaria o grafo de ``modelagem``; o stub
        # apenas honra a assinatura de duas vias e carimba gerado_ia.
        _ = modelagem
        return [
            DagProduzido(
                origem=OrigemDag.GERADO_IA,
                identificador=caso,
                rotulo=None,
                conteudo={"structural_nodes": [], "arestas": []},
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
# Estratégia Hypothesis — DAG curado acíclico (arestas só anterior->posterior).
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
# curadoria a IGNORA; variamos o conteúdo para asserir que o carimbo de origem
# não depende dela.
# ---------------------------------------------------------------------------


_IDENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    min_size=1,
    max_size=12,
)


@st.composite
def _modelagem(draw) -> ModelagemEstrutural:
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
    return ModelagemEstrutural(techniques=techniques, relationships=relationships)


# ===========================================================================
# Property 13 (Estágio 3) — para qualquer DAG curado e qualquer Modelagem:
#   (1) a curadoria carimba curadoria_humana em origem() e em cada DagProduzido;
#   (2) a curadoria satisfaz o Protocol GeradorDeDag estruturalmente;
#   (3) um stub gerado_ia também satisfaz o Protocol e produz origem gerado_ia,
#       com assinatura e artefato inalterados (extensível sem redesenho).
# ===========================================================================


# Feature: pipeline-seis-estagios, Property 13: Origem explícita e extensível nos
# Estágios 3, 4 e 5 (parte Estágio 3 / DAG).
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(structural_nodes=_acyclic_dag(), modelagem=_modelagem())
def test_property13_estagio3_origem_explicita_e_extensivel(
    structural_nodes: list[dict],
    modelagem: ModelagemEstrutural,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A origem do DAG é explícita (curadoria_humana) e extensível (gerado_ia).

    Validates: Requirements 7.5, 16.1, 16.4
    """
    data_dir = tmp_path_factory.mktemp("origem_extensivel_estagio3")
    _write_case_fixture(data_dir, _CURATED_SLUG, structural_nodes)

    curadoria = CuradoriaGeradorDeDag(case_service=CaseService(data_dir=data_dir))

    # (1) A curadoria carimba curadoria_humana — em origem() e em CADA
    #     DagProduzido devolvido, independentemente do DAG e da Modelagem
    #     (Req. 7.5, 16.1).
    assert curadoria.origem() == OrigemDag.CURADORIA_HUMANA
    produzidos = curadoria.gerar(_CURATED_SLUG, modelagem)
    assert len(produzidos) == 1  # DAG curado válido → exatamente um DagProduzido
    assert all(isinstance(p, DagProduzido) for p in produzidos)
    assert all(p.origem == OrigemDag.CURADORIA_HUMANA for p in produzidos)

    # (2) A curadoria satisfaz o Protocol @runtime_checkable estruturalmente,
    #     sem herança explícita (Req. 16.1).
    assert isinstance(curadoria, GeradorDeDag)

    # (3) Um stub gerado_ia também satisfaz o Protocol e produz origem gerado_ia
    #     — extensibilidade sem redesenho: mesma assinatura, mesmo artefato
    #     DagProduzido, origem distinta (Req. 16.1, 16.4).
    stub = _StubGeradoIa()
    assert isinstance(stub, GeradorDeDag)
    assert stub.origem() == OrigemDag.GERADO_IA
    produzidos_ia = stub.gerar(_CURATED_SLUG, modelagem)
    assert all(isinstance(p, DagProduzido) for p in produzidos_ia)
    assert all(p.origem == OrigemDag.GERADO_IA for p in produzidos_ia)

    # A assinatura da costura é a MESMA para ambas as origens — a extensão não
    # redesenha ``gerar`` (Req. 16.1): mesmos parâmetros ``(caso, modelagem)``.
    assert list(inspect.signature(curadoria.gerar).parameters) == ["caso", "modelagem"]
    assert list(inspect.signature(stub.gerar).parameters) == ["caso", "modelagem"]

    # ``gerado_ia`` é um valor legítimo da MESMA enum de origem usada pelo DAG
    # (persistida como VARCHAR+CHECK que já o aceita) — persistir um DAG
    # gerado_ia no futuro não exige alteração de esquema (Req. 16.4).
    assert OrigemDag.GERADO_IA in set(OrigemDag)
    assert OrigemDag.CURADORIA_HUMANA in set(OrigemDag)
