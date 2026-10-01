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
   *if and only if* ``confirmado is True``. If confirmation is absent/false or
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
``total_sucesso`` / ``total_falha`` onto the :class:`Operation` (Req. 4.5), and
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
:data:`OperationState.ABORTED` and ``finalizada_em``) and returns a result with
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

_Requisitos: 6.6, 6.7, 4.5, 6.1, 6.2, 6.3, 6.4, 4.6_
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Sequence

from sqlalchemy.orm import Session

from app.models.domain import AbilityResult, Operation
from app.models.enums import AbilityResultStatus, OperationState
from app.services.audit import AuditLogger, AuditPersistenceError
from app.services.caldera import CalderaClient, OperationLink
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


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp, consistent with :class:`AuditLogger` and the models."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class RunOutcome(str, enum.Enum):
    """Why an Operation run ended the way it did (for precise upstream mapping).

    Subclasses ``str`` so it serializes to its value over JSON like the domain
    enums. These are *runner* outcomes; the persisted Operation state is the
    separate :class:`OperationState`.
    """

    #: ``confirmado`` was not True (absent/false/cancelled). Nothing executed;
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


@dataclass(frozen=True)
class AbilityRunResult:
    """The per-Ability outcome the runner produced and persisted.

    Attributes:
        ability_id: The Ability that executed (``None`` when Caldera did not
            associate the link to an ability).
        status: The derived :class:`AbilityResultStatus`
            ("pendente"/"em_execucao"/"sucesso"/"falha").
        command: The concrete command executed (decoded), if known.
        container_destino: The resolved target container label used for the audit
            row (e.g. ``"nginx (172.21.0.20)"``), or a neutral local label.
        output: The command's stdout/stderr, if collected.
    """

    ability_id: str | None
    status: AbilityResultStatus
    command: str | None
    container_destino: str
    output: str | None = None


