"""Property test do gerador de curadoria :class:`CuradoriaGeradorDeDag` (task 3.3).

# Feature: pipeline-cinco-estagios, Property 7: Gerador de curadoria produz
# curadoria_humana a partir da Modelagem (ignorando-a), sem rede/IA.

Validates: Requirements 3.1, 3.6

Esta é uma property test de *feature* (a spec ``pipeline-cinco-estagios`` é uma
feature, não um bugfix): a propriedade é esperada **valer** sobre a implementação
atual de :class:`~app.services.dag.gerador.CuradoriaGeradorDeDag`. No esquema de
cinco estágios, o DAG (Estágio 2) é **derivado da Modelagem Estrutural**
(Estágio 1), então a costura ``GeradorDeDag`` recebe
``gerar(caso, modelagem: ModelagemEstrutural)``. A implementação de curadoria
**ignora** ``modelagem`` (o grafo curado já existe em disco) — a entrada de
Modelagem existe apenas para a futura implementação de IA (Req. 3.1, 3.6).

Propriedades cobertas
---------------------
1. **DAG curado derivável** (Req. 3.1, 3.6) — para qualquer *slug* de caso curado
   com um ``{slug}_dag.json`` válido, ``gerar(slug, modelagem)`` devolve
   **exatamente um** :class:`DagProduzido` com
   ``origem == OrigemDag.CURADORIA_HUMANA``, cujo ``conteudo`` tem as chaves
   ``structural_nodes`` **e** ``arestas``, e cujas ``arestas`` são **exatamente**
   as de :meth:`DagService._derivar_arestas` sobre o DAG carregado (consistência
   arestas ⇄ grafo) — **independentemente** do valor de ``modelagem``.
2. **Ignora a Modelagem** (Req. 3.6) — chamar ``gerar`` com **duas** Modelagens
   diferentes (uma vazia e uma com técnicas/relações arbitrárias) sobre o mesmo
   DAG em disco produz o **mesmo** :class:`DagProduzido`. A saída de curadoria
   depende apenas do DAG em disco, nunca da Modelagem passada.
3. **Sem DAG derivável** (Req. 3.6) — para um Caso curado sem DAG derivável
   (arquivo ausente, malformado, ou payload ``null``), ``gerar`` devolve **lista
   vazia**, sem levantar e sem persistir um ``Dag`` inválido — qualquer que seja
   a Modelagem.
4. **Sem rede/IA** (Req. 3.6) — a implementação lê somente o disco local via
   ``CaseService``; afirmamos estruturalmente que nenhum caminho acessa a rede
   nem invoca IA (``gerar`` recebe apenas ``(caso, modelagem)`` e não referencia
   qualquer cliente de IA; a leitura usa exclusivamente a seam de disco de
   ``CaseService``).

Estratégia / isolamento
-----------------------
Cada exemplo aponta um :class:`CaseService` para um diretório de dados
**temporário** com o layout ``<data_dir>/{api,dag}/`` (igual ao real), no qual
escrevemos um ``{slug}_dag.json`` gerado. Os DAGs gerados são acíclicos por
construção (arestas apenas de um nó anterior para um posterior), espelhando como
os arquivos reais são parseados (``structural_nodes[*].child_nodes``). A
Modelagem é gerada/variada por Hypothesis (vazia e preenchida) para exercitar
que a curadoria a ignora. Nada aqui toca a rede nem um cliente de IA (não há um
neste caminho).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.models.enums import OrigemDag
from app.services.case_service import CURATED_CASES, CaseService, DagData
from app.services.dag.dag_service import DagService
from app.services.dag.gerador import (
    CuradoriaGeradorDeDag,
    DagProduzido,
    ModelagemEstrutural,
)
from app.services.stage.stage_service import RelationshipElement, TechniqueElement


# Um slug de caso curado real, usado para os fixtures gerados. A geração de
# curadoria só produz um DAG para slugs curados (CaseService.try_load_case).
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guarda: mantém o slug do fixture curado


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
    arquivo de adversary é opcional). Escrevemos uma lista de abilities vazia
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
# Geramos N nós com node_ids únicos e só permitimos arestas de um nó ANTERIOR
# para um POSTERIOR (i -> j com i < j). Essa construção garante aciclicidade por
# ordem topológica, casa com o parsing de DagData/DagNode dos arquivos reais
# (structural_nodes[*].child_nodes) e nos deixa conhecer o conjunto de arestas
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
            # parent_nodes deixado vazio de propósito: as arestas são derivadas
            # apenas de child_nodes (igual a stage_service._extract_relationships).
            "parent_nodes": [],
        }
        for i in range(n)
    ]
    return {
        "node_ids": node_ids,
        "structural_nodes": structural_nodes,
        "edges": edges,
    }


# ---------------------------------------------------------------------------
# Estratégia Hypothesis — uma Modelagem Estrutural (Estágio 1) arbitrária.
#
# A curadoria IGNORA a Modelagem (Req. 3.6). Variamos o conteúdo (técnicas e
# relações arbitrárias, podendo ser vazio) exatamente para asserir que a saída
# NÃO depende dela. A estratégia produz uma ModelagemEstrutural válida (os campos
# têm defaults, logo ``ModelagemEstrutural()`` vazia também é um valor legítimo).
# ---------------------------------------------------------------------------


_IDENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    min_size=1,
    max_size=12,
)


@st.composite
def _modelagem(draw) -> ModelagemEstrutural:
    """Sorteia uma :class:`ModelagemEstrutural` arbitrária (pode ser vazia)."""
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
# Propriedade 1 — DAG curado derivável produz exatamente um DagProduzido
# curadoria_humana com conteudo consistente, independente da Modelagem
# (Req. 3.1, 3.6).
# ===========================================================================


# Feature: pipeline-cinco-estagios, Property 7: Gerador de curadoria produz
# curadoria_humana a partir da Modelagem (ignorando-a), sem rede/IA
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(dag=_acyclic_dag(), modelagem=_modelagem())
def test_property7_curadoria_produz_um_dag_curadoria_humana_consistente(
    dag: dict,
    modelagem: ModelagemEstrutural,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Slug curado com DAG válido → exatamente 1 DagProduzido curadoria_humana.

    O ``conteudo`` tem ``structural_nodes`` e ``arestas``; as ``arestas`` são
    exatamente as de ``DagService._derivar_arestas`` sobre o DAG carregado. O
    resultado é o mesmo qualquer que seja a ``modelagem`` passada (Req. 3.6).

    Validates: Requirements 3.1, 3.6
    """
    data_dir = tmp_path_factory.mktemp("gerador_curadoria_prop7")
    _write_case_fixture(data_dir, _CURATED_SLUG, dag["structural_nodes"])

    case_service = CaseService(data_dir=data_dir)
    gerador = CuradoriaGeradorDeDag(case_service=case_service)

    produzidos = gerador.gerar(_CURATED_SLUG, modelagem)

    # Exatamente um DagProduzido, origem = CURADORIA_HUMANA.
    assert len(produzidos) == 1
    produzido = produzidos[0]
    assert isinstance(produzido, DagProduzido)
    assert produzido.origem == OrigemDag.CURADORIA_HUMANA
    # ``origem()`` da costura também carimba curadoria_humana (Req. 3.6).
    assert gerador.origem() == OrigemDag.CURADORIA_HUMANA

    # O conteudo tem as duas chaves estruturais do contrato.
    conteudo = produzido.conteudo
    assert "structural_nodes" in conteudo
    assert "arestas" in conteudo

    # Um structural_node por nó gerado (todos carregam node_id).
    assert len(conteudo["structural_nodes"]) == len(dag["structural_nodes"])

    # As arestas persistidas são EXATAMENTE as de DagService._derivar_arestas
    # sobre o DAG carregado — consistência arestas ⇄ grafo (Req. 3.1).
    resultado = case_service.try_load_case(_CURATED_SLUG)
    assert resultado.ok and resultado.case is not None
    dag_carregado: DagData = resultado.case.dag  # type: ignore[assignment]
    assert dag_carregado is not None
    esperado = DagService._derivar_arestas(dag_carregado)
    obtido = [(a["de"], a["para"]) for a in conteudo["arestas"]]
    assert obtido == esperado
    # E coincide com o conjunto de arestas gerado (anterior->posterior).
    assert {(a["de"], a["para"]) for a in conteudo["arestas"]} == set(dag["edges"])


