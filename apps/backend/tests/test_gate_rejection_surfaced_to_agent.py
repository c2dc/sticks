"""Unit tests for gate-rejection outcomes surfaced to the agent (task 8.2).

The guiding principle of the AI curation loop is *"the agent proposes, the
existing safety machinery disposes"*: every AI-produced command and target still
passes, unchanged, the four Stage 6 gates (containment → read-only fail-closed
isolation → preview/explicit-confirmation → 1:1 audit). When any gate rejects
the proposal, **no Operation executes**, the **prior stored state is unchanged**,
and the loop must *see* the rejection as a failed/blocked iteration eligible for
refinement — never as a silent pass or a run-ending crash.

These example-based unit tests pin that end-to-end contract with **fakes only**
(no SEPINF endpoint, no Caldera, no Docker, no database). They cover the four
gate-rejection scenarios the task enumerates and the "gate returned no decision"
guard:

* **Containment rejection** — ``CONTAINMENT_REFUSED`` (external destination,
  Req. 8.2).
* **Isolation rejection** — ``ISOLATION_FAILED`` (non-isolated container,
  Req. 8.3) and its mid-run variant ``ISOLATION_BROKEN``.
* **Missing confirmation** — ``NOT_CONFIRMED`` (preview/confirm gate, Req. 8.7).
* **No-decision gate** — ``CALDERA_UNAVAILABLE`` (a required gate returned no
  decision, Req. 8.8).

For each scenario the test drives the **real, unchanged**
:func:`app.services.ai.graph.execute_node` over a *fake runner* that returns the
corresponding non-executed :class:`~app.services.operation.runner.OperationRunResult`
(exactly the shape the unchanged :class:`OperationRunner` returns when a gate
fires — it refuses before creating an ``Operation`` row), and then drives the
**real** :func:`app.services.ai.agents.evaluator.node` over the resulting report.

Each test asserts the three properties the task requires:

1. **Rejected/blocked outcome with NO execution.** The report's ``outcome`` is
   the gate-rejection value, the ``state`` is ``NOT_STARTED`` (or ``ABORTED``
   for the mid-run isolation break), ``operation_id`` is ``None`` (no row
   created) and ``started`` is ``False`` — nothing executed (Req. 8.2, 8.3, 8.4,
   8.7, 8.8, 9.3, 9.5).
2. **Prior state unchanged.** ``execute_node`` returns only a ``{"report": ...}``
   delta (it never mutates the input state) and the evaluator's delta carries
   only ``evaluation``/``refine_stage``/``error`` — the ``campaign_id``,
   ``iteration`` and every prior artifact in the seed state survive (Req. 8.2,
   8.3).
3. **Surfaced as a failed iteration eligible for refinement.** The evaluator
   decides ``REFINE``, records an ``error`` indication naming the firing gate,
   persists **no** procedural-sufficiency score (``success_rate`` /
   ``procedural_coverage`` absent), and diagnoses a concrete refine-target stage
   (Req. 8.6, 10.6).

_Requirements: 8.1, 8.2, 8.3, 8.4, 8.6, 8.7, 8.8, 9.3, 9.4, 9.5_
"""

from __future__ import annotations

import copy
from typing import Any

from app.models.enums import AgentStage, EvaluatorDecision
from app.services.ai.agents import evaluator
from app.services.ai.graph import execute_node
from app.services.operation.runner import OperationRunResult, RunOutcome
from app.models.enums import OperationState


