"""Pure Attack Flow model (MITRE Attack Flow, STIX 2.1 extension).

No I/O. Objects are named like the STIX extension (``attack-action--<uuid>``).
Edges are always derived from ``effect_refs`` (never stored separately), and the
execution view is the edge set minus the back edges found by cycle analysis.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.enums import FlowProvenance

ObjectType = Literal[
    "attack-flow",
    "attack-action",
    "attack-condition",
    "attack-operator",
    "attack-asset",
]

# Object types that take part in the control flow graph.
CONTROL_TYPES: frozenset[str] = frozenset(
    {"attack-action", "attack-condition", "attack-operator"}
)

# Fixed timestamp keeps bundles deterministic (same flow, same bytes).
_STIX_TIMESTAMP = "1970-01-01T00:00:00.000Z"
_SPEC_VERSION = "2.1"
_BUNDLE_PREFIX = "bundle--"


class FlowProvenanceRecord(BaseModel):
    """Which decision path produced the flow and which flows were referenced."""

    kind: FlowProvenance
    references: list[int] = Field(default_factory=list)
    similarity: float = 0.0


class AttackFlowObject(BaseModel):
    id: str  # "attack-action--<uuid>"
    type: ObjectType
    name: str | None = None
    description: str | None = None
    # attack-action
    technique_id: str | None = None
    tactic_id: str | None = None
    command_ref: int | None = None  # technique_commands.id chosen by the agent
    asset_refs: list[str] = Field(default_factory=list)
    # attack-action, attack-condition and attack-operator
    effect_refs: list[str] = Field(default_factory=list)
    # attack-condition
    pattern: str | None = None
    # attack-operator
    operator: Literal["AND", "OR"] | None = None
    # attack-asset: original builder type and other free-form metadata
    properties: dict[str, Any] = Field(default_factory=dict)


class AttackFlow(BaseModel):
    flow_id: str  # id of the ``attack-flow`` object, "attack-flow--<uuid>"
    name: str
    description: str = ""
    scope: str = ""
    start_refs: list[str] = Field(default_factory=list)
    objects: list[AttackFlowObject] = Field(default_factory=list)
    provenance: FlowProvenanceRecord | None = None
    cycles: list[list[str]] = Field(default_factory=list)
    back_edges: list[tuple[str, str]] = Field(default_factory=list)

    # ------------------------------------------------------------------ edges
    def edges(self) -> list[tuple[str, str]]:
        """Every ``(object.id, ref)`` pair from ``effect_refs``, in object order."""
        return [(obj.id, ref) for obj in self.objects for ref in obj.effect_refs]

    def execution_edges(self) -> list[tuple[str, str]]:
        """Acyclic view: ``edges()`` minus ``back_edges``."""
        removed = {(src, dst) for src, dst in self.back_edges}
        return [edge for edge in self.edges() if edge not in removed]

    # ------------------------------------------------------------------- STIX
    def to_stix_bundle(self) -> dict[str, Any]:
        """Export a STIX 2.1 bundle; ``from_stix_bundle`` restores it losslessly."""
        flow_object: dict[str, Any] = {
            "type": "attack-flow",
            "spec_version": _SPEC_VERSION,
            "id": self.flow_id,
            "created": _STIX_TIMESTAMP,
            "modified": _STIX_TIMESTAMP,
            "name": self.name,
            "description": self.description,
            "scope": self.scope,
            "start_refs": list(self.start_refs),
        }
        if self.provenance is not None:
            flow_object["x_sticks_provenance"] = self.provenance.model_dump(mode="json")
        if self.cycles:
            flow_object["x_sticks_cycles"] = [list(c) for c in self.cycles]
        if self.back_edges:
            flow_object["x_sticks_back_edges"] = [list(e) for e in self.back_edges]

        stix_objects: list[dict[str, Any]] = [flow_object]
        for obj in self.objects:
            item: dict[str, Any] = {
                "type": obj.type,
                "spec_version": _SPEC_VERSION,
                "id": obj.id,
                "created": _STIX_TIMESTAMP,
                "modified": _STIX_TIMESTAMP,
            }
            for key in ("name", "description", "technique_id", "tactic_id", "pattern", "operator"):
                value = getattr(obj, key)
                if value is not None:
                    item[key] = value
            if obj.command_ref is not None:
                item["command_ref"] = obj.command_ref
            if obj.asset_refs:
                item["asset_refs"] = list(obj.asset_refs)
            if obj.effect_refs:
                item["effect_refs"] = list(obj.effect_refs)
            if obj.properties:
                item["x_sticks_properties"] = dict(obj.properties)
            stix_objects.append(item)

        suffix = self.flow_id.split("--", 1)[-1]
        return {
            "type": "bundle",
            "id": f"{_BUNDLE_PREFIX}{suffix}",
            "objects": stix_objects,
        }

    @classmethod
    def from_stix_bundle(cls, bundle: dict[str, Any]) -> "AttackFlow":
        raw_objects = bundle.get("objects")
        if not isinstance(raw_objects, list):
            raise ValueError("bundle_without_objects")
        flow_items = [o for o in raw_objects if o.get("type") == "attack-flow"]
        if len(flow_items) != 1:
            raise ValueError("bundle_must_have_one_attack_flow")
        head = flow_items[0]

        objects: list[AttackFlowObject] = []
        for item in raw_objects:
            if item.get("type") in (None, "attack-flow"):
                continue
            objects.append(
                AttackFlowObject(
                    id=item["id"],
                    type=item["type"],
                    name=item.get("name"),
                    description=item.get("description"),
                    technique_id=item.get("technique_id"),
                    tactic_id=item.get("tactic_id"),
                    command_ref=item.get("command_ref"),
                    asset_refs=list(item.get("asset_refs", [])),
                    effect_refs=list(item.get("effect_refs", [])),
                    pattern=item.get("pattern"),
                    operator=item.get("operator"),
                    properties=dict(item.get("x_sticks_properties", {})),
                )
            )

        provenance = head.get("x_sticks_provenance")
        return cls(
            flow_id=head["id"],
            name=head["name"],
            description=head.get("description", ""),
            scope=head.get("scope", ""),
            start_refs=list(head.get("start_refs", [])),
            objects=objects,
            provenance=FlowProvenanceRecord(**provenance) if provenance else None,
            cycles=[list(c) for c in head.get("x_sticks_cycles", [])],
            back_edges=[(e[0], e[1]) for e in head.get("x_sticks_back_edges", [])],
        )
