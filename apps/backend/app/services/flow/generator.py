"""Flow-generation seam: ``FlowGenerator`` protocol and its DTOs.

``ProducedFlow`` is the typed result of a generation, ready for persistence as an
``AttackFlowRow``. ``StructuralModeling`` is the (optional) input of the seam.
The curated implementation was removed with the curated pipeline; the AI
implementation is :class:`app.services.ai.agents.flow_agent.FlowAgent`.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.models.enums import FlowOrigin


class TechniqueElement(BaseModel):
    """One ATT&CK technique of a structural modeling."""

    technique_id: str
    technique_name: Optional[str] = None
    tactic: Optional[str] = None
    description: Optional[str] = None
    node_id: Optional[str] = None


class RelationshipElement(BaseModel):
    """A directed relationship between two technique nodes.

    ``source``/``target`` are node ids; ``source_technique``/``target_technique``
    carry the technique ids for display when available.
    """

    source: str
    target: str
    source_technique: Optional[str] = None
    target_technique: Optional[str] = None


class StructuralModeling(BaseModel):
    """Techniques and relationships consumed by flow generation."""

    techniques: list[TechniqueElement] = Field(default_factory=list)
    relationships: list[RelationshipElement] = Field(default_factory=list)

class ProducedFlow(BaseModel):
    """An attack flow produced by a :class:`FlowGenerator`, ready for persistence.

    Mirrors the ``AttackFlowRow`` entity columns (Req. 4.1–4.4), so a ``ProducedFlow`` can
    be persisted directly without intermediate conversion:

    * ``origin`` — the :class:`~app.models.enums.FlowOrigin` that stamps the attack flow's
      provenance (``curadoria_humana`` for the curation implementation;
      ``gerado_ia`` for a future AI implementation — Req. 4.3, 9.3).
    * ``identifier`` — stable attack flow identifier within the Campaign (for example the
      curated campaign *slug*), used for the idempotent backfill.
    * ``label`` — optional readable label shown in the UI (``None`` when there is
      no specific label).
    * ``content`` — the structural graph in the format
      ``{"structural_nodes": [...], "arestas": [{"de", "para"}]}``: the list of
      structural nodes and the directed edges (graph-content keys ``de``/``para``,
      exactly like the ``GET /api/campaigns/{campaign}/flows/{flow_id}`` contract and the
      edge-derivation helper). It is a ``dict`` to match the
      ``content`` (JSON) column of the ``AttackFlowRow`` entity and let an AI implementation
      produce the same format with no schema change (Req. 9.3, 9.4).
    * ``gaps`` — the campaign techniques (ATT&CK ``technique_id`` values) that
      made it into the proposed graph but have **no backing command** in the
      Command_Base (neither an exact ``technique_commands`` mapping nor a
      similarity suggestion). The attack flow agent records them here instead of
      inventing an unsupported command, so the gap is surfaced rather than
      papered over (Req. 11.4). Empty for the curation generator (which produces
      no gaps) and whenever every node technique is backed.
    """

    origin: FlowOrigin
    identifier: str
    label: Optional[str] = None
    content: dict = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    # Three-path decision record ``{kind, references, similarity}`` for flows
    # produced by the AI (``exact`` / ``adapted`` / ``created``); ``None`` for
    # curated flows.
    provenance: Optional[dict] = None


@runtime_checkable
class FlowGenerator(Protocol):
    """attack flow-generation extension seam (Req. 5.1, 5.3).

    A ``@runtime_checkable`` ``Protocol`` with a single responsibility: produce
    attack flows for a Campaign from the Structural Modeling (Stage 1). Any object that
    exposes ``origin()`` and ``generate(campaign, modeling)`` with these
    signatures satisfies the protocol — no explicit inheritance is required
    (structural duck typing).

    The AI implementation is ``FlowAgent``; another
    AI implementation (``FlowAgent``) can be added later **without** changing
    this signature nor the ``AttackFlowRow`` entity (Req. 5.3, 9.3). Swapping the origin is a matter of injection, not redesign.
    """

    def origin(self) -> FlowOrigin:
        """The :class:`~app.models.enums.FlowOrigin` stamped on produced attack flows."""
        ...

    def generate(self, campaign: str, modeling: "StructuralModeling") -> list["ProducedFlow"]:
        """Produce the attack flows of the ``campaign`` *slug* from the Structural Modeling.

        ``modeling`` is the Stage 1 output (techniques + relationships); the
        Stage 2 attack flow is derived from it. The curation implementation
        may ignore ``modeling`` — the curated
        graph already exists in the database, and it returns exactly one ``ProducedFlow``
        (``origin=curadoria_humana``) read from the persisted ``attack_flows`` row (a
        Campaign with no derivable attack flow results in an empty list). The future AI
        implementation (``FlowAgent``) **will use** ``modeling`` to generate
        the graph. Does not access the network nor invoke AI (Req. 5.5, 9.1).
        """
        ...
