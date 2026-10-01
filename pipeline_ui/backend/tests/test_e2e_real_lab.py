"""End-to-end integration against the REAL lab environment (task 15.3).

⚠️  LAB-ONLY — **A executar exclusivamente na máquina de laboratório do
usuário. NÃO rodar no ambiente de desenvolvimento nem em CI.**

Unlike ``test_e2e_emulation_mocked.py`` (task 15.2), which drives the whole
pipeline with Caldera and the Docker Engine API **mocked**, this suite drives
the *real* end-to-end flow: a **real Caldera** at ``http://localhost:8888`` and
the **real Ambiente_Docker** (the lab containers on the internal-only networks),
with containment actually satisfied. It runs 1–2 curated cases from start to
finish and validates both the real emulation and the **persisted audit trail**
(Req. 4.2, 4.4, 6.3, 6.8).

Why it is skipped by default
----------------------------
The current dev environment (Windows, no lab Caldera/Docker attack environment)
cannot satisfy containment. Per the design's *"Decisão registrada — endurecimento
via compose separado (Opção A)"*, containment is only satisfied on the hardened
lab machine, where ``docker/docker-compose.hardened.yml`` has been applied so the
attacker/target containers (``kali``, ``nginx``, ``db``) sit **exclusively** on
the ``internal: true`` networks (172.20/21/22.0.0/24), with no ``local-network``
bridge and no external DNS.

This test is therefore guarded **two ways** so it never runs in dev/CI:

1. The ``@pytest.mark.lab`` marker — ``addopts = -m "not lab"`` in
   ``pyproject.toml`` deselects it by default.
2. ``@pytest.mark.skipif(os.getenv("PIPELINE_UI_LAB") != "1", ...)`` — even if
   the marker filter is overridden, it stays **skipped** unless the researcher
   explicitly opts in on the lab machine.

It uses the **REAL** :class:`CalderaClient` (no ``transport=`` / no mock) and the
**REAL** read-only Docker isolation inspector (:class:`DockerSdkInspector` via
``inspect_isolation`` with no injected inspector) — no mocks, no fixtures for the
external systems. See ``LAB.md`` for how to run it.

How to run it on the lab machine
--------------------------------
1. Apply the hardened compose (lab-only reference — see the design decision):
   ``docker compose -f docker/docker-compose.yml -f docker/docker-compose.hardened.yml up -d``
2. Confirm Caldera answers at ``http://localhost:8888`` (header ``KEY``).
3. From ``pipeline_ui/backend`` with the venv active:
   ``$env:PIPELINE_UI_LAB = "1"; pytest -m lab``

_Requisitos: 4.2, 4.4, 6.3, 6.8_
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.api.emulacao import _runner_abilities
from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AbilityResult, AuditLogEntry, Operation
from app.services.audit import AuditLogger
from app.services.caldera import CalderaClient
from app.services.case_service import CaseService
from app.services.containment.isolation import inspect_isolation
from app.services.operation.runner import OperationRunner

# ---------------------------------------------------------------------------
# The curated case(s) to drive end-to-end on the lab machine. Kept to 1–2 cases
# per the task ("Executar 1–2 casos curados ... reais, com contenção
# satisfeita"). Slugs match the CaseService registry / the files under
# sticks/data. A researcher may override the selection via PIPELINE_UI_LAB_CASES
# (comma-separated slugs) without editing this file.
# ---------------------------------------------------------------------------

_DEFAULT_LAB_CASES: tuple[str, ...] = ("shadowray",)


def _lab_cases() -> list[str]:
    """The curated case slugs to run against the real lab (1–2, overridable)."""
    override = os.getenv("PIPELINE_UI_LAB_CASES", "").strip()
    if override:
        return [slug.strip() for slug in override.split(",") if slug.strip()]
    return list(_DEFAULT_LAB_CASES)


# Both guards: the ``lab`` marker (deselected by default via pyproject addopts)
# and a skipif on the opt-in env var, so the test is inert in dev/CI regardless
# of how pytest is invoked.
pytestmark = [
    pytest.mark.lab,
    pytest.mark.skipif(
        os.getenv("PIPELINE_UI_LAB") != "1",
        reason=(
            "requires the real lab Caldera + hardened Docker environment "
            "(task 15.3); set PIPELINE_UI_LAB=1 on the lab machine to run"
        ),
    ),
]


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A temp-file SQLite engine + schema for the lab run.

    Uses SQLite so the run is self-contained; on the lab machine the researcher
    may point ``PIPELINE_UI_DATABASE_URL`` at the real PostgreSQL if they want
    the audit trail persisted there instead — the assertions below read whatever
    engine this fixture builds.
    """
    db_url = os.getenv("PIPELINE_UI_DATABASE_URL")
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_e2e_real_lab_")
    db_path = Path(tmp_dir) / "e2e_real_lab.db"
    settings_obj = Settings(
        database_url=db_url or f"sqlite:///{db_path.as_posix()}"
    )

    engine = create_db_engine(settings_obj)
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
    try:
        yield factory
    finally:
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
            os.rmdir(db_path.parent)


@pytest.fixture(scope="module")
def real_caldera() -> Iterator[CalderaClient]:
    """The REAL CalderaClient pointed at localhost:8888 (no mock transport).

    Verifies availability up front (Req. 4.6 window): if the lab Caldera is not
    answering, the whole lab run is skipped with a clear message rather than
    failing — the researcher simply hasn't brought the hardened stack up yet.
    """
    client = CalderaClient()  # real settings -> http://localhost:8888, header KEY
    availability = client.check_availability()
    if not availability.available:
        client.close()
        pytest.skip(
            "lab Caldera não respondeu em http://localhost:8888 — suba a stack "
            "endurecida (docker-compose.hardened.yml) antes de rodar o teste de "
            "laboratório"
        )
    try:
        yield client
    finally:
        client.close()


