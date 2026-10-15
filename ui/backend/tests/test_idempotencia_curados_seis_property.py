"""Property test da idempotência da leitura curada do DAG — Estágio 3 (task 4.3).

# Feature: pipeline-seis-estagios, Property 5: Idempotência da leitura dos
# artefatos curados. Para qualquer campanha curada, ler os artefatos curados do
# Estágio 3 (DAG) duas ou mais vezes produz resultados equivalentes, sem rede e
# sem IA, e o backfill de persistência não duplica linhas.

Validates: Requirements 7.4, 11.1, 11.3, 11.4

Contexto (design.md — Property 5)
---------------------------------
No pipeline de **seis** Estágios, o Estágio 3 "Geração do DAG" de uma campanha
curada lê o DAG curado **diretamente de disco** (``data/dag/{slug}_dag.json`` via
:class:`~app.services.case_service.CaseService`), **sem rede e sem IA**
(Req. 7.4, 11.1, 11.3, 11.4). A costura
:class:`~app.services.dag.gerador.CuradoriaGeradorDeDag` carimba
``origem = curadoria_humana`` e
:meth:`~app.services.dag.dag_service.DagService.sincronizar_dags_curados`
persiste com **backfill idempotente**, reconciliando pela chave
``(caso_id, origem, identificador)``.

Propriedade
-----------
*Para todo* DAG curado derivável e *para todo* número de leituras/backfills
``N ≥ 1``:

1. **Leitura pura equivalente (sem rede/IA):** chamar
   ``CuradoriaGeradorDeDag.gerar`` ``N`` vezes produz exatamente o mesmo
   ``DagProduzido`` (mesma ``origem``, ``identificador``, ``rotulo`` e
   ``conteudo``) a cada leitura — a leitura curada do Estágio 3 é idempotente.
2. **Backfill não duplica:** aplicar ``sincronizar_dags_curados(caso)`` ``N``
   vezes converge para exatamente **uma** linha ``Dag``
   ``origem = curadoria_humana`` por Caso; as tuplas persistidas
   ``(id, caso_id, origem, identificador)`` após ``N`` chamadas são **idênticas**
   às de após 1 chamada (mesmos ``id`` → prova de no-op, não re-inserção), sem
   duplicatas da chave ``(caso_id, origem, identificador)``.
3. **Listagem estável:** ``listar_dags(caso)`` devolve a mesma projeção entre a
   1ª e a N-ésima chamada e casa com o estado observado via ORM.

Esta é uma property test de *feature* (``pipeline-seis-estagios``): a propriedade
é esperada **valer** sobre a implementação atual.

Estratégia / isolamento
-----------------------
Cada exemplo aponta um :class:`CaseService` para um diretório de dados
**temporário** com o layout ``<data_dir>/{api,dag}/`` (igual ao real), no qual
escrevemos um ``{slug}_dag.json`` gerado, e constrói um ``DagService`` com uma
``Session`` SQLite **efêmera** (engine em arquivo temporário com o esquema
completo via ``Base.metadata.create_all``). Os DAGs gerados são acíclicos por
construção (arestas apenas de um nó anterior para um posterior), espelhando como
os arquivos reais são parseados (``structural_nodes[*].child_nodes``). Nenhum
caminho toca a rede nem um cliente de IA (não há um neste caminho — Req. 11.3,
11.4).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

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
from app.services.dag.gerador import CuradoriaGeradorDeDag, ModelagemEstrutural


# Um slug de caso curado real: a geração de curadoria só produz um DAG para
# slugs curados (``CaseService.try_load_case``).
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guarda: mantém o slug do fixture curado


# ---------------------------------------------------------------------------
# Fixture de banco efêmero: um engine SQLite em arquivo temporário com o esquema
# completo via Base.metadata.create_all, para que a persistência do DagService
# rode contra um banco real descartável.
# ---------------------------------------------------------------------------


def _make_session_factory() -> tuple[sessionmaker[Session], Path, str]:
    """Cria um engine SQLite efêmero e devolve ``(factory, db_path, tmp_dir)``."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_idemp6_prop_")
    db_path = Path(tmp_dir) / "idemp6.db"
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
    """Escreve os fixtures de ability + DAG para ``slug`` sob ``data_dir``."""
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
# anterior->posterior, logo acíclico por ordem topológica).
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
# Property 5 — idempotência da leitura/backfill curado do DAG (Estágio 3):
# N leituras/backfills == 1 leitura/backfill.
# ===========================================================================


