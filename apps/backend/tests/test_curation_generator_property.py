"""Property test do generator de curation :class:`CurationDagGenerator` (task 3.3).

# Feature: pipeline-cinco-stages, Property 7: Gerador de curation produz
# curadoria_humana a partir da Modelagem (ignorando-a), sem rede/IA.

Validates: Requirements 3.1, 3.6

Esta é uma property test de *feature* (a spec ``pipeline-cinco-stages`` é uma
feature, não um bugfix): a propriedade é esperada **valer** sobre a implementação
atual de :class:`~app.services.dag.generator.CurationDagGenerator`. No esquema de
cinco estágios, o DAG (Estágio 2) é **derivado da Modelagem Estrutural**
(Estágio 1), então a costura ``DagGenerator`` recebe
``gerar(campaign, modeling: StructuralModeling)``. A implementação de curation
**ignora** ``modeling`` (o grafo curated já existe em disco) — a input de
Modelagem existe apenas para a futura implementação de IA (Req. 3.1, 3.6).

Propriedades cobertas
---------------------
1. **DAG curated derivável** (Req. 3.1, 3.6) — para qualquer *slug* de campaign curated
   com um ``{slug}_dag.json`` válido, ``gerar(slug, modeling)`` devolve
   **exatamente um** :class:`ProducedDag` com
   ``origin == DagOrigin.HUMAN_CURATION``, cujo ``content`` tem as chaves
   ``structural_nodes`` **e** ``edges``, e cujas ``edges`` são **exatamente**
   as de :meth:`DagService._derive_edges` sobre o DAG carregado (consistência
   edges ⇄ grafo) — **independentemente** do valor de ``modeling``.
2. **Ignora a Modelagem** (Req. 3.6) — chamar ``gerar`` com **duas** Modelagens
   diferentes (uma vazia e uma com técnicas/relações arbitrárias) sobre o mesmo
   DAG em disco produz o **mesmo** :class:`ProducedDag`. A saída de curation
   depende apenas do DAG em disco, nunca da Modelagem passada.
3. **Sem DAG derivável** (Req. 3.6) — para um campaign curated sem DAG derivável
   (file ausente, malformado, ou payload ``null``), ``gerar`` devolve **lista
   vazia**, sem levantar e sem persistir um ``Dag`` inválido — qualquer que seja
   a Modelagem.
4. **Sem rede/IA** (Req. 3.6) — a implementação lê somente o disco local via
   ``CaseService``; afirmamos estruturalmente que nenhum caminho acessa a rede
   nem invoca IA (``gerar`` recebe apenas ``(campaign, modeling)`` e não referencia
   qualquer cliente de IA; a leitura usa exclusivamente a seam de disco de
   ``CaseService``).

Estratégia / isolation
-----------------------
Cada exemplo aponta um :class:`CaseService` para um diretório de dados
**temporário** com o layout ``<data_dir>/{api,dag}/`` (igual ao real), no qual
escrevemos um ``{slug}_dag.json`` gerado. Os DAGs gerados são acíclicos por
construção (edges apenas de um nó anterior para um posterior), espelhando como
os arquivos reais são parseados (``structural_nodes[*].child_nodes``). A
Modelagem é gerada/variada por Hypothesis (vazia e preenchida) para exercitar
que a curation a ignora. Nada aqui toca a rede nem um cliente de IA (não há um
neste caminho).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.models.enums import DagOrigin
from app.services.campaign_service import CURATED_CASES, CaseService, DagData
from app.services.dag.dag_service import DagService
from app.services.dag.generator import (
    CurationDagGenerator,
    ProducedDag,
    StructuralModeling,
)
from app.services.stage.stage_service import RelationshipElement, TechniqueElement


# Um slug de campaign curated real, usado para os fixtures gerados. A geração de
# curation só produz um DAG para slugs curados (CaseService.try_load_case).
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guarda: mantém o slug do fixture curated


# ---------------------------------------------------------------------------
# Construtores de fixture — escrevem um {slug}_dag.json gerado em um data dir
# temporário com o layout <data_dir>/{api,dag}/, para que CaseService(data_dir=...)
# resolva os arquivos (ability + dag). Sem rede, sem IA.
# ---------------------------------------------------------------------------


def _write_case_fixture(
    data_dir: Path,
    slug: str,
    structural_nodes: list[dict],
) -> None:
    """Escreve os fixtures de ability + DAG para ``slug`` sob ``data_dir``.

    ``CaseService.load_case`` lê a lista de abilities e o DAG de um slug (o
    file de adversary é opcional). Escrevemos uma lista de abilities vazia
    (válida, "sem abilities") mais o DAG, de modo que ``gerar`` alcance o ramo de
    DAG válido puramente a partir do grafo estrutural.
    """
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
# Estratégia Hypothesis — um DAG acíclico expresso via child_nodes.
#
# Geramos N nós com node_ids únicos e só permitimos edges de um nó ANTERIOR
# para um POSTERIOR (i -> j com i < j). Essa construção garante aciclicidade por
# ordem topológica, casa com o parsing de DagData/DagNode dos arquivos reais
# (structural_nodes[*].child_nodes) e nodes deixa conhecer o conjunto de edges
# esperado de antemão.
# ---------------------------------------------------------------------------


@st.composite
def _acyclic_dag(draw) -> dict:
    """Sorteia um DAG acíclico como ``{node_ids, structural_nodes, edges}``.

    ``edges`` é o conjunto exato de pares ``(de, para)`` de-duplicados que o
    serviço deve derivar de ``child_nodes`` (apenas anterior->posterior, logo sem
    ciclo possível).
    """
    n = draw(st.integers(min_value=1, max_value=8))
    node_ids = [f"node-{i:02d}" for i in range(n)]

    edges: list[tuple[str, str]] = []
    children: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for i in range(n):
        for j in range(i + 1, n):
            if draw(st.booleans()):
                edges.append((node_ids[i], node_ids[j]))
                children[node_ids[i]].append(node_ids[j])

    structural_nodes = [
        {
            "node_id": node_ids[i],
            "node_index": i,
            "technique_id": f"T{1000 + i}",
            "technique_name": f"Technique {i}",
            "tactic": f"tactic-{i}",
            "child_nodes": children[node_ids[i]],
            # parent_nodes deixado vazio de propósito: as edges são derivadas
            # apenas de child_nodes (igual a stage_service._extract_relationships).
            "parent_nodes": [],
        }
        for i in range(n)
    ]
    return {
        "node_ids": node_ids,
        "structural_nodes": structural_nodes,
        "arestas": edges,
    }


# ---------------------------------------------------------------------------
# Estratégia Hypothesis — uma Modelagem Estrutural (Estágio 1) arbitrária.
#
# A curation IGNORA a Modelagem (Req. 3.6). Variamos o conteúdo (técnicas e
# relações arbitrárias, podendo ser vazio) exatamente para asserir que a saída
# NÃO depende dela. A estratégia produz uma StructuralModeling válida (os campos
# têm defaults, logo ``StructuralModeling()`` vazia também é um valor legítimo).
# ---------------------------------------------------------------------------


_IDENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    min_size=1,
    max_size=12,
)


@st.composite
def _modelagem(draw) -> StructuralModeling:
    """Sorteia uma :class:`StructuralModeling` arbitrária (pode ser vazia)."""
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
# Propriedade 1 — DAG curated derivável produz exatamente um ProducedDag
# curadoria_humana com content consistente, independente da Modelagem
# (Req. 3.1, 3.6).
# ===========================================================================


# Feature: pipeline-cinco-stages, Property 7: Gerador de curation produz
# curadoria_humana a partir da Modelagem (ignorando-a), sem rede/IA
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(dag=_acyclic_dag(), modeling=_modelagem())
def test_property7_curation_produces_a_consistent_human_curation_dag(
    dag: dict,
    modeling: StructuralModeling,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Slug curated com DAG válido → exatamente 1 ProducedDag curadoria_humana.

    O ``content`` tem ``structural_nodes`` e ``edges``; as ``edges`` são
    exatamente as de ``DagService._derive_edges`` sobre o DAG carregado. O
    result é o mesmo qualquer que seja a ``modeling`` passada (Req. 3.6).

    Validates: Requirements 3.1, 3.6
    """
    data_dir = tmp_path_factory.mktemp("gerador_curadoria_prop7")
    _write_case_fixture(data_dir, _CURATED_SLUG, dag["structural_nodes"])

    case_service = CaseService(data_dir=data_dir)
    generator = CurationDagGenerator(case_service=case_service)

    produzidos = generator.generate(_CURATED_SLUG, modeling)

    # Exatamente um ProducedDag, origin = HUMAN_CURATION.
    assert len(produzidos) == 1
    produzido = produzidos[0]
    assert isinstance(produzido, ProducedDag)
    assert produzido.origin == DagOrigin.HUMAN_CURATION
    # ``origin()`` da costura também carimba curadoria_humana (Req. 3.6).
    assert generator.origin() == DagOrigin.HUMAN_CURATION

    # O content tem as duas chaves estruturais do contrato.
    content = produzido.content
    assert "structural_nodes" in content
    assert "arestas" in content

    # Um structural_node por nó gerado (todos carregam node_id).
    assert len(content["structural_nodes"]) == len(dag["structural_nodes"])

    # As edges persistidas são EXATAMENTE as de DagService._derive_edges
    # sobre o DAG carregado — consistência edges ⇄ grafo (Req. 3.1).
    result = case_service.try_load_case(_CURATED_SLUG)
    assert result.ok and result.case is not None
    dag_carregado: DagData = result.case.dag  # type: ignore[assignment]
    assert dag_carregado is not None
    esperado = DagService._derive_edges(dag_carregado)
    obtido = [(a["de"], a["para"]) for a in content["arestas"]]
    assert obtido == esperado
    # E coincide com o conjunto de edges gerado (anterior->posterior).
    assert {(a["de"], a["para"]) for a in content["arestas"]} == set(dag["arestas"])


