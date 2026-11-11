"""Bug-condition exploration test for the AI-path ``AbilityResult`` foreign key.

Spec: ``ai-run-ability-result-fk-violation`` (bugfix). Task 1 — exploration test.

# Property 1: Bug Condition - AI-generated ability materialized before its result
# Validates: Requirements 1.1, 1.2

What this test encodes
----------------------
The AI curation path produces its abilities as in-memory ``Stage2AbilityView``
objects stamped ``translation_source = AI_GENERATED`` (origin ``"gerado_ia"``),
each carrying a generated ``ability_id``, and hands them straight to the
unchanged Stage 6 :class:`~app.services.operation.runner.OperationRunner` via
:func:`~app.services.ai.graph.execute_node` — WITHOUT first materializing the
parent rows into the ``abilities`` table. When the runner inserts the first
``AbilityResult``, its ``ability_id`` has no parent row in ``abilities``, so the
database rejects the insert against the
``ability_results.ability_id -> abilities.ability_id`` foreign key
(``resultados_ability_ability_id_fkey``).

This test drives exactly that seam over the suite's SQLAlchemy session and
asserts the expected (fixed) behaviour: the ``AbilityResult`` insert persists
WITHOUT raising a foreign-key ``IntegrityError``, and the run proceeds past the
Stage 6 persistence. It is the SAME test that validates the fix once it lands.

Expected outcome on the UNFIXED code (Task 1)
---------------------------------------------
This test is EXPECTED TO FAIL on the current (unfixed) code: the AI path never
materialises the parent ``abilities`` row, so the ``AbilityResult`` insert raises
an ``IntegrityError`` / foreign-key violation on
``resultados_ability_ability_id_fkey`` ("key not present in table abilities").
That failure IS the success signal — it confirms the bug and the root cause in
``design.md`` (Hypothesized Root Cause). Do NOT fix the production code here.

Faithful reproduction of the foreign key
-----------------------------------------
The product database is PostgreSQL, which enforces
``resultados_ability_ability_id_fkey``; the backend unit suite runs on an
ephemeral SQLite file. SQLite does NOT enforce foreign keys unless
``PRAGMA foreign_keys=ON`` is set, so this module enables that PRAGMA on every
pooled connection (mirroring ``test_migration_domain_value_roundtrip_property``),
reproducing exactly the FK enforcement the product database applies.

Everything external is mocked: Caldera is a real ``CalderaClient`` backed by an
``httpx.MockTransport`` that drives a single finished link for the AI
``ability_id``; isolation is a fake verifier that approves the target containers.
No real Caldera and no real Docker daemon are touched.

_Requirements: 1.1, 1.2_
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Ability, AbilityResult, Operation
from app.models.enums import TranslationSource
from app.services.ai.graph import execute_node
from app.services.audit.audit_logger import AuditLogger
from app.services.caldera import (
    ABILITIES_PATH,
    ADVERSARIES_PATH,
    HEALTH_PATH,
    OPERATIONS_PATH,
    CalderaClient,
)
from app.services.containment.isolation import (
    ContainerIsolationVerdict,
    IsolationResult,
)
from app.services.operation.runner import OperationRunner


# The generated-UUID ``ability_id`` shape the AI path produces, matching the
# reproduction recorded in bugfix.md / design.md (the observed counterexample).
_AI_ABILITY_ID = "1d124748-96de-5461-85a0-3e80ba9ec61c"
_ADVERSARY_ID = "ai-adv-1"
_CAMPAIGN_ID = "shadowray"
# An internal-only (local) command so the containment gate always passes; this
# isolates the variable under test (the missing parent ``abilities`` row) from
# the destination gate.
_LOCAL_COMMAND = "cat /etc/passwd"


@pytest.fixture()
def session() -> Iterator[Session]:
    """An ephemeral temp-file SQLite session with foreign keys ENFORCED.

    Mirrors the stage-6 test's ephemeral-SQLite fixture, but registers a
    ``connect`` listener that runs ``PRAGMA foreign_keys=ON`` on every pooled
    connection so SQLite enforces ``resultados_ability_ability_id_fkey`` exactly
    as the product PostgreSQL does.
    """
    tmp = Path(tempfile.mkdtemp(prefix="ai_fk_violation_"))
    db_path = tmp / "test.db"
    settings = Settings(database_url=f"sqlite:///{db_path}")
    engine = create_db_engine(settings)

    @event.listens_for(engine, "connect")
    def _enforce_sqlite_foreign_keys(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    import_models()
    Base.metadata.create_all(engine)
    local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = local()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def _seed_campaign(session: Session) -> None:
    """Seed the parent ``campaigns`` row the AI run belongs to.

    An AI run is started via ``POST /api/campaigns/{campaign}/ai/run``, so the
    ``campaigns.id`` parent always exists. Seeding it keeps the
    ``abilities.campaign_id -> campaigns.id`` FK satisfiable for the fix's upsert
    while leaving the ``abilities`` row itself ABSENT — which is the bug
    condition under test.
    """
    from app.models.domain import Case

    session.add(Case(id=_CAMPAIGN_ID, name="ShadowRay"))
    session.commit()


def _ai_operationalization_artifact() -> dict:
    """Build the serialized AI operationalization ``execute_node`` consumes.

    Shapes a serialized ``ProducedOperationalization`` whose ``abilities`` is a
    serialized ``Stage2Output`` carrying exactly one AI-origin ability (origin
    ``gerado_ia``) with the generated-UUID ``ability_id`` that has NO row in
    ``abilities``. ``_runner_inputs`` rebuilds this into the runner-facing
    ``_RunnerAbility`` + adversary payload, so the runner consumes the AI-origin
    artifact exactly like a curated one.
    """
    ability_view = {
        "ability_id": _AI_ABILITY_ID,
        "name": "T1003 - OS Credential Dumping",
        "tactic": "credential-access",
        "technique_name": "OS Credential Dumping",
        "technique_id": "T1003",
        "description": "AI-generated ability for the non-curated package.",
        "translation_source": TranslationSource.AI_GENERATED.value,
        "executors": [
            {"name": "sh", "platform": "linux", "command": _LOCAL_COMMAND}
        ],
    }
    adversary = {
        "id": _ADVERSARY_ID,
        "name": "AI Adversary",
        "description": "ordered AI-generated chain",
        "atomic_ordering": [_AI_ABILITY_ID],
    }
    return {
        "abilities": {
            "abilities": [ability_view],
            "adversary": adversary,
            "ordering": [_AI_ABILITY_ID],
        }
    }


def _approving_isolation_verifier(containers, _inspector):  # noqa: ANN001
    """A fake isolation verifier reporting every target container isolated.

    Keeps the isolation gate open so the only variable left is the missing parent
    ``abilities`` row (the bug condition).
    """
    return IsolationResult(
        passed=True,
        verdicts=tuple(
            ContainerIsolationVerdict(container=name, isolated=True, reasons=())
            for name in (containers or ["host"])
        ),
    )


def _healthy_caldera_for_ai_ability() -> CalderaClient:
    """A real ``CalderaClient`` over ``MockTransport`` driving a finished chain.

    Serves a healthy Caldera and a single finished link (``status == 0``) for the
    AI ``ability_id`` so the runner reconciles exactly one ``AbilityResult`` for
    that ability — the insert whose foreign key is under test.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        path = request.url.path
        if method == "GET" and path == HEALTH_PATH:
            return httpx.Response(200, json={"status": "ok"})
        if method == "POST" and path == ABILITIES_PATH:
            return httpx.Response(
                200, json=json.loads(request.content.decode("utf-8"))
            )
        if path == ADVERSARIES_PATH:
            return httpx.Response(200, json={"adversary_id": _ADVERSARY_ID})
        if method == "POST" and path == OPERATIONS_PATH:
            return httpx.Response(
                200, json={"id": "op-ai", "name": "ai-run", "state": "running"}
            )
        if method == "GET" and path.startswith(OPERATIONS_PATH):
            return httpx.Response(
                200,
                json={
                    "id": "op-ai",
                    "state": "finished",
                    "chain": [
                        {
                            "id": "link-ai-1",
                            "ability": {"ability_id": _AI_ABILITY_ID},
                            "command": "",
                            "status": 0,
                            "output": "root:x:0:0",
                        }
                    ],
                },
            )
        return httpx.Response(404, json={})

    return CalderaClient(transport=httpx.MockTransport(handler))


