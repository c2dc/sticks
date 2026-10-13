"""DAG services for the explicit DAG Generation/Visualization phase.

``DagService`` (task 7.1 skeleton) loads a curated case's structural graph from
``data/dag/{slug}_dag.json``, assembles an acyclic Mermaid ``flowchart`` and
distinguishes a curated case from the non-curated "passa-direto" stub. It powers
``GET /api/casos/{caso}/dag`` (Req. 5, 6).

Path resolution and typed parsing (``DagData``/``DagNode``) are reused from
:class:`~app.services.case_service.CaseService`; the curated slugs come from
``CaseService.CURATED_CASES`` (no slug is hardcoded). Behaviour is implemented
across tasks 7.2 (``montar_mermaid``) and 7.3 (``obter_dag``).

This package exposes the typed pt-BR response models (``DagView``,
``DagNoView``, ``DagArestaView``) and the service itself, plus the generation
seam: the ``GeradorDeDag`` Protocol and the ``DagProduzido`` DTO (task 4.1),
which let an AI generator be plugged in later without redesign (Req. 5.1, 5.3,
9.3).
"""

from app.services.dag.dag_service import (
    DagArestaView,
    DagNoView,
    DagService,
    DagView,
)
from app.services.dag.gerador import (
    CuradoriaGeradorDeDag,
    DagProduzido,
    GeradorDeDag,
)

__all__ = [
    "DagService",
    # Response models (pt-BR contracts)
    "DagView",
    "DagNoView",
    "DagArestaView",
    # Generation seam (task 4.1)
    "GeradorDeDag",
    "DagProduzido",
    # Curation generator (task 4.2)
    "CuradoriaGeradorDeDag",
]
