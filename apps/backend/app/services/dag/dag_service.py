"""DagService — DAG loading, acyclic Mermaid and curated/stub distinction.

This service powers the explicit DAG Generation/Visualization phase of the new
flow (``GET /api/campaigns/{campaign}/dag`` — Req. 5, 6). For a **curated** case it
reads the already-produced structural graph in ``data/dag/{slug}_dag.json`` and
assembles a (acyclic-by-construction) Mermaid ``flowchart`` the frontend hands
straight to the existing ``MermaidDiagram`` (``securityLevel: "strict"``). For a
**non-curated** slug it returns a "pass-through" availability stub — **without
ever invoking any AI service** (there is no AI client on this path — Req. 9.1,
9.2). A curated case whose DAG is missing/malformed yields a typed error state
(``curated=True, can_advance=False`` — Req. 5.3, 6.4).

Design alignment
----------------
* Path resolution and typed parsing are **reused** from
  :class:`~app.services.case_service.CaseService`: ``dag_path(slug)`` locates
  ``data/dag/{slug}_dag.json`` and the service's typed parsing produces
  :class:`~app.services.case_service.DagData` / :class:`~app.services.case_service.DagNode`
  (the same ``{slug}_dag.json`` the StageService already loads). The DAG service
  never re-locates files or re-implements JSON parsing.
* The curated slugs are taken from :data:`~app.services.case_service.CURATED_CASES`
  (via ``CaseService``) — the 8 slugs are **not** hardcoded here.
* Mermaid assembly mirrors the style of ``app/api/topology.py::_mermaid``: one
  node per structural node and one edge per edge entry, ``flowchart TD``, no
  embedded scripts. Implemented as a pure function in task 7.2.

DAG edges
---------
The design contract models edges as a list of ``{source, target}`` pairs keyed by
``node_id`` (see the ``GET /api/campaigns/{campaign}/dag`` JSON, field ``edges``).
However, the real ``{slug}_dag.json`` files express
their relations via ``parent_nodes`` / ``child_nodes`` on each
``structural_node`` rather than a top-level ``edges`` array. The actual edge
derivation — turning those ``parent_nodes``/``child_nodes`` relations into the
``{from, to}`` pairs **without introducing extra edges** (preserving the
acyclicity guaranteed by ``metadata.validation.is_dag``) — is the subject of
task 7.2 (``build_mermaid``) and task 7.3 (``get_dag``). The ``{source, target}``
pairs preserve the acyclicity guaranteed by ``metadata.validation.is_dag``.

The pure read path only ever **reads** from ``sticks/`` (through
``CaseService``) and performs **no network access**. The persistence path
(``sync_curated_dags``/``list_dags``/``choose_dag``) uses the
injected :class:`~sqlalchemy.orm.Session`.

DAG choice — Stage 2 completion (task 5.2)
------------------------------------------
:meth:`DagService.choose_dag` writes ``chosen_dag_id`` on the Stage 2 (DAG
Generation) ``StageRun`` of the campaign and completes it (progress 100),
unblocking Stage 3 (Req. 6.3), validating that ``dag_id`` belongs to the campaign
(typed refusal ``DAG_NOT_FOUND`` → 404) and that the campaign has at least one
persisted DAG (typed refusal ``NO_DAG`` → 409, Req. 6.2). It is idempotent
(absolute assignment via upsert of the ``stage=2`` ``StageRun``) and transactional
(rollback preserves the previous state on a persistence failure, like
``SessionStateService``).

Automatic Stage 2 completion (task 5.1)
---------------------------------------
In the five-stage scheme the DAG (Stage 2) is **derived** from the Structural
Modeling (Stage 1): the ``DagGenerator.generate(campaign, modeling)`` seam now
receives the Modeling (Req. 3.1, 10.1). For a curated campaign,
:meth:`DagService.auto_complete_stage2` — mirroring
:meth:`~app.services.environment.stage_service.EnvironmentStageService.auto_complete_stage4`
— respects the sequential gate (Stage 1 must be ``completed`` — Req. 9.3), is
idempotent, runs the curated backfill (``sync_curated_dags``, which now passes the
Modeling to the generator) and, when at least one derivable DAG is persisted,
marks the ``stage=2`` ``StageRun`` ``completed``. With no derivable DAG, Stage 2
stays not completed ("Generate DAG" button disabled, advance blocked — Req. 9.4).

_Requirements: 3.2, 3.3, 3.4, 3.5, 4.7, 5.1, 5.2, 6.2, 6.3, 8.3, 9.1, 9.3, 9.4_
"""

from __future__ import annotations

import datetime as dt
import enum
import re
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.domain import (
    Case as CaseModel,
    Dag as DagModel,
    StageRun,
)
from app.models.enums import DagOrigin, StageState
from app.services.campaign_service import (
    CaseLoadErrorType,
    CaseService,
    DagData,
)
from app.services.stage.stage_service import (
    STAGE_DAG_GENERATION,
    STAGE_MODELING,
    STAGE_CAMPAIGN_SELECTION,
    StageService,
)

if TYPE_CHECKING:  # pragma: no cover - import only for typing (avoids a cycle)
    from app.services.dag.generator import DagGenerator, StructuralModeling


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp (portable across PostgreSQL and the test SQLite).

    Matches the rest of the backend (``StageService``/``SessionStateService``)
    so ``created_at`` round-trips identically on both the product PostgreSQL and
    the ephemeral test SQLite.
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


# Characters that are safe inside a Mermaid node id. Anything else in a
# ``node_id`` is replaced with ``_`` before the ``n_`` prefix is applied, so the
# resulting id is always a valid Mermaid identifier (deterministic, no scripts).
_UNSAFE_NODE_ID_CHARS = re.compile(r"[^A-Za-z0-9_]")


# ---------------------------------------------------------------------------
# Typed response models (English field names per the design contract for
# GET /api/campaigns/{campaign}/dag)
# ---------------------------------------------------------------------------


