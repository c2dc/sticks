"""FlowService: read access to persisted attack flows.

Replaces the removed curated-era attack flow read path. It reads ``attack_flows``
rows, turns the stored ``content`` into an :class:`AttackFlow` (current format,
or the legacy ``structural_nodes`` + ``edges``/``arestas`` format used by
curated and earlier AI flows), and renders Mermaid with back edges dashed.

Pure database reads: no network, no AI, no writes.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import AttackFlowRow
from app.models.enums import FlowOrigin, FlowProvenance
from app.services.attack_flow.cycles import annotate_flow_cycles
from app.services.attack_flow.model import (
    CONTROL_TYPES,
    AttackFlow,
    AttackFlowObject,
    FlowProvenanceRecord,
)

# Namespace for deterministic ids of legacy nodes (same flow, same ids).
_LEGACY_NAMESPACE = uuid.UUID("5d0a1c1e-7f0b-4d55-9a54-1d2c3b4a5e6f")
_MERMAID_UNSAFE = re.compile(r'["`<>\[\]{}|\\]')


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class FlowReference(BaseModel):
    """A corpus flow referenced by the provenance of a campaign flow."""

    id: int
    label: Optional[str] = None
    source_key: Optional[str] = None


class FlowProvenanceView(BaseModel):
    """Provenance of a flow: ``exact`` | ``adapted`` | ``created`` + references."""

    kind: FlowProvenance
    similarity: float = 0.0
    references: list[int] = Field(default_factory=list)
    reference_flows: list[FlowReference] = Field(default_factory=list)


class FlowSummary(BaseModel):
    id: int
    campaign_id: Optional[str] = None
    origin: FlowOrigin
    identifier: str
    label: Optional[str] = None
    scope: Optional[str] = None
    has_cycles: bool = False
    provenance: Optional[FlowProvenanceView] = None
    created_at: dt.datetime


class FlowNodeView(BaseModel):
    node_id: str
    type: str
    name: Optional[str] = None
    technique_id: Optional[str] = None
    tactic_id: Optional[str] = None
    command_ref: Optional[int] = None


class FlowEdgeView(BaseModel):
    source: str
    target: str
    is_back_edge: bool = False


class FlowDetail(FlowSummary):
    description: Optional[str] = None
    nodes: list[FlowNodeView] = Field(default_factory=list)
    edges: list[FlowEdgeView] = Field(default_factory=list)
    mermaid: str = ""
    cycles: list[list[str]] = Field(default_factory=list)
    back_edges: list[tuple[str, str]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Content conversion
# ---------------------------------------------------------------------------


def _legacy_object_id(flow_key: str, node_id: str) -> str:
    return f"attack-action--{uuid.uuid5(_LEGACY_NAMESPACE, f'{flow_key}/{node_id}')}"


def _legacy_edges(content: dict[str, Any]) -> list[tuple[str, str]]:
    """Edges of a legacy content: ``edges`` {from,to}, ``arestas`` {de,para}, or child_nodes."""
    pairs: list[tuple[str, str]] = []
    for key, src_key, dst_key in (("edges", "from", "to"), ("arestas", "de", "para")):
        for item in content.get(key) or []:
            if isinstance(item, dict) and item.get(src_key) and item.get(dst_key):
                pairs.append((str(item[src_key]), str(item[dst_key])))
    if not pairs:
        for node in content.get("structural_nodes") or []:
            node_id = node.get("node_id")
            if not node_id:
                continue
            for child in node.get("child_nodes") or []:
                pairs.append((str(node_id), str(child)))
    return list(dict.fromkeys(pairs))


def flow_from_content(
    content: dict[str, Any],
    *,
    flow_key: str,
    name: str = "",
    description: str = "",
    scope: str = "",
    provenance: Optional[dict[str, Any]] = None,
) -> AttackFlow:
    """Build an :class:`AttackFlow` from stored ``content`` (current or legacy).

    The legacy ``structural_nodes`` become ``attack-action`` objects with
    deterministic ids; cycles and back edges are always (re)computed so the
    result never depends on what was stored.
    """
    if not isinstance(content, dict):
        raise ValueError("flow_content_must_be_object")

    if "objects" in content and "flow_id" in content:
        flow = AttackFlow.model_validate(content)
    else:
        nodes = [
            n
            for n in (content.get("structural_nodes") or [])
            if isinstance(n, dict) and n.get("node_id")
        ]
        id_map = {str(n["node_id"]): _legacy_object_id(flow_key, str(n["node_id"])) for n in nodes}
        effects: dict[str, list[str]] = {oid: [] for oid in id_map.values()}
        has_parent: set[str] = set()
        for src, dst in _legacy_edges(content):
            if src in id_map and dst in id_map:
                effects[id_map[src]].append(id_map[dst])
                has_parent.add(id_map[dst])
        objects = [
            AttackFlowObject(
                id=id_map[str(n["node_id"])],
                type="attack-action",
                name=n.get("technique_name") or str(n["node_id"]),
                technique_id=n.get("technique_id"),
                tactic_id=n.get("tactic"),
                effect_refs=effects[id_map[str(n["node_id"])]],
                properties={"legacy_node_id": str(n["node_id"])},
            )
            for n in nodes
        ]
        flow = AttackFlow(
            flow_id=f"attack-flow--{uuid.uuid5(_LEGACY_NAMESPACE, flow_key)}",
            name=name or str(content.get("campaign_name") or flow_key),
            description=description,
            scope=scope,
            start_refs=[o.id for o in objects if o.id not in has_parent],
            objects=objects,
        )

    if provenance:
        flow = flow.model_copy(
            update={"provenance": FlowProvenanceRecord.model_validate(provenance)}
        )
    return annotate_flow_cycles(flow)


def _node_label(obj: AttackFlowObject) -> str:
    if obj.type == "attack-operator":
        text = obj.operator or "OP"
    elif obj.type == "attack-condition":
        text = obj.name or obj.pattern or "condition"
    else:
        text = f"{obj.technique_id} {obj.name}".strip() if obj.technique_id else (obj.name or obj.id)
    return _MERMAID_UNSAFE.sub("", text)[:80] or "node"


def build_flow_mermaid(flow: AttackFlow) -> str:
    """Mermaid ``flowchart TD`` of the control graph; back edges are dashed."""
    control = [o for o in flow.objects if o.type in CONTROL_TYPES]
    alias = {o.id: f"n{i}" for i, o in enumerate(control)}
    back = {(s, d) for s, d in flow.back_edges}
    lines = ["flowchart TD"]
    for obj in control:
        label = _node_label(obj)
        if obj.type == "attack-operator":
            lines.append(f'    {alias[obj.id]}{{"{label}"}}')
        elif obj.type == "attack-condition":
            lines.append(f'    {alias[obj.id]}(["{label}"])')
        else:
            lines.append(f'    {alias[obj.id]}["{label}"]')
    for src, dst in flow.edges():
        if src not in alias or dst not in alias:
            continue
        arrow = "-.->" if (src, dst) in back else "-->"
        lines.append(f"    {alias[src]} {arrow} {alias[dst]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class FlowService:
    """Lists and reads persisted attack flows (campaign flows and corpus)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ---- public API --------------------------------------------------------

    def list_flows(self, campaign: str) -> list[FlowSummary]:
        rows = (
            self._session.execute(
                select(AttackFlowRow)
                .where(AttackFlowRow.campaign_id == campaign)
                .order_by(AttackFlowRow.id.asc())
            )
            .scalars()
            .all()
        )
        return [self._summary(row) for row in rows]

    def get_flow(self, campaign: str, flow_id: int) -> Optional[FlowDetail]:
        """Flow of the campaign, or ``None`` if missing / of another campaign."""
        row = self._session.get(AttackFlowRow, flow_id)
        if row is None or row.campaign_id != campaign:
            return None
        return self._detail(row)

    def get_corpus_flow(self, flow_id: int) -> Optional[FlowDetail]:
        """Flow by id regardless of campaign (reference shown in provenance)."""
        row = self._session.get(AttackFlowRow, flow_id)
        return self._detail(row) if row is not None else None

    # ---- internals ---------------------------------------------------------

    def _provenance(self, raw: Any) -> Optional[FlowProvenanceView]:
        if not isinstance(raw, dict) or not raw.get("kind"):
            return None
        try:
            record = FlowProvenanceRecord.model_validate(raw)
        except ValueError:
            return None
        references: list[FlowReference] = []
        if record.references:
            rows = (
                self._session.execute(
                    select(AttackFlowRow).where(AttackFlowRow.id.in_(record.references))
                )
                .scalars()
                .all()
            )
            by_id = {r.id: r for r in rows}
            references = [
                FlowReference(id=i, label=by_id[i].label, source_key=by_id[i].source_key)
                for i in record.references
                if i in by_id
            ]
        return FlowProvenanceView(
            kind=record.kind,
            similarity=record.similarity,
            references=list(record.references),
            reference_flows=references,
        )

    def _summary(self, row: AttackFlowRow) -> FlowSummary:
        return FlowSummary(
            id=row.id,
            campaign_id=row.campaign_id,
            origin=row.origin,
            identifier=row.identifier,
            label=row.label,
            scope=row.scope,
            has_cycles=bool(row.has_cycles),
            provenance=self._provenance(row.provenance),
            created_at=row.created_at,
        )

    def _detail(self, row: AttackFlowRow) -> FlowDetail:
        try:
            flow = flow_from_content(
                row.content,
                flow_key=f"{row.campaign_id or 'corpus'}/{row.identifier}",
                name=row.label or "",
                description=row.description or "",
                scope=row.scope or "",
            )
        except ValueError:
            flow = AttackFlow(flow_id=f"attack-flow--{uuid.uuid5(_LEGACY_NAMESPACE, str(row.id))}", name=row.label or "")
        back = {(s, d) for s, d in flow.back_edges}
        nodes = [
            FlowNodeView(
                node_id=o.id,
                type=o.type,
                name=o.name,
                technique_id=o.technique_id,
                tactic_id=o.tactic_id,
                command_ref=o.command_ref,
            )
            for o in flow.objects
            if o.type in CONTROL_TYPES
        ]
        edges = [
            FlowEdgeView(source=s, target=d, is_back_edge=(s, d) in back)
            for s, d in flow.edges()
        ]
        summary = self._summary(row)
        data = summary.model_dump()
        data["has_cycles"] = bool(flow.cycles)
        return FlowDetail(
            **data,
            description=row.description or flow.description or None,
            nodes=nodes,
            edges=edges,
            mermaid=build_flow_mermaid(flow),
            cycles=flow.cycles,
            back_edges=flow.back_edges,
        )
