"""Testes de exemplo do Provisioning_Map (derivação, idempotência e consumo).

Casos concretos (sem Hypothesis) exercitando a Fase 4.1 (design Component 12) do
:class:`app.services.provisioning_map_service.ProvisioningMapService` e o seu
consumo pelo Stage 5. Cada caso prova uma face distinta do contrato dos Req.
8.1–8.4:

* **Derivação + coerência do ``target_os`` (Req. 8.1).** Semeando as ``Ability``
  de uma campanha com executores de plataforma Windows, ``derive`` devolve
  ``target_os == "windows"``; com executores Linux, ``"linux"``. O inventário
  (``content``) é coerente com o ``target_os`` derivado.
* **Idempotência (Req. 8.2).** Derivar duas vezes a mesma campanha mantém
  **exatamente uma** linha em ``provisioning_map`` e o ``content`` estável
  (upsert ``latest-wins`` pela ``campaign_id`` única).
* **Associação no ``experience_memory`` (Req. 8.3).** Após ``derive`` há uma
  linha de afinidade ``environment_affinity`` no ``experience_memory`` carregando
  a campanha e o ``target_os`` derivado.
* **Consumo pelo Stage 5 (Req. 8.4).** ``EnvironmentStageService
  .recommended_provisioning_map`` — a fonte que o Stage 5 consulta — devolve o
  ``target_os`` derivado e persiste a linha de ``provisioning_map`` que o Stage 5
  leria.

Infra dos testes
----------------
Os caminhos relacional + vetorial usam uma ``Session`` SQLite efêmera (arquivo
temporário + ``Base.metadata.create_all``, o mesmo padrão dos testes irmãos) com
um ``_FakeEmbedder`` determinístico injetado no :class:`ExperienceMemory` (o
SQLite não possui backend de embeddings; o vetor é apenas armazenado na coluna
portável ``Vector``). Nenhuma rede é tocada.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import (
    Ability,
    Case,
    ExperienceMemory as ExperienceMemoryRow,
    ProvisioningMap as ProvisioningMapRow,
)
from app.services.ai.memory import ExperienceMemory
from app.services.environment.stage_service import EnvironmentStageService
from app.services.provisioning_map_service import (
    TARGET_OS_LINUX,
    TARGET_OS_WINDOWS,
    ProvisioningMapService,
)

# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterwards. Mirrors the temp-file SQLite +
    ``Base.metadata.create_all`` pattern of the sibling tests.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="provisioning_map_"))
    db_path = tmp_dir / "provisioning_map.db"
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


def _dispose(session: Session, engine: Any, db_path: Path) -> None:
    """Close the session, dispose the engine and remove the temp db file + dir."""
    session.close()
    engine.dispose()
    shutil.rmtree(db_path.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Deterministic embedder (the SQLite db has no embedding backend).
# ---------------------------------------------------------------------------

# Small, fixed width for the deterministic embedder. The real column is declared
# as a portable ``Vector`` whose width SQLite does not enforce, so a small vector
# is stored verbatim — the derivation never computes cosine here.
_EMBED_DIM = 8


class _FakeEmbedder:
    """Deterministic text->vector map standing in for the SEPINF embedder.

    Structurally satisfies the ``@runtime_checkable`` ``Embedder`` Protocol (an
    ``embed(text) -> list[float]`` method). Stable projection of a
    text-and-dimension hash; no network, no model weights.
    """

    def embed(self, text: str) -> list[float]:
        vec: list[float] = []
        for dim in range(_EMBED_DIM):
            component = hashlib.sha256(f"{dim}:{text}".encode("utf-8")).digest()[0]
            vec.append((component / 127.5) - 1.0)
        if all(abs(c) < 1e-12 for c in vec):
            vec[0] = 1.0
        return vec


# ---------------------------------------------------------------------------
# Seeding helpers (campaigns + abilities via the ORM).
# ---------------------------------------------------------------------------


def _seed_campaign(session: Session, campaign_id: str) -> None:
    """Insert one ``campaigns`` row so the FK of abilities/map is satisfied."""
    session.add(Case(id=campaign_id, name=campaign_id))
    session.flush()


def _seed_ability(
    session: Session,
    *,
    campaign_id: str,
    ability_id: str,
    technique_id: str,
    platform: str,
) -> None:
    """Insert one ``abilities`` row with a single executor on ``platform``."""
    session.add(
        Ability(
            ability_id=ability_id,
            campaign_id=campaign_id,
            name=f"{technique_id} - ability",
            technique_id=technique_id,
            executors=[
                {
                    "name": "cmd",
                    "platform": platform,
                    "command": f"run {technique_id} on {platform}",
                }
            ],
        )
    )
    session.flush()


def _make_service(session: Session) -> ProvisioningMapService:
    """Build a ProvisioningMapService over the session with the fake embedder."""
    memory = ExperienceMemory(session, embedder=_FakeEmbedder())
    return ProvisioningMapService(session, memory=memory)


# ---------------------------------------------------------------------------
# Case 1 — derivation + target_os coherence (Req. 8.1).
# ---------------------------------------------------------------------------


def test_derive_target_os_is_coherent_with_platforms() -> None:
    """Windows executors derive ``windows``; Linux executors derive ``linux`` (Req. 8.1).

    The derived ``content`` inventory is coherent with the derived ``target_os``:
    its ``target_os`` field and topology match, and the recommended image belongs
    to the derived OS family.
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        _seed_campaign(session, "win_campaign")
        _seed_ability(
            session,
            campaign_id="win_campaign",
            ability_id="win-1",
            technique_id="T1059",
            platform="windows",
        )

        _seed_campaign(session, "linux_campaign")
        _seed_ability(
            session,
            campaign_id="linux_campaign",
            ability_id="lin-1",
            technique_id="T1105",
            platform="linux",
        )
        session.commit()

        service = _make_service(session)

        windows_map = service.derive("win_campaign")
        linux_map = service.derive("linux_campaign")
        session.commit()

        # The derived target_os matches the dominant platform family (Req. 8.1).
        assert windows_map.target_os == TARGET_OS_WINDOWS
        assert linux_map.target_os == TARGET_OS_LINUX

        # The inventory is coherent with the derived target_os on both paths.
        for provisioning_map in (windows_map, linux_map):
            content = provisioning_map.content
            assert content["target_os"] == provisioning_map.target_os
            assert content["topology"]["target_os"] == provisioning_map.target_os
            # A single recommended image, coherent with the OS family.
            assert len(content["images"]) == 1
            image = content["images"][0]
            if provisioning_map.target_os == TARGET_OS_WINDOWS:
                assert "windows" in image.lower()
            else:
                assert "windows" not in image.lower()
    finally:
        _dispose(session, engine, db_path)


