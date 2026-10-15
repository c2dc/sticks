"""Property test do backfill idempotente de DAGs curados (task 5.4).

# Feature: dag-estagio-multi-origem, Property 11: Backfill de DAGs curados é
# idempotente.

Validates: Requirements 4.5, 5.1

Esta é uma property test de *feature* (a spec ``dag-estagio-multi-origem`` é uma
feature, não um bugfix): a propriedade é esperada **valer** sobre a implementação
atual de
:meth:`~app.services.dag.dag_service.DagService.sincronizar_dags_curados`.

Propriedade (design.md — Property 11)
-------------------------------------
*Para todo* Caso curado com um DAG derivável e *para todo* número de execuções
``N ≥ 1``, aplicar ``sincronizar_dags_curados(caso)`` repetidamente produz o
**mesmo conjunto persistido** de DAGs — exatamente um ``Dag``
``origem = curadoria_humana`` por Caso, reconciliado pela chave
``(caso_id, origem, identificador)``, **sem duplicação** em execuções
subsequentes. A primeira chamada insere; as seguintes são no-ops.

Concretamente, para um DAG curado arbitrário escrito em um data dir temporário e
um ``N`` arbitrário (``st.integers(1, 6)``):

1. O conjunto persistido após ``N`` chamadas é **igual** ao conjunto após 1
   chamada: mesma contagem (sempre 1), mesmas tuplas
   ``(caso_id, origem, identificador)``, sem duplicatas.
2. Os ``id`` das linhas ``Dag`` são **estáveis** entre as chamadas (a primeira
   insere, as subsequentes não tocam a linha) — prova mais forte de "no-op".
3. ``listar_dags(caso)`` devolve uma listagem **estável** entre as chamadas.

Estratégia / isolamento
-----------------------
Cada exemplo aponta um :class:`CaseService` para um diretório de dados
**temporário** com o layout ``<data_dir>/{api,dag}/`` (igual ao real), no qual
escrevemos um ``{slug}_dag.json`` gerado, e constrói um ``DagService`` com uma
``Session`` SQLite **efêmera** (engine em arquivo temporário com o esquema
completo via ``Base.metadata.create_all``, igual às fixtures de
``test_dag_models.py``). Os DAGs gerados são acíclicos por construção (arestas
apenas de um nó anterior para um posterior), espelhando como os arquivos reais
são parseados (``structural_nodes[*].child_nodes``). Nada aqui toca a rede nem um
cliente de IA (não há um neste caminho).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Dag as DagModel
from app.models.enums import OrigemDag
from app.services.case_service import CURATED_CASES, CaseService
from app.services.dag.dag_service import DagService
from app.services.dag.gerador import CuradoriaGeradorDeDag


# Um slug de caso curado real: a geração de curadoria só produz um DAG para
# slugs curados (``CaseService.try_load_case``).
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guarda: mantém o slug do fixture curado


# ---------------------------------------------------------------------------
# Fixture de banco efêmero (igual a test_dag_models.py): um engine SQLite em
# arquivo temporário com o esquema completo via Base.metadata.create_all, para
# que a persistência do DagService rode contra um banco real descartável.
# ---------------------------------------------------------------------------


def _make_session_factory() -> tuple[sessionmaker[Session], Path, str]:
    """Cria um engine SQLite efêmero e devolve ``(factory, db_path, tmp_dir)``.

    O arquivo (não ``:memory:``) é usado para que ``create_all`` rode contra um
    engine de arquivo real e o ``CHECK`` portável (``native_enum=False``) de
    ``origem`` espelhe o produto, igual a ``test_dag_models.py``.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_backfill_prop_")
    db_path = Path(tmp_dir) / "backfill.db"
    settings = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings)
    import_models()
    Base.metadata.create_all(bind=engine)

    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    # O engine é mantido vivo pela factory (bind); o teardown fica a cargo do
    # chamador via o diretório temporário retornado.
    return factory, db_path, tmp_dir


# ---------------------------------------------------------------------------
# Construtor de fixture — escreve um {slug}_dag.json gerado num data dir
# temporário com o layout <data_dir>/{api,dag}/, igual ao real. Sem rede, sem IA.
# ---------------------------------------------------------------------------