class DagNodeView(BaseModel):
    """One node of the DAG as shown in the ``GET /api/campaigns/{campaign}/dag`` payload.

    Mirrors the ``nodes`` entries of the design JSON: the structural node's
    ``node_id`` plus its ATT&CK technique identity (``technique_id``,
    ``technique_name``, ``tactic``).
    """

    node_id: str
    technique_id: Optional[str] = None
    technique_name: Optional[str] = None
    tactic: Optional[str] = None


class DagEdgeView(BaseModel):
    """One directed edge of the DAG (``edges`` entry of the design JSON).

    The field names are ``source``/``target`` from the contract
    (``"edges": [{ "source": "…", "target": "…" }]``): ``source`` is the source
    node_id, ``target`` the target node_id.
    """

    source: str
    target: str


class DagView(BaseModel):
    """The DAG view returned by ``GET /api/campaigns/{campaign}/dag`` (Req. 5, 6).

    Covers the three states of the contract:

    * **Curated** valid case — ``curated=True``, ``can_advance=True``, with
      ``nodes``/``edges`` filled and ``mermaid`` assembled.
    * **Non-curated** case — "pass-through": ``curated=False``,
      ``can_advance=False``, ``unavailable=True`` and a ``message`` (no AI is
      ever invoked — Req. 5.4, 5.5, 9.1, 9.2).
    * **Curated but missing/malformed** DAG — typed error: ``curated=True``,
      ``can_advance=False`` with an ``error`` message (Req. 5.3, 6.4).
    """

    campaign_id: str
    name: str
    curated: bool
    can_advance: bool
    nodes: list[DagNodeView] = Field(default_factory=list)
    edges: list[DagEdgeView] = Field(default_factory=list)
    mermaid: str = ""
    # Stub / error fields (populated only in the non-curated or error states).
    unavailable: bool = False
    message: Optional[str] = None
    error: Optional[str] = None


class DagSummary(BaseModel):
    """Summary of a persisted DAG, for a Campaign's listing (Req. 4.8).

    Mirrors the identity columns of the :class:`~app.models.domain.Dag` entity
    exposed by the ``GET /api/campaigns/{campaign}/dags`` contract — ``id``,
    ``origin``, ``identifier``/version, ``label`` and ``created_at`` — without
    loading the graph ``content`` (obtained via ``get_persisted_dag``). ``origin``
    is the persisted :class:`~app.models.enums.DagOrigin` (``curadoria_humana`` or,
    in the future, ``gerado_ia``).
    """

    id: int
    campaign_id: str
    origin: DagOrigin
    identifier: str
    label: Optional[str] = None
    created_at: dt.datetime


class DagDetail(BaseModel):
    """Graph + Mermaid of a persisted DAG (``GET /api/campaigns/{campaign}/dags/{dag_id}``).

    Mirrors the graph shape of :class:`DagView` (``nodes``/``edges``/``mermaid``,
    handed straight to ``MermaidDiagram`` with ``securityLevel: "strict"``) and
    adds the persisted DAG identity (``dag_id``, ``origin``, ``identifier``,
    ``label``), exactly as the design contract ("Get the graph + Mermaid of a
    DAG" — Req. 4.4) and the frontend wrapper ``getDagById`` (type ``DagDetail``)
    expect. The graph ``content`` is rebuilt from the persisted ``Dag.content``
    (``structural_nodes`` + ``edges``), and ``mermaid`` is assembled consistently
    with ``get_dag`` (same ``build_mermaid``/``_derive_edges``).
    """

    campaign_id: str
    dag_id: int
    origin: DagOrigin
    identifier: str
    label: Optional[str] = None
    nodes: list[DagNodeView] = Field(default_factory=list)
    edges: list[DagEdgeView] = Field(default_factory=list)
    mermaid: str = ""


# ---------------------------------------------------------------------------
# DAG choice for execution (completes Stage 1) — Req. 4.7, 6.2, 6.3
# ---------------------------------------------------------------------------

# The "DAG Generation" Stage number is the single source of truth in
# ``stage_service`` (``STAGE_DAG_GENERATION == 2`` after the renumbering to five
# Stages — Req. 1.1); imported above rather than redeclared as a local literal.
# The choice writes ``chosen_dag_id`` on the ``StageRun`` of this Stage and
# completes it; the automatic completion (``auto_complete_stage2``) marks the
# same ``StageRun`` ``completed`` when there is a derivable curated DAG,
# unblocking Stage 3 through the sequential gate.


class DagChoiceFailure(str, enum.Enum):
    """Typed reason for a refused DAG choice (Req. 6.2, 6.3).

    The API translates each reason into an HTTP code (see ``choose_dag``):

    * ``NO_DAG`` → 409 Conflict: the campaign has no persisted DAG, so advancing
      beyond Stage 2 stays blocked (Req. 6.2).
    * ``DAG_NOT_FOUND`` → 404 Not Found: the ``dag_id`` does not exist or does not
      belong to the campaign (Req. 6.3 — error); the Stage 2 ``StageRun`` is
      **not** altered.
    * ``PERSISTENCE_FAILURE`` → failure persisting the choice; the previously
      persisted state is preserved unchanged (same transactional pattern as
      ``SessionStateService.persist_stage_completion``).
    """

    NO_DAG = "no_dag"
    DAG_NOT_FOUND = "dag_nao_encontrado"
    PERSISTENCE_FAILURE = "falha_persistencia"


# pt-BR messages per refusal reason, for the API/UI to surface (Req. 6.2, 6.3).
_MESSAGE_NO_DAG = (
    "A Campanha não possui nenhum DAG persistido; o avanço para além do "
    "Estágio 2 (Geração do DAG) permanece bloqueado."
)
_MESSAGE_DAG_NOT_FOUND = (
    "O DAG escolhido não existe ou não pertence a esta Campanha."
)
_MESSAGE_PERSISTENCE_FAILURE = (
    "A escolha do DAG não foi salva: falha ao persistir na base de dados. "
    "O Estágio 2 foi preservado sem alteração."
)


