"""Property test for safety gates as unconditional pre-conditions (task 8.1).

# Feature: ai-curation-agents, Property 2: Safety gates are unconditional
# pre-conditions. For any AI-proposed set of commands and targets, if at least
# one command has an external destination OR at least one target container fails
# the read-only isolation gate, then NO Operation is executed, the host is never
# used, and every executed command yields EXACTLY ONE audit entry (1:1 audit).

Validates: Requirements 8.2, 8.3, 8.5

What this test guarantees
-------------------------
This validates that the **UNCHANGED** safety gates reject AI-origin artifacts
exactly as they reject curation ones. It deliberately **reuses the existing
Stage 6 ``OperationRunner`` / containment / isolation property-test machinery**
(``test_execution_security_stage6_property.py`` and
``test_operation_runner_property.py``): a *real* :class:`OperationRunner` driven
over an injected healthy Caldera (``httpx.MockTransport``) and a parametrizable
isolation verifier, against an **ephemeral per-example SQLite** — now **fed
AI-origin artifacts** instead of curated ones.

The AI artifacts are genuine: a real
:class:`~app.services.ai.agents.op_agent.AiOperationalizer` (whose ``origin()``
is :attr:`TranslationSource.AI_GENERATED` = ``"gerado_ia"``) produces the Stage 4
``ProducedOperationalization``, which is serialized and handed to the UNCHANGED
Stage 6 execute node (:func:`app.services.ai.graph.execute_node`) exactly as the
orchestrated AI loop does. No gate is modified, replaced, disabled or bypassed —
the agent proposes, the existing machinery disposes.

The generated space
-------------------
For each example Hypothesis draws:

* a **destination kind** for the single AI-proposed command — ``internal`` (a lab
  IP, contained), ``external_ip`` (a public IP, external) or ``external_host`` (a
  bare hostname via URL, external by definition); and
* an **isolation state** — all target containers isolated, or at least one not
  isolated.

An **independent oracle** (:func:`_should_execute`) recomputes the safety verdict
straight from those two choices: execution is allowed **iff** the destination is
internal **AND** every container is isolated (fail-closed). The assertions never
trust the implementation to decide what is "safe".

Central invariant (Property 2, fail-closed)
-------------------------------------------
If the AI command's destination is external **OR** isolation fails, then the
runner refuses/aborts pre-flight (``CONTAINMENT_REFUSED`` or ``ISOLATION_FAILED``),
``started is False``, **no** ``Operation`` / ``AbilityResult`` / ``AuditLogEntry``
row is persisted, and the injected Caldera is never touched (nothing ran on the
host). Only the single safe combination (internal destination AND full isolation)
executes, creating exactly one ``Operation`` and recording **exactly one audit
entry per executed command** (the 1:1 audit, Req. 8.5).

Everything is offline: no real Caldera, no Docker daemon, no SEPINF endpoint (no
LLM is injected, so the agent uses its deterministic selection), no network and
no disk beyond the per-example temp-file SQLite, which is torn down in a
``finally`` block so row counts are exact and no state leaks between examples.

_Requirements: 8.2, 8.3, 8.5_
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable

import httpx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AbilityResult, AuditLogEntry, Operation
from app.models.enums import OperationState, TranslationSource
from app.services.ai.agents.op_agent import AiOperationalizer
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
from app.services.dag.dag_service import DagNodeView, DagView
from app.services.operation.runner import OperationRunner, RunOutcome

# The exact pt-BR origin string the AI artifacts must carry (a literal, so the
# assertions never trust the implementation to decide the expected value).
_GERADO_IA = "ai_generated"

# Internal lab IPs (resolve to lab containers; contained → allowed by containment).
_INTERNAL_IPS = ("192.168.10.20", "192.168.20.30", "192.168.30.40")
# External destinations: public IPs and bare hostnames (external by definition).
_EXTERNAL_IPS = ("8.8.8.8", "1.1.1.1", "203.0.113.7")
_EXTERNAL_HOSTS = ("nmap.org", "evil.example.com", "attacker.net")

# Only URL / user@host forms carry a destination the parser extracts; a URL is
# used for every kind so the single command always has exactly one destination of
# the chosen classification.
_DESTINATION_KINDS = ("internal", "external_ip", "external_host")


# ---------------------------------------------------------------------------
# A real CalderaClient over MockTransport serving a healthy, finished chain.
# Recording every request lets us prove the negative path never touches Caldera
# (nothing ran on the host) and the positive path did exercise it.
# ---------------------------------------------------------------------------


class _RecordingCaldera:
    """MockTransport handler: a healthy Caldera whose Operation finished one link.

    Records every request so the fail-closed assertions can prove Caldera was
    never touched on a refused/aborted pre-flight (no adversary command ran, the
    host was never used), while the single safe path drives one finished
    (``status == 0`` → SUCCESS) link so exactly one command executes and yields
    exactly one audit entry.
    """

    def __init__(self, ability_id: str) -> None:
        self._ability_id = ability_id
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.method.upper()
        path = request.url.path
        if method == "GET" and path == HEALTH_PATH:
            return httpx.Response(200, json={"access": "red"})
        if method == "POST" and path == ABILITIES_PATH:
            return httpx.Response(200, json=[])
        if method == "POST" and path == ADVERSARIES_PATH:
            return httpx.Response(200, json={"adversary_id": "adv"})
        if method == "POST" and path == OPERATIONS_PATH:
            return httpx.Response(
                200, json={"id": "op-ai", "name": "n", "state": "running"}
            )
        if method == "GET" and path.startswith(OPERATIONS_PATH):  # poll
            return httpx.Response(
                200,
                json={
                    "id": "op-ai",
                    "state": "finished",
                    "chain": [
                        {
                            "id": "link-1",
                            "ability": {"ability_id": self._ability_id},
                            "command": "",
                            "status": 0,
                        }
                    ],
                },
            )
        return httpx.Response(404, json={})


class _ConfigurableVerifier:
    """Isolation verifier approving IFF ``isolated_all`` is True.

    When ``isolated_all`` is False, EVERY inspected target container is reported
    not isolated (with a reason), so the offending containers are always surfaced
    and the gate fails closed. A fallback container keeps the failed verdict
    observable when no containers were derived (a local-only command).
    """

    def __init__(self, *, isolated_all: bool) -> None:
        self._isolated_all = isolated_all

    def __call__(self, containers, inspector):  # noqa: ANN001
        names = list(containers) if containers else []
        if self._isolated_all:
            return IsolationResult(
                passed=True,
                verdicts=tuple(
                    ContainerIsolationVerdict(container=c, isolated=True, reasons=())
                    for c in names
                ),
            )
        offenders = names or ["nginx"]
        return IsolationResult(
            passed=False,
            verdicts=tuple(
                ContainerIsolationVerdict(
                    container=c, isolated=False, reasons=("bridge externa",)
                )
                for c in offenders
            ),
        )


# ---------------------------------------------------------------------------
# Ephemeral per-example SQLite (exact row counts, no state leak).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_ai_gates_")
    db_path = Path(tmp_dir) / "ai_gates.db"
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


def _count(session: Session, model: type) -> int:
    """Count all rows of ``model`` in the (per-example) database."""
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# ---------------------------------------------------------------------------
# Generators: a destination kind + an isolation state.
# ---------------------------------------------------------------------------


def _command_for(kind: str, token: str) -> str:
    """A single URL command whose one destination is of the given ``kind``."""
    return f"curl http://{token}/x"


@st.composite
def _destination(draw: st.DrawFn) -> tuple[str, str]:
    """Draw ``(kind, command)`` with exactly one destination of that kind."""
    kind = draw(st.sampled_from(_DESTINATION_KINDS))
    if kind == "internal":
        token = draw(st.sampled_from(_INTERNAL_IPS))
    elif kind == "external_ip":
        token = draw(st.sampled_from(_EXTERNAL_IPS))
    else:  # external_host
        token = draw(st.sampled_from(_EXTERNAL_HOSTS))
    return kind, _command_for(kind, token)


def _should_execute(dest_kind: str, isolated_all: bool) -> bool:
    """Independent oracle: execution is allowed IFF internal AND fully isolated."""
    return dest_kind == "internal" and isolated_all


# ---------------------------------------------------------------------------
# Build the AI-origin artifacts and hand them to the UNCHANGED execute node.
# ---------------------------------------------------------------------------

_TECHNIQUE_ID = "T1059"
_POOL_ABILITY_ID = "ai-ab-1"


def _ai_operationalization_payload(command: str) -> dict[str, Any]:
    """Produce a genuine AI-origin Stage 4 artifact carrying ``command``.

    A real :class:`AiOperationalizer` (origin ``gerado_ia``) is driven with a
    single-node DAG and a fake one-entry Atomic Red Team pool whose command is
    ``command``. No LLM is injected, so the agent uses its deterministic
    selection and emits exactly that command. The serialized
    ``ProducedOperationalization`` is returned as the execute node reads it.
    """
    dag = DagView(
        campaign_id="ai-campaign",
        name="ai-campaign",
        curated=False,
        can_advance=True,
        nodes=[
            DagNodeView(
                node_id="n0",
                technique_id=_TECHNIQUE_ID,
                technique_name="Command and Scripting Interpreter",
                tactic="execution",
            )
        ],
    )

    def fake_atomic_lookup(technique_id: str) -> list[dict]:
        # Pure, in-memory: no disk, no network. A single-platform entry so the
        # deterministic selection proposes exactly this command.
        if technique_id != _TECHNIQUE_ID:
            return []
        return [
            {
                "ability_id": _POOL_ABILITY_ID,
                "name": "AI proposed",
                "platform": "sh",
                "command": command,
                "technique_name": "Command and Scripting Interpreter",
                "tactic": "execution",
            }
        ]

    agent = AiOperationalizer(atomic_lookup=fake_atomic_lookup)
    # Sanity: the AI agent stamps AI_GENERATED on the artifact (Req. 2.4) — this
    # is what makes the gates below act on an AI-origin proposal.
    assert agent.origin() is TranslationSource.AI_GENERATED

    produced = agent.operationalize(dag.campaign_id, dag)
    assert produced.origin is TranslationSource.AI_GENERATED
    assert produced.origin.value == _GERADO_IA

    return produced.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Property 2 (main): fail-closed over any mix of destination + isolation.
# ---------------------------------------------------------------------------


# Feature: ai-curation-agents, Property 2: Safety gates are unconditional
# pre-conditions.
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(destination=_destination(), isolated_all=st.booleans())
def test_property2_no_execution_with_external_or_unisolated(
    destination: tuple[str, str],
    isolated_all: bool,
) -> None:
    """No AI Operation runs if its destination is external OR a target not isolated.

    # Feature: ai-curation-agents, Property 2: Safety gates are unconditional
    # pre-conditions.
    Validates: Requirements 8.2, 8.3, 8.5
    """
    dest_kind, command = destination
    operationalization = _ai_operationalization_payload(command)
    state: dict[str, Any] = {
        "campaign_id": "ai-campaign",
        "artifacts": {"operationalization": operationalization},
    }

    session, db_path, engine = _build_ephemeral_session()
    recorder = _RecordingCaldera(_POOL_ABILITY_ID)
    caldera = CalderaClient(transport=httpx.MockTransport(recorder))
    try:
        runner = OperationRunner(
            session=session,
            caldera=caldera,
            audit_logger=AuditLogger(session),
            isolation_verifier=_ConfigurableVerifier(isolated_all=isolated_all),
        )

        # Feed the AI-origin artifacts through the UNCHANGED Stage 6 execute node.
        delta = execute_node(state, runner=runner)
        report = delta["report"]
        outcome = RunOutcome(report["outcome"])

        # The runner "started" an Operation iff the run reached execution; the
        # serialized report carries the final ``outcome`` field (``started`` is a
        # derived property on the dataclass, so recompute it from the outcome).
        started = outcome is RunOutcome.COMPLETED

        expected_run = _should_execute(dest_kind, isolated_all)

        # --- Exactly one Operation iff the only safe combination; else none. ---
        assert _count(session, Operation) == (1 if expected_run else 0)
        assert started is expected_run

        if not expected_run:
            # Fail-closed: refused/aborted pre-flight — nothing ran on the host.
            # Containment runs BEFORE isolation: an external destination is
            # refused first regardless of isolation; an internal-but-not-isolated
            # destination aborts at the isolation gate instead.
            if dest_kind != "internal":
                assert outcome is RunOutcome.CONTAINMENT_REFUSED
            else:
                assert outcome is RunOutcome.ISOLATION_FAILED

            # No observable side effect: no Operation / AbilityResult / audit rows.
            assert _count(session, Operation) == 0
            assert _count(session, AbilityResult) == 0
            assert _count(session, AuditLogEntry) == 0

            # Caldera was NEVER touched — no adversary command executed anywhere
            # (the host is never used, Req. 8.4 as a pre-condition of 8.5).
            assert recorder.requests == []
        else:
            # The ONLY safe combination: internal destination AND full isolation.
            assert outcome is RunOutcome.COMPLETED
            assert report["state"] == OperationState.FINISHED.value

            # Exactly one Operation executed.
            assert _count(session, Operation) == 1

            # 1:1 audit (Req. 8.5): exactly one executed command → exactly one
            # audit entry, and exactly one AbilityResult for that command.
            executed_commands = _count(session, AbilityResult)
            audit_entries = _count(session, AuditLogEntry)
            assert executed_commands == 1
            assert audit_entries == 1
            assert audit_entries == executed_commands  # strict 1:1

            # Caldera WAS exercised (contrast with the fail-closed path).
            assert recorder.requests != []
    finally:
        caldera.close()
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        try:
            os.rmdir(db_path.parent)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Property 2 (targeted): the ONLY safe combination always executes with 1:1 audit.
# ---------------------------------------------------------------------------


# Feature: ai-curation-agents, Property 2: Safety gates are unconditional
# pre-conditions.
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(internal_ip=st.sampled_from(_INTERNAL_IPS))
def test_property2_internal_and_isolated_executes_with_one_audit_per_command(
    internal_ip: str,
) -> None:
    """Internal AND fully isolated is the only combination that runs (1:1 audit).

    # Feature: ai-curation-agents, Property 2: Safety gates are unconditional
    # pre-conditions.
    Validates: Requirements 8.5
    """
    # Oracle sanity: this combination is exactly the one allowed to execute.
    assert _should_execute("internal", True) is True

    operationalization = _ai_operationalization_payload(f"curl http://{internal_ip}/x")
    state: dict[str, Any] = {
        "campaign_id": "ai-campaign",
        "artifacts": {"operationalization": operationalization},
    }

    session, db_path, engine = _build_ephemeral_session()
    recorder = _RecordingCaldera(_POOL_ABILITY_ID)
    caldera = CalderaClient(transport=httpx.MockTransport(recorder))
    try:
        runner = OperationRunner(
            session=session,
            caldera=caldera,
            audit_logger=AuditLogger(session),
            isolation_verifier=_ConfigurableVerifier(isolated_all=True),
        )

        delta = execute_node(state, runner=runner)
        report = delta["report"]

        assert RunOutcome(report["outcome"]) is RunOutcome.COMPLETED
        assert _count(session, Operation) == 1
        # Exactly one executed command → exactly one audit entry (strict 1:1).
        assert _count(session, AbilityResult) == 1
        assert _count(session, AuditLogEntry) == 1
    finally:
        caldera.close()
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        try:
            os.rmdir(db_path.parent)
        except OSError:
            pass
