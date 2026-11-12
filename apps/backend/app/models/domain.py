"""SQLAlchemy domain models for the Pipeline_UI backend (task 2.1).

These models implement exactly the Data Models section of the design. Every
model inherits from the shared ``Base`` in :mod:`app.db.base`, so importing
this module registers all tables on ``Base.metadata`` (which is what Alembic
autogenerate and ``create_all`` target).

Portability notes (PostgreSQL is the product database; the unit tests run
against an ephemeral SQLite file, so the mappings stay portable across both):

* Enums are mapped with ``native_enum=False`` so they persist as a portable
  ``VARCHAR`` with a ``CHECK`` constraint on both PostgreSQL and the test
  SQLite — we do not depend on PostgreSQL native ``ENUM`` types, and we store
  the enum *values* rather than the member names.
* JSON columns use the generic :class:`sqlalchemy.JSON` type, which maps to a
  portable representation on both backends.
* Column types are the portable generics (``String``, ``Text``, ``Integer``,
  ``DateTime``), avoiding backend-specific types. The sole backend-specific
  type is the pgvector ``VECTOR`` on ``experience_memory.embedding`` (the AI
  experience memory, task 3.1), which is PostgreSQL + pgvector only by design.

The models use the SQLAlchemy 2.x typed ``Mapped`` / ``mapped_column`` style to
match the ``DeclarativeBase`` used by ``Base``.

_Requirements: 9.1, 9.7, 11.1, 11.2, 11.4, 11.8, 11.9, 6.8_
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy import false as sa_false
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    AbilityResultStatus,
    AgentRunStatus,
    AgentStage,
    FlowOrigin,
    IngestSource,
    IngestStatus,
    EnvironmentPlatform,
    EvaluatorDecision,
    Language,
    OperationState,
    StageState,
    TechniqueEmulabilityKind,
    Theme,
    TranslationSource,
)


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp (portable across PostgreSQL and the test SQLite).

    Matches the convention used across the service layer (``SessionStateService``,
    ``OperationRunner``, ...): the AI bookkeeping rows stamp
    their creation/update timestamps with a timezone-naive UTC value so the
    column stays portable between PostgreSQL (the product database) and the
    ephemeral SQLite used by the unit tests.
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


# Design default for the pgvector embedding width (``AI_EMBEDDING_DIM``); mirrors
# the default declared by ``app.services.ai.config.SepinfSettings``. The product
# embedder is Ollama's ``nomic-embed-text``, which produces 768-dim vectors.
_DEFAULT_EMBEDDING_DIM = 768


def _embedding_dim() -> int:
    """Resolve the pgvector embedding dimension (``AI_EMBEDDING_DIM``, default 768).

    The ``experience_memory.embedding`` column is a fixed-width
    ``VECTOR(EMBEDDING_DIM)``; the width comes from the ``AI_EMBEDDING_DIM``
    setting, exactly as :class:`app.services.ai.config.SepinfSettings` reads it
    (process environment first, then the repository-root ``.env``, else the
    design default of 768).

    The models layer is intentionally dependency-free of the services layer
    (importing ``app.services.ai.config`` here would create an import cycle via
    ``app.services.__init__``), so the lookup is reproduced locally against the
    environment and the root ``.env``. This performs no network or socket I/O,
    keeping the model import cheap and side-effect free. Default 768 matches the
    ``nomic-embed-text`` embedder.
    """
    raw = os.environ.get("AI_EMBEDDING_DIM")
    if raw is None:
        raw = _read_root_env_value("AI_EMBEDDING_DIM")
    if raw is None or raw.strip() == "":
        return _DEFAULT_EMBEDDING_DIM
    try:
        return int(raw.strip())
    except ValueError:
        return _DEFAULT_EMBEDDING_DIM


def _read_root_env_value(key: str) -> Optional[str]:
    """Read ``key`` from the repository-root ``.env`` without extra dependencies.

    Mirrors how :class:`app.services.ai.config.SepinfSettings` locates the root
    ``.env`` (relative to the source tree, not the CWD). Returns ``None`` when
    the file or the key is absent. Only simple ``KEY=value`` lines are parsed;
    comments and blank lines are ignored.
    """
    # parents: [models] [app] [backend] [apps] [repo-root]
    env_path = Path(__file__).resolve().parents[4] / ".env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            return value.strip()
    return None


# Maximum length of the evaluator rationale persisted for auditability
# (design/Req. 11.9): the LLM reasoning text is bounded to 10,000 characters.
_RATIONALE_MAX_LENGTH = 10_000


def _portable_enum(enum_cls: type, name: str) -> SAEnum:
    """Build a portable SQLAlchemy ``Enum`` column type.

    Using ``native_enum=False`` makes the column a ``VARCHAR`` + ``CHECK`` on
    every backend (identical behavior on PostgreSQL and the test SQLite). We
    persist the enum *values* via ``values_callable`` so the stored data matches
    the API/DB contract exactly.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )


class Case(Base):
    """Campaign row (curated or AI-materialized); the abilities, adversary and
    attack flows hang off it by ``campaign_id``.

    ``translation_source`` is an explicit, extensible attribute (Req. 11.1/11.2).
    """

    __tablename__ = "campaigns"

    # slug: "shadowray", "apt41_dust", ...
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)  # "ShadowRay"
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    translation_source: Mapped[TranslationSource] = mapped_column(
        _portable_enum(TranslationSource, "translation_source"),
        default=TranslationSource.HUMAN_CURATION,
        nullable=False,
    )
    # True for the curated cases (derived by migration e1f2a3b4c5d6 from the
    # existence of a human_curation flow); false for AI-materialized campaigns.
    curated: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_false(), nullable=False
    )


