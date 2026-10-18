"""OperationRunner — explicit-confirmation gating + containment pre-flight +
aggregation (task 7.3) — security core.

This is the enforcement point the design's emulation sequence diagram describes:
it conducts an Operation **only after** the Pesquisador confirms explicitly and
the containment pre-flight passes, then drives the Caldera execution, persists
the per-Ability results and the Operation state transitions, aggregates the
success/failure totals, and records one audit row per executed command.

Order of enforcement (faithful to the design sequence diagram)
--------------------------------------------------------------
1. **Explicit confirmation (Req. 6.6, 6.7 — Property 6).** The Operation starts
   *if and only if* ``confirmed is True``. If confirmation is absent/false or
   the request is cancelled, **no adversary command is executed** and the state
   returns to the prior state (``NOT_STARTED``). This gate runs *before* any
   pre-flight, Caldera call or DB write for the run — nothing observable happens.
2. **Containment of destinations (Req. 6.1, 6.2 — Property 1).** Every Ability's
   commands are validated with the injected ``validate_abilities``. If any
   command reaches an external destination, the Operation is **refused** and does
   not start (no Caldera call, no execution).
3. **Container isolation (Req. 6.3, 6.4 — Property 2).** Each target container is
   inspected (read-only) with the injected isolation verifier. If any target
   container is not isolated, the Operation is **aborted** and does not start.
4. **Caldera availability (Req. 4.6).** ``CalderaClient.ensure_available`` must
   confirm Caldera answers within 10s; otherwise the Operation is **not started**
   (the raised :class:`CalderaUnavailable` is mapped to HTTP 503 upstream).

Only when all four gates pass does the runner create the Caldera Operation, poll
its per-Ability links, persist an :class:`AbilityResult` per Ability, aggregate
``total_success`` / ``total_failure`` onto the :class:`Operation` (Req. 4.5), and
record one :class:`AuditLogEntry` per executed command through the injected
:class:`AuditLogger` (Req. 6.8).

Audit-failure abort in the per-command loop (task 7.5 — Req. 6.9)
-----------------------------------------------------------------
Each executed command is handled by :meth:`OperationRunner._handle_command`,
which persists the ability result and calls the audit logger through the small
seam :meth:`OperationRunner._audit_command`. If persisting an
:class:`AuditLogEntry` fails, ``_audit_command`` raises
:class:`~app.services.audit.AuditPersistenceError`; the per-command loop catches
it at the seam and **aborts the Operation from that command onward** — it breaks
the loop so no further command is processed (no additional AbilityResult/audit
row is written), marks the Operation ``ABORTED`` (persists
:data:`OperationState.ABORTED` and ``finished_at``) and returns a result with
:data:`RunOutcome.ABORTED` plus a message indicating the audit trail could not be
recorded. Commands executed before the failure keep their persisted
AbilityResults, so the aggregate reflects exactly what ran up to the abort. The
happy path and the four pre-flight gates are untouched.

Dependency injection (no real Caldera/Docker in dev)
----------------------------------------------------
Everything external is injected so tests (7.4 / 7.6 / 7.7) can drive the runner
fully mocked:

* ``session`` — SQLAlchemy session for persisting Operation / AbilityResult.
* ``caldera`` — a :class:`CalderaClient` (real or MockTransport-backed).
* ``containment_validator`` — the destination validator function (defaults to
  :func:`app.services.containment.validate_abilities`).
* ``isolation_inspector`` — a read-only container inspector; when omitted the
  runner uses :func:`app.services.containment.inspect_isolation` with the
  injected inspector (``None`` only in production, where a real read-only Docker
  adapter is built lazily).
* ``audit_logger`` — an :class:`AuditLogger` bound to the same session.

The runner performs pure orchestration; the actual command execution belongs to
Caldera (mocked in dev). It never touches the internal attack networks.

_Requirements: 6.6, 6.7, 4.5, 6.1, 6.2, 6.3, 6.4, 4.6_
"""

from __future__ import annotations

import datetime as dt
import enum
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Sequence

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.domain import AbilityResult, Operation
from app.models.enums import AbilityResultStatus, OperationState
from app.services.audit import AuditLogger, AuditPersistenceError
from app.services.caldera import CalderaClient, OperationLink
from app.services.caldera.client import (
    OperationStateResult,
    TERMINAL_OPERATION_STATES,
)
from app.services.containment import (
    ContainerInspector,
    IsolationResult,
    inspect_isolation,
    preview_abilities,
)
from app.services.containment.subnet_validator import (
    OperationContainmentReport,
    validate_abilities,
)

# Type of the injected destination-containment validator (task 4.2). Kept as a
# Callable alias so tests can inject a lambda/fake and production uses the real
# ``validate_abilities`` by default.
ContainmentValidator = Callable[[Sequence[object]], OperationContainmentReport]

# Type of the injected isolation verifier (task 4.4). It receives the list of
# target container names and an optional inspector, returning an IsolationResult.
IsolationVerifier = Callable[[list[str], Optional[ContainerInspector]], IsolationResult]