@dataclass(frozen=True)
class OperationRunResult:
    """Typed result of a run: final state + aggregate + per-Ability results.

    Attributes:
        outcome: Why the run ended (:class:`RunOutcome`).
        state: The final persisted :class:`OperationState` of the Operation.
        operation_id: The local ``Operation.id`` (``None`` when no Operation row
            was created — i.e. an unconfirmed request that changed nothing).
        caldera_operation_id: The Caldera operation id, when one was created.
        total_sucesso: Aggregated count of "sucesso" Abilities (Req. 4.5).
        total_falha: Aggregated count of "falha" Abilities (Req. 4.5).
        ability_results: Per-Ability results, in execution order.
        containment: The containment verdict when a pre-flight ran (for messages).
        isolation: The isolation verdict when a pre-flight ran (for messages).
        message: Human-readable summary suitable for the UI / error body.
    """

    outcome: RunOutcome
    state: OperationState
    operation_id: int | None = None
    caldera_operation_id: str | None = None
    total_sucesso: int = 0
    total_falha: int = 0
    ability_results: tuple[AbilityRunResult, ...] = ()
    containment: OperationContainmentReport | None = None
    isolation: IsolationResult | None = None
    message: str = ""

    @property
    def started(self) -> bool:
        """True iff the Operation actually started executing on Caldera."""
        return self.outcome in (RunOutcome.COMPLETED, RunOutcome.ABORTED)


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
        """
        self._session = session
        self._caldera = caldera
        self._audit = audit_logger
        self._validate_containment = containment_validator
        self._verify_isolation = isolation_verifier
        self._inspector = isolation_inspector

    # -- public entry point --------------------------------------------------

    def run(
        self,
        *,
        caso_id: str | None,
        abilities: Sequence[object],
        adversary_id: str,
        confirmado: bool,
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
            caso_id: The curated case slug the Operation belongs to (for the row).
            abilities: The Abilities to run (typically the Adversary's set, in
                ``atomic_ordering``). Each exposes ``ability_id``/``name``/
                ``executors``.
            adversary_id: The Caldera Adversary profile id to execute.
            confirmado: The **explicit** confirmation flag. The Operation starts
                iff this is exactly ``True`` (Req. 6.6, 6.7).
            operation_name: Optional Caldera Operation name; a stable default is
                derived from ``caso_id`` when omitted.
            target_containers: The container names whose isolation is verified in
                the pre-flight. When omitted, they are derived from the Abilities'
                internal destinations via the preview resolver.

        Returns:
            An :class:`OperationRunResult` with the final state, the aggregate
            (``total_sucesso`` / ``total_falha``) and the per-Ability results.
        """
        # --- Gate 1: explicit confirmation (Req. 6.6, 6.7 — Property 6) -----
        # This runs before ANY pre-flight, Caldera call or DB write for the run:
        # an unconfirmed/cancelled request must change nothing and execute no
        # adversary command, returning to the prior (NOT_STARTED) state.
        if confirmado is not True:
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

        # All gates passed — start and conduct the Operation.
        return self._execute(
            caso_id=caso_id,
            abilities=abilities,
            adversary_id=adversary_id,
            operation_name=operation_name,
            containment=containment,
            isolation=isolation,
            adversary_payload=adversary_payload,
        )

    # -- execution (post-confirmation, post-pre-flight) ----------------------

    def _execute(
        self,
        *,
        caso_id: str | None,
        abilities: Sequence[object],
        adversary_id: str,
        operation_name: str | None,
        containment: OperationContainmentReport,
        isolation: IsolationResult,
        adversary_payload: object | None = None,
    ) -> OperationRunResult:
        """Create the Caldera Operation, run the loop and aggregate results.

        Persists the Operation state transitions
        (NOT_STARTED -> RUNNING -> FINISHED) and one :class:`AbilityResult` per
        Ability, aggregates the totals (Req. 4.5), and records one audit row per
        executed command (Req. 6.8) via the hook-friendly per-command loop.
        """
        name = operation_name or self._default_operation_name(caso_id)

        # Persist the Operation as RUNNING with its start instant, so the state
        # transition is durable and observable before any command runs.
        operation = Operation(
            caso_id=caso_id,
            estado=OperationState.RUNNING,
            total_sucesso=0,
            total_falha=0,
            iniciada_em=_utcnow(),
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

        # Poll the Operation until it reaches a terminal state (Req. 4.3/4.4).
        # A real Operation needs time for each link to dispatch, beacon and
        # collect; a single immediate poll would see an empty chain. The mocked
        # dev/CI flow returns `finished` immediately, so the wait is a no-op
        # there. The WebSocket channel (task 11.3) drives live progress updates.
        state_result = self._caldera.poll_until_complete(created.operation_id)
        target_label = self._primary_target_label(abilities, isolation)

        ability_results: list[AbilityRunResult] = []
        total_sucesso = 0
        total_falha = 0
        aborted = False
        abort_message: str | None = None

        for link in state_result.links:
            # Audit-failure abort seam (task 7.5 — Req. 6.9): ``_handle_command``
            # persists this command's AbilityResult and then records its audit
            # row. If persisting the AuditLogEntry fails, it raises
            # ``AuditPersistenceError``; we abort **from this point on** — the
            # loop breaks so NO subsequent command is processed, no further
            # AbilityResult/audit row is written, and the Operation is marked
            # ABORTED below. Commands already executed keep their persisted
            # AbilityResults, so the aggregate reflects exactly what ran up to
            # the failing audit write.
            try:
                result = self._handle_command(
                    operation_id=operation.id,
                    link=link,
                    target_destino=target_label,
                )
            except AuditPersistenceError as exc:
                aborted = True
                abort_message = (
                    "Operação abortada: a trilha de auditoria não pôde ser "
                    "registrada e a Operação foi interrompida para preservar a "
                    f"contenção. Detalhe: {exc}"
                )
                break

            ability_results.append(result)
            if result.status is AbilityResultStatus.SUCCESS:
                total_sucesso += 1
            elif result.status is AbilityResultStatus.FAILURE:
                total_falha += 1

        # Aggregate the totals onto the Operation and finalize its state (Req. 4.5).
        # On an audit-failure abort, the aggregate reflects only the commands that
        # ran (and were audited) before the failing write.
        operation.total_sucesso = total_sucesso
        operation.total_falha = total_falha
        operation.estado = (
            OperationState.ABORTED if aborted else OperationState.FINISHED
        )
        operation.finalizada_em = _utcnow()
        self._session.commit()

        return OperationRunResult(
            outcome=RunOutcome.ABORTED if aborted else RunOutcome.COMPLETED,
            state=operation.estado,
            operation_id=operation.id,
            caldera_operation_id=operation.caldera_operation_id,
            total_sucesso=total_sucesso,
            total_falha=total_falha,
            ability_results=tuple(ability_results),
            containment=containment,
            isolation=isolation,
            message=(
                abort_message
                if aborted
                else (
                    f"Operação finalizada: {total_sucesso} sucesso(s), "
                    f"{total_falha} falha(s)."
                )
            ),
        )

    # -- per-command handling (hook seam for task 7.5) -----------------------

    def _handle_command(
        self,
        *,
        operation_id: int | None,
        link: OperationLink,
        target_destino: str,
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
            container_destino=target_destino,
            output=link.output,
        )

        # Persist the per-Ability result row (Req. 4.5 source data).
        self._session.add(
            AbilityResult(
                operacao_id=operation_id,
                ability_id=link.ability_id,
                status=link.status,
                saida_comando=link.output,
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
        resultado = self._format_result_text(result)
        self._audit.log_command(
            comando=result.command or "(comando não informado)",
            container_destino=result.container_destino,
            resultado=resultado,
            operacao_id=operation_id,
            ability_id=result.ability_id,
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _format_result_text(result: AbilityRunResult) -> str:
        """Compose the audit ``resultado`` text (status + optional output)."""
        status = result.status.value
        if result.output:
            return f"{status}: {result.output}"
        return status

    @staticmethod
    def _default_operation_name(caso_id: str | None) -> str:
        """Derive a stable Operation name from the case slug (or a fallback)."""
        base = caso_id or "operacao"
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
        preview = preview_abilities(abilities)
        seen: set[str] = set()
        ordered: list[str] = []
        for command in preview.commands:
            for container in command.target_containers:
                if container not in seen:
                    seen.add(container)
                    ordered.append(container)
        return ordered

    def _primary_target_label(
        self,
        abilities: Sequence[object],
        isolation: IsolationResult,
    ) -> str:
        """Pick a human-readable target label for the audit ``container_destino``.

        Prefers the first resolved internal destination label from the preview
        (e.g. ``"nginx (172.21.0.20)"``, matching the design's audit convention).
        Falls back to the first verified container name, then to a neutral local
        label so the NOT NULL ``container_destino`` is always populated.
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
