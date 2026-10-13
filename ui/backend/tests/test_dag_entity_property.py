"""Property-based test for the multi-origin DAG entity (task 5.3).

# Feature: pipeline-cinco-estagios, Property 8: Entidade DAG multi-origem
# (reconfirmada sob a nova ordem) — 1 Caso → N, origem no domínio, round-trip
# do conteúdo. Para QUALQUER ``origem`` do enum ``OrigemDag``
# (``curadoria_humana`` E ``gerado_ia``), persistir uma entidade ``Dag`` e
# lê-la de volta preserva ``identificador``, ``rotulo`` (incl. ``None``),
# ``conteudo`` (JSON) e ``criado_em`` byte-a-byte, a FK ``caso_id → casos.id``
# é respeitada, e o valor pt-BR da origem é o persistido. A entidade ``Dag`` é
# inalterada por esta spec; ``gerado_ia`` persiste sem migração nova.

Validates: Requirements 3.7, 10.4

This is a ``feature`` property test (``pipeline-cinco-estagios`` é uma spec de
feature, não bugfix): a propriedade é esperada para VALER na definição atual do
modelo :class:`~app.models.domain.Dag` + enum
:class:`~app.models.enums.OrigemDag`.

O que a propriedade garante
---------------------------
* **1 Caso → N DAGs (Req. 4.1, 4.2):** cada exemplo insere um ``Case`` e associa
  um ``Dag`` a ele pela FK ``caso_id → casos.id`` — a inserção só é válida se a
  FK é respeitada (um ``caso_id`` sem ``Case`` correspondente violaria a FK).
* **Origem no domínio e extensível (Req. 4.3, 4.6, 9.4):** a geração varre
  ``st.sampled_from(list(OrigemDag))``, cobrindo tanto ``curadoria_humana``
  QUANTO ``gerado_ia``. O ``gerado_ia`` persiste **sem migração nova** porque o
  esquema de cada exemplo é construído pelo ORM (``Base.metadata.create_all``),
  e o ``CHECK`` portável (``native_enum=False``) já inclui ambos os valores.
* **Round-trip do conteúdo (Req. 4.4, 4.8):** ``identificador``, ``rotulo``
  (inclusive ``None``), ``conteudo`` (dict JSON seguindo
  ``{structural_nodes, arestas}``) e ``criado_em`` voltam idênticos ao
  persistido; a ``origem`` volta como o membro de enum exato; e o valor cru
  gravado no disco é a string pt-BR da origem (não o nome do membro).

Como a propriedade é dirigida
------------------------------
Hypothesis gera, por exemplo: uma ``origem`` de ``OrigemDag``; um
``identificador`` não vazio; um ``rotulo`` opcional (``None`` incluído); um
``conteudo`` JSON-serializável com a forma ``{structural_nodes, arestas}``; e um
``criado_em`` arbitrário. Para cada exemplo, um banco SQLite efêmero em arquivo
temporário é construído pelo ORM (mesmo padrão de ``test_dag_models.py`` e
``test_session_state_roundtrip_property.py``), inserimos o ``Case`` e o ``Dag``,
lemos de volta e comparamos cada campo contra o valor gerado — as asserções
nunca confiam na implementação para decidir qual deve ser o resultado. Cada
exemplo constrói e descarta seu próprio banco, sem vazamento de estado, offline
e sem rede/Docker (seguro no ambiente de dev).
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
from app.models.enums import OrigemDag


# ---------------------------------------------------------------------------
# Generators for an arbitrary, valid persisted DAG.
# ---------------------------------------------------------------------------

# Origem varre TODO o enum: curadoria_humana E gerado_ia (Req. 4.3, 4.6, 9.4).
_origem_strategy = st.sampled_from(list(OrigemDag))

# identificador/versão legível — não vazio (NOT NULL, Req. 4.4).
_identificador_strategy = st.text(min_size=1, max_size=40)

# rotulo opcional, incluindo None (coluna nullable, Req. 4.4).
_rotulo_strategy = st.one_of(st.none(), st.text(max_size=60))

# criado_em arbitrário. Microssegundos zerados: SQLite guarda DateTime como
# texto ISO e alguns formatos truncam frações, então mantemos o round-trip
# determinístico zerando microssegundos (ainda cobre data/hora arbitrárias).
_criado_em_strategy = st.datetimes(
    min_value=dt.datetime(2000, 1, 1),
    max_value=dt.datetime(2100, 12, 31, 23, 59, 59),
).map(lambda d: d.replace(microsecond=0))

# Um node_id curto e estável para os structural_nodes/arestas gerados.
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

# Uma aresta {de, para} JSON-serializável.
_aresta_strategy = st.fixed_dictionaries(
    {
        "de": _node_id_strategy,
        "para": _node_id_strategy,
    }
)

# conteudo JSON seguindo a forma {structural_nodes, arestas} (Req. 4.4).
_conteudo_strategy = st.fixed_dictionaries(
    {
        "structural_nodes": st.lists(_structural_node_strategy, max_size=6),
        "arestas": st.lists(_aresta_strategy, max_size=8),
    }
)

# O DAG arbitrário completo.
_dag_strategy = st.fixed_dictionaries(
    {
        "origem": _origem_strategy,
        "identificador": _identificador_strategy,
        "rotulo": _rotulo_strategy,
        "conteudo": _conteudo_strategy,
        "criado_em": _criado_em_strategy,
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


# ``deadline=None``: cada exemplo cria e descarta um banco SQLite em arquivo
# temporário, cujo I/O pode exceder o deadline padrão do Hypothesis. O banco por
# exemplo é intencional (sem vazamento de estado), então desabilitamos o
# deadline em vez de enfraquecer o isolamento.
@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(spec=_dag_strategy)
def test_property8_dag_entity_roundtrips_for_any_origem(spec: dict) -> None:
    """Para QUALQUER origem de OrigemDag, o Dag persiste e lê de volta idêntico.

    # Feature: pipeline-cinco-estagios, Property 8: Entidade DAG multi-origem
    # (reconfirmada sob a nova ordem) — 1 Caso → N, origem no domínio,
    # round-trip do conteúdo; ``gerado_ia`` sem migração nova.
    Validates: Requirements 3.7, 10.4
    """
    origem: OrigemDag = spec["origem"]
    identificador: str = spec["identificador"]
    rotulo: Optional[str] = spec["rotulo"]
    conteudo: dict = spec["conteudo"]
    criado_em: dt.datetime = spec["criado_em"]

    session, db_path, engine = _build_ephemeral_session()
    try:
        # Insere o Caso para que a FK caso_id → casos.id seja respeitada
        # (1 Caso → N DAGs — Req. 4.1, 4.2).
        caso_id = "caso-prop7"
        session.add(Case(id=caso_id, nome="Caso Prop7"))
        session.commit()

        # Insere o Dag com os atributos gerados.
        dag = Dag(
            caso_id=caso_id,
            origem=origem,
            identificador=identificador,
            rotulo=rotulo,
            conteudo=conteudo,
            criado_em=criado_em,
        )
        session.add(dag)
        session.commit()
        dag_id = dag.id

        # Lê de volta via ORM a partir de um cache limpo (round-trip real).
        session.expunge_all()
        loaded = session.get(Dag, dag_id)
        assert loaded is not None

        # FK respeitada: o DAG permanece associado ao Caso inserido.
        assert loaded.caso_id == caso_id

        # origem volta como o membro de enum EXATO (curadoria_humana ou gerado_ia).
        assert loaded.origem is origem

        # Round-trip byte-a-byte dos demais campos (incl. rotulo None).
        assert loaded.identificador == identificador
        assert loaded.rotulo == rotulo
        assert loaded.conteudo == conteudo
        assert loaded.criado_em == criado_em

        # O valor cru gravado no disco é a string pt-BR da origem — não o nome
        # do membro (Req. 4.3). Confirma gerado_ia sem migração nova.
        with engine.connect() as conn:
            stored_origem = conn.execute(
                text("SELECT origem FROM dags WHERE id = :id"),
                {"id": dag_id},
            ).scalar_one()
        assert stored_origem == origem.value
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