class AttackFlowRow(Base):
    """First-class persisted attack flow, multi-origin (Req. 4).

    A Campaign (``Case``) can have N attack flows: each row references a ``Case`` by
    ``campaign_id`` (FK → ``campaigns.id``), establishing the 1 Campaign → N Flows
    relation (Req. 4.1, 4.2) — in the same FK style already used by ``Ability``/
    ``Adversary``/``StageRun``.

    ``origin`` is an explicit, extensible attribute (``FlowOrigin``), persisted
    with ``native_enum=False`` (VARCHAR + CHECK) via ``_portable_enum``: the
    generated CHECK already includes ``ai_generated``, so inserting an
    ``ai_generated`` attack flow in the future requires no schema migration (Req. 4.3,
    4.6).

    The graph content (``structural_nodes`` + ``edges``) is stored in a single
    portable JSON column (Req. 4.4).
    """

    __tablename__ = "attack_flows"
    __table_args__ = (
        CheckConstraint(
            "origin IN ('human_curation', 'ai_generated', 'mitre_corpus')",
            name="ck_attack_flows_origin",
        ),
        Index(
            "uq_attack_flows_source_key",
            "source_key",
            unique=True,
            sqlite_where=sa_text("source_key IS NOT NULL"),
            postgresql_where=sa_text("source_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Nullable since migration f2a3b4c5d6e7: MITRE corpus flows do not belong
    # to a Sticks campaign.
    campaign_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("campaigns.id"), nullable=True
    )
    origin: Mapped[FlowOrigin] = mapped_column(
        _portable_enum(FlowOrigin, "flow_origin"),
        default=FlowOrigin.HUMAN_CURATION,
        nullable=False,
    )
    # Corpus metadata (migration f2a3b4c5d6e7). ``source_key`` is the corpus
    # file name (idempotency key, partial UNIQUE index); ``source_sha`` is the
    # blob SHA from the tag listing.
    source_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    scope: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    has_cycles: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_false(), nullable=False
    )
    # {kind, references[], similarity} for AI-produced flows.
    provenance: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # readable attack flow identifier/version (Req. 4.4); for curation,
    # derived from the file (e.g. "human-curation-v1")
    identifier: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # graph content: {"structural_nodes": [...], "edges": [{"from","to"}]}
    content: Mapped[Any] = mapped_column(JSON, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)


class StageRun(Base):
    """Run of a Stage of a Campaign (states, progress, failure)."""

    __tablename__ = "stage_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("campaigns.id"), nullable=True
    )
    # Pipeline stage in the range 1..6 (plain Integer column, no range CHECK:
    # the range is accommodated by the column itself — Req. 1.2).
    stage: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..6
    state: Mapped[StageState] = mapped_column(
        _portable_enum(StageState, "stage_state"),
        default=StageState.NOT_STARTED,
        nullable=False,
    )
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0..100
    # cause of the failure, when state == error
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # modeling step at which it failed (Stage 1)
    failed_step: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    updated_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime, nullable=True
    )


class Ability(Base):
    """A campaign Ability (curated or AI-materialized).

    ``translation_source`` is an explicit, extensible attribute (Req. 11.1/11.2).
    """

    __tablename__ = "abilities"

    # uuid from the file
    ability_id: Mapped[str] = mapped_column(String, primary_key=True)
    campaign_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("campaigns.id"), nullable=True
    )
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # "T1102 - Web Service"
    tactic: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    technique_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    technique_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # list of {name, platform, command}
    executors: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    translation_source: Mapped[TranslationSource] = mapped_column(
        _portable_enum(TranslationSource, "translation_source"),
        default=TranslationSource.HUMAN_CURATION,
        nullable=False,
    )


class Adversary(Base):
    """The Adversary of a campaign (single object with its ``atomic_ordering``).

    ``translation_source`` is an explicit, extensible attribute (Req. 11.1/11.2).
    """

    __tablename__ = "adversaries"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # id from the file
    campaign_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("campaigns.id"), nullable=True
    )
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # ordered list of ability_ids
    atomic_ordering: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    translation_source: Mapped[TranslationSource] = mapped_column(
        _portable_enum(TranslationSource, "translation_source"),
        default=TranslationSource.HUMAN_CURATION,
        nullable=False,
    )


class Operation(Base):
    """Run of an Adversary in Caldera against the Docker environment."""

    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("campaigns.id"), nullable=True
    )
    caldera_operation_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    state: Mapped[OperationState] = mapped_column(
        _portable_enum(OperationState, "operation_state"),
        default=OperationState.NOT_STARTED,
        nullable=False,
    )
    # aggregate (Req. 4.5)
    total_success: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_failure: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime, nullable=True
    )


class AbilityResult(Base):
    """Per-Ability result of an Operation (status + command output).

    For the live technique-by-technique tracking of Stage 6 (Req. 2.4), the
    per-Ability result carries, besides the pre-existing ``status`` and
    ``command_output``, the fields shown in the UI. These additive, nullable
    columns are mapped by ``OperationRunner._reconcile`` from the Caldera
    ``OperationLink`` (which exposes ``command``, ``paw`` and ``output``):

    * ``command`` — the concrete command of the link.
    * ``target_container`` — e.g. ``"nginx (192.168.20.30)"``.
    * ``attacker_host`` — the attacker host (e.g. ``kali``), via ``link.paw`` /
      the agent.

    All three are nullable so the migration is purely additive and existing
    rows need no backfill; the serialization layer resolves an explicit
    "not available" indicator for any null/empty value (Req. 2.5).
    """

    __tablename__ = "ability_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("operations.id"), nullable=True
    )
    ability_id: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("abilities.ability_id"), nullable=True
    )
    status: Mapped[AbilityResultStatus] = mapped_column(
        _portable_enum(AbilityResultStatus, "ability_result_status"),
        default=AbilityResultStatus.PENDING,
        nullable=False,
    )
    # command stdout/stderr
    command_output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # concrete executed command of the link (Req. 2.4)
    command: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # target container + IP, e.g. "nginx (192.168.20.30)" (Req. 2.4)
    target_container: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # attacker host (e.g. "kali"), via link.paw / agent (Req. 2.4)
    attacker_host: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class AuditLogEntry(Base):
    """Persisted audit trail — Req. 6.8.

    ``command``, ``target_container`` and ``recorded_at`` are NOT NULL: each
    executed command produces exactly one record with the concrete command, the
    target container and the record timestamp.
    """

    __tablename__ = "audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("operations.id"), nullable=True
    )
    ability_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # concrete executed command
    command: Mapped[str] = mapped_column(Text, nullable=False)
    # e.g. "nginx (192.168.20.30)"
    target_container: Mapped[str] = mapped_column(String, nullable=False)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # success/failure + output
    recorded_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)