@pytest.mark.parametrize("caso_slug", _lab_cases())
def test_real_end_to_end_emulation_and_audit_trail(
    caso_slug: str,
    session_factory: sessionmaker[Session],
    real_caldera: CalderaClient,
) -> None:
    """Drive one curated case end-to-end against the REAL lab and audit it.

    This is the real counterpart of the mocked task-15.2 flow. On the lab
    machine (containment satisfied) it:

    * loads the curated case's real Abilities + Adversary from ``sticks/data``;
    * runs the **real** pre-flight: destination containment (every command must
      target the internal subnets) **and** the **real** read-only Docker
      isolation inspection of the target containers (Req. 6.3) — no mocks;
    * loads the Abilities/Adversary into the **real Caldera** and starts a real
      Operation against the real Ambiente_Docker (Req. 4.2);
    * asserts the per-Ability results and the aggregate totals were persisted
      (Req. 4.4);
    * asserts the audit trail was persisted **1:1** — exactly one row per
      executed command, each carrying the command + target container + result
      (Req. 6.8).

    The body uses the REAL :class:`CalderaClient` and the REAL isolation
    inspector (``inspect_isolation`` with no injected inspector, which builds a
    read-only :class:`DockerSdkInspector` from the ambient Docker environment).
    Nothing here is mocked; it is inert off the lab machine because the module
    guards keep it skipped.
    """
    case_service = CaseService()
    case = case_service.load_case(caso_slug)
    assert case.abilities, f"caso {caso_slug} deve ter Abilities curadas"
    assert case.adversary is not None, f"caso {caso_slug} deve ter Adversary"

    session = session_factory()
    try:
        runner = OperationRunner(
            session=session,
            caldera=real_caldera,
            audit_logger=AuditLogger(session),
            # Defaults are the REAL containment validator and the REAL
            # read-only Docker isolation verifier (inspect_isolation with no
            # injected inspector) — no mocks on the lab machine.
            isolation_verifier=inspect_isolation,
        )

        # Confirmed, real run: pre-flight (containment + isolation, Req. 6.3) ->
        # real Caldera Operation against the real Ambiente_Docker (Req. 4.2).
        # Abilities are adapted to the runner-facing form exactly as the API
        # layer does; the target containers are derived from the abilities'
        # internal destinations by the runner's own preview resolver.
        # Build the raw Adversary payload Caldera needs (v2 uses `adversary_id`,
        # not `id`). Loading it before the Operation gives the chain its links.
        adversary_payload = {
            "adversary_id": case.adversary.id,
            "name": case.adversary.name or case.adversary.id,
            "description": case.adversary.description or "",
            "atomic_ordering": list(case.adversary.atomic_ordering),
        }

        result = runner.run(
            caso_id=case.id,
            abilities=_runner_abilities(case.abilities),
            adversary_id=case.adversary.id,
            confirmado=True,
            adversary_payload=adversary_payload,
        )

        # The Operation actually started and reached a final state; its
        # aggregate is persisted (Req. 4.4/4.5).
        assert result.started is True
        assert result.operation_id is not None
        operacao_id = result.operation_id

        operation = session.get(Operation, operacao_id)
        assert operation is not None
        assert operation.total_sucesso >= 0
        assert operation.total_falha >= 0

        # Per-Ability results persisted (Req. 4.4): one AbilityResult per
        # executed command, with a resolved target container.
        ability_results = (
            session.query(AbilityResult)
            .filter(AbilityResult.operacao_id == operacao_id)
            .all()
        )
        assert ability_results, "a Operação real deve persistir resultados por Ability"
        for r in ability_results:
            # AbilityResult carries the per-Ability outcome (status) and output;
            # the target container is recorded on the AuditLogEntry (checked
            # below), not here. Validate the fields this model actually has.
            assert r.ability_id, "cada resultado deve referenciar a Ability"
            assert r.status is not None, "cada resultado deve ter um status"

        # Aggregate is consistent with the persisted per-Ability results
        # (Req. 4.5): sucesso + falha count the FINISHED links; any still
        # pending/running (a real Operation may not finish every link within the
        # poll window) is persisted as a result but not yet aggregated, so the
        # aggregate is <= the number of results and never exceeds it.
        assert operation.total_sucesso >= 0 and operation.total_falha >= 0
        assert (
            operation.total_sucesso + operation.total_falha <= len(ability_results)
        )

        # Audit trail persisted 1:1 per executed command (Req. 6.8): exactly one
        # AuditLogEntry per executed command, each with command + target
        # container + result, and no duplicates.
        audit_rows = (
            session.query(AuditLogEntry)
            .filter(AuditLogEntry.operacao_id == operacao_id)
            .all()
        )
        # Audit is 1:1 per EXECUTED command (one row per link), not per distinct
        # command text — the ShadowRay set legitimately repeats some commands
        # (e.g. the same curl in two abilities), so uniqueness is not required.
        assert len(audit_rows) == len(ability_results)
        for row in audit_rows:
            assert row.comando and row.comando.strip()
            assert row.container_destino, "auditoria deve registrar o container-alvo"
            assert row.resultado, "auditoria deve registrar o resultado"
    finally:
        session.close()
