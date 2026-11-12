"""Parser for Attack Flow Builder ``.afb`` files (``schema = "attack_flow_v2"``).

An ``.afb`` has no ``effect_refs``. An edge is a ``dynamic_line`` whose
``source``/``target`` are latches; a latch belongs to an anchor and an anchor
belongs to an object. The parser resolves ``latch -> anchor -> object`` and
builds the STIX-style objects. Ids are deterministic: they reuse the UUID of
the ``.afb`` instance, so re-ingesting a file yields the same ids.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from app.services.attack_flow.model import CONTROL_TYPES, AttackFlow, AttackFlowObject

SCHEMA = "attack_flow_v2"
_TECHNIQUE_RE = re.compile(r"^T\d{4}(\.\d{3})?$")
_NAMESPACE = uuid.UUID("6f1d2c1e-5a53-4b8e-9c1d-2d7f6a0b8e11")

# afb object id -> (stix type, operator)
_DOMAIN_TYPES: dict[str, tuple[str, str | None]] = {
    "action": ("attack-action", None),
    "condition": ("attack-condition", None),
    "AND_operator": ("attack-operator", "AND"),
    "OR_operator": ("attack-operator", "OR"),
    "asset": ("attack-asset", None),
}
# STIX cyber observable / support objects stored as assets.
_SUPPORT_TYPES: frozenset[str] = frozenset(
    {
        "tool", "malware", "file", "process", "infrastructure", "vulnerability",
        "mitigation", "directory", "url", "ipv4_addr", "ipv6_addr", "domain_name",
        "email_addr", "email_message", "user_account", "windows_registry_key",
        "network_traffic", "software", "artifact", "autonomous_system", "mutex",
        "x509_certificate", "identity", "threat_actor", "intrusion_set",
        "campaign", "indicator", "attack_pattern", "course_of_action",
    }
)


class AfbParseError(ValueError):
    """The whole flow file is unusable; ``reason`` is a short English code."""

    def __init__(self, reason: str, *, flow_file: str):
        super().__init__(f"{flow_file}: {reason}")
        self.reason = reason
        self.flow_file = flow_file


def _stix_uuid(instance: str) -> str:
    try:
        return str(uuid.UUID(instance))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(_NAMESPACE, str(instance)))


def _props(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize ``properties`` (list of [key, value] pairs, or a dict)."""
    props = raw.get("properties", [])
    if isinstance(props, dict):
        return dict(props)
    result: dict[str, Any] = {}
    if isinstance(props, list):
        for pair in props:
            if isinstance(pair, (list, tuple)) and len(pair) == 2 and isinstance(pair[0], str):
                result[pair[0]] = pair[1]
    return result


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _anchor_ids(raw: dict[str, Any]) -> list[str]:
    anchors = raw.get("anchors", {})
    if isinstance(anchors, dict):
        values = list(anchors.values())
    elif isinstance(anchors, list):
        values = anchors
    else:
        return []
    return [a for a in values if isinstance(a, str)]


