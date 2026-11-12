"""Flat attack flow view models (nodes/edges/Mermaid) still used by the agents."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

class FlowNodeView(BaseModel):
    """One node of the attack flow as shown in the ``GET /api/campaigns/{campaign}/flows/{flow_id}`` payload.

    Mirrors the ``nodes`` entries of the design JSON: the structural node's
    ``node_id`` plus its ATT&CK technique identity (``technique_id``,
    ``technique_name``, ``tactic``).
    """

    node_id: str
    technique_id: Optional[str] = None
    technique_name: Optional[str] = None
    tactic: Optional[str] = None


class FlowEdgeView(BaseModel):
    """One directed edge of the attack flow (``edges`` entry of the design JSON).

    The field names are ``source``/``target`` from the contract
    (``"edges": [{ "source": "…", "target": "…" }]``): ``source`` is the source
    node_id, ``target`` the target node_id.
    """

    source: str
    target: str


class FlowView(BaseModel):
    """The attack flow view returned by ``GET /api/campaigns/{campaign}/flows/{flow_id}`` (Req. 5, 6).

    Covers the three states of the contract:

    * **Curated** valid case — ``curated=True``, ``can_advance=True``, with
      ``nodes``/``edges`` filled and ``mermaid`` assembled.
    * **Non-curated** case — "pass-through": ``curated=False``,
      ``can_advance=False``, ``unavailable=True`` and a ``message`` (no AI is
      ever invoked — Req. 5.4, 5.5, 9.1, 9.2).
    * **Curated but missing/malformed** attack flow — typed error: ``curated=True``,
      ``can_advance=False`` with an ``error`` message (Req. 5.3, 6.4).
    """

    campaign_id: str
    name: str
    curated: bool
    can_advance: bool
    nodes: list[FlowNodeView] = Field(default_factory=list)
    edges: list[FlowEdgeView] = Field(default_factory=list)
    mermaid: str = ""
    # Stub / error fields (populated only in the non-curated or error states).
    unavailable: bool = False
    message: Optional[str] = None
    error: Optional[str] = None
