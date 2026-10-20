"""Example test for the per-phase setup application (task 5.1).

Validates: Requirements 3.3 — ``TargetSetup.apply`` runs, **in order**, only the
commands of the requested :class:`SetupPhase` via ``ComposeExecutor.exec_in``,
and propagates ``code != 0`` on the first failing command.

Given a single classified list (a mix of ONLINE and OFFLINE commands):

- ``apply(ONLINE)`` runs **only** the ONLINE commands, in order (the OFFLINE ones
  are skipped); ``apply(OFFLINE)`` runs **only** the OFFLINE ones, in order.
- Each curated command reaches ``exec_in`` as ``["bash", "-lc", <command>]``
  (argument-list form — the wrapper is fixed, only the last element is curated).
- A failure of a matching command (``code != 0``) stops the run at that command
  and propagates the non-zero code; subsequent matching commands do **not** run.

The ``ComposeExecutor`` is a **fake** that records every ``exec_in`` call (no
Docker daemon, no subprocess, no shell); the production
``DockerComposeExecutor.exec_in`` is not touched here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.services.environment.compose_executor import CommandResult, LogLine
from app.services.environment.target_setup import (
    ClassifiedCommand,
    DockerTargetSetup,
    SetupPhase,
)


# ---------------------------------------------------------------------------
# Fake ComposeExecutor recording exec_in calls (no Docker daemon, no shell)
# ---------------------------------------------------------------------------


@dataclass
class _ExecCall:
    """One recorded ``exec_in`` invocation: the container and the argv passed."""

    container: str
    argv: list[str]


class _FakeComposeExecutor:
    """Records each ``exec_in`` call and returns scripted exit codes.

    Satisfies the ``exec_in`` surface structurally: it exposes ``exec_in`` with
    the protocol signature and nothing else is needed by ``DockerTargetSetup``.
    Each call emits a ``LogLine`` through ``on_log`` (so the aggregation path is
    exercised) and returns a :class:`CommandResult` whose ``code`` is taken from
    ``codes`` in order (default ``0`` once exhausted).
    """

    def __init__(self, codes: list[int] | None = None) -> None:
        self.calls: list[_ExecCall] = []
        self._codes = list(codes or [])

    def exec_in(
        self,
        container: str,
        argv: list[str],
        *,
        on_log: Callable[[LogLine], None],
    ) -> CommandResult:
        self.calls.append(_ExecCall(container=container, argv=list(argv)))
        code = self._codes.pop(0) if self._codes else 0
        line = LogLine(step="setup", stream="comando", text=" ".join(argv))
        on_log(line)
        return CommandResult(code=code, lines=[line])


# ---------------------------------------------------------------------------
# A single classified list reused across the tests (mix of both phases)
# ---------------------------------------------------------------------------


def _mixed_commands() -> list[ClassifiedCommand]:
    """A classified list interleaving ONLINE and OFFLINE commands, in order."""
    return [
        ClassifiedCommand("apt-get install -y curl", SetupPhase.ONLINE),
        ClassifiedCommand("useradd -m attacker", SetupPhase.OFFLINE),
        ClassifiedCommand("pip3 install flask", SetupPhase.ONLINE),
        ClassifiedCommand("chmod +x /opt/payload.sh", SetupPhase.OFFLINE),
        ClassifiedCommand('pgrep -x sshd || /usr/sbin/sshd &', SetupPhase.OFFLINE),
    ]


def _texts(calls: list[_ExecCall]) -> list[str]:
    """The curated command (last argv element) of each recorded call, in order."""
    return [call.argv[-1] for call in calls]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_apply_online_runs_only_online_commands_in_order() -> None:
    """apply(ONLINE) runs only the ONLINE commands, in order; OFFLINE skipped."""
    fake = _FakeComposeExecutor()
    setup = DockerTargetSetup(fake)

    result = setup.apply(
        "nginx-target", _mixed_commands(), SetupPhase.ONLINE, on_log=lambda _l: None
    )

    assert result.code == 0
    assert _texts(fake.calls) == ["apt-get install -y curl", "pip3 install flask"]


def test_apply_offline_runs_only_offline_commands_in_order() -> None:
    """apply(OFFLINE) runs only the OFFLINE commands, in order; ONLINE skipped."""
    fake = _FakeComposeExecutor()
    setup = DockerTargetSetup(fake)

    result = setup.apply(
        "nginx-target", _mixed_commands(), SetupPhase.OFFLINE, on_log=lambda _l: None
    )

    assert result.code == 0
    assert _texts(fake.calls) == [
        "useradd -m attacker",
        "chmod +x /opt/payload.sh",
        'pgrep -x sshd || /usr/sbin/sshd &',
    ]


def test_apply_wraps_each_command_as_bash_lc_argv() -> None:
    """Each curated command reaches exec_in as ['bash', '-lc', <command>]."""
    fake = _FakeComposeExecutor()
    setup = DockerTargetSetup(fake)

    setup.apply(
        "nginx-target", _mixed_commands(), SetupPhase.ONLINE, on_log=lambda _l: None
    )

    assert fake.calls[0].container == "nginx-target"
    assert fake.calls[0].argv == ["bash", "-lc", "apt-get install -y curl"]
    assert fake.calls[1].argv == ["bash", "-lc", "pip3 install flask"]


def test_apply_propagates_nonzero_code_on_first_failure_and_stops() -> None:
    """A failing matching command stops the run and propagates code != 0."""
    # First ONLINE command fails (code 7); the second must NOT run.
    fake = _FakeComposeExecutor(codes=[7])
    setup = DockerTargetSetup(fake)

    result = setup.apply(
        "nginx-target", _mixed_commands(), SetupPhase.ONLINE, on_log=lambda _l: None
    )

    assert result.code == 7
    assert _texts(fake.calls) == ["apt-get install -y curl"]


def test_apply_empty_phase_is_success_with_no_exec() -> None:
    """A phase with no matching command returns code 0 and never calls exec_in."""
    fake = _FakeComposeExecutor()
    setup = DockerTargetSetup(fake)

    only_online = [ClassifiedCommand("apt-get update", SetupPhase.ONLINE)]
    result = setup.apply(
        "nginx-target", only_online, SetupPhase.OFFLINE, on_log=lambda _l: None
    )

    assert result.code == 0
    assert fake.calls == []