# ---------------------------------------------------------------------------
# Case 2 — idempotency: deriving twice keeps exactly one row (Req. 8.2).
# ---------------------------------------------------------------------------


def test_derive_twice_keeps_single_row_and_stable_content() -> None:
    """Re-deriving a campaign upserts the single ``provisioning_map`` row (Req. 8.2).

    Two ``derive`` calls for the same campaign leave exactly one row (unique
    ``campaign_id``) with stable ``content`` and the same ``row_id``.
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        _seed_campaign(session, "campaign_a")
        _seed_ability(
            session,
            campaign_id="campaign_a",
            ability_id="a-1",
            technique_id="T1082",
            platform="linux",
        )
        session.commit()

        service = _make_service(session)

        first = service.derive("campaign_a")
        session.commit()
        second = service.derive("campaign_a")
        session.commit()

        # Exactly one provisioning_map row for the campaign (Req. 8.2).
        row_count = session.execute(
            select(func.count())
            .select_from(ProvisioningMapRow)
            .where(ProvisioningMapRow.campaign_id == "campaign_a")
        ).scalar_one()
        assert row_count == 1

        # Latest-wins upsert: same row updated, content stable across derivations.
        assert first.row_id == second.row_id
        assert first.target_os == second.target_os
        assert first.content == second.content
    finally:
        _dispose(session, engine, db_path)


# ---------------------------------------------------------------------------
# Case 3 — environment_affinity stored into experience_memory (Req. 8.3).
# ---------------------------------------------------------------------------


def test_derive_stores_environment_affinity() -> None:
    """``derive`` stores one ``environment_affinity`` row in experience_memory (Req. 8.3).

    The stored affinity carries the campaign and the derived ``target_os`` in its
    metadata so similar target patterns can be retrieved by similarity later.
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        _seed_campaign(session, "campaign_mem")
        _seed_ability(
            session,
            campaign_id="campaign_mem",
            ability_id="mem-1",
            technique_id="T1059",
            platform="windows",
        )
        session.commit()

        service = _make_service(session)
        provisioning_map = service.derive("campaign_mem")
        session.commit()

        affinity_rows = (
            session.execute(select(ExperienceMemoryRow)).scalars().all()
        )
        environment_affinities = [
            row
            for row in affinity_rows
            if (row.meta or {}).get("kind") == "environment_affinity"
        ]
        assert len(environment_affinities) == 1

        affinity = environment_affinities[0]
        assert affinity.meta.get("campaign_id") == "campaign_mem"
        assert affinity.meta.get("target_os") == provisioning_map.target_os
        # Every stored affinity carries a non-null embedding.
        assert affinity.embedding is not None
        assert len(list(affinity.embedding)) == _EMBED_DIM
    finally:
        _dispose(session, engine, db_path)


# ---------------------------------------------------------------------------
# Case 4 — Stage 5 consumes the Provisioning_Map (Req. 8.4).
# ---------------------------------------------------------------------------


def test_stage5_recommended_provisioning_map_returns_derived_target_os() -> None:
    """Stage 5 reads the derived ``target_os`` through the Provisioning_Map (Req. 8.4).

    ``EnvironmentStageService.recommended_provisioning_map`` — the source Stage 5
    consults — returns the derived ``target_os`` and persists the single
    ``provisioning_map`` row Stage 5 would read.
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        _seed_campaign(session, "campaign_stage5")
        _seed_ability(
            session,
            campaign_id="campaign_stage5",
            ability_id="s5-1",
            technique_id="T1059",
            platform="windows",
        )
        session.commit()

        stage_service = EnvironmentStageService(session)
        recommended = stage_service.recommended_provisioning_map("campaign_stage5")

        # Stage 5 reads the derived target_os from the Provisioning_Map (Req. 8.4).
        assert recommended == TARGET_OS_WINDOWS

        # The persisted row is the source Stage 5 reads: one row carrying the
        # same target_os (recommended_provisioning_map commits its own unit of
        # work).
        persisted = (
            session.execute(
                select(ProvisioningMapRow).where(
                    ProvisioningMapRow.campaign_id == "campaign_stage5"
                )
            )
            .scalars()
            .all()
        )
        assert len(persisted) == 1
        assert persisted[0].target_os == TARGET_OS_WINDOWS
    finally:
        _dispose(session, engine, db_path)
