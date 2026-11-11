"""Property test — loop termination + terminal experience write (task 17.2).

# Feature: stix-db-single-source-ai-pipeline, Property 27: O loop termina em
# sucesso ou no guard, é limitado por Max_Iterations_Guard, e todo run terminal
# grava a experiência no Vector_Store

Validates: Requirements 15.7, 15.8, 6.5, 15.9

Esta é uma property de *feature* (o spec ``stix-db-single-source-ai-pipeline`` é
feature, não bugfix): a invariante deve **valer** contra a implementação atual.

Property 27 (funde a antiga Property 28)
----------------------------------------
*Para qualquer* ``Max_Iterations_Guard`` válido e *qualquer* sequência de
vereditos por iteração, o laço de melhoria do avaliador:

* **termina** — ou no primeiro veredito de sucesso, ou no guard de iterações
  máximas (nunca roda para sempre), realizando ``1..max_iterations`` passagens
  (Req. 15.7, 15.8);
* **é limitado** pelo ``Max_Iterations_Guard`` — o número de iterações executadas
  nunca excede ``max_iterations`` (a antiga Property 28);
* mapeia para um status terminal determinístico — ``COMPLETED`` quando o veredito
  final passa, senão ``ABORTED`` com ``iterations_used <= max_iterations``
  (Req. 15.7, 15.8);
* **grava exatamente uma** experiência nova no ``Vector_Store``
  (``experience_memory``) em AMBOS os casos terminais (sucesso e esgotamento),
  para que runs futuros aprendam por similaridade (Req. 6.5, 15.9).

Como a property é dirigida de forma honesta
-------------------------------------------
Três peças REAIS são exercidas por exemplo, sem rede/Postgres/SEPINF:

1. **Terminação + limite.** O laço é simulado em volta do roteador REAL
   :func:`app.services.ai.graph.route_after_eval` (a mesma fiação que o
   ``StateGraph`` registra depois de ``evaluate``), avançando o contador
   ``iteration`` 0-based exatamente como o grafo faz. O roteador é a única
   autoridade sobre quando o laço termina; o teste deriva ``passes`` dele e afirma
   ``1 <= passes <= max_iterations``.

2. **Status terminal (ex-Property 28).** O ``AgentRunService`` REAL é dirigido com
   um grafo dublê cujo estado final reflete o veredito/iteração terminais da
   simulação; o serviço marca ``COMPLETED``/``ABORTED`` e deriva
   ``iterations_used`` pela sua própria lógica (não confiamos no dublê para
   decidir o status esperado). A persistência cai numa ``Session`` SQLite efêmera
   construída do ORM.

3. **Gravação da experiência.** O nó REAL do avaliador
   (:func:`app.services.ai.agents.evaluator.node`) é executado sobre um relatório
   terminal que reflete o veredito final, com um :class:`ExperienceMemory` REAL
   (mesma ``Session`` SQLite efêmera + um ``_FakeEmbedder`` determinístico). O
   teste conta as linhas de ``experience_memory`` antes e depois e afirma que
   exatamente uma linha nova foi gravada — o resultado terminal foi para o
   ``Vector_Store`` (Req. 6.5, 15.9).

Isolamento / dublês
-------------------
Cada exemplo usa uma ``Session`` SQLite efêmera (arquivo temporário +
``Base.metadata.create_all``, o mesmo padrão dos testes irmãos
``test_run_service_lifecycle``/``test_memory_rule_routing_property``). O
``_build_graph``/``_require_dependencies`` do serviço são substituídos por dublês
e a memória vetorial recebe um ``_FakeEmbedder`` determinístico (o SQLite não tem
o backend de embeddings do pgvector; o vetor é apenas armazenado na coluna
portável ``Vector``). Nenhuma rede é tocada.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Case
from app.models.domain import ExperienceMemory as ExperienceMemoryRow
from app.models.enums import AgentRunStatus
from app.services.ai.agents import evaluator
from app.services.ai.graph import route_after_eval
from app.services.ai.memory import ExperienceMemory
from app.services.ai.run_service import AgentRunService

_CAMPAIGN_ID = "campaign-loop-termination"

# The exact terminal English status strings (Req. 15.7, 15.8) — literals, so the
# assertions never trust the implementation to decide the expected value.
_COMPLETED = "completed"
_ABORTED = "aborted"


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema per example).
# Mirrors the sibling property/lifecycle tests.
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the temp directory afterwards.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="evaluator_loop_termination_prop_"))
    db_path = tmp_dir / "evaluator_loop.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)

    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    return session_factory(), db_path, engine


def _dispose(session: Session, engine: Any, db_path: Path) -> None:
    """Close the session, dispose the engine and remove the temp db file + dir."""
    session.close()
    engine.dispose()
    shutil.rmtree(db_path.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Doubles: a deterministic embedder and a scripted compiled graph.
# ---------------------------------------------------------------------------

# Fixed, small embedding width for the deterministic embedder; the store path
# only persists the vector (it never computes cosine on SQLite), so the width is
# arbitrary as long as it is non-empty and non-zero.
_EMBED_DIM = 8


class _FakeEmbedder:
    """Deterministic text->vector map standing in for the SEPINF embedder.

    Structurally satisfies the ``@runtime_checkable``
    :class:`~app.services.ai.memory.Embedder` Protocol. The mapping is a stable
    projection of a text-and-dimension hash, so the SAME text always embeds to
    the SAME non-zero vector. No network, no model weights.
    """

    def embed(self, text: str) -> list[float]:
        vec: list[float] = []
        for dim in range(_EMBED_DIM):
            component = hashlib.sha256(f"{dim}:{text}".encode("utf-8")).digest()[0]
            vec.append((component / 127.5) - 1.0)
        if all(abs(c) < 1e-12 for c in vec):
            vec[0] = 1.0
        return vec


class _FakeGraph:
    """Stand-in compiled graph exposing only ``invoke(state, config=...)``.

    Returns a controlled final ``AgentState`` dict so the ``AgentRunService``
    terminal-status derivation runs against a controlled loop outcome.
    """

    def __init__(self, final_state: dict) -> None:
        self._final_state = final_state

    def invoke(self, state: Any, config: dict | None = None) -> dict:
        return dict(self._final_state)


def _install_fake_graph(
    monkeypatch: Any, service_cls: type[AgentRunService], graph: _FakeGraph
) -> None:
    """Patch ``_build_graph``/``_require_dependencies`` so the service runs offline.

    The dependency-availability probe is a no-op (no real checkpointer/pgvector)
    and the compiled graph is the scripted ``graph``; the lifecycle under test is
    the terminal-status derivation, not the probe or the real graph.
    """
    monkeypatch.setattr(
        service_cls, "_build_graph", lambda self: graph, raising=True
    )
    monkeypatch.setattr(
        service_cls, "_require_dependencies", lambda self: None, raising=True
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A reasonable configured max_iterations range. Req. 3.6 defaults to 3, but the
# guard must terminate for ANY valid bound, so we sweep a small range including
# the degenerate bound of 1 (a single pass then stop).
_max_iterations = st.integers(min_value=1, max_value=8)

# A non-empty finite verdict stream, cycled per pass. Includes the adversarial
# all-False stream (never passes -> only the guard can stop it) and mixed/true
# streams (an early pass finishes before the guard).
_verdict_streams = st.lists(st.booleans(), min_size=1, max_size=12)


# ---------------------------------------------------------------------------
# Loop simulation around the REAL route_after_eval.
# ---------------------------------------------------------------------------


def _simulate_loop(
    max_iterations: int, verdicts: list[bool]
) -> tuple[int, bool]:
    """Drive the improvement loop around the REAL ``route_after_eval``.

    Mirrors the graph's control flow EXACTLY: ``orchestrate`` seeds
    ``iteration = 0``; each ``evaluate`` pass writes that pass's verdict into
    ``state["evaluation"]`` and asks ``route_after_eval`` for the next hop. On
    ``"finish"`` the loop ends; on a refine target the graph re-enters that stage
    and ``iteration`` advances by one before the next evaluate pass.

    Returns ``(passes, final_verdict)`` where ``passes`` is the number of evaluate
    passes performed (1-based iteration count) and ``final_verdict`` is the
    verdict of the finishing pass.
    """
    iteration = 0
    passes = 0
    final_verdict = False
    refine_stage = "operationalize"

    # Hard safety bound: the loop must terminate within max_iterations passes, so
    # a bound of max_iterations + 1 can never be reached if the guard holds. If it
    # is ever reached the guard failed to terminate; the property asserts on
    # ``passes`` below, so this only protects against an infinite loop.
    safety_bound = max_iterations + 1

    while True:
        verdict = verdicts[passes % len(verdicts)]
        state: dict[str, Any] = {
            "campaign_id": _CAMPAIGN_ID,
            "iteration": iteration,
            "max_iterations": max_iterations,
            "evaluation": {"verdict_success": verdict},
            "refine_stage": refine_stage,
        }
        passes += 1
        final_verdict = verdict

        hop = route_after_eval(state)
        if hop == "finish":
            break

        # A refine hop re-enters the chosen stage; the router must return exactly
        # the refine target it was given.
        assert hop == refine_stage, (
            f"non-finish hop must be the refine target {refine_stage!r}, got {hop!r}"
        )
        iteration += 1

        assert passes <= safety_bound, (
            "the guard failed to terminate the loop within max_iterations"
        )

    return passes, final_verdict


def _terminal_final_state(
    passes: int, final_verdict: bool, max_iterations: int
) -> dict:
    """Build the final ``AgentState`` the service reads after the loop terminates.

    The graph's ``iteration`` counter is the 0-based index of the LAST pass
    evaluated, i.e. ``passes - 1`` (see ``route_after_eval``'s guard semantics).
    The evaluation carries the finishing pass's verdict so the service derives
    ``COMPLETED``/``ABORTED`` exactly as production does.
    """
    return {
        "campaign_id": _CAMPAIGN_ID,
        "iteration": passes - 1,
        "max_iterations": max_iterations,
        "evaluation": {"verdict_success": final_verdict},
        # No artifacts: the terminal persistence writes only the iteration row +
        # its evaluation, which is enough to exercise the COMPLETED/ABORTED path.
        "artifacts": {},
        "history": [],
    }


def _terminal_report(final_verdict: bool) -> dict:
    """A Stage 6 report reflecting the finishing pass's verdict.

    A passing verdict carries one success and no failures (the evaluator reads it
    as a FINISH); a failing verdict carries a failure so the evaluator reads it as
    a REFINE outcome. Either way the finalized outcome is stored back into the
    Vector_Store by the evaluator node (design §Evaluator node, Req. 6.5, 15.9).
    """
    if final_verdict:
        return {
            "outcome": "concluida",
            "verdict_success": True,
            "total_success": 1,
            "total_failure": 0,
            "ability_results": [{"ability_id": "A1", "status": "success"}],
        }
    return {
        "outcome": "concluida",
        "verdict_success": False,
        "total_success": 0,
        "total_failure": 1,
        "ability_results": [{"ability_id": "A1", "status": "failure"}],
    }


# ===========================================================================
# Property 27 — loop termination bounded by the guard + terminal experience
# write, with COMPLETED/ABORTED mapping (fuses the old Property 28).
#
# ``deadline=None``: each example builds and tears down a fresh temp-file SQLite
# database, whose file I/O timing varies on Windows and can exceed Hypothesis's
# default per-example deadline. The per-example isolation is intentional, so the
# deadline is lifted rather than weakened.
# ===========================================================================
# Feature: stix-db-single-source-ai-pipeline, Property 27: O loop termina em sucesso ou no guard, é limitado por Max_Iterations_Guard, e todo run terminal grava a experiência no Vector_Store
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(max_iterations=_max_iterations, verdicts=_verdict_streams)
def test_property27_loop_terminates_bounded_and_writes_experience(
    max_iterations: int,
    verdicts: list[bool],
    monkeypatch: Any,
) -> None:
    """Loop terminates within the guard; status maps correctly; one experience row.

    # Feature: stix-db-single-source-ai-pipeline, Property 27: O loop termina em sucesso ou no guard, é limitado por Max_Iterations_Guard, e todo run terminal grava a experiência no Vector_Store

    Validates: Requirements 15.7, 15.8, 6.5, 15.9
    """
    # --- (1) Termination + boundedness around the REAL router (Req. 15.7/15.8) ---
    passes, final_verdict = _simulate_loop(max_iterations, verdicts)

    # The loop performed at least one and at most max_iterations passes: the guard
    # bounds the loop and it never runs forever (the ex-Property 28 boundedness).
    assert 1 <= passes <= max_iterations, (
        f"performed {passes} passes, expected within 1..{max_iterations}"
    )

    # Independently reconstruct why the loop stopped (passing verdict vs guard) so
    # the termination reason is asserted, not trusted.
    candidate_verdicts = [verdicts[i % len(verdicts)] for i in range(max_iterations)]
    first_pass = next((i for i, v in enumerate(candidate_verdicts) if v), None)
    if first_pass is not None:
        # A verdict passes within the bound -> finish on exactly that pass.
        assert passes == first_pass + 1, (
            "a passing verdict must finish the loop on exactly that pass"
        )
        assert final_verdict is True
    else:
        # No verdict passes within the bound -> only the guard stops the loop, at
        # exactly max_iterations passes (Req. 15.8, the upper bound).
        assert passes == max_iterations, (
            "an all-failing stream must be stopped by the guard at max_iterations"
        )
        assert final_verdict is False

    session, db_path, engine = _build_ephemeral_session()
    try:
        # The experience store's non-null campaign FK (SQLite does not enforce it,
        # but we create the anchor so the row is faithful to production).
        session.add(Case(id=_CAMPAIGN_ID, name="Loop termination campaign"))
        session.flush()

        # --- (2) Terminal status mapping via the REAL service (ex-Property 28) ---
        final_state = _terminal_final_state(passes, final_verdict, max_iterations)
        _install_fake_graph(monkeypatch, AgentRunService, _FakeGraph(final_state))

        run = AgentRunService(session).start(
            _CAMPAIGN_ID, max_iterations=max_iterations
        )

        if final_verdict:
            # Passing verdict -> COMPLETED (Req. 15.7).
            assert run.status is AgentRunStatus.COMPLETED
            assert run.status.value == _COMPLETED
            assert run.final_verdict_success is True
        else:
            # Guard exhaustion without a passing verdict -> ABORTED (Req. 15.8).
            assert run.status is AgentRunStatus.ABORTED
            assert run.status.value == _ABORTED
            assert run.final_verdict_success is False

        # iterations_used never exceeds the guard and equals the passes performed
        # (clamped to 1..max_iterations) — the boundedness carried to the summary.
        assert run.iterations_used == passes
        assert 1 <= run.iterations_used <= max_iterations

        # --- (3) Terminal experience write to the Vector_Store (Req. 6.5, 15.9) --
        # Count experience_memory rows before the evaluator stores the terminal
        # outcome, then run the REAL evaluator node over the terminal report with
        # a REAL ExperienceMemory, and assert exactly one NEW row was written in
        # BOTH terminal cases (success and guard exhaustion).
        before = session.execute(
            select(func.count()).select_from(ExperienceMemoryRow)
        ).scalar_one()

        memory = ExperienceMemory(session, embedder=_FakeEmbedder())
        evaluator.node(
            {
                "campaign_id": _CAMPAIGN_ID,
                "report": _terminal_report(final_verdict),
                "retrieved_experience": [],
            },
            memory=memory,
        )
        session.flush()

        after = session.execute(
            select(func.count()).select_from(ExperienceMemoryRow)
        ).scalar_one()

        assert after - before == 1, (
            "every terminal run must write exactly one new experience row to the "
            f"Vector_Store, but {after - before} were written "
            f"(final_verdict={final_verdict})"
        )

        # The stored row carries the correct outcome (sucesso/falha) and a
        # non-null embedding — the terminal experience is retrievable by
        # similarity for future runs (Req. 6.5, 15.9).
        stored = session.execute(
            select(ExperienceMemoryRow)
            .order_by(ExperienceMemoryRow.id.desc())
            .limit(1)
        ).scalar_one()
        assert stored.campaign_id == _CAMPAIGN_ID
        assert stored.outcome == ("success" if final_verdict else "failure")
        assert stored.embedding is not None
        assert len(list(stored.embedding)) == _EMBED_DIM
    finally:
        _dispose(session, engine, db_path)
