"""Domain enums for the Pipeline_UI backend (task 2.1).

These enums implement exactly the value sets defined in the design's Data
Models section. They subclass ``str`` so their members serialize to their
string values (the values persisted in the database and exchanged over the
REST/JSON API), while still being proper ``enum.Enum`` members in Python code.

The string values are the source of truth: they match the API/DB contracts
described in the design (endpoints and WebSocket channels use these exact
strings, e.g. ``"nao_iniciado"``, ``"em_execucao"``, ``"pendente"``).

_Requirements: 11.1, 11.2, 6.8, 7.6, 8.6_
"""

from __future__ import annotations

import enum


class TranslationSource(str, enum.Enum):
    """Origin of an artifact — an explicit, extensible attribute.

    Today all 8 curated translations use ``HUMAN_CURATION``. The future value
    ``AI_GENERATED`` is already **declared** here, exactly like the precedent of
    :class:`DagOrigin`: the columns that use this enum
    (``Ability``/``Adversary``/``Case.origem_traducao`` and
    ``EnvironmentParameters.origem``) persist with ``native_enum=False``
    (VARCHAR + CHECK), so the generated ``CHECK`` already includes ``gerado_ia``
    from the first migration. As a result, persisting an artifact with origin
    ``gerado_ia`` in the future does **not** require any schema change or
    migration (Req. 16.2, 16.4). Implementing the AI agent that would produce
    such artifacts is a non-goal of this spec (Req. 18.1).

    _Requirements: 11.1, 11.2, 16.2, 16.4_
    """

    HUMAN_CURATION = "curadoria_humana"  # value of the 8 existing curated translations
    AI_GENERATED = "gerado_ia"  # produced by an AI agent (pluggable later, Req. 16)


class DagOrigin(str, enum.Enum):
    """Origin of a DAG — an explicit, extensible attribute (Req. 4.3, 4.6, 9.4).

    Follows **exactly** the precedent of ``TranslationSource``: a ``str`` Enum
    with pt-BR values that serialize to the strings persisted in the database
    and exchanged over the REST/JSON API.

    The ``Dag`` entity persists ``origem`` with ``native_enum=False`` (VARCHAR +
    CHECK), so the generated ``CHECK`` already includes **all** the values
    defined here. Including ``AI_GENERATED`` from the start makes the ``CHECK``
    accept ``"gerado_ia"`` from the first migration: inserting a ``gerado_ia``
    DAG in the future does **not** require any schema change or migration
    (Req. 4.6, 9.4).

    _Requirements: 4.3, 4.6, 9.4_
    """

    HUMAN_CURATION = "curadoria_humana"  # DAGs produced by human curation
    AI_GENERATED = "gerado_ia"  # produced by an AI agent (pluggable later, Req. 9)


class EnvironmentPlatform(str, enum.Enum):
    """Environment platform — container today; VM admitted without redesign (Req. 17).

    Lives in the models layer (dependency-free) so it can be referenced both by
    persistence (``environment_parameters.plataforma``) and by the service layer
    (``app.services.environment.operationalization`` re-exports this enum),
    avoiding an import cycle between models and services.

    Subclasses ``str`` to serialize to its value in the API/DB, following the
    precedent of :class:`TranslationSource`/:class:`DagOrigin`.

    In this spec only ``CONTAINER`` is implemented (Req. 17.1). ``maquina_virtual``
    is **not** a member of the enum (Req. 17.2) — but the CHECK of the persisted
    column already admits it via ``_portable_enum(..., extra_values=("maquina_virtual",))``,
    exactly like ``DagOrigin``/``gerado_ia``: adding it later requires no schema
    migration.

    _Requirements: 17.1, 17.2_
    """

    CONTAINER = "conteiner"
    # VM = "maquina_virtual"  # future (Req. 17.2) — not implemented in this spec


class PersistedEnvironmentPlatform(str, enum.Enum):
    """**Persistence** enum for the platform — source of the column CHECK.

    Exists only to build the column type of ``environment_parameters.plataforma``
    via ``_portable_enum`` (VARCHAR + CHECK). Unlike :class:`EnvironmentPlatform`
    (used by the application layer, with only ``CONTAINER`` implemented —
    Req. 17.1), this enum already **declares** ``maquina_virtual`` so the
    generated ``CHECK`` accepts it from the first migration — exactly the
    precedent of ``DagOrigin``/``gerado_ia``: persisting a virtual-machine
    platform in the future does **not** require a schema migration (Req. 17.2).
    ``CONTAINER`` has the same value (``"conteiner"``) as the public enum, so
    writing ``EnvironmentPlatform.CONTAINER`` stays compatible.

    _Requirements: 17.1, 17.2_
    """

    CONTAINER = "conteiner"
    VM = "maquina_virtual"  # admitted in the CHECK; not implemented in this spec (Req. 17.2)


class StageState(str, enum.Enum):
    """State of a Stage (Req. 1.4, 5.3)."""

    NOT_STARTED = "nao_iniciado"
    IN_PROGRESS = "em_andamento"
    COMPLETED = "concluido"
    ERROR = "erro"


class OperationState(str, enum.Enum):
    """State of an Operation in Caldera."""

    NOT_STARTED = "nao_iniciada"
    RUNNING = "em_execucao"
    FINISHED = "finalizada"
    ABORTED = "abortada"
    INCOMPLETE_TIMEOUT = "incompleta_timeout"  # novo (Req. 3.2)


class AbilityResultStatus(str, enum.Enum):
    """Execution status of an Ability during an Operation (Req. 4.3)."""

    PENDING = "pendente"
    RUNNING = "em_execucao"
    SUCCESS = "sucesso"
    FAILURE = "falha"


class Theme(str, enum.Enum):
    """Visual theme of the interface (Req. 7)."""

    LIGHT = "claro"
    DARK = "escuro"


class Language(str, enum.Enum):
    """Interface language (Req. 8)."""

    PT_BR = "pt-BR"
    EN = "en"
