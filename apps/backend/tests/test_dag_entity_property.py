"""Property-based test for the multi-origin DAG entity (task 5.3).

# Feature: pipeline-cinco-stages, Property 8: Entidade DAG multi-origin
# (reconfirmada sob a nova ordem) — 1 campaign → N, origin no domínio, round-trip
# do conteúdo. Para QUALQUER ``origin`` do enum ``DagOrigin``
# (``curadoria_humana`` E ``gerado_ia``), persistir uma entidade ``Dag`` e
# lê-la de volta preserva ``identifier``, ``label`` (incl. ``None``),
# ``content`` (JSON) e ``created_at`` byte-a-byte, a FK ``campaign_id → campaigns.id``
# é respeitada, e o valor pt-BR da origin é o persistido. A entidade ``Dag`` é
# inalterada por esta spec; ``gerado_ia`` persiste sem migração nova.

Validates: Requirements 3.7, 10.4

This is a ``feature`` property test (``pipeline-cinco-stages`` é uma spec de
feature, não bugfix): a propriedade é esperada para VALER na definição atual do
modelo :class:`~app.models.domain.Dag` + enum
:class:`~app.models.enums.DagOrigin`.

O que a propriedade garante
---------------------------
* **1 campaign → N DAGs (Req. 4.1, 4.2):** cada exemplo insere um ``Case`` e associa
  um ``Dag`` a ele pela FK ``campaign_id → campaigns.id`` — a inserção só é válida se a
  FK é respeitada (um ``campaign_id`` sem ``Case`` correspondente violaria a FK).
* **Origem no domínio e extensível (Req. 4.3, 4.6, 9.4):** a geração varre
  ``st.sampled_from(list(DagOrigin))``, cobrindo tanto ``curadoria_humana``
  QUANTO ``gerado_ia``. O ``gerado_ia`` persiste **sem migração nova** porque o
  esquema de cada exemplo é construído pelo ORM (``Base.metadata.create_all``),
  e o ``CHECK`` portável (``native_enum=False``) já inclui ambos os valores.
* **Round-trip do conteúdo (Req. 4.4, 4.8):** ``identifier``, ``label``
  (inclusive ``None``), ``content`` (dict JSON seguindo
  ``{structural_nodes, edges}``) e ``created_at`` voltam idênticos ao
  persistido; a ``origin`` volta como o membro de enum exato; e o valor cru
  gravado no disco é a string pt-BR da origin (não o name do membro).

Como a propriedade é dirigida
------------------------------
Hypothesis gera, por exemplo: uma ``origin`` de ``DagOrigin``; um
``identifier`` não vazio; um ``label`` opcional (``None`` incluído); um
``content`` JSON-serializável com a forma ``{structural_nodes, edges}``; e um
``created_at`` arbitrário. Para cada exemplo, um banco SQLite efêmero em file
temporário é construído pelo ORM (mesmo padrão de ``test_dag_models.py`` e
``test_session_state_roundtrip_property.py``), inserimos o ``Case`` e o ``Dag``,
lemos de volta e comparamos cada campo contra o valor gerado — as asserções
nunca confiam na implementação para decidir qual deve ser o result. Cada
exemplo constrói e descarta seu próprio banco, sem vazamento de state, offline
e sem rede/Docker (seguro no environment de dev).
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path
from typing import Optional

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Case, Dag
from app.models.enums import DagOrigin


# ---------------------------------------------------------------------------
# Generators for an arbitrary, valid persisted DAG.
# ---------------------------------------------------------------------------

# Origem varre TODO o enum: curadoria_humana E gerado_ia (Req. 4.3, 4.6, 9.4).
_origem_strategy = st.sampled_from(list(DagOrigin))

# identifier/versão legível — não vazio (NOT NULL, Req. 4.4).
_identificador_strategy = st.text(min_size=1, max_size=40)

# label opcional, incluindo None (coluna nullable, Req. 4.4).
_rotulo_strategy = st.one_of(st.none(), st.text(max_size=60))

# created_at arbitrário. Microssegundos zerados: SQLite guarda DateTime como
# text ISO e alguns formatos truncam frações, então mantemos o round-trip
# determinístico zerando microssegundos (ainda cobre data/hora arbitrárias).
_criado_em_strategy = st.datetimes(
    min_value=dt.datetime(2000, 1, 1),
    max_value=dt.datetime(2100, 12, 31, 23, 59, 59),
).map(lambda d: d.replace(microsecond=0))

# Um node_id curto e estável para os structural_nodes/edges gerados.
_node_id_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=10
)

# Um structural_node mínimo, JSON-serializável, no estilo de data/dag/*.json.
_structural_node_strategy = st.fixed_dictionaries(
    {
        "node_id": _node_id_strategy,
        "technique_id": st.text(
            alphabet="T0123456789", min_size=1, max_size=6
        ),
        "technique_name": st.text(max_size=30),
        "tactic": st.text(max_size=20),
    }
)

# Uma edge {de, para} JSON-serializável.
_aresta_strategy = st.fixed_dictionaries(
    {
        "de": _node_id_strategy,
        "para": _node_id_strategy,
    }
)

# content JSON seguindo a forma {structural_nodes, edges} (Req. 4.4).
_conteudo_strategy = st.fixed_dictionaries(
    {
        "structural_nodes": st.lists(_structural_node_strategy, max_size=6),
        "edges": st.lists(_aresta_strategy, max_size=8),
    }
)

# O DAG arbitrário completo.
_dag_strategy = st.fixed_dictionaries(
    {
        "origin": _origem_strategy,
        "identifier": _identificador_strategy,
        "label": _rotulo_strategy,
        "content": _conteudo_strategy,
        "created_at": _criado_em_strategy,
    }
)


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema per example).
#
# Mirrors test_dag_models.py / test_session_state_roundtrip_property.py: the
# schema is built with the ORM (``Base.metadata.create_all``), NOT alembic — so
# both ``curadoria_humana`` and ``gerado_ia`` persist with no extra migration,
# because the portable ``native_enum=False`` CHECK already includes both.
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterward (no ``.db`` state leaks between examples).
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_dag_entity_prop_")
    db_path = Path(tmp_dir) / "dag_entity_prop.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)

    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    return session_factory(), db_path, engine


# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------


# ``deadline=None``: cada exemplo cria e descarta um banco SQLite em file
# temporário, cujo I/O pode exceder o deadline padrão do Hypothesis. O banco por
# exemplo é intencional (sem vazamento de state), então desabilitamos o
# deadline em vez de enfraquecer o isolation.
@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(spec=_dag_strategy)
def test_property8_dag_entity_roundtrips_for_any_origem(spec: dict) -> None:
    """Para QUALQUER origin de DagOrigin, o Dag persiste e lê de volta idêntico.

    # Feature: pipeline-cinco-stages, Property 8: Entidade DAG multi-origin
    # (reconfirmada sob a nova ordem) — 1 campaign → N, origin no domínio,
    # round-trip do conteúdo; ``gerado_ia`` sem migração nova.
    Validates: Requirements 3.7, 10.4
    """
    origin: DagOrigin = spec["origin"]
    identifier: str = spec["identifier"]
    label: Optional[str] = spec["label"]
    content: dict = spec["content"]
    created_at: dt.datetime = spec["created_at"]

    session, db_path, engine = _build_ephemeral_session()
    try:
        # Insere o campaign para que a FK campaign_id → campaigns.id seja respeitada
        # (1 campaign → N DAGs — Req. 4.1, 4.2).
        campaign_id = "campaign-prop7"
        session.add(Case(id=campaign_id, name="campaign Prop7"))
        session.commit()

        # Insere o Dag com os atributos gerados.
        dag = Dag(
            campaign_id=campaign_id,
            origin=origin,
            identifier=identifier,
            label=label,
            content=content,
            created_at=created_at,
        )
        session.add(dag)
        session.commit()
        dag_id = dag.id

        # Lê de volta via ORM a partir de um cache limpo (round-trip real).
        session.expunge_all()
        loaded = session.get(Dag, dag_id)
        assert loaded is not None

        # FK respeitada: o DAG permanece associado ao campaign inserido.
        assert loaded.campaign_id == campaign_id

        # origin volta como o membro de enum EXATO (curadoria_humana ou gerado_ia).
        assert loaded.origin is origin

        # Round-trip byte-a-byte dos demais campos (incl. label None).
        assert loaded.identifier == identifier
        assert loaded.label == label
        assert loaded.content == content
        assert loaded.created_at == created_at

        # O valor cru gravado no disco é a string pt-BR da origin — não o name
        # do membro (Req. 4.3). Confirma gerado_ia sem migração nova.
        with engine.connect() as conn:
            stored_origem = conn.execute(
                text("SELECT origin FROM dags WHERE id = :id"),
                {"id": dag_id},
            ).scalar_one()
        assert stored_origem == origin.value
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