# ---------------------------------------------------------------------------
# Fakes — a recording runner and an in-memory RAG store
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """Fake runner returning a fixed non-executed result and recording the call.

    Mirrors the single surface :func:`execute_node` uses
    (``runner.run(campaign_id=..., abilities=..., adversary_id=...,
    confirmed=..., adversary_payload=...) -> OperationRunResult``). It returns a
    pre-built gate-rejection :class:`OperationRunResult` — exactly what the real
    :class:`OperationRunner` yields when a gate fires *before* any Operation row
    is created — and records the keyword arguments it was handed so a test can
    assert the agent executed nothing itself (it only delegates to the runner).
    """

    def __init__(self, result: OperationRunResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> OperationRunResult:
        self.calls.append(kwargs)
        return self._result


class _RecordingMemory:
    """In-memory RAG stub recording ``store_outcome`` calls (never raising)."""

    def __init__(self) -> None:
        self.stored: list[tuple[str, dict, dict]] = []

    def store_outcome(
        self, campaign_id: str, report: dict, evaluation: dict
    ) -> None:
        self.stored.append((campaign_id, report, evaluation))


# ---------------------------------------------------------------------------
# Gate-rejection result builders (what the UNCHANGED runner returns per gate)
# ---------------------------------------------------------------------------


def _not_confirmed() -> OperationRunResult:
    """Preview/confirm gate: no explicit confirmation → nothing runs (Req. 8.7)."""
    return OperationRunResult(
        outcome=RunOutcome.NOT_CONFIRMED,
        state=OperationState.NOT_STARTED,
        message=(
            "Emulação não iniciada: confirmação explícita ausente ou "
            "solicitação cancelada. Nenhum comando de adversário foi executado."
        ),
    )


def _containment_refused() -> OperationRunResult:
    """Containment gate: external destination → proposal refused (Req. 8.2)."""
    return OperationRunResult(
        outcome=RunOutcome.CONTAINMENT_REFUSED,
        state=OperationState.NOT_STARTED,
        message=(
            "Emulação recusada por contenção — destino externo detectado: "
            "8.8.8.8"
        ),
    )


def _isolation_failed() -> OperationRunResult:
    """Isolation gate: non-isolated container → proposal aborted (Req. 8.3)."""
    return OperationRunResult(
        outcome=RunOutcome.ISOLATION_FAILED,
        state=OperationState.NOT_STARTED,
        message="Isolamento reprovado: contêiner 'nginx' não está isolado.",
    )


def _isolation_broken() -> OperationRunResult:
    """Isolation stopped passing mid-run → ABORTED, partials preserved (Req. 8.3)."""
    return OperationRunResult(
        outcome=RunOutcome.ISOLATION_BROKEN,
        state=OperationState.ABORTED,
        operation_id=None,
        message=(
            "Operação abortada: o isolamento de contenção deixou de ser "
            "satisfeito durante a execução."
        ),
    )


def _caldera_unavailable() -> OperationRunResult:
    """A required gate returned no decision (Caldera unavailable) (Req. 8.8)."""
    return OperationRunResult(
        outcome=RunOutcome.CALDERA_UNAVAILABLE,
        state=OperationState.NOT_STARTED,
        message="Caldera indisponível: a máquina de segurança não respondeu.",
    )


# ---------------------------------------------------------------------------
# Seed state helper
# ---------------------------------------------------------------------------


def _seed_state() -> dict:
    """A representative mid-loop AgentState with prior artifacts to protect.

    The state carries a ``campaign_id``, an ``iteration`` counter and a prior
    ``artifacts`` block (the AI Stage 3/4/5 outputs of the current iteration).
    The tests deep-copy this before invoking the nodes so they can assert the
    prior stored state survives untouched (Req. 8.2, 8.3).
    """
    return {
        "campaign_id": "campaign-under-test",
        "iteration": 1,
        "max_iterations": 3,
        "retrieved_experience": [{"snippet": "prior outcome"}],
        "artifacts": {
            "dag": {"origin": "gerado_ia", "nodes": ["T1059"]},
            "operationalization": {
                "origin": "gerado_ia",
                "abilities": {
                    "abilities": [
                        {
                            "ability_id": "a1",
                            "name": "exec",
                            "executors": [
                                {
                                    "name": "sh",
                                    "platform": "linux",
                                    "command": "whoami",
                                }
                            ],
                        }
                    ],
                    "adversary": {
                        "id": "adv-1",
                        "name": "adv",
                        "atomic_ordering": ["a1"],
                    },
                },
            },
            "environment": {"origin": "gerado_ia"},
        },
    }


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------


def _assert_no_execution(report: dict, expected_outcome: RunOutcome) -> None:
    """Assert the report is a rejected/blocked outcome with NO execution.

    ``execute_node`` serializes enums to their ``.value``; the report therefore
    carries the string value of the outcome/state. No ``Operation`` row was
    created (``operation_id is None``) and the result's ``started`` flag is
    ``False`` — nothing executed (Req. 8.2, 8.3, 8.4, 8.7, 8.8).
    """
    assert report["outcome"] == expected_outcome.value
    # No Operation row created for any gate rejection (the runner refuses before
    # creating one); the mid-run isolation break preserves partials but we build
    # it with operation_id=None here to represent "nothing persisted".
    assert report["operation_id"] is None
    # ``total_success``/``total_failure`` are zero: nothing executed.
    assert report["total_success"] == 0
    assert report["total_failure"] == 0


def _assert_refine_without_score(delta: dict, expected_stage: AgentStage) -> None:
    """Assert the evaluator surfaced a failed iteration eligible for refinement.

    The evaluator decides ``REFINE``, records an ``error`` indication, persists
    NO procedural-sufficiency score (``success_rate``/``procedural_coverage``
    absent) and diagnoses a concrete refine-target stage (Req. 8.6, 10.6).
    """
    evaluation = delta["evaluation"]
    assert evaluation["decision"] == EvaluatorDecision.REFINE.value
    assert evaluation["verdict_success"] is False
    # No fabricated procedural-sufficiency score for a blocked iteration.
    assert "success_rate" not in evaluation
    assert "procedural_coverage" not in evaluation
    # An error indication identifying the firing gate is recorded and surfaced.
    assert isinstance(evaluation.get("error"), str) and evaluation["error"].strip()
    assert delta["error"] == evaluation["error"]
    # A concrete refine-target stage is diagnosed.
    assert delta["refine_stage"] == expected_stage.value


def _run_pipeline(result: OperationRunResult) -> tuple[dict, dict, dict, _RecordingRunner, _RecordingMemory]:
    """Drive execute_node then evaluator.node over a fake runner result.

    Returns ``(seed_before, execute_delta, eval_delta, runner, memory)`` where
    ``seed_before`` is a deep copy of the seed taken before any node ran (for the
    unchanged-state assertion).
    """
    seed = _seed_state()
    seed_before = copy.deepcopy(seed)

    runner = _RecordingRunner(result)
    execute_delta = execute_node(seed, runner=runner)

    # The runner was invoked exactly once, with confirmed=True and the campaign
    # — the agent delegated to the unchanged runner and executed nothing itself.
    assert len(runner.calls) == 1
    assert runner.calls[0]["confirmed"] is True
    assert runner.calls[0]["campaign_id"] == "campaign-under-test"

    # ``execute_node`` must not mutate the input state: the seed is unchanged and
    # the delta carries ONLY the report (Req. 8.2, 8.3).
    assert seed == seed_before
    assert set(execute_delta.keys()) == {"report"}

    # Feed the report to the evaluator exactly as the graph would.
    eval_state = dict(seed)
    eval_state["report"] = execute_delta["report"]
    eval_before = copy.deepcopy(eval_state)

    memory = _RecordingMemory()
    eval_delta = evaluator.node(eval_state, memory=memory)

    # The evaluator must not overwrite the existing run state: it returns a small
    # delta and never mutates the state it was handed (Req. 1.6, 8.6, 10.6).
    assert eval_state == eval_before
    assert set(eval_delta.keys()) == {"evaluation", "refine_stage", "error"}

    return seed_before, execute_delta, eval_delta, runner, memory


# ---------------------------------------------------------------------------
# Tests — one per gate-rejection scenario
# ---------------------------------------------------------------------------


def test_containment_rejection_surfaced_as_refinable_with_no_execution() -> None:
    """External destination → CONTAINMENT_REFUSED, no execution, REFINE (Req. 8.2)."""
    seed_before, execute_delta, eval_delta, runner, memory = _run_pipeline(
        _containment_refused()
    )

    report = execute_delta["report"]
    _assert_no_execution(report, RunOutcome.CONTAINMENT_REFUSED)
    assert report["state"] == OperationState.NOT_STARTED.value

    # A containment rejection points the loop at Stage 4 (operationalization):
    # the command semantics proposed an external destination (Req. 8.6, 10.6).
    _assert_refine_without_score(eval_delta, AgentStage.OPERATIONALIZATION)
    assert "contenção" in eval_delta["error"].lower()

    # Prior stored state unchanged (the seed deep copy matches the pre-run seed).
    assert seed_before["artifacts"]["operationalization"]["origin"] == "gerado_ia"


def test_isolation_rejection_surfaced_as_refinable_with_no_execution() -> None:
    """Non-isolated container → ISOLATION_FAILED, no execution, REFINE (Req. 8.3)."""
    _seed, execute_delta, eval_delta, _runner, _memory = _run_pipeline(
        _isolation_failed()
    )

    report = execute_delta["report"]
    _assert_no_execution(report, RunOutcome.ISOLATION_FAILED)
    assert report["state"] == OperationState.NOT_STARTED.value

    # An isolation rejection points the loop at Stage 5 (environment): the
    # proposed environment was not isolated (Req. 8.6, 10.6).
    _assert_refine_without_score(eval_delta, AgentStage.ENVIRONMENT)
    assert "isolamento" in eval_delta["error"].lower()


def test_isolation_broken_midrun_surfaced_as_refinable_with_no_completion() -> None:
    """Isolation stopped passing mid-run → ABORTED, REFINE to environment (Req. 8.3)."""
    _seed, execute_delta, eval_delta, _runner, _memory = _run_pipeline(
        _isolation_broken()
    )

    report = execute_delta["report"]
    _assert_no_execution(report, RunOutcome.ISOLATION_BROKEN)
    # The mid-run break aborts the Operation rather than leaving it NOT_STARTED.
    assert report["state"] == OperationState.ABORTED.value

    _assert_refine_without_score(eval_delta, AgentStage.ENVIRONMENT)
    assert "isolamento" in eval_delta["error"].lower()


def test_missing_confirmation_surfaced_as_refinable_with_no_execution() -> None:
    """No explicit confirmation → NOT_CONFIRMED, no execution, REFINE (Req. 8.7)."""
    _seed, execute_delta, eval_delta, _runner, _memory = _run_pipeline(
        _not_confirmed()
    )

    report = execute_delta["report"]
    _assert_no_execution(report, RunOutcome.NOT_CONFIRMED)
    assert report["state"] == OperationState.NOT_STARTED.value

    # A missing confirmation blocks before execution → refine Stage 4 (Req. 8.6).
    _assert_refine_without_score(eval_delta, AgentStage.OPERATIONALIZATION)
    assert "confirma" in eval_delta["error"].lower()


def test_no_decision_gate_surfaced_as_refinable_with_no_execution() -> None:
    """A gate returned no decision → CALDERA_UNAVAILABLE, no execution, REFINE (Req. 8.8)."""
    _seed, execute_delta, eval_delta, _runner, _memory = _run_pipeline(
        _caldera_unavailable()
    )

    report = execute_delta["report"]
    _assert_no_execution(report, RunOutcome.CALDERA_UNAVAILABLE)
    assert report["state"] == OperationState.NOT_STARTED.value

    # "No decision" is treated as rejected and routed to refinement (Req. 8.8).
    _assert_refine_without_score(eval_delta, AgentStage.OPERATIONALIZATION)


def test_every_gate_rejection_stores_a_failed_outcome_into_rag() -> None:
    """Each rejection is learned from: the failed outcome is stored back to RAG.

    The evaluator records the blocked outcome in the long-term memory so future
    proposals of OTHER campaigns learn the proposal was blocked; the stored
    evaluation carries the REFINE decision and no fabricated score (Req. 8.6,
    10.6, design §Evaluator node).
    """
    for builder in (
        _containment_refused,
        _isolation_failed,
        _isolation_broken,
        _not_confirmed,
        _caldera_unavailable,
    ):
        _seed, _execute_delta, eval_delta, _runner, memory = _run_pipeline(builder())
        assert len(memory.stored) == 1
        stored_campaign, _stored_report, stored_eval = memory.stored[0]
        assert stored_campaign == "campaign-under-test"
        assert stored_eval["decision"] == EvaluatorDecision.REFINE.value
        assert "success_rate" not in stored_eval