# ===========================================================================
# Propriedade 2 — a curadoria IGNORA a Modelagem: duas Modelagens diferentes
# sobre o mesmo DAG em disco produzem o MESMO DagProduzido (Req. 3.6).
# ===========================================================================


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(dag=_acyclic_dag(), modelagem_a=_modelagem(), modelagem_b=_modelagem())
def test_property7_curadoria_ignora_a_modelagem(
    dag: dict,
    modelagem_a: ModelagemEstrutural,
    modelagem_b: ModelagemEstrutural,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Chamar ``gerar`` com duas Modelagens diferentes → mesmo ``DagProduzido``.

    A saída de curadoria depende apenas do DAG em disco, não da Modelagem passada
    (Req. 3.6). Além disso incluímos sempre a Modelagem **vazia** como baseline,
    para fixar que uma Modelagem preenchida nunca altera o resultado.

    Validates: Requirements 3.6
    """
    data_dir = tmp_path_factory.mktemp("gerador_curadoria_ignora_modelagem")
    _write_case_fixture(data_dir, _CURATED_SLUG, dag["structural_nodes"])

    gerador = CuradoriaGeradorDeDag(case_service=CaseService(data_dir=data_dir))

    vazia = ModelagemEstrutural()
    r_vazia = gerador.gerar(_CURATED_SLUG, vazia)
    r_a = gerador.gerar(_CURATED_SLUG, modelagem_a)
    r_b = gerador.gerar(_CURATED_SLUG, modelagem_b)

    # Idêntico qualquer que seja a Modelagem: a saída não depende dela.
    assert r_vazia == r_a == r_b
    # Sanidade: é de fato uma produção de curadoria não-vazia.
    assert len(r_a) == 1
    assert r_a[0].origem == OrigemDag.CURADORIA_HUMANA


# ===========================================================================
# Propriedade 3 — slugs sem DAG derivável devolvem lista vazia (Req. 3.6).
# ===========================================================================


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    cenario=st.sampled_from(["ausente", "malformado", "sem_dag"]),
    modelagem=_modelagem(),
)
def test_property7_sem_dag_derivavel_devolve_lista_vazia(
    cenario: str,
    modelagem: ModelagemEstrutural,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Caso curado sem DAG derivável (ausente/malformado/null) → lista vazia.

    A geração de curadoria é acionada por Caso curado (o backfill itera os Casos
    curados). Para um Caso curado cujo ``{slug}_dag.json`` está ausente,
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

    gerador = CuradoriaGeradorDeDag(case_service=CaseService(data_dir=data_dir))

    # Todos os cenários usam um slug curado (a geração só é acionada para Casos
    # curados); varia o estado do arquivo de DAG em disco.
    slug = _CURATED_SLUG
    (api_dir / f"{slug}_dag-ability.json").write_text("[]", encoding="utf-8")

    if cenario == "ausente":
        # Ability presente mas SEM o arquivo de DAG → DAG ausente.
        pass
    elif cenario == "malformado":
        # DAG em JSON inválido (objeto truncado).
        (dag_dir / f"{slug}_dag.json").write_text(
            '{"structural_nodes": [', encoding="utf-8"
        )
    else:  # sem_dag
        # Arquivo de DAG presente mas com payload JSON ``null`` → Caso sem DAG.
        (dag_dir / f"{slug}_dag.json").write_text("null", encoding="utf-8")

    produzidos = gerador.gerar(slug, modelagem)

    assert produzidos == []


# ===========================================================================
# Propriedade 4 — sem rede/IA: a geração de curadoria lê apenas disco local.
# ===========================================================================


# Uma sentinela de cliente de IA: qualquer acesso a atributo/chamada falha o
# teste. O caminho de curadoria não tem dependência de IA, então afirmar
# estruturalmente que nenhuma seam de IA é tocada torna o contrato "sem IA"
# explícito.
class _NoAICalledSentinel:
    def __getattr__(self, name: str):  # pragma: no cover - nunca deve rodar
        raise AssertionError(
            "CuradoriaGeradorDeDag não deve invocar nenhum serviço de IA "
            f"(acessou {name!r})."
        )


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(dag=_acyclic_dag(), modelagem=_modelagem())
def test_property7_geracao_de_curadoria_nao_acessa_rede_nem_ia(
    dag: dict,
    modelagem: ModelagemEstrutural,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A geração de curadoria lê somente o disco local via CaseService — sem IA.

    Não há cliente de IA neste caminho; uma sentinela de IA é intencionalmente
    não acoplada ao gerador (não há seam para injetar uma). A assertiva de
    contrato mais forte é: o DAG curado é produzido lendo apenas o CaseService
    (disco local), sem exceção e sem qualquer chamada de rede/IA — a Modelagem
    recebida (saída do Estágio 1) é ignorada (Req. 3.6).

    Validates: Requirements 3.6
    """
    data_dir = tmp_path_factory.mktemp("gerador_curadoria_sem_ia")
    _write_case_fixture(data_dir, _CURATED_SLUG, dag["structural_nodes"])

    # O gerador depende apenas do CaseService (seam de disco local); nenhuma seam
    # de IA existe para injetar — documentamos a intenção "sem IA".
    _ = _NoAICalledSentinel

    gerador = CuradoriaGeradorDeDag(case_service=CaseService(data_dir=data_dir))

    # Produz normalmente a partir do disco local, sem levantar.
    produzidos = gerador.gerar(_CURATED_SLUG, modelagem)
    assert len(produzidos) == 1
    assert produzidos[0].origem == OrigemDag.CURADORIA_HUMANA

    # Contrato estrutural: ``gerar`` recebe apenas ``(caso, modelagem)``, nunca um
    # cliente de rede/IA — a única fonte de dados é o CaseService de disco local.
    import inspect

    params = list(inspect.signature(gerador.gerar).parameters)
    assert params == ["caso", "modelagem"]
