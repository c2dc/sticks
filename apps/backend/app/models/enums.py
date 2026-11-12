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
    :class:`FlowOrigin`: the columns that use this enum
    (``Ability``/``Adversary``/``Case.origem_traducao`` and
    ``EnvironmentParameters.origem``) persist with ``native_enum=False``
    (VARCHAR + CHECK), so the generated ``CHECK`` already includes ``gerado_ia``
    from the first migration. As a result, persisting an artifact with origin
    ``gerado_ia`` in the future does **not** require any schema change or
    migration (Req. 16.2, 16.4). Implementing the AI agent that would produce
    such artifacts is a non-goal of this spec (Req. 18.1).

    _Requirements: 11.1, 11.2, 16.2, 16.4_
    """

    HUMAN_CURATION = "human_curation"  # value of the 8 existing curated translations
    AI_GENERATED = "ai_generated"  # produced by an AI agent (pluggable later, Req. 16)


class FlowOrigin(str, enum.Enum):
    """Origin of an attack flow — an explicit, extensible attribute.

    Replaces the former ``DagOrigin``. Follows the precedent of
    ``TranslationSource``: a ``str`` Enum whose values are persisted in
    ``attack_flows.origin`` (VARCHAR + CHECK via ``native_enum=False``) and
    exchanged over the REST/JSON API.

    ``MITRE_CORPUS`` marks flows ingested from the MITRE Attack Flow corpus;
    they do not belong to a Sticks campaign.

    _Requirements: 2.1, 5.1, 5.2_
    """

    HUMAN_CURATION = "human_curation"  # flows produced by human curation
    AI_GENERATED = "ai_generated"  # produced by an AI agent
    MITRE_CORPUS = "mitre_corpus"  # ingested from the MITRE Attack Flow corpus


class FlowProvenance(str, enum.Enum):
    """How a campaign flow was obtained from the corpus (three-path policy)."""

    EXACT = "exact"  # a corpus flow matches the campaign as is
    ADAPTED = "adapted"  # corpus flows were used as a base and adapted
    CREATED = "created"  # no usable corpus flow; the agent created it


class IngestSource(str, enum.Enum):
    """Knowledge sources loaded by the ingestion orchestrator."""

    ATOMIC = "atomic"
    DOCKER_CATALOG = "docker_catalog"
    ATTACK_FLOW = "attack_flow"
    STIX_PACKAGES = "stix_packages"
    CURATED_SEED = "curated_seed"


class IngestStatus(str, enum.Enum):
    """Lifecycle of the ingestion of one source."""

    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"

class EnvironmentPlatform(str, enum.Enum):
    """Environment platform — container today; VM admitted without redesign (Req. 17).

    Lives in the models layer (dependency-free) so it can be referenced both by
    persistence (``environment_parameters.plataforma``) and by the service layer
    (``app.services.environment.operationalization`` re-exports this enum),
    avoiding an import cycle between models and services.

    Subclasses ``str`` to serialize to its value in the API/DB, following the
    precedent of :class:`TranslationSource`/:class:`FlowOrigin`.

    Both members are **declared** on this single enum: ``CONTAINER`` is
    implemented and exercised this phase (Req. 17.1), while ``VM`` remains
    declared-but-not-exercised (Req. 17.2). Because both live on one enum, the
    portable column CHECK built via ``_portable_enum`` already admits
    ``virtual_machine`` from this migration forward — exactly the precedent of
    ``FlowOrigin``/``ai_generated``: persisting a virtual-machine platform in the
    future does **not** require a schema migration. This consolidation replaces
    the former split between the public enum and a persistence-only
    ``PersistedEnvironmentPlatform``.

    _Requirements: 17.1, 17.2_
    """

    CONTAINER = "container"
    VM = "virtual_machine"  # declared for the CHECK; not exercised this spec (Req. 17.2)


class StageState(str, enum.Enum):
    """State of a Stage (Req. 1.4, 5.3)."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ERROR = "error"


class OperationState(str, enum.Enum):
    """State of an Operation in Caldera."""

    NOT_STARTED = "not_started"
    RUNNING = "running"
    FINISHED = "finished"
    ABORTED = "aborted"
    INCOMPLETE_TIMEOUT = "incomplete_timeout"  # novo (Req. 3.2)


