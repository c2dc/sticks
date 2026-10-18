"""ReportBuilder — Stage 6 final report (verdict + invariants) — task 6.1.

This module builds the **final execution report** of Stage 6 from a persisted
:class:`~app.models.domain.Operation`, enforcing the consistency invariants of
Req. 3.4 and the "never mix executions" rule of Req. 3.5.

Why a dedicated builder (the bug this fixes)
--------------------------------------------
In the real ShadowRay run the Operation was persisted ``FINISHED`` with only a
partial slice of ``AbilityResult`` rows collected, producing an **inconsistent**
report: verdict "Falha", "0 nós falharam", yet techniques stuck "em execução".
The incremental collection loop (task 3.5) already fixes the *collection* side;
this builder guarantees the *report* side is internally consistent:

Given the per-Ability results persisted for an Operation, :meth:`build_report`
derives the report **deterministically** and guarantees, for a report presented
as final (Req. 3.4):

* **(a)** the verdict is a deterministic function of the computed success rate;
* **(b)** the sum of techniques per terminal state (``sucesso`` + ``falha``)
  equals the campaign's total techniques;
* **(c)** the failed-nodes count equals the number of techniques in ``falha``;
* **(d)** no technique remains in a non-terminal state (``pendente`` /
  ``em_execucao``) in a final report.

Discard & recompute stale persisted state (Req. 3.5)
----------------------------------------------------
A report may already be persisted in :class:`~app.models.domain.ExecutionReport`
for the campaign. If that persisted row references a **different** execution
(its ``operation_id`` diverges from the current Operation) or an unreadable /
divergent ``metrics`` schema, the builder **discards** it and recomputes the
report **exclusively** from the current Operation's results — it never combines
data from distinct executions. The freshly derived ``metrics`` and
``success_verdict`` are then persisted back onto the (reused or newly created)
``ExecutionReport`` row for the campaign.

Purity & dependencies
----------------------
The derivation of the view from the per-Ability results is a **pure** function
(:meth:`ReportBuilder._derive`), independent of the database — the tests exercise
it directly. :meth:`build_report` adds the thin persistence layer (read the
Operation's results, discard stale state, upsert the ``ExecutionReport`` row).

_Requirements: 3.4, 3.5_
"""

from __future__ import annotations

import datetime as dt
from typing import Optional, Sequence

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import AbilityResult, ExecutionReport, Operation
from app.models.enums import AbilityResultStatus

# Terminal per-Ability states (Req. 3.4d): a final report admits only these two.
# ``pendente`` / ``em_execucao`` are non-terminal and must not survive into a
# final report — the builder treats any leftover non-terminal result as a
# ``falha`` (a technique that never completed did not succeed).
_TERMINAL_ABILITY_STATES = (AbilityResultStatus.SUCCESS, AbilityResultStatus.FAILURE)


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp, consistent with the models and the runner."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class ExecutionReportView(BaseModel):
    """The final, consistency-checked Stage 6 report (Req. 3.4).

    This is the return type of :meth:`ReportBuilder.build_report`: a typed,
    serializable view derived **deterministically** from the Operation's
    per-Ability results. It is constructed only through
    :meth:`ReportBuilder._derive`, which guarantees the four invariants of
    Req. 3.4 hold for every instance:

    * ``total_success + total_failure == total_techniques`` (b);
    * ``len(failed_nodes) == total_failure`` (c);
    * ``success_rate == total_success / total_techniques`` (``0.0`` when there are
      no techniques) and ``success_verdict`` is a deterministic function of it
      (a) — ``True`` iff there is at least one technique and the success rate is
      exactly ``1.0``;
    * no entry in ``per_ability`` is in a non-terminal state (d).

    Attributes:
        operation_id: The local ``Operation.id`` this report was derived from
            (the single execution it describes — Req. 3.5).
        total_techniques: Total number of techniques of the campaign's execution
            (the number of per-Ability results of the Operation).
        total_success: Count of techniques in ``sucesso``.
        total_failure: Count of techniques in ``falha`` (includes any technique
            that never reached a terminal state — normalized to ``falha`` for a
            final report, Req. 3.4d).
        success_rate: ``total_success / total_techniques`` ∈ ``[0, 1]`` (``0.0``
            when there are no techniques).
        success_verdict: The deterministic verdict derived from ``success_rate``.
        failed_nodes: The ``ability_id`` of each technique in ``falha`` (its
            length equals ``total_failure`` — invariant c).
        per_ability: The per-Ability results in execution order, each already in
            a terminal state (invariant d).
    """

    operation_id: Optional[int] = None
    total_techniques: int = 0
    total_success: int = 0
    total_failure: int = 0
    success_rate: float = 0.0
    success_verdict: bool = False
    failed_nodes: list[str] = Field(default_factory=list)
    per_ability: list["AbilityReportEntry"] = Field(default_factory=list)


