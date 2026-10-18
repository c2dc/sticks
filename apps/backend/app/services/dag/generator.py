"""DAG-generation seam: the ``DagGenerator`` Protocol and the ``ProducedDag`` DTO.

This module defines the **extension point** through which a DAG is produced for
a Campaign. The idea is to separate *how* a DAG is generated (today by human
curation, tomorrow by an AI agent) from the *rest* of the system (persistence,
listing, Mermaid assembly, Stage 1 choice), so an AI implementation can be
plugged in later **without** changing this signature nor the ``Dag`` entity
(Req. 5.1, 5.3, 9.3).

``DagGenerator``
    A ``@runtime_checkable`` :class:`typing.Protocol` with two operations:

    * ``origin() -> DagOrigin`` — the origin (``curadoria_humana`` | ``gerado_ia``)
      the implementation stamps on the DAGs it produces.
    * ``generate(campaign: str, modeling: StructuralModeling) -> list[ProducedDag]``
      — produces zero or more DAGs for the Campaign *slug* from the Structural
      Modeling (Stage 1); in the five-stage scheme the DAG (Stage 2) is derived
      from the Modeling (Req. 3.1, 10.1). The curation implementation
      (:class:`CurationDagGenerator`) **ignores** ``modeling`` (the curated graph
      already exists on disk) and returns exactly one ``ProducedDag`` with
      ``origin = curadoria_humana``; a future AI implementation (``AiDagGenerator``,
      **out of scope** — Req. 9) **would use** ``modeling`` to produce DAGs with
      ``origin = gerado_ia`` without redesign.

``ProducedDag``
    The typed (Pydantic) result of a generation, ready for persistence as a
    ``Dag`` entity (``origin``, ``identifier``, ``label`` and the structural
    ``content`` of the graph). The fields mirror the ``Dag`` entity columns
    directly (Req. 4.1–4.4), so ``DagService`` can persist a ``ProducedDag``
    without extra conversion.

As in the rest of the ``dag`` package, nothing here accesses the network nor
invokes AI; the curation generation only reads local disk via ``CaseService``
(Req. 5.5, 9.1).

_Requirements: 3.1, 10.1_
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.models.enums import DagOrigin
from app.services.campaign_service import CaseService, DagData
from app.services.dag.dag_service import DagService
from app.services.stage.stage_service import RelationshipElement, TechniqueElement


class StructuralModeling(BaseModel):
    """The Structural Modeling (Stage 1) consumed by DAG generation (Stage 2).

    Typed DTO that carries the Stage 1 structural output — at minimum the ATT&CK
    **techniques** and the **relationships** between them — reusing the same
    elements of :class:`~app.services.stage.stage_service.Stage1Output`
    (:class:`~app.services.stage.stage_service.TechniqueElement` and
    :class:`~app.services.stage.stage_service.RelationshipElement`), so the
    Stage 2 graph is **derived from the Modeling** (Req. 3.1, 10.1).

    In the five-stage scheme, the DAG (Stage 2) is derived from the Structural
    Modeling (Stage 1). This is the *input* of the :class:`DagGenerator` seam:
    the curation implementation **ignores** it (the curated graph already exists
    on disk), while a future AI implementation (``AiDagGenerator``, out of scope
    — Req. 9) will use it to generate the graph without redesigning the seam.
    """

    techniques: list[TechniqueElement] = Field(default_factory=list)
    relationships: list[RelationshipElement] = Field(default_factory=list)


class ProducedDag(BaseModel):
    """A DAG produced by a :class:`DagGenerator`, ready for persistence.

    Mirrors the ``Dag`` entity columns (Req. 4.1–4.4), so a ``ProducedDag`` can
    be persisted directly by ``DagService`` without intermediate conversion:

    * ``origin`` — the :class:`~app.models.enums.DagOrigin` that stamps the DAG's
      provenance (``curadoria_humana`` for the curation implementation;
      ``gerado_ia`` for a future AI implementation — Req. 4.3, 9.3).
    * ``identifier`` — stable DAG identifier within the Campaign (for example the
      curated campaign *slug*), used for the idempotent backfill.
    * ``label`` — optional readable label shown in the UI (``None`` when there is
      no specific label).
    * ``content`` — the structural graph in the format
      ``{"structural_nodes": [...], "arestas": [{"de", "para"}]}``: the list of
      structural nodes and the directed edges (graph-content keys ``de``/``para``,
      exactly like the ``GET /api/campaigns/{campaign}/dag`` contract and the
      ``DagService._derive_edges`` helper). It is a ``dict`` to match the
      ``content`` (JSON) column of the ``Dag`` entity and let an AI implementation
      produce the same format with no schema change (Req. 9.3, 9.4).
    """

    origin: DagOrigin
    identifier: str
    label: Optional[str] = None
    content: dict = Field(default_factory=dict)


@runtime_checkable
class DagGenerator(Protocol):
    """DAG-generation extension seam (Req. 5.1, 5.3).

    A ``@runtime_checkable`` ``Protocol`` with a single responsibility: produce
    DAGs for a Campaign from the Structural Modeling (Stage 1). Any object that
    exposes ``origin()`` and ``generate(campaign, modeling)`` with these
    signatures satisfies the protocol — no explicit inheritance is required
    (structural duck typing).

    Today there is a curation implementation (:class:`CurationDagGenerator`); an
    AI implementation (``AiDagGenerator``) can be added later **without** changing
    this signature nor the ``Dag`` entity (Req. 5.3, 9.3). ``DagService`` injects
    a ``DagGenerator`` (curation by default), so swapping the origin is a matter
    of injection, not redesign.
    """

    def origin(self) -> DagOrigin:
        """The :class:`~app.models.enums.DagOrigin` stamped on produced DAGs."""
        ...

    def generate(self, campaign: str, modeling: "StructuralModeling") -> list["ProducedDag"]:
        """Produce the DAGs of the ``campaign`` *slug* from the Structural Modeling.

        ``modeling`` is the Stage 1 output (techniques + relationships); the
        Stage 2 DAG is derived from it. The curation implementation
        (:class:`CurationDagGenerator`) **ignores** ``modeling`` — the curated
        graph already exists on disk, and it returns exactly one ``ProducedDag``
        (``origin=curadoria_humana``) read from ``data/dag/{slug}_dag.json`` (a
        Campaign with no derivable DAG results in an empty list). The future AI
        implementation (``AiDagGenerator``) **will use** ``modeling`` to generate
        the graph. Does not access the network nor invoke AI (Req. 5.5, 9.1).
        """
        ...


class CurationDagGenerator:
    """Curation implementation of the :class:`DagGenerator` seam (Req. 5.2, 5.4, 5.5).

    Produces a Campaign's DAG from the material **already curated by a human** in
    ``data/dag/{slug}_dag.json``, stamping ``origin = curadoria_humana`` (Req. 5.2,
    5.4). The read is done exclusively from local disk via
    :class:`~app.services.campaign_service.CaseService`
    (:meth:`~app.services.campaign_service.CaseService.try_load_case`): there is
    **no network access nor invocation of any AI service** on any path of this
    implementation (Req. 5.5, 9.1).

    The ``content`` of the :class:`ProducedDag` is assembled in the same format as
    the ``GET /api/campaigns/{campaign}/dag`` contract — ``structural_nodes`` (the
    structural nodes with the ATT&CK identity) and ``arestas`` (``{"de", "para"}``)
    — reusing :meth:`DagService._derive_edges`, so the persisted edges are always
    consistent with the ones Mermaid draws (Req. 4.4).

    Satisfies :class:`DagGenerator` structurally (``origin()`` + ``generate()``),
    with no explicit inheritance — exactly the point of the seam (Req. 5.1, 5.3).

    _Requirements: 5.2, 5.4, 5.5_
    """

    def __init__(self, case_service: Optional[CaseService] = None) -> None:
        # Reuse the CaseService for path resolution and typed parsing; a custom
        # CaseService can be injected (e.g. tests/fixtures).
        self._case_service: CaseService = (
            case_service if case_service is not None else CaseService()
        )

    @property
    def case_service(self) -> CaseService:
        """The :class:`CaseService` used to locate and read the curated DAG."""
        return self._case_service

    def origin(self) -> DagOrigin:
        """The origin stamped on produced DAGs: always ``curadoria_humana`` (Req. 5.4)."""
        return DagOrigin.HUMAN_CURATION

    @staticmethod
    def _assemble_content(dag: DagData) -> dict:
        """Assemble the persistable structural ``content`` from a :class:`DagData`.

        Produces ``{"structural_nodes": [...], "arestas": [{"de", "para"}]}``: one
        node per ``structural_node`` carrying a ``node_id`` (with the ATT&CK
        identity ``technique_id``/``technique_name``/``tactic`` and the relations
        ``parent_nodes``/``child_nodes`` preserved), and one edge per relation
        derived via :meth:`DagService._derive_edges` — the same routine used by
        ``montar_mermaid``/``get_dag``, so the persisted edges match the graph's
        edges exactly (Req. 4.4).

        Pure: deterministic, no I/O and no network.
        """
        structural_nodes = [
            {
                "node_id": node.node_id,
                "technique_id": node.technique_id,
                "technique_name": node.technique_name,
                "tactic": node.tactic,
                "parent_nodes": list(node.parent_nodes),
                "child_nodes": list(node.child_nodes),
            }
            for node in dag.structural_nodes
            if node.node_id is not None
        ]
        edges = [
            {"de": src, "para": dst} for src, dst in DagService._derive_edges(dag)
        ]
        return {"structural_nodes": structural_nodes, "arestas": edges}

    def generate(self, campaign: str, modeling: StructuralModeling) -> list[ProducedDag]:
        """Produce the curated DAG of the ``campaign`` *slug* as a list of ``ProducedDag``.

        ``modeling`` (Stage 1 output) is **intentionally ignored** by this
        curation implementation — the curated graph already exists on disk, and
        the parameter is only here to honor the :class:`DagGenerator` seam
        signature (Req. 3.1, 10.1), so a future AI implementation
        (``AiDagGenerator``, out of scope — Req. 9) can use it to generate the
        graph without redesigning the seam.

        Reads ``data/dag/{slug}_dag.json`` via
        :meth:`~app.services.campaign_service.CaseService.try_load_case` (which
        **never raises** on an absent/malformed file — Req. 5.5) and returns
        **exactly one** :class:`ProducedDag` with ``origin = curadoria_humana``
        (Req. 5.2, 5.4). A Campaign whose curated DAG is absent/malformed — or a
        Campaign with no DAG — results in an **empty list** (no invalid ``Dag`` is
        produced for the backfill to persist, Req. 4.5), without taking down the
        rest.

        Does not access the network and does not invoke AI on any path (Req. 5.5,
        9.1).
        """
        result = self._case_service.try_load_case(campaign)

        # Curated DAG absent/malformed (or Campaign with no DAG): nothing to
        # produce — the backfill must not persist an invalid Dag (Req. 4.5, 5.5).
        if not result.ok or result.case is None:
            return []
        dag = result.case.dag
        if dag is None:
            return []

        return [
            ProducedDag(
                origin=DagOrigin.HUMAN_CURATION,
                identifier=campaign,
                label=result.case.name,
                content=self._assemble_content(dag),
            )
        ]