class AbilityResultStatus(str, enum.Enum):
    """Execution status of an Ability during an Operation (Req. 4.3)."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"


class Theme(str, enum.Enum):
    """Visual theme of the interface (Req. 7)."""

    LIGHT = "light"
    DARK = "dark"


class Language(str, enum.Enum):
    """Interface language (Req. 8)."""

    PT_BR = "pt-BR"
    EN = "en"


class AgentRunStatus(str, enum.Enum):
    """Lifecycle of an AI curation run (Req. 2.4, 2.5, 11.2).

    Subclasses ``str`` so members serialize to their pt-BR values — the strings
    persisted in the database and exchanged over the REST/JSON API — following
    the precedent of :class:`OperationState`/:class:`StageState`. The column
    that stores this enum persists with ``native_enum=False`` (VARCHAR + CHECK)
    via ``_portable_enum`` in ``app/models/domain.py``, so the generated CHECK
    bounds the value set to exactly these members.

    ``ABORTED`` marks a run that hit the max-iteration guard without a passing
    verdict (Req. 2.5).

    _Requirements: 2.4, 2.5, 11.2_
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"  # hit the max-iteration guard without a passing verdict


class AgentStage(str, enum.Enum):
    """Which agentic phase an agent output belongs to.

    Subclasses ``str`` so members serialize to their values, matching the
    precedent of the other domain enums. The values are the **semantic,
    number-free** phase names of the agentic-centric model
    (``operationalization``, ``flow``, ``environment``, ``execution``,
    ``evaluation``); the legacy ``stage_N_*`` numbering is removed. ``input``
    (the STIX base + campaign selection) is a user Input_Phase, not an agent
    phase, so it is intentionally absent here. ``execution`` and ``evaluation``
    are distinct, ordered phases: ``evaluation`` observes the real execution
    result and feeds the refinement loop.

    Because the backing columns persist with ``native_enum=False`` (VARCHAR +
    CHECK), the CHECK is regenerated to this value set by a reversible
    migration, which also converts legacy rows (``dag → flow``, etc.).

    _Requirements: 1.1, 1.2, 1.6, 2.1_
    """

    OPERATIONALIZATION = "operationalization"
    FLOW = "flow"
    ENVIRONMENT = "environment"
    EXECUTION = "execution"  # explicit own phase (plan -> act)
    EVALUATION = "evaluation"  # explicit own phase (evaluate; feeds refinement)


# The Phase_Order: the single, ordered list the backend agentic loop walks,
# ``operationalization → flow → environment → execution → evaluation`` (design
# §Phase_Order; Req. 1.1, 2.1). ``input`` is intentionally absent — it is the
# user Input_Phase, not an agent phase. Derived from the enum members so the
# list and the enum value set can never drift (``execution`` follows
# ``environment``; ``evaluation`` follows ``execution``). Consumers that need a
# canonical ordering of the agentic phases reference this tuple rather than
# hardcoding the sequence.
PHASE_ORDER: tuple[str, ...] = tuple(member.value for member in AgentStage)


class TechniqueEmulabilityKind(str, enum.Enum):
    """Container-emulability classification of an ATT&CK technique (Req. 6.1).

    Subclasses ``str`` so members serialize to their values — the strings
    persisted in the database and exchanged over the REST/JSON API — following
    the precedent of the other domain enums. The backing
    ``technique_emulability.emulability`` column persists with
    ``native_enum=False`` (VARCHAR + CHECK) via ``_portable_enum`` in
    ``app/models/domain.py``, so the generated CHECK bounds the value set to
    exactly these three members.

    * ``CONTAINER`` — the technique is emulable inside a container.
    * ``DISPENSABLE`` — the technique needs no emulation (e.g. social
      engineering), so it does not block container emulation of a campaign.
    * ``NOT_CONTAINER`` — the technique is not emulable in a container (requires
      a VM, firmware, or a human), so it blocks container emulation.

    A campaign is container-emulable if and only if every one of its techniques
    is ``CONTAINER`` or ``DISPENSABLE`` (Req. 6.2); a single ``NOT_CONTAINER``
    technique blocks it.

    _Requirements: 6.1, 6.2_
    """

    CONTAINER = "container"
    DISPENSABLE = "dispensable"
    NOT_CONTAINER = "not_container"


class EvaluatorDecision(str, enum.Enum):
    """The evaluator's routing decision for the conditional edge (Req. 2.4, 2.5).

    Subclasses ``str`` so members serialize to their values. ``REFINE`` routes
    the graph back to a curation stage for another pass; ``FINISH`` ends the run
    on a passing verdict.

    _Requirements: 2.4, 2.5_
    """

    REFINE = "refine"
    FINISH = "finish"