class AbilityReportEntry(BaseModel):
    """One technique's entry in a final report — always in a terminal state.

    Attributes:
        ability_id: The technique's Ability id (``""`` when Caldera did not
            associate the link to an ability — still a terminal entry).
        status: The terminal status (``sucesso`` or ``falha``) — never
            ``pendente`` / ``em_execucao`` in a final report (Req. 3.4d).
    """

    ability_id: str = ""
    status: AbilityResultStatus = AbilityResultStatus.FAILURE


# Rebuild the forward reference now that ``AbilityReportEntry`` is defined.
ExecutionReportView.model_rebuild()


class ReportBuilder:
    """Build the final Stage 6 report with the Req. 3.4/3.5 guarantees.

    The pure derivation (:meth:`_derive`) turns a sequence of per-Ability results
    into a consistency-checked :class:`ExecutionReportView`. :meth:`build_report`
    reads the current Operation's persisted results, discards any stale
    ``ExecutionReport`` state (divergent ``operation_id``/schema — Req. 3.5) and
    persists the freshly derived ``metrics`` / ``success_verdict`` back onto the
    campaign's ``ExecutionReport`` row.

    _Requirements: 3.4, 3.5_
    """

    def __init__(self, *, session: Session) -> None:
        """Build a ReportBuilder.

        Args:
            session: SQLAlchemy session used to read the Operation's
                ``AbilityResult`` rows and upsert the ``ExecutionReport``.
        """
        self._session = session

    # -- public entry point --------------------------------------------------

    def build_report(self, operation: Operation) -> ExecutionReportView:
        """Build (and persist) the final report for ``operation``.

        Reads the Operation's persisted per-Ability results, derives the
        consistency-checked view (Req. 3.4), discards any stale persisted
        ``ExecutionReport`` whose ``operation_id`` or ``metrics`` schema diverges
        from this execution (Req. 3.5), and persists the fresh ``metrics`` /
        ``success_verdict`` onto the campaign's ``ExecutionReport`` row.

        The report is derived **exclusively** from ``operation`` — it never
        combines results of distinct executions.

        Args:
            operation: The persisted :class:`~app.models.domain.Operation` whose
                final report is built.

        Returns:
            The :class:`ExecutionReportView` for ``operation`` (invariants of
            Req. 3.4 guaranteed).
        """
        results = self._load_results(operation.id)
        view = self._derive(operation_id=operation.id, results=results)

        if operation.campaign_id is not None:
            self._persist(operation=operation, view=view)

        return view

    # -- pure derivation (Req. 3.4) ------------------------------------------

    def _derive(
        self,
        *,
        operation_id: Optional[int],
        results: Sequence[AbilityResult],
    ) -> ExecutionReportView:
        """Derive the consistency-checked view from the per-Ability results.

        Pure and deterministic (no DB access). Guarantees the four invariants of
        Req. 3.4:

        * each result is normalized to a **terminal** state — ``sucesso`` stays
          ``sucesso`` and **anything else** (``falha``, ``pendente``,
          ``em_execucao``) becomes ``falha`` (a technique that did not succeed did
          not pass), so no non-terminal state survives into the final report (d);
        * ``total_success + total_failure == total_techniques`` because every
          result lands in exactly one of the two terminal buckets (b);
        * ``failed_nodes`` collects exactly the ``falha`` techniques, so its
          length equals ``total_failure`` (c);
        * ``success_rate`` is computed from the counts and the ``success_verdict``
          is a deterministic function of it (a).
        """
        per_ability: list[AbilityReportEntry] = []
        failed_nodes: list[str] = []
        total_success = 0
        total_failure = 0

        for result in results:
            ability_id = result.ability_id or ""
            if result.status is AbilityResultStatus.SUCCESS:
                total_success += 1
                per_ability.append(
                    AbilityReportEntry(
                        ability_id=ability_id, status=AbilityResultStatus.SUCCESS
                    )
                )
            else:
                # Normalize non-success (incl. non-terminal) to ``falha`` so a
                # final report never carries a pending/running technique (d) and
                # the failed-nodes count tracks the failures exactly (c).
                total_failure += 1
                failed_nodes.append(ability_id)
                per_ability.append(
                    AbilityReportEntry(
                        ability_id=ability_id, status=AbilityResultStatus.FAILURE
                    )
                )

        total = total_success + total_failure
        success_rate = (total_success / total) if total else 0.0
        # Deterministic verdict from the computed success rate (Req. 3.4a):
        # a run succeeds iff it has at least one technique and every technique
        # succeeded (success rate exactly 1.0). This is a pure function of
        # ``success_rate`` and the technique count.
        success_verdict = total > 0 and success_rate >= 1.0

        return ExecutionReportView(
            operation_id=operation_id,
            total_techniques=total,
            total_success=total_success,
            total_failure=total_failure,
            success_rate=success_rate,
            success_verdict=success_verdict,
            failed_nodes=failed_nodes,
            per_ability=per_ability,
        )

    # -- persistence (Req. 3.5) ----------------------------------------------

    def _load_results(self, operation_id: Optional[int]) -> list[AbilityResult]:
        """Load the Operation's persisted per-Ability results, in insertion order.

        Returns an empty list when the Operation has no id (not yet persisted),
        so the derivation yields an empty — but still consistent — report.
        """
        if operation_id is None:
            return []
        stmt = (
            select(AbilityResult)
            .where(AbilityResult.operation_id == operation_id)
            .order_by(AbilityResult.id)
        )
        return list(self._session.execute(stmt).scalars().all())

    def _persist(self, *, operation: Operation, view: ExecutionReportView) -> None:
        """Persist the fresh ``metrics`` / ``success_verdict`` for the campaign.

        Discards any stale ``ExecutionReport`` whose ``operation_id`` diverges
        from the current execution (Req. 3.5): such a row describes a **different**
        execution, so it is deleted before persisting the recomputed report —
        the builder never combines data from distinct executions. A single row
        per campaign is maintained (latest-execution-wins upsert).
        """
        campaign_id = operation.campaign_id
        assert campaign_id is not None  # guarded by the caller

        existing = list(
            self._session.execute(
                select(ExecutionReport).where(
                    ExecutionReport.campaign_id == campaign_id
                )
            )
            .scalars()
            .all()
        )

        row: Optional[ExecutionReport] = None
        for candidate in existing:
            # Stale state (divergent operation_id/schema) is discarded, never
            # reused or merged (Req. 3.5).
            if candidate.operation_id == operation.id:
                row = candidate
            else:
                self._session.delete(candidate)

        metrics = view.model_dump(mode="json")
        if row is None:
            row = ExecutionReport(
                campaign_id=campaign_id,
                operation_id=operation.id,
                metrics=metrics,
                success_verdict=view.success_verdict,
                environment_deprovisioned=False,
                generated_at=_utcnow(),
            )
            self._session.add(row)
        else:
            row.operation_id = operation.id
            row.metrics = metrics
            row.success_verdict = view.success_verdict
            row.generated_at = _utcnow()

        self._session.commit()


__all__ = [
    "AbilityReportEntry",
    "ExecutionReportView",
    "ReportBuilder",
]
