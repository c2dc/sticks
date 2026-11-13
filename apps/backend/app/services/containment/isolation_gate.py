"""Isolation_Gate — the Network_Boundary between ``environment`` and ``execution``.

This module formalizes the contract the design describes under "Components and
Interfaces" → *Isolation_Gate / Network_Boundary* (Req. 4). It sits between the
``environment`` phase (which provisions the lab with external egress **allowed**,
automatically — Req. 4.1) and the ``execution`` phase (which runs the offensive
commands with **no external egress**, only the Internal_Network — Req. 4.3).

The gate is deliberately thin and reuses the project's existing containment
primitives rather than reinventing them:

- :func:`~app.services.environment.egress.egress_lost` over a batch of
  :class:`~app.services.environment.egress.EgressVerdict` is the *positive,
  fail-closed* verification that external egress was actually cut.
- :func:`~app.services.containment.subnets.is_internal_ip` is the single source
  of truth that validates every command destination during ``execution``
  (Req. 4.3) — the gate exposes it as :func:`validate_destination` so callers
  do not re-implement the internal/external verdict.

Contract (design §3)
--------------------
- ``isolate(run)`` — once provisioning completes, cut external egress, record an
  **auditable event** on the run's trail, and return an :class:`IsolationProof`
  (Req. 4.2, 4.5).
- ``require_isolation(run)`` — block the start of ``execution`` when the run has
  no valid :class:`IsolationProof` (Req. 4.4), raising :class:`IsolationRequired`.

What isolation is — and is NOT
------------------------------
Isolation is **cutting external egress while preserving the Internal_Network**
(Caldera ↔ targets), NOT a full network cut (design DD3, Req. 4.6). The gate
therefore keeps the internal lab networks active and only proves the loss of
external reachability. Destinations are validated with :func:`is_internal_ip`:
an internal destination is accepted, an external one is rejected (Req. 4.3).

Auditable trail
---------------
State changes are recorded through an injectable :class:`IsolationAuditSink`
(Req. 4.5). Production wires a sink backed by the audit trail; tests inject a
recording fake. The event is recorded **whether isolation succeeds or fails**,
so a blocked ``execution`` is itself auditable (design Error Handling).

This module performs no network access and no Docker calls of its own: all
verification arrives as already-computed :class:`EgressVerdict` values (produced
by the read-only ``EgressProber`` seam), so it is import- and unit-test-safe on a
machine without a Docker daemon.

_Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.services.containment.subnets import is_internal_ip

if TYPE_CHECKING:  # pragma: no cover - typing only, no import-time cost
    # ``egress`` lives in the ``environment`` package, which imports back into the
    # ``containment`` package at import time; importing it eagerly here would form
    # a circular import. The runtime use is a lazy import inside :func:`isolate`
    # (the same convention the package's other seams follow), so this guarded
    # import only feeds the type checker.
    from app.services.environment.egress import EgressVerdict

__all__ = [
    "IsolationAuditSink",
    "IsolationEvent",
    "IsolationProof",
    "IsolationRequired",
    "RecordingIsolationAuditSink",
    "isolate",
    "require_isolation",
    "validate_destination",
]


def _utcnow() -> dt.datetime:
    """Return the current naive UTC timestamp.

    Naive UTC keeps the isolation trail consistent with the rest of the audit
    trail (:func:`app.services.audit.audit_logger._utcnow`), round-tripping
    identically on PostgreSQL and the ephemeral test SQLite.
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Proof + auditable event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IsolationProof:
    """Immutable proof that external egress was cut for a run (Req. 4.2).

    An :class:`IsolationProof` is the token ``require_isolation`` checks before
    allowing ``execution`` to start (Req. 4.4). It is produced only by
    :func:`isolate` and only when external egress was **positively verified** as
    lost for every target (fail-closed via
    :func:`~app.services.environment.egress.egress_lost`), so holding a proof
    means the Network_Boundary is in place.

    Attributes:
        run_id: The run the proof belongs to (an ``AgentRun`` id / thread key).
        isolated: ``True`` iff external egress was cut for every target. Only a
            proof with ``isolated=True`` is **valid** (see :attr:`is_valid`).
        proven_at: When the proof was produced (naive UTC).
        targets: The target containers the proof covers, in input order.
        reason: A human-readable cause when isolation was **not** achieved
            (``isolated=False``); ``None`` on success.
    """

    run_id: str
    isolated: bool
    proven_at: dt.datetime = field(default_factory=_utcnow)
    targets: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def is_valid(self) -> bool:
        """``True`` iff this proof attests that external egress was cut.

        A proof is valid only when :attr:`isolated` is ``True``. An invalid proof
        (isolation failed) never approves the start of ``execution``.
        """
        return self.isolated


