"""AI curation agents package.

Hosts the LangGraph-based agent system that plugs into the existing six-stage
pipeline seams (``FlowGenerator``, ``OperationalizerByOrigin``,
``EnvironmentProvisioner``) with ``origin = AI_GENERATED`` (``"gerado_ia"``),
closes a bounded plan->execute->measure->improve loop over Stage 6, and persists
its bookkeeping plus a pgvector experience memory in the existing PostgreSQL.

Package layout (populated incrementally by later tasks)::

    config.py        SEPINF settings + lazy LangChain client factory
    llm.py           ChatOpenAI wiring (OpenAI-compatible, SEPINF)
    state.py         TypedDict state schema + reducers
    graph.py         build_graph(): StateGraph, nodes, conditional edge, guard
    checkpointer.py  PostgresSaver factory (LangGraph <-> PostgreSQL)
    memory.py        pgvector experience store: embed, upsert, similarity search
    tools.py         tool wrappers over existing backend services
    agents/          FlowAgent / AiOperationalizer / AiEnvironmentProvisioner
                     + evaluator node
    run_service.py   AgentRunService (Session-injected orchestration + persistence)

Import-time side effects are intentionally avoided across the whole package:
nothing here opens network sockets, spawns subprocesses, or connects to the
database. Clients, checkpointers and the vector store are built lazily on first
use (Requirement 1.7). Submodules are therefore imported on demand rather than
eagerly re-exported from this ``__init__``.
"""
