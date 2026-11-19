"""Startup reconciliation of orphaned runs and operations (Req. 13).

A backend restart (for example the uvicorn ``--reload`` triggered by a file edit)
kills every in-process phase job. Rows left ``running`` or ``pending`` then have no
job behind them. :func:`reconcile_orphans` runs once at startup and closes them:

* an orphan Run (``pending`` / ``running``) becomes ``failed`` with
  ``failure.code = "interrupted_by_restart"``;
* an orphan Operation (``running``) becomes ``aborted`` with
  ``abort_reason = "interrupted_by_restart"`` and ``finished_at`` set.

Runs in ``awaiting_approval`` are never touched: they wait for the researcher and
survive a restart. The function is idempotent, because a second pass finds no
orphan left. The process just started, so no job of this process is alive yet.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import AgentRun, Operation
from app.models.enums import AgentRunStatus, OperationState

logger = logging.getLogger(__name__)

INTERRUPTED_BY_RESTART = "interrupted_by_restart"

_ORPHAN_RUN_STATUSES = (AgentRunStatus.PENDING, AgentRunStatus.RUNNING)


@dataclass(frozen=True)
class ReconcileResult:
    """How many rows the reconciliation changed."""

    runs_failed: int
    operations_aborted: int


def reconcile_orphans(session_factory: Callable[[], Session]) -> ReconcileResult:
    """Fail orphan runs and abort orphan operations in one transaction."""
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    with session_factory() as db:
        runs = db.scalars(select(AgentRun).where(AgentRun.status.in_(_ORPHAN_RUN_STATUSES))).all()
        for run in runs:
            run.failure = {
                "phase": run.current_phase,
                "code": INTERRUPTED_BY_RESTART,
                "cause_code": None,
            }
            run.error = INTERRUPTED_BY_RESTART
            run.status = AgentRunStatus.FAILED
            run.updated_at = now

        operations = db.scalars(
            select(Operation).where(Operation.state == OperationState.RUNNING)
        ).all()
        for operation in operations:
            operation.state = OperationState.ABORTED
            operation.abort_reason = INTERRUPTED_BY_RESTART
            operation.finished_at = now

        db.commit()

    if runs or operations:
        logger.warning(
            "reconciler: %d run(s) marked failed, %d operation(s) marked aborted (%s)",
            len(runs),
            len(operations),
            INTERRUPTED_BY_RESTART,
        )
    return ReconcileResult(runs_failed=len(runs), operations_aborted=len(operations))
