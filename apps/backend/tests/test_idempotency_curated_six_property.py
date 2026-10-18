"""Property test da idempotência da leitura curated do DAG — Estágio 3 (task 4.3).

# Feature: pipeline-seis-stages, Property 5: Idempotência da leitura dos
# artefatos curados. Para qualquer campaign curated, ler os artefatos curados do
# Estágio 3 (DAG) duas ou mais vezes produz results equivalentes, sem rede e
# sem IA, e o backfill de persistência não duplica lines.

Validates: Requirements 7.4, 11.1, 11.3, 11.4

Contexto (design.md — Property 5)
---------------------------------
No pipeline de **seis** Estágios, o Estágio 3 "Geração do DAG" de uma campaign
curated lê o DAG curated **diretamente de disco** (``data/dag/{slug}_dag.json`` via
:class:`~app.services.case_service.CaseService`), **sem rede e sem IA**
(Req. 7.4, 11.1, 11.3, 11.4). A costura
:class:`~app.services.dag.generator.CurationDagGenerator` carimba
``origin = curadoria_humana`` e
:meth:`~app.services.dag.dag_service.DagService.sync_curated_dags`
persiste com **backfill idempotente**, reconciliando pela chave
``(campaign_id, origin, identifier)``.

Propriedade
-----------
*Para todo* DAG curated derivável e *para todo* número de leituras/backfills
``N ≥ 1``:

1. **Leitura pura equivalente (sem rede/IA):** chamar
   ``CurationDagGenerator.generate`` ``N`` vezes produz exatamente o mesmo
   ``ProducedDag`` (mesma ``origin``, ``identifier``, ``label`` e
   ``content``) a cada leitura — a leitura curated do Estágio 3 é idempotente.
2. **Backfill não duplica:** aplicar ``sync_curated_dags(campaign)`` ``N``
   vezes converge para exatamente **uma** line ``Dag``
   ``origin = curadoria_humana`` por campaign; as tuplas persistidas
   ``(id, campaign_id, origin, identifier)`` após ``N`` chamadas são **idênticas**
   às de após 1 chamada (mesmos ``id`` → prova de no-op, não re-inserção), sem
   duplicatas da chave ``(campaign_id, origin, identifier)``.
3. **Listagem estável:** ``list_dags(campaign)`` devolve a mesma projeção entre a
   1ª e a N-ésima chamada e casa com o state observado via ORM.

Esta é uma property test de *feature* (``pipeline-seis-stages``): a propriedade
é esperada **valer** sobre a implementação atual.

Estratégia / isolation
-----------------------
Cada exemplo aponta um :class:`CaseService` para um diretório de dados
**temporário** com o layout ``<data_dir>/{api,dag}/`` (igual ao real), no qual
escrevemos um ``{slug}_dag.json`` gerado, e constrói um ``DagService`` com uma
``Session`` SQLite **efêmera** (engine em file temporário com o esquema
completo via ``Base.metadata.create_all``). Os DAGs gerados são acíclicos por
construção (edges apenas de um nó anterior para um posterior), espelhando como
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
from app.models.enums import DagOrigin
from app.services.campaign_service import CURATED_CASES, CaseService
from app.services.dag.dag_service import DagService
from app.services.dag.generator import CurationDagGenerator, StructuralModeling


# Um slug de campaign curated real: a geração de curation só produz um DAG para
# slugs curados (``CaseService.try_load_case``).
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guarda: mantém o slug do fixture curated


# ---------------------------------------------------------------------------
# Fixture de banco efêmero: um engine SQLite em file temporário com o esquema
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


def _persisted_tuples(session: Session, campaign: str) -> list[tuple]:
    """Conjunto persistido como tuplas ``(id, campaign_id, origin, identifier)``.

    Ordenado por ``id`` para uma comparação estável. Lê diretamente da tabela
    ``dags`` (ORM) para observar o state persistido sem depender da projeção de
    ``list_dags``.
    """
    lines = (
        session.execute(
            select(DagModel).where(DagModel.campaign_id == campaign).order_by(DagModel.id.asc())
        )
        .scalars()
        .all()
    )
    return [
        (line.id, line.campaign_id, line.origin, line.identifier)
        for line in lines
    ]


# ===========================================================================
# Property 5 — idempotência da leitura/backfill curated do DAG (Estágio 3):
# N leituras/backfills == 1 leitura/backfill.
# ===========================================================================


# Feature: pipeline-seis-stages, Property 5: Idempotência da leitura dos
# artefatos curados (foco no DAG do Estágio 3).
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(structural_nodes=_acyclic_dag(), n=st.integers(min_value=1, max_value=6))
def test_property5_curated_dag_read_is_idempotent(
    structural_nodes: list[dict], n: int
) -> None:
    """Ler/backfill o DAG curated N vezes == 1 vez: mesma leitura, sem duplicar.

    Para um DAG curated arbitrário e ``N`` arbitrário (1..6), a leitura curated do
    Estágio 3 é equivalente a cada chamada (sem rede/IA) e o backfill converge
    para exatamente 1 ``Dag`` ``curadoria_humana`` por campaign: mesmas tuplas
    ``(id, campaign_id, origin, identifier)`` após N chamadas e após a 1ª (mesmos
    ``id`` → no-op, não re-inserção), e ``list_dags`` estável.

    Validates: Requirements 7.4, 11.1, 11.3, 11.4
    """
    data_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_idemp6_data_"))
    factory, db_path, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        _write_case_fixture(data_dir, _CURATED_SLUG, structural_nodes)

        case_service = CaseService(data_dir=data_dir)
        generator = CurationDagGenerator(case_service)
        service = DagService(
            session=session,
            case_service=case_service,
            generator=generator,
        )

        # (1) Leitura pura equivalente (Estágio 3, sem rede/IA — Req. 7.4, 11.1,
        # 11.3, 11.4): N leituras produzem exatamente o mesmo ProducedDag.
        # ``modeling`` é ignorada pela curation (o grafo já existe em disco).
        leitura_1 = generator.generate(_CURATED_SLUG, StructuralModeling())
        assert len(leitura_1) == 1
        assert leitura_1[0].origin == DagOrigin.HUMAN_CURATION
        assert leitura_1[0].identifier == _CURATED_SLUG
        for _ in range(n - 1):
            leitura_k = generator.generate(_CURATED_SLUG, StructuralModeling())
            # Equivalência de conteúdo entre leituras repetidas (idempotência da
            # leitura curated): o mesmo artefato é lido do disco a cada chamada.
            assert [d.model_dump() for d in leitura_k] == [
                d.model_dump() for d in leitura_1
            ]

        # (2) Backfill não duplica (Req. 11.1): 1ª chamada insere exatamente um
        # Dag curadoria_humana reconciliado por (campaign_id, origin, identifier).
        listagem_1 = service.sync_curated_dags(_CURATED_SLUG)
        tuples_1 = _persisted_tuples(session, _CURATED_SLUG)

        assert len(tuples_1) == 1
        _, campaign_id_1, origin_1, identifier_1 = tuples_1[0]
        assert origin_1 == DagOrigin.HUMAN_CURATION
        assert campaign_id_1 == _CURATED_SLUG
        assert identifier_1 == _CURATED_SLUG  # curation usa identifier=slug

        # Chamadas 2..N são no-ops reconciliadas pela mesma chave.
        listagem_n = listagem_1
        for _ in range(n - 1):
            listagem_n = service.sync_curated_dags(_CURATED_SLUG)

        tuplas_n = _persisted_tuples(session, _CURATED_SLUG)

        # Conjunto persistido após N chamadas == após 1 chamada: mesma contagem,
        # mesmas tuplas, MESMOS ids (no-op, não re-inserção). Sem duplicar.
        assert tuplas_n == tuples_1
        assert len(tuplas_n) == 1

        # A chave (campaign_id, origin, identifier) permanece única.
        chaves = [(c, o, ident) for (_id, c, o, ident) in tuplas_n]
        assert len(chaves) == len(set(chaves))

        # (3) list_dags estável entre a 1ª e a N-ésima chamada e casa com o ORM.
        resumos_1 = [
            (r.id, r.campaign_id, r.origin, r.identifier) for r in listagem_1
        ]
        resumos_n = [
            (r.id, r.campaign_id, r.origin, r.identifier) for r in listagem_n
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