def _write_case_fixture(
    data_dir: Path,
    slug: str,
    structural_nodes: list[dict],
) -> None:
    """Escreve os fixtures de ability + DAG para ``slug`` sob ``data_dir``.

    Lista de abilities vazia (válida) mais o DAG, de modo que o gerador alcance o
    ramo de DAG válido puramente a partir do grafo estrutural.
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
# Estratégia Hypothesis — um DAG acíclico expresso via child_nodes (apenas
# anterior->posterior, logo acíclico por ordem topológica), igual à estratégia
# de test_gerador_curadoria_property.py.
# ---------------------------------------------------------------------------


@st.composite
def _acyclic_dag(draw) -> list[dict]:
    """Sorteia os ``structural_nodes`` de um DAG acíclico."""
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


def _persisted_tuples(session: Session, caso: str) -> list[tuple]:
    """Conjunto persistido como tuplas ``(id, caso_id, origem, identificador)``.

    Ordenado por ``id`` para uma comparação estável. Lê diretamente da tabela
    ``dags`` (ORM) para observar o estado persistido sem depender da projeção de
    ``listar_dags``.
    """
    linhas = (
        session.execute(
            select(DagModel).where(DagModel.caso_id == caso).order_by(DagModel.id.asc())
        )
        .scalars()
        .all()
    )
    return [
        (linha.id, linha.caso_id, linha.origem, linha.identificador)
        for linha in linhas
    ]


# ===========================================================================
# Propriedade 11 — backfill idempotente: N chamadas == 1 chamada.
# ===========================================================================


# Feature: dag-estagio-multi-origem, Property 11: Backfill de DAGs curados é
# idempotente
@settings(
    max_examples=100,
    # Cada exemplo faz I/O de disco (SQLite efêmero + fixtures de arquivo), cujo
    # custo varia com o ambiente; o ``deadline`` padrão (200ms) é por isso uma
    # fonte de flakiness de *timing* — não de correção. A propriedade continua
    # a ser exercida em todos os exemplos; só desativamos o relógio por exemplo.
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(structural_nodes=_acyclic_dag(), n=st.integers(min_value=1, max_value=6))
def test_property11_backfill_e_idempotente(
    structural_nodes: list[dict], n: int
) -> None:
    """``sincronizar_dags_curados`` N vezes == 1 vez: mesmo conjunto, sem duplicar.

    Para um DAG curado arbitrário e ``N`` arbitrário (1..6), o conjunto
    persistido após ``N`` chamadas é idêntico ao conjunto após a 1ª chamada:
    exatamente 1 ``Dag`` ``curadoria_humana`` por Caso, mesmas tuplas
    ``(caso_id, origem, identificador)``, mesmos ``id`` (1ª insere, as demais são
    no-op), e ``listar_dags`` estável.

    Validates: Requirements 4.5, 5.1
    """
    data_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_backfill_data_"))
    factory, db_path, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        _write_case_fixture(data_dir, _CURATED_SLUG, structural_nodes)

        case_service = CaseService(data_dir=data_dir)
        service = DagService(
            session=session,
            case_service=case_service,
            gerador=CuradoriaGeradorDeDag(case_service),
        )

        # 1ª chamada: insere exatamente um Dag curadoria_humana (Req. 4.5, 5.1).
        listagem_1 = service.sincronizar_dags_curados(_CURATED_SLUG)
        tuplas_1 = _persisted_tuples(session, _CURATED_SLUG)

        # Exatamente um DAG curadoria_humana após a 1ª chamada.
        assert len(tuplas_1) == 1
        _, caso_id_1, origem_1, identificador_1 = tuplas_1[0]
        assert origem_1 == OrigemDag.CURADORIA_HUMANA
        assert caso_id_1 == _CURATED_SLUG
        assert identificador_1 == _CURATED_SLUG  # curadoria usa identificador=slug

        # Chamadas 2..N são no-ops reconciliadas por (caso_id, origem, identificador).
        listagem_n = listagem_1
        for _ in range(n - 1):
            listagem_n = service.sincronizar_dags_curados(_CURATED_SLUG)

        tuplas_n = _persisted_tuples(session, _CURATED_SLUG)

        # Conjunto persistido após N chamadas == após 1 chamada: mesma contagem,
        # mesmas tuplas, MESMOS ids (prova de no-op, não re-inserção). Sem duplicar.
        assert tuplas_n == tuplas_1
        assert len(tuplas_n) == 1

        # A chave (caso_id, origem, identificador) permanece única — sem duplicatas.
        chaves = [(c, o, ident) for (_id, c, o, ident) in tuplas_n]
        assert len(chaves) == len(set(chaves))

        # listar_dags estável entre a 1ª e a N-ésima chamada.
        resumos_1 = [
            (r.id, r.caso_id, r.origem, r.identificador) for r in listagem_1
        ]
        resumos_n = [
            (r.id, r.caso_id, r.origem, r.identificador) for r in listagem_n
        ]
        assert resumos_n == resumos_1
        # A listagem casa com o estado persistido observado via ORM.
        assert resumos_n == [
            (_id, c, o, ident) for (_id, c, o, ident) in tuplas_n
        ]
    finally:
        session.close()
        factory.kw["bind"].dispose()
        # Limpa o data dir e o banco temporário.
        for base in (data_dir, Path(db_tmp_dir)):
            for root, _dirs, files in os.walk(base, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                os.rmdir(root)