class SessionState(Base):
    """Session state — continuity across machines (Req. 9.1, 9.7).

    The completed stages per campaign and the results are derived from the
    ``StageRun``/``Operation`` rows associated with the corresponding campaign.
    """

    __tablename__ = "session_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    current_campaign: Mapped[Optional[str]] = mapped_column(
        String, ForeignKey("campaigns.id"), nullable=True
    )
    updated_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime, nullable=True
    )


class UserPreferences(Base):
    """Researcher theme and language preferences (Req. 7.6, 8.6)."""

    __tablename__ = "preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    theme: Mapped[Theme] = mapped_column(
        _portable_enum(Theme, "theme"),
        default=Theme.LIGHT,  # default Light mode (Req. 7.6)
        nullable=False,
    )
    language: Mapped[Language] = mapped_column(
        _portable_enum(Language, "language"),
        default=Language.PT_BR,  # default pt-BR (Req. 8.6)
        nullable=False,
    )


class EnvironmentParametersRow(Base):
    """Stage 4 environment parameters (Operationalization) — Req. 8.3.

    The second typed Output of Stage 4 (``EnvironmentParameters``) is persisted
    in a single portable JSON column (``content``), like ``AttackFlowRow.content``, avoiding
    unnecessary normalization (Req. 8.3, 17.1).

    ``platform`` persists as VARCHAR + CHECK (``native_enum=False``) from the
    consolidated :class:`EnvironmentPlatform`. Following the precedent of
    ``FlowOrigin``/``ai_generated``, this enum already declares
    ``virtual_machine``, so the generated CHECK admits it even though only
    ``container`` is implemented in the application (Req. 17.1): persisting a
    virtual-machine platform in the future requires no schema migration
    (Req. 17.2).

    ``origin`` is the explicit, extensible attribute (``TranslationSource``,
    VARCHAR + CHECK that already includes the enum values): a future
    ``ai_generated`` operationalization fits with no schema change (Req. 16.2,
    16.4).
    """

    __tablename__ = "environment_parameters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        String, ForeignKey("campaigns.id"), nullable=False
    )
    # The column type is derived from the consolidated ``EnvironmentPlatform``
    # (which already declares ``virtual_machine``), so the portable CHECK accepts
    # the VM from the first migration — no future migration (Req. 17.2). The
    # default ``EnvironmentPlatform.CONTAINER`` serializes to ``container``.
    platform: Mapped[EnvironmentPlatform] = mapped_column(
        _portable_enum(EnvironmentPlatform, "environment_platform"),
        default=EnvironmentPlatform.CONTAINER,
        nullable=False,
    )
    # ``EnvironmentParameters`` serialized (hosts/services/network_context).
    content: Mapped[Any] = mapped_column(JSON, nullable=False)
    origin: Mapped[TranslationSource] = mapped_column(
        _portable_enum(TranslationSource, "translation_source"),
        default=TranslationSource.HUMAN_CURATION,
        nullable=False,
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)


class ExecutionReport(Base):
    """Stage 6 report/metrics (execution, measurement and report) — Req. 15.7.

    The ``SufficiencyMetrics`` are persisted in a portable JSON column
    (``metrics``), like ``AttackFlowRow.content``. ``success_verdict`` stores the overall
    execution verdict (Req. 15.6) and ``environment_deprovisioned`` records
    whether the environment was deprovisioned at the end of Stage 6 (Req. 12.1).
    """

    __tablename__ = "execution_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        String, ForeignKey("campaigns.id"), nullable=False
    )
    # links to the executed Operation; nullable (a report can exist without an operation).
    operation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("operations.id"), nullable=True
    )
    # ``SufficiencyMetrics`` serialized (Req. 15.8).
    metrics: Mapped[Any] = mapped_column(JSON, nullable=False)
    success_verdict: Mapped[bool] = mapped_column(Boolean, nullable=False)
    environment_deprovisioned: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Structured teardown result from the ``DeprovisioningResult`` produced at
    # Stage 6 completion (Req. 7.3). ``deprovisioned`` records whether the
    # environment was removed; ``remaining_resources`` lists the resources that
    # remained when the teardown failed. Both are nullable so legacy/pre-teardown
    # rows (and reports persisted before the environment was torn down) stay valid.
    deprovisioned: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    remaining_resources: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    generated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)


# ---------------------------------------------------------------------------
# AI curation agent bookkeeping (task 3.1)
#
# The five tables below are the ONLY new persistence introduced by the AI
# curation agents: the agent-run bookkeeping (runs → iterations → per-stage
# outputs, plus the evaluator readings) and the pgvector experience memory.
# They follow the design's ER diagram and table notes exactly.
#
# Conventions carried over from the existing models:
#   * Every row has a non-null primary key and a non-null naive-UTC creation
#     timestamp (``created_at``) defaulted via :func:`_utcnow`.
#   * Portable enums persist as VARCHAR + CHECK via ``_portable_enum`` (no
#     native PostgreSQL ENUM), storing the enum *values*.
#   * FK links reuse the existing style (``ForeignKey("table.column")``) into
#     ``campaigns``, ``attack_flows``, ``environment_parameters`` and the Stage 6
#     ``execution_reports``.
#
# _Requirements: 11.1, 11.4, 11.8, 11.9_
# ---------------------------------------------------------------------------