def _run_stage6_over_ai_ability(session: Session) -> None:
    """Drive the AI Stage 6 persistence (``execute_node``) over ``session``.

    Builds the shared ``AgentState`` holding the AI operationalization artifact,
    injects a real ``OperationRunner`` (mocked Caldera + approving isolation) over
    the same session, and invokes ``execute_node`` — the exact fix seam. On the
    unfixed code the runner's ``AbilityResult`` insert hits the foreign key with
    no parent ``abilities`` row and raises an ``IntegrityError``.
    """
    caldera = _healthy_caldera_for_ai_ability()
    runner = OperationRunner(
        session=session,
        caldera=caldera,
        audit_logger=AuditLogger(session),
        isolation_verifier=_approving_isolation_verifier,
    )
    state = {
        "campaign_id": _CAMPAIGN_ID,
        "artifacts": {"operationalization": _ai_operationalization_artifact()},
    }
    try:
        execute_node(state, runner=runner, session=session)  # type: ignore[arg-type]
    finally:
        caldera.close()


def test_ai_generated_ability_result_persists_without_fk_violation(
    session: Session,
) -> None:
    """The AI-path ``AbilityResult`` insert must NOT raise an FK ``IntegrityError``.

    # Property 1: Bug Condition - AI-generated ability materialized before its result
    # Validates: Requirements 1.1, 1.2

    Bug condition (``isBugCondition`` true): an AI-origin ability (origin
    ``gerado_ia``) whose generated ``ability_id`` is ABSENT from ``abilities`` is
    handed to the Stage 6 runner, which inserts an ``AbilityResult`` referencing
    it. The expected (fixed) behaviour asserted here is that the parent
    ``abilities`` row is materialised first, so the insert satisfies
    ``resultados_ability_ability_id_fkey`` and the run proceeds past Stage 6.

    EXPECTED ON UNFIXED CODE: this assertion FAILS — the ``AbilityResult`` insert
    raises an ``IntegrityError`` (foreign-key violation on
    ``resultados_ability_ability_id_fkey``: key ``1d124748-...`` not present in
    ``abilities``), which is the counterexample confirming the bug.
    """
    _seed_campaign(session)

    # Precondition: the AI ability has NO parent row in ``abilities`` (bug input).
    assert (
        session.execute(
            select(func.count())
            .select_from(Ability)
            .where(Ability.ability_id == _AI_ABILITY_ID)
        ).scalar()
        == 0
    )

    # On the unfixed code this raises IntegrityError (the counterexample). The
    # assertion below encodes the expected FIXED behaviour: no FK violation.
    try:
        _run_stage6_over_ai_ability(session)
    except IntegrityError as exc:  # pragma: no cover - the unfixed-code path
        pytest.fail(
            "AbilityResult insert for AI ability_id "
            f"{_AI_ABILITY_ID!r} violated the foreign key "
            "resultados_ability_ability_id_fkey (key not present in table "
            f"abilities): {exc.orig}. This is the bug-condition counterexample: "
            "the AI path never materialized the parent abilities row before the "
            "result insert."
        )

    # The parent ability row now exists (materialized before the result insert).
    assert (
        session.execute(
            select(func.count())
            .select_from(Ability)
            .where(Ability.ability_id == _AI_ABILITY_ID)
        ).scalar()
        == 1
    )
    # The AbilityResult for the AI ability persisted and the run proceeded past
    # the Stage 6 persistence (one Operation row, one result row for the ability).
    assert (
        session.execute(
            select(func.count())
            .select_from(AbilityResult)
            .where(AbilityResult.ability_id == _AI_ABILITY_ID)
        ).scalar()
        == 1
    )
    assert (
        session.execute(select(func.count()).select_from(Operation)).scalar() == 1
    )


