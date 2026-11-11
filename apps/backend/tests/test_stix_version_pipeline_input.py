"""Example tests for the Stix_Package_Version pipeline input (task 16.2 / Fase 3.8).

Feature: stix-db-single-source-ai-pipeline.

Validates: Requirements 10.1, 10.2, 10.4, 10.5

Cada ``Stix_Package_Version`` é um ``Pipeline_Input`` do pipeline de emulação
(Req. 10.1): a versão vive **tanto** no ``Relational_Store`` (dados exatos
escritos pelo ``StixPersistenceService``) **quanto** no ``Vector_Store`` (uma
projeção semântica em ``experience_memory`` escrita pelo
:class:`~app.services.stix.stix_vector_index_service.StixVectorIndexService`).
Reprocessar a mesma ``version`` deve ser idempotente nos dois stores (Req. 10.2,
10.5), e as versões persistidas devem ser apresentáveis como entradas
selecionáveis (Req. 10.4).

Estes são testes de **exemplo** (1–2 casos concretos, sem Hypothesis), seguindo
o padrão dos testes irmãos: uma ``Session`` SQLite efêmera (arquivo temporário +
``Base.metadata.create_all``) com um embedder determinístico falso injetado no
:class:`~app.services.ai.memory.ExperienceMemory` (o SQLite não possui backend de
embeddings; o vetor é apenas armazenado na coluna portável ``Vector``). Nenhuma
rede nem disco de produção é tocado.

Casos concretos
---------------
1. **Re-ingestão idempotente no vetorial (Req. 10.2, 10.5).** Semeia uma
   ``Stix_Package_Version`` persistida no ``Relational_Store`` (linhas
   ``StixPackage`` + ``StixCampaign`` + ``StixCampaignTechnique`` diretas), roda
   :meth:`StixVectorIndexService.index_version` duas vezes e afirma que a segunda
   passada não insere nada (``indexed == 0``, ``skipped == campanhas``) e que o
   total de linhas ``stix_context`` em ``experience_memory`` é igual ao número de
   campanhas (uma por campanha, sem duplicatas). A idempotência relacional (uma
   linha de pacote por ``version``) também é conferida por simetria.
2. **Seleção de versões como ``Pipeline_Input`` (Req. 10.1, 10.4).** Semeia duas
   ``Stix_Package_Version`` persistidas e afirma que
   :meth:`StixPackageReadService.list_persisted_versions` devolve ambas (as
   versões disponíveis apresentadas como entradas selecionáveis), newest-first.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import (
    ExperienceMemory as ExperienceMemoryRow,
    StixCampaign,
    StixCampaignTechnique,
    StixPackage,
)
from app.services.ai.memory import ExperienceMemory
from app.services.stix.stix_package_read_service import StixPackageReadService
from app.services.stix.stix_vector_index_service import StixVectorIndexService


# ---------------------------------------------------------------------------
# Ephemeral DB fixture: a SQLite engine on a temporary file with the full
# schema via Base.metadata.create_all (same pattern as
# test_stix_package_read_service_unit.py).
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> Iterator[Session]:
    """Yield an ephemeral SQLite ``Session`` with the full schema created."""
    tmp_dir = tempfile.mkdtemp(prefix="stix_version_pipeline_input_db_")
    db_path = Path(tmp_dir) / "pipeline_input.db"
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
    db_session = factory()
    try:
        yield db_session
    finally:
        db_session.close()
        engine.dispose()
        for root, _dirs, files in os.walk(tmp_dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            os.rmdir(root)


# ---------------------------------------------------------------------------
# Deterministic fake embedder (the SEPINF/Ollama embedder stand-in).
# ---------------------------------------------------------------------------

# Small fixed embedding width: the SQLite portable ``Vector`` column stores the
# vector as-is and never computes cosine, so any stable width works. Kept small
# so the stored rows stay cheap.
_EMBED_DIM = 8


class _FakeEmbedder:
    """Deterministic text->vector map standing in for the Ollama embedder.

    Structurally satisfies the ``@runtime_checkable``
    :class:`~app.services.ai.memory.Embedder` Protocol (an
    ``embed(text) -> list[float]`` method). The mapping is a stable projection of
    a text-and-dimension hash, so the SAME text always embeds to the SAME vector.
    No network, no model weights.
    """

    def embed(self, text: str) -> list[float]:
        vec: list[float] = []
        for dim in range(_EMBED_DIM):
            component = hashlib.sha256(f"{dim}:{text}".encode("utf-8")).digest()[0]
            vec.append((component / 127.5) - 1.0)
        return vec


# ---------------------------------------------------------------------------
# Seed helper: persist a Stix_Package_Version (package + campaigns + techniques)
# straight into the Relational_Store, mirroring tests/_stix_seed.py and the
# read-service unit test's direct-ORM seeding.
# ---------------------------------------------------------------------------


def _seed_version(
    session: Session,
    *,
    version: str,
    campaigns: list[tuple[str, str, list[str]]],
) -> None:
    """Persist one ``Stix_Package_Version`` with its campaigns and techniques.

    ``campaigns`` is a list of ``(stix_id, name, technique_ids)`` tuples. Each
    campaign gets one ``stix_campaigns`` row and one ``stix_campaign_techniques``
    row per technique id, so the index service has real relational content to
    project into the Vector_Store.
    """
    package = StixPackage(
        version=version,
        name=f"Enterprise ATT&CK v{version}",
    )
    session.add(package)
    session.flush()  # assign package.id

    for stix_id, name, technique_ids in campaigns:
        campaign = StixCampaign(
            package_id=package.id,
            stix_id=stix_id,
            name=name,
            curated=False,
        )
        session.add(campaign)
        session.flush()  # assign campaign.id
        for technique_id in technique_ids:
            session.add(
                StixCampaignTechnique(
                    campaign_id=campaign.id,
                    technique_id=technique_id,
                    technique_name=f"Technique {technique_id}",
                    tactic="execution",
                )
            )
    session.commit()


# ---------------------------------------------------------------------------
# Case 1 — vector re-ingest idempotency (Req. 10.2, 10.5).
# ---------------------------------------------------------------------------


def test_reindexing_same_version_is_idempotent_in_both_stores(
    session: Session,
) -> None:
    """Re-persisting the same ``version`` == persisting it once, in both stores.

    Seeds a persisted ``Stix_Package_Version`` in the Relational_Store, runs
    :meth:`StixVectorIndexService.index_version` twice and asserts:

    * the first run inserts exactly one ``stix_context`` row per campaign
      (``indexed == campaigns``, ``skipped == 0``);
    * the second run inserts nothing (``indexed == 0``, ``skipped ==
      campaigns``) — the Vector_Store stays idempotent per version (Req. 10.5);
    * the total ``stix_context`` row count equals the number of campaigns (one
      per campaign, no duplicates);
    * the relational side is itself a single persisted package per version
      (Req. 10.2) — no duplicate ``stix_packages`` / ``stix_campaigns`` rows.

    Validates: Requirements 10.2, 10.5
    """
    version = "18.0"
    seeded_campaigns = [
        ("campaign--alpha", "Alpha Campaign", ["T1059", "T1071"]),
        ("campaign--bravo", "Bravo Campaign", ["T1105"]),
        ("campaign--charlie", "Charlie Campaign", ["T1486", "T1021", "T1003"]),
    ]
    _seed_version(session, version=version, campaigns=seeded_campaigns)
    campaign_count = len(seeded_campaigns)

    memory = ExperienceMemory(session, embedder=_FakeEmbedder())
    service = StixVectorIndexService(session, memory=memory)

    # --- First index pass: one stix_context row per campaign. ---------------
    first = service.index_version(version)
    assert first.version == version
    assert first.campaigns == campaign_count
    assert first.indexed == campaign_count, (
        "the first index must insert exactly one stix_context row per campaign"
    )
    assert first.skipped == 0

    rows_after_first = session.execute(
        select(func.count())
        .select_from(ExperienceMemoryRow)
        .where(ExperienceMemoryRow.meta["kind"].as_string() == "stix_context")
    ).scalar_one()
    assert rows_after_first == campaign_count

    # --- Second index pass of the same version: a no-op (Req. 10.5). --------
    second = service.index_version(version)
    assert second.version == version
    assert second.campaigns == campaign_count
    assert second.indexed == 0, (
        "re-indexing the same version must insert nothing into the Vector_Store"
    )
    assert second.skipped == campaign_count, (
        "every campaign of the re-indexed version is skipped as already present"
    )

    # The Vector_Store holds exactly one stix_context row per campaign — no
    # duplicates after the second pass.
    rows_after_second = session.execute(
        select(func.count())
        .select_from(ExperienceMemoryRow)
        .where(ExperienceMemoryRow.meta["kind"].as_string() == "stix_context")
    ).scalar_one()
    assert rows_after_second == campaign_count, (
        "re-indexing must not duplicate the per-campaign stix_context rows"
    )

    # Each row carries a non-null embedding and the version in its metadata, so
    # the version's vector projection is actually durable and versioned.
    stix_context_rows = (
        session.execute(
            select(ExperienceMemoryRow).where(
                ExperienceMemoryRow.meta["kind"].as_string() == "stix_context"
            )
        )
        .scalars()
        .all()
    )
    for row in stix_context_rows:
        assert row.embedding is not None
        assert len(list(row.embedding)) == _EMBED_DIM
        assert row.meta["version"] == version

    # --- Relational idempotency (Req. 10.2): one package per version, and the
    #     seeded campaigns are not duplicated. ---
    package_rows = session.execute(
        select(func.count())
        .select_from(StixPackage)
        .where(StixPackage.version == version)
    ).scalar_one()
    assert package_rows == 1, "a version maps to exactly one persisted package"

    campaign_rows = session.execute(
        select(func.count()).select_from(StixCampaign)
    ).scalar_one()
    assert campaign_rows == campaign_count, (
        "the relational campaigns of the version are not duplicated"
    )


# ---------------------------------------------------------------------------
# Case 2 — persisted versions presented as selectable Pipeline_Input
# (Req. 10.1, 10.4).
# ---------------------------------------------------------------------------


def test_persisted_versions_are_selectable_pipeline_inputs(
    session: Session,
) -> None:
    """Available ``Stix_Package_Version`` are presented as selectable inputs.

    Seeds two persisted versions' packages (each a ``Pipeline_Input``, Req. 10.1)
    and asserts :meth:`StixPackageReadService.list_persisted_versions` returns
    both — the base-selection surface presents the available versions as
    selectable inputs (Req. 10.4) — newest-first and sourced only from the
    Relational_Store.

    Validates: Requirements 10.1, 10.4
    """
    read_service = StixPackageReadService(session)

    # Nothing persisted yet: no selectable inputs.
    assert read_service.list_persisted_versions() == []

    _seed_version(
        session,
        version="17.1",
        campaigns=[("campaign--old", "Old Campaign", ["T1059"])],
    )
    _seed_version(
        session,
        version="18.0",
        campaigns=[("campaign--new", "New Campaign", ["T1071"])],
    )

    versions = read_service.list_persisted_versions()

    # Both persisted versions are presented as selectable Pipeline_Input.
    assert set(versions) == {"17.1", "18.0"}
    # Newest-first ordering (descending by version string) for a stable listing.
    assert versions == ["18.0", "17.1"]