# Feature: pipeline-seis-estagios, Property 5: Idempotência da leitura dos
# artefatos curados (foco no DAG do Estágio 3).
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(structural_nodes=_acyclic_dag(), n=st.integers(min_value=1, max_value=6))
def test_property5_leitura_curada_dag_e_idempotente(
    structural_nodes: list[dict], n: int
) -> None:
    """Ler/backfill o DAG curado N vezes == 1 vez: mesma leitura, sem duplicar.

    Para um DAG curado arbitrário e ``N`` arbitrário (1..6), a leitura curada do
    Estágio 3 é equivalente a cada chamada (sem rede/IA) e o backfill converge
    para exatamente 1 ``Dag`` ``curadoria_humana`` por Caso: mesmas tuplas
    ``(id, caso_id, origem, identificador)`` após N chamadas e após a 1ª (mesmos
    ``id`` → no-op, não re-inserção), e ``listar_dags`` estável.

    Validates: Requirements 7.4, 11.1, 11.3, 11.4
    """
    data_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_idemp6_data_"))
    factory, db_path, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        _write_case_fixture(data_dir, _CURATED_SLUG, structural_nodes)

        case_service = CaseService(data_dir=data_dir)
        gerador = CuradoriaGeradorDeDag(case_service)
        service = DagService(
            session=session,
            case_service=case_service,
            gerador=gerador,
        )

        # (1) Leitura pura equivalente (Estágio 3, sem rede/IA — Req. 7.4, 11.1,
        # 11.3, 11.4): N leituras produzem exatamente o mesmo DagProduzido.
        # ``modelagem`` é ignorada pela curadoria (o grafo já existe em disco).
        leitura_1 = gerador.gerar(_CURATED_SLUG, ModelagemEstrutural())
        assert len(leitura_1) == 1
        assert leitura_1[0].origem == OrigemDag.CURADORIA_HUMANA
        assert leitura_1[0].identificador == _CURATED_SLUG
        for _ in range(n - 1):
            leitura_k = gerador.gerar(_CURATED_SLUG, ModelagemEstrutural())
            # Equivalência de conteúdo entre leituras repetidas (idempotência da
            # leitura curada): o mesmo artefato é lido do disco a cada chamada.
            assert [d.model_dump() for d in leitura_k] == [
                d.model_dump() for d in leitura_1
            ]

        # (2) Backfill não duplica (Req. 11.1): 1ª chamada insere exatamente um
        # Dag curadoria_humana reconciliado por (caso_id, origem, identificador).
        listagem_1 = service.sincronizar_dags_curados(_CURATED_SLUG)
        tuplas_1 = _persisted_tuples(session, _CURATED_SLUG)

        assert len(tuplas_1) == 1
        _, caso_id_1, origem_1, identificador_1 = tuplas_1[0]
        assert origem_1 == OrigemDag.CURADORIA_HUMANA
        assert caso_id_1 == _CURATED_SLUG
        assert identificador_1 == _CURATED_SLUG  # curadoria usa identificador=slug

        # Chamadas 2..N são no-ops reconciliadas pela mesma chave.
        listagem_n = listagem_1
        for _ in range(n - 1):
            listagem_n = service.sincronizar_dags_curados(_CURATED_SLUG)

        tuplas_n = _persisted_tuples(session, _CURATED_SLUG)

        # Conjunto persistido após N chamadas == após 1 chamada: mesma contagem,
        # mesmas tuplas, MESMOS ids (no-op, não re-inserção). Sem duplicar.
        assert tuplas_n == tuplas_1
        assert len(tuplas_n) == 1

        # A chave (caso_id, origem, identificador) permanece única.
        chaves = [(c, o, ident) for (_id, c, o, ident) in tuplas_n]
        assert len(chaves) == len(set(chaves))

        # (3) listar_dags estável entre a 1ª e a N-ésima chamada e casa com o ORM.
        resumos_1 = [
            (r.id, r.caso_id, r.origem, r.identificador) for r in listagem_1
        ]
        resumos_n = [
            (r.id, r.caso_id, r.origem, r.identificador) for r in listagem_n
        ]
        assert resumos_n == resumos_1
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