# =============================================================================
# Task 2 — preservation tests (Property 2) for ``isBugCondition``-false inputs.
#
# # Property 2: Preservation - Curated and already-present abilities unchanged
# # Validates: Requirements 3.1, 3.2, 3.3
#
# Observation-first methodology
# -----------------------------
# These tests capture the BASELINE behaviour the fix must NOT change, observed
# on the current (UNFIXED) code. Every input below has ``isBugCondition`` FALSE
# (the parent ``abilities`` row already exists, or the failure is unrelated to a
# missing parent row), so each test is EXPECTED TO PASS on the unfixed code and
# must keep passing after the fix lands. They reuse the module's foreign-key
# enforcing SQLite ``session`` fixture, the ``MockTransport`` ``CalderaClient``
# helpers and the approving isolation verifier from Task 1.
#
#   * Curated path preservation (Req. 3.1): a curated ability result whose parent
#     ``abilities`` row was already upserted (exactly as ``stage_service.py::
#     _persist`` does) persists with no foreign-key violation.
#   * Already-present AI ability preservation (Req. 3.1/3.3): when the AI
#     ``ability_id`` already has a row in ``abilities`` the ``AbilityResult``
#     insert succeeds identically and NO duplicate ``abilities`` row is created.
#   * Genuine-failure preservation (Req. 3.2): a persistence failure UNRELATED to
#     a missing parent row (a faked Caldera API failure) still propagates out of
#     the Stage 6 seam so the AI run service rolls back and marks the run FAILED
#     via ``_fail_run`` with the 503 ``run_failed`` contract; nothing is left
#     persisted for the failing run.
#
# _Requirements: 3.1, 3.2, 3.3_
# =============================================================================

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from app.services.caldera import CalderaApiError  # noqa: E402


