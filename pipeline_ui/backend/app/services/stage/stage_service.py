"""StageService — Stage 1/2/3 orchestration (tasks 8.1, 8.2, 8.3).

Stage 1 (structural modeling) orchestration (task 8.1).

Stage 1 of the sticks pipeline is *Modelagem Estrutural Automatizada*: for a
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
  ``StageRun.estado`` to ``concluido`` (Req. 2.4).
* **Empty extraction**: a case that yields **no techniques** is *not* an error.
  It is treated as SUCCESS with a :class:`Stage1Output` flagged ``is_empty`` and
  a human-readable note that no structural element was extracted (Req. 2.5).
* **Failure**: an unreadable / invalid / malformed case (or an unexpected error
  during modeling) marks the stage ``erro``, records ``mensagem_erro`` and the
  modeling ``etapa_falha`` (Req. 2.6), and persists **no partial extraction**:
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

_Requisitos: 2.2, 2.3, 2.4, 2.5, 2.6_
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
from app.services.case_service import (
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


# ---------------------------------------------------------------------------
# Modeling steps (used for ``etapa_falha`` on failure — Req. 2.6)
# ---------------------------------------------------------------------------


class Stage1Step(str, enum.Enum):
    """The Stage-1 modeling steps, in execution order.

    On failure the step in progress is recorded on ``StageRun.etapa_falha`` so
    the UI can identify *where* the structural modeling failed for the affected
    case (Req. 2.6). Subclasses ``str`` for a clean DB/JSON value.
    """

    READ_CASE = "leitura_do_caso"                 # load DAG/abilities/adversary
    EXTRACT_TECHNIQUES = "extracao_de_tecnicas"    # techniques + relationships
    EXTRACT_ELEMENTS = "extracao_de_elementos"     # indicators/infra/malware/meta
    PERSIST = "persistencia"                        # persist extracted elements


# ---------------------------------------------------------------------------
# Typed Stage-1 result (Entrada / Saída) — for the API / UI later
# ---------------------------------------------------------------------------


class TechniqueElement(BaseModel):
    """One extracted ATT&CK technique (Req. 2.3 — técnicas)."""

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


class Stage1Input(BaseModel):
    """Entrada do Estágio 1 — the source STIX/DAG reference (Req. 2.1).

    The dev pipeline derives Stage 1 from the DAG the curated pipeline produced,
    so the Entrada references the DAG file (and the campaign it models) rather
    than a raw STIX bundle. ``stix_reference`` names the source dataset when the
    DAG records it (``campaign_file``).
    """

    caso_id: str
    nome: str
    dag_file: Optional[str] = None
    stix_reference: Optional[str] = None
    campaign_name: Optional[str] = None


class Stage1Output(BaseModel):
    """Saída do Estágio 1 — the extracted structural elements (Req. 2.3, 2.5).

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

    Carries the final ``estado`` (``concluido`` on success/empty, ``erro`` on
    failure), the Entrada/Saída, and, on failure, the ``mensagem_erro`` and the
    modeling ``etapa_falha`` (Req. 2.6). ``stage_run_id`` links to the persisted
    :class:`~app.models.domain.StageRun` row.
    """

    caso_id: str
    estado: StageState
    entrada: Stage1Input
    saida: Optional[Stage1Output] = None
    mensagem_erro: Optional[str] = None
    etapa_falha: Optional[str] = None
    stage_run_id: Optional[int] = None

    @property
    def ok(self) -> bool:
        """``True`` when the stage completed (success or empty extraction)."""
        return self.estado is StageState.COMPLETED

    @property
    def is_error(self) -> bool:
        """``True`` when the stage ended in error."""
        return self.estado is StageState.ERROR


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
# Stage 2 — typed Entrada/Saída (task 8.2)
# ===========================================================================
#
# Stage 2 of the sticks pipeline is *Tradução com Humano no Loop*: a human
# analyst turns the abstract ATT&CK techniques from Stage 1 into the minimal
# executable steps — the concrete curated Abilities, their ordering /
# dependencies, and the Adversary grouping (Req. 3). For the 8 curated cases
# this translation *already exists* on disk (``data/api/{slug}_dag-ability.json``
# and ``{slug}_dag-adversary.json``), read by :class:`CaseService`. So, in the
# dev pipeline, **Stage 2 = assemble the Entrada from the Stage-1 techniques and
# the Saída from the curated Abilities/Adversary**, tagging the explicit,
# extensible translation source (Req. 11.1). No emulation happens here.
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
    """Entrada do Estágio 2 — the abstract techniques from Stage 1 (Req. 3.1).

    ``has_input`` is ``False`` when Stage 1 produced no technique; in that case
    ``note`` carries the "no Stage-2 input" message and the Saída is suppressed
    (Req. 3.6).
    """

    caso_id: str
    nome: str
    techniques: list[Stage2InputTechnique] = Field(default_factory=list)
    has_input: bool = True
    note: Optional[str] = None


class Stage2ExecutorView(BaseModel):
    """A single executor of a curated Ability — the concrete command (Req. 3.2)."""

    name: Optional[str] = None
    platform: Optional[str] = None
    command: Optional[str] = None


class Stage2AbilityView(BaseModel):
    """One curated Ability as shown in the Stage-2 Saída (Req. 3.2).

    Carries the technique id, tactic and description, plus the concrete command
    of each executor. ``origem_traducao`` is the explicit, extensible
    translation source (Req. 11.1) — ``"curadoria_humana"`` for the 8 curated
    cases.
    """

    ability_id: str
    name: Optional[str] = None
    technique_id: Optional[str] = None
    tactic: Optional[str] = None
    description: Optional[str] = None
    executors: list[Stage2ExecutorView] = Field(default_factory=list)
    origem_traducao: TranslationSource = TranslationSource.HUMAN_CURATION


class Stage2AdversaryView(BaseModel):
    """The Adversary grouping of Abilities shown in the Stage-2 Saída (Req. 3.5).

    ``atomic_ordering`` is the ordered list of ability ids that composes the
    Adversary — the sequence/dependencies between Abilities (Req. 3.3).
    """

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    atomic_ordering: list[str] = Field(default_factory=list)
    origem_traducao: TranslationSource = TranslationSource.HUMAN_CURATION


class Stage2Output(BaseModel):
    """Saída do Estágio 2 — the curated translation (Req. 3.2–3.5, 3.7, 3.8, 11.1).

    Holds the curated Abilities (each with the concrete command per executor),
    the ordering/dependencies (``atomic_ordering``), the Adversary grouping and
    the explicit translation source ``origem_traducao`` (Req. 11.1).

    Absence flags let the UI render the distinct states cleanly:

    * ``has_abilities`` is ``False`` — the case has no curated Abilities, so
      there is no Stage-2 Saída (Req. 3.7); ``note`` explains it.
    * ``has_adversary`` is ``False`` — the case has no Adversary grouping, so the
      Abilities are not grouped into an Adversary (Req. 3.8);
      ``adversary_note`` explains it.

    ``curadoria_humana`` (``human_curation`` flag + textual ``origem_traducao``)
    is the explicit "human curation" indicator associated with the Saída
    (Req. 3.4).
    """

    abilities: list[Stage2AbilityView] = Field(default_factory=list)
    ordering: list[str] = Field(default_factory=list)
    adversary: Optional[Stage2AdversaryView] = None
    origem_traducao: TranslationSource = TranslationSource.HUMAN_CURATION
    human_curation: bool = True
    has_abilities: bool = True
    has_adversary: bool = True
    note: Optional[str] = None
    adversary_note: Optional[str] = None


class Stage2Result(BaseModel):
    """The full typed Stage-2 result returned to the API/UI (Req. 3).

    Mirrors the :class:`Stage1Result` style. ``suppressed`` is ``True`` when
    Stage 1 produced no technique: the Entrada has no input (Req. 3.6) and the
    Saída is suppressed (``saida is None``). Otherwise ``saida`` carries the
    curated translation, including its absence sub-states (no abilities / no
    adversary).
    """

    caso_id: str
    entrada: Stage2Input
    saida: Optional[Stage2Output] = None
    suppressed: bool = False


# ===========================================================================
# Stage 3 — typed Entrada/Saída (task 8.2)
# ===========================================================================
#
# Stage 3 is *Emulação de Adversário*: the curated Adversary + Abilities are
# loaded into Caldera and run as an Operation against the isolated Docker
# environment (Req. 4). The execution itself (containment pre-flight, Caldera
# calls, per-command auditing, aggregation) is the responsibility of the
# ``OperationRunner`` (task 7.3) — this module does NOT re-implement it. Instead
# it:
#
# * assembles the Entrada = Adversary + curated Abilities (Req. 4.1), and
# * delegates execution to an injected :class:`OperationRunnerProtocol`, mapping
#   its result into the Saída = per-Ability result + aggregate (Req. 4.4, 4.5).
#
# The assembly is kept independent of the runner so the API layer can call
# preview / confirm / execute separately: :meth:`StageService.assemble_stage3`
# builds the Entrada (and the runner input) with no side effects, and
# :meth:`StageService.run_stage3` performs the delegated execution.


class Stage3AbilityInput(BaseModel):
    """One curated Ability listed in the Stage-3 Entrada (Req. 4.1)."""

    ability_id: str
    name: Optional[str] = None
    technique_id: Optional[str] = None
    tactic: Optional[str] = None
    executors: list[Stage2ExecutorView] = Field(default_factory=list)


class Stage3AdversaryInput(BaseModel):
    """The Adversary listed in the Stage-3 Entrada (Req. 4.1)."""

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    atomic_ordering: list[str] = Field(default_factory=list)


class Stage3Input(BaseModel):
    """Entrada do Estágio 3 — the Adversary + curated Abilities (Req. 4.1).

    ``can_emulate`` is ``True`` only when both an Adversary and at least one
    Ability are present (otherwise there is nothing to run); ``note`` explains
    the absence when it is ``False``.
    """

    caso_id: str
    nome: str
    adversary: Optional[Stage3AdversaryInput] = None
    abilities: list[Stage3AbilityInput] = Field(default_factory=list)
    can_emulate: bool = True
    note: Optional[str] = None


class Stage3AbilityResult(BaseModel):
    """The per-Ability result of the Operation (Req. 4.4).

    ``status`` is one of ``pendente`` / ``em_execucao`` / ``sucesso`` / ``falha``
    (:class:`~app.models.enums.AbilityResultStatus`); ``saida_comando`` is the
    executed command's output.
    """

    ability_id: str
    name: Optional[str] = None
    status: AbilityResultStatus = AbilityResultStatus.PENDING
    saida_comando: Optional[str] = None


class Stage3Aggregate(BaseModel):
    """The aggregate Operation result (Req. 4.5).

    ``total_sucesso`` / ``total_falha`` count the Abilities that finished with
    ``sucesso`` / ``falha``; ``estado`` is the final Operation state.
    """

    total_sucesso: int = 0
    total_falha: int = 0
    estado: OperationState = OperationState.NOT_STARTED


class Stage3Output(BaseModel):
    """Saída do Estágio 3 — per-Ability result + aggregate (Req. 4.4, 4.5).

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
    is ``False`` and ``saida is None``; ``mensagem`` explains why. When the
    runner ran, ``saida`` carries the per-Ability results and the aggregate.
    ``mensagem_erro`` carries a containment/availability/audit failure surfaced
    by the runner without executing (Req. 4.6, 6.2, 6.4, 6.9).
    """

    caso_id: str
    entrada: Stage3Input
    saida: Optional[Stage3Output] = None
    executed: bool = False
    mensagem: Optional[str] = None
    mensagem_erro: Optional[str] = None


# ===========================================================================
# Sequential blocking + aggregate progress + state/percentage reporting
# (task 8.3)
# ===========================================================================
#
# The Pipeline_UI conducts each curated case through Stage 1 -> 2 -> 3 in order
# (Req. 5.2). A later stage is *blocked* — and cannot be started — while its
# immediately preceding stage is not "concluido" (Req. 5.4). The aggregate
# replication progress counts exactly the cases whose all three stages are
# "concluido", out of the 8 curated cases (Req. 5.5). Finally, the real-time
# channels (task 11.3) need a way to report a stage's current state and progress
# percentage, and the state transition it just made (Req. 1.4, 1.5, 1.6).
#
# All of this is *pure logic over the persisted ``StageRun`` rows*: the state of
# a stage is read from its ``StageRun`` row (defaulting to ``nao_iniciado`` when
# there is no row yet), so it works identically on the app's ``SessionLocal``
# and an ephemeral test SQLite session. There is no re-modeling here — the
# Stage 1/2/3 assembly above is left intact; this section only reads state and
# derives the blocking/progress views.

# The three pipeline stages, in conduction order (Req. 5.2). A curated case has
# exactly these stages; ``VALID_STAGES`` bounds every stage argument.
FIRST_STAGE: int = 1
LAST_STAGE: int = 3
VALID_STAGES: tuple[int, ...] = (1, 2, 3)


class StageStateView(BaseModel):
    """The state of a single stage of a case (Req. 5.3, 1.4).

    Attributes
    ----------
    estagio:
        The stage number (1, 2 or 3).
    estado:
        The stage state — exactly one of ``nao_iniciado`` / ``em_andamento`` /
        ``concluido`` / ``erro`` (:class:`~app.models.enums.StageState`),
        derived from the stage's ``StageRun`` row (``nao_iniciado`` when there
        is no row yet — Req. 9.5).
    progresso:
        The processing percentage 0..100 (Req. 1.5). Persisted on the
        ``StageRun``; defaults to 0 when there is no row.
    bloqueado:
        ``True`` when the stage cannot be started because its immediately
        preceding stage is not ``concluido`` (Req. 5.4). Stage 1 is never
        blocked.
    iniciavel:
        ``True`` when the stage can be started now — i.e. it is Stage 1, or its
        preceding stage is ``concluido`` (Req. 5.4). The exact complement of
        ``bloqueado``.
    mensagem_erro:
        The failure cause when ``estado`` is ``erro`` (Req. 1.6), else ``None``.
    etapa_falha:
        The Stage-1 modeling step that failed, when recorded (Req. 2.6).
    """

    estagio: int
    estado: StageState = StageState.NOT_STARTED
    progresso: int = 0
    bloqueado: bool = False
    iniciavel: bool = True
    mensagem_erro: Optional[str] = None
    etapa_falha: Optional[str] = None


class CaseStageStates(BaseModel):
    """The per-stage state of a case plus whether it is fully replicated.

    ``estagios`` holds one :class:`StageStateView` per stage (1..3), in order,
    each carrying its state, percentage and ``bloqueado`` flag (Req. 5.3). A case
    is ``concluido_total`` iff all three stages are ``concluido`` (the unit the
    aggregate progress counts — Req. 5.5).
    """

    caso_id: str
    estagios: list[StageStateView] = Field(default_factory=list)
    concluido_total: bool = False


class AggregateProgress(BaseModel):
    """The aggregate replication progress across the 8 curated cases (Req. 5.5).

    ``casos_concluidos`` is the number of cases whose all three stages are
    ``concluido``; ``total_casos`` is the case universe (8 curated cases);
    ``percentual`` is ``casos_concluidos / total_casos`` as an integer 0..100 for
    display. ``casos_concluidos_ids`` lists which cases are fully replicated so
    the UI can highlight them.
    """

    casos_concluidos: int = 0
    total_casos: int = 0
    percentual: int = 0
    casos_concluidos_ids: list[str] = Field(default_factory=list)


class StageProgressEvent(BaseModel):
    """A stage state/percentage report for the real-time channels (Req. 1.4–1.6).

    This is the payload the WebSocket layer (task 11.3, ``WS /ws/estagios/{caso}``)
    pushes: the stage's current ``estado`` and ``progresso`` percentage, plus —
    when this event is emitted in response to a change — the ``estado_anterior``
    it transitioned from (Req. 1.4). While a stage is ``em_andamento`` the channel
    re-emits this at least every 5s so the UI can show the live percentage
    (Req. 1.5); on failure ``mensagem_erro`` carries the cause (Req. 1.6).
    """

    caso_id: str
    estagio: int
    estado: StageState
    progresso: int = 0
    estado_anterior: Optional[StageState] = None
    mensagem_erro: Optional[str] = None
    etapa_falha: Optional[str] = None


def _validate_stage(estagio: int) -> None:
    """Guard that ``estagio`` is one of the valid pipeline stages (1, 2, 3)."""
    if estagio not in VALID_STAGES:
        raise ValueError(
            f"Estágio inválido: {estagio!r}. Os estágios válidos são "
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
    human-readable ``mensagem`` / ``mensagem_erro`` (Req. 4.4, 4.5, 4.6, 6.x).

    The real :class:`OperationRunner` (task 7.3) may return a richer object; the
    assembly only reads these attributes, so any object exposing them (including
    a mock) satisfies the seam.
    """

    results: list[Stage3AbilityResult] = Field(default_factory=list)
    total_sucesso: int = 0
    total_falha: int = 0
    estado: OperationState = OperationState.NOT_STARTED
    operation_id: Optional[int] = None
    executed: bool = True
    mensagem: Optional[str] = None
    mensagem_erro: Optional[str] = None


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

    def run(self, case: CaseData, *, confirmado: bool) -> OperationRunResult:
        """Run the Operation for ``case`` iff ``confirmado`` is true.

        Must not execute any adversary command unless ``confirmado`` is true
        (Req. 6.6, 6.7). Returns an :class:`OperationRunResult`-shaped result.
        """
        ...


