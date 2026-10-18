"""StageService — Stage 1/2/3 orchestration (tasks 8.1, 8.2, 8.3).

Stage 1 (structural modeling) orchestration (task 8.1).

Stage 1 of the sticks pipeline is *Automated Structural Modeling*: for a
curated case it extracts ATT&CK **techniques**, the **relationships** between
actions, and — when present — **indicators**, **infrastructure**, **malware**
and **campaign metadata** from the source threat intelligence. The extracted
elements are persisted and the stage is marked ``concluido`` (Req. 2.2, 2.3,
2.4).

Dev-environment integration decision
-------------------------------------
The original structural modeling in ``sticks/lib/stix.py`` requires the full
merged STIX bundle on disk and has a **network download path**
(``download_all`` → ``requests.get``); it also ``sys.exit``s on a missing
bundle and pulls heavy STIX parsing dependencies. None of that is available (or
allowed) in the Windows dev environment, which must run against **local
fixtures only and perform no network access**.

The curated pipeline has *already produced* the structural extraction for every
curated case: it lives in ``data/dag/{slug}_dag.json`` (campaign name,
``structural_nodes`` with technique identity, per-node ``parent_nodes`` /
``child_nodes`` relationships, ``provides`` capability tags, ``attacker_commands``
and ``campaign_context``, plus the top-level ``metadata`` / ``capability_flow``).
So, for a curated case, **Stage 1 = read and assemble the structural elements
already present in the case's DAG** via :class:`~app.services.case_service.CaseService`
/ :class:`~app.services.case_service.DagData`. We reuse ``lib`` only where it
would not require the network or the full STIX bundle — which, for the
structural extraction, means deriving the output from the DAG the curated
pipeline emitted rather than re-parsing STIX.

What is derived, and how
------------------------
* **Techniques** — one per structural node with a ``technique_id`` (id, name,
  tactic, description), de-duplicated by ``(technique_id, technique_name)``.
* **Relationships** — directed ``parent -> child`` edges between technique
  nodes, taken from each node's ``child_nodes`` (cross-checked against
  ``parent_nodes``), so the Stage-1/2 structural graph is preserved.
* **Indicators** — network destinations mined from the nodes' ``attacker_commands``
  using the existing containment command parser (IPs/hosts/URLs), each tagged
  internal/external against the lab subnets. Purely local commands yield none.
* **Infrastructure** — nodes whose ``provides`` include an infrastructure/C2/
  resource capability, or whose technique is a resource-development one.
* **Malware** — nodes whose technique is a malware/tool development technique
  (e.g. ``T1587.001 - Malware``, ``T1588.002 - Tool``) or that provide such a
  capability.
* **Metadata** — campaign name/file, generation timestamp, the raw DAG
  ``metadata`` block and ``capability_flow`` summary.

Success, empty extraction and failure (Req. 2.4, 2.5, 2.6)
---------------------------------------------------------
* **Success**: persist the extracted elements associated with the case
  (``Case`` + ``Ability`` rows + ``Adversary`` when present) and set the
  ``StageRun.state`` to ``concluido`` (Req. 2.4).
* **Empty extraction**: a case that yields **no techniques** is *not* an error.
  It is treated as SUCCESS with a :class:`Stage1Output` flagged ``is_empty`` and
  a human-readable note that no structural element was extracted (Req. 2.5).
* **Failure**: an unreadable / invalid / malformed case (or an unexpected error
  during modeling) marks the stage ``erro``, records ``error_message`` and the
  modeling ``failed_step`` (Req. 2.6), and persists **no partial extraction**:
  the whole persistence runs in a single transaction that is rolled back on
  failure, so the DB never ends up with half the elements.

Injectable Session
------------------
The service takes an injected SQLAlchemy :class:`~sqlalchemy.orm.Session` (like
:class:`~app.services.audit.AuditLogger`), so the same code works with the app's
``SessionLocal`` and an ephemeral test SQLite session. A custom
:class:`~app.services.case_service.CaseService` may also be injected to point at
alternative local fixtures.

This module only ever **reads** from ``sticks/`` (through ``CaseService``) — it
never modifies anything there, and it performs **no network access**.

_Requirements: 2.2, 2.3, 2.4, 2.5, 2.6_
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.domain import (
    Ability as AbilityModel,
    Adversary as AdversaryModel,
    Case as CaseModel,
    StageRun,
)
from app.models.enums import (
    AbilityResultStatus,
    OperationState,
    StageState,
    TranslationSource,
)
from app.services.campaign_service import (
    AbilityData,
    AdversaryData,
    CaseData,
    CaseFileError,
    CaseService,
    DagData,
    DagNode,
)
from app.services.containment.command_parser import parse_command
from app.services.containment.subnets import is_internal_ip
from app.services.stix.stix_service import (
    SLUG_TO_STIX,
    DetailedCampaignStix,
    StixFileError,
    StixService,
)


# ---------------------------------------------------------------------------
# Modeling steps (used for ``failed_step`` on failure — Req. 2.6)
# ---------------------------------------------------------------------------


class Stage1Step(str, enum.Enum):
    """The Stage-1 modeling steps, in execution order.

    On failure the step in progress is recorded on ``StageRun.failed_step`` so
    the UI can identify *where* the structural modeling failed for the affected
    case (Req. 2.6). Subclasses ``str`` for a clean DB/JSON value.
    """

    READ_CASE = "leitura_do_caso"                 # load DAG/abilities/adversary
    EXTRACT_TECHNIQUES = "extracao_de_tecnicas"    # techniques + relationships
    EXTRACT_ELEMENTS = "extracao_de_elementos"     # indicators/infra/malware/meta
    PERSIST = "persistencia"                        # persist extracted elements


# ---------------------------------------------------------------------------
# Typed Stage-1 result (input / output) — for the API / UI later
# ---------------------------------------------------------------------------


class TechniqueElement(BaseModel):
    """One extracted ATT&CK technique (Req. 2.3 — techniques)."""

    technique_id: str
    technique_name: Optional[str] = None
    tactic: Optional[str] = None
    description: Optional[str] = None
    node_id: Optional[str] = None


class RelationshipElement(BaseModel):
    """A directed relationship between two technique nodes (Req. 2.3).

    ``source``/``target`` are node ids; ``source_technique``/``target_technique``
    carry the technique ids for display when available.
    """

    source: str
    target: str
    source_technique: Optional[str] = None
    target_technique: Optional[str] = None


class IndicatorElement(BaseModel):
    """A network indicator mined from an attacker command (Req. 2.3).

    ``value`` is the destination host/IP, ``kind`` its syntactic form (ip/host/
    url), ``is_internal`` whether it falls inside the lab subnets, and
    ``origin_technique`` the technique whose command produced it.
    """

    value: str
    kind: str
    is_internal: bool
    origin_technique: Optional[str] = None


class InfrastructureElement(BaseModel):
    """An infrastructure element inferred from a node (Req. 2.3).

    Nodes that provide infrastructure/C2/resource capabilities, or that are
    resource-development techniques, are surfaced here.
    """

    technique_id: Optional[str] = None
    technique_name: Optional[str] = None
    provides: list[str] = Field(default_factory=list)


class MalwareElement(BaseModel):
    """A malware/tooling element inferred from a node (Req. 2.3)."""

    technique_id: Optional[str] = None
    technique_name: Optional[str] = None
    provides: list[str] = Field(default_factory=list)


class CampaignStixInput(BaseModel):
    """Identity block of the STIX campaign in the Stage 1 Input (Req. 5.1, 2.3).

    A slice of the ``campaign`` object from the STIX package of the base
    selected in the flow, which becomes the *conceptual input* of the Structural
    Modeling (Stage 1). All fields are optional except ``found``, which marks
    whether the ``campaign`` object was located in the base; when ``False``,
    ``unavailable_reason`` carries the fallback reason
    (``versao_ausente`` / ``bundle_indisponivel`` / ``campanha_nao_encontrada``)
    and the rich fields may be absent, still carrying the curated identifier
    (name + ``external_id``) when available.

    The Input is informative: the absence/fallback of this block does NOT affect
    the structural extraction of Stage 1 (Req. 4.3).
    """

    found: bool
    base_version: Optional[str] = None
    stix_id: Optional[str] = None
    name: Optional[str] = None
    external_id: Optional[str] = None
    description: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    unavailable_reason: Optional[str] = None


class Stage1Input(BaseModel):
    """Stage 1 Input — the campaign slice in the STIX package (Req. 2.1, 5.1).

    The *conceptual input* of the Structural Modeling is the STIX intelligence
    of the selected campaign: ``campaign_stix`` carries the slice of the
    ``campaign`` object from the STIX base selected in the flow (or the clear
    fallback when the object is not found — see :class:`CampaignStixInput`).

    The ``dag_file``/``stix_reference`` fields remain in the DTO for contract
    compatibility, but are no longer displayed by the UI (which now consumes
    ``campaign_stix``). ``stix_reference`` named the source dataset when the DAG
    recorded it (``campaign_file``).
    """

    campaign_id: str
    name: str
    campaign_stix: Optional[CampaignStixInput] = None
    dag_file: Optional[str] = None
    stix_reference: Optional[str] = None
    campaign_name: Optional[str] = None


class Stage1Output(BaseModel):
    """Stage 1 Output — the extracted structural elements (Req. 2.3, 2.5).

    ``is_empty`` is ``True`` when no technique was extracted; in that case
    ``note`` carries the "no structural elements extracted" message (Req. 2.5)
    and every element list is empty. This is a SUCCESS output, not an error.
    """

    techniques: list[TechniqueElement] = Field(default_factory=list)
    relationships: list[RelationshipElement] = Field(default_factory=list)
    indicators: list[IndicatorElement] = Field(default_factory=list)
    infrastructure: list[InfrastructureElement] = Field(default_factory=list)
    malware: list[MalwareElement] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    is_empty: bool = False
    note: Optional[str] = None


class Stage1Result(BaseModel):
    """The full typed Stage-1 result returned to the API/UI later.

    Carries the final ``state`` (``concluido`` on success/empty, ``erro`` on
    failure), the Input/Output, and, on failure, the ``error_message`` and the
    modeling ``failed_step`` (Req. 2.6). ``stage_run_id`` links to the persisted
    :class:`~app.models.domain.StageRun` row.
    """

    campaign_id: str
    state: StageState
    input: Stage1Input
    output: Optional[Stage1Output] = None
    error_message: Optional[str] = None
    failed_step: Optional[str] = None
    stage_run_id: Optional[int] = None

    @property
    def ok(self) -> bool:
        """``True`` when the stage completed (success or empty extraction)."""
        return self.state is StageState.COMPLETED

    @property
    def is_error(self) -> bool:
        """``True`` when the stage ended in error."""
        return self.state is StageState.ERROR


# ---------------------------------------------------------------------------
# Derivation helpers (pure functions over DagData) — no I/O, no network
# ---------------------------------------------------------------------------

# Capability tags (in a node's ``provides``) that signal infrastructure / C2 /
# resources contributed by the technique.
_INFRA_CAPABILITIES: frozenset[str] = frozenset(
    {"infrastructure", "resources", "c2_channel"}
)

# Substrings (case-insensitive) in a technique name that signal a malware /
# tooling development technique (e.g. "T1587.001 - Malware", "T1588.002 - Tool").
_MALWARE_NAME_SIGNALS: tuple[str, ...] = ("malware", "tool")

# Capability tags that signal a malware/tooling capability was produced.
_MALWARE_CAPABILITIES: frozenset[str] = frozenset({"malware"})


def _iter_technique_nodes(dag: DagData) -> list[DagNode]:
    """Return the structural nodes that carry an ATT&CK technique id."""
    return [n for n in dag.structural_nodes if n.technique_id]


def _extract_techniques(dag: DagData) -> list[TechniqueElement]:
    """Extract the ATT&CK techniques, de-duplicated by (id, name).

    Preserves first-seen order so the structural graph reads top-to-bottom.
    """
    seen: set[tuple[str, Optional[str]]] = set()
    techniques: list[TechniqueElement] = []
    for node in _iter_technique_nodes(dag):
        key = (node.technique_id, node.technique_name)  # type: ignore[arg-type]
        if key in seen:
            continue
        seen.add(key)
        techniques.append(
            TechniqueElement(
                technique_id=node.technique_id,  # type: ignore[arg-type]
                technique_name=node.technique_name,
                tactic=node.tactic,
                description=node.description,
                node_id=node.node_id,
            )
        )
    return techniques


def _extract_relationships(dag: DagData) -> list[RelationshipElement]:
    """Extract directed ``parent -> child`` relationships between nodes.

    Uses each node's ``child_nodes`` as the authoritative edge source and
    resolves the endpoints' technique ids for display. Edges whose endpoints are
    unknown node ids are skipped defensively. De-duplicated by ``(source,
    target)``.
    """
    tech_by_node: dict[str, Optional[str]] = {
        n.node_id: n.technique_id
        for n in dag.structural_nodes
        if n.node_id is not None
    }
    seen: set[tuple[str, str]] = set()
    relationships: list[RelationshipElement] = []
    for node in dag.structural_nodes:
        if node.node_id is None:
            continue
        for child in node.child_nodes:
            edge = (node.node_id, child)
            if edge in seen or child not in tech_by_node:
                continue
            seen.add(edge)
            relationships.append(
                RelationshipElement(
                    source=node.node_id,
                    target=child,
                    source_technique=tech_by_node.get(node.node_id),
                    target_technique=tech_by_node.get(child),
                )
            )
    return relationships


def _extract_indicators(dag: DagData) -> list[IndicatorElement]:
    """Mine network indicators from the nodes' attacker commands.

    Reuses the containment command parser (pure Python, no network) to pull
    destinations out of each command, and classifies each as internal/external
    against the known lab subnets. De-duplicated by ``(value, origin_technique)``.
    """
    seen: set[tuple[str, Optional[str]]] = set()
    indicators: list[IndicatorElement] = []
    for node in dag.structural_nodes:
        for command in node.attacker_commands:
            for dest in parse_command(command).destinations:
                key = (dest.value, node.technique_id)
                if key in seen:
                    continue
                seen.add(key)
                indicators.append(
                    IndicatorElement(
                        value=dest.value,
                        kind=dest.kind.value,
                        is_internal=is_internal_ip(dest.value),
                        origin_technique=node.technique_id,
                    )
                )
    return indicators


def _is_infrastructure_node(node: DagNode) -> bool:
    """Whether a node contributes infrastructure/C2/resources."""
    if any(cap in _INFRA_CAPABILITIES for cap in node.provides):
        return True
    return (node.tactic or "").lower() == "resource-development"


def _is_malware_node(node: DagNode) -> bool:
    """Whether a node represents malware/tooling development."""
    name = (node.technique_name or "").lower()
    if any(signal in name for signal in _MALWARE_NAME_SIGNALS):
        return True
    return any(cap in _MALWARE_CAPABILITIES for cap in node.provides)


def _extract_infrastructure(dag: DagData) -> list[InfrastructureElement]:
    """Surface infrastructure elements, de-duplicated by (id, name)."""
    seen: set[tuple[Optional[str], Optional[str]]] = set()
    out: list[InfrastructureElement] = []
    for node in dag.structural_nodes:
        if not _is_infrastructure_node(node):
            continue
        key = (node.technique_id, node.technique_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            InfrastructureElement(
                technique_id=node.technique_id,
                technique_name=node.technique_name,
                provides=list(node.provides),
            )
        )
    return out


def _extract_malware(dag: DagData) -> list[MalwareElement]:
    """Surface malware/tooling elements, de-duplicated by (id, name)."""
    seen: set[tuple[Optional[str], Optional[str]]] = set()
    out: list[MalwareElement] = []
    for node in dag.structural_nodes:
        if not _is_malware_node(node):
            continue
        key = (node.technique_id, node.technique_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            MalwareElement(
                technique_id=node.technique_id,
                technique_name=node.technique_name,
                provides=list(node.provides),
            )
        )
    return out


def _extract_metadata(dag: DagData) -> dict:
    """Assemble the campaign metadata block for the Stage-1 output."""
    meta: dict = {
        "campaign_name": dag.campaign_name,
        "campaign_file": dag.campaign_file,
        "generated_at": dag.generated_at,
        "total_nodes": len(dag.structural_nodes),
    }
    if dag.metadata:
        meta["dag_metadata"] = dag.metadata
    if dag.capability_flow:
        # Keep the capability-flow summary compact: only the aggregate lists the
        # UI cares about, if present.
        cf = dag.capability_flow
        summary = {
            k: cf[k]
            for k in ("initial_capabilities", "final_capabilities")
            if k in cf
        }
        if summary:
            meta["capability_flow"] = summary
    return meta


# ===========================================================================
# Operationalization — Stage 3 — typed Input/Output (formerly "Curated Translation")
# ===========================================================================
#
# Operationalization is Stage 3 of the five-stage pipeline (``stage=3``,
# :data:`STAGE_OPERATIONALIZATION`; Req. 8.4). It turns the abstract ATT&CK
# techniques (consolidated in the Stage-2 DAG) into executable Abilities —
# the concrete commands, their ordering / dependencies, and the Adversary
# grouping (Req. 4.1). The product terminology is "Operationalization", no longer
# "Curated Translation" (Req. 4.5); the Pydantic classes keep their historical
# ``Stage2*`` names and the operationalization source is still exposed via
# ``translation_source`` / :class:`TranslationSource` — the explicit, extensible
# precedent (``curadoria_humana`` today; Req. 4.3). For the 8 curated cases this
# operationalization *already exists* on disk (``data/api/{slug}_dag-ability.json``
# and ``{slug}_dag-adversary.json``), read by :class:`CaseService`. So, in the
# dev pipeline, **Operationalization = assemble the Input from the upstream
# techniques and the Output from the curated Abilities/Adversary**, tagging the
# operationalization source (Req. 4.3). No campaign execution happens here.
#
# The absence states are represented *cleanly* (never as errors), so the UI can
# render each one distinctly (Req. 3.6/3.7/3.8).


class Stage2InputTechnique(BaseModel):
    """One abstract ATT&CK technique fed into Stage 2 (Req. 3.1).

    These come straight from the Stage-1 output techniques — the abstract
    behaviour descriptions the human curator translated into executable steps.
    """

    technique_id: str
    technique_name: Optional[str] = None
    tactic: Optional[str] = None
    description: Optional[str] = None


class Stage2Input(BaseModel):
    """Stage 2 Input — the abstract techniques from Stage 1 (Req. 3.1).

    ``has_input`` is ``False`` when Stage 1 produced no technique; in that case
    ``note`` carries the "no Stage-2 input" message and the Output is suppressed
    (Req. 3.6).
    """

    campaign_id: str
    name: str
    techniques: list[Stage2InputTechnique] = Field(default_factory=list)
    has_input: bool = True
    note: Optional[str] = None


class Stage2ExecutorView(BaseModel):
    """A single executor of a curated Ability — the concrete command (Req. 3.2)."""

    name: Optional[str] = None
    platform: Optional[str] = None
    command: Optional[str] = None


class Stage2AbilityView(BaseModel):
    """One curated Ability as shown in the Stage-2 Output (Req. 3.2).

    Carries the technique id, tactic and description, plus the concrete command
    of each executor. ``translation_source`` is the explicit, extensible
    translation source (Req. 11.1) — ``"curadoria_humana"`` for the 8 curated
    cases.
    """

    ability_id: str
    name: Optional[str] = None
    technique_id: Optional[str] = None
    tactic: Optional[str] = None
    description: Optional[str] = None
    executors: list[Stage2ExecutorView] = Field(default_factory=list)
    translation_source: TranslationSource = TranslationSource.HUMAN_CURATION


class Stage2AdversaryView(BaseModel):
    """The Adversary grouping of Abilities shown in the Stage-2 Output (Req. 3.5).

    ``atomic_ordering`` is the ordered list of ability ids that composes the
    Adversary — the sequence/dependencies between Abilities (Req. 3.3).
    """

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    atomic_ordering: list[str] = Field(default_factory=list)
    translation_source: TranslationSource = TranslationSource.HUMAN_CURATION


class Stage2Output(BaseModel):
    """Stage 2 Output — the curated translation (Req. 3.2–3.5, 3.7, 3.8, 11.1).

    Holds the curated Abilities (each with the concrete command per executor),
    the ordering/dependencies (``atomic_ordering``), the Adversary grouping and
    the explicit translation source ``translation_source`` (Req. 11.1).

    Absence flags let the UI render the distinct states cleanly:

    * ``has_abilities`` is ``False`` — the case has no curated Abilities, so
      there is no Stage-2 Output (Req. 3.7); ``note`` explains it.
    * ``has_adversary`` is ``False`` — the case has no Adversary grouping, so the
      Abilities are not grouped into an Adversary (Req. 3.8);
      ``adversary_note`` explains it.

    ``curadoria_humana`` (``human_curation`` flag + textual ``translation_source``)
    is the explicit "human curation" indicator associated with the Output
    (Req. 3.4).
    """

    abilities: list[Stage2AbilityView] = Field(default_factory=list)
    ordering: list[str] = Field(default_factory=list)
    adversary: Optional[Stage2AdversaryView] = None
    translation_source: TranslationSource = TranslationSource.HUMAN_CURATION
    human_curation: bool = True
    has_abilities: bool = True
    has_adversary: bool = True
    note: Optional[str] = None
    adversary_note: Optional[str] = None


class Stage2Result(BaseModel):
    """The full typed Stage-2 result returned to the API/UI (Req. 3).

    Mirrors the :class:`Stage1Result` style. ``suppressed`` is ``True`` when
    Stage 1 produced no technique: the Input has no input (Req. 3.6) and the
    Output is suppressed (``output is None``). Otherwise ``output`` carries the
    curated translation, including its absence sub-states (no abilities / no
    adversary).
    """

    campaign_id: str
    input: Stage2Input
    output: Optional[Stage2Output] = None
    suppressed: bool = False


# ===========================================================================
# Stage 3 — typed input/output (task 8.2)
# ===========================================================================
#
# Stage 3 is *Adversary Emulation*: the curated Adversary + Abilities are
# loaded into Caldera and run as an Operation against the isolated Docker
# environment (Req. 4). The execution itself (containment pre-flight, Caldera
# calls, per-command auditing, aggregation) is the responsibility of the
# ``OperationRunner`` (task 7.3) — this module does NOT re-implement it. Instead
# it:
#
# * assembles the input = Adversary + curated Abilities (Req. 4.1), and
# * delegates execution to an injected :class:`OperationRunnerProtocol`, mapping
#   its result into the output = per-Ability result + aggregate (Req. 4.4, 4.5).
#
# The assembly is kept independent of the runner so the API layer can call
# preview / confirm / execute separately: :meth:`StageService.assemble_stage3`
# builds the input (and the runner input) with no side effects, and
# :meth:`StageService.run_stage3` performs the delegated execution.


class Stage3AbilityInput(BaseModel):
    """One curated Ability listed in the Stage-3 input (Req. 4.1)."""

    ability_id: str
    name: Optional[str] = None
    technique_id: Optional[str] = None
    tactic: Optional[str] = None
    executors: list[Stage2ExecutorView] = Field(default_factory=list)


class Stage3AdversaryInput(BaseModel):
    """The Adversary listed in the Stage-3 input (Req. 4.1)."""

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    atomic_ordering: list[str] = Field(default_factory=list)


class Stage3Input(BaseModel):
    """Stage 3 Input — the Adversary + curated Abilities (Req. 4.1).

    ``can_emulate`` is ``True`` only when both an Adversary and at least one
    Ability are present (otherwise there is nothing to run); ``note`` explains
    the absence when it is ``False``.
    """

    campaign_id: str
    name: str
    adversary: Optional[Stage3AdversaryInput] = None
    abilities: list[Stage3AbilityInput] = Field(default_factory=list)
    can_emulate: bool = True
    note: Optional[str] = None


class Stage3AbilityResult(BaseModel):
    """The per-Ability result of the Operation (Req. 4.4).

    ``status`` is one of ``pendente`` / ``em_execucao`` / ``sucesso`` / ``falha``
    (:class:`~app.models.enums.AbilityResultStatus`); ``command_output`` is the
    executed command's output.
    """

    ability_id: str
    name: Optional[str] = None
    status: AbilityResultStatus = AbilityResultStatus.PENDING
    command_output: Optional[str] = None


class Stage3Aggregate(BaseModel):
    """The aggregate Operation result (Req. 4.5).

    ``total_success`` / ``total_failure`` count the Abilities that finished with
    ``sucesso`` / ``falha``; ``state`` is the final Operation state.
    """

    total_success: int = 0
    total_failure: int = 0
    state: OperationState = OperationState.NOT_STARTED


class Stage3Output(BaseModel):
    """Stage 3 Output — per-Ability result + aggregate (Req. 4.4, 4.5).

    ``operation_id`` links back to the persisted :class:`~app.models.domain.Operation`
    when the runner created one.
    """

    results: list[Stage3AbilityResult] = Field(default_factory=list)
    aggregate: Stage3Aggregate = Field(default_factory=Stage3Aggregate)
    operation_id: Optional[int] = None


class Stage3Result(BaseModel):
    """The full typed Stage-3 result returned to the API/UI (Req. 4).

    Mirrors the :class:`Stage1Result` / :class:`Stage2Result` style. When the
    emulation was not run (no confirmation, or nothing to emulate) ``executed``
    is ``False`` and ``output is None``; ``message`` explains why. When the
    runner ran, ``output`` carries the per-Ability results and the aggregate.
    ``error_message`` carries a containment/availability/audit failure surfaced
    by the runner without executing (Req. 4.6, 6.2, 6.4, 6.9).
    """

    campaign_id: str
    input: Stage3Input
    output: Optional[Stage3Output] = None
    executed: bool = False
    message: Optional[str] = None
    error_message: Optional[str] = None


# ===========================================================================
# Sequential blocking + aggregate progress + state/percentage reporting
# (task 8.3)
# ===========================================================================
#
# The Pipeline_UI conducts each curated case through Stage 1 -> 2 -> 3 -> 4 -> 5
# in order (Req. 5.2): 1 = Structural Modeling, 2 = DAG Generation,
# 3 = Operationalization, 4 = Environment Provisioning, 5 = Campaign
# Execution. A later stage is *blocked* — and cannot be started — while its
# immediately preceding stage is not "concluido" (Req. 5.4). The aggregate
# replication progress counts exactly the cases whose all six stages are
# "concluido", out of the 8 curated cases (Req. 1.5). Finally, the real-time
# channels (task 11.3) need a way to report a stage's current state and progress
# percentage, and the state transition it just made (Req. 1.4, 1.5, 1.6).
#
# All of this is *pure logic over the persisted ``StageRun`` rows*: the state of
# a stage is read from its ``StageRun`` row (defaulting to ``nao_iniciado`` when
# there is no row yet), so it works identically on the app's ``SessionLocal``
# and an ephemeral test SQLite session. There is no re-modeling here — the
# per-stage assembly above is left intact; this section only reads state and
# derives the blocking/progress views, parametrised by ``VALID_STAGES`` so the
# same algorithm spans the 1..6 range (Req. 1.1, 1.2, 1.3, 1.4, 1.5).

# The six pipeline stages, in conduction order (Req. 1.1, 1.3, 1.4, 1.5, 3.1):
#   1 = STIX base selection, 2 = Campaign selection, 3 = DAG generation,
#   4 = Operationalization, 5 = Simulation environment creation,
#   6 = Execution + measurement + report.
# A curated case has exactly these stages; ``VALID_STAGES`` bounds every stage
# argument and is the single source of truth for the pipeline range. The
# sequential blocking, aggregate progress and per-stage state derivation below
# are all derived from these constants (iterate ``VALID_STAGES`` / use
# ``FIRST_STAGE`` and ``LAST_STAGE``) — there are no hardcoded stage literals,
# so widening the range here makes them cover 1..6 automatically (Req. 1.1,
# 3.1, 3.2, 3.3).
FIRST_STAGE: int = 1
LAST_STAGE: int = 6
VALID_STAGES: tuple[int, ...] = (1, 2, 3, 4, 5, 6)

# Named stage roles for the six-stage pipeline (design: "Roles of the six
# Stages"). These are the single source of truth for each stage number; the
# sequential blocking, aggregate progress and per-``StageRun`` derivation
# reference ``VALID_STAGES``/``FIRST_STAGE``/``LAST_STAGE`` rather than literals,
# so they span 1..6 without changes.
STAGE_BASE_SELECTION: int = 1        # Stage 1 — STIX base selection
STAGE_CAMPAIGN_SELECTION: int = 2    # Stage 2 — Campaign selection
STAGE_DAG_GENERATION: int = 3         # Stage 3 — DAG generation
STAGE_OPERATIONALIZATION: int = 4   # Stage 4 — Operationalization (Abilities + environment parameters)
STAGE_ENVIRONMENT: int = 5            # Stage 5 — Simulation environment creation
STAGE_EXECUTION: int = 6            # Stage 6 — Execution + measurement + report

# Technical-debt note (Req. 18.5): the preceding constants (historical names
# from the renumbering to five Stages) remain as ALIASES so they do not break
# imports of modules not yet migrated (``dag/dag_service.py``,
# ``environment/stage_service.py``, ``api/emulation.py`` etc.). The downstream
# modules will be re-wired to the new roles in each Stage's specific tasks;
# until then the aliases preserve the current behavior.
#   * ``STAGE_MODELING`` → role of the old Stage 1 (today base selection).
#   * ``STAGE_PROVISIONING`` → environment creation/provisioning role
#     (today :data:`STAGE_ENVIRONMENT`).
# ``STAGE_DAG_GENERATION``/``STAGE_OPERATIONALIZATION``/``STAGE_EXECUTION``
# exist in both name sets and already point at the new roles above.
STAGE_MODELING: int = STAGE_BASE_SELECTION        # historical alias (Req. 18.5)
STAGE_PROVISIONING: int = STAGE_ENVIRONMENT      # historical alias (Req. 18.5)


class StageStateView(BaseModel):
    """The state of a single stage of a case (Req. 5.3, 1.4).

    Attributes
    ----------
    stage:
        The stage number (1, 2, 3, 4, 5 or 6).
    state:
        The stage state — exactly one of ``nao_iniciado`` / ``em_andamento`` /
        ``concluido`` / ``erro`` (:class:`~app.models.enums.StageState`),
        derived from the stage's ``StageRun`` row (``nao_iniciado`` when there
        is no row yet — Req. 9.5).
    progress:
        The processing percentage 0..100 (Req. 1.5). Persisted on the
        ``StageRun``; defaults to 0 when there is no row.
    blocked:
        ``True`` when the stage cannot be started because its immediately
        preceding stage is not ``concluido`` (Req. 5.4). Stage 1 is never
        blocked.
    startable:
        ``True`` when the stage can be started now — i.e. it is Stage 1, or its
        preceding stage is ``concluido`` (Req. 5.4). The exact complement of
        ``blocked``.
    error_message:
        The failure cause when ``state`` is ``erro`` (Req. 1.6), else ``None``.
    failed_step:
        The Stage-1 modeling step that failed, when recorded (Req. 2.6).
    """

    stage: int
    state: StageState = StageState.NOT_STARTED
    progress: int = 0
    blocked: bool = False
    startable: bool = True
    error_message: Optional[str] = None
    failed_step: Optional[str] = None


class CaseStageStates(BaseModel):
    """The per-stage state of a case plus whether it is fully replicated.

    ``stages`` holds one :class:`StageStateView` per stage (1..6), in order,
    each carrying its state, percentage and ``blocked`` flag (Req. 5.3). A case
    is ``fully_completed`` iff all six stages are ``concluido`` (the unit the
    aggregate progress counts — Req. 1.5).
    """

    campaign_id: str
    stages: list[StageStateView] = Field(default_factory=list)
    fully_completed: bool = False


class AggregateProgress(BaseModel):
    """The aggregate replication progress across the 8 curated cases (Req. 5.5).

    ``completed_campaigns`` is the number of cases whose all six stages are
    ``concluido``; ``total_campaigns`` is the case universe (8 curated cases);
    ``percentage`` is ``completed_campaigns / total_campaigns`` as an integer
    0..100 for display. ``completed_campaign_ids`` lists which cases are fully
    replicated so the UI can highlight them.
    """

    completed_campaigns: int = 0
    total_campaigns: int = 0
    percentage: int = 0
    completed_campaign_ids: list[str] = Field(default_factory=list)


class LogLineView(BaseModel):
    """A provisioning log line carried over the Stages channel (Req. 2.6).

    A single provisioning log line carried over the existing stage channel. Each
    line has a monotonic ``seq`` per case (so the UI can dedupe/reorder on
    reconnect), the pipeline ``step`` that emitted it, the ``stream`` it belongs
    to (``comando`` | ``stdout`` | ``stderr`` | ``info``), the ``text`` itself,
    and ``at`` — a naive-UTC timestamp consistent with the module's
    :func:`_utcnow` helper (Req. 2.6).
    """

    seq: int
    step: str
    stream: str
    text: str
    at: dt.datetime


class StageProgressEvent(BaseModel):
    """A stage state/percentage report for the real-time channels (Req. 1.4–1.6).

    This is the payload the WebSocket layer (task 11.3, ``WS /ws/stages/{campaign}``)
    pushes: the stage's current ``state`` and ``progress`` percentage, plus —
    when this event is emitted in response to a change — the ``previous_state``
    it transitioned from (Req. 1.4). While a stage is ``em_andamento`` the channel
    re-emits this at least every 5s so the UI can show the live percentage
    (Req. 1.5); on failure ``error_message`` carries the cause (Req. 1.6).

    For Stage 5 the frame also carries ``logs`` — the DELTA of new provisioning
    log lines emitted since the last frame (Req. 2.6). It defaults to an empty
    list, so Stages 1–4 and 6 keep emitting the exact same frame they do today
    (``logs`` stays empty for them — a pure additive change).
    """

    campaign_id: str
    stage: int
    state: StageState
    progress: int = 0
    previous_state: Optional[StageState] = None
    error_message: Optional[str] = None
    failed_step: Optional[str] = None
    # DELTA (Stage 5): new lines since the last frame (Req. 2.6).
    logs: list[LogLineView] = Field(default_factory=list)


def _validate_stage(stage: int) -> None:
    """Guard that ``stage`` is one of the valid pipeline stages (1..6).

    Interpolates :data:`VALID_STAGES`, so widening the tuple to ``(1, 2, 3, 4,
    5, 6)`` makes this accept 1..6 and reject anything out of range
    (Req. 3.1, 3.2, 3.3).
    """
    if stage not in VALID_STAGES:
        raise ValueError(
            f"Estágio inválido: {stage!r}. Os estágios válidos são "
            f"{VALID_STAGES}."
        )


# ---------------------------------------------------------------------------
# OperationRunner seam (task 7.3) — injected, never re-implemented here
# ---------------------------------------------------------------------------


class OperationRunResult(BaseModel):
    """The result shape the Stage-3 assembly expects from an OperationRunner.

    This is the minimal contract Stage 3 depends on: the per-Ability results,
    the aggregate totals and final state, an optional persisted ``operation_id``,
    and — when the runner refused to execute (containment violation, Caldera
    unavailable, missing confirmation, audit failure) — ``executed=False`` with a
    human-readable ``message`` / ``error_message`` (Req. 4.4, 4.5, 4.6, 6.x).

    The real :class:`OperationRunner` (task 7.3) may return a richer object; the
    assembly only reads these attributes, so any object exposing them (including
    a mock) satisfies the seam.
    """

    results: list[Stage3AbilityResult] = Field(default_factory=list)
    total_success: int = 0
    total_failure: int = 0
    state: OperationState = OperationState.NOT_STARTED
    operation_id: Optional[int] = None
    executed: bool = True
    message: Optional[str] = None
    error_message: Optional[str] = None


@runtime_checkable
class OperationRunnerProtocol(Protocol):
    """Structural interface for the injected OperationRunner (task 7.3).

    Stage 3 delegates the actual emulation to an object implementing this
    protocol. The assembly stays independent of *how* the Operation runs
    (Caldera calls, containment pre-flight, per-command auditing, aggregation);
    it only needs to hand the runner the case and the explicit confirmation and
    read back an :class:`OperationRunResult`-shaped result.

    Defining this as a ``Protocol`` (structural typing) means the real
    ``OperationRunner`` need not import or subclass anything here, and tests can
    inject a lightweight mock.
    """

    def run(self, case: CaseData, *, confirmed: bool) -> OperationRunResult:
        """Run the Operation for ``case`` iff ``confirmed`` is true.

        Must not execute any adversary command unless ``confirmed`` is true
        (Req. 6.6, 6.7). Returns an :class:`OperationRunResult`-shaped result.
        """
        ...


# ---------------------------------------------------------------------------
# Stage 2/3 assembly helpers (pure functions) — no I/O, no network
# ---------------------------------------------------------------------------


def _stage2_input_from_stage1(
    slug: str, name: str, stage1: Stage1Result
) -> Stage2Input:
    """Build the Stage-2 Input from a Stage-1 result (Req. 3.1, 3.6).

    The abstract techniques from the Stage-1 Output become the Stage-2 input.
    When Stage 1 produced no technique (empty extraction, error, or missing
    Output), the input is flagged ``has_input=False`` with a note (Req. 3.6) and
    the Stage-2 Output is suppressed by the caller.
    """
    techniques: list[Stage2InputTechnique] = []
    if stage1.output is not None:
        techniques = [
            Stage2InputTechnique(
                technique_id=t.technique_id,
                technique_name=t.technique_name,
                tactic=t.tactic,
                description=t.description,
            )
            for t in stage1.output.techniques
        ]

    if not techniques:
        return Stage2Input(
            campaign_id=slug,
            name=name,
            techniques=[],
            has_input=False,
            note=(
                "O Estágio 1 não produziu técnicas ATT&CK para o caso "
                f"{name}; não há Entrada para o Estágio 2."
            ),
        )
    return Stage2Input(campaign_id=slug, name=name, techniques=techniques)


def _ability_view(ability: AbilityData) -> Stage2AbilityView:
    """Map a curated :class:`AbilityData` into the Stage-2 Output view (Req. 3.2)."""
    return Stage2AbilityView(
        ability_id=ability.ability_id,
        name=ability.name,
        technique_id=ability.technique_id,
        tactic=ability.tactic,
        description=ability.description,
        executors=[
            Stage2ExecutorView(
                name=e.name, platform=e.platform, command=e.command
            )
            for e in ability.executors
        ],
        translation_source=TranslationSource.HUMAN_CURATION,
    )


def _adversary_view(adversary: AdversaryData) -> Stage2AdversaryView:
    """Map a curated :class:`AdversaryData` into the Stage-2 Output view (Req. 3.5)."""
    return Stage2AdversaryView(
        id=adversary.id,
        name=adversary.name,
        description=adversary.description,
        atomic_ordering=list(adversary.atomic_ordering),
        translation_source=TranslationSource.HUMAN_CURATION,
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp (portable across PostgreSQL and the test SQLite)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class StageService:
    """Orchestrates Stage 1 (structural modeling) for a curated case.

    Parameters
    ----------
    session:
        An open SQLAlchemy :class:`~sqlalchemy.orm.Session`, injected by the
        caller. The service does not open or close it; it commits on success and
        rolls back on failure so no partial extraction is ever persisted.
    case_service:
        Optional :class:`~app.services.case_service.CaseService`. Defaults to a
        new one pointing at ``<repo>/sticks/data``. Inject a custom one (e.g.
        pointing at temp fixtures) for tests.
    operation_runner:
        Optional object implementing :class:`OperationRunnerProtocol` (the
        ``OperationRunner`` from task 7.3). Stage 3 delegates the actual
        emulation to it (Req. 4.2–4.5); the assembly never re-implements it.
        Defaults to ``None`` — the Stage-3 *assembly* (:meth:`assemble_stage3`)
        works without a runner, but :meth:`run_stage3` requires one to be
        injected.
    stix_service:
        Optional :class:`~app.services.stix.stix_service.StixService` used to
        resolve the Stage-1 Input's ``campaign_stix`` block (the ``campaign``
        object of the selected STIX base). Defaults to a new ``StixService()``;
        inject a custom one (e.g. pointing at temp fixtures) for tests. The
        block is always built defensively, so a read failure here never breaks
        the Stage (the Input is informative — Property 1).
    """

    def __init__(
        self,
        session: Session,
        case_service: Optional[CaseService] = None,
        operation_runner: Optional[OperationRunnerProtocol] = None,
        stix_service: Optional[StixService] = None,
    ) -> None:
        self._session = session
        self._case_service = case_service or CaseService()
        self._operation_runner = operation_runner
        self._stix_service = stix_service or StixService()

    # ---- Stage-1 Input: campaign STIX block (task 2.2) --------------------

    def _build_campaign_stix(
        self, slug: str, version: Optional[str]
    ) -> CampaignStixInput:
        """Build the ``campaign_stix`` block of the Stage-1 Input (Req. 1.1, 3.1, 4.2, 4.3).

        The *conceptual input* of Stage 1 is the STIX ``campaign`` object of
        the base selected in the flow. This helper resolves it for ``slug`` in
        the base ``version`` and returns a :class:`CampaignStixInput` for all
        four scenarios, **never raising** — the Input is informative and must
        never break the structural extraction (Property 1):

        * **missing version** (``version`` ``None``/empty) → ``found=False``,
          ``unavailable_reason="versao_ausente"``, plus the curated identifier
          (name + external_id from :data:`SLUG_TO_STIX`) when available.
        * **bundle unavailable** (:class:`StixFileError` from
          :meth:`StixService.get_campaign_by_slug` / ``read_bundle``) →
          ``found=False``, ``base_version=version``,
          ``unavailable_reason="bundle_indisponivel"`` (error NOT propagated),
          plus the curated identifier when available.
        * **not found** (``get_campaign_by_slug`` returns ``None``) →
          ``found=False``, ``base_version=version``,
          ``unavailable_reason="campanha_nao_encontrada"``, plus the curated
          identifier (name + external_id from :data:`SLUG_TO_STIX`).
        * **found** (rich DTO) → ``found=True``, ``base_version=version``
          with ``stix_id``, ``name``, ``external_id``, ``description``,
          ``aliases``, ``first_seen``, ``last_seen``.

        The whole body is wrapped in ``try/except`` so no exception can escape;
        on an unexpected error it degrades to the ``bundle_indisponivel``
        fallback with the curated identifier.
        """
        try:
            # missing version — nothing to read; fall back to the curated id.
            if not version:
                name, external_id = self._curated_identifier(slug)
                return CampaignStixInput(
                    found=False,
                    base_version=None,
                    name=name,
                    external_id=external_id,
                    unavailable_reason="versao_ausente",
                )

            try:
                detailed = self._stix_service.get_campaign_by_slug(version, slug)
            except StixFileError:
                # bundle unavailable (missing/malformed/invalid) — do NOT
                # propagate: the Input is informative (Property 1).
                name, external_id = self._curated_identifier(slug)
                return CampaignStixInput(
                    found=False,
                    base_version=version,
                    name=name,
                    external_id=external_id,
                    unavailable_reason="bundle_indisponivel",
                )

            # not found — fall back to the curated identifier.
            if detailed is None:
                name, external_id = self._curated_identifier(slug)
                return CampaignStixInput(
                    found=False,
                    base_version=version,
                    name=name,
                    external_id=external_id,
                    unavailable_reason="campanha_nao_encontrada",
                )

            # found — rich fields of the campaign object.
            return CampaignStixInput(
                found=True,
                base_version=version,
                stix_id=detailed.stix_id,
                name=detailed.name,
                external_id=detailed.external_id,
                description=detailed.description,
                aliases=list(detailed.aliases),
                first_seen=detailed.first_seen,
                last_seen=detailed.last_seen,
            )
        except Exception:  # noqa: BLE001 — the Input must never raise.
            # Defensive catch-all: any unexpected error degrades gracefully so
            # the structural extraction is never affected (Property 1).
            name, external_id = self._curated_identifier(slug)
            return CampaignStixInput(
                found=False,
                base_version=version,
                name=name,
                external_id=external_id,
                unavailable_reason="bundle_indisponivel",
            )

    @staticmethod
    def _curated_identifier(slug: str) -> tuple[Optional[str], Optional[str]]:
        """Curated identifier (canonical name, external_id) from :data:`SLUG_TO_STIX`.

        Reuses the single resolution map so the fallback identifier never
        diverges from the campaigns screen (no duplicated rule). Returns
        ``(None, None)`` for an unknown slug.
        """
        entry = SLUG_TO_STIX.get(slug)
        if not entry:
            return (None, None)
        name = entry.get("nome_canonico")
        references = entry.get("referencias") or []
        external_id = references[0] if references else None
        return (name, external_id)

    # ---- public API -------------------------------------------------------

    def run_stage1(self, slug: str, version: Optional[str] = None) -> Stage1Result:
        """Run Stage 1 (structural modeling) for the curated case ``slug``.

        Reads the case's structural DAG, derives the structural elements
        (techniques, relationships, indicators, infrastructure, malware,
        metadata), persists them associated with the case and marks the stage
        ``concluido`` (Req. 2.2, 2.3, 2.4). An empty extraction (no techniques)
        is a SUCCESS with a "no structural elements extracted" note (Req. 2.5).
        Any read/parse/modeling failure marks the stage ``erro`` with
        ``error_message`` and ``failed_step`` set and persists no partial
        extraction (Req. 2.6).

        The ``campaign_stix`` block (the STIX ``campaign`` slice of the base
        ``version``) is attached to the input in every code path. It is built
        defensively and can never fail the structural extraction (Property 1);
        ``version`` is optional and backward-compatible (omitted → ``versao_ausente``
        fallback).

        Args:
            slug: One of the registered curated case slugs.
            version: The selected STIX base version (e.g. ``"18.0"``). Optional;
                when omitted the ``campaign_stix`` falls back to
                ``versao_ausente`` without any error.

        Returns:
            A typed :class:`Stage1Result` (input + output on success; error
            fields on failure). This method does not raise for a bad case — the
            failure is reported in the result — but a genuinely unknown slug
            (programming error) still raises ``KeyError`` from ``CaseService``.
        """
        name = self._case_service.list_cases().get(slug, slug)
        # The campaign STIX block is informative and defensive — never affects
        # the structural extraction (Property 1). Build it once and attach it
        # to the input in every code path.
        campaign_stix = self._build_campaign_stix(slug, version)
        input = Stage1Input(campaign_id=slug, name=name, campaign_stix=campaign_stix)

        # --- Step 1: read the case (DAG + abilities + adversary) -----------
        step = Stage1Step.READ_CASE
        try:
            case = self._case_service.load_case(slug)
        except CaseFileError as exc:
            return self._fail(
                slug,
                input,
                step,
                f"Falha ao ler o caso {slug!r}: {exc.detail}",
            )
        except Exception as exc:  # defensive: any unexpected read error
            return self._fail(
                slug, input, step, f"Erro inesperado ao ler o caso {slug!r}: {exc}"
            )

        # A case with no DAG cannot be structurally modeled — that is a failure
        # (the structural input is missing), not an empty-but-valid extraction.
        if case.dag is None:
            return self._fail(
                slug,
                input,
                step,
                f"Caso {slug!r} não possui DAG estrutural para modelagem.",
            )

        # Enrich the input now that the DAG is loaded (Req. 2.1).
        input = Stage1Input(
            campaign_id=slug,
            name=name,
            campaign_stix=campaign_stix,
            dag_file=case.dag_file,
            stix_reference=case.dag.campaign_file,
            campaign_name=case.dag.campaign_name,
        )

        # --- Step 2 + 3: derive the structural elements --------------------
        try:
            step = Stage1Step.EXTRACT_TECHNIQUES
            techniques = _extract_techniques(case.dag)
            relationships = _extract_relationships(case.dag)

            step = Stage1Step.EXTRACT_ELEMENTS
            indicators = _extract_indicators(case.dag)
            infrastructure = _extract_infrastructure(case.dag)
            malware = _extract_malware(case.dag)
            metadata = _extract_metadata(case.dag)
        except Exception as exc:  # defensive: derivation should not throw
            return self._fail(
                slug,
                input,
                step,
                f"Erro na modelagem estrutural do caso {slug!r}: {exc}",
            )

        # --- Empty extraction => SUCCESS with a note (Req. 2.5) ------------
        if not techniques:
            output = Stage1Output(
                is_empty=True,
                note=(
                    "Nenhum elemento estrutural foi extraído para o caso "
                    f"{name}."
                ),
                metadata=metadata,
            )
        else:
            output = Stage1Output(
                techniques=techniques,
                relationships=relationships,
                indicators=indicators,
                infrastructure=infrastructure,
                malware=malware,
                metadata=metadata,
            )

        # --- Step 4: persist everything in ONE transaction (Req. 2.4/2.6) --
        step = Stage1Step.PERSIST
        try:
            stage_run = self._persist(case, output)
        except SQLAlchemyError as exc:
            # Rollback guarantees no partial extraction is persisted (Req. 2.6).
            self._session.rollback()
            return self._fail(
                slug,
                input,
                step,
                f"Falha ao persistir a extração do caso {slug!r}: {exc}",
            )

        return Stage1Result(
            campaign_id=slug,
            state=StageState.COMPLETED,
            input=input,
            output=output,
            stage_run_id=stage_run.id,
        )

    def read_stage1(
        self, slug: str, version: Optional[str] = None
    ) -> Stage1Result:
        """Read-only view of Stage 1 — NEVER executes or persists.

        The ``GET /campaigns/{campaign}/stage/1`` endpoint must be side-effect
        free: merely opening the stage screen must not run the modeling nor mark
        the stage ``concluido``. This method reflects the PERSISTED state:

        - It always builds the input (derivable from the case files, no write).
        - If the stage's :class:`StageRun` is ``concluido``, it derives the output
          from the DAG purely (same pure extractors as :meth:`run_stage1`) so the
          screen can show what was modeled — WITHOUT persisting anything.
        - If the stage is ``erro``, it surfaces the persisted error detail.
        - Otherwise (``nao_iniciado`` / ``em_andamento`` / no row) it returns the
          current state with ``output=None`` — nothing is shown as modeled until
          the researcher actually runs the stage via ``POST .../execute``.

        The ``campaign_stix`` block (the STIX ``campaign`` slice of the base
        ``version``) is attached to the input in every view (not-started, error
        and completed). It is built defensively and never raises; ``version`` is
        optional and backward-compatible (omitted → ``versao_ausente`` fallback).

        Only :meth:`run_stage1` (the POST path) performs the modeling + write.
        """
        name = self._case_service.list_cases().get(slug, slug)
        # Informative, defensive campaign STIX block — attached in every view.
        campaign_stix = self._build_campaign_stix(slug, version)
        input = Stage1Input(campaign_id=slug, name=name, campaign_stix=campaign_stix)

        # The persisted state is the source of truth for whether the stage ran.
        # Structural modeling is Stage 1 after the renumbering.
        row = self._stage_run_row(slug, STAGE_MODELING)
        state = row.state if row is not None else StageState.NOT_STARTED

        # Try to enrich the input from the case files (read-only). A read
        # failure here is non-fatal for the "not started" view; it only matters
        # when the stage is already concluido (then we surface it).
        case = None
        try:
            case = self._case_service.load_case(slug)
            if case.dag is not None:
                input = Stage1Input(
                    campaign_id=slug,
                    name=name,
                    campaign_stix=campaign_stix,
                    dag_file=case.dag_file,
                    stix_reference=case.dag.campaign_file,
                    campaign_name=case.dag.campaign_name,
                )
        except Exception:  # noqa: BLE001 - read-only view must not raise
            case = None

        # Not completed: report the current state with NO output. Opening the
        # screen never fabricates a "concluido" output (the bug this fixes).
        if state is not StageState.COMPLETED:
            return Stage1Result(
                campaign_id=slug,
                state=state,
                input=input,
                output=None,
                error_message=(row.error_message if row is not None else None),
                failed_step=(row.failed_step if row is not None else None),
                stage_run_id=(row.id if row is not None else None),
            )

        # Completed: derive the output purely from the DAG for display, without
        # any persistence. If the DAG can't be read now, show an empty output.
        if case is not None and case.dag is not None:
            techniques = _extract_techniques(case.dag)
            if techniques:
                output = Stage1Output(
                    techniques=techniques,
                    relationships=_extract_relationships(case.dag),
                    indicators=_extract_indicators(case.dag),
                    infrastructure=_extract_infrastructure(case.dag),
                    malware=_extract_malware(case.dag),
                    metadata=_extract_metadata(case.dag),
                )
            else:
                output = Stage1Output(
                    is_empty=True,
                    note=f"Nenhum elemento estrutural foi extraído para o caso {name}.",
                    metadata=_extract_metadata(case.dag),
                )
        else:
            output = Stage1Output(is_empty=True, note=None)

        return Stage1Result(
            campaign_id=slug,
            state=StageState.COMPLETED,
            input=input,
            output=output,
            stage_run_id=(row.id if row is not None else None),
        )

    # ---- Stage 2: curated-translation assembly (task 8.2) -----------------

    def assemble_stage2(
        self, slug: str, stage1: Optional[Stage1Result] = None
    ) -> Stage2Result:
        """Assemble the Operationalization (Stage 3) input/output for ``slug``.

        Operationalization is Stage 3 (``stage=3``,
        :data:`STAGE_OPERATIONALIZATION`; Req. 8.4) — the stage that turns the
        abstract techniques into executable Abilities (Req. 4.1). For the curated
        cases the operationalization already exists on disk, so this method
        *assembles* it (the method keeps its historical ``assemble_stage2`` name,
        as do the ``Stage2*`` result types):

        * **Input** = the abstract ATT&CK techniques produced upstream
          (Req. 4.1). ``stage1`` may be passed in to avoid re-running the
          upstream modeling; when omitted it is computed via :meth:`run_stage1`.
        * **Output** = the curated Abilities — each with its technique id, tactic,
          description and the concrete command per executor (Req. 4.1) — the
          ordering/dependencies (``atomic_ordering``, Req. 4.1), the Adversary
          grouping (Req. 4.1) and the explicit, extensible operationalization
          source ``translation_source`` = ``curadoria_humana`` (Req. 4.3), with the
          "human curation" indicator attached to the output (Req. 4.3).

        Absence handling (represented cleanly, never as errors):

        * **No Stage-1 techniques** → the input is flagged ``has_input=False``
          with a note and the output is *suppressed* (``output is None``,
          ``suppressed=True``) (Req. 3.6).
        * **No curated Abilities** → the output is present but flagged
          ``has_abilities=False`` with a note (Req. 3.7).
        * **No Adversary** → the output is flagged ``has_adversary=False`` with a
          note indicating there is no Ability grouping (Req. 3.8).

        Args:
            slug: One of the registered curated case slugs.
            stage1: Optional precomputed Stage-1 result to source the input
                techniques from. When ``None``, Stage 1 is run for the case.

        Returns:
            A typed :class:`Stage2Result`.
        """
        name = self._case_service.list_cases().get(slug, slug)

        # Source the abstract techniques from Stage 1 (Req. 3.1). Reuse a passed
        # result when available so callers can avoid re-running Stage 1.
        if stage1 is None:
            stage1 = self.run_stage1(slug)

        input = _stage2_input_from_stage1(slug, name, stage1)

        # No Stage-1 techniques → no Stage-2 input, output suppressed (Req. 3.6).
        if not input.has_input:
            return Stage2Result(
                campaign_id=slug, input=input, output=None, suppressed=True
            )

        # Load the curated Abilities/Adversary for the output. A read/parse
        # failure here should not crash the assembly — represent it as "no
        # abilities" so the UI can render the absence state (Req. 3.7).
        try:
            case = self._case_service.load_case(slug)
        except CaseFileError:
            case = None
        except Exception:  # defensive: any unexpected read error
            case = None

        abilities: list[AbilityData] = list(case.abilities) if case else []
        adversary: Optional[AdversaryData] = case.adversary if case else None

        # No curated Abilities → indicate absence of Stage-2 output (Req. 3.7).
        if not abilities:
            output = Stage2Output(
                abilities=[],
                ordering=[],
                adversary=(_adversary_view(adversary) if adversary else None),
                has_abilities=False,
                has_adversary=adversary is not None,
                note=(
                    "O caso "
                    f"{name} não possui Abilities curadas; não há Saída do "
                    "Estágio 2."
                ),
                adversary_note=(
                    None
                    if adversary is not None
                    else (
                        "Este caso não possui Adversary associado; não há "
                        "agrupamento de Abilities."
                    )
                ),
            )
            return Stage2Result(campaign_id=slug, input=input, output=output)

        ability_views = [_ability_view(a) for a in abilities]

        # Ordering/dependencies: prefer the Adversary's atomic_ordering; fall
        # back to the natural ability order when there is no Adversary (Req. 3.3).
        if adversary is not None:
            ordering = list(adversary.atomic_ordering)
            adversary_view = _adversary_view(adversary)
            adversary_note = None
        else:
            ordering = [a.ability_id for a in abilities]
            adversary_view = None
            adversary_note = (
                "Este caso não possui Adversary associado; não há agrupamento "
                "de Abilities em Adversary."  # Req. 3.8
            )

        output = Stage2Output(
            abilities=ability_views,
            ordering=ordering,
            adversary=adversary_view,
            translation_source=TranslationSource.HUMAN_CURATION,
            human_curation=True,
            has_abilities=True,
            has_adversary=adversary is not None,
            adversary_note=adversary_note,
        )
        return Stage2Result(campaign_id=slug, input=input, output=output)

    # ---- Stage 3: emulation assembly + delegated execution (task 8.2) -----

    def assemble_stage3(self, slug: str) -> Stage3Input:
        """Assemble the Stage-3 input = Adversary + curated Abilities (Req. 4.1).

        This is a pure, side-effect-free assembly so the API layer can call it
        independently of running the emulation (e.g. to render the input or to
        build a preview before confirmation). It does **not** touch the
        OperationRunner.

        ``can_emulate`` is ``True`` only when both an Adversary and at least one
        Ability are present; otherwise there is nothing to emulate and ``note``
        explains the absence.

        Args:
            slug: One of the registered curated case slugs.

        Returns:
            A typed :class:`Stage3Input`.
        """
        name = self._case_service.list_cases().get(slug, slug)

        try:
            case = self._case_service.load_case(slug)
        except CaseFileError:
            case = None
        except Exception:  # defensive
            case = None

        abilities = list(case.abilities) if case else []
        adversary = case.adversary if case else None

        ability_inputs = [
            Stage3AbilityInput(
                ability_id=a.ability_id,
                name=a.name,
                technique_id=a.technique_id,
                tactic=a.tactic,
                executors=[
                    Stage2ExecutorView(
                        name=e.name, platform=e.platform, command=e.command
                    )
                    for e in a.executors
                ],
            )
            for a in abilities
        ]
        adversary_input = (
            Stage3AdversaryInput(
                id=adversary.id,
                name=adversary.name,
                description=adversary.description,
                atomic_ordering=list(adversary.atomic_ordering),
            )
            if adversary is not None
            else None
        )

        can_emulate = adversary_input is not None and bool(ability_inputs)
        note: Optional[str] = None
        if not can_emulate:
            if adversary_input is None and not ability_inputs:
                note = (
                    "Este caso não possui Adversary nem Abilities curadas; não "
                    "há emulação a executar."
                )
            elif adversary_input is None:
                note = (
                    "Este caso não possui Adversary associado; não há emulação "
                    "a executar."
                )
            else:
                note = (
                    "Este caso não possui Abilities curadas; não há emulação a "
                    "executar."
                )

        return Stage3Input(
            campaign_id=slug,
            name=name,
            adversary=adversary_input,
            abilities=ability_inputs,
            can_emulate=can_emulate,
            note=note,
        )

    def run_stage3(self, slug: str, *, confirmed: bool = False) -> Stage3Result:
        """Run Stage 3 by delegating execution to the injected OperationRunner.

        Assembles the input (Adversary + Abilities, Req. 4.1) and, when an
        Adversary and Abilities are present and ``confirmed`` is true, delegates
        the emulation to the injected :class:`OperationRunnerProtocol` (task
        7.3) — this method never re-implements the Caldera calls, containment
        pre-flight, auditing or aggregation. It maps the runner's result into the
        Stage-3 output = per-Ability result + aggregate (Req. 4.4, 4.5).

        The runner is responsible for enforcing explicit confirmation and the
        containment/availability pre-flight (Req. 4.6, 6.x); this method simply
        forwards ``confirmed`` and surfaces the runner's ``message`` /
        ``error_message`` when it refuses to execute. When there is nothing to
        emulate, or ``confirmed`` is false, no runner call is made and
        ``executed=False``.

        Args:
            slug: One of the registered curated case slugs.
            confirmed: The explicit confirmation flag (Req. 6.6, 6.7).

        Returns:
            A typed :class:`Stage3Result`.

        Raises:
            RuntimeError: if execution is requested (an emulatable, confirmed
                case) but no OperationRunner was injected.
        """
        input = self.assemble_stage3(slug)

        # Nothing to emulate → assembled input, no execution (Req. 4.1).
        if not input.can_emulate:
            return Stage3Result(
                campaign_id=slug,
                input=input,
                output=None,
                executed=False,
                message=input.note,
            )

        # No explicit confirmation → do not execute anything (Req. 6.6, 6.7).
        if not confirmed:
            return Stage3Result(
                campaign_id=slug,
                input=input,
                output=None,
                executed=False,
                message=(
                    "A emulação requer confirmação explícita do Pesquisador "
                    "antes de iniciar."
                ),
            )

        if self._operation_runner is None:
            raise RuntimeError(
                "Nenhum OperationRunner injetado: a execução do Estágio 3 "
                "requer um OperationRunner (tarefa 7.3)."
            )

        # Delegate the actual emulation (Req. 4.2–4.5). Reload the full case so
        # the runner has the complete curated data to load into Caldera.
        case = self._case_service.load_case(slug)
        run_result = self._operation_runner.run(case, confirmed=confirmed)

        # Runner refused to execute (containment violation, Caldera unavailable,
        # audit failure) → surface the message without an output (Req. 4.6, 6.x).
        if not run_result.executed:
            return Stage3Result(
                campaign_id=slug,
                input=input,
                output=None,
                executed=False,
                message=run_result.message,
                error_message=run_result.error_message,
            )

        output = Stage3Output(
            results=list(run_result.results),
            aggregate=Stage3Aggregate(
                total_success=run_result.total_success,
                total_failure=run_result.total_failure,
                state=run_result.state,
            ),
            operation_id=run_result.operation_id,
        )
        return Stage3Result(
            campaign_id=slug,
            input=input,
            output=output,
            executed=True,
            message=run_result.message,
        )

    # ---- sequential blocking + aggregate progress (task 8.3) --------------

    def get_stage_state(self, campaign_id: str, stage: int) -> StageStateView:
        """Return the current state of stage ``stage`` for ``campaign_id`` (Req. 5.3).

        Reads the stage's persisted :class:`~app.models.domain.StageRun`. When
        there is no row yet the stage is ``nao_iniciado`` with 0% progress
        (Req. 9.5). The ``blocked`` / ``startable`` flags encode the sequential
        gate (Req. 5.4): a stage is startable iff it is Stage 1 or its preceding
        stage is ``concluido``. ``error_message`` / ``failed_step`` are surfaced
        when the stage is in ``erro`` (Req. 1.6, 2.6).

        Args:
            campaign_id: The curated case slug.
            stage: The stage number (1, 2, 3, 4, 5 or 6).

        Returns:
            A typed :class:`StageStateView`.

        Raises:
            ValueError: if ``stage`` is not one of (1, 2, 3, 4, 5, 6).
        """
        _validate_stage(stage)
        states = self._stage_states_map(campaign_id)
        return self._build_stage_state_view(campaign_id, stage, states)

    def get_stage_states(self, campaign_id: str) -> CaseStageStates:
        """Return the per-stage state of a case (Req. 5.3, 5.4).

        Assembles one :class:`StageStateView` per stage (1..6), each carrying its
        state, progress percentage and ``blocked`` flag, and flags the case as
        ``fully_completed`` when all six stages are ``concluido`` (the unit the
        aggregate progress counts — Req. 1.5).

        Reads all of the case's ``StageRun`` rows once, so the blocking decision
        for stage *n* uses the same snapshot as stage *n-1*'s state.

        Args:
            campaign_id: The curated case slug.

        Returns:
            A typed :class:`CaseStageStates`.
        """
        states = self._stage_states_map(campaign_id)
        views = [
            self._build_stage_state_view(campaign_id, stage, states)
            for stage in VALID_STAGES
        ]
        fully_completed = all(
            v.state is StageState.COMPLETED for v in views
        )
        return CaseStageStates(
            campaign_id=campaign_id, stages=views, fully_completed=fully_completed
        )

    def is_stage_startable(self, campaign_id: str, stage: int) -> bool:
        """Whether stage ``stage`` of ``campaign_id`` can be started now (Req. 5.4).

        Stage 1 is always startable. Any later stage *n* is startable iff its
        immediately preceding stage *n-1* is ``concluido``; otherwise it is
        BLOCKED and must not be started (Req. 5.4 — Property 3 territory). This
        depends only on the preceding stage's persisted state, never on the
        stage's own state, so the gate is purely sequential.

        Args:
            campaign_id: The curated case slug.
            stage: The stage number (1, 2, 3, 4, 5 or 6).

        Returns:
            ``True`` if the stage is startable, ``False`` if it is blocked.

        Raises:
            ValueError: if ``stage`` is not one of (1, 2, 3, 4, 5, 6).
        """
        _validate_stage(stage)
        if stage == FIRST_STAGE:
            return True
        states = self._stage_states_map(campaign_id)
        previous_state = states.get(stage - 1, StageState.NOT_STARTED)
        return previous_state is StageState.COMPLETED

    def aggregate_progress(
        self, campaign_ids: Optional[list[str]] = None
    ) -> AggregateProgress:
        """Aggregate replication progress across the curated cases (Req. 5.5).

        Counts exactly the cases whose **all six** stages are ``concluido``,
        out of the case universe. By default the universe is the 8 curated cases
        (from :meth:`CaseService.case_slugs`); pass ``campaign_ids`` to score a
        specific set (used by tests to drive distributions).

        The count reads ``StageRun`` rows only: a case with fewer than six
        ``concluido`` stages — including a case with no rows at all — does not
        count (Req. 1.5, 9.5). ``percentage`` is the integer percentage
        ``completed_campaigns / total_campaigns * 100`` for display (0 when the universe
        is empty).

        Args:
            campaign_ids: Optional explicit case universe. Defaults to the 8 curated
                case slugs.

        Returns:
            A typed :class:`AggregateProgress`.
        """
        if campaign_ids is None:
            campaign_ids = self._case_service.case_slugs()

        completed_ids = [
            campaign_id
            for campaign_id in campaign_ids
            if self._is_case_fully_completed(campaign_id)
        ]
        total = len(campaign_ids)
        done = len(completed_ids)
        percentage = round(done * 100 / total) if total else 0
        return AggregateProgress(
            completed_campaigns=done,
            total_campaigns=total,
            percentage=percentage,
            completed_campaign_ids=completed_ids,
        )

    def stage_progress_event(
        self,
        campaign_id: str,
        stage: int,
        *,
        previous_state: Optional[StageState] = None,
    ) -> StageProgressEvent:
        """Build a stage state/percentage report for the real-time channels.

        Exposes the payload the WebSocket layer (task 11.3) pushes on
        ``WS /ws/stages/{campaign}``: the stage's current ``state`` and
        ``progress`` percentage (Req. 1.4, 1.5), the optional
        ``previous_state`` it transitioned from (Req. 1.4), and — when the
        stage is in ``erro`` — the ``error_message`` / ``failed_step`` cause
        (Req. 1.6). The WebSocket loop re-emits this at least every 5s while the
        stage is ``em_andamento`` so the UI shows the live percentage (Req. 1.5).

        This is a pure read over the persisted ``StageRun`` — reporting only,
        never mutating state.

        Args:
            campaign_id: The curated case slug.
            stage: The stage number (1, 2, 3, 4, 5 or 6).
            previous_state: The state the stage transitioned from, when this
                event is emitted in response to a change (Req. 1.4).

        Returns:
            A typed :class:`StageProgressEvent`.

        Raises:
            ValueError: if ``stage`` is not one of (1, 2, 3, 4, 5, 6).
        """
        _validate_stage(stage)
        view = self.get_stage_state(campaign_id, stage)
        return StageProgressEvent(
            campaign_id=campaign_id,
            stage=stage,
            state=view.state,
            progress=view.progress,
            previous_state=previous_state,
            error_message=view.error_message,
            failed_step=view.failed_step,
        )

    # ---- blocking/progress helpers (pure reads over StageRun) -------------

    def _stage_states_map(self, campaign_id: str) -> dict[int, StageState]:
        """Map ``stage -> StageState`` for a case, from its ``StageRun`` rows.

        Reads all of the case's stage runs in one query so a single snapshot
        drives every stage decision. Stages with no row are simply absent from
        the map; callers default them to ``nao_iniciado`` (Req. 9.5). If two rows
        somehow exist for the same stage, the highest-``id`` (latest) one wins.
        """
        stmt = (
            select(StageRun.stage, StageRun.state)
            .where(StageRun.campaign_id == campaign_id)
            .order_by(StageRun.id.asc())
        )
        states: dict[int, StageState] = {}
        for stage, state in self._session.execute(stmt).all():
            states[stage] = state
        return states

    def _stage_run_row(self, campaign_id: str, stage: int) -> Optional[StageRun]:
        """Return the latest ``StageRun`` row for ``(campaign_id, stage)``, if any.

        When more than one row exists for the same ``(campaign_id, stage)`` the
        highest-``id`` (latest) one wins, matching :meth:`_stage_states_map`.
        """
        stmt = (
            select(StageRun)
            .where(StageRun.campaign_id == campaign_id)
            .where(StageRun.stage == stage)
            .order_by(StageRun.id.asc())
        )
        rows = self._session.execute(stmt).scalars().all()
        return rows[-1] if rows else None

    def _build_stage_state_view(
        self, campaign_id: str, stage: int, states: dict[int, StageState]
    ) -> StageStateView:
        """Assemble a :class:`StageStateView` from a states snapshot + the row.

        ``states`` provides the state of every stage (for the blocking decision);
        the individual row is read for the persisted ``progress`` /
        ``error_message`` / ``failed_step`` detail.
        """
        state = states.get(stage, StageState.NOT_STARTED)
        # Sequential gate (Req. 5.4): Stage 1 always startable; later stages
        # startable only when the previous stage is concluido.
        if stage == FIRST_STAGE:
            startable = True
        else:
            startable = (
                states.get(stage - 1, StageState.NOT_STARTED)
                is StageState.COMPLETED
            )

        progress = 0
        error_message: Optional[str] = None
        failed_step: Optional[str] = None
        row = self._stage_run_row(campaign_id, stage)
        if row is not None:
            progress = row.progress
            error_message = row.error_message
            failed_step = row.failed_step

        return StageStateView(
            stage=stage,
            state=state,
            progress=progress,
            blocked=not startable,
            startable=startable,
            error_message=error_message,
            failed_step=failed_step,
        )

    def _is_case_fully_completed(self, campaign_id: str) -> bool:
        """Whether all six stages of ``campaign_id`` are ``concluido`` (Req. 1.5)."""
        states = self._stage_states_map(campaign_id)
        return all(
            states.get(stage) is StageState.COMPLETED
            for stage in VALID_STAGES
        )

    # ---- persistence ------------------------------------------------------

    def _persist(self, case: CaseData, output: Stage1Output) -> StageRun:
        """Persist the case, its extracted elements and the completed StageRun.

        Runs as a single unit of work committed once at the end. On any DB error
        the caller rolls back, so either *all* extracted elements are persisted
        or *none* are (no partial extraction — Req. 2.6). The ``StageRun`` is
        upserted (one row per ``(campaign_id, stage=1)``) so re-running the
        structural modeling for a case updates the existing run rather than
        piling up rows. Structural modeling persists under stage 1 after the
        renumbering.
        """
        # Upsert the Case row (metadata + file paths + translation source).
        db_case = self._session.get(CaseModel, case.id)
        if db_case is None:
            db_case = CaseModel(id=case.id)
            self._session.add(db_case)
        db_case.name = case.name
        db_case.ability_file = case.ability_file
        db_case.adversary_file = case.adversary_file
        db_case.dag_file = case.dag_file
        db_case.translation_source = case.translation_source

        # Flush the parent Case row BEFORE inserting any child Ability/Adversary.
        # The children carry a FK to campaigns.id; without this explicit flush the
        # unit-of-work can order a child INSERT before the parent, raising a
        # ForeignKeyViolation (abilities_campaign_id_fkey). Still one transaction:
        # the single commit() at the end keeps the all-or-nothing guarantee.
        self._session.flush()

        # Persist the curated Abilities (upsert by ability_id), associated with
        # the case. Executors are stored as the JSON column.
        for ability in case.abilities:
            db_ability = self._session.get(AbilityModel, ability.ability_id)
            if db_ability is None:
                db_ability = AbilityModel(ability_id=ability.ability_id)
                self._session.add(db_ability)
            db_ability.campaign_id = case.id
            db_ability.name = ability.name
            db_ability.tactic = ability.tactic
            db_ability.technique_name = ability.technique_name
            db_ability.technique_id = ability.technique_id
            db_ability.description = ability.description
            db_ability.executors = [e.model_dump() for e in ability.executors]
            db_ability.translation_source = TranslationSource.HUMAN_CURATION

        # Persist the Adversary when present (upsert by id).
        if case.adversary is not None:
            db_adv = self._session.get(AdversaryModel, case.adversary.id)
            if db_adv is None:
                db_adv = AdversaryModel(id=case.adversary.id)
                self._session.add(db_adv)
            db_adv.campaign_id = case.id
            db_adv.name = case.adversary.name
            db_adv.description = case.adversary.description
            db_adv.atomic_ordering = list(case.adversary.atomic_ordering)
            db_adv.translation_source = TranslationSource.HUMAN_CURATION

        # Upsert the structural-modeling StageRun (Stage 1) and mark it
        # concluido (Req. 2.4).
        stage_run = self._get_or_create_stage_run(
            case.id, stage=STAGE_MODELING
        )
        stage_run.state = StageState.COMPLETED
        stage_run.progress = 100
        stage_run.error_message = None
        stage_run.failed_step = None
        stage_run.updated_at = _utcnow()

        self._session.commit()
        self._session.refresh(stage_run)
        return stage_run

    def _get_or_create_stage_run(self, campaign_id: str, *, stage: int) -> StageRun:
        """Return the existing Stage-``stage`` run for the case, or a new one."""
        stmt = (
            select(StageRun)
            .where(StageRun.campaign_id == campaign_id)
            .where(StageRun.stage == stage)
        )
        stage_run = self._session.execute(stmt).scalars().first()
        if stage_run is None:
            stage_run = StageRun(campaign_id=campaign_id, stage=stage)
            self._session.add(stage_run)
        return stage_run

    # ---- failure handling (Req. 2.6) -------------------------------------

    def _fail(
        self,
        slug: str,
        input: Stage1Input,
        step: Stage1Step,
        error_message: str,
    ) -> Stage1Result:
        """Record a structural-modeling failure without persisting any partial extraction.

        Marks the modeling ``StageRun`` (Stage 1) as ``erro`` with the failure cause
        (``error_message``) and the modeling step that failed (``failed_step``),
        and returns the error :class:`Stage1Result` (Req. 2.6). Persisting *only*
        the failed modeling StageRun is intentional: it records the affected case and the
        failing step, and carries **no** extracted elements (Ability/Adversary
        rows are only written on the success path).

        If even the failure-marker write fails, the DB error is swallowed (the
        session is rolled back) so callers always get a descriptive result
        rather than an exception — the in-memory result still identifies the
        case, cause and step.
        """
        try:
            stage_run = self._get_or_create_stage_run(
                slug, stage=STAGE_MODELING
            )
            stage_run.state = StageState.ERROR
            stage_run.error_message = error_message
            stage_run.failed_step = step.value
            stage_run.updated_at = _utcnow()
            self._session.commit()
            stage_run_id = stage_run.id
        except SQLAlchemyError:
            self._session.rollback()
            stage_run_id = None

        return Stage1Result(
            campaign_id=slug,
            state=StageState.ERROR,
            input=input,
            output=None,
            error_message=error_message,
            failed_step=step.value,
            stage_run_id=stage_run_id,
        )


__all__ = [
    "StageService",
    # Stage 1
    "Stage1Result",
    "Stage1Input",
    "Stage1Output",
    "Stage1Step",
    "TechniqueElement",
    "RelationshipElement",
    "IndicatorElement",
    "InfrastructureElement",
    "MalwareElement",
    # Stage 2
    "Stage2Result",
    "Stage2Input",
    "Stage2InputTechnique",
    "Stage2Output",
    "Stage2AbilityView",
    "Stage2AdversaryView",
    "Stage2ExecutorView",
    # Stage 3
    "Stage3Result",
    "Stage3Input",
    "Stage3AbilityInput",
    "Stage3AdversaryInput",
    "Stage3Output",
    "Stage3AbilityResult",
    "Stage3Aggregate",
    # OperationRunner seam (task 7.3)
    "OperationRunnerProtocol",
    "OperationRunResult",
    # Sequential blocking + aggregate progress + reporting (task 8.3)
    "StageStateView",
    "CaseStageStates",
    "AggregateProgress",
    "StageProgressEvent",
    "VALID_STAGES",
    "FIRST_STAGE",
    "LAST_STAGE",
    # Roles of the six stages (new names — single source per number)
    "STAGE_BASE_SELECTION",
    "STAGE_CAMPAIGN_SELECTION",
    "STAGE_DAG_GENERATION",
    "STAGE_OPERATIONALIZATION",
    "STAGE_ENVIRONMENT",
    "STAGE_EXECUTION",
    # Historical aliases kept so as not to break imports (Req. 18.5)
    "STAGE_MODELING",
    "STAGE_PROVISIONING",
]