# A curated ability id distinct from the AI one so the two preservation paths
# never collide in the same assertion.
_CURATED_ABILITY_ID = "c0ffee00-1111-2222-3333-444455556666"


def _seed_ability(
    session: Session,
    *,
    ability_id: str,
    translation_source: TranslationSource,
    name: str = "T1003 - OS Credential Dumping",
    tactic: str = "credential-access",
    technique_name: str = "OS Credential Dumping",
    technique_id: str = "T1003",
    description: str = "Pre-existing parent ability row.",
) -> None:
    """Upsert a parent ``abilities`` row, mirroring ``stage_service.py::_persist``.

    Materialises the parent row BEFORE any ``AbilityResult`` is written, exactly
    as the curated path's ``_persist`` upsert does (``session.get(AbilityModel,
    id)`` else create, then set the metadata + ``translation_source``). This is
    the ``isBugCondition``-false precondition shared by the curated-path and
    already-present-AI-ability preservation tests.
    """
    db_ability = session.get(Ability, ability_id)
    if db_ability is None:
        db_ability = Ability(ability_id=ability_id)
        session.add(db_ability)
    db_ability.campaign_id = _CAMPAIGN_ID
    db_ability.name = name
    db_ability.tactic = tactic
    db_ability.technique_name = technique_name
    db_ability.technique_id = technique_id
    db_ability.description = description
    db_ability.executors = [
        {"name": "sh", "platform": "linux", "command": _LOCAL_COMMAND}
    ]
    db_ability.translation_source = translation_source
    session.commit()


def _curated_operationalization_artifact() -> dict:
    """Build an operationalization artifact carrying the curated ability id.

    Identical in shape to :func:`_ai_operationalization_artifact` but the single
    ability carries ``_CURATED_ABILITY_ID`` and ``translation_source =
    HUMAN_CURATION`` — a curated (non-AI) ability whose parent row the curated
    ``_persist`` path already wrote. ``execute_node`` consumes it exactly as it
    does the AI one, so the runner inserts the curated ``AbilityResult``.
    """
    ability_view = {
        "ability_id": _CURATED_ABILITY_ID,
        "name": "T1003 - OS Credential Dumping",
        "tactic": "credential-access",
        "technique_name": "OS Credential Dumping",
        "technique_id": "T1003",
        "description": "Curated ability whose parent row _persist upserted.",
        "translation_source": TranslationSource.HUMAN_CURATION.value,
        "executors": [
            {"name": "sh", "platform": "linux", "command": _LOCAL_COMMAND}
        ],
    }
    adversary = {
        "id": _ADVERSARY_ID,
        "name": "Curated Adversary",
        "description": "ordered curated chain",
        "atomic_ordering": [_CURATED_ABILITY_ID],
    }
    return {
        "abilities": {
            "abilities": [ability_view],
            "adversary": adversary,
            "ordering": [_CURATED_ABILITY_ID],
        }
    }