# ---------------------------------------------------------------------------
# Stage 2/3 assembly helpers (pure functions) — no I/O, no network
# ---------------------------------------------------------------------------


def _stage2_input_from_stage1(
    slug: str, nome: str, stage1: Stage1Result
) -> Stage2Input:
    """Build the Stage-2 Entrada from a Stage-1 result (Req. 3.1, 3.6).

    The abstract techniques from the Stage-1 Saída become the Stage-2 input.
    When Stage 1 produced no technique (empty extraction, error, or missing
    Saída), the input is flagged ``has_input=False`` with a note (Req. 3.6) and
    the Stage-2 Saída is suppressed by the caller.
    """
    techniques: list[Stage2InputTechnique] = []
    if stage1.saida is not None:
        techniques = [
            Stage2InputTechnique(
                technique_id=t.technique_id,
                technique_name=t.technique_name,
                tactic=t.tactic,
                description=t.description,
            )
            for t in stage1.saida.techniques
        ]

    if not techniques:
        return Stage2Input(
            caso_id=slug,
            nome=nome,
            techniques=[],
            has_input=False,
            note=(
                "O Estágio 1 não produziu técnicas ATT&CK para o caso "
                f"{nome}; não há Entrada para o Estágio 2."
            ),
        )
    return Stage2Input(caso_id=slug, nome=nome, techniques=techniques)


