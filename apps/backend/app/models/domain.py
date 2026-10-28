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
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    AbilityResultStatus,
    AgentRunStatus,
    AgentStage,
    DagOrigin,
    EnvironmentPlatform,
    EvaluatorDecision,
    Language,
    OperationState,
    PersistedEnvironmentPlatform,
    StageState,
    Theme,
    TranslationSource,
)


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp (portable across PostgreSQL and the test SQLite).

    Matches the convention used across the service layer (``StageService``,
    ``DagService``, ``OperationRunner``, ...): the AI bookkeeping rows stamp
    their creation/update timestamps with a timezone-naive UTC value so the
    column stays portable between PostgreSQL (the product database) and the
    ephemeral SQLite used by the unit tests.
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


# Design default for the pgvector embedding width (``AI_EMBEDDING_DIM``); mirrors
# the default declared by ``app.services.ai.config.SepinfSettings``.
_DEFAULT_EMBEDDING_DIM = 1024


def _embedding_dim() -> int:
    """Resolve the pgvector embedding dimension (``AI_EMBEDDING_DIM``, default 1024).

    The ``experience_memory.embedding`` column is a fixed-width
    ``VECTOR(EMBEDDING_DIM)``; the width comes from the ``AI_EMBEDDING_DIM``
    setting, exactly as :class:`app.services.ai.config.SepinfSettings` reads it
    (process environment first, then the repository-root ``.env``, else the
    design default of 1024).

    The models layer is intentionally dependency-free of the services layer
    (importing ``app.services.ai.config`` here would create an import cycle via
    ``app.services.__init__``), so the lookup is reproduced locally against the
    environment and the root ``.env``. This performs no network or socket I/O,
    keeping the model import cheap and side-effect free.
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
    """Curated campaign — maps the file pair in ``data/api/`` and the DAG.

    ``translation_source`` is an explicit, extensible attribute (Req. 11.1/11.2).
    """

    __tablename__ = "campaigns"

    # slug: "shadowray", "apt41_dust", ...
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)  # "ShadowRay"
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # path data/api/{campaign}_dag-ability.json
    ability_file: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # path data/api/{campaign}_dag-adversary.json
    adversary_file: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # path data/dag/{campaign}_dag.json
    dag_file: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    translation_source: Mapped[TranslationSource] = mapped_column(
        _portable_enum(TranslationSource, "translation_source"),
        default=TranslationSource.HUMAN_CURATION,
        nullable=False,
    )


class Dag(Base):
    """First-class persisted DAG, multi-origin (Req. 4).

    A Campaign (``Case``) can have N DAGs: each row references a ``Case`` by
    ``campaign_id`` (FK → ``campaigns.id``), establishing the 1 Campaign → N Dags
    relation (Req. 4.1, 4.2) — in the same FK style already used by ``Ability``/
    ``Adversary``/``StageRun``.

    ``origin`` is an explicit, extensible attribute (``DagOrigin``), persisted
    with ``native_enum=False`` (VARCHAR + CHECK) via ``_portable_enum``: the
    generated CHECK already includes ``gerado_ia``, so inserting a ``gerado_ia``
    DAG in the future requires no schema migration (Req. 4.3, 4.6).

    The graph content (``structural_nodes`` + ``edges``) is stored in a single
    portable JSON column, equivalent to the content of
    ``data/dag/{slug}_dag.json`` (Req. 4.4).
    """

    __tablename__ = "dags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        String, ForeignKey("campaigns.id"), nullable=False
    )
    origin: Mapped[DagOrigin] = mapped_column(
        _portable_enum(DagOrigin, "dag_origin"),
        default=DagOrigin.HUMAN_CURATION,
        nullable=False,
    )
    # readable DAG identifier/version (Req. 4.4); for curation,
    # derived from the file (e.g. "curadoria-v1")
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
    # DAG chosen for this run (filled in Stage 1 — Req. 4.7, 6.3).
    # Nullable: Stages 2/3/4 and old runs have no value.
    chosen_dag_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("dags.id"), nullable=True
    )
    updated_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime, nullable=True
    )


class Ability(Base):
    """Maps ``data/api/{campaign}_dag-ability.json`` (list of abilities).

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
    """Maps ``data/api/{campaign}_dag-adversary.json`` (single object).

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


class StixVersion(Base):
    """Curated-version flag per STIX version (Req. 5.5, 5.6).

    Persistable source of the curated-version flag. The ``CURATED_VERSION``
    constant (in :mod:`app.services.stix.stix_service`) is the single source in
    code and seeds this table in the migration; here we only define the portable
    schema.

    * ``version`` is the PK (e.g. ``"18.0"``).
    * ``curated`` marks the version as curated (``True`` for version 18 — Req. 5.5).
    * ``curated_artifacts_path`` points to the version's curated artifacts
      (Req. 5.6), ``None`` when the version has no associated curated artifacts.
    """

    __tablename__ = "stix_versions"

    version: Mapped[str] = mapped_column(String, primary_key=True)  # e.g. "18.0"
    curated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    curated_artifacts_path: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )


class EnvironmentParametersRow(Base):
    """Stage 4 environment parameters (Operationalization) — Req. 8.3.

    The second typed Output of Stage 4 (``EnvironmentParameters``) is persisted
    in a single portable JSON column (``content``), like ``Dag.content``, avoiding
    unnecessary normalization (Req. 8.3, 17.1).

    ``platform`` persists as VARCHAR + CHECK (``native_enum=False``) from
    ``PersistedEnvironmentPlatform``. Following the precedent of
    ``DagOrigin``/``gerado_ia``, this persistence enum already declares
    ``maquina_virtual``, so the generated CHECK admits it even though only
    ``conteiner`` is implemented in the application (Req. 17.1): persisting a
    virtual-machine platform in the future requires no schema migration
    (Req. 17.2).

    ``origin`` is the explicit, extensible attribute (``TranslationSource``,
    VARCHAR + CHECK that already includes the enum values): a future
    ``gerado_ia`` operationalization fits with no schema change (Req. 16.2, 16.4).
    """

    __tablename__ = "environment_parameters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        String, ForeignKey("campaigns.id"), nullable=False
    )
    # The column type is derived from ``PersistedEnvironmentPlatform`` (which
    # already declares ``maquina_virtual``), so the portable CHECK accepts the VM
    # from the first migration — no future migration (Req. 17.2). The value
    # ``conteiner`` matches ``EnvironmentPlatform.CONTAINER``, so writing the
    # public enum stays valid.
    platform: Mapped[PersistedEnvironmentPlatform] = mapped_column(
        _portable_enum(PersistedEnvironmentPlatform, "environment_platform"),
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


class ProvisionedEnvironmentRow(Base):
    """Persisted Stage 5 (environment creation) Output — Req. 6.3, 1.6.

    The typed artifact
    :class:`~app.services.environment.provisioner.ProvisionedEnvironment` produced
    when Stage 5 provisioning becomes **ready** (healthy containers + Abilities
    loaded + isolation approved) is persisted here, in a single portable JSON
    column (``content``) associated with the Campaign, exactly in the precedent of
    ``Dag.content`` / ``EnvironmentParametersRow.content`` /
    ``ExecutionReport.metrics`` — so Stage 6 and the screen consume the Output
    **after completion** (Req. 6.3) and provisioning is **reused** without
    bringing up a new one when the environment is already ready (idempotency —
    Req. 1.6).

    ``platform`` persists as VARCHAR + CHECK (``native_enum=False``) from
    ``PersistedEnvironmentPlatform`` — like ``EnvironmentParametersRow.platform`` —
    so the generated CHECK already accepts ``maquina_virtual`` with no future
    migration (Req. 17.2). The value ``conteiner`` matches
    ``EnvironmentPlatform.CONTAINER``, so writing the public enum stays valid.

    ``origin`` is the explicit, extensible attribute (``TranslationSource``,
    VARCHAR + CHECK that already includes ``gerado_ia``): a future ``gerado_ia``
    provisioning fits with no schema change (Req. 16.2, 16.4).

    ``ready`` stores the acceptance verdict of the persisted Output (the three
    prerequisites passed) — the signal Stage 5 idempotency reads to reuse the
    environment (Req. 1.6). A single row per Campaign (latest-row-wins upsert):
    persistence always updates the Campaign's existing row.
    """

    __tablename__ = "provisioned_environment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        String, ForeignKey("campaigns.id"), nullable=False
    )
    # The column type is derived from ``PersistedEnvironmentPlatform`` (which
    # already declares ``maquina_virtual``), so the portable CHECK accepts the VM
    # from the first migration — no future migration (Req. 17.2). The value
    # ``conteiner`` matches ``EnvironmentPlatform.CONTAINER``, so writing the
    # public enum stays valid.
    platform: Mapped[PersistedEnvironmentPlatform] = mapped_column(
        _portable_enum(PersistedEnvironmentPlatform, "environment_platform"),
        default=EnvironmentPlatform.CONTAINER,
        nullable=False,
    )
    # ``ProvisionedEnvironment`` serialized via ``model_dump(mode="json")``
    # (platform, target containers, compose_file, isolation, health,
    # loaded_abilities, origin) — portable Stage 5 Output (Req. 6.3).
    content: Mapped[Any] = mapped_column(JSON, nullable=False)
    origin: Mapped[TranslationSource] = mapped_column(
        _portable_enum(TranslationSource, "translation_source"),
        default=TranslationSource.HUMAN_CURATION,
        nullable=False,
    )
    # Acceptance verdict of the Output (the three prerequisites passed) — the
    # "ready+persisted" signal Stage 5 idempotency reads (Req. 1.6).
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)


class ExecutionReport(Base):
    """Stage 6 report/metrics (execution, measurement and report) — Req. 15.7.

    The ``SufficiencyMetrics`` are persisted in a portable JSON column
    (``metrics``), like ``Dag.content``. ``success_verdict`` stores the overall
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
#     ``campaigns``, ``dags``, ``environment_parameters`` and the Stage 6
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
    enum members (``pendente`` / ``em_execucao`` / ``concluido`` / ``falhou`` /
    ``abortado``).
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
    (``estagio_3_dag`` / ``estagio_4_operacionalizacao`` /
    ``estagio_5_ambiente``).
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
    N stage outputs). The serialized artifact (``ProducedDag`` |
    ``ProducedOperationalization`` | ``ProvisionedEnvironment``) lives in the
    portable JSON ``payload`` column. Where it corresponds to an existing
    persisted entity it is linked via ``dag_id`` (the AI-origin ``Dag`` row
    persisted through ``DagService``) or ``params_id`` (the
    ``EnvironmentParameters`` row); both FKs are nullable because only one
    applies per stage.

    ``origin`` is always ``gerado_ia`` here — persisted via the existing
    :class:`TranslationSource` portable enum, whose CHECK already admits
    ``gerado_ia``, so no schema migration is required to store AI artifacts
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
    # Always ``gerado_ia`` for AI-produced artifacts (Req. 11.8); reuses the
    # existing portable origin enum whose CHECK already includes the value.
    origin: Mapped[TranslationSource] = mapped_column(
        _portable_enum(TranslationSource, "translation_source"),
        default=TranslationSource.AI_GENERATED,
        nullable=False,
    )
    # Serialized stage artifact (ProducedDag | ProducedOperationalization |
    # ProvisionedEnvironment).
    payload: Mapped[Any] = mapped_column(JSON, nullable=False)
    # Linked AI-origin Dag row (when this is a DAG stage output); nullable.
    dag_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("dags.id"), nullable=True
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
    # Serialized list of the DAG nodes that failed (drives the refine target).
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
    (:class:`AgentStage` and :class:`AbilityResultStatus` — whose ``sucesso`` /
    ``falha`` values match the design's ``sucesso | falha`` outcome).
    """

    __tablename__ = "experience_memory"

    __table_args__ = (
        # The design constrains the outcome to exactly two values
        # (``sucesso | falha``). Task 2.1 defined no dedicated two-value outcome
        # enum, so we bound the portable VARCHAR column with an explicit CHECK
        # rather than widening it by reusing a four-member enum.
        CheckConstraint(
            "outcome IN ('sucesso', 'falha')",
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
    # Outcome of the stored curation, bounded to ``sucesso | falha`` by the
    # table CHECK above (matches AbilityResultStatus.SUCCESS/FAILURE values).
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