def _healthy_caldera_for_ability(ability_id: str) -> CalderaClient:
    """A healthy ``CalderaClient`` whose finished chain carries ``ability_id``.

    Generalises :func:`_healthy_caldera_for_ai_ability` so the curated
    preservation path can drive a finished link for the curated ``ability_id``
    (the id the runner reconciles is the one in Caldera's chain), reconciling
    exactly one ``AbilityResult`` for that ability.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        path = request.url.path
        if method == "GET" and path == HEALTH_PATH:
            return httpx.Response(200, json={"status": "ok"})
        if method == "POST" and path == ABILITIES_PATH:
            return httpx.Response(
                200, json=json.loads(request.content.decode("utf-8"))
            )
        if path == ADVERSARIES_PATH:
            return httpx.Response(200, json={"adversary_id": _ADVERSARY_ID})
        if method == "POST" and path == OPERATIONS_PATH:
            return httpx.Response(
                200, json={"id": "op-ai", "name": "ai-run", "state": "running"}
            )
        if method == "GET" and path.startswith(OPERATIONS_PATH):
            return httpx.Response(
                200,
                json={
                    "id": "op-ai",
                    "state": "finished",
                    "chain": [
                        {
                            "id": "link-1",
                            "ability": {"ability_id": ability_id},
                            "command": "",
                            "status": 0,
                            "output": "root:x:0:0",
                        }
                    ],
                },
            )
        return httpx.Response(404, json={})

    return CalderaClient(transport=httpx.MockTransport(handler))


def _failing_caldera_api() -> CalderaClient:
    """A real ``CalderaClient`` over ``MockTransport`` that fails the run.

    Serves a healthy availability check but returns HTTP 500 on the
    operation-create endpoint, so the runner raises :class:`CalderaApiError` — a
    genuine failure UNRELATED to a missing parent ``abilities`` row — before any
    ``AbilityResult`` is reconciled. This isolates the genuine-failure contract
    from the bug condition under test.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        path = request.url.path
        if method == "GET" and path == HEALTH_PATH:
            return httpx.Response(200, json={"status": "ok"})
        if method == "POST" and path == ABILITIES_PATH:
            return httpx.Response(
                200, json=json.loads(request.content.decode("utf-8"))
            )
        if path == ADVERSARIES_PATH:
            return httpx.Response(200, json={"adversary_id": _ADVERSARY_ID})
        if method == "POST" and path == OPERATIONS_PATH:
            # Genuine upstream failure unrelated to any missing parent row.
            return httpx.Response(500, json={"error": "caldera boom"})
        return httpx.Response(404, json={})

    return CalderaClient(transport=httpx.MockTransport(handler))


def _run_stage6_over_curated_ability(session: Session) -> None:
    """Drive the Stage 6 persistence (``execute_node``) for the curated ability.

    Mirrors :func:`_run_stage6_over_ai_ability` but feeds the curated
    operationalization artifact, so the runner reconciles one ``AbilityResult``
    for the curated ability whose parent row already exists.
    """
    caldera = _healthy_caldera_for_ability(_CURATED_ABILITY_ID)
    runner = OperationRunner(
        session=session,
        caldera=caldera,
        audit_logger=AuditLogger(session),
        isolation_verifier=_approving_isolation_verifier,
    )
    state = {
        "campaign_id": _CAMPAIGN_ID,
        "artifacts": {"operationalization": _curated_operationalization_artifact()},
    }
    try:
        execute_node(state, runner=runner, session=session)  # type: ignore[arg-type]
    finally:
        caldera.close()


def _run_stage6_with_failing_caldera(session: Session) -> None:
    """Drive the Stage 6 persistence with a Caldera that fails the operation.

    The parent ``abilities`` row for the AI ability is already present (so the
    foreign key is NOT the cause); the failing Caldera raises
    :class:`CalderaApiError` out of the runner. ``execute_node`` lets it
    propagate — the exact signal the AI run service's ``_fail_run`` catches to
    mark the run FAILED and surface 503 ``run_failed``.
    """
    caldera = _failing_caldera_api()
    runner = OperationRunner(
        session=session,
        caldera=caldera,
        audit_logger=AuditLogger(session),
        isolation_verifier=_approving_isolation_verifier,
    )
    state = {
        "campaign_id": _CAMPAIGN_ID,
        "artifacts": {"operationalization": _ai_operationalization_artifact()},
    }
    try:
        execute_node(state, runner=runner, session=session)  # type: ignore[arg-type]
    finally:
        caldera.close()