def parse_afb(raw: bytes, *, source_key: str) -> AttackFlow:
    """Parse one ``.afb`` document. Raises ``AfbParseError`` on any defect."""

    def fail(reason: str) -> AfbParseError:
        return AfbParseError(reason, flow_file=source_key)

    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise fail("invalid_json") from None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise fail("unsupported_schema")
    objects = document.get("objects")
    if not isinstance(objects, list) or not all(isinstance(o, dict) for o in objects):
        raise fail("missing_objects")

    by_instance: dict[str, dict[str, Any]] = {}
    for obj in objects:
        instance = obj.get("instance")
        if isinstance(instance, str):
            by_instance[instance] = obj

    flows = [o for o in objects if o.get("id") == "flow"]
    if len(flows) != 1:
        raise fail("expected_exactly_one_flow_object")
    flow_raw = flows[0]
    flow_props = _props(flow_raw)
    flow_name = _text(flow_props.get("name"))
    if flow_name is None or not isinstance(flow_raw.get("instance"), str):
        raise fail("missing_flow_name")

    # latch -> anchor -> owner
    anchor_owner: dict[str, str] = {}
    for obj in objects:
        owner = obj.get("instance")
        if isinstance(owner, str):
            for anchor in _anchor_ids(obj):
                anchor_owner[anchor] = owner
    latch_owner: dict[str, str] = {}
    for obj in objects:
        instance = obj.get("instance")
        if isinstance(obj.get("id"), str) and obj["id"].endswith("_anchor") and instance in anchor_owner:
            for latch in obj.get("latches", []) or []:
                if isinstance(latch, str):
                    latch_owner[latch] = anchor_owner[instance]

    def resolve(endpoint: Any) -> str:
        if isinstance(endpoint, str):
            if endpoint in latch_owner:
                return latch_owner[endpoint]
            if endpoint in anchor_owner:
                return anchor_owner[endpoint]
        raise fail("unresolved_line_endpoint")

    lines: list[tuple[str, str]] = []
    for obj in objects:
        if obj.get("id") == "dynamic_line":
            lines.append((resolve(obj.get("source")), resolve(obj.get("target"))))

    # Domain objects, in file order.
    built: dict[str, AttackFlowObject] = {}
    for obj in objects:
        kind = obj.get("id")
        instance = obj.get("instance")
        if not isinstance(kind, str) or not isinstance(instance, str):
            continue
        props = _props(obj)
        if kind in _DOMAIN_TYPES:
            stix_type, operator = _DOMAIN_TYPES[kind]
        elif kind in _SUPPORT_TYPES:
            stix_type, operator = "attack-asset", None
        else:
            continue  # flow, drawing objects, notes, text blocks

        stix_id = f"{stix_type}--{_stix_uuid(instance)}"
        item = AttackFlowObject(id=stix_id, type=stix_type)  # type: ignore[arg-type]
        if stix_type == "attack-action":
            technique = _text(props.get("technique_id"))
            if technique is not None and not _TECHNIQUE_RE.match(technique):
                raise fail("invalid_technique_id")
            item.name = _text(props.get("name"))
            item.description = _text(props.get("description"))
            item.technique_id = technique
            item.tactic_id = _text(props.get("tactic_id"))
        elif stix_type == "attack-condition":
            item.description = _text(props.get("description"))
            item.pattern = _text(props.get("pattern"))
        elif stix_type == "attack-operator":
            item.operator = operator  # type: ignore[assignment]
        else:
            item.name = _text(props.get("name"))
            item.description = _text(props.get("description"))
            item.properties = {"source_type": kind}
        built[instance] = item

    # Edges: control-flow lines become effect_refs, action -> asset becomes asset_refs.
    for src, dst in lines:
        source, target = built.get(src), built.get(dst)
        if source is None or target is None:
            continue
        if source.type in CONTROL_TYPES and target.type in CONTROL_TYPES:
            if target.id not in source.effect_refs:
                source.effect_refs.append(target.id)
        elif source.type == "attack-action" and target.type == "attack-asset":
            if target.id not in source.asset_refs:
                source.asset_refs.append(target.id)

    ordered = list(built.values())
    control = [o for o in ordered if o.type in CONTROL_TYPES]
    has_incoming = {ref for o in control for ref in o.effect_refs}
    start_refs = [o.id for o in control if o.id not in has_incoming]
    if not start_refs and control:
        # Pure cycle: fall back to the first action (creation order), else first control object.
        first = next((o for o in control if o.type == "attack-action"), control[0])
        start_refs = [first.id]

    return AttackFlow(
        flow_id=f"attack-flow--{_stix_uuid(flow_raw['instance'])}",
        name=flow_name,
        description=_text(flow_props.get("description")) or "",
        scope=_text(flow_props.get("scope")) or "",
        start_refs=start_refs,
        objects=ordered,
    )