# Injection seams for the incremental collection loop (task 3.1). ``Clock``
# returns a monotonically increasing time in seconds (used to measure the
# collection budget); ``Sleeper`` sleeps for N seconds between polls. Defaulting
# to :func:`time.monotonic` / :func:`time.sleep` in production and overridable in
# tests so the loop runs deterministically without touching the real clock.
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp, consistent with :class:`AuditLogger` and the models."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` to the inclusive range ``[low, high]`` (Req. 3.1).

    The collection loop uses this to bound the effective poll interval to
    ``clamp(value, 1, 10)`` seconds regardless of how the interval was configured
    or injected, keeping the loop's timing faithful to the requirement.
    """
    return max(low, min(high, float(value)))


class RunOutcome(str, enum.Enum):
    """Why an Operation run ended the way it did (for precise upstream mapping).

    Subclasses ``str`` so it serializes to its value over JSON like the domain
    enums. These are *runner* outcomes; the persisted Operation state is the
    separate :class:`OperationState`.
    """

    #: ``confirmed`` was not True (absent/false/cancelled). Nothing executed;
    #: state returned to NOT_STARTED (Req. 6.6, 6.7 — Property 6).
    NOT_CONFIRMED = "nao_confirmada"
    #: A command reached an external destination; refused pre-flight (Req. 6.1/6.2).
    CONTAINMENT_REFUSED = "contencao_recusada"
    #: A target container was not isolated; aborted pre-flight (Req. 6.3/6.4).
    ISOLATION_FAILED = "isolamento_falhou"
    #: Caldera did not answer within 10s; not started (Req. 4.6, maps to 503).
    CALDERA_UNAVAILABLE = "caldera_indisponivel"
    #: The Operation ran to completion (per-Ability results aggregated).
    COMPLETED = "concluida"
    #: The Operation was aborted mid-run because persisting an audit row failed;
    #: no further command ran after the failing write (Req. 6.9 — Property 7).
    ABORTED = "abortada"
    #: No terminal Caldera state was observed within the collection timeout; the
    #: Operation is marked INCOMPLETE_TIMEOUT, partial results are preserved and
    #: it is not counted as a successful run (Req. 3.2 — Property 3).
    INCOMPLETE_TIMEOUT = "incompleta_timeout"
    #: Container isolation stopped passing during execution; the run is
    #: interrupted with no further commands, partials preserved, state ABORTED
    #: (Req. 6.4).
    ISOLATION_BROKEN = "isolamento_quebrado"


@dataclass(frozen=True)
class AbilityRunResult:
    """The per-Ability outcome the runner produced and persisted.

    Attributes:
        ability_id: The Ability that executed (``None`` when Caldera did not
            associate the link to an ability).
        status: The derived :class:`AbilityResultStatus`
            ("pendente"/"em_execucao"/"sucesso"/"falha").
        command: The concrete command executed (decoded), if known.
        target_container: The resolved target container label used for the audit
            row (e.g. ``"nginx (192.168.20.30)"``), or a neutral local label.
        output: The command's stdout/stderr, if collected.
    """

    ability_id: str | None
    status: AbilityResultStatus
    command: str | None
    target_container: str
    output: str | None = None


@dataclass(frozen=True)
class _CollectionResult:
    """Outcome of the incremental collection loop (task 3.5).

    Produced by :meth:`OperationRunner._collect_until_terminal`, this carries the
    final reconciled per-Ability view plus how the loop ended so ``_execute`` can
    finalize the Operation state and aggregate.

    Attributes:
        ability_results: The final reconciled per-Ability results, in execution
            order (one entry per terminal/observed link of the last poll).
        total_success: Count of ``sucesso`` Abilities in the final view.
        total_failure: Count of ``falha`` Abilities in the final view.
        reached_terminal: ``True`` when the Caldera Operation reached a terminal
            state (``finished`` / ``out_of_time`` / ``cleanup``) before the
            collection timeout; ``False`` when the budget elapsed first (Req. 3.2).
        aborted: ``True`` when an audit-persistence failure aborted the run from
            a command onward (Req. 6.9); partials up to that point are preserved.
        abort_message: Human-readable abort cause when ``aborted`` or
            ``isolation_broken`` is ``True``.
        isolation_broken: ``True`` when container isolation stopped passing during
            execution; the loop interrupted with no further commands and the
            partials up to that point are preserved (Req. 6.3, 6.4).
        isolation: The failing :class:`IsolationResult` from the re-check that
            broke, carried so the run result can surface the affected container.
    """

    ability_results: tuple[AbilityRunResult, ...]
    total_success: int
    total_failure: int
    reached_terminal: bool
    aborted: bool = False
    abort_message: str | None = None
    isolation_broken: bool = False
    isolation: IsolationResult | None = None


@dataclass(frozen=True)
class OperationRunResult:
    """Typed result of a run: final state + aggregate + per-Ability results.

    Attributes:
        outcome: Why the run ended (:class:`RunOutcome`).
        state: The final persisted :class:`OperationState` of the Operation.
        operation_id: The local ``Operation.id`` (``None`` when no Operation row
            was created — i.e. an unconfirmed request that changed nothing).
        caldera_operation_id: The Caldera operation id, when one was created.
        total_success: Aggregated count of "sucesso" Abilities (Req. 4.5).
        total_failure: Aggregated count of "falha" Abilities (Req. 4.5).
        ability_results: Per-Ability results, in execution order.
        containment: The containment verdict when a pre-flight ran (for messages).
        isolation: The isolation verdict when a pre-flight ran (for messages).
        message: Human-readable summary suitable for the UI / error body.
    """

    outcome: RunOutcome
    state: OperationState
    operation_id: int | None = None
    caldera_operation_id: str | None = None
    total_success: int = 0
    total_failure: int = 0
    ability_results: tuple[AbilityRunResult, ...] = ()
    containment: OperationContainmentReport | None = None
    isolation: IsolationResult | None = None
    message: str = ""

    @property
    def started(self) -> bool:
        """True iff the Operation actually started executing on Caldera.

        Includes ``INCOMPLETE_TIMEOUT``: the Operation was created and polled on
        Caldera but no terminal state was observed within the collection budget;
        it is not a *successful* run, but it did start executing (Req. 3.2).
        """
        return self.outcome in (
            RunOutcome.COMPLETED,
            RunOutcome.ABORTED,
            RunOutcome.INCOMPLETE_TIMEOUT,
        )


class OperationRunner:
    """Conduct an Operation with explicit confirmation, pre-flight and aggregation.

    Pure orchestration over injected dependencies (Caldera, containment,
    isolation, audit). See the module docstring for the full enforcement order
    and the hook seam for task 7.5.
    """

    def __init__(
        self,
        *,
        session: Session,
        caldera: CalderaClient,
        audit_logger: AuditLogger,
        containment_validator: ContainmentValidator = validate_abilities,
        isolation_verifier: IsolationVerifier = inspect_isolation,
        isolation_inspector: ContainerInspector | None = None,
        collection_interval: float | None = None,
        collection_timeout: float | None = None,
        isolation_recheck_interval: float | None = None,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        """Build an OperationRunner.

        Args:
            session: SQLAlchemy session used to persist the :class:`Operation` and
                its :class:`AbilityResult` rows (state transitions + aggregate).
            caldera: The :class:`CalderaClient` (real or mocked) used to check
                availability, load abilities/adversary, create and poll the
                Operation.
            audit_logger: The :class:`AuditLogger` (bound to ``session``) that
                records one :class:`AuditLogEntry` per executed command (Req. 6.8).
            containment_validator: The destination-containment validator (task
                4.2). Defaults to :func:`validate_abilities`. Injected in tests.
            isolation_verifier: The isolation verifier (task 4.4). Defaults to
                :func:`inspect_isolation`. Injected in tests.
            isolation_inspector: Optional read-only container inspector passed to
                ``isolation_verifier``. In production this is ``None`` and the
                default verifier lazily builds a real read-only Docker adapter;
                tests inject a fake inspector so no daemon is touched.
            collection_interval: Seconds between polls in the incremental
                collection loop (task 3.5). Clamped to the inclusive range
                1..10 where it is consumed (Req. 3.1). When omitted it defaults to
                the settings' ``operation_poll_interval_clamped``.
            collection_timeout: Total collection budget in seconds, counted from
                the start of the Operation (Req. 3.1/3.2). When omitted it
                defaults to the settings' ``operation_collection_timeout_seconds``.
            isolation_recheck_interval: Maximum interval in seconds between the
                periodic isolation re-checks the collection loop performs during
                execution (Req. 6.3). Clamped to the inclusive range 0..30 where
                it is consumed so the effective re-check interval never exceeds
                30s. When omitted it defaults to the settings'
                ``isolation_recheck_interval_clamped``.
            clock: Monotonic time source (seconds) used to measure the collection
                budget. Defaults to :func:`time.monotonic`; injected in tests for
                a deterministic loop.
            sleeper: Callable that sleeps N seconds between polls. Defaults to
                :func:`time.sleep`; injected in tests so no real sleep happens.
        """
        self._session = session
        self._caldera = caldera
        self._audit = audit_logger
        self._validate_containment = containment_validator
        self._verify_isolation = isolation_verifier
        self._inspector = isolation_inspector

        # Resolve collection defaults from settings so the runner's defaults stay
        # a single source of truth with ``app/core/config.py`` (Req. 3.1). The
        # interval is clamped to 1..10s at consumption; here we take the already
        # clamped settings value as the default so an unset interval is in range.
        settings = get_settings()
        self._collection_interval = (
            settings.operation_poll_interval_clamped
            if collection_interval is None
            else collection_interval
        )
        self._collection_timeout = (
            settings.operation_collection_timeout_seconds
            if collection_timeout is None
            else collection_timeout
        )
        # Periodic isolation re-check interval (Req. 6.3). Default from settings
        # (already clamped to ≤ 30s); clamped again at consumption so an injected
        # value can never exceed the requirement's 30s cap.
        self._isolation_recheck_interval = (
            settings.isolation_recheck_interval_clamped
            if isolation_recheck_interval is None
            else isolation_recheck_interval
        )
        self._clock = clock
        self._sleep = sleeper

    # -- public entry point --------------------------------------------------

    def run(
        self,
        *,
        campaign_id: str | None,
        abilities: Sequence[object],
        adversary_id: str,
        confirmed: bool,
        operation_name: str | None = None,
        target_containers: Sequence[str] | None = None,
        adversary_payload: object | None = None,
    ) -> OperationRunResult:
        """Run one emulation, gating on confirmation + pre-flight, then aggregate.

        The four enforcement gates run in the exact order of the design sequence
        diagram (confirmation -> destinations -> isolation -> availability).
        Only when all pass is a Caldera Operation created, polled, its per-Ability
        results persisted and aggregated, and one audit row recorded per executed
        command.

        Args:
            campaign_id: The curated case slug the Operation belongs to (for the row).
            abilities: The Abilities to run (typically the Adversary's set, in
                ``atomic_ordering``). Each exposes ``ability_id``/``name``/
                ``executors``.
            adversary_id: The Caldera Adversary profile id to execute.
            confirmed: The **explicit** confirmation flag. The Operation starts
                iff this is exactly ``True`` (Req. 6.6, 6.7).
            operation_name: Optional Caldera Operation name; a stable default is
                derived from ``campaign_id`` when omitted.
            target_containers: The container names whose isolation is verified in
                the pre-flight. When omitted, they are derived from the Abilities'
                internal destinations via the preview resolver.

        Returns:
            An :class:`OperationRunResult` with the final state, the aggregate
            (``total_success`` / ``total_failure``) and the per-Ability results.
        """
        # --- Gate 1: explicit confirmation (Req. 6.6, 6.7 — Property 6) -----
        # This runs before ANY pre-flight, Caldera call or DB write for the run:
        # an unconfirmed/cancelled request must change nothing and execute no
        # adversary command, returning to the prior (NOT_STARTED) state.
        if confirmed is not True:
            return OperationRunResult(
                outcome=RunOutcome.NOT_CONFIRMED,
                state=OperationState.NOT_STARTED,
                message=(
                    "Emulação não iniciada: confirmação explícita ausente ou "
                    "solicitação cancelada. Nenhum comando de adversário foi executado."
                ),
            )

        abilities = list(abilities)

        # --- Gate 2: containment of destinations (Req. 6.1, 6.2 — Property 1) 
        containment = self._validate_containment(abilities)
        if containment.refused:
            return OperationRunResult(
                outcome=RunOutcome.CONTAINMENT_REFUSED,
                state=OperationState.NOT_STARTED,
                containment=containment,
                message=(
                    "Emulação recusada por contenção — destino externo detectado: "
                    + " | ".join(containment.describe_violations())
                ),
            )

        # --- Gate 3: container isolation (Req. 6.3, 6.4 — Property 2) --------
        containers = (
            list(target_containers)
            if target_containers is not None
            else self._derive_target_containers(abilities)
        )
        isolation = self._verify_isolation(containers, self._inspector)
        if not isolation.passed:
            return OperationRunResult(
                outcome=RunOutcome.ISOLATION_FAILED,
                state=OperationState.NOT_STARTED,
                containment=containment,
                isolation=isolation,
                message=isolation.summary(),
            )

        # --- Gate 4: Caldera availability (Req. 4.6) ------------------------
        # ``ensure_available`` raises CalderaUnavailable on timeout/unreachable;
        # it is intentionally allowed to propagate so the API layer maps it to
        # 503. No Operation row is created before this passes, so a failure here
        # leaves the state untouched (nothing started).
        self._caldera.ensure_available()

        # All gates passed — start and conduct the Operation. ``containers`` (the
        # same target list gate 3 verified) is threaded into execution so the
        # loop can re-verify isolation periodically during the run (Req. 6.3).
        return self._execute(
            campaign_id=campaign_id,
            abilities=abilities,
            adversary_id=adversary_id,
            operation_name=operation_name,
            containment=containment,
            isolation=isolation,
            containers=containers,
            adversary_payload=adversary_payload,
        )

    # -- execution (post-confirmation, post-pre-flight) ----------------------

    def _execute(
        self,
        *,
        campaign_id: str | None,
        abilities: Sequence[object],
        adversary_id: str,
        operation_name: str | None,
        containment: OperationContainmentReport,
        isolation: IsolationResult,
        containers: Sequence[str],
        adversary_payload: object | None = None,
    ) -> OperationRunResult:
        """Create the Caldera Operation, run the loop and aggregate results.

        Persists the Operation state transitions
        (NOT_STARTED -> RUNNING -> FINISHED) and one :class:`AbilityResult` per
        Ability, aggregates the totals (Req. 4.5), and records one audit row per
        executed command (Req. 6.8) via the hook-friendly per-command loop.
        """
        name = operation_name or self._default_operation_name(campaign_id)

        # Persist the Operation as RUNNING with its start instant, so the state
        # transition is durable and observable before any command runs.
        operation = Operation(
            campaign_id=campaign_id,
            state=OperationState.RUNNING,
            total_success=0,
            total_failure=0,
            started_at=_utcnow(),
        )
        self._session.add(operation)
        self._session.commit()
        self._session.refresh(operation)

        # Load the curated abilities AND the adversary into Caldera before the
        # Operation references it (Req. 4.2). Without the adversary (and its
        # `atomic_ordering`) loaded, the Operation would run with an empty
        # chain (zero links) — the real-lab failure this fixes.
        self._caldera.load_abilities(self._ability_payloads(abilities))
        if adversary_payload is not None:
            self._caldera.load_adversary(dict(adversary_payload))  # type: ignore[arg-type]

        # Create the Operation with the `batch` planner and the `red` group,
        # matching the lab agent registration and sticks/lib/operation.py.
        created = self._caldera.create_operation(
            name, adversary_id, group="red", planner="batch"
        )
        operation.caldera_operation_id = created.operation_id or None
        self._session.commit()

        # Collect incrementally until the Caldera Operation reaches a terminal
        # state or the collection budget elapses (task 3.5 — the bug fix). The
        # old single ``poll_until_complete`` + one-shot loop is replaced by a
        # loop that reconciles the persisted ``AbilityResult`` rows on every poll
        # (Req. 3.1/3.2/3.3). ``started_at`` anchors the timeout to the Operation
        # start, matching the design.
        target_label = self._primary_target_label(abilities, isolation)
        started_at = self._clock()
        collection = self._collect_until_terminal(
            operation_id=operation.id,
            started_at=started_at,
            caldera_operation_id=created.operation_id,
            target_label=target_label,
            containers=list(containers),
        )

        # Finalize the Operation state and aggregate from the collection result.
        #   * audit-failure abort -> ABORTED (partials preserved, Req. 6.9);
        #   * terminal reached     -> FINISHED, persisted count == terminal links
        #                             (reconciled, Req. 3.3);
        #   * timeout w/o terminal -> INCOMPLETE_TIMEOUT, partials preserved and
        #                             NOT counted as a successful run (Req. 3.2).
        operation.total_success = collection.total_success
        operation.total_failure = collection.total_failure
        if collection.isolation_broken:
            # Isolation stopped passing mid-run: interrupt with no further
            # commands, preserve the partials, and mark the Operation ABORTED
            # with the dedicated ISOLATION_BROKEN outcome (Req. 6.3, 6.4).
            operation.state = OperationState.ABORTED
            outcome = RunOutcome.ISOLATION_BROKEN
        elif collection.aborted:
            operation.state = OperationState.ABORTED
            outcome = RunOutcome.ABORTED
        elif collection.reached_terminal:
            operation.state = OperationState.FINISHED
            outcome = RunOutcome.COMPLETED
        else:
            operation.state = OperationState.INCOMPLETE_TIMEOUT
            outcome = RunOutcome.INCOMPLETE_TIMEOUT
        operation.finished_at = _utcnow()
        self._session.commit()

        if collection.isolation_broken:
            message = collection.abort_message or (
                "Operação abortada: o isolamento de contenção deixou de ser "
                "satisfeito durante a execução."
            )
        elif collection.aborted:
            message = collection.abort_message or (
                "Operação abortada: a trilha de auditoria não pôde ser registrada."
            )
        elif collection.reached_terminal:
            message = (
                f"Operação finalizada: {collection.total_success} sucesso(s), "
                f"{collection.total_failure} falha(s)."
            )
        else:
            message = (
                "Operação incompleta: o timeout de coleta decorreu sem um estado "
                "terminal no Caldera. Os resultados parciais foram preservados "
                f"({collection.total_success} sucesso(s), "
                f"{collection.total_failure} falha(s)) e a Operação NÃO é contada "
                "entre as execuções finalizadas com sucesso."
            )

        return OperationRunResult(
            outcome=outcome,
            state=operation.state,
            operation_id=operation.id,
            caldera_operation_id=operation.caldera_operation_id,
            total_success=collection.total_success,
            total_failure=collection.total_failure,
            ability_results=collection.ability_results,
            containment=containment,
            # On an isolation break surface the failing verdict (affected
            # container) rather than the pre-flight pass (Req. 6.4).
            isolation=collection.isolation or isolation,
            message=message,
        )

    # -- incremental collection loop (task 3.5 — core bug fix) ---------------

    def _collect_until_terminal(
        self,
        *,
        operation_id: int | None,
        started_at: float,
        caldera_operation_id: str,
        target_label: str,
        containers: list[str],
    ) -> _CollectionResult:
        """Poll + reconcile until a terminal state or the collection timeout.

        This is the heart of the bug fix (Req. 3). Instead of a single
        ``poll_until_complete`` followed by one iteration over the links, the
        runner drives its **own** collection loop:

        * On every iteration it calls :meth:`CalderaClient.get_operation` and
          reconciles the persisted :class:`AbilityResult` rows with the observed
          links via :meth:`_reconcile` (upsert per link), so the persisted state
          always tracks the best observation and feeds the live WS channel
          (Req. 2.3).
        * It stops at the **first** terminal Caldera state in
          :data:`TERMINAL_OPERATION_STATES` (``finished`` / ``out_of_time`` /
          ``cleanup``), **or** when the collection timeout elapses counted from
          ``started_at`` (Req. 3.1).
        * Between polls it sleeps the **clamped** interval ``clamp(value, 1, 10)``
          seconds via the injected ``sleeper`` (Req. 3.1).
        * On every iteration, if at least ``isolation_recheck_interval`` seconds
          (clamped to ≤ 30s — Req. 6.3) have elapsed since the last isolation
          check, it re-verifies the target ``containers`` with
          ``self._verify_isolation``. If isolation stopped passing it **stops the
          loop immediately with no further commands**, preserves the partials
          reconciled so far and reports ``isolation_broken=True`` with the failing
          verdict (which identifies the affected container — Req. 6.3, 6.4). This
          re-check is purely a read-only inspection; it issues no Caldera command.

        On a terminal state it performs a final reconcile so the persisted count
        equals the number of terminal links observed in the last poll (Req. 3.3)
        and reports ``reached_terminal=True``. On timeout it reports
        ``reached_terminal=False`` while **preserving** every partial already
        reconciled (Req. 3.2). If the audit seam inside :meth:`_reconcile` raises
        :class:`AuditPersistenceError`, the loop stops immediately, preserving the
        partials persisted up to that command and reporting ``aborted=True``
        (Req. 6.9).

        Args:
            operation_id: The local ``Operation.id`` whose results are reconciled.
            started_at: The monotonic instant (from ``self._clock``) the
                collection began; the timeout is measured from here.
            caldera_operation_id: The Caldera operation id to poll.
            target_label: The human-readable target label applied to each row.
            containers: The target container names re-verified for isolation on
                each iteration (the same list gate 3 verified). Threaded from
                ``run`` so the loop re-checks exactly the containers under attack.

        Returns:
            A :class:`_CollectionResult` with the final per-Ability view, the
            aggregate, and how the loop ended.
        """
        interval = _clamp(self._collection_interval, 1.0, 10.0)
        timeout = max(0.0, float(self._collection_timeout))
        deadline = started_at + timeout
        # Clamp the re-check interval to ≤ 30s at consumption (Req. 6.3), so even
        # an injected value cannot push the re-check beyond the requirement's cap.
        recheck_interval = _clamp(self._isolation_recheck_interval, 0.0, 30.0)
        # Anchor the first re-check to the collection start: the next re-check is
        # due once ``recheck_interval`` has elapsed from here. The pre-flight gate
        # already proved isolation at ``started_at``, so no re-check fires on the
        # first iteration unless the interval is 0 — keeping the immediate
        # terminal flow untouched.
        last_isolation_check = started_at

        latest: tuple[AbilityRunResult, ...] = ()
        reached_terminal = False

        while True:
            # Periodic fail-closed isolation re-check (Req. 6.3, 6.4). Runs
            # before issuing any further Caldera read for this iteration so a
            # broken isolation stops the loop without observing new links. The
            # pre-flight already proved isolation at ``started_at``; here we only
            # re-check once the interval has elapsed since the last check.
            if self._clock() - last_isolation_check >= recheck_interval:
                isolation = self._verify_isolation(containers, self._inspector)
                last_isolation_check = self._clock()
                if not isolation.passed:
                    # Isolation broke mid-run: interrupt with no further commands,
                    # preserve the partials reconciled so far and signal the
                    # containment break with the affected container (Req. 6.4).
                    success, failure = self._tally(latest)
                    return _CollectionResult(
                        ability_results=latest,
                        total_success=success,
                        total_failure=failure,
                        reached_terminal=False,
                        isolation_broken=True,
                        isolation=isolation,
                        abort_message=(
                            "Operação interrompida: o isolamento de contenção "
                            "deixou de ser satisfeito durante a execução. Nenhum "
                            "novo comando foi disparado e os resultados parciais "
                            "foram preservados. " + isolation.summary()
                        ),
                    )

            state_result: OperationStateResult = self._caldera.get_operation(
                caldera_operation_id
            )

            # Reconcile this poll's links into the persisted rows. The audit seam
            # inside ``_reconcile`` may raise AuditPersistenceError (Req. 6.9): we
            # stop the loop here, keeping everything reconciled so far.
            try:
                latest = self._reconcile(
                    operation_id=operation_id,
                    links=state_result.links,
                    target_label=target_label,
                )
            except AuditPersistenceError as exc:
                # Preserve exactly the partials whose audit trail was recorded
                # before the failing write (Req. 6.9). ``_reconcile`` attaches the
                # results reconciled-and-audited so far to the exception; fall
                # back to the last fully-reconciled poll if it is absent.
                partials: tuple[AbilityRunResult, ...] = getattr(
                    exc, "reconciled_results", latest
                )
                success, failure = self._tally(partials)
                return _CollectionResult(
                    ability_results=partials,
                    total_success=success,
                    total_failure=failure,
                    reached_terminal=False,
                    aborted=True,
                    abort_message=(
                        "Operação abortada: a trilha de auditoria não pôde ser "
                        "registrada e a Operação foi interrompida para preservar "
                        f"a contenção. Detalhe: {exc}"
                    ),
                )

            state = (state_result.state or "").lower()
            if state in TERMINAL_OPERATION_STATES:
                # Terminal: ``latest`` already reflects the terminal links, so the
                # persisted count equals the number of terminal links (Req. 3.3).
                reached_terminal = True
                break

            # Not terminal yet — stop if the collection budget has elapsed,
            # otherwise sleep the clamped interval and poll again (Req. 3.1/3.2).
            if self._clock() >= deadline:
                reached_terminal = False
                break
            self._sleep(interval)

        success, failure = self._tally(latest)
        return _CollectionResult(
            ability_results=latest,
            total_success=success,
            total_failure=failure,
            reached_terminal=reached_terminal,
        )

    @staticmethod
    def _tally(results: Sequence[AbilityRunResult]) -> tuple[int, int]:
        """Count ``(sucesso, falha)`` across the per-Ability results."""
        success = sum(
            1 for r in results if r.status is AbilityResultStatus.SUCCESS
        )
        failure = sum(
            1 for r in results if r.status is AbilityResultStatus.FAILURE
        )
        return success, failure

    # -- incremental reconciliation (task 3.2) -------------------------------

    #: Monotonic rank of a per-Ability status along the lifecycle
    #: ``pendente -> em_execucao -> sucesso/falha``. Used by :meth:`_reconcile`
    #: to decide whether an observed link **advanced** past the persisted row:
    #: a reconciliation only updates a row when the observed rank is strictly
    #: greater than the persisted one, so a transient out-of-order/stale read
    #: never regresses a result that already reached a terminal state. The two
    #: terminal states (``sucesso``/``falha``) share the top rank: a terminal
    #: status is never overwritten by the other terminal status.
    _STATUS_RANK: dict[AbilityResultStatus, int] = {
        AbilityResultStatus.PENDING: 0,
        AbilityResultStatus.RUNNING: 1,
        AbilityResultStatus.SUCCESS: 2,
        AbilityResultStatus.FAILURE: 2,
    }

    def _reconcile(
        self,
        *,
        operation_id: int | None,
        links: Sequence[OperationLink],
        target_label: str = "host executor (comando local)",
    ) -> tuple[AbilityRunResult, ...]:
        """Upsert the persisted ``AbilityResult`` rows from the observed links.

        This is the reusable reconciliation step of the incremental collection
        loop (task 3.5 wires it into ``_execute``). For each :class:`OperationLink`
        observed in the current Caldera poll it:

        * **inserts** a new :class:`AbilityResult` when no row yet exists for this
          link (keyed by ``operation_id`` + ``ability_id``/``link_id``), or
        * **updates** the existing row **only when the link advanced** — i.e. the
          observed status rank is strictly greater than the persisted one
          (``pendente`` -> ``em_execucao`` -> ``sucesso``/``falha``). A terminal
          status is never regressed by a later stale/transient read.

        The link's ``command``, ``target_container`` (``target_label``) and
        ``attacker_host`` (``link.paw``) are mapped onto the additive nullable
        columns (Req. 2.4); values still uncollected stay ``NULL`` and the UI
        shows "não disponível" (Req. 2.5).

        The per-command audit seam is preserved: every command that is inserted
        or that advanced is recorded through :meth:`_audit_command`, keeping the
        audit-failure abort behaviour — an :class:`AuditPersistenceError` raised
        by the seam propagates to the caller, which aborts the Operation from
        that command onward (Req. 6.9). The audit row is written only after the
        ``AbilityResult`` is persisted, so a failing audit write never leaves an
        un-audited row behind for a command it could not record.

        Keying: rows are matched by ``(operation_id, ability_id)`` when the link
        carries an ``ability_id``; links without an ``ability_id`` fall back to a
        best-effort match that treats one ``NULL``-ability row per link as the
        same link (so repeated polls of the same ability-less link update in
        place rather than duplicating).

        Returns the current per-Ability view (one :class:`AbilityRunResult` per
        link, in the observed order) reflecting the reconciled state.
        """
        # Snapshot the already-persisted rows for this Operation so repeated
        # polls match in-place instead of inserting duplicates. Keyed by
        # ability_id; ability-less links are tracked positionally below.
        existing = (
            self._session.query(AbilityResult)
            .filter(AbilityResult.operation_id == operation_id)
            .all()
        )
        by_ability: dict[str, AbilityResult] = {
            row.ability_id: row for row in existing if row.ability_id is not None
        }
        # Rows with no ability_id, consumed in order for ability-less links.
        orphan_rows: list[AbilityResult] = [
            row for row in existing if row.ability_id is None
        ]

        results: list[AbilityRunResult] = []
        for index, link in enumerate(links):
            row = by_ability.get(link.ability_id) if link.ability_id else None
            if row is None and link.ability_id is None and index < len(orphan_rows):
                row = orphan_rows[index]

            observed_rank = self._STATUS_RANK[link.status]

            if row is None:
                # New link — insert the AbilityResult and audit the command.
                row = AbilityResult(
                    operation_id=operation_id,
                    ability_id=link.ability_id,
                    status=link.status,
                    command_output=link.output,
                    command=link.command,
                    target_container=target_label,
                    attacker_host=link.paw,
                )
                self._session.add(row)
                self._session.commit()
                if link.ability_id is not None:
                    by_ability[link.ability_id] = row
                advanced = True
            else:
                # Existing link — update only if it advanced past the persisted
                # status, so a stale read never regresses a terminal result.
                advanced = observed_rank > self._STATUS_RANK[row.status]
                if advanced:
                    row.status = link.status
                    row.command_output = link.output
                    row.command = link.command
                    row.target_container = target_label
                    row.attacker_host = link.paw
                    self._session.commit()

            result = AbilityRunResult(
                ability_id=link.ability_id,
                status=link.status,
                command=link.command,
                target_container=target_label,
                output=link.output,
            )

            # Preserve the per-command audit seam (Req. 6.9): record the command
            # when it is first seen or when it advanced. The ``AbilityResult`` row
            # is already persisted above, so a failing audit write leaves the row
            # behind for the command it could not record. A failing audit raises
            # AuditPersistenceError, which aborts the Operation from this command
            # onward. The result is only added to the **reported** view *after* a
            # successful audit, so the view reflects exactly the commands whose
            # audit trail was recorded; the partials audited so far are attached
            # to the exception so the collection loop can preserve them.
            if advanced:
                try:
                    self._audit_command(operation_id=operation_id, result=result)
                except AuditPersistenceError as exc:
                    exc.reconciled_results = tuple(results)  # type: ignore[attr-defined]
                    raise

            results.append(result)

        return tuple(results)

    # -- per-command handling (hook seam for task 7.5) -----------------------

    def _handle_command(
        self,
        *,
        operation_id: int | None,
        link: OperationLink,
        target_label: str,
    ) -> AbilityRunResult:
        """Persist one Ability's result and record its audit row (Req. 6.8, 4.5).

        This is the per-command step of the loop. It persists the
        :class:`AbilityResult`, then records the audit row through the
        :meth:`_audit_command` seam. If that seam raises
        :class:`AuditPersistenceError` (an audit write failed), the exception
        propagates to the loop in :meth:`_execute`, which aborts the Operation
        from this command onward (Req. 6.9).
        """
        result = AbilityRunResult(
            ability_id=link.ability_id,
            status=link.status,
            command=link.command,
            target_container=target_label,
            output=link.output,
        )

        # Persist the per-Ability result row (Req. 4.5 source data).
        self._session.add(
            AbilityResult(
                operation_id=operation_id,
                ability_id=link.ability_id,
                status=link.status,
                command_output=link.output,
            )
        )
        self._session.commit()

        # Record exactly one audit row for the executed command (Req. 6.8).
        self._audit_command(
            operation_id=operation_id,
            result=result,
        )
        return result

    def _audit_command(
        self,
        *,
        operation_id: int | None,
        result: AbilityRunResult,
    ) -> None:
        """Record one audit row for one executed command (the abort seam).

        Isolated as its own method so the audit-failure abort (task 7.5,
        Req. 6.9) has a single place to fail: an
        :class:`app.services.audit.AuditPersistenceError` raised here propagates
        to the per-command loop, which aborts the Operation from this command
        onward without touching the loop's shape. This method deliberately does
        **not** swallow the error.
        """
        result_text = self._format_result_text(result)
        self._audit.log_command(
            command=result.command or "(comando não informado)",
            target_container=result.target_container,
            result=result_text,
            operation_id=operation_id,
            ability_id=result.ability_id,
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _format_result_text(result: AbilityRunResult) -> str:
        """Compose the audit ``result`` text (status + optional output)."""
        status = result.status.value
        if result.output:
            return f"{status}: {result.output}"
        return status

    @staticmethod
    def _default_operation_name(campaign_id: str | None) -> str:
        """Derive a stable Operation name from the case slug (or a fallback)."""
        base = campaign_id or "operation"
        return f"pipeline-ui:{base}"

    @staticmethod
    def _ability_payloads(abilities: Sequence[object]) -> list[dict]:
        """Build the ``POST /api/v2/abilities`` payloads from the Abilities.

        Accepts SQLAlchemy ``Ability`` rows or duck-typed equivalents; sends the
        fields Caldera's v2 ability schema uses. Missing fields are simply
        omitted (Caldera fills its own defaults).
        """
        payloads: list[dict] = []
        for ability in abilities:
            payload: dict = {}
            ability_id = getattr(ability, "ability_id", None)
            if ability_id:
                payload["ability_id"] = ability_id
            for attr in (
                "name",
                "tactic",
                "technique_name",
                "technique_id",
                "description",
            ):
                value = getattr(ability, attr, None)
                if value is not None:
                    payload[attr] = value
            executors = getattr(ability, "executors", None)
            if executors:
                payload["executors"] = executors
            payloads.append(payload)
        return payloads

    def _derive_target_containers(self, abilities: Sequence[object]) -> list[str]:
        """Derive the distinct internal target containers from the Abilities.

        Reuses the preview resolver (task 4.6) to map each internal destination
        IP to its lab container, so the isolation pre-flight checks exactly the
        containers the commands actually target. Order is first-seen; external or
        unresolved destinations contribute nothing here (they are already handled
        by the destination containment gate).
        """
        # Lazy import to avoid the environment<->operation module import cycle:
        # topology_builder.HOST_CONTAINERS maps the logical host label
        # (e.g. "nginx", "db") to the real lab container_name the Docker Engine
        # knows (e.g. "sticks-nginx-target"), so the read-only isolation
        # pre-flight inspects the actual running containers.
        from app.services.environment.topology_builder import HOST_CONTAINERS

        preview = preview_abilities(abilities)
        seen: set[str] = set()
        ordered: list[str] = []
        for command in preview.commands:
            for container in command.target_containers:
                resolved = HOST_CONTAINERS.get(container, container)
                if resolved not in seen:
                    seen.add(resolved)
                    ordered.append(resolved)
        return ordered

    def _primary_target_label(
        self,
        abilities: Sequence[object],
        isolation: IsolationResult,
    ) -> str:
        """Pick a human-readable target label for the audit ``target_container``.

        Prefers the first resolved internal destination label from the preview
        (e.g. ``"nginx (192.168.20.30)"``, matching the design's audit convention).
        Falls back to the first verified container name, then to a neutral local
        label so the NOT NULL ``target_container`` is always populated.
        """
        preview = preview_abilities(abilities)
        for command in preview.commands:
            for dest in command.destinations:
                if not dest.is_external and dest.container is not None:
                    return dest.target_label
        if isolation.verdicts:
            return isolation.verdicts[0].container
        return "host executor (comando local)"


__all__ = [
    "OperationRunner",
    "OperationRunResult",
    "AbilityRunResult",
    "RunOutcome",
    "ContainmentValidator",
    "IsolationVerifier",
]