def test_curated_ability_result_persists_without_fk_violation(
    session: Session,
) -> None:
    """Curated path preservation: parent already upserted, result persists.

    # Property 2: Preservation - Curated and already-present abilities unchanged
    # Validates: Requirements 3.1

    ``isBugCondition`` is FALSE: the curated ability's parent ``abilities`` row
    was already upserted by the curated ``_persist`` path, so the Stage 6
    ``AbilityResult`` insert satisfies ``resultados_ability_ability_id_fkey`` and
    persists with no foreign-key violation — exactly as today. EXPECTED: PASS on
    the unfixed code, and unchanged after the fix.
    """
    _seed_campaign(session)
    _seed_ability(
        session,
        ability_id=_CURATED_ABILITY_ID,
        translation_source=TranslationSource.HUMAN_CURATION,
    )

    # The parent curated ability row exists before any result is written.
    assert session.get(Ability, _CURATED_ABILITY_ID) is not None

    _run_stage6_over_curated_ability(session)

    # The curated AbilityResult persisted and the run proceeded past Stage 6.
    assert (
        session.execute(
            select(func.count())
            .select_from(AbilityResult)
            .where(AbilityResult.ability_id == _CURATED_ABILITY_ID)
        ).scalar()
        == 1
    )
    assert (
        session.execute(select(func.count()).select_from(Operation)).scalar() == 1
    )
    # The curated parent row's metadata is untouched (still one row, HUMAN_CURATION).
    curated = session.get(Ability, _CURATED_ABILITY_ID)
    assert curated is not None
    assert curated.translation_source is TranslationSource.HUMAN_CURATION


def test_already_present_ai_ability_result_persists_with_no_duplicate(
    session: Session,
) -> None:
    """Already-present AI ability preservation: identical insert, no duplicate row.

    # Property 2: Preservation - Curated and already-present abilities unchanged
    # Validates: Requirements 3.1, 3.3

    ``isBugCondition`` is FALSE: the AI ``ability_id`` ALREADY has a row in
    ``abilities`` (an idempotent re-run of an iteration), so the Stage 6
    ``AbilityResult`` insert succeeds identically and NO duplicate ``abilities``
    row is created. EXPECTED: PASS on the unfixed code, and unchanged after the
    fix (whose upsert is an idempotent no-op for this input).
    """
    _seed_campaign(session)
    _seed_ability(
        session,
        ability_id=_AI_ABILITY_ID,
        translation_source=TranslationSource.AI_GENERATED,
    )

    # Exactly one parent row exists before the run (the bug condition is absent).
    assert (
        session.execute(
            select(func.count())
            .select_from(Ability)
            .where(Ability.ability_id == _AI_ABILITY_ID)
        ).scalar()
        == 1
    )

    _run_stage6_over_ai_ability(session)

    # The AbilityResult persisted and the run proceeded past Stage 6.
    assert (
        session.execute(
            select(func.count())
            .select_from(AbilityResult)
            .where(AbilityResult.ability_id == _AI_ABILITY_ID)
        ).scalar()
        == 1
    )
    assert (
        session.execute(select(func.count()).select_from(Operation)).scalar() == 1
    )
    # Still exactly one parent ``abilities`` row — no duplicate was created.
    assert (
        session.execute(
            select(func.count())
            .select_from(Ability)
            .where(Ability.ability_id == _AI_ABILITY_ID)
        ).scalar()
        == 1
    )


