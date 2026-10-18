"""DAG services for the explicit DAG Generation/Visualization phase.

``DagService`` (task 7.1 skeleton) loads a curated case's structural graph from
``data/dag/{slug}_dag.json``, assembles an acyclic Mermaid ``flowchart`` and
distinguishes a curated case from the non-curated "pass-through" stub. It powers
``GET /api/campaigns/{campaign}/dag`` (Req. 5, 6).

Path resolution and typed parsing (``DagData``/``DagNode``) are reused from
:class:`~app.services.campaign_service.CaseService`; the curated slugs come from
``CaseService.CURATED_CASES`` (no slug is hardcoded). Behaviour is implemented
across tasks 7.2 (``build_mermaid``) and 7.3 (``get_dag``).

This package exposes the typed response models (``DagView``, ``DagNodeView``,
``DagEdgeView``) and the service itself, plus the generation seam: the
``DagGenerator`` Protocol and the ``ProducedDag`` DTO (task 4.1), which let an AI
generator be plugged in later without redesign (Req. 5.1, 5.3, 9.3).
"""

from app.services.dag.dag_service import (
    DagEdgeView,
    DagNodeView,
    DagService,
    DagView,
)
from app.services.dag.gerador import (
    CurationDagGenerator,
    DagGenerator,
    ProducedDag,
)

__all__ = [
    "DagService",
    # Response models
    "DagView",
    "DagNodeView",
    "DagEdgeView",
    # Generation seam (task 4.1)
    "DagGenerator",
    "ProducedDag",
    # Curation generator (task 4.2)
    "CurationDagGenerator",
]
