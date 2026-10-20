"""Deterministic online/offline split of the curated target setup (Stage 5.1/5.2).

Stage 5.1 (Build/online) needs to know **what** to run with an egress window
open, and Stage 5.2 (Runtime/isolated) needs the remainder to run with no
internet. The curated setup scripts (e.g. ``shadowray_suta.sh``) are
**monolithic**: a single file mixes ``apt-get install``/``pip3 install``
(online) with ``useradd``, payload writing (``cat > ... << EOF``) and service
starts (``sshd &``) — all offline.

This module provides the **pure, deterministic, network-free** classification
that the ``TargetSetup`` seam (task 5) and the orchestration (task 7) consume:

``SetupPhase``
    The phase a command belongs to — :attr:`SetupPhase.ONLINE` (needs the
    internet, runs inside the egress window of 5.1) or :attr:`SetupPhase.OFFLINE`
    (no internet, runs in the isolated window of 5.2).

``ClassifiedCommand``
    A single curated command paired with its classified :class:`SetupPhase`
    (*frozen* ``dataclass``).

``classify_commands(vulnerabilities)``
    Classifies each command of a host's ``vulnerabilities`` by **stable regex
    patterns**, command by command. A command that matches **no** online pattern
    is classified ``OFFLINE`` by default — the safe default: the worst case of a
    misclassification is attempting to run offline something that would need the
    internet, which **fails safely and contained** (it never opens undue egress),
    preserving the containment invariant even with an imperfect heuristic.

Online patterns (stable regex, from the design "Split online/offline"):

- ``apt``/``apt-get`` ``install``/``update``
- ``pip``/``pip3`` ``install``
- ``wget``
- ``curl`` with ``-O``/``-o``/``--output`` (an explicit download)
- ``git clone``
- an explicit binary download (``https?://`` followed by writing to a file)

Everything else defaults to ``OFFLINE``.

_Requirements: 2.2, 5.1_
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol, runtime_checkable

from app.services.environment.compose_executor import (
    CommandResult,
    LogLine,
)


class SetupPhase(str, Enum):
    """The phase a curated setup command belongs to (Req. 2.2, 5.1).

    ``ONLINE`` commands need the internet (download/install) and run inside the
    controlled egress window of sub-stage 5.1; ``OFFLINE`` commands do **not**
    need the internet (users, payloads, service starts) and run in the isolated
    window of sub-stage 5.2.
    """

    ONLINE = "online"
    OFFLINE = "offline"


@dataclass(frozen=True)
class ClassifiedCommand:
    """A single curated command paired with its classified phase.

    *Frozen* (immutable): :func:`classify_commands` produces a list of these in
    the same order as the input, and the ``TargetSetup`` seam runs only the ones
    matching the requested :class:`SetupPhase`.

    Attributes:
        command: The curated command line, verbatim.
        phase: The :class:`SetupPhase` it was classified into.
    """

    command: str
    phase: SetupPhase


# ---------------------------------------------------------------------------
# Stable ONLINE patterns (compiled once; pure, no network)
# ---------------------------------------------------------------------------

# Each pattern matches a command that REQUIRES the internet. A command matching
# none of these defaults to OFFLINE (safe default). The patterns are backend
# constants — never interpolated from user input.
_ONLINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # apt / apt-get install|update
    re.compile(r"\bapt(?:-get)?\s+(?:install|update)\b"),
    # pip / pip3 install
    re.compile(r"\bpip3?\s+install\b"),
    # wget
    re.compile(r"\bwget\b"),
    # curl with an explicit output flag (a download, not a probe)
    re.compile(r"\bcurl\b.*\b(?:-O|-o|--output)\b"),
    # git clone
    re.compile(r"\bgit\s+clone\b"),
    # explicit binary download: an http(s) URL followed by writing to a file
    re.compile(r"https?://\S+.*(?:>\s*\S+|\b-O\b|\b-o\b|--output)"),
)


def _is_online(command: str) -> bool:
    """Return ``True`` when ``command`` matches any stable ONLINE pattern.

    Pure and deterministic: a simple regex scan, no network, no side effects.
    """
    return any(pattern.search(command) for pattern in _ONLINE_PATTERNS)


def classify_commands(vulnerabilities: list[str]) -> list[ClassifiedCommand]:
    """Classify each command of a host into ``ONLINE`` or ``OFFLINE`` (Req. 2.2, 5.1).

    Pure and deterministic (same input ⇒ same output), with no network access.
    Each command is matched against the stable ONLINE patterns; a command that
    matches none is classified ``OFFLINE`` by default (safe default — see module
    docstring). Order is preserved: the returned list mirrors ``vulnerabilities``
    one-to-one.

    Args:
        vulnerabilities: The curated setup commands of a target host (the
            ``target_vuln_host_*`` lines derived from the DAG node).

    Returns:
        A list of :class:`ClassifiedCommand`, in the same order as the input.
    """
    return [
        ClassifiedCommand(
            command=command,
            phase=SetupPhase.ONLINE if _is_online(command) else SetupPhase.OFFLINE,
        )
        for command in vulnerabilities
    ]


# ---------------------------------------------------------------------------
# Setup application seam (task 5) — run the curated setup per phase
# ---------------------------------------------------------------------------


@runtime_checkable
class TargetSetup(Protocol):
    """Apply the curated target setup for a single phase (injection point — Req. 2.3, 3.3).

    A ``@runtime_checkable Protocol``, following the precedent of the package's
    other injectable seams (``ComposeExecutor``, ``CalderaLoader``,
    ``ContainerInspector``): any object exposing ``apply`` with this signature
    satisfies the protocol structurally (duck typing), without explicit
    inheritance. The tests inject a *fake* that records the ``exec_in`` calls (no
    Docker daemon); production injects :class:`DockerTargetSetup`.

    The orchestration (task 7) calls :meth:`apply` **twice** per host — once with
    :attr:`SetupPhase.ONLINE` inside the egress window of sub-stage 5.1, and once
    with :attr:`SetupPhase.OFFLINE` in the isolated window of sub-stage 5.2 —
    passing the **same** classified list each time. The implementation runs only
    the commands whose :attr:`ClassifiedCommand.phase` matches ``phase``.
    """

    def apply(
        self,
        container: str,
        commands: list[ClassifiedCommand],
        phase: SetupPhase,
        *,
        on_log: Callable[[LogLine], None],
    ) -> CommandResult:
        """Run, in order, only the ``commands`` of ``phase`` against ``container``.

        Each matching command runs via ``ComposeExecutor.exec_in`` in the
        argument-list form (no shell at this layer); on the **first** command that
        returns ``code != 0`` the application stops and that non-zero code
        propagates (Req. 3.3). Non-matching commands are skipped. Each emitted
        :class:`LogLine` is pushed through ``on_log`` in order.

        Args:
            container: The target container the setup is applied in.
            commands: The classified setup commands (both phases); only those whose
                phase equals ``phase`` are run.
            phase: The :class:`SetupPhase` to apply (``ONLINE`` for 5.1, ``OFFLINE``
                for 5.2).
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            A :class:`CommandResult`: ``code == 0`` when every matching command
            succeeded (or none matched), otherwise the non-zero code of the first
            failing command. ``lines`` carries the emitted lines, in order.
        """
        ...


# ---------------------------------------------------------------------------
# Production adapter (task 5)
# ---------------------------------------------------------------------------


class DockerTargetSetup:
    """Production implementation of the :class:`TargetSetup` seam (task 5).

    Delegates to an injected :class:`ComposeExecutor`, running each phase-matching
    command via its ``exec_in`` operation. The curated command line is passed as
    ``["bash", "-lc", <command>]`` — a fixed three-element argument list where only
    the **last** element is the curated command, so the ``bash``/``-lc`` wrapper is
    never interpolated from user input (the curated commands themselves run through
    a login shell inside the container, which the monolithic setup scripts expect).

    It runs the matching commands **in order** and returns on the first failure
    (``code != 0``), aggregating every emitted :class:`LogLine` into the returned
    :class:`CommandResult`. When no command matches the requested phase it returns
    ``CommandResult(code=0, lines=[])`` — an empty phase is a success, not a
    failure.

    It satisfies :class:`TargetSetup` structurally (it exposes ``apply`` with the
    protocol's signature), without explicit inheritance.

    _Requirements: 2.3, 3.3_
    """

    def __init__(self, executor: "_ExecInExecutor") -> None:
        """Assemble the adapter with the injected ``ComposeExecutor``.

        Args:
            executor: The :class:`ComposeExecutor` (extended with ``exec_in``) used
                to run each setup command inside the target container. Injectable: a
                *fake* in the tests, the real ``DockerComposeExecutor`` in production.
        """
        self._executor = executor

    def apply(
        self,
        container: str,
        commands: list[ClassifiedCommand],
        phase: SetupPhase,
        *,
        on_log: Callable[[LogLine], None],
        best_effort: bool = False,
    ) -> CommandResult:
        """Run only the ``commands`` of ``phase``, in order, via ``exec_in``.

        See :meth:`TargetSetup.apply`. By default (``best_effort=False``) it stops
        and propagates the non-zero code of the **first** failing command (Req.
        3.3); skips non-matching commands; aggregates the emitted lines.

        ``best_effort=True`` — used by the Stage-5 orchestration when applying the
        curated per-host setup — makes a non-zero command **non-fatal**: the setup
        of a lab target is inherently idempotent/best-effort (the curated
        ``*_suta.sh`` scripts themselves use ``|| true``), and a host aggregates
        the ``target_vuln`` of several DAG nodes, so redundant steps collide
        benignly (e.g. ``useradd attacker`` when the user already exists → exit 9,
        ``apt-get install`` of an already-present package, a service already
        listening). Treating those as fatal would abort provisioning against a
        target that is, in fact, correctly prepared. In this mode each failing
        command is logged as an ``info`` note and the run continues; the aggregate
        ``code`` stays ``0`` (the phase as a whole succeeded in getting the target
        into the desired state). Genuinely blocking problems surface downstream
        (health/egress/isolation gates, and the Execution itself).
        """
        lines: list[LogLine] = []
        for classified in commands:
            if classified.phase is not phase:
                continue
            result = self._executor.exec_in(
                container,
                ["bash", "-lc", classified.command],
                on_log=on_log,
            )
            lines.extend(result.lines)
            if result.code != 0:
                if not best_effort:
                    # Strict mode: stop and propagate the non-zero code (Req. 3.3).
                    return CommandResult(code=result.code, lines=lines)
                # Best-effort mode: a redundant/benign setup failure is non-fatal;
                # note it and continue so the whole curated setup is applied.
                note = LogLine(
                    step="setup",
                    stream="info",
                    text=(
                        f"setup (best-effort): comando retornou code={result.code}; "
                        f"seguindo (passo idempotente/redundante)"
                    ),
                )
                lines.append(note)
                on_log(note)
        return CommandResult(code=0, lines=lines)


class _ExecInExecutor(Protocol):
    """Minimal structural view of the ``exec_in`` surface :class:`DockerTargetSetup` uses.

    The full :class:`ComposeExecutor` protocol (extended in task 2) declares
    ``exec_in(container, argv, *, on_log) -> CommandResult`` alongside ``up``/
    ``down``/network operations. :class:`DockerTargetSetup` only ever calls
    ``exec_in``, so this narrow Protocol documents (and type-checks) exactly that
    dependency without coupling the adapter to the rest of the executor surface.
    """

    def exec_in(
        self,
        container: str,
        argv: list[str],
        *,
        on_log: Callable[[LogLine], None],
    ) -> CommandResult:
        ...


__all__ = [
    "SetupPhase",
    "ClassifiedCommand",
    "classify_commands",
    "TargetSetup",
    "DockerTargetSetup",
]