@dataclass(frozen=True)
class IsolationEvent:
    """One auditable entry on a run's isolation trail (Req. 4.5).

    Recorded by :func:`isolate` on **every** isolation transition — success or
    failure — so the boundary crossing is always traceable, including a blocked
    ``execution`` (design Error Handling).

    Attributes:
        run_id: The run the event belongs to.
        isolated: The isolation verdict recorded by this event.
        recorded_at: When the event was recorded (naive UTC).
        detail: A human-readable description of the transition.
    """

    run_id: str
    isolated: bool
    recorded_at: dt.datetime = field(default_factory=_utcnow)
    detail: str = ""


@runtime_checkable
class IsolationAuditSink(Protocol):
    """Injectable sink that records :class:`IsolationEvent` to an audit trail.

    A ``@runtime_checkable Protocol`` — like the package's other injectable seams
    (``ContainerInspector``, ``EgressProber``) — so any object exposing
    ``record`` with this signature satisfies it structurally. Production wires a
    sink backed by the audit trail (associated with the run); tests inject a
    recording fake (:class:`RecordingIsolationAuditSink`).
    """

    def record(self, event: IsolationEvent) -> None:
        """Persist/append one isolation event to the run's auditable trail."""
        ...


class RecordingIsolationAuditSink:
    """In-memory :class:`IsolationAuditSink` that keeps events for inspection.

    The default sink used when a caller does not inject one, and the fake used by
    the tests: it simply appends each :class:`IsolationEvent` to :attr:`events`,
    preserving order, so the auditable trail can be asserted without a database.
    """

    def __init__(self) -> None:
        self.events: list[IsolationEvent] = []

    def record(self, event: IsolationEvent) -> None:
        """Append ``event`` to :attr:`events` in order."""
        self.events.append(event)


# ---------------------------------------------------------------------------
# Gate failure
# ---------------------------------------------------------------------------


class IsolationRequired(RuntimeError):
    """Raised by :func:`require_isolation` when a run has no valid proof (Req. 4.4).

    Blocks the start of ``execution`` before any offensive command runs when the
    Isolation_Gate has not confirmed that external egress was cut. Carries the
    ``run_id`` so the caller can surface a contained, auditable failure.
    """

    def __init__(self, run_id: str, *, reason: str | None = None) -> None:
        message = (
            f"execution bloqueada: run {run_id!r} não possui prova de isolamento "
            f"válida (egresso externo não confirmadamente cortado)"
        )
        if reason:
            message = f"{message} — {reason}"
        super().__init__(message)
        self.run_id = run_id
        self.reason = reason


# ---------------------------------------------------------------------------
# Destination validation (Req. 4.3) — reuses is_internal_ip verbatim
# ---------------------------------------------------------------------------


def validate_destination(destination: str) -> bool:
    """Return ``True`` iff ``destination`` is an internal (contained) destination.

    Thin, explicit re-export of
    :func:`~app.services.containment.subnets.is_internal_ip` so the ``execution``
    phase validates every command destination through the Isolation_Gate without
    re-implementing the internal/external verdict (Req. 4.3). Under isolation an
    internal destination (Caldera ↔ targets on the Internal_Network) is accepted
    and any external destination is rejected — isolation cuts external egress but
    keeps the internal network, so internal traffic must still be allowed
    (design DD3).

    Args:
        destination: An IPv4 literal (or any string) extracted from a command.

    Returns:
        ``True`` when ``destination`` is inside a known internal lab subnet;
        ``False`` for any external or non-IP destination.
    """
    return is_internal_ip(destination)


# ---------------------------------------------------------------------------
# Gate: isolate + require_isolation
# ---------------------------------------------------------------------------


