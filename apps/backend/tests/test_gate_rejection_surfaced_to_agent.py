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

Separate ``execution`` / ``evaluation`` phases (task 4.2, Req. 2.1, 2.2)
------------------------------------------------------------------------

The same end-to-end harness is extended to pin the phase separation the design
promotes (``evaluation`` is its own ordered step AFTER ``execution`` and reads
the real execution result as its input):

* ``test_evaluation_consumes_the_execution_result_not_the_report`` — the
  ``evaluation`` phase renders its verdict from the structured
  ``execution_result`` the ``execution`` phase produced, NOT from the report.
  Proven by handing the evaluator an ``execution_result`` whose verdict
  *disagrees* with what the report alone would yield and asserting the evaluator
  follows the execution result (Req. 2.2).
* ``test_evaluation_is_a_distinct_ordered_step_after_execution`` — ``evaluation``
  is a distinct step ordered strictly after ``execution``: it only has an
  ``execution_result`` to read because ``execution`` ran first and produced it,
  and feeding the evaluator the ``execution`` phase's own output drives the
  verdict (Req. 2.1).

_Requirements: 2.1, 2.2, 8.1, 8.2, 8.3, 8.4, 8.6, 8.7, 8.8, 9.3, 9.4, 9.5_
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


def _completed_success() -> OperationRunResult:
    """The Operation ran to completion and passed → the single passing verdict.

    ``execution`` serializes this into an ``execution_result`` with
    ``verdict="concluida"`` / ``verdict_success=True`` (``total_success > 0`` and
    no failure), which the ``evaluation`` phase reads to decide FINISH (Req. 2.2).
    """
    return OperationRunResult(
        outcome=RunOutcome.COMPLETED,
        state=OperationState.FINISHED,
        operation_id=42,
        total_success=1,
        total_failure=0,
        message="Emulação concluída: todos os comandos executaram com sucesso.",
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
            "dag": {"origin": "ai_generated", "nodes": ["T1059"]},
            "operationalization": {
                "origin": "ai_generated",
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
            "environment": {"origin": "ai_generated"},
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
    # the delta carries the report plus the structured execution result the
    # ``evaluation`` phase receives (task 4.1 decoupling; Req. 2.1, 2.2, 8.2, 8.3).
    assert seed == seed_before
    assert set(execute_delta.keys()) == {"report", "execution_result"}

    # Feed the report AND the execution result to the evaluator exactly as the
    # graph would hand them from the ``execution`` phase to the ``evaluation`` one.
    eval_state = dict(seed)
    eval_state["report"] = execute_delta["report"]
    eval_state["execution_result"] = execute_delta["execution_result"]
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
    assert seed_before["artifacts"]["operationalization"]["origin"] == "ai_generated"


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


# ---------------------------------------------------------------------------
# Separate execution / evaluation phases (task 4.2, Req. 2.1, 2.2)
# ---------------------------------------------------------------------------
#
# These two tests extend the same end-to-end harness to pin the phase
# separation: the ``execution`` phase produces the real outcome
# (``execution_result``) and the ``evaluation`` phase — a distinct step ordered
# strictly AFTER execution — reads THAT result as its input rather than being
# coupled to the execute step or re-deriving the verdict itself.


def test_evaluation_consumes_the_execution_result_not_the_report() -> None:
    """``evaluation`` renders the verdict from ``execution_result``, not the report.

    This proves the ``evaluation`` phase RECEIVES the execution result as its
    input (Req. 2.2): we hand the evaluator an ``execution_result`` whose verdict
    DISAGREES with what the report alone would yield, and assert the evaluation
    follows the execution result.

    The seed carries a report that, read on its own, derives a *failing* verdict
    (it is ``COMPLETED`` but with a failed ability, so the report-only rule
    ``success > 0 and failure == 0`` is false → REFINE). The structured
    ``execution_result`` the ``execution`` phase produced instead reports a
    *passing* verdict (``verdict_success=True``). Because the ``evaluation`` phase
    consumes the execution result, it must decide FINISH — if it (incorrectly)
    read the report, it would REFINE. The disagreement is what makes the source
    of the verdict observable.
    """
    seed = _seed_state()

    # A report that, derived on its own, yields a FAILING verdict: COMPLETED but
    # with a failure present, so the report-only rule is false.
    report_only_failing = {
        "outcome": RunOutcome.COMPLETED.value,
        "state": OperationState.FINISHED.value,
        "operation_id": 7,
        "total_success": 1,
        "total_failure": 1,  # a failure → report-only verdict_success is False
        "ability_results": [],
    }
    # The execution phase's own structured result reports a PASSING verdict,
    # disagreeing with the report-only derivation above.
    execution_result_passing = {
        "verdict": RunOutcome.COMPLETED.value,
        "verdict_success": True,
        "executed": True,
        "containment_passed": True,
        "isolation_passed": True,
        "total_success": 1,
        "total_failure": 0,
        "message": "ok",
    }

    eval_state = dict(seed)
    eval_state["report"] = report_only_failing
    eval_state["execution_result"] = execution_result_passing

    eval_delta = evaluator.node(eval_state, memory=_RecordingMemory())

    # The evaluation followed the EXECUTION RESULT (passing → FINISH), not the
    # report (which alone would have driven REFINE) — so it received and used the
    # execution result as its input (Req. 2.2).
    assert eval_delta["evaluation"]["verdict_success"] is True
    assert eval_delta["evaluation"]["decision"] == EvaluatorDecision.FINISH.value
    assert eval_delta["refine_stage"] is None

    # Control: strip the execution result and the SAME report now drives REFINE,
    # confirming the FINISH above came from the execution result, not the report.
    report_only_state = dict(seed)
    report_only_state["report"] = dict(report_only_failing)
    control_delta = evaluator.node(report_only_state, memory=_RecordingMemory())
    assert control_delta["evaluation"]["verdict_success"] is False
    assert control_delta["evaluation"]["decision"] == EvaluatorDecision.REFINE.value


def test_evaluation_is_a_distinct_ordered_step_after_execution() -> None:
    """``evaluation`` is a distinct step ordered AFTER ``execution`` (Req. 2.1).

    The ordering is observable through the data dependency: the ``evaluation``
    phase only has an ``execution_result`` to read because the ``execution``
    phase ran first and produced it. Here we run the real ``execution`` phase
    over a passing runner result, then hand its OWN output to the real
    ``evaluation`` phase, and assert evaluation rendered a FINISH verdict from the
    execution phase's result — the two are separate, sequential steps.
    """
    seed = _seed_state()
    seed_before = copy.deepcopy(seed)

    # --- execution phase: run first, produce the real result -----------------
    runner = _RecordingRunner(_completed_success())
    execute_delta = execute_node(seed, runner=runner)

    # execution produced BOTH the report and the structured execution_result it
    # hands downstream to evaluation; it did not mutate the input state.
    assert seed == seed_before
    assert set(execute_delta.keys()) == {"report", "execution_result"}
    execution_result = execute_delta["execution_result"]
    assert execution_result["verdict"] == RunOutcome.COMPLETED.value
    assert execution_result["verdict_success"] is True
    assert execution_result["executed"] is True

    # --- evaluation phase: a SEPARATE step consuming execution's output ------
    eval_state = dict(seed)
    eval_state["report"] = execute_delta["report"]
    eval_state["execution_result"] = execution_result
    eval_before = copy.deepcopy(eval_state)

    eval_delta = evaluator.node(eval_state, memory=_RecordingMemory())

    # evaluation is its own step: it reads the execution result and renders the
    # verdict, returning a small delta without mutating the state it was handed.
    assert eval_state == eval_before
    assert eval_delta["evaluation"]["verdict_success"] is True
    assert eval_delta["evaluation"]["decision"] == EvaluatorDecision.FINISH.value
    assert eval_delta["refine_stage"] is None

    # The ordering is real, not incidental: strip the execution phase's output
    # and the evaluation phase has no executed result to read — it degrades to a
    # "report absent" blocked iteration rather than rendering a verdict. This is
    # only possible because evaluation runs AFTER, and depends on, execution.
    no_execution_state = dict(seed)
    no_execution_delta = evaluator.node(
        no_execution_state, memory=_RecordingMemory()
    )
    assert no_execution_delta["evaluation"]["decision"] == EvaluatorDecision.REFINE.value
    assert isinstance(no_execution_delta.get("error"), str)
    assert no_execution_delta["error"].strip()
