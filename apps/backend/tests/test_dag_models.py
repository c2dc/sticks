"""Example (non-property) tests for the DAG enum and models (task 1.4).

Cobrem três concerns contra um banco SQLite efêmero construído via o ``Base``/
``create_all`` do app (o mesmo wiring do default de dev SQLite e do target do
Alembic), seguindo o padrão de fixtures de ``test_models.py``:

1. Enum ``DagOrigin`` — cobre exatamente ``{curadoria_humana, gerado_ia}`` com os
   valores pt-BR corretos (``str`` Enum). _Requirements: 4.6, 9.4_
2. Modelo ``Dag`` — colunas corretas (``id``, ``campaign_id`` FK→``campaigns.id``,
   ``origin`` default ``HUMAN_CURATION`` via enum portável, ``identifier``,
   ``label`` nullable, ``content`` JSON, ``created_at``); persistir um ``Dag``
   com ``origin=gerado_ia`` sem migração nova e lê-lo de volta.
   _Requirements: 4.1, 4.2, 4.3, 4.4, 4.6, 9.4_
3. Modelo ``StageRun`` — coluna ``chosen_dag_id`` nullable (FK→``dags.id``).
   _Requirements: 4.7_

Como em ``test_models.py``, cada teste usa um engine SQLite em file temporário
criado a partir de um override de ``Settings`` e remove o file ao final, de
modo que nenhum state ``.db`` vaze.

_Requirements: 4.1, 4.2, 4.3, 4.4, 4.6, 4.7, 9.2, 9.4_
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Case, Dag, StageRun
from app.models.enums import DagOrigin


@pytest.fixture()
def sqlite_engine() -> Iterator[Engine]:
    """Yield an ephemeral temp-file SQLite engine with the full schema built.

    Mirrors ``test_models.py``: a temp file (not ``:memory:``) is used so
    ``create_all`` runs against a real file engine and the portable
    ``native_enum=False`` ``CHECK`` behavior matches production. The file is
    removed afterward so no ``.db`` state leaks.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_dag_models_")
    db_path = Path(tmp_dir) / "dag_models.db"
    settings = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings)
    try:
        import_models()
        Base.metadata.create_all(bind=engine)
        yield engine
    finally:
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(tmp_dir)

    assert not db_path.exists()


