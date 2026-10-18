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
  ``DateTime``), avoiding backend-specific types.

The models use the SQLAlchemy 2.x typed ``Mapped`` / ``mapped_column`` style to
match the ``DeclarativeBase`` used by ``Base``.

_Requirements: 9.1, 9.7, 11.1, 11.2, 6.8_
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    AbilityResultStatus,
    DagOrigin,
    EnvironmentPlatform,
    Language,
    OperationState,
    PersistedEnvironmentPlatform,
    StageState,
    Theme,
    TranslationSource,
)


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
    generated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
