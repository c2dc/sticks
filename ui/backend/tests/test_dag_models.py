"""Example (non-property) tests for the DAG enum and models (task 1.4).

Cobrem três concerns contra um banco SQLite efêmero construído via o ``Base``/
``create_all`` do app (o mesmo wiring do default de dev SQLite e do alvo do
Alembic), seguindo o padrão de fixtures de ``test_models.py``:

1. Enum ``OrigemDag`` — cobre exatamente ``{curadoria_humana, gerado_ia}`` com os
   valores pt-BR corretos (``str`` Enum). _Requisitos: 4.6, 9.4_
2. Modelo ``Dag`` — colunas corretas (``id``, ``caso_id`` FK→``casos.id``,
   ``origem`` default ``CURADORIA_HUMANA`` via enum portável, ``identificador``,
   ``rotulo`` nullable, ``conteudo`` JSON, ``criado_em``); persistir um ``Dag``
   com ``origem=gerado_ia`` sem migração nova e lê-lo de volta.
   _Requisitos: 4.1, 4.2, 4.3, 4.4, 4.6, 9.4_
3. Modelo ``StageRun`` — coluna ``dag_escolhido_id`` nullable (FK→``dags.id``).
   _Requisitos: 4.7_

Como em ``test_models.py``, cada teste usa um engine SQLite em arquivo temporário
criado a partir de um override de ``Settings`` e remove o arquivo ao final, de
modo que nenhum estado ``.db`` vaze.

_Requisitos: 4.1, 4.2, 4.3, 4.4, 4.6, 4.7, 9.2, 9.4_
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
from app.models.enums import OrigemDag


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
# 1. Enum OrigemDag cobre {curadoria_humana, gerado_ia} (_Requisitos: 4.6, 9.4_)
# ---------------------------------------------------------------------------


def test_origem_dag_covers_exactly_two_members() -> None:
    """``OrigemDag`` cobre exatamente curadoria_humana e gerado_ia.
    _Requisitos: 4.6, 9.4_"""
    assert {member.value for member in OrigemDag} == {
        "curadoria_humana",
        "gerado_ia",
    }


def test_origem_dag_member_values() -> None:
    """Os valores dos membros são as strings pt-BR corretas (``str`` Enum).
    _Requisitos: 4.3, 4.6_"""
    assert OrigemDag.CURADORIA_HUMANA.value == "curadoria_humana"
    assert OrigemDag.GERADO_IA.value == "gerado_ia"
    # str Enum: cada membro é instância de str.
    assert isinstance(OrigemDag.CURADORIA_HUMANA, str)
    assert isinstance(OrigemDag.GERADO_IA, str)


# ---------------------------------------------------------------------------
# 2. Modelo Dag — colunas, default de origem, FK, nullability, round-trip
# ---------------------------------------------------------------------------


def _make_case(session: Session, case_id: str = "shadowray") -> Case:
    case = Case(id=case_id, nome="ShadowRay")
    session.add(case)
    session.commit()
    return case


def _valid_conteudo() -> dict[str, object]:
    return {
        "structural_nodes": ["T1102", "T1059"],
        "arestas": [{"de": "T1102", "para": "T1059"}],
    }


def test_dag_has_expected_columns(sqlite_engine: Engine) -> None:
    """A tabela ``dags`` tem as colunas corretas. _Requisitos: 4.1, 4.3, 4.4_"""
    inspector = inspect(sqlite_engine)
    columns = {col["name"] for col in inspector.get_columns("dags")}

    assert columns == {
        "id",
        "caso_id",
        "origem",
        "identificador",
        "rotulo",
        "conteudo",
        "criado_em",
    }


def test_dag_caso_id_is_fk_to_casos(sqlite_engine: Engine) -> None:
    """``Dag.caso_id`` é FK → ``casos.id`` (1 Caso → N Dags).
    _Requisitos: 4.1, 4.2_"""
    inspector = inspect(sqlite_engine)
    fks = inspector.get_foreign_keys("dags")

    caso_fk = next(fk for fk in fks if fk["constrained_columns"] == ["caso_id"])
    assert caso_fk["referred_table"] == "casos"
    assert caso_fk["referred_columns"] == ["id"]


def test_dag_origem_defaults_to_curadoria_humana(db_session: Session) -> None:
    """``Dag.origem`` default é ``CURADORIA_HUMANA`` quando não informado.
    _Requisitos: 4.3_"""
    _make_case(db_session)
    dag = Dag(
        caso_id="shadowray",
        identificador="curadoria-v1",
        conteudo=_valid_conteudo(),
        criado_em=dt.datetime(2024, 1, 1, 12, 0, 0),
    )
    db_session.add(dag)
    db_session.commit()
    db_session.refresh(dag)

    assert dag.origem is OrigemDag.CURADORIA_HUMANA
    assert dag.origem.value == "curadoria_humana"


def test_dag_rotulo_is_nullable(db_session: Session) -> None:
    """``Dag.rotulo`` é nullable — persistir sem rótulo é válido.
    _Requisitos: 4.4_"""
    _make_case(db_session)
    dag = Dag(
        caso_id="shadowray",
        identificador="curadoria-v1",
        rotulo=None,
        conteudo=_valid_conteudo(),
        criado_em=dt.datetime(2024, 1, 1, 12, 0, 0),
    )
    db_session.add(dag)
    db_session.commit()
    db_session.refresh(dag)

    assert dag.rotulo is None


def test_dag_persists_gerado_ia_without_new_migration(
    db_session: Session, sqlite_engine: Engine
) -> None:
    """Persistir um ``Dag`` com ``origem=gerado_ia`` e lê-lo de volta.

    O CHECK portável gerado por ``native_enum=False`` já inclui ``gerado_ia``,
    então inserir um DAG ``gerado_ia`` não requer migração de esquema nova.
    _Requisitos: 4.6, 9.4_
    """
    _make_case(db_session)
    dag = Dag(
        caso_id="shadowray",
        origem=OrigemDag.GERADO_IA,
        identificador="ia-v1",
        rotulo="Gerado por IA",
        conteudo=_valid_conteudo(),
        criado_em=dt.datetime(2024, 1, 2, 9, 30, 0),
    )
    db_session.add(dag)
    db_session.commit()
    dag_id = dag.id

    # Lê de volta via ORM: round-trip do membro de enum e do conteúdo JSON.
    db_session.expunge_all()
    loaded = db_session.get(Dag, dag_id)
    assert loaded is not None
    assert loaded.origem is OrigemDag.GERADO_IA
    assert loaded.identificador == "ia-v1"
    assert loaded.rotulo == "Gerado por IA"
    assert loaded.conteudo == _valid_conteudo()
    assert loaded.criado_em == dt.datetime(2024, 1, 2, 9, 30, 0)

    # Confirma que o valor pt-BR foi gravado no disco (não o nome do membro).
    with sqlite_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT origem FROM dags WHERE id = :id"),
            {"id": dag_id},
        ).scalar_one()
    assert stored == "gerado_ia"


# ---------------------------------------------------------------------------
# 3. StageRun.dag_escolhido_id nullable FK → dags.id (_Requisitos: 4.7_)
# ---------------------------------------------------------------------------


def test_stage_run_has_dag_escolhido_id_column(sqlite_engine: Engine) -> None:
    """``execucoes_estagio`` tem a coluna nullable ``dag_escolhido_id``.
    _Requisitos: 4.7_"""
    inspector = inspect(sqlite_engine)
    column = next(
        col
        for col in inspector.get_columns("execucoes_estagio")
        if col["name"] == "dag_escolhido_id"
    )
    assert column["nullable"] is True


def test_stage_run_dag_escolhido_id_is_fk_to_dags(sqlite_engine: Engine) -> None:
    """``StageRun.dag_escolhido_id`` é FK → ``dags.id``. _Requisitos: 4.7_"""
    inspector = inspect(sqlite_engine)
    fks = inspector.get_foreign_keys("execucoes_estagio")

    dag_fk = next(
        fk for fk in fks if fk["constrained_columns"] == ["dag_escolhido_id"]
    )
    assert dag_fk["referred_table"] == "dags"
    assert dag_fk["referred_columns"] == ["id"]


def test_stage_run_dag_escolhido_id_defaults_to_null(db_session: Session) -> None:
    """Um ``StageRun`` sem DAG escolhido tem ``dag_escolhido_id`` NULL.
    _Requisitos: 4.7_"""
    _make_case(db_session)
    run = StageRun(caso_id="shadowray", estagio=1)
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    assert run.dag_escolhido_id is None


def test_stage_run_can_reference_a_persisted_dag(db_session: Session) -> None:
    """``dag_escolhido_id`` pode apontar para um ``Dag`` persistido.
    _Requisitos: 4.7_"""
    _make_case(db_session)
    dag = Dag(
        caso_id="shadowray",
        identificador="curadoria-v1",
        conteudo=_valid_conteudo(),
        criado_em=dt.datetime(2024, 1, 1, 12, 0, 0),
    )
    db_session.add(dag)
    db_session.commit()

    run = StageRun(caso_id="shadowray", estagio=1, dag_escolhido_id=dag.id)
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    assert run.dag_escolhido_id == dag.id