def test_genuine_caldera_failure_propagates_and_persists_no_result(
    session: Session,
) -> None:
    """Genuine-failure preservation: a non-FK failure still fails the run.

    # Property 2: Preservation - Curated and already-present abilities unchanged
    # Validates: Requirements 3.2

    ``isBugCondition`` is FALSE: the parent ``abilities`` row is present, so the
    failure is UNRELATED to a missing parent row. A faked Caldera API failure
    (HTTP 500 on operation create) raises :class:`CalderaApiError` out of the
    Stage 6 seam. ``execute_node`` lets it propagate — the exact signal the AI
    run service's ``_fail_run`` catches to roll back, mark the run FAILED and
    surface the 503 ``run_failed`` contract. Here we observe the seam-level
    invariant the contract rests on: the genuine failure propagates and no
    ``AbilityResult`` is persisted for the failing run. EXPECTED: PASS on the
    unfixed code, and unchanged after the fix.
    """
    _seed_campaign(session)
    _seed_ability(
        session,
        ability_id=_AI_ABILITY_ID,
        translation_source=TranslationSource.AI_GENERATED,
    )

    # The failure must NOT be a foreign-key / missing-parent-row error.
    with pytest.raises(CalderaApiError):
        _run_stage6_with_failing_caldera(session)

    session.rollback()

    # No per-ability result was persisted for the failing run (the run is marked
    # FAILED upstream via ``_fail_run`` with the 503 ``run_failed`` contract).
    assert (
        session.execute(
            select(func.count())
            .select_from(AbilityResult)
            .where(AbilityResult.ability_id == _AI_ABILITY_ID)
        ).scalar()
        == 0
    )


def _build_fk_enforcing_session() -> tuple[Session, Path, object]:
    """Build a fresh FK-enforcing temp-file SQLite session (per Hypothesis example).

    Mirrors the module's ``session`` fixture (``PRAGMA foreign_keys=ON`` on every
    pooled connection) but returns a brand-new engine/session so each Hypothesis
    example runs over an isolated database with no state leak — avoiding the
    duplicate-``campaigns.id`` collision a reused function-scoped fixture would
    hit across examples.
    """
    tmp = Path(tempfile.mkdtemp(prefix="ai_fk_preservation_"))
    db_path = tmp / "test.db"
    settings = Settings(database_url=f"sqlite:///{db_path.as_posix()}")
    engine = create_db_engine(settings)

    @event.listens_for(engine, "connect")
    def _enforce_sqlite_foreign_keys(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    import_models()
    Base.metadata.create_all(engine)
    local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return local(), tmp, engine


@settings(max_examples=40, deadline=None)
@given(
    name=st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
        min_size=1,
        max_size=40,
    ),
    technique_id=st.from_regex(r"T[0-9]{4}", fullmatch=True),
    tactic=st.sampled_from(
        ["credential-access", "execution", "discovery", "persistence"]
    ),
)
def test_property2_already_present_ai_ability_upsert_is_idempotent(
    name: str,
    technique_id: str,
    tactic: str,
) -> None:
    """Property 2: for any already-present AI ability, no duplicate parent row.

    # Property 2: Preservation - Curated and already-present abilities unchanged
    # Validates: Requirements 3.1, 3.3

    Over many metadata shapes (Hypothesis-generated ``name`` / ``technique_id`` /
    ``tactic``), when the AI ``ability_id`` ALREADY has a row in ``abilities``
    (``isBugCondition`` false), driving the Stage 6 persistence inserts exactly
    one ``AbilityResult`` and leaves exactly one ``abilities`` row — the insert
    behaves identically and no duplicate parent row is created. Each example runs
    over its own fresh FK-enforcing database. EXPECTED: PASS on the unfixed code,
    and unchanged after the fix (its upsert is a no-op here).
    """
    db, tmp, engine = _build_fk_enforcing_session()
    try:
        _seed_campaign(db)
        _seed_ability(
            db,
            ability_id=_AI_ABILITY_ID,
            translation_source=TranslationSource.AI_GENERATED,
            name=name,
            tactic=tactic,
            technique_name=name,
            technique_id=technique_id,
        )

        _run_stage6_over_ai_ability(db)

        assert (
            db.execute(
                select(func.count())
                .select_from(Ability)
                .where(Ability.ability_id == _AI_ABILITY_ID)
            ).scalar()
            == 1
        )
        assert (
            db.execute(
                select(func.count())
                .select_from(AbilityResult)
                .where(AbilityResult.ability_id == _AI_ABILITY_ID)
            ).scalar()
            == 1
        )
    finally:
        db.close()
        engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)
