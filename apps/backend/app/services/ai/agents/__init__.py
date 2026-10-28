"""Stage agents and the evaluator node for the AI curation loop.

Will host the seam implementations ``AiDagGenerator`` (``dag_agent.py``),
``AiOperationalizer`` (``op_agent.py``) and ``AiEnvironmentProvisioner``
(``env_agent.py``), plus the evaluator node (``evaluator.py``), populated by
later tasks.

Submodules are imported on demand; this package ``__init__`` performs no I/O and
has no import-time side effects (Requirement 1.7).
"""
