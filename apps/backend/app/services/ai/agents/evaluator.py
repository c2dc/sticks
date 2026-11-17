"""Evaluator node — closes the plan→execute→measure→improve loop (task 13.2).

This module hosts the LangGraph ``node(state) -> dict`` that reads the Execution phase
report produced by the execute node (:func:`app.services.ai.graph.execute_node`),
scores it against the existing **procedural-sufficiency metric**, and decides
whether to ``FINISH`` the run or ``REFINE`` a phase and loop again.

What the node does (Req. 1.5, 1.6, 8.6, 10.1-10.6):

* Reads ``state["report"]`` — the serialized Execution phase
  :class:`~app.services.operation.runner.OperationRunResult`.
* Extracts the procedural-sufficiency metric and the ``verdict_success`` flag
  **unchanged** and records them as the iteration's score (Req. 10.1, 10.5): the
  evaluator never recomputes or rescales the Execution phase numbers, it only reads them.
* Decides :attr:`EvaluatorDecision.FINISH` when ``verdict_success`` is true, else
  :attr:`EvaluatorDecision.REFINE` (Req. 10.2, 10.3). The max-iteration *guard*
  that turns a would-be REFINE into a FINISH/ABORTED when the bound is hit lives
  in ``route_after_eval`` (task 14.3): the evaluator always decides REFINE on a
  failing verdict and the router enforces the bound (Req. 10.4).
* Diagnoses which phase to re-enter (``refine_phase``) from the report's failed
  nodes and the retrieved cross-campaign experience.
* Persists the LLM ``rationale`` bounded to 10,000 characters (Req. 11.8) and
  stores the finalized outcome back into the long-term RAG memory via
  :meth:`app.services.ai.memory.ExperienceMemory.store_outcome` (design §Evaluator
  node).

Failed / blocked iterations (Req. 1.6, 8.6, 10.6):

A missing/unreadable report **or** a safety-gate rejection (an external
destination refused by containment, a non-isolated container, a missing
confirmation, or a gate that returned no decision) is treated as a *failed
iteration eligible for refinement*: the node records an ``error`` indication,
decides ``REFINE``, and does **not** persist a procedural-sufficiency score for
that iteration (``success_rate``/``procedural_coverage`` are left out of the
evaluation). Crucially it does **not** overwrite the existing run state — the
returned delta carries only ``evaluation``, ``refine_phase`` and ``error``; the
``report`` and every prior artifact in ``state`` survive the merge.

Persistence is **factored out** of the node (Req. 11.5/11.6): the node only reads
the report and writes the evaluation into the state; the
:func:`persist_evaluation` helper is the Run_Service hook (tasks 15.x) that writes
the ``agent_evaluations`` row within the service's atomic per-iteration
transaction. Keeping it separate leaves the node free of any DB session (and of
import-time DB deps) while the Run_Service owns the transaction boundary; this
task does **not** implement ``AgentRunService``.

Laziness (Req. 1.7): the enums, the ORM models and the RAG memory are all
resolved lazily inside the functions, so importing this module opens no socket,
spawns no subprocess and touches no database.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from app.services.ai.memory import ExperienceMemory
    from app.services.ai.state import AgentState

__all__ = ["node", "persist_evaluation"]


# Maximum length of the persisted evaluator rationale (Req. 11.8): the LLM
# reasoning text is bounded to 10,000 characters, matching the DB CHECK on
# ``agent_evaluations.rationale`` (``_RATIONALE_MAX_LENGTH`` in models.domain).
_RATIONALE_MAX_LENGTH = 10_000

# The Execution phase runner outcomes that mean the safety machinery rejected the
# proposal (or the run never executed): each is a failed/blocked iteration the
# evaluator must treat as eligible for refinement (Req. 8.6, 10.6). The values
# mirror ``app.services.operation.runner.RunOutcome``; kept as literals here so
# importing this module needs no runner import (Req. 1.7).
_GATE_REJECTION_OUTCOMES = frozenset(
    {
        "nao_confirmada",  # NOT_CONFIRMED — missing explicit confirmation (Req. 8.7)
        "contencao_recusada",  # CONTAINMENT_REFUSED — external destination (Req. 8.2)
        "isolamento_falhou",  # ISOLATION_FAILED — non-isolated container (Req. 8.3)
        "isolamento_quebrado",  # ISOLATION_BROKEN — isolation stopped passing
        "caldera_indisponivel",  # CALDERA_UNAVAILABLE — gate returned no decision
    }
)

# The runner outcome that means the Operation actually ran to completion; only
# then is the Execution phase procedural-sufficiency metric meaningful as a score.
_COMPLETED_OUTCOME = "concluida"


def node(
    state: AgentState,
    *,
    memory: ExperienceMemory | None = None,
) -> dict:
    """LangGraph node: receive the execution result, decide REFINE vs FINISH.

    This is the ``evaluation`` phase — a distinct, ordered step that runs AFTER
    the ``execution`` phase and receives the real execution result as its input
    (task 4.1, Req. 2.1, 2.2). It reads ``state["execution_result"]`` — the
    runner verdict and the real containment/isolation state the ``execution``
    phase produced — and renders the verdict from it, decoupled from the execute
    step. For the procedural-sufficiency score it still reads ``state["report"]``
    (the serialized Execution phase result) **unchanged** (Req. 10.1). When no execution
    result was carried (an older checkpoint, or a report-only caller/test) the
    verdict is derived from the report, keeping the contract backward-compatible.

    Decides :attr:`EvaluatorDecision.FINISH` on a passing verdict
    else :attr:`EvaluatorDecision.REFINE` (Req. 10.2, 10.3), diagnoses the
    refine-target phase, persists the rationale (≤10,000 chars) and stores the
    outcome back into the RAG memory (design §Evaluator node). The max-iteration
    guard is enforced downstream in ``route_after_eval`` (task 14.3), so the node
    decides REFINE on failure and leaves the bound to the router (Req. 10.4).

    A gate rejection or a missing/unreadable report is a failed iteration
    eligible for refinement (Req. 1.6, 8.6, 10.6): the node decides REFINE,
    records an ``error`` indication, does **not** persist a
    procedural-sufficiency score for the iteration, and does **not** overwrite the
    existing run state (the returned delta touches only ``evaluation``,
    ``refine_phase`` and ``error``).

    Args:
        state: The shared :class:`~app.services.ai.state.AgentState`; the
            structured execution result is read from ``state["execution_result"]``
            (the ``execution`` phase's verdict + containment/isolation state), the
            Execution phase report from ``state["report"]`` (for the unchanged metric),
            and the retrieved cross-campaign experience from
            ``state["retrieved_experience"]``.
        memory: Optional :class:`~app.services.ai.memory.ExperienceMemory` used to
            store the finalized outcome for cross-campaign RAG learning. Injected
            by tests/callers; when ``None`` it is built lazily from a
            ``state["_session"]`` if present, else the RAG store is skipped (the
            decision itself never depends on the store being reachable — Req.
            5.7).

    Returns:
        A state delta. On a readable, executed report::

            {"evaluation": {...metrics + decision + rationale...},
             "refine_phase": <AgentPhase value | None>}

        On a gate rejection or a missing/unreadable report the delta additionally
        carries ``"error"`` and the evaluation omits the procedural-sufficiency
        score (Req. 10.6).
    """
    from app.models.enums import EvaluatorDecision

    report = state.get("report")
    experience = state.get("retrieved_experience") or []

    # The structured execution result the ``execution`` phase produced (task
    # 4.1, Req. 2.1, 2.2): the runner verdict and the real containment/isolation
    # state, decoupled from the execute step. ``evaluation`` receives it as its
    # input and renders the verdict from it; when it is absent (an older
    # checkpoint, or a report-only test) the evaluator falls back to reading the
    # report directly, so the contract stays backward-compatible.
    execution_result = state.get("execution_result")

    # --- failed / blocked iteration paths (Req. 1.6, 8.6, 10.6) -------------
    #
    # Either the report is absent/unreadable, or the safety machinery rejected
    # the proposal. Both are failed iterations eligible for refinement: decide
    # REFINE, record an error, do NOT persist a procedural-sufficiency score, and
    # do NOT overwrite the existing run state.
    read_error = _report_read_error(report)
    if read_error is not None:
        return _refine_without_score(
            state,
            report=report,
            experience=experience,
            error=read_error,
            memory=memory,
        )

    # Prefer the execution phase's own verdict (task 4.1): a gate rejection /
    # non-executed outcome is identified from the structured execution result
    # when present, else derived from the report (backward-compatible).
    gate_cause = _gate_rejection_cause_from(execution_result, report)
    if gate_cause is not None:
        return _refine_without_score(
            state,
            report=report,
            experience=experience,
            error=gate_cause,
            memory=memory,
        )

    # --- executed iteration: score it unchanged and decide (Req. 10.1-10.5) --
    # The verdict is the one the ``execution`` phase reported (Req. 2.2); it
    # falls back to reading the report when no execution result was carried.
    verdict_success = _verdict_success_from(execution_result, report)

    # Record the procedural-sufficiency metric UNCHANGED as the iteration's
    # score (Req. 10.1, 10.5): read the Execution phase numbers, never recompute them.
    evaluation: dict[str, Any] = {
        "success_rate": _read_success_rate(report),
        "procedural_coverage": _read_procedural_coverage(report),
        "verdict_success": verdict_success,
        "failed_nodes": _read_failed_nodes(report),
    }

    if verdict_success:
        # Passing verdict → FINISH (Req. 10.2). The router then routes to END and
        # the Run_Service marks the run COMPLETED (Req. 3.2).
        evaluation["decision"] = EvaluatorDecision.FINISH.value
        evaluation["rationale"] = _bounded(_finish_rationale(report, evaluation))
        refine_phase: str | None = None
    else:
        # Failing verdict → REFINE (Req. 10.3). The bound is enforced by
        # route_after_eval (task 14.3), which turns a guard-hit REFINE into a
        # FINISH/ABORTED (Req. 10.4) — the evaluator always decides REFINE here.
        evaluation["decision"] = EvaluatorDecision.REFINE.value
        refine_phase, rationale = _diagnose_failing_phase(report, experience)
        evaluation["rationale"] = _bounded(rationale)

    # Store the finalized curation outcome (success OR failure) back into the
    # long-term RAG memory so future proposals of OTHER campaigns learn from it
    # (design §Evaluator node). Best-effort: a store failure must not break the
    # decision the node already made.
    _store_outcome(state, report, evaluation, memory=memory)

    return {"evaluation": evaluation, "refine_phase": refine_phase}


# ---------------------------------------------------------------------------
# Failed / blocked iteration handling (Req. 1.6, 8.6, 10.6)
# ---------------------------------------------------------------------------


def _refine_without_score(
    state: AgentState,
    *,
    report: Any,
    experience: list[dict[str, Any]],
    error: str,
    memory: ExperienceMemory | None,
) -> dict:
    """Build the REFINE delta for a failed/blocked iteration (Req. 1.6, 8.6, 10.6).

    Decides REFINE, records the ``error`` indication, and deliberately **omits**
    the procedural-sufficiency score from the evaluation (no ``success_rate`` /
    ``procedural_coverage`` key) because the iteration produced no meaningful
    metric (Req. 10.6). The returned delta carries only ``evaluation``,
    ``refine_phase`` and ``error`` so the existing run state (the ``report`` and
    every prior artifact) is preserved on the merge, not overwritten.
    """
    from app.models.enums import EvaluatorDecision

    refine_phase, rationale = _diagnose_failing_phase(report, experience)

    evaluation: dict[str, Any] = {
        "verdict_success": False,
        "failed_nodes": _read_failed_nodes(report) if isinstance(report, dict) else [],
        "decision": EvaluatorDecision.REFINE.value,
        "error": error,
        "rationale": _bounded(f"{error} " + rationale),
    }

    # Still record the (failed) outcome in RAG so the agent learns the proposal
    # was blocked/unreadable; best-effort and never required for the decision.
    _store_outcome(state, report, evaluation, memory=memory)

    return {
        "evaluation": evaluation,
        "refine_phase": refine_phase,
        "error": error,
    }


def _report_read_error(report: Any) -> str | None:
    """Return an error indication when the Execution phase report is absent/unreadable.

    Signals Req. 1.6 / 10.6: ``None`` means the report is a usable mapping;
    otherwise a human-readable cause identifying the missing or unreadable report
    is returned so the node can record it and refine.
    """
    if report is None:
        return "Relatório da fase execution ausente: nenhuma execução foi registrada."
    if not isinstance(report, dict):
        return (
            f"Relatório da fase execution ilegível: formato inesperado ({type(report).__name__})."
        )
    if not report:
        return "Relatório da fase execution vazio: nenhuma métrica disponível."
    return None


def _gate_rejection_cause(report: dict[str, Any]) -> str | None:
    """Identify the safety gate that rejected the proposal, if any (Req. 8.6).

    Inspects the serialized runner ``outcome`` for one of the gate-rejection /
    non-executed outcomes and returns a cause string naming the gate, else
    ``None`` when the Operation actually executed. A rejection is a failed
    iteration eligible for refinement (Req. 10.6), never a run-ending error.
    """
    outcome = report.get("outcome")
    outcome_value = getattr(outcome, "value", outcome)
    if outcome_value in _GATE_REJECTION_OUTCOMES:
        return _gate_cause_message(outcome_value, report)
    return None


def _gate_rejection_cause_from(
    execution_result: dict[str, Any] | None, report: dict[str, Any]
) -> str | None:
    """Identify a gate rejection from the execution result, else the report (task 4.1).

    When the ``execution`` phase handed the evaluator a structured execution
    result (Req. 2.2), its ``verdict`` is the authoritative runner outcome: a
    gate-rejection / non-executed verdict is read straight from it. Otherwise the
    evaluator falls back to reading the serialized report (backward-compatible).
    The human-readable cause is still built from the report so it carries the
    runner's message when present.
    """
    if isinstance(execution_result, dict) and execution_result:
        verdict = execution_result.get("verdict")
        if verdict in _GATE_REJECTION_OUTCOMES:
            return _gate_cause_message(verdict, report)
        # An executed verdict from the execution phase overrides any stale
        # report outcome: the Operation ran, so there is no gate rejection.
        if verdict is not None:
            return None
    return _gate_rejection_cause(report)


def _verdict_success_from(execution_result: dict[str, Any] | None, report: dict[str, Any]) -> bool:
    """Read the passing verdict from the execution result, else the report (task 4.1).

    The ``execution`` phase computes ``verdict_success`` from the live runner
    result (Req. 2.2), so the ``evaluation`` phase consumes it directly when
    present. When no execution result was carried (older checkpoint, report-only
    test) the verdict is derived from the report unchanged, preserving the prior
    behaviour.
    """
    if isinstance(execution_result, dict) and "verdict_success" in execution_result:
        return bool(execution_result["verdict_success"])
    return _read_verdict_success(report)


def _gate_cause_message(outcome_value: str, report: dict[str, Any]) -> str:
    """Map a gate-rejection outcome to a human-readable cause for the evaluation."""
    mapping = {
        "nao_confirmada": (
            "Proposta bloqueada pela confirmação explícita: execução não confirmada."
        ),
        "contencao_recusada": (
            "Proposta rejeitada pela verificação de contenção: destino externo detectado."
        ),
        "isolamento_falhou": (
            "Proposta rejeitada pela verificação de isolamento: contêiner não isolado."
        ),
        "isolamento_quebrado": (
            "Execução interrompida: o isolamento do contêiner deixou de ser garantido."
        ),
        "caldera_indisponivel": (
            "Proposta bloqueada: a máquina de segurança não retornou uma decisão "
            "(Caldera indisponível)."
        ),
    }
    base = mapping.get(outcome_value, f"Proposta bloqueada (outcome={outcome_value}).")
    message = report.get("message")
    if isinstance(message, str) and message.strip():
        return f"{base} {message.strip()}"
    return base


# ---------------------------------------------------------------------------
# Metric extraction — read the Execution phase numbers UNCHANGED (Req. 10.1, 10.5)
# ---------------------------------------------------------------------------
#
# The design sketch reads ``report["metrics"][...]``. The real Execution phase report is
# a serialized ``OperationRunResult`` (``outcome``/``state``/``total_success``/
# ``total_failure``/``ability_results``/...), which has no ``metrics`` sub-dict.
# These readers therefore honour an explicit ``metrics`` block when present (the
# idealized/fake shape used by tests), and otherwise derive the same values from
# the real runner result — in both cases reading the Execution phase output *unchanged*
# (never rescaling or recomputing the underlying counts).


def _metrics(report: dict[str, Any]) -> dict[str, Any]:
    """Return the report's ``metrics`` sub-dict when present, else an empty dict."""
    metrics = report.get("metrics")
    return metrics if isinstance(metrics, dict) else {}


def _read_verdict_success(report: dict[str, Any]) -> bool:
    """Read ``verdict_success`` unchanged from the report (Req. 10.1, 10.2).

    Honours an explicit ``verdict_success`` (top-level or inside ``metrics``);
    otherwise derives it from the real runner result: a run passes only when it
    ran to completion (``outcome == "concluida"``) with at least one success and
    no failures.
    """
    metrics = _metrics(report)
    for source in (report, metrics):
        if "verdict_success" in source:
            return bool(source["verdict_success"])
        if "success_verdict" in source:
            return bool(source["success_verdict"])

    outcome = getattr(report.get("outcome"), "value", report.get("outcome"))
    if outcome != _COMPLETED_OUTCOME:
        return False
    total_success = _as_int(report.get("total_success"))
    total_failure = _as_int(report.get("total_failure"))
    return total_success > 0 and total_failure == 0


def _read_success_rate(report: dict[str, Any]) -> float:
    """Read the success rate unchanged, or derive it from the runner totals.

    Prefers an explicit ``success_rate`` (top-level or in ``metrics``); otherwise
    computes ``total_success / (total_success + total_failure)`` from the real
    runner aggregate (0.0 when nothing executed).
    """
    metrics = _metrics(report)
    for source in (report, metrics):
        if "success_rate" in source:
            return _as_float(source["success_rate"])

    total_success = _as_int(report.get("total_success"))
    total_failure = _as_int(report.get("total_failure"))
    total = total_success + total_failure
    if total <= 0:
        return 0.0
    return total_success / total


def _read_procedural_coverage(report: dict[str, Any]) -> float:
    """Read the procedural-sufficiency coverage unchanged, or derive it.

    Prefers an explicit ``procedural_coverage`` (top-level or in ``metrics``);
    otherwise falls back to the derived success rate as the best available
    procedural-sufficiency proxy over the executed chain.
    """
    metrics = _metrics(report)
    for source in (report, metrics):
        if "procedural_coverage" in source:
            return _as_float(source["procedural_coverage"])
    return _read_success_rate(report)


def _read_failed_nodes(report: dict[str, Any]) -> list[str]:
    """Read the failed-node identifiers unchanged, or derive them.

    Prefers an explicit ``failed_nodes`` (top-level or in ``metrics``); otherwise
    scans the serialized ``ability_results`` and collects the id/name of every
    ability whose status is ``failure`` (the terminal
    ``AbilityResultStatus.FAILURE`` value). The list drives the refine-target
    diagnosis.
    """
    metrics = _metrics(report)
    for source in (report, metrics):
        if "failed_nodes" in source and isinstance(source["failed_nodes"], list):
            return [str(node) for node in source["failed_nodes"]]

    # Imported lazily so importing this module needs no enum dep (Req. 1.7).
    from app.models.enums import AbilityResultStatus

    failed: list[str] = []
    for result in report.get("ability_results") or []:
        if not isinstance(result, dict):
            continue
        status = getattr(result.get("status"), "value", result.get("status"))
        if status == AbilityResultStatus.FAILURE.value:
            identifier = result.get("ability_id") or result.get("node_id") or result.get("name")
            if identifier is not None:
                failed.append(str(identifier))
    return failed


# ---------------------------------------------------------------------------
# Refine-target diagnosis + rationale
# ---------------------------------------------------------------------------


def _diagnose_failing_phase(report: Any, experience: list[dict[str, Any]]) -> tuple[str, str]:
    """Diagnose which curated phase to re-enter and explain why (design §Evaluator).

    Returns ``(refine_phase, rationale)`` where ``refine_phase`` is an
    :class:`~app.models.enums.AgentPhase` *value* (``flow`` /
    ``operationalization`` / ``environment``). The heuristic keeps
    the loop focused:

    * a gate rejection / non-executed outcome points at the environment phase
      (isolation) or the operationalization phase (containment/confirmation),
      since those are what the gate judged;
    * otherwise failed executed nodes point at the operationalization phase (the
      command semantics that did not achieve the technique);
    * with no signal at all it defaults to operationalization — the
      command-semantics core, the most common refine target.

    The rationale is a short, auditable explanation that cites the metrics and,
    when available, how many cross-campaign experience snippets primed the pass.
    """
    from app.models.enums import AgentPhase

    report_dict = report if isinstance(report, dict) else {}
    outcome = getattr(report_dict.get("outcome"), "value", report_dict.get("outcome"))
    failed_nodes = _read_failed_nodes(report_dict) if report_dict else []
    experience_count = len(experience or [])

    if outcome in {"isolamento_falhou", "isolamento_quebrado"}:
        phase = AgentPhase.ENVIRONMENT
        why = (
            "o gate de isolamento rejeitou o ambiente proposto; refinar o "
            "fase environment para garantir contêineres isolados."
        )
    elif outcome in {"contencao_recusada", "nao_confirmada", "caldera_indisponivel"}:
        phase = AgentPhase.OPERATIONALIZATION
        why = (
            "a proposta foi bloqueada antes da execução (contenção/confirmação); "
            "refinar a fase operationalization para propor comandos "
            "contidos e executáveis."
        )
    elif failed_nodes:
        phase = AgentPhase.OPERATIONALIZATION
        why = (
            "nós executados falharam em cumprir a técnica "
            f"({', '.join(failed_nodes[:5])}); refinar a fase operationalization "
            "(operacionalização) para ajustar a semântica dos comandos."
        )
    else:
        phase = AgentPhase.OPERATIONALIZATION
        why = (
            "veredito de insucesso sem nó falho identificado; refinar o "
            "fase operationalization como alvo padrão."
        )

    rationale = (
        f"Decisão REFINAR. Alvo: {phase.value}. Motivo: {why} "
        f"Experiência cruzada utilizada: {experience_count} registro(s)."
    )
    return phase.value, rationale


def _finish_rationale(report: dict[str, Any], evaluation: dict[str, Any]) -> str:
    """Build the auditable rationale for a FINISH decision (passing verdict)."""
    return (
        "Decisão FINALIZAR. Veredito de sucesso na fase execution: taxa de sucesso "
        f"{evaluation.get('success_rate')}, cobertura procedural "
        f"{evaluation.get('procedural_coverage')}. A curadoria gerada atende à "
        "métrica de suficiência procedural; encerrando o laço."
    )


# ---------------------------------------------------------------------------
# RAG store-back (design §Evaluator node) — best-effort
# ---------------------------------------------------------------------------


def _store_outcome(
    state: AgentState,
    report: Any,
    evaluation: dict[str, Any],
    *,
    memory: ExperienceMemory | None,
) -> None:
    """Store the finalized outcome back into the long-term RAG memory.

    Resolves an :class:`~app.services.ai.memory.ExperienceMemory` from the
    injected ``memory`` or, lazily, from a ``state["_session"]`` if one is
    present; when neither is available the store is simply skipped. The call is
    **best-effort** (the memory already retries and preserves the record on
    failure — Req. 5.2): a store failure here is swallowed so the evaluator's
    decision, which the loop depends on, is never undone by a RAG-write problem.
    """
    campaign_id = state.get("campaign_id")
    if not campaign_id:
        return

    store = memory
    if store is None:
        session = state.get("_session")
        if session is None:
            return
        try:
            from app.services.ai.memory import ExperienceMemory

            store = ExperienceMemory(session)
        except Exception:  # noqa: BLE001 -- RAG store-back is best-effort
            return

    report_dict = report if isinstance(report, dict) else {}
    # Never undo the decision on a store failure.
    with contextlib.suppress(Exception):
        store.store_outcome(campaign_id, report_dict, evaluation)


# ---------------------------------------------------------------------------
# Run_Service persistence hook (task 15.x) — factored out of the node (Req. 11.5)
# ---------------------------------------------------------------------------


def persist_evaluation(
    session: Any,
    iteration_id: int,
    evaluation: dict[str, Any],
    *,
    report_id: int | None = None,
) -> Any:
    """Run_Service hook: persist the evaluation as an ``agent_evaluations`` row.

    Factored out of :func:`node` so the ``AgentRunService`` (tasks 15.x) can drive
    it inside its atomic per-iteration transaction (Req. 11.5/11.6): the node
    stays free of any DB session, and this helper owns only the row assembly — it
    ``add``s and ``flush``es onto the **injected** ``session`` but never commits,
    leaving the transaction boundary to the caller.

    It writes one :class:`~app.models.domain.AgentEvaluation` for the iteration,
    storing the metric values the decision was derived from (``success_rate``,
    ``procedural_coverage``, ``verdict_success``, ``failed_nodes``) together with
    the ``decision`` and the ``rationale`` bounded to 10,000 characters (Req.
    11.8). For a failed/blocked iteration that carries no procedural-sufficiency
    score (Req. 10.6) the metric columns default to ``0.0`` so the non-null row
    still persists while recording no fabricated score; the absence is reflected
    by ``verdict_success=False`` and the rationale's error indication.

    Args:
        session: The injected SQLAlchemy ``Session`` (the Run_Service's unit of
            work). This helper only ``add``/``flush``es; it does not commit.
        iteration_id: The owning ``agent_iterations`` row id.
        evaluation: The evaluation dict produced by :func:`node`.
        report_id: The Execution phase ``execution_reports`` row id the evaluator read,
            when the Run_Service has persisted/resolved one.

    Returns:
        The persisted :class:`~app.models.domain.AgentEvaluation` instance
        (flushed, so its ``id`` is populated).
    """
    # Imported lazily so importing this module needs no DB/model deps (Req. 1.7).
    from app.models.domain import AgentEvaluation, _utcnow
    from app.models.enums import EvaluatorDecision

    decision = evaluation.get("decision") or EvaluatorDecision.REFINE.value

    row = AgentEvaluation(
        iteration_id=iteration_id,
        report_id=report_id,
        success_rate=_as_float(evaluation.get("success_rate")),
        procedural_coverage=_as_float(evaluation.get("procedural_coverage")),
        verdict_success=bool(evaluation.get("verdict_success")),
        failed_nodes=list(evaluation.get("failed_nodes") or []),
        decision=EvaluatorDecision(decision),
        rationale=_bounded(evaluation.get("rationale") or ""),
        created_at=_utcnow(),
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------


def _bounded(text: str) -> str:
    """Clamp ``text`` to the persisted rationale max length (Req. 11.8).

    The ``agent_evaluations.rationale`` column is non-null text bounded to 10,000
    characters by a DB CHECK; clamping here keeps the in-state rationale and the
    persisted one within that bound rather than letting an over-long LLM
    explanation break the insert.
    """
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= _RATIONALE_MAX_LENGTH:
        return text
    return text[:_RATIONALE_MAX_LENGTH]


def _as_int(value: Any) -> int:
    """Best-effort coercion of a report value to ``int`` (0 on failure)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    """Best-effort coercion of a report/evaluation value to ``float`` (0.0 on failure)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
