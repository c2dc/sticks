"""Example (non-property) tests for the attack flow enum and models (task 1.4).

Cobrem três concerns contra um banco SQLite efêmero construído via o ``Base``/
``create_all`` do app (o mesmo wiring do default de dev SQLite e do target do
Alembic), seguindo o padrão de fixtures de ``test_models.py``:

1. Enum ``FlowOrigin`` — cobre exatamente ``{curadoria_humana, gerado_ia}`` com os
   valores pt-BR corretos (``str`` Enum). _Requirements: 4.6, 9.4_
2. Modelo ``AttackFlowRow`` — colunas corretas (``id``, ``campaign_id`` FK→``campaigns.id``,
   ``origin`` default ``HUMAN_CURATION`` via enum portável, ``identifier``,
   ``label`` nullable, ``content`` JSON, ``created_at``); persistir um ``AttackFlowRow``
   com ``origin=gerado_ia`` sem migração nova e lê-lo de volta.
   _Requirements: 4.1, 4.2, 4.3, 4.4, 4.6, 9.4_
3. Modelo ``StageRun`` — sem a coluna morta ``chosen_flow_id`` (removida pela M7).

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
from app.models.domain import Case, AttackFlowRow, StageRun
from app.models.enums import FlowOrigin


@pytest.fixture()
def sqlite_engine() -> Iterator[Engine]:
    """Yield an ephemeral temp-file SQLite engine with the full schema built.

    Mirrors ``test_models.py``: a temp file (not ``:memory:``) is used so
    ``create_all`` runs against a real file engine and the portable
    ``native_enum=False`` ``CHECK`` behavior matches production. The file is
    removed afterward so no ``.db`` state leaks.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_flow_models_")
    db_path = Path(tmp_dir) / "flow_models.db"
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
# 1. Enum FlowOrigin cobre {curadoria_humana, gerado_ia} (_Requirements: 4.6, 9.4_)
# ---------------------------------------------------------------------------


def test_flow_origin_covers_exactly_three_members() -> None:
    """``FlowOrigin`` cobre human_curation, ai_generated e mitre_corpus.
    _Requirements: 4.6, 9.4_"""
    assert {member.value for member in FlowOrigin} == {
        "human_curation",
        "ai_generated",
        "mitre_corpus",    }


def test_origin_flow_member_values() -> None:
    """Os valores dos membros são as strings pt-BR corretas (``str`` Enum).
    _Requirements: 4.3, 4.6_"""
    assert FlowOrigin.HUMAN_CURATION.value == "human_curation"
    assert FlowOrigin.AI_GENERATED.value == "ai_generated"
    # str Enum: cada membro é instância de str.
    assert isinstance(FlowOrigin.HUMAN_CURATION, str)
    assert isinstance(FlowOrigin.AI_GENERATED, str)


# ---------------------------------------------------------------------------
# 2. Modelo AttackFlowRow — colunas, default de origin, FK, nullability, round-trip
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


def test_flow_has_expected_columns(sqlite_engine: Engine) -> None:
    """A tabela ``attack_flows`` tem as colunas corretas (com metadados do corpus
    MITRE e proveniência). _Requirements: 4.1, 4.3, 4.4_"""
    inspector = inspect(sqlite_engine)
    columns = {col["name"] for col in inspector.get_columns("attack_flows")}

    assert columns == {
        "id",
        "campaign_id",
        "origin",
        "identifier",
        "label",
        "content",
        "created_at",
        "source_key",
        "source_sha",
        "scope",
        "description",
        "has_cycles",
        "provenance",
    }


def test_flow_campaign_id_is_fk_to_campaigns(sqlite_engine: Engine) -> None:
    """``AttackFlowRow.campaign_id`` é FK → ``campaigns.id`` (1 campaign → N Flows).
    _Requirements: 4.1, 4.2_"""
    inspector = inspect(sqlite_engine)
    fks = inspector.get_foreign_keys("attack_flows")

    campaign_fk = next(fk for fk in fks if fk["constrained_columns"] == ["campaign_id"])
    assert campaign_fk["referred_table"] == "campaigns"
    assert campaign_fk["referred_columns"] == ["id"]


def test_flow_origin_defaults_to_human_curation(db_session: Session) -> None:
    """``AttackFlowRow.origin`` default é ``HUMAN_CURATION`` quando não informado.
    _Requirements: 4.3_"""
    _make_case(db_session)
    flow = AttackFlowRow(
        campaign_id="shadowray",
        identifier="curation-v1",
        content=_valid_conteudo(),
        created_at=dt.datetime(2024, 1, 1, 12, 0, 0),
    )
    db_session.add(flow)
    db_session.commit()
    db_session.refresh(flow)

    assert flow.origin is FlowOrigin.HUMAN_CURATION
    assert flow.origin.value == "human_curation"


def test_flow_rotulo_is_nullable(db_session: Session) -> None:
    """``AttackFlowRow.label`` é nullable — persistir sem rótulo é válido.
    _Requirements: 4.4_"""
    _make_case(db_session)
    flow = AttackFlowRow(
        campaign_id="shadowray",
        identifier="curation-v1",
        label=None,
        content=_valid_conteudo(),
        created_at=dt.datetime(2024, 1, 1, 12, 0, 0),
    )
    db_session.add(flow)
    db_session.commit()
    db_session.refresh(flow)

    assert flow.label is None


def test_flow_persists_ai_generated_without_new_migration(
    db_session: Session, sqlite_engine: Engine
) -> None:
    """Persistir um ``AttackFlowRow`` com ``origin=gerado_ia`` e lê-lo de volta.

    O CHECK portável gerado por ``native_enum=False`` já inclui ``gerado_ia``,
    então inserir um attack flow ``gerado_ia`` não requer migração de esquema nova.
    _Requirements: 4.6, 9.4_
    """
    _make_case(db_session)
    flow = AttackFlowRow(
        campaign_id="shadowray",
        origin=FlowOrigin.AI_GENERATED,
        identifier="ia-v1",
        label="Gerado por IA",
        content=_valid_conteudo(),
        created_at=dt.datetime(2024, 1, 2, 9, 30, 0),
    )
    db_session.add(flow)
    db_session.commit()
    flow_id = flow.id

    # Lê de volta via ORM: round-trip do membro de enum e do conteúdo JSON.
    db_session.expunge_all()
    loaded = db_session.get(AttackFlowRow, flow_id)
    assert loaded is not None
    assert loaded.origin is FlowOrigin.AI_GENERATED
    assert loaded.identifier == "ia-v1"
    assert loaded.label == "Gerado por IA"
    assert loaded.content == _valid_conteudo()
    assert loaded.created_at == dt.datetime(2024, 1, 2, 9, 30, 0)

    # Confirma que o valor pt-BR foi gravado no disco (não o name do membro).
    with sqlite_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT origin FROM attack_flows WHERE id = :id"),
            {"id": flow_id},
        ).scalar_one()
    assert stored == "ai_generated"


# ---------------------------------------------------------------------------
# 3. StageRun no longer carries chosen_flow_id (dropped by migration M7)
# ---------------------------------------------------------------------------


def test_stage_run_has_no_chosen_flow_id_column(
    sqlite_engine: Engine, db_session: Session
) -> None:
    """``stage_runs`` dropped the dead ``chosen_flow_id`` column; runs still persist."""
    columns = {col["name"] for col in inspect(sqlite_engine).get_columns("stage_runs")}
    assert "chosen_flow_id" not in columns

    _make_case(db_session)
    run = StageRun(campaign_id="shadowray", stage=1)
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    assert run.id is not None