class DagChoiceResult(BaseModel):
    """Typed result of :meth:`DagService.choose_dag` (Req. 4.7, 6.2, 6.3).

    Instead of raising exceptions, ``choose_dag`` returns this typed result
    (same contract as ``SessionStateService.persist_stage_completion`` /
    ``Stage1Result``): ``success`` indicates whether the choice was persisted
    and, on a refusal/failure, ``failure`` carries the reason and ``message`` the
    pt-BR text.

    Attributes
    ----------
    success:
        ``True`` when ``chosen_dag_id`` was written to the Stage 2 ``StageRun`` of
        the campaign and that ``StageRun`` was marked ``completed`` (progress
        100), unblocking Stage 3 (Req. 6.3). ``False`` on any refusal/failure, in
        which case the Stage 2 ``StageRun`` is left unchanged.
    campaign_id:
        The campaign the choice was requested for.
    dag_id:
        The requested ``dag_id``.
    failure:
        ``None`` on success; the corresponding :class:`DagChoiceFailure` on a
        refusal/failure. The API maps it to 409 (``NO_DAG``) or 404
        (``DAG_NOT_FOUND``).
    message:
        ``None`` on success; the pt-BR reason message on a refusal/failure, for
        the API/UI to surface.
    stage:
        The Stage number completed by the choice (always
        :data:`STAGE_DAG_GENERATION`), also present on a refusal for context.
    stage_run_id:
        The id of the written Stage 2 ``StageRun`` on success; ``None`` when the
        choice was refused/failed.
    """

    success: bool
    campaign_id: str
    dag_id: int
    failure: Optional[DagChoiceFailure] = None
    message: Optional[str] = None
    stage: int = STAGE_DAG_GENERATION
    stage_run_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Automatic Stage 2 completion (curated DAG) — Req. 3.2–3.5, 9.1, 9.3, 9.4
# ---------------------------------------------------------------------------

# pt-BR messages per non-completion path of the Stage 2 auto-completion
# (for the API/UI to surface), mirroring ``EnvironmentStageService`` (Stage 4).
_MSG_STAGE3_GATE = (
    "O Estágio 2 (Seleção da campanha) deste Caso ainda não está concluído; o "
    "Estágio 3 (Geração do DAG) permanece bloqueado."
)
_MSG_STAGE3_NO_DAG = (
    "Este Caso não possui DAG derivável; o Estágio 3 (Geração do DAG) não é "
    "concluído automaticamente (o botão \"Gerar DAG\" permanece desabilitado e o "
    "avanço bloqueado)."
)
_MSG_STAGE3_PERSISTENCE_FAILURE = (
    "A conclusão automática do Estágio 3 não foi salva: falha ao persistir na "
    "base de dados. O estado anterior foi preservado sem alteração."
)