@pytest.fixture()
def db_session(sqlite_engine: Engine) -> Iterator[Session]:
    """Yield a session bound to the ephemeral engine, closed after each test."""
    session_factory: sessionmaker[Session] = sessionmaker(
        bind=sqlite_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 1. Enum DagOrigin cobre {curadoria_humana, gerado_ia} (_Requirements: 4.6, 9.4_)
# ---------------------------------------------------------------------------


def test_origin_dag_covers_exactly_two_members() -> None:
    """``DagOrigin`` cobre exatamente curadoria_humana e gerado_ia.
    _Requirements: 4.6, 9.4_"""
    assert {member.value for member in DagOrigin} == {
        "curadoria_humana",
        "gerado_ia",
    }


def test_origin_dag_member_values() -> None:
    """Os valores dos membros são as strings pt-BR corretas (``str`` Enum).
    _Requirements: 4.3, 4.6_"""
    assert DagOrigin.HUMAN_CURATION.value == "curadoria_humana"
    assert DagOrigin.AI_GENERATED.value == "gerado_ia"
    # str Enum: cada membro é instância de str.
    assert isinstance(DagOrigin.HUMAN_CURATION, str)
    assert isinstance(DagOrigin.AI_GENERATED, str)


# ---------------------------------------------------------------------------
# 2. Modelo Dag — colunas, default de origin, FK, nullability, round-trip
# ---------------------------------------------------------------------------


def _make_case(session: Session, case_id: str = "shadowray") -> Case:
    case = Case(id=case_id, name="ShadowRay")
    session.add(case)
    session.commit()
    return case


def _valid_conteudo() -> dict[str, object]:
    return {
        "structural_nodes": ["T1102", "T1059"],
        "edges": [{"de": "T1102", "para": "T1059"}],
    }


def test_dag_has_expected_columns(sqlite_engine: Engine) -> None:
    """A tabela ``dags`` tem as colunas corretas. _Requirements: 4.1, 4.3, 4.4_"""
    inspector = inspect(sqlite_engine)
    columns = {col["name"] for col in inspector.get_columns("dags")}

    assert columns == {
        "id",
        "campaign_id",
        "origin",
        "identifier",
        "label",
        "content",
        "created_at",
    }


def test_dag_campaign_id_is_fk_to_campaigns(sqlite_engine: Engine) -> None:
    """``Dag.campaign_id`` é FK → ``campaigns.id`` (1 campaign → N Dags).
    _Requirements: 4.1, 4.2_"""
    inspector = inspect(sqlite_engine)
    fks = inspector.get_foreign_keys("dags")

    campaign_fk = next(fk for fk in fks if fk["constrained_columns"] == ["campaign_id"])
    assert campaign_fk["referred_table"] == "campaigns"
    assert campaign_fk["referred_columns"] == ["id"]


def test_dag_origin_defaults_to_human_curation(db_session: Session) -> None:
    """``Dag.origin`` default é ``HUMAN_CURATION`` quando não informado.
    _Requirements: 4.3_"""
    _make_case(db_session)
    dag = Dag(
        campaign_id="shadowray",
        identifier="curation-v1",
        content=_valid_conteudo(),
        created_at=dt.datetime(2024, 1, 1, 12, 0, 0),
    )
    db_session.add(dag)
    db_session.commit()
    db_session.refresh(dag)

    assert dag.origin is DagOrigin.HUMAN_CURATION
    assert dag.origin.value == "curadoria_humana"


def test_dag_rotulo_is_nullable(db_session: Session) -> None:
    """``Dag.label`` é nullable — persistir sem rótulo é válido.
    _Requirements: 4.4_"""
    _make_case(db_session)
    dag = Dag(
        campaign_id="shadowray",
        identifier="curation-v1",
        label=None,
        content=_valid_conteudo(),
        created_at=dt.datetime(2024, 1, 1, 12, 0, 0),
    )
    db_session.add(dag)
    db_session.commit()
    db_session.refresh(dag)

    assert dag.label is None


def test_dag_persists_ai_generated_without_new_migration(
    db_session: Session, sqlite_engine: Engine
) -> None:
    """Persistir um ``Dag`` com ``origin=gerado_ia`` e lê-lo de volta.

    O CHECK portável gerado por ``native_enum=False`` já inclui ``gerado_ia``,
    então inserir um DAG ``gerado_ia`` não requer migração de esquema nova.
    _Requirements: 4.6, 9.4_
    """
    _make_case(db_session)
    dag = Dag(
        campaign_id="shadowray",
        origin=DagOrigin.AI_GENERATED,
        identifier="ia-v1",
        label="Gerado por IA",
        content=_valid_conteudo(),
        created_at=dt.datetime(2024, 1, 2, 9, 30, 0),
    )
    db_session.add(dag)
    db_session.commit()
    dag_id = dag.id

    # Lê de volta via ORM: round-trip do membro de enum e do conteúdo JSON.
    db_session.expunge_all()
    loaded = db_session.get(Dag, dag_id)
    assert loaded is not None
    assert loaded.origin is DagOrigin.AI_GENERATED
    assert loaded.identifier == "ia-v1"
    assert loaded.label == "Gerado por IA"
    assert loaded.content == _valid_conteudo()
    assert loaded.created_at == dt.datetime(2024, 1, 2, 9, 30, 0)

    # Confirma que o valor pt-BR foi gravado no disco (não o name do membro).
    with sqlite_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT origin FROM dags WHERE id = :id"),
            {"id": dag_id},
        ).scalar_one()
    assert stored == "gerado_ia"


# ---------------------------------------------------------------------------
# 3. StageRun.chosen_dag_id nullable FK → dags.id (_Requirements: 4.7_)
# ---------------------------------------------------------------------------


def test_stage_run_has_chosen_dag_id_column(sqlite_engine: Engine) -> None:
    """``stage_runs`` tem a coluna nullable ``chosen_dag_id``.
    _Requirements: 4.7_"""
    inspector = inspect(sqlite_engine)
    column = next(
        col
        for col in inspector.get_columns("stage_runs")
        if col["name"] == "chosen_dag_id"
    )
    assert column["nullable"] is True


def test_stage_run_chosen_dag_id_is_fk_to_dags(sqlite_engine: Engine) -> None:
    """``StageRun.chosen_dag_id`` é FK → ``dags.id``. _Requirements: 4.7_"""
    inspector = inspect(sqlite_engine)
    fks = inspector.get_foreign_keys("stage_runs")

    dag_fk = next(
        fk for fk in fks if fk["constrained_columns"] == ["chosen_dag_id"]
    )
    assert dag_fk["referred_table"] == "dags"
    assert dag_fk["referred_columns"] == ["id"]


def test_stage_run_chosen_dag_id_defaults_to_null(db_session: Session) -> None:
    """Um ``StageRun`` sem DAG escolhido tem ``chosen_dag_id`` NULL.
    _Requirements: 4.7_"""
    _make_case(db_session)
    run = StageRun(campaign_id="shadowray", stage=1)
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    assert run.chosen_dag_id is None


def test_stage_run_can_reference_a_persisted_dag(db_session: Session) -> None:
    """``chosen_dag_id`` pode apontar para um ``Dag`` persistido.
    _Requirements: 4.7_"""
    _make_case(db_session)
    dag = Dag(
        campaign_id="shadowray",
        identifier="curation-v1",
        content=_valid_conteudo(),
        created_at=dt.datetime(2024, 1, 1, 12, 0, 0),
    )
    db_session.add(dag)
    db_session.commit()

    run = StageRun(campaign_id="shadowray", stage=1, chosen_dag_id=dag.id)
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    assert run.chosen_dag_id == dag.id