class AgentRun(Base):
    """One AI curation run of a campaign (Req. 11.1, 11.4).

    ``thread_id`` is the LangGraph checkpoint thread key that makes the run
    resumable; it is non-null so every persisted run is addressable by its
    checkpoint thread (Req. 11.4). ``max_iterations`` is the loop guard, while
    ``iterations_used`` and ``final_verdict_success`` summarize the outcome.

    ``status`` persists as VARCHAR + CHECK (``native_enum=False``) from
    :class:`AgentRunStatus`, so the generated CHECK bounds the value set to the
    enum members (``pending`` / ``running`` / ``completed`` / ``failed`` /
    ``aborted``).
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        String, ForeignKey("campaigns.id"), nullable=False
    )
    status: Mapped[AgentRunStatus] = mapped_column(
        _portable_enum(AgentRunStatus, "agent_run_status"),
        default=AgentRunStatus.PENDING,
        nullable=False,
    )
    # Loop guard (default 3 — Req. 3.6) and the summary of the outcome.
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False)
    iterations_used: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    # LangGraph checkpoint thread key — non-null so the run is resumable.
    thread_id: Mapped[str] = mapped_column(String, nullable=False)
    # Overall verdict summary; nullable until the run reaches a terminal state.
    final_verdict_success: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    # Structured failure cause written by ``_fail_run`` (failing stage + the
    # absent-input/cause message) and surfaced by the read endpoint (Req. 2.6,
    # 2.7). Additive and nullable so existing rows need no backfill.
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


class AgentIteration(Base):
    """One pass through the plan→execute→measure→improve loop (Req. 11.5).

    Belongs to an :class:`AgentRun` via ``run_id`` (1 run → N iterations).
    ``index`` is the 0-based iteration number; ``refined_stage`` records which
    stage node the evaluator routed back into and is null on the first pass.

    ``refined_stage`` persists as VARCHAR + CHECK from :class:`AgentStage`
    (``flow`` / ``operationalization`` / ``environment``).
    """

    __tablename__ = "agent_iterations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_runs.id"), nullable=False
    )
    # 0-based iteration number within the run.
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Which stage the evaluator refined into this iteration (null on first pass).
    refined_stage: Mapped[Optional[AgentStage]] = mapped_column(
        _portable_enum(AgentStage, "agent_stage"),
        nullable=True,
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


class AgentStageOutput(Base):
    """The per-stage agent artifact of an iteration (Req. 11.5).

    Belongs to an :class:`AgentIteration` via ``iteration_id`` (1 iteration →
    N stage outputs). The serialized artifact (``ProducedFlow`` |
    ``ProducedOperationalization`` | ``ProvisionedEnvironment``) lives in the
    portable JSON ``payload`` column. Where it corresponds to an existing
    persisted entity it is linked via ``flow_id`` (the AI-origin ``AttackFlowRow`` row
    persisted by the flow agent) or ``params_id`` (the
    ``EnvironmentParameters`` row); both FKs are nullable because only one
    applies per stage.

    ``origin`` is always ``ai_generated`` here — persisted via the existing
    :class:`TranslationSource` portable enum, whose CHECK already admits
    ``ai_generated``, so no schema migration is required to store AI artifacts
    (Req. 11.8).
    """

    __tablename__ = "agent_stage_outputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    iteration_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_iterations.id"), nullable=False
    )
    stage: Mapped[AgentStage] = mapped_column(
        _portable_enum(AgentStage, "agent_stage"),
        nullable=False,
    )
    # Always ``ai_generated`` for AI-produced artifacts (Req. 11.8); reuses the
    # existing portable origin enum whose CHECK already includes the value.
    origin: Mapped[TranslationSource] = mapped_column(
        _portable_enum(TranslationSource, "translation_source"),
        default=TranslationSource.AI_GENERATED,
        nullable=False,
    )
    # Serialized stage artifact (ProducedFlow | ProducedOperationalization |
    # ProvisionedEnvironment).
    payload: Mapped[Any] = mapped_column(JSON, nullable=False)
    # Linked AI-origin AttackFlowRow row (when this is a flow stage output); nullable.
    flow_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("attack_flows.id"), nullable=True
    )
    # Linked EnvironmentParameters row (when this is an operationalization
    # output); nullable.
    params_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("environment_parameters.id"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


class AgentEvaluation(Base):
    """The evaluator's reading of the Stage 6 report for an iteration (Req. 11.9).

    Belongs to an :class:`AgentIteration` via ``iteration_id`` (0..1 per
    iteration). ``report_id`` links the Stage 6 ``execution_reports`` row the
    evaluator keyed on. The metrics it read (``success_rate``,
    ``procedural_coverage``, ``verdict_success``, ``failed_nodes``) are stored
    alongside the ``decision`` that drives the conditional edge and the LLM
    ``rationale`` kept for auditability.

    ``decision`` persists as VARCHAR + CHECK from :class:`EvaluatorDecision`
    (``refinar`` / ``finalizar``). ``rationale`` is bounded to 10,000 characters
    (Req. 11.9) with both a ``String`` length and a portable ``CHECK`` so the
    bound is enforced on PostgreSQL and the test SQLite alike.
    """

    __tablename__ = "agent_evaluations"

    __table_args__ = (
        CheckConstraint(
            f"length(rationale) <= {_RATIONALE_MAX_LENGTH}",
            name="ck_agent_evaluations_rationale_max_length",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    iteration_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_iterations.id"), nullable=False
    )
    # Stage 6 execution report the evaluator read.
    report_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("execution_reports.id"), nullable=True
    )
    success_rate: Mapped[float] = mapped_column(Float, nullable=False)
    procedural_coverage: Mapped[float] = mapped_column(Float, nullable=False)
    verdict_success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Serialized list of the attack flow nodes that failed (drives the refine target).
    failed_nodes: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    decision: Mapped[EvaluatorDecision] = mapped_column(
        _portable_enum(EvaluatorDecision, "evaluator_decision"),
        nullable=False,
    )
    # LLM-produced reasoning, bounded to 10,000 chars (Req. 11.9).
    rationale: Mapped[str] = mapped_column(
        String(_RATIONALE_MAX_LENGTH), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


class ExperienceMemory(Base):
    """Long-term RAG experience memory over pgvector (Req. 11.1).

    One row per stored curation outcome of a campaign (1 campaign → N rows).
    ``embedding`` is a fixed-width pgvector ``VECTOR(EMBEDDING_DIM)`` column (the
    width resolved from the ``AI_EMBEDDING_DIM`` setting, design default 1024)
    retrieved by cosine similarity; ``content`` is the human-readable curation
    snippet (technique, command, node, params); ``metadata`` carries the
    technique id, verdict and metrics used to filter retrieval.

    ``stage`` and ``outcome`` persist as VARCHAR + CHECK via the portable enums
    (:class:`AgentStage` and :class:`AbilityResultStatus` — whose ``success`` /
    ``failure`` values bound the stored outcome).
    """

    __tablename__ = "experience_memory"

    __table_args__ = (
        # The outcome is bounded to exactly two values (``success | failure``,
        # the two terminal ``AbilityResultStatus`` values). No dedicated
        # two-value outcome enum exists, so we bound the portable VARCHAR column
        # with an explicit CHECK rather than widening a four-member enum. The
        # English value set matches migration ``b2c3d4e5f6a7``, which recreates
        # this CHECK as ``IN ('success', 'failure')``.
        CheckConstraint(
            "outcome IN ('success', 'failure')",
            name="ck_experience_memory_outcome",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        String, ForeignKey("campaigns.id"), nullable=False
    )
    stage: Mapped[AgentStage] = mapped_column(
        _portable_enum(AgentStage, "agent_stage"),
        nullable=False,
    )
    # Outcome of the stored curation, bounded to ``success | failure`` by the
    # table CHECK above (the AbilityResultStatus.SUCCESS/FAILURE values).
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    # Human-readable curation snippet (technique, command, node, params).
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # pgvector fixed-width embedding, dim = AI_EMBEDDING_DIM (default 1024).
    embedding: Mapped[Any] = mapped_column(Vector(_embedding_dim()), nullable=False)
    # technique_id, verdict and metrics used to filter retrieval.
    meta: Mapped[Any] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


class AgentStageTrace(Base):
    """Per-stage-output Audit_Trace — the Agent_Input + processing (Req. 2.1, 2.7).

    One row per :class:`AgentStageOutput`, in a strict 1:1 relationship via the
    non-null ``stage_output_id`` FK: the FK carries a UNIQUE constraint so each
    stage output has at most one trace (design §Data Models — Audit_Trace).
    Keeping the trace in its own table isolates the large, optional
    prompt/response/reasoning payload from the hot ``agent_stage_outputs`` path.

    The trace content is stored in two portable JSON columns, following the
    precedent of ``AgentStageOutput.payload``:

    * ``agent_input`` — the data the agent received (prior-stage artifacts,
      campaign context, retrieved cross-campaign experience, the ATT&CK/STIX
      attack context).
    * ``processing`` — what the agent performed (the LLM prompt and response,
      reasoning, tools invoked, repair attempts, and for the Environment agent
      the fixed-vs-agent-decided split plus the gate verdicts).

    ``created_at`` is the naive-UTC creation timestamp defaulted via
    :func:`_utcnow`, matching the other AI bookkeeping rows.
    """

    __tablename__ = "agent_stage_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 1:1 with the stage output: non-null FK + UNIQUE so each output has at
    # most one trace row.
    stage_output_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("agent_stage_outputs.id"),
        nullable=False,
        unique=True,
    )
    # Agent_Input: prior artifacts, campaign context, retrieved experience,
    # ATT&CK/STIX attack context.
    agent_input: Mapped[Any] = mapped_column(JSON, nullable=False)
    # Processing: llm_prompt, llm_response, reasoning, tools, repair_attempts,
    # and the environment fixed-vs-agent-decided + gate verdicts.
    processing: Mapped[Any] = mapped_column(JSON, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# STIX package persistence (task 1.1)
#
# The six tables below persist a downloaded STIX base ("package") and the
# content derived from it by ``StixService`` (type counts, campaigns, the
# techniques each campaign uses, intrusion-set actors and the actor→campaign
# attributions). They implement exactly the Data Models section of the
# stix-package-persistence-and-overview design.
#
# ``stix_packages.version`` is the stable upsert key that makes re-downloading
# a version idempotent: re-persisting a version reuses its row and replaces the
# derived child rows (delete-and-insert) within a single transaction.
#
# Conventions carried over from the existing models:
#   * Every row has an Integer surrogate primary key.
#   * Portable generic types only (String / Text / Integer / Boolean /
#     DateTime / JSON) so the mapping runs identically on PostgreSQL (the
#     product database) and the ephemeral SQLite used by the unit tests. No
#     ``VECTOR`` column is created here (embeddings are a future phase).
#   * ``downloaded_at`` is a naive-UTC timestamp defaulted via :func:`_utcnow`,
#     matching the project-wide convention, and is refreshed on every
#     re-persistence.
#   * Composite UNIQUE constraints are declared through ``__table_args__``,
#     following the precedent of the AI bookkeeping tables.
#
# _Requirements: 1.2, 4.6, 10.1, 10.5, 11.1, 11.2, 11.3_
# ---------------------------------------------------------------------------


class StixPackage(Base):
    """Root row per downloaded STIX version (design Model 1).

    One row per downloaded base. ``version`` is UNIQUE and NOT NULL — it is the
    stable key that guarantees idempotency of the re-download: persisting the
    same version reuses the existing row. ``name`` is NOT NULL (e.g.
    ``"Enterprise ATT&CK v{version}"``). ``downloaded_at`` is a NOT NULL
    naive-UTC timestamp (project convention via :func:`_utcnow`) refreshed on
    every re-persistence.
    """

    __tablename__ = "stix_packages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Stable upsert key: UNIQUE + NOT NULL (e.g. "18.0").
    version: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # Human-readable name, e.g. "Enterprise ATT&CK v18.0".
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Naive-UTC download timestamp (project convention); refreshed on every
    # re-persistence of the same version.
    downloaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


class StixTypeCount(Base):
    """Per-type object counts of a package (design Model 2).

    Projection of ``DashboardStix.counts`` so the overview cards are served
    without reprocessing the bundle. One row per (``package_id``, ``stix_type``).
    ``count`` is ``>= 0`` and ``description`` is non-empty (reusing
    ``TYPE_DESCRIPTIONS``).
    """

    __tablename__ = "stix_type_counts"

    __table_args__ = (
        UniqueConstraint(
            "package_id", "stix_type", name="uq_stix_type_counts_package_type"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    package_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stix_packages.id"), nullable=False
    )
    # STIX object type, e.g. "attack-pattern".
    stix_type: Mapped[str] = mapped_column(String, nullable=False)
    # Human label for the type, e.g. "tecnicas".
    label: Mapped[str] = mapped_column(String, nullable=False)
    # Number of objects of this type in the bundle (>= 0).
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Non-empty description reused from TYPE_DESCRIPTIONS.
    description: Mapped[str] = mapped_column(String, nullable=False)


class StixCampaign(Base):
    """One campaign of the bundle, with curated flag and identity (design Model 3).

    One row per ``campaign`` object of the bundle. ``curated`` is NOT NULL
    (default ``False``) and holds ``True`` if and only if ``curated_slug`` is
    not null (consistency with ``StixService._resolve_curated_slug``). The
    ``description`` / ``aliases`` columns are persisted as Text / JSON to be a
    clean source for future embeddings (no ``VECTOR`` column). Unique per
    (``package_id``, ``stix_id``).
    """

    __tablename__ = "stix_campaigns"

    __table_args__ = (
        UniqueConstraint(
            "package_id", "stix_id", name="uq_stix_campaigns_package_stix_id"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    package_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stix_packages.id"), nullable=False
    )
    # STIX id of the campaign object.
    stix_id: Mapped[str] = mapped_column(String, nullable=False)
    # Campaign name; falls back to stix_id.
    name: Mapped[str] = mapped_column(String, nullable=False)
    # External id, e.g. "C0045".
    external_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Free text persisted for future embeddings.
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # List of alias strings; persisted as JSON for future embeddings.
    aliases: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # Curated flag: True iff curated_slug is not null (default False).
    curated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Curated slug when the campaign is recognized; nullable.
    curated_slug: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # ISO timestamps carried over from the bundle; nullable.
    first_seen: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_seen: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class StixCampaignTechnique(Base):
    """ATT&CK techniques a campaign uses (design Model 4).

    The ``attack-pattern`` objects a campaign uses (via ``uses`` relationships).
    ``description`` is persisted as Text for future embeddings. Unique per
    (``campaign_id``, ``technique_id``).
    """

    __tablename__ = "stix_campaign_techniques"

    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "technique_id",
            name="uq_stix_campaign_techniques_campaign_technique",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stix_campaigns.id"), nullable=False
    )
    # ATT&CK technique id, e.g. "T1059".
    technique_id: Mapped[str] = mapped_column(String, nullable=False)
    technique_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    tactic: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Free text persisted for future embeddings.
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class StixActor(Base):
    """One intrusion-set of the bundle (design Model 5).

    One row per ``intrusion-set`` object. ``name`` is NOT NULL (falls back to
    ``stix_id``). Unique per (``package_id``, ``stix_id``).
    """

    __tablename__ = "stix_actors"

    __table_args__ = (
        UniqueConstraint(
            "package_id", "stix_id", name="uq_stix_actors_package_stix_id"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    package_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stix_packages.id"), nullable=False
    )
    stix_id: Mapped[str] = mapped_column(String, nullable=False)
    # Actor name; falls back to stix_id.
    name: Mapped[str] = mapped_column(String, nullable=False)


class StixActorCampaign(Base):
    """Actor→campaign attribution (design Model 6).

    Attribution relation derived from the ``attributed-to`` relationships.
    Unique per (``actor_id``, ``campaign_id``) — no duplicates of the same
    attribution.
    """

    __tablename__ = "stix_actor_campaigns"

    __table_args__ = (
        UniqueConstraint(
            "actor_id",
            "campaign_id",
            name="uq_stix_actor_campaigns_actor_campaign",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stix_actors.id"), nullable=False
    )
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stix_campaigns.id"), nullable=False
    )


# ---------------------------------------------------------------------------
# Command_Base / provisioning tables (task 9.1)
#
# The relational/exact half of the ``Command_Base``: the mapping from an ATT&CK
# technique to the concrete commands that exercise it, sourced from Atomic Red
# Team ingestion or threat-intel uploads. The associative affinities live in
# ``experience_memory`` (the Vector_Store) per the Memory_Rule — no new table is
# created for those.
#
# Conventions carried over from the existing models:
#   * Integer surrogate primary key.
#   * Portable generic types only (String / Text / Integer / DateTime), so the
#     mapping runs identically on PostgreSQL (the product database) and the
#     ephemeral SQLite used by the unit tests. No ``VECTOR`` column here.
#   * ``created_at`` is a naive-UTC timestamp defaulted via :func:`_utcnow`,
#     matching the project-wide convention.
#   * The composite UNIQUE constraint is declared through ``__table_args__``,
#     following the precedent of the STIX/AI bookkeeping tables.
#
# _Requirements: 7.2, 13.3, 9.4_
# ---------------------------------------------------------------------------


class TechniqueCommand(Base):
    """Technique → concrete command mapping — the exact half of the Command_Base.

    One row per concrete command that exercises an ATT&CK technique, ingested
    from Atomic Red Team or a threat-intel upload (Req. 7.2, 13.3). There is no
    FK to ``campaigns``: the row is keyed by the ATT&CK ``technique_id`` (e.g.
    ``T1059``), not by a campaign, so the mapping is shared across campaigns.

    ``source`` records the provenance (``atomic_red_team`` | ``threat_intel``)
    as a portable ``String`` rather than an enum — the ingestion services own
    the value set and the design specifies a plain ``String`` column.

    ``command_hash`` is a deterministic hash of the normalized ``command``; it
    is the stable component of the idempotency key so re-ingesting the same
    command for the same technique and source reuses the existing row rather
    than duplicating it (Req. 7.6, 13.6).
    """

    __tablename__ = "technique_commands"

    __table_args__ = (
        # Idempotency key: re-ingestion does not duplicate the same command for
        # the same technique and source (Req. 7.6, 13.6).
        UniqueConstraint(
            "technique_id",
            "command_hash",
            "source",
            name="uq_technique_commands_technique_hash_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # ATT&CK technique id, e.g. "T1059".
    technique_id: Mapped[str] = mapped_column(String, nullable=False)
    # Concrete command that exercises the technique.
    command: Mapped[str] = mapped_column(Text, nullable=False)
    # Executor of the command, e.g. "sh" / "powershell" / "cmd".
    executor: Mapped[str] = mapped_column(String, nullable=False)
    # Target platform, e.g. "linux" / "windows".
    platform: Mapped[str] = mapped_column(String, nullable=False)
    # Provenance: "atomic_red_team" | "threat_intel".
    source: Mapped[str] = mapped_column(String, nullable=False)
    # Deterministic hash of the normalized command (idempotency key component).
    command_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Image_Catalog table (task 9.2)
#
# The offline fallback catalog for the ImageResolver: when Docker Hub is
# unreachable, the Environment agent resolves a container image for a campaign's
# target OS from this table instead of the live registry (Req. 14.4). Each row
# names an image for a given target OS, optionally listing the services/ports it
# covers and free-form notes.
#
# Conventions carried over from the existing models:
#   * Integer surrogate primary key.
#   * Portable generic types only (String / Text / Integer / JSON / DateTime),
#     so the mapping runs identically on PostgreSQL (the product database) and
#     the ephemeral SQLite used by the unit tests. No ``VECTOR`` column here.
#   * ``created_at`` is a naive-UTC timestamp defaulted via :func:`_utcnow`,
#     matching the project-wide convention.
#   * The composite UNIQUE constraint is declared through ``__table_args__``,
#     following the precedent of the STIX/AI bookkeeping tables.
#
# _Requirements: 14.4, 9.4_
# ---------------------------------------------------------------------------


class ImageCatalog(Base):
    """Offline image catalog — the ImageResolver fallback (Req. 14.4).

    One row per container image offered for a given target OS. When Docker Hub
    is unreachable, the ImageResolver falls back to this catalog to resolve an
    image for the campaign's ``target_os`` (``linux`` | ``windows``) rather than
    the live registry.

    ``services`` is an optional JSON column listing the services/ports the image
    covers (e.g. the exposed services of a ``nginx:1.25`` image); ``notes`` is
    optional free-form text. ``target_os`` is a portable ``String`` rather than
    an enum — the resolver owns the value set and the design specifies a plain
    ``String`` column.

    The composite UNIQUE ``(image, target_os)`` is the idempotency key: a given
    image is cataloged at most once per target OS, so re-seeding the catalog
    reuses the existing row rather than duplicating it.
    """

    __tablename__ = "image_catalog"

    __table_args__ = (
        # Idempotency key: a given image is cataloged at most once per target OS.
        UniqueConstraint(
            "image",
            "target_os",
            name="uq_image_catalog_image_target_os",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Container image reference, e.g. "nginx:1.25".
    image: Mapped[str] = mapped_column(String, nullable=False)
    # Target OS the image is cataloged for: "linux" | "windows".
    target_os: Mapped[str] = mapped_column(String, nullable=False)
    # Optional services/ports the image covers; portable JSON, nullable.
    services: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # Optional free-form notes about the image; nullable.
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Provisioning_Map table (task 9.3)
#
# The relational/exact half of the Provisioning_Map: the recommended target
# environment (fixed inventory of images/services/topology) derived per
# campaign from the techniques/platforms of its abilities (Req. 8.1, 8.2). The
# "environments that worked" associations live in ``experience_memory`` (the
# Vector_Store) per the Memory_Rule — no new table is created for those
# (Req. 8.3).
#
# Conventions carried over from the existing models:
#   * Integer surrogate primary key.
#   * ``campaign_id`` String FK into ``campaigns.id`` (NOT NULL), following the
#     precedent of ``EnvironmentParametersRow``.
#   * Portable generic types only (String / JSON / DateTime), so the mapping
#     runs identically on PostgreSQL (the product database) and the ephemeral
#     SQLite used by the unit tests. No ``VECTOR`` column here.
#   * ``created_at`` / ``updated_at`` are naive-UTC timestamps defaulted via
#     :func:`_utcnow`, matching the project-wide convention.
#   * The single-column UNIQUE constraint is declared through ``__table_args__``,
#     following the precedent of the STIX/AI bookkeeping tables.
#
# _Requirements: 8.2, 9.4_
# ---------------------------------------------------------------------------


class ProvisioningMap(Base):
    """Recommended target environment per campaign — the Provisioning_Map (Req. 8.2).

    One row per campaign carrying the fixed inventory (images/services/topology)
    derived from the techniques/platforms of the campaign's abilities (Req. 8.1).
    ``campaign_id`` is a NOT NULL FK into ``campaigns.id`` (the same FK style
    used by ``EnvironmentParametersRow``).

    ``target_os`` is a portable ``String`` rather than an enum — the
    ``ProvisioningMapService`` owns the value set (``linux`` | ``windows``) and
    the design specifies a plain ``String`` column. ``content`` is the fixed
    inventory serialized in a single portable JSON column, like ``AttackFlowRow.content``.

    The single-column UNIQUE ``(campaign_id)`` is the idempotency key: one map
    per campaign (upsert latest-wins), consistent with
    ``ContainerEmulability`` — re-deriving the map updates the campaign's
    existing row rather than duplicating it.
    """

    __tablename__ = "provisioning_map"

    __table_args__ = (
        # Idempotency key: one map per campaign (upsert latest-wins).
        UniqueConstraint(
            "campaign_id",
            name="uq_provisioning_map_campaign_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        String, ForeignKey("campaigns.id"), nullable=False
    )
    # Target OS derived for the campaign: "linux" | "windows".
    target_os: Mapped[str] = mapped_column(String, nullable=False)
    # Fixed inventory (images/services/topology derived); portable JSON.
    content: Mapped[Any] = mapped_column(JSON, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Technique_Emulability / Container_Emulability tables (task 12.1)
#
# The container-emulability knowledge for the agentic-phase-model-refactor
# spec: per-technique classification (Technique_Emulability) and its per-campaign
# derivation (Container_Emulability). The Container_Emulability_Service (task
# 12.2) reads the per-campaign techniques, marks the campaign container-emulable
# iff every technique is ``container`` or ``dispensable``, lists the blocking
# techniques otherwise, and upserts idempotently by ``campaign_id``.
#
# Conventions carried over from the existing models:
#   * ``technique_emulability`` is keyed by the ATT&CK ``technique_id`` as a
#     logical primary key (a plain String PK, e.g. "T1059") — the classification
#     is shared across campaigns, so there is no FK to ``campaigns``.
#   * ``container_emulability`` carries a single-column UNIQUE on ``campaign_id``
#     (the idempotency key — one row per campaign, upsert latest-wins),
#     following the precedent of ``ProvisioningMap``.
#   * The ``emulability`` enum persists as VARCHAR + CHECK via ``_portable_enum``
#     (``native_enum=False``), storing the enum *values*, so the generated CHECK
#     bounds the value set to ``container`` | ``dispensable`` | ``not_container``
#     on PostgreSQL (the product database) and the test SQLite alike.
#   * All other columns use portable generic types (String / Text / Boolean /
#     JSON), so the mapping runs identically on both backends. No ``VECTOR``
#     column here.
#
# _Requirements: 6.1, 6.3_
# ---------------------------------------------------------------------------


class TechniqueEmulability(Base):
    """Per-technique container-emulability classification (Req. 6.1).

    One row per ATT&CK technique. ``technique_id`` is the logical primary key
    (e.g. ``"T1059"``); the classification is shared across campaigns, so there
    is no FK to ``campaigns``. ``emulability`` is one of ``container`` |
    ``dispensable`` | ``not_container`` (:class:`TechniqueEmulabilityKind`),
    persisted as VARCHAR + CHECK via ``_portable_enum`` so the generated CHECK
    bounds the value set.

    The Container_Emulability_Service derives a campaign's emulability from these
    rows: a campaign is container-emulable iff every one of its techniques is
    ``container`` or ``dispensable`` (Req. 6.2).
    """

    __tablename__ = "technique_emulability"

    # ATT&CK technique id as the logical PK, e.g. "T1059".
    technique_id: Mapped[str] = mapped_column(String, primary_key=True)
    # Container-emulability class: "container" | "dispensable" | "not_container".
    emulability: Mapped[TechniqueEmulabilityKind] = mapped_column(
        _portable_enum(TechniqueEmulabilityKind, "technique_emulability_kind"),
        nullable=False,
    )


class ContainerEmulability(Base):
    """Per-campaign container-emulability marking — the derived verdict (Req. 6.3).

    One row per campaign carrying whether all of its techniques are emulable in
    a container (or dispensable). ``campaign_id`` carries a single-column UNIQUE
    constraint — the idempotency key — so re-deriving the campaign's emulability
    updates the existing row rather than duplicating it (upsert latest-wins),
    consistent with ``ProvisioningMap``.

    ``container_emulable`` is ``True`` iff every technique of the campaign is
    ``container`` or ``dispensable`` (Req. 6.2). ``blocking_techniques`` is an
    optional JSON list of the technique ids that block container emulation
    (the ``not_container`` ones) — the source for the accessible indication the
    UI shows for a non-emulable campaign (Req. 6.5). It is null/empty when the
    campaign is container-emulable.
    """

    __tablename__ = "container_emulability"

    __table_args__ = (
        # Idempotency key: one row per campaign (upsert latest-wins).
        UniqueConstraint(
            "campaign_id",
            name="uq_container_emulability_campaign_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String, nullable=False)
    # True iff every technique of the campaign is container/dispensable (Req. 6.2).
    container_emulable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Optional JSON list of the technique ids that block container emulation;
    # null/empty when the campaign is container-emulable (Req. 6.5).
    blocking_techniques: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)


class AttackFlowObjectRow(Base):
    """One object of a persisted attack flow (action, condition, operator, asset).

    ``stix_id`` is the STIX-style id (``attack-action--<uuid>``); it is unique
    within a flow. ``technique_id`` is indexed so the relational half of the
    flow retriever can find flows by technique.
    """

    __tablename__ = "attack_flow_objects"

    __table_args__ = (
        UniqueConstraint(
            "flow_id", "stix_id", name="uq_attack_flow_objects_flow_id_stix_id"
        ),
        Index("ix_attack_flow_objects_technique_id", "technique_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    flow_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("attack_flows.id", ondelete="CASCADE"), nullable=False
    )
    stix_id: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    technique_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    tactic_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    properties: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)


class AttackFlowEdgeRow(Base):
    """One directed edge of a persisted attack flow.

    ``is_back_edge`` marks edges removed to derive the acyclic execution view.
    """

    __tablename__ = "attack_flow_edges"

    __table_args__ = (
        Index("ix_attack_flow_edges_flow_id", "flow_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    flow_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("attack_flows.id", ondelete="CASCADE"), nullable=False
    )
    source_stix_id: Mapped[str] = mapped_column(String, nullable=False)
    target_stix_id: Mapped[str] = mapped_column(String, nullable=False)
    # "effect" for control edges, "asset" for action -> asset references.
    kind: Mapped[str] = mapped_column(String, nullable=False, default="effect")
    is_back_edge: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_false(), nullable=False
    )


class IngestSourceState(Base):
    """Persisted ingestion state of one knowledge source (one row per source).

    Also acts as the per-source lock: the orchestrator moves a row to
    ``running`` atomically and refreshes ``heartbeat_at`` while it works.
    """

    __tablename__ = "ingest_source_state"

    source: Mapped[IngestSource] = mapped_column(
        _portable_enum(IngestSource, "ingest_source"), primary_key=True
    )
    status: Mapped[IngestStatus] = mapped_column(
        _portable_enum(IngestStatus, "ingest_status"),
        default=IngestStatus.PENDING,
        nullable=False,
    )
    loaded_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    embedded_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    expected_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Short failure cause, never containing secrets.
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # List of {item, reason} for items skipped during ingestion.
    skipped: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