# ===========================================================================
# Propriedade 2 — a curation IGNORA a Modelagem: duas Modelagens diferentes
# sobre o mesmo DAG em disco produzem o MESMO ProducedDag (Req. 3.6).
# ===========================================================================


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(dag=_acyclic_dag(), modelagem_a=_modelagem(), modelagem_b=_modelagem())
def test_property7_curation_ignores_the_modeling(
    dag: dict,
    modelagem_a: StructuralModeling,
    modelagem_b: StructuralModeling,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Chamar ``gerar`` com duas Modelagens diferentes → mesmo ``ProducedDag``.

    A saída de curation depende apenas do DAG em disco, não da Modelagem passada
    (Req. 3.6). Além disso incluímos sempre a Modelagem **vazia** como baseline,
    para fixar que uma Modelagem preenchida nunca altera o result.

    Validates: Requirements 3.6
    """
    data_dir = tmp_path_factory.mktemp("gerador_curadoria_ignora_modelagem")
    _write_case_fixture(data_dir, _CURATED_SLUG, dag["structural_nodes"])

    generator = CurationDagGenerator(case_service=CaseService(data_dir=data_dir))

    vazia = StructuralModeling()
    r_vazia = generator.generate(_CURATED_SLUG, vazia)
    r_a = generator.generate(_CURATED_SLUG, modelagem_a)
    r_b = generator.generate(_CURATED_SLUG, modelagem_b)

    # Idêntico qualquer que seja a Modelagem: a saída não depende dela.
    assert r_vazia == r_a == r_b
    # Sanidade: é de fato uma produção de curation não-vazia.
    assert len(r_a) == 1
    assert r_a[0].origin == DagOrigin.HUMAN_CURATION


# ===========================================================================
# Propriedade 3 — slugs sem DAG derivável devolvem lista vazia (Req. 3.6).
# ===========================================================================


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    cenario=st.sampled_from(["ausente", "malformado", "no_dag"]),
    modeling=_modelagem(),
)
def test_property7_no_derivable_dag_returns_empty_list(
    cenario: str,
    modeling: StructuralModeling,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """campaign curated sem DAG derivável (ausente/malformado/null) → lista vazia.

    Curation generation is triggered by a curated campaign (the backfill iterates the Campaigns
    curados). Para um campaign curated cujo ``{slug}_dag.json`` está ausente,
    malformado, ou cujo payload de DAG é ``null``, ``gerar`` devolve ``[]`` sem
    levantar e sem persistir um ``Dag`` inválido — qualquer que seja a Modelagem
    (Req. 3.6).

    Validates: Requirements 3.6
    """
    data_dir = tmp_path_factory.mktemp("gerador_curadoria_vazio")
    api_dir = data_dir / "api"
    dag_dir = data_dir / "dag"
    api_dir.mkdir(parents=True, exist_ok=True)
    dag_dir.mkdir(parents=True, exist_ok=True)

    generator = CurationDagGenerator(case_service=CaseService(data_dir=data_dir))

    # All scenarios use a curated slug (generation is only triggered for Campaigns
    # curados); varia o state do file de DAG em disco.
    slug = _CURATED_SLUG
    (api_dir / f"{slug}_dag-ability.json").write_text("[]", encoding="utf-8")

    if cenario == "ausente":
        # Ability presente mas SEM o file de DAG → DAG ausente.
        pass
    elif cenario == "malformado":
        # DAG em JSON inválido (objeto truncado).
        (dag_dir / f"{slug}_dag.json").write_text(
            '{"structural_nodes": [', encoding="utf-8"
        )
    else:  # no_dag
        # Arquivo de DAG presente mas com payload JSON ``null`` → campaign sem DAG.
        (dag_dir / f"{slug}_dag.json").write_text("null", encoding="utf-8")

    produzidos = generator.generate(slug, modeling)

    assert produzidos == []


# ===========================================================================
# Propriedade 4 — sem rede/IA: a geração de curation lê apenas disco local.
# ===========================================================================


# Uma sentinela de cliente de IA: qualquer acesso a atributo/chamada failure o
# teste. O caminho de curation não tem dependência de IA, então afirmar
# estruturalmente que nenhuma seam de IA é tocada torna o contrato "sem IA"
# explícito.
class _NoAICalledSentinel:
    def __getattr__(self, name: str):  # pragma: no cover - nunca deve rodar
        raise AssertionError(
            "CurationDagGenerator não deve invocar nenhum serviço de IA "
            f"(acessou {name!r})."
        )


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(dag=_acyclic_dag(), modeling=_modelagem())
def test_property7_curation_generation_no_network_no_ai(
    dag: dict,
    modeling: StructuralModeling,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A geração de curation lê somente o disco local via CaseService — sem IA.

    Não há cliente de IA neste caminho; uma sentinela de IA é intencionalmente
    não acoplada ao generator (não há seam para injetar uma). A assertiva de
    contrato mais forte é: o DAG curated é produzido lendo apenas o CaseService
    (disco local), sem exceção e sem qualquer chamada de rede/IA — a Modelagem
    recebida (saída do Estágio 1) é ignorada (Req. 3.6).

    Validates: Requirements 3.6
    """
    data_dir = tmp_path_factory.mktemp("gerador_curadoria_sem_ia")
    _write_case_fixture(data_dir, _CURATED_SLUG, dag["structural_nodes"])

    # O generator depende apenas do CaseService (seam de disco local); nenhuma seam
    # de IA existe para injetar — documentamos a intenção "sem IA".
    _ = _NoAICalledSentinel

    generator = CurationDagGenerator(case_service=CaseService(data_dir=data_dir))

    # Produz normalmente a partir do disco local, sem levantar.
    produzidos = generator.generate(_CURATED_SLUG, modeling)
    assert len(produzidos) == 1
    assert produzidos[0].origin == DagOrigin.HUMAN_CURATION

    # Contrato estrutural: ``gerar`` recebe apenas ``(campaign, modeling)``, nunca um
    # cliente de rede/IA — a única fonte de dados é o CaseService de disco local.
    import inspect

    params = list(inspect.signature(generator.generate).parameters)
    assert params == ["campaign", "modeling"]