class DagAutoGenerationResult(BaseModel):
    """Typed result of :meth:`DagService.auto_complete_stage2`.

    Mirrors :class:`~app.services.environment.stage_service.ProvisionamentoAutoResult`
    (Stage 4 auto-completion): instead of raising exceptions, the auto-completion
    returns this typed result — ``completed`` indicates whether Stage 2 became
    ``completed`` in this call (or already was, idempotently); the remaining
    fields explain **why** it did not complete, so the API/screen layer can
    surface the reason and keep the advance blocked.

    Attributes
    ----------
    campaign_id:
        The campaign whose Stage 2 auto-completion was attempted.
    completed:
        ``True`` when, at the end of the call, the campaign's Stage 2 ``StageRun``
        is ``completed`` (progress 100) — by this call or already before
        (idempotency). ``False`` on any path that does **not** complete (gate not
        released, no derivable DAG, or persistence failure).
    blocked_by_gate:
        ``True`` when the campaign's Stage 1 (Modeling) is **not** ``completed``;
        Stage 2 stays blocked and nothing is persisted (Req. 9.3).
    no_dag:
        ``True`` when there is no derivable DAG for the campaign (the curated
        backfill persisted no DAG); the auto-completion does not occur and Stage 2
        stays not completed (Req. 9.4).
    total_dags:
        Number of persisted DAGs for the campaign after the curated backfill.
    persistence_failed:
        ``True`` when there was a derivable DAG but the ``commit`` failed; the
        previous state was preserved unchanged (rollback).
    message:
        pt-BR text explaining the result (reason for non-completion), for the
        API/UI to surface; ``None`` when it completed without caveats.
    stage_run_id:
        The id of the Stage 2 ``StageRun`` when completed; ``None`` otherwise.
    """

    campaign_id: str
    completed: bool = False
    blocked_by_gate: bool = False
    no_dag: bool = False
    total_dags: int = 0
    persistence_failed: bool = False
    message: Optional[str] = None
    stage_run_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DagService:
    """Loads/persists DAGs, builds Mermaid, and distinguishes curated/stub.

    Reuses :class:`~app.services.case_service.CaseService` for path resolution
    (``dag_path``) and typed parsing (``DagData``/``DagNode``), and knows which
    slugs are curated via ``CaseService.CURATED_CASES`` — no slug is hardcoded
    here. A custom ``case_service`` may be injected (e.g. in tests / fixtures) to
    point at alternative local fixtures.

    Persistence / generation (task 5.1 — Req. 4.1–4.5, 4.8, 5.1)
    ------------------------------------------------------------
    The service also persists and lists first-class :class:`~app.models.domain.Dag`
    entities for a campaign, consuming a :class:`~app.services.dag.generator.DagGenerator`
    (``CurationDagGenerator`` by default — Req. 5.1). It takes an injected
    SQLAlchemy :class:`~sqlalchemy.orm.Session` (same convention as
    ``StageService``/``SessionStateService``), so the same code works with the
    app's ``SessionLocal`` and an ephemeral test SQLite session. The ``session``
    is **optional**: the pure read path (``get_dag``/``build_mermaid``) does
    not need a database, so the legacy ``DagService(case_service=...)`` call used
    by ``GET /api/campaigns/{campaign}/dag`` keeps working unchanged. The persistence
    methods (:meth:`sync_curated_dags`, :meth:`list_dags`) require a
    session and raise a clear error if none was injected.
    """

    def __init__(
        self,
        session: Optional[Session] = None,
        case_service: Optional[CaseService] = None,
        generator: Optional["DagGenerator"] = None,
        stage_service: Optional[StageService] = None,
    ) -> None:
        self._session = session
        self._case_service: CaseService = (
            case_service if case_service is not None else CaseService()
        )
        # StageService over the SAME Session, to obtain the Stage 1 Modeling
        # (``read_stage1``/``run_stage1``) and read the Stages' state in the
        # auto-completion gate. Built lazily (only when there is a Session),
        # reusing ``case_service`` to point at the same fixtures.
        self._stage_service: Optional[StageService] = stage_service
        if generator is None:
            # Lazy import: ``generator`` imports ``DagService`` from this module,
            # so importing it at module top-level would create a cycle. The
            # default generator is the curation implementation (Req. 5.1).
            from app.services.dag.generator import CurationDagGenerator

            generator = CurationDagGenerator(self._case_service)
        self._gerador: "DagGenerator" = generator

    @property
    def case_service(self) -> CaseService:
        """The underlying :class:`CaseService` used for paths and parsing."""
        return self._case_service

    @property
    def generator(self) -> "DagGenerator":
        """The injected :class:`DagGenerator` (curation by default — Req. 5.1)."""
        return self._gerador

    def _require_session(self) -> Session:
        """Return the injected :class:`Session` or raise if persistence is unused.

        The pure read path (``get_dag``/``build_mermaid``) never needs a
        database, so ``session`` is optional. The persistence methods require
        one; calling them without an injected session is a programming error.
        """
        if self._session is None:
            raise RuntimeError(
                "DagService requer uma Session injetada para persistir/listar "
                "DAGs; nenhuma foi fornecida."
            )
        return self._session

    def _get_stage_service(self) -> StageService:
        """Return the :class:`StageService` over the same Session (lazy).

        The ``StageService`` is used to obtain the Stage 1 Modeling (the input of
        DAG generation — Req. 3.1, 10.1) and to read the Stages' state in the
        auto-completion gate. Built lazily over the injected Session and the same
        ``case_service``, so it operates on the same fixtures/database as the rest
        of the service.
        """
        if self._stage_service is None:
            self._stage_service = StageService(
                session=self._require_session(),
                case_service=self._case_service,
            )
        return self._stage_service

    def _get_modeling(self, campaign: str) -> "StructuralModeling":
        """Obtain the campaign's Structural Modeling (Stage 1) for DAG generation.

        In the five-stage scheme the DAG (Stage 2) is derived from the Modeling
        (Stage 1 — Req. 3.1, 10.1), so the ``DagGenerator.generate`` seam receives
        this Modeling. The Stage 1 output (techniques + relationships) is read via
        :meth:`StageService.read_stage1` when Stage 1 is already ``completed``
        (side-effect-free path) and, otherwise, materialized by
        :meth:`StageService.run_stage1` (the Modeling must exist for the DAG to be
        derived from it). The ``techniques`` / ``relationships`` elements of
        ``Stage1Output`` are mapped 1:1 to
        :class:`~app.services.dag.generator.StructuralModeling` (the same
        ``TechniqueElement`` / ``RelationshipElement``), so the Modeling → DAG
        data flow is correct even if the curation implementation ignores the
        Modeling (the curated graph already exists on disk).

        A Stage 1 that produced no Output (empty/read error) results in an empty
        Modeling — faithful to the absence of structural material; the curation
        generation still produces the curated DAG from disk (or an empty list
        with no derivable DAG).
        """
        # Local import to avoid the cycle (``generator`` imports ``DagService``).
        from app.services.dag.generator import StructuralModeling

        stage_service = self._get_stage_service()
        states = stage_service._stage_states_map(campaign)
        if states.get(STAGE_MODELING) is StageState.COMPLETED:
            # Stage 1 already completed: pure read, no re-run/persist.
            result = stage_service.read_stage1(campaign)
        else:
            # Materialize the Modeling (run + persist Stage 1), so the Stage 2
            # DAG is actually derived from it.
            result = stage_service.run_stage1(campaign)

        output = result.output
        if output is None:
            return StructuralModeling()
        return StructuralModeling(
            techniques=list(output.techniques),
            relationships=list(output.relationships),
        )

    # ---- persistence / generation (task 5.1) -----------------------------

    def sync_curated_dags(self, campaign: str) -> list[DagSummary]:
        """Idempotent backfill of a campaign's curated DAGs (Req. 4.5).

        Calls ``generator.generate(campaign, modeling)`` (``CurationDagGenerator`` by
        default, which reads ``data/dag/{slug}_dag.json`` via ``CaseService`` —
        **no network and no AI**, Req. 5.5, 9.1) and persists each
        :class:`ProducedDag` as a :class:`~app.models.domain.Dag` entity
        associated with the campaign, **if an equivalent one does not already
        exist**.

        Idempotency (Req. 4.5): reconciliation is keyed on
        ``(campaign_id, origin, identifier)``. Since ``CurationDagGenerator`` uses
        ``identifier = slug`` and ``origin = curadoria_humana``, this amounts to
        "one curated DAG per campaign": running this step once or many times
        produces exactly the same persisted set, with no duplication. A campaign
        whose curated DAG is absent/malformed makes the generator return an empty
        list — nothing is persisted (no invalid ``Dag``), and the other campaigns
        are not affected (same per-campaign isolation as ``GET /api/campaigns``).

        The FK ``dags.campaign_id → campaigns.id`` requires the ``Case`` row: it is
        upserted defensively (same pattern as ``StageService._persist``) before
        inserting the DAG, so the backfill works even before Stage 1 has persisted
        the campaign. Everything in a single transaction (commit at the end;
        rollback preserves the previous state).

        In the five-stage scheme the DAG (Stage 2) is derived from the Structural
        Modeling (Stage 1): the ``DagGenerator.generate(campaign, modeling)`` seam
        now **receives** the Modeling (Req. 3.1, 10.1). This backfill obtains the
        campaign's Stage 1 Modeling (via :meth:`_get_modeling`) and passes it to
        the generator. The curation implementation **ignores** it (the curated
        graph already exists on disk), but the Modeling is built faithfully from
        the real Stage 1 output so the data flow is correct for the future AI
        implementation.

        Returns the resulting listing of the campaign's DAGs (:meth:`list_dags`).
        """
        session = self._require_session()

        modeling = self._get_modeling(campaign)
        produced_list = self._gerador.generate(campaign, modeling)
        for produced in produced_list:
            self._upsert_case_row(campaign, produced.label)
            # Idempotent reconciliation by (campaign_id, origin, identifier).
            exists = (
                session.execute(
                    select(DagModel.id)
                    .where(DagModel.campaign_id == campaign)
                    .where(DagModel.origin == produced.origin)
                    .where(DagModel.identifier == produced.identifier)
                )
                .scalars()
                .first()
            )
            if exists is not None:
                # Already persisted: do not duplicate (Req. 4.5).
                continue
            session.add(
                DagModel(
                    campaign_id=campaign,
                    origin=produced.origin,
                    identifier=produced.identifier,
                    label=produced.label,
                    content=produced.content,
                    created_at=_utcnow(),
                )
            )

        session.commit()
        return self.list_dags(campaign)

    def list_dags(self, campaign: str) -> list[DagSummary]:
        """Persisted DAGs of a campaign (Req. 4.8).

        Returns one :class:`DagSummary` per campaign ``Dag`` row — ``id``,
        ``origin``, ``identifier``/version, ``label`` and ``created_at`` — without
        the graph ``content`` (obtained on demand via ``get_persisted_dag``).
        Ordered by ``id`` ascending (creation order) for a stable listing. A
        campaign with no persisted DAG returns an empty list.
        """
        session = self._require_session()
        stmt = (
            select(DagModel)
            .where(DagModel.campaign_id == campaign)
            .order_by(DagModel.id.asc())
        )
        rows = session.execute(stmt).scalars().all()
        return [
            DagSummary(
                id=row.id,
                campaign_id=row.campaign_id,
                origin=row.origin,
                identifier=row.identifier,
                label=row.label,
                created_at=row.created_at,
            )
            for row in rows
        ]

    def get_persisted_dag(self, campaign: str, dag_id: int) -> Optional[DagDetail]:
        """Graph + Mermaid of a persisted DAG by its ``dag_id`` (Req. 4.4).

        Rebuilds the graph from the persisted ``Dag.content``
        (``structural_nodes`` + ``edges``), assembling an equivalent
        :class:`DagData` so it can reuse :meth:`build_mermaid` /
        :meth:`_derive_edges` — exactly as :meth:`get_dag` does for the curated
        DAG on disk. This keeps the payload ``edges`` and the edges drawn in
        ``mermaid`` always consistent.

        Validation (Req. 4.4 — 404): the ``dag_id`` must exist **and** belong to
        the campaign (``Dag.campaign_id == campaign``). A non-existent ``dag_id`` or
        one from another campaign returns ``None`` — the API maps it to 404 Not
        Found. On success it returns a typed :class:`DagDetail`.

        Pure read over the database: no network access and no AI invocation.
        """
        session = self._require_session()

        dag = session.get(DagModel, dag_id)
        if dag is None or dag.campaign_id != campaign:
            # Non-existent or not belonging to the campaign → 404 in the API (Req. 4.4).
            return None

        # Rebuild a DagData from the persisted content to reuse the same
        # node/edge/Mermaid assembly as ``get_dag``. The content stores
        # ``structural_nodes`` (with node_id/technique_*/relations) and
        # ``edges``; the nodes preserve ``child_nodes``, so ``_derive_edges``
        # reproduces exactly the persisted edges.
        content = dag.content if isinstance(dag.content, dict) else {}
        nodes_raw = content.get("structural_nodes") or []
        dag_data = DagData.model_validate({"structural_nodes": nodes_raw})

        nodes = [
            DagNodeView(
                node_id=node.node_id,
                technique_id=node.technique_id,
                technique_name=node.technique_name,
                tactic=node.tactic,
            )
            for node in dag_data.structural_nodes
            if node.node_id is not None
        ]
        edges = [
            DagEdgeView(source=src, target=dst)
            for src, dst in DagService._derive_edges(dag_data)
        ]
        mermaid = DagService.build_mermaid(dag_data)

        return DagDetail(
            campaign_id=dag.campaign_id,
            dag_id=dag.id,
            origin=dag.origin,
            identifier=dag.identifier,
            label=dag.label,
            nodes=nodes,
            edges=edges,
            mermaid=mermaid,
        )

    def choose_dag(self, campaign: str, dag_id: int) -> DagChoiceResult:
        """Choose a DAG for execution, completing Stage 2 (Req. 6.3).

        Writes ``chosen_dag_id = dag_id`` on the campaign's Stage 2 ``StageRun``
        (``stage=2`` — DAG Generation) and marks that ``StageRun`` as
        ``completed`` (progress 100), unblocking the advance to Stage 3 through
        the sequential gate (Req. 6.3). The Stage 2 ``StageRun`` is upserted by
        the ``(campaign_id, stage=2)`` pair — same contract as ``StageService`` /
        ``SessionStateService._get_or_create_stage_run`` — so a run without a
        Stage 2 row yet gets one.

        Validation (Req. 6.2, 6.3) — typed result, no exceptions
        ------------------------------------------------------------
        * **No persisted DAG** for the campaign (Req. 6.2): returns
          ``success=False`` with ``failure=NO_DAG`` (the API maps it to 409
          Conflict). The Stage 2 ``StageRun`` is **not** altered.
        * **Non-existent ``dag_id`` or one not belonging to the campaign** (Req.
          6.3 — error): returns ``success=False`` with ``failure=DAG_NOT_FOUND``
          (the API maps it to 404 Not Found). Validation requires the ``Dag`` to
          exist **and** ``Dag.campaign_id == campaign``; a ``dag_id`` from another
          campaign is treated as not found. The Stage 2 ``StageRun`` is **not**
          altered.

        Idempotency / consistency
        --------------------------
        Since the write is an upsert of the ``stage=2`` ``StageRun`` and the
        choice is an absolute assignment (``chosen_dag_id = dag_id`` +
        ``completed``/100), choosing the **same** ``dag_id`` again produces
        exactly the same persisted state (no new row, no divergent field);
        re-choosing a **different** (valid) ``dag_id`` of the campaign overwrites
        the previous choice deterministically.

        Atomicity / previous-state preservation
        ----------------------------------------
        The write runs in a single transaction (``commit``). If the ``commit``
        fails, the session is rolled back (``rollback``), discarding the pending
        ``StageRun`` mutation and leaving the previously persisted state
        **exactly** as it was — same pattern as
        ``SessionStateService.persist_stage_completion``. In that case it returns
        ``success=False`` with ``failure=PERSISTENCE_FAILURE`` rather than raising.

        Parameters
        ----------
        campaign:
            The slug of the campaign whose execution will have the DAG chosen.
        dag_id:
            The id of the persisted ``Dag`` to choose. Must exist and belong to
            the campaign.

        Returns
        -------
        DagChoiceResult
            ``success=True`` with the Stage 2 ``stage_run_id`` on success;
            ``success=False`` with the typed reason in ``failure`` on a refusal
            (``NO_DAG`` / ``DAG_NOT_FOUND``) or persistence failure
            (``PERSISTENCE_FAILURE``).
        """
        session = self._require_session()

        # Req. 6.2: the campaign must have at least one persisted DAG; otherwise
        # the advance stays blocked (409 in the API). Checking before validating
        # the specific dag_id distinguishes "no DAG" from "DAG not belonging".
        has_any_dag = (
            session.execute(
                select(DagModel.id).where(DagModel.campaign_id == campaign).limit(1)
            )
            .scalars()
            .first()
        )
        if has_any_dag is None:
            return DagChoiceResult(
                success=False,
                campaign_id=campaign,
                dag_id=dag_id,
                failure=DagChoiceFailure.NO_DAG,
                message=_MESSAGE_NO_DAG,
            )

        # Req. 6.3: the dag_id must exist AND belong to the campaign. A dag_id
        # from another campaign (or non-existent) is "not found" (404 in the
        # API). The Stage 2 StageRun is not touched on this path.
        dag = session.get(DagModel, dag_id)
        if dag is None or dag.campaign_id != campaign:
            return DagChoiceResult(
                success=False,
                campaign_id=campaign,
                dag_id=dag_id,
                failure=DagChoiceFailure.DAG_NOT_FOUND,
                message=_MESSAGE_DAG_NOT_FOUND,
            )

        # Write the choice on the Stage 2 StageRun (upsert by (campaign_id, 2))
        # and complete it (progress 100), unblocking Stage 3 (Req. 6.3). Absolute
        # assignment ⇒ idempotent when re-choosing the same dag_id.
        stage_run = self._get_or_create_stage_run_dag_generation(campaign)
        stage_run.chosen_dag_id = dag_id
        stage_run.state = StageState.COMPLETED
        stage_run.progress = 100
        # Completed: clear any previous Stage 2 error markers.
        stage_run.error_message = None
        stage_run.failed_step = None
        stage_run.updated_at = _utcnow()

        try:
            session.commit()
        except SQLAlchemyError:
            # Persist failed: rollback discards the pending mutation, leaving the
            # previously persisted state intact (no partial update).
            session.rollback()
            return DagChoiceResult(
                success=False,
                campaign_id=campaign,
                dag_id=dag_id,
                failure=DagChoiceFailure.PERSISTENCE_FAILURE,
                message=_MESSAGE_PERSISTENCE_FAILURE,
            )

        session.refresh(stage_run)
        return DagChoiceResult(
            success=True,
            campaign_id=campaign,
            dag_id=dag_id,
            stage_run_id=stage_run.id,
        )

    def auto_complete_stage2(self, campaign: str) -> DagAutoGenerationResult:
        """Try to auto-complete Stage 2 (curated DAG) of campaign ``campaign``.

        The DAG counterpart of
        :meth:`~app.services.environment.stage_service.EnvironmentStageService.auto_concluir_estagio4`:
        same sequential-gate seam, idempotency, transactional ``StageRun`` upsert
        and typed result.

        Flow (all paths are read-only except the final completion):

        1. **Sequential gate (Req. 9.3).** If the campaign's Stage 1 (Modeling) is
           **not** ``completed``, it does nothing and returns
           ``blocked_by_gate=True`` — Stage 2 stays blocked.
        2. **Idempotency.** If Stage 2 is already ``completed``, it returns
           ``completed=True`` without re-persisting (the state is already the
           desired one).
        3. **Curated backfill (Req. 9.1, 9.4).** Runs :meth:`sync_curated_dags`
           (which now passes the Stage 1 Modeling to the generator — **no network
           and no AI**). If **no** derivable DAG was persisted for the campaign,
           it returns ``no_dag=True`` without completing — the UI shows "Generate
           DAG" disabled and the advance stays blocked (Req. 9.4).
        4. **Completion.** With at least one persisted DAG ⇒ upsert of the
           ``(campaign, 2)`` ``StageRun`` to ``completed``/100 in a single
           transaction. A ``commit`` failure ⇒ ``rollback`` +
           ``persistence_failed=True`` (previous state preserved).

        It does not raise on expected paths — the reason is always a typed signal
        in the result, like ``DagChoiceResult`` / ``ProvisionamentoAutoResult``.
        """
        session = self._require_session()
        stage_service = self._get_stage_service()
        states = stage_service._stage_states_map(campaign)

        # (1) Sequential gate (Req. 7.1, 3.1): DAG Generation is Stage 3 in the
        # six-stage pipeline; its Input is the campaign attributes from Stage 2
        # (Campaign Selection — ``STAGE_CAMPAIGN_SELECTION``). The gate only
        # releases when the immediately preceding Stage (the 2nd) is
        # ``completed``, exactly what ``is_stage_startable(campaign, 3)`` requires.
        if (
            states.get(STAGE_CAMPAIGN_SELECTION) is not StageState.COMPLETED
        ):
            return DagAutoGenerationResult(
                campaign_id=campaign,
                completed=False,
                blocked_by_gate=True,
                message=_MSG_STAGE3_GATE,
            )

        # (2) Idempotency: already completed ⇒ nothing to re-persist.
        if states.get(STAGE_DAG_GENERATION) is StageState.COMPLETED:
            row = stage_service._stage_run_row(campaign, STAGE_DAG_GENERATION)
            dags = self.list_dags(campaign)
            return DagAutoGenerationResult(
                campaign_id=campaign,
                completed=True,
                total_dags=len(dags),
                stage_run_id=(row.id if row is not None else None),
            )

        # (3) Curated backfill (passes the Modeling to the generator). With no
        # derivable DAG, Stage 2 does not complete (Req. 9.4).
        dags = self.sync_curated_dags(campaign)
        if not dags:
            return DagAutoGenerationResult(
                campaign_id=campaign,
                completed=False,
                no_dag=True,
                total_dags=0,
                message=_MSG_STAGE3_NO_DAG,
            )

        # (4) There is a derivable DAG ⇒ complete Stage 2 (absolute, transactional upsert).
        stage_run = self._get_or_create_stage_run_dag_generation(campaign)
        stage_run.state = StageState.COMPLETED
        stage_run.progress = 100
        stage_run.error_message = None
        stage_run.failed_step = None
        stage_run.updated_at = _utcnow()

        try:
            session.commit()
        except SQLAlchemyError:
            session.rollback()
            return DagAutoGenerationResult(
                campaign_id=campaign,
                completed=False,
                total_dags=len(dags),
                persistence_failed=True,
                message=_MSG_STAGE3_PERSISTENCE_FAILURE,
            )

        session.refresh(stage_run)
        return DagAutoGenerationResult(
            campaign_id=campaign,
            completed=True,
            total_dags=len(dags),
            stage_run_id=stage_run.id,
        )

    def _get_or_create_stage_run_dag_generation(self, campaign: str) -> StageRun:
        """Return the Stage 2 (DAG Generation) ``StageRun``, creating it if absent.

        Keeps one row per ``(campaign_id, stage=2)`` (same upsert contract as
        ``StageService`` / ``SessionStateService._get_or_create_stage_run``), so
        writing the choice (or the auto-completion) updates the existing Stage 2
        run instead of piling up rows.
        """
        session = self._require_session()
        stmt = (
            select(StageRun)
            .where(StageRun.campaign_id == campaign)
            .where(StageRun.stage == STAGE_DAG_GENERATION)
            .order_by(StageRun.id.asc())
        )
        stage_run = session.execute(stmt).scalars().first()
        if stage_run is None:
            stage_run = StageRun(campaign_id=campaign, stage=STAGE_DAG_GENERATION)
            session.add(stage_run)
        return stage_run

    def _upsert_case_row(self, campaign: str, label: Optional[str]) -> None:
        """Ensure the ``Case`` row exists to satisfy the ``dags.campaign_id`` FK.

        Same upsert-by-``id`` pattern used in ``StageService`` when persisting a
        campaign. Only fills ``name`` when the row is new (or has no name), so it
        does not overwrite metadata already persisted by Stage 1.
        """
        session = self._require_session()
        db_case = session.get(CaseModel, campaign)
        if db_case is None:
            db_case = CaseModel(id=campaign, name=label or campaign)
            session.add(db_case)
        elif not db_case.name:
            db_case.name = label or campaign
        session.flush()

    @staticmethod
    def _derive_edges(dag: DagData) -> list[tuple[str, str]]:
        """Derive the DAG's ``(from, to)`` edges from the structural relations.

        Edges come from each structural node's ``child_nodes`` (the
        authoritative edge source, exactly as
        ``stage_service._extract_relationships`` does). This is the single shared
        edge-derivation routine used by both :meth:`build_mermaid` and
        :meth:`get_dag`, so the ``edges`` returned in the payload are always
        consistent with the edges drawn in the Mermaid string.

        Only nodes carrying a ``node_id`` can participate (the DAG files always
        key relations by ``node_id``), edges are de-duplicated by ``(from, to)``
        and edges to unknown node ids are skipped defensively — deriving edges
        only from these relations (never inventing any) preserves the
        acyclicity guaranteed by ``metadata.validation.is_dag``.

        Pure: deterministic, no I/O and no network.
        """
        known_ids: set[str] = {
            node.node_id for node in dag.structural_nodes if node.node_id is not None
        }
        edges: list[tuple[str, str]] = []
        seen_edges: set[tuple[str, str]] = set()
        for node in dag.structural_nodes:
            if node.node_id is None:
                continue
            for child in node.child_nodes:
                edge = (node.node_id, child)
                if edge in seen_edges or child not in known_ids:
                    continue
                seen_edges.add(edge)
                edges.append(edge)
        return edges

    @staticmethod
    def build_mermaid(dag: DagData) -> str:
        """Build a pure, acyclic-by-construction Mermaid ``flowchart TD`` string.

        One node per ``structural_node`` labelled
        ``n_{node_id}["{technique_id}<br/>{technique_name}<br/>{tactic}"]`` and
        one edge per edge entry (``n_{from} --> n_{to}``), mirroring
        ``topology._mermaid``. Edges are derived from the DAG's structural
        relations (``parent_nodes``/``child_nodes``) without introducing extra
        edges, preserving the acyclicity guaranteed by
        ``metadata.validation.is_dag``. No embedded scripts.

        Pure function: deterministic, no I/O and no network — it depends only on
        its ``dag`` argument. An empty DAG (no structural nodes) yields a minimal
        valid ``flowchart TD`` (header only), never raising.
        """

        def _mermaid_node_id(node_id: str) -> str:
            """Sanitize a ``node_id`` into a Mermaid-safe ``n_...`` identifier.

            Replaces every character that is not ``[A-Za-z0-9_]`` with ``_`` and
            prefixes with ``n_`` so the id is always a valid Mermaid node id.
            """
            return "n_" + _UNSAFE_NODE_ID_CHARS.sub("_", node_id)

        def _label_part(value: Optional[str]) -> str:
            """A label segment with any ``"`` stripped so it can't break syntax.

            Mermaid node labels are quoted with ``"``; a literal ``"`` inside the
            label would terminate the string early (and could be abused to inject
            markup), so quotes are removed. ``None`` becomes an empty segment.
            """
            return (value or "").replace('"', "")

        lines: list[str] = ["flowchart TD"]

        # Only nodes that carry a ``node_id`` can be addressed by an edge; the
        # DAG files always key relations by ``node_id`` (see
        # stage_service._extract_relationships), so a node without one is skipped
        # — it cannot participate in the graph.
        for node in dag.structural_nodes:
            if node.node_id is None:
                continue
            nid = _mermaid_node_id(node.node_id)
            technique_id = _label_part(node.technique_id)
            technique_name = _label_part(node.technique_name)
            tactic = _label_part(node.tactic)
            label = f"{technique_id}<br/>{technique_name}<br/>{tactic}"
            lines.append(f'  {nid}["{label}"]')

        # One edge per DAG relation, derived from the structural nodes'
        # ``child_nodes`` via the shared :meth:`_derive_edges` routine (the
        # authoritative edge source, exactly as
        # stage_service._extract_relationships does). Deriving edges from these
        # relations — instead of inventing any — preserves the acyclicity
        # guaranteed by ``metadata.validation.is_dag``. De-duplicated by
        # (from, to); edges to unknown node ids are skipped defensively.
        for src_id, dst_id in DagService._derive_edges(dag):
            src = _mermaid_node_id(src_id)
            dst = _mermaid_node_id(dst_id)
            lines.append(f"  {src} --> {dst}")

        return "\n".join(lines)

    def get_dag(self, campaign: str) -> DagView:
        """Resolve the DAG view for a case slug (curated / stub / error).

        * Curated slug (in ``CaseService.CURATED_CASES``) with a valid
          ``{slug}_dag.json``: ``curated=True``, ``can_advance=True``, with
          ``nodes``/``edges`` and the assembled ``mermaid``.
        * Non-curated slug: "pass-through" (``curated=False``,
          ``can_advance=False``, ``unavailable=True``, ``message``) — **no AI
          service is invoked**.
        * Curated slug with missing/malformed DAG: typed error (``curated=True``,
          ``can_advance=False``, ``error``) distinguishing missing from
          malformed.

        Pure read: this method only reads ``{slug}_dag.json`` through
        ``CaseService`` and performs **no network access** and **no AI call** on
        any path (there is no AI client on this path — Req. 9.1, 9.2).
        """
        curated_cases = self._case_service.list_cases()

        # --- Non-curated slug: "pass-through" availability stub -------------
        # No AI service is invoked (and none exists on this path); just return
        # the stub (Req. 5.4, 5.5, 9.1, 9.2).
        if campaign not in curated_cases:
            return DagView(
                campaign_id=campaign,
                name=campaign.replace("_", " ").title(),
                curated=False,
                can_advance=False,
                unavailable=True,
                message=(
                    "A geração automática do DAG ainda não está disponível para "
                    "esta campanha."
                ),
            )

        name = curated_cases[campaign]

        # --- Curated slug: load the case, capturing file/parse problems as a
        # typed value (never a raised exception) via try_load_case (Req. 5.3,
        # 6.4). ``load_case`` would instead fail fast.
        result = self._case_service.try_load_case(campaign)

        if not result.ok or result.case is None:
            error = result.error
            # Distinguish a missing DAG file from a malformed/invalid one in a
            # descriptive pt-BR message (Req. 5.3, 6.4).
            if error is not None and error.error_type is CaseLoadErrorType.MISSING_FILE:
                message = (
                    f"O DAG de '{campaign}' não foi encontrado (arquivo ausente)."
                )
            elif error is not None and (
                error.error_type is CaseLoadErrorType.MALFORMED_JSON
            ):
                message = (
                    f"O DAG de '{campaign}' está malformado (JSON inválido)."
                )
            elif error is not None and (
                error.error_type is CaseLoadErrorType.INVALID_STRUCTURE
            ):
                message = (
                    f"O DAG de '{campaign}' está malformado (estrutura inválida)."
                )
            else:
                message = (
                    f"O DAG de '{campaign}' não pôde ser lido ou está malformado."
                )
            return DagView(
                campaign_id=campaign,
                name=name,
                curated=True,
                can_advance=False,
                error=message,
            )

        dag = result.case.dag
        if dag is None:
            # The case loaded, but carries no DAG payload (null/absent): treat as
            # a missing DAG for the curated case (Req. 5.3, 6.4).
            return DagView(
                campaign_id=campaign,
                name=name,
                curated=True,
                can_advance=False,
                error=f"O DAG de '{campaign}' não foi encontrado (arquivo ausente).",
            )

        # --- Curated slug with a valid DAG: build nodes, edges and Mermaid.
        # Edges are derived EXACTLY like build_mermaid (shared helper), so the
        # ``edges`` always match the Mermaid edges (Req. 5.2, 6.1, 6.3).
        nodes = [
            DagNodeView(
                node_id=node.node_id,
                technique_id=node.technique_id,
                technique_name=node.technique_name,
                tactic=node.tactic,
            )
            for node in dag.structural_nodes
            if node.node_id is not None
        ]
        edges = [
            DagEdgeView(source=src, target=dst)
            for src, dst in DagService._derive_edges(dag)
        ]
        mermaid = DagService.build_mermaid(dag)

        return DagView(
            campaign_id=campaign,
            name=name,
            curated=True,
            can_advance=True,
            nodes=nodes,
            edges=edges,
            mermaid=mermaid,
        )