def _ability_view(ability: AbilityData) -> Stage2AbilityView:
    """Map a curated :class:`AbilityData` into the Stage-2 Saída view (Req. 3.2)."""
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
        origem_traducao=TranslationSource.HUMAN_CURATION,
    )


def _adversary_view(adversary: AdversaryData) -> Stage2AdversaryView:
    """Map a curated :class:`AdversaryData` into the Stage-2 Saída view (Req. 3.5)."""
    return Stage2AdversaryView(
        id=adversary.id,
        name=adversary.name,
        description=adversary.description,
        atomic_ordering=list(adversary.atomic_ordering),
        origem_traducao=TranslationSource.HUMAN_CURATION,
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp (portable across SQLite dev / PostgreSQL prod)."""
    return dt.datetime.utcnow()


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
    """

    def __init__(
        self,
        session: Session,
        case_service: Optional[CaseService] = None,
        operation_runner: Optional[OperationRunnerProtocol] = None,
    ) -> None:
        self._session = session
        self._case_service = case_service or CaseService()
        self._operation_runner = operation_runner

    # ---- public API -------------------------------------------------------

    def run_stage1(self, slug: str) -> Stage1Result:
        """Run Stage 1 (structural modeling) for the curated case ``slug``.

        Reads the case's structural DAG, derives the structural elements
        (techniques, relationships, indicators, infrastructure, malware,
        metadata), persists them associated with the case and marks the stage
        ``concluido`` (Req. 2.2, 2.3, 2.4). An empty extraction (no techniques)
        is a SUCCESS with a "no structural elements extracted" note (Req. 2.5).
        Any read/parse/modeling failure marks the stage ``erro`` with
        ``mensagem_erro`` and ``etapa_falha`` set and persists no partial
        extraction (Req. 2.6).

        Args:
            slug: One of the registered curated case slugs.

        Returns:
            A typed :class:`Stage1Result` (Entrada + Saída on success; error
            fields on failure). This method does not raise for a bad case — the
            failure is reported in the result — but a genuinely unknown slug
            (programming error) still raises ``KeyError`` from ``CaseService``.
        """
        nome = self._case_service.list_cases().get(slug, slug)
        entrada = Stage1Input(caso_id=slug, nome=nome)

        # --- Step 1: read the case (DAG + abilities + adversary) -----------
        step = Stage1Step.READ_CASE
        try:
            case = self._case_service.load_case(slug)
        except CaseFileError as exc:
            return self._fail(
                slug,
                entrada,
                step,
                f"Falha ao ler o caso {slug!r}: {exc.detail}",
            )
        except Exception as exc:  # defensive: any unexpected read error
            return self._fail(
                slug, entrada, step, f"Erro inesperado ao ler o caso {slug!r}: {exc}"
            )

        # A case with no DAG cannot be structurally modeled — that is a failure
        # (the structural input is missing), not an empty-but-valid extraction.
        if case.dag is None:
            return self._fail(
                slug,
                entrada,
                step,
                f"Caso {slug!r} não possui DAG estrutural para modelagem.",
            )

        # Enrich the Entrada now that the DAG is loaded (Req. 2.1).
        entrada = Stage1Input(
            caso_id=slug,
            nome=nome,
            dag_file=case.arquivo_dag,
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
                entrada,
                step,
                f"Erro na modelagem estrutural do caso {slug!r}: {exc}",
            )

        # --- Empty extraction => SUCCESS with a note (Req. 2.5) ------------
        if not techniques:
            saida = Stage1Output(
                is_empty=True,
                note=(
                    "Nenhum elemento estrutural foi extraído para o caso "
                    f"{nome}."
                ),
                metadata=metadata,
            )
        else:
            saida = Stage1Output(
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
            stage_run = self._persist(case, saida)
        except SQLAlchemyError as exc:
            # Rollback guarantees no partial extraction is persisted (Req. 2.6).
            self._session.rollback()
            return self._fail(
                slug,
                entrada,
                step,
                f"Falha ao persistir a extração do caso {slug!r}: {exc}",
            )

        return Stage1Result(
            caso_id=slug,
            estado=StageState.COMPLETED,
            entrada=entrada,
            saida=saida,
            stage_run_id=stage_run.id,
        )

    # ---- Stage 2: curated-translation assembly (task 8.2) -----------------

    def assemble_stage2(
        self, slug: str, stage1: Optional[Stage1Result] = None
    ) -> Stage2Result:
        """Assemble the Stage-2 Entrada/Saída for the curated case ``slug``.

        Stage 2 is the human-in-the-loop translation. For the curated cases the
        translation already exists on disk, so this method *assembles* it:

        * **Entrada** = the abstract ATT&CK techniques produced by Stage 1
          (Req. 3.1). ``stage1`` may be passed in to avoid re-running Stage 1;
          when omitted it is computed via :meth:`run_stage1`.
        * **Saída** = the curated Abilities — each with its technique id, tactic,
          description and the concrete command per executor (Req. 3.2) — the
          ordering/dependencies (``atomic_ordering``, Req. 3.3), the Adversary
          grouping (Req. 3.5) and the explicit, extensible translation source
          ``origem_traducao`` = ``curadoria_humana`` (Req. 11.1), with the
          "human curation" indicator attached to the Saída (Req. 3.4).

        Absence handling (represented cleanly, never as errors):

        * **No Stage-1 techniques** → the Entrada is flagged ``has_input=False``
          with a note and the Saída is *suppressed* (``saida is None``,
          ``suppressed=True``) (Req. 3.6).
        * **No curated Abilities** → the Saída is present but flagged
          ``has_abilities=False`` with a note (Req. 3.7).
        * **No Adversary** → the Saída is flagged ``has_adversary=False`` with a
          note indicating there is no Ability grouping (Req. 3.8).

        Args:
            slug: One of the registered curated case slugs.
            stage1: Optional precomputed Stage-1 result to source the Entrada
                techniques from. When ``None``, Stage 1 is run for the case.

        Returns:
            A typed :class:`Stage2Result`.
        """
        nome = self._case_service.list_cases().get(slug, slug)

        # Source the abstract techniques from Stage 1 (Req. 3.1). Reuse a passed
        # result when available so callers can avoid re-running Stage 1.
        if stage1 is None:
            stage1 = self.run_stage1(slug)

        entrada = _stage2_input_from_stage1(slug, nome, stage1)

        # No Stage-1 techniques → no Stage-2 input, Saída suppressed (Req. 3.6).
        if not entrada.has_input:
            return Stage2Result(
                caso_id=slug, entrada=entrada, saida=None, suppressed=True
            )

        # Load the curated Abilities/Adversary for the Saída. A read/parse
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

        # No curated Abilities → indicate absence of Stage-2 Saída (Req. 3.7).
        if not abilities:
            saida = Stage2Output(
                abilities=[],
                ordering=[],
                adversary=(_adversary_view(adversary) if adversary else None),
                has_abilities=False,
                has_adversary=adversary is not None,
                note=(
                    "O caso "
                    f"{nome} não possui Abilities curadas; não há Saída do "
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
            return Stage2Result(caso_id=slug, entrada=entrada, saida=saida)

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

        saida = Stage2Output(
            abilities=ability_views,
            ordering=ordering,
            adversary=adversary_view,
            origem_traducao=TranslationSource.HUMAN_CURATION,
            human_curation=True,
            has_abilities=True,
            has_adversary=adversary is not None,
            adversary_note=adversary_note,
        )
        return Stage2Result(caso_id=slug, entrada=entrada, saida=saida)

    # ---- Stage 3: emulation assembly + delegated execution (task 8.2) -----

    def assemble_stage3(self, slug: str) -> Stage3Input:
        """Assemble the Stage-3 Entrada = Adversary + curated Abilities (Req. 4.1).

        This is a pure, side-effect-free assembly so the API layer can call it
        independently of running the emulation (e.g. to render the Entrada or to
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
        nome = self._case_service.list_cases().get(slug, slug)

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
            caso_id=slug,
            nome=nome,
            adversary=adversary_input,
            abilities=ability_inputs,
            can_emulate=can_emulate,
            note=note,
        )

    def run_stage3(self, slug: str, *, confirmado: bool = False) -> Stage3Result:
        """Run Stage 3 by delegating execution to the injected OperationRunner.

        Assembles the Entrada (Adversary + Abilities, Req. 4.1) and, when an
        Adversary and Abilities are present and ``confirmado`` is true, delegates
        the emulation to the injected :class:`OperationRunnerProtocol` (task
        7.3) — this method never re-implements the Caldera calls, containment
        pre-flight, auditing or aggregation. It maps the runner's result into the
        Stage-3 Saída = per-Ability result + aggregate (Req. 4.4, 4.5).

        The runner is responsible for enforcing explicit confirmation and the
        containment/availability pre-flight (Req. 4.6, 6.x); this method simply
        forwards ``confirmado`` and surfaces the runner's ``mensagem`` /
        ``mensagem_erro`` when it refuses to execute. When there is nothing to
        emulate, or ``confirmado`` is false, no runner call is made and
        ``executed=False``.

        Args:
            slug: One of the registered curated case slugs.
            confirmado: The explicit confirmation flag (Req. 6.6, 6.7).

        Returns:
            A typed :class:`Stage3Result`.

        Raises:
            RuntimeError: if execution is requested (an emulatable, confirmed
                case) but no OperationRunner was injected.
        """
        entrada = self.assemble_stage3(slug)

        # Nothing to emulate → assembled Entrada, no execution (Req. 4.1).
        if not entrada.can_emulate:
            return Stage3Result(
                caso_id=slug,
                entrada=entrada,
                saida=None,
                executed=False,
                mensagem=entrada.note,
            )

        # No explicit confirmation → do not execute anything (Req. 6.6, 6.7).
        if not confirmado:
            return Stage3Result(
                caso_id=slug,
                entrada=entrada,
                saida=None,
                executed=False,
                mensagem=(
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
        run_result = self._operation_runner.run(case, confirmado=confirmado)

        # Runner refused to execute (containment violation, Caldera unavailable,
        # audit failure) → surface the message without a Saída (Req. 4.6, 6.x).
        if not run_result.executed:
            return Stage3Result(
                caso_id=slug,
                entrada=entrada,
                saida=None,
                executed=False,
                mensagem=run_result.mensagem,
                mensagem_erro=run_result.mensagem_erro,
            )

        saida = Stage3Output(
            results=list(run_result.results),
            aggregate=Stage3Aggregate(
                total_sucesso=run_result.total_sucesso,
                total_falha=run_result.total_falha,
                estado=run_result.estado,
            ),
            operation_id=run_result.operation_id,
        )
        return Stage3Result(
            caso_id=slug,
            entrada=entrada,
            saida=saida,
            executed=True,
            mensagem=run_result.mensagem,
        )

    # ---- sequential blocking + aggregate progress (task 8.3) --------------

    def get_stage_state(self, caso_id: str, estagio: int) -> StageStateView:
        """Return the current state of stage ``estagio`` for ``caso_id`` (Req. 5.3).

        Reads the stage's persisted :class:`~app.models.domain.StageRun`. When
        there is no row yet the stage is ``nao_iniciado`` with 0% progress
        (Req. 9.5). The ``bloqueado`` / ``iniciavel`` flags encode the sequential
        gate (Req. 5.4): a stage is startable iff it is Stage 1 or its preceding
        stage is ``concluido``. ``mensagem_erro`` / ``etapa_falha`` are surfaced
        when the stage is in ``erro`` (Req. 1.6, 2.6).

        Args:
            caso_id: The curated case slug.
            estagio: The stage number (1, 2 or 3).

        Returns:
            A typed :class:`StageStateView`.

        Raises:
            ValueError: if ``estagio`` is not one of 1, 2, 3.
        """
        _validate_stage(estagio)
        states = self._stage_states_map(caso_id)
        return self._build_stage_state_view(caso_id, estagio, states)

    def get_stage_states(self, caso_id: str) -> CaseStageStates:
        """Return the per-stage state of a case (Req. 5.3, 5.4).

        Assembles one :class:`StageStateView` per stage (1..3), each carrying its
        state, progress percentage and ``bloqueado`` flag, and flags the case as
        ``concluido_total`` when all three stages are ``concluido`` (the unit the
        aggregate progress counts — Req. 5.5).

        Reads all of the case's ``StageRun`` rows once, so the blocking decision
        for stage *n* uses the same snapshot as stage *n-1*'s state.

        Args:
            caso_id: The curated case slug.

        Returns:
            A typed :class:`CaseStageStates`.
        """
        states = self._stage_states_map(caso_id)
        views = [
            self._build_stage_state_view(caso_id, estagio, states)
            for estagio in VALID_STAGES
        ]
        concluido_total = all(
            v.estado is StageState.COMPLETED for v in views
        )
        return CaseStageStates(
            caso_id=caso_id, estagios=views, concluido_total=concluido_total
        )

    def is_stage_startable(self, caso_id: str, estagio: int) -> bool:
        """Whether stage ``estagio`` of ``caso_id`` can be started now (Req. 5.4).

        Stage 1 is always startable. Any later stage *n* is startable iff its
        immediately preceding stage *n-1* is ``concluido``; otherwise it is
        BLOCKED and must not be started (Req. 5.4 — Property 3 territory). This
        depends only on the preceding stage's persisted state, never on the
        stage's own state, so the gate is purely sequential.

        Args:
            caso_id: The curated case slug.
            estagio: The stage number (1, 2 or 3).

        Returns:
            ``True`` if the stage is startable, ``False`` if it is blocked.

        Raises:
            ValueError: if ``estagio`` is not one of 1, 2, 3.
        """
        _validate_stage(estagio)
        if estagio == FIRST_STAGE:
            return True
        states = self._stage_states_map(caso_id)
        previous_state = states.get(estagio - 1, StageState.NOT_STARTED)
        return previous_state is StageState.COMPLETED

    def aggregate_progress(
        self, caso_ids: Optional[list[str]] = None
    ) -> AggregateProgress:
        """Aggregate replication progress across the curated cases (Req. 5.5).

        Counts exactly the cases whose **all three** stages are ``concluido``,
        out of the case universe. By default the universe is the 8 curated cases
        (from :meth:`CaseService.case_slugs`); pass ``caso_ids`` to score a
        specific set (used by tests to drive distributions).

        The count reads ``StageRun`` rows only: a case with fewer than three
        ``concluido`` stages — including a case with no rows at all — does not
        count (Req. 5.5, 9.5). ``percentual`` is the integer percentage
        ``casos_concluidos / total_casos * 100`` for display (0 when the universe
        is empty).

        Args:
            caso_ids: Optional explicit case universe. Defaults to the 8 curated
                case slugs.

        Returns:
            A typed :class:`AggregateProgress`.
        """
        if caso_ids is None:
            caso_ids = self._case_service.case_slugs()

        completed_ids = [
            caso_id
            for caso_id in caso_ids
            if self._is_case_fully_completed(caso_id)
        ]
        total = len(caso_ids)
        done = len(completed_ids)
        percentual = round(done * 100 / total) if total else 0
        return AggregateProgress(
            casos_concluidos=done,
            total_casos=total,
            percentual=percentual,
            casos_concluidos_ids=completed_ids,
        )

    def stage_progress_event(
        self,
        caso_id: str,
        estagio: int,
        *,
        estado_anterior: Optional[StageState] = None,
    ) -> StageProgressEvent:
        """Build a stage state/percentage report for the real-time channels.

        Exposes the payload the WebSocket layer (task 11.3) pushes on
        ``WS /ws/estagios/{caso}``: the stage's current ``estado`` and
        ``progresso`` percentage (Req. 1.4, 1.5), the optional
        ``estado_anterior`` it transitioned from (Req. 1.4), and — when the
        stage is in ``erro`` — the ``mensagem_erro`` / ``etapa_falha`` cause
        (Req. 1.6). The WebSocket loop re-emits this at least every 5s while the
        stage is ``em_andamento`` so the UI shows the live percentage (Req. 1.5).

        This is a pure read over the persisted ``StageRun`` — reporting only,
        never mutating state.

        Args:
            caso_id: The curated case slug.
            estagio: The stage number (1, 2 or 3).
            estado_anterior: The state the stage transitioned from, when this
                event is emitted in response to a change (Req. 1.4).

        Returns:
            A typed :class:`StageProgressEvent`.

        Raises:
            ValueError: if ``estagio`` is not one of 1, 2, 3.
        """
        _validate_stage(estagio)
        view = self.get_stage_state(caso_id, estagio)
        return StageProgressEvent(
            caso_id=caso_id,
            estagio=estagio,
            estado=view.estado,
            progresso=view.progresso,
            estado_anterior=estado_anterior,
            mensagem_erro=view.mensagem_erro,
            etapa_falha=view.etapa_falha,
        )

    # ---- blocking/progress helpers (pure reads over StageRun) -------------

    def _stage_states_map(self, caso_id: str) -> dict[int, StageState]:
        """Map ``estagio -> StageState`` for a case, from its ``StageRun`` rows.

        Reads all of the case's stage runs in one query so a single snapshot
        drives every stage decision. Stages with no row are simply absent from
        the map; callers default them to ``nao_iniciado`` (Req. 9.5). If two rows
        somehow exist for the same stage, the highest-``id`` (latest) one wins.
        """
        stmt = (
            select(StageRun.estagio, StageRun.estado)
            .where(StageRun.caso_id == caso_id)
            .order_by(StageRun.id.asc())
        )
        states: dict[int, StageState] = {}
        for estagio, estado in self._session.execute(stmt).all():
            states[estagio] = estado
        return states

    def _stage_run_row(self, caso_id: str, estagio: int) -> Optional[StageRun]:
        """Return the latest ``StageRun`` row for ``(caso_id, estagio)``, if any.

        When more than one row exists for the same ``(caso_id, estagio)`` the
        highest-``id`` (latest) one wins, matching :meth:`_stage_states_map`.
        """
        stmt = (
            select(StageRun)
            .where(StageRun.caso_id == caso_id)
            .where(StageRun.estagio == estagio)
            .order_by(StageRun.id.asc())
        )
        rows = self._session.execute(stmt).scalars().all()
        return rows[-1] if rows else None

    def _build_stage_state_view(
        self, caso_id: str, estagio: int, states: dict[int, StageState]
    ) -> StageStateView:
        """Assemble a :class:`StageStateView` from a states snapshot + the row.

        ``states`` provides the state of every stage (for the blocking decision);
        the individual row is read for the persisted ``progresso`` /
        ``mensagem_erro`` / ``etapa_falha`` detail.
        """
        estado = states.get(estagio, StageState.NOT_STARTED)
        # Sequential gate (Req. 5.4): Stage 1 always startable; later stages
        # startable only when the previous stage is concluido.
        if estagio == FIRST_STAGE:
            iniciavel = True
        else:
            iniciavel = (
                states.get(estagio - 1, StageState.NOT_STARTED)
                is StageState.COMPLETED
            )

        progresso = 0
        mensagem_erro: Optional[str] = None
        etapa_falha: Optional[str] = None
        row = self._stage_run_row(caso_id, estagio)
        if row is not None:
            progresso = row.progresso
            mensagem_erro = row.mensagem_erro
            etapa_falha = row.etapa_falha

        return StageStateView(
            estagio=estagio,
            estado=estado,
            progresso=progresso,
            bloqueado=not iniciavel,
            iniciavel=iniciavel,
            mensagem_erro=mensagem_erro,
            etapa_falha=etapa_falha,
        )

    def _is_case_fully_completed(self, caso_id: str) -> bool:
        """Whether all three stages of ``caso_id`` are ``concluido`` (Req. 5.5)."""
        states = self._stage_states_map(caso_id)
        return all(
            states.get(estagio) is StageState.COMPLETED
            for estagio in VALID_STAGES
        )

    # ---- persistence ------------------------------------------------------

    def _persist(self, case: CaseData, saida: Stage1Output) -> StageRun:
        """Persist the case, its extracted elements and the completed StageRun.

        Runs as a single unit of work committed once at the end. On any DB error
        the caller rolls back, so either *all* extracted elements are persisted
        or *none* are (no partial extraction — Req. 2.6). The ``StageRun`` is
        upserted (one row per ``(caso_id, estagio=1)``) so re-running Stage 1 for
        a case updates the existing run rather than piling up rows.
        """
        # Upsert the Case row (metadata + file paths + translation source).
        db_case = self._session.get(CaseModel, case.id)
        if db_case is None:
            db_case = CaseModel(id=case.id)
            self._session.add(db_case)
        db_case.nome = case.nome
        db_case.arquivo_ability = case.arquivo_ability
        db_case.arquivo_adversary = case.arquivo_adversary
        db_case.arquivo_dag = case.arquivo_dag
        db_case.origem_traducao = case.origem_traducao

        # Persist the curated Abilities (upsert by ability_id), associated with
        # the case. Executors are stored as the JSON column.
        for ability in case.abilities:
            db_ability = self._session.get(AbilityModel, ability.ability_id)
            if db_ability is None:
                db_ability = AbilityModel(ability_id=ability.ability_id)
                self._session.add(db_ability)
            db_ability.caso_id = case.id
            db_ability.name = ability.name
            db_ability.tactic = ability.tactic
            db_ability.technique_name = ability.technique_name
            db_ability.technique_id = ability.technique_id
            db_ability.description = ability.description
            db_ability.executors = [e.model_dump() for e in ability.executors]
            db_ability.origem_traducao = TranslationSource.HUMAN_CURATION

        # Persist the Adversary when present (upsert by id).
        if case.adversary is not None:
            db_adv = self._session.get(AdversaryModel, case.adversary.id)
            if db_adv is None:
                db_adv = AdversaryModel(id=case.adversary.id)
                self._session.add(db_adv)
            db_adv.caso_id = case.id
            db_adv.name = case.adversary.name
            db_adv.description = case.adversary.description
            db_adv.atomic_ordering = list(case.adversary.atomic_ordering)
            db_adv.origem_traducao = TranslationSource.HUMAN_CURATION

        # Upsert the Stage-1 StageRun and mark it concluido (Req. 2.4).
        stage_run = self._get_or_create_stage_run(case.id, estagio=1)
        stage_run.estado = StageState.COMPLETED
        stage_run.progresso = 100
        stage_run.mensagem_erro = None
        stage_run.etapa_falha = None
        stage_run.atualizado_em = _utcnow()

        self._session.commit()
        self._session.refresh(stage_run)
        return stage_run

    def _get_or_create_stage_run(self, caso_id: str, *, estagio: int) -> StageRun:
        """Return the existing Stage-``estagio`` run for the case, or a new one."""
        stmt = (
            select(StageRun)
            .where(StageRun.caso_id == caso_id)
            .where(StageRun.estagio == estagio)
        )
        stage_run = self._session.execute(stmt).scalars().first()
        if stage_run is None:
            stage_run = StageRun(caso_id=caso_id, estagio=estagio)
            self._session.add(stage_run)
        return stage_run

    # ---- failure handling (Req. 2.6) -------------------------------------

    def _fail(
        self,
        slug: str,
        entrada: Stage1Input,
        step: Stage1Step,
        mensagem_erro: str,
    ) -> Stage1Result:
        """Record a Stage-1 failure without persisting any partial extraction.

        Marks the Stage-1 ``StageRun`` as ``erro`` with the failure cause
        (``mensagem_erro``) and the modeling step that failed (``etapa_falha``),
        and returns the error :class:`Stage1Result` (Req. 2.6). Persisting *only*
        the failed StageRun is intentional: it records the affected case and the
        failing step, and carries **no** extracted elements (Ability/Adversary
        rows are only written on the success path).

        If even the failure-marker write fails, the DB error is swallowed (the
        session is rolled back) so callers always get a descriptive result
        rather than an exception — the in-memory result still identifies the
        case, cause and step.
        """
        try:
            stage_run = self._get_or_create_stage_run(slug, estagio=1)
            stage_run.estado = StageState.ERROR
            stage_run.mensagem_erro = mensagem_erro
            stage_run.etapa_falha = step.value
            stage_run.atualizado_em = _utcnow()
            self._session.commit()
            stage_run_id = stage_run.id
        except SQLAlchemyError:
            self._session.rollback()
            stage_run_id = None

        return Stage1Result(
            caso_id=slug,
            estado=StageState.ERROR,
            entrada=entrada,
            saida=None,
            mensagem_erro=mensagem_erro,
            etapa_falha=step.value,
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
]