def isolate(
    run_id: str,
    *,
    egress_verdicts: list[EgressVerdict],
    targets: list[str] | None = None,
    audit_sink: IsolationAuditSink | None = None,
) -> IsolationProof:
    """Cut external egress for a run, record an auditable event, return a proof.

    The gate crossing between ``environment`` and ``execution`` (Req. 4.2). It is
    called once provisioning completes, with the per-target egress verdicts the
    read-only :class:`~app.services.environment.egress.EgressProber` produced. It:

    1. **Proves egress loss** — ``isolated`` is ``True`` iff
       :func:`~app.services.environment.egress.egress_lost` approves the batch:
       every target lost egress **conclusively** (fail-closed — an empty batch or
       any inconclusive/still-reachable target yields ``isolated=False``).
    2. **Records an auditable event** — an :class:`IsolationEvent` is recorded on
       the sink whether isolation succeeded or failed (Req. 4.5), so a blocked
       ``execution`` is itself traceable.
    3. **Returns a proof** — an :class:`IsolationProof`. Only a proof with
       ``isolated=True`` is valid and will let ``require_isolation`` pass.

    The Internal_Network is never touched here: isolation is the cut of
    *external* egress only (design DD3, Req. 4.6). This function performs no
    network or Docker access — all verification arrives as pre-computed verdicts.

    Args:
        run_id: The run being isolated (an ``AgentRun`` id / thread key).
        egress_verdicts: The per-target egress verdicts from the read-only probe.
        targets: The target container names the proof covers (for the record and
            the proof). Defaults to the containers named in ``egress_verdicts``.
        audit_sink: Where to record the :class:`IsolationEvent`. Defaults to a
            fresh :class:`RecordingIsolationAuditSink` (the event is still
            recorded; the caller just will not see it unless it injects one).

    Returns:
        The :class:`IsolationProof` for the run (valid iff egress was cut).
    """
    # Lazy import: ``egress`` lives in the ``environment`` package, which imports
    # the ``containment`` package at import time. Importing it at module top level
    # would create a circular import; importing it here (the package's lazy
    # convention) keeps this module import-safe.
    from app.services.environment.egress import egress_lost

    sink = audit_sink if audit_sink is not None else RecordingIsolationAuditSink()
    covered = tuple(targets) if targets is not None else tuple(v.container for v in egress_verdicts)

    isolated = egress_lost(egress_verdicts)
    if isolated:
        reason: str | None = None
        detail = (
            f"egresso externo cortado e verificado para {len(covered)} alvo(s); "
            f"rede interna preservada"
        )
    else:
        # Compose the cause from the batch so the blocked transition is legible.
        offenders = [
            v.reason or v.container for v in egress_verdicts if not (v.egress_lost and v.conclusive)
        ]
        if not egress_verdicts:
            reason = "nenhum alvo verificado (fail-closed)"
        else:
            reason = "egresso externo não confirmadamente cortado: " + "; ".join(offenders)
        detail = f"isolamento NÃO confirmado — {reason}"

    sink.record(IsolationEvent(run_id=run_id, isolated=isolated, detail=detail))

    return IsolationProof(
        run_id=run_id,
        isolated=isolated,
        targets=covered,
        reason=reason,
    )


def require_isolation(run_id: str, proof: IsolationProof | None) -> IsolationProof:
    """Block ``execution`` unless the run holds a valid :class:`IsolationProof`.

    The guard ``execution`` calls before running any offensive command (Req. 4.4):
    it raises :class:`IsolationRequired` when ``proof`` is missing, belongs to a
    different run, or attests that isolation was **not** achieved
    (``proof.is_valid`` is ``False``). On success it returns the validated proof
    so the caller can thread it forward.

    Args:
        run_id: The run about to start ``execution``.
        proof: The :class:`IsolationProof` produced by :func:`isolate`, or
            ``None`` when the gate was never crossed.

    Returns:
        The validated :class:`IsolationProof`.

    Raises:
        IsolationRequired: when there is no valid proof for ``run_id`` — the
            start of ``execution`` is blocked.
    """
    if proof is None:
        raise IsolationRequired(run_id, reason="nenhuma prova de isolamento")
    if proof.run_id != run_id:
        raise IsolationRequired(
            run_id,
            reason=(f"prova pertence a outro run ({proof.run_id!r})"),
        )
    if not proof.is_valid:
        raise IsolationRequired(run_id, reason=proof.reason)
    return proof
