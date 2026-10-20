"""Example-based tests for the Stage-6 teardown sweeping the egress network (task 10.1).

The base Stage-5 teardown already runs ``compose down`` and maps its result to a
typed :class:`DeprovisioningResult` (see
``tests/test_stage5_e2e_provision_deprovision.py``). This spec **extends** the
teardown so :meth:`CurationEnvironmentProvisioner.deprovision` also sweeps the
ephemeral **egress network**
(:data:`~app.services.environment.egress.EGRESS_NETWORK`) that sub-stage 5.1 may
have created and that could have survived an error — covering what 5.1/5.2
created, not just the base lab (Req. 10.2) — and reports the resources that
remained provisioned on a teardown failure (Req. 10.3).

These tests cover the extended teardown contract with a self-contained
``_RecordingComposeExecutor`` *fake* that records every ``down``/``remove_network``
call and scripts their exit codes and emitted lines — never touching the Docker
daemon, a subprocess, the public network or AI:

* **Success** — ``down`` ok **and** ``remove_network`` ok ⇒ the fake recorded a
  ``remove_network`` call for ``EGRESS_NETWORK`` and the result is
  ``deprovisioned=True`` with **empty** ``remaining_resources``.
* **Egress already gone** — ``down`` ok and ``remove_network`` fails with a
  *not-found* message (the happy path closed the window in 5.2) ⇒ still a
  **success** (not a residue), ``remaining_resources`` empty.
* **``compose down`` fails** ⇒ ``deprovisioned=False`` with the campaign's
  ``target_containers`` in ``remaining_resources`` and a readable ``message``.
* **Egress removal fails** (real error, not *not-found*) ⇒ ``deprovisioned=False``
  with ``EGRESS_NETWORK`` listed in ``remaining_resources`` and a readable
  ``message``.
* **Legacy fake** — an ``up``/``down``-only executor (no ``remove_network``)
  degrades gracefully: the teardown skips the sweep and still succeeds on a clean
  ``down``.

_Requirements: 10.2, 10.3_
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.models.enums import TranslationSource
from app.services.environment.compose_executor import (
    CommandResult,
    ComposeExecutor,
    LogLine,
)
from app.services.environment.egress import EGRESS_NETWORK
from app.services.environment.provisioner import (
    CurationEnvironmentProvisioner,
    ProvisionedEnvironment,
)


_COMPOSE_FILE = "/lab/docker-compose.yml"
_TARGETS = ["sticks-nginx-target", "sticks-mariadb-target"]


def _environment() -> ProvisionedEnvironment:
    """A minimal provisioned environment carrying a compose file + target containers."""
    return ProvisionedEnvironment(
        campaign_id="shadowray",
        compose_file=_COMPOSE_FILE,
        target_containers=list(_TARGETS),
        origin=TranslationSource.HUMAN_CURATION,
    )


# ---------------------------------------------------------------------------
# Fakes (no subprocess, no Docker daemon, no shell, no network)
# ---------------------------------------------------------------------------


@dataclass
class _Call:
    """A recorded call to the fake: the operation and its single argument."""

    op: str  # "down" | "remove_network"
    arg: str  # the compose file (down) or the network name (remove_network)


class _RecordingComposeExecutor:
    """Injectable ``ComposeExecutor`` *fake* recording ``down``/``remove_network``.

    Satisfies the ``@runtime_checkable`` :class:`ComposeExecutor` structurally
    (it exposes the full surface) without touching the Docker daemon, a
    subprocess, a shell or the network. ``down`` and ``remove_network`` record
    their call and emit the scripted lines through ``on_log``, returning a
    :class:`CommandResult` with the scripted exit code — so the test asserts both
    the recorded order and the mapped :class:`DeprovisioningResult`.

    Attributes:
        down_code: exit code returned by :meth:`down` (``0`` = success).
        remove_code: exit code returned by :meth:`remove_network`.
        down_lines: ``(stream, text)`` lines emitted by ``down``.
        remove_lines: ``(stream, text)`` lines emitted by ``remove_network``.
    """

    def __init__(
        self,
        *,
        down_code: int = 0,
        remove_code: int = 0,
        down_lines: list[tuple[str, str]] | None = None,
        remove_lines: list[tuple[str, str]] | None = None,
    ) -> None:
        self.down_code = down_code
        self.remove_code = remove_code
        self._down_lines = down_lines or [("stdout", "Container removed")]
        self._remove_lines = remove_lines or [("stdout", EGRESS_NETWORK)]
        self.calls: list[_Call] = []

    def _emit(
        self,
        step: str,
        code: int,
        lines_spec: list[tuple[str, str]],
        on_log: Callable[[LogLine], None],
    ) -> CommandResult:
        lines: list[LogLine] = []
        for stream, text in lines_spec:
            line = LogLine(step=step, stream=stream, text=text)
            lines.append(line)
            on_log(line)
        return CommandResult(code=code, lines=lines)

    # --- operations exercised here ---
    def down(
        self, compose_file: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        self.calls.append(_Call(op="down", arg=compose_file))
        return self._emit("desprovisionar", self.down_code, self._down_lines, on_log)

    def remove_network(
        self, network: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        self.calls.append(_Call(op="remove_network", arg=network))
        return self._emit(
            "remove_network", self.remove_code, self._remove_lines, on_log
        )

    # --- rest of the protocol surface (not exercised here) ---
    def up(
        self, compose_file: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:  # pragma: no cover - not exercised
        return CommandResult(code=0, lines=[])

    def attach_network(
        self, container: str, network: str, *, on_log: Callable[[LogLine], None], ip=None
    ) -> CommandResult:  # pragma: no cover - not exercised
        return CommandResult(code=0, lines=[])

    def detach_network(
        self, container: str, network: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:  # pragma: no cover - not exercised
        return CommandResult(code=0, lines=[])

    def exec_in(
        self, container: str, argv: list[str], *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:  # pragma: no cover - not exercised
        return CommandResult(code=0, lines=[])


class _LegacyComposeExecutor:
    """An ``up``/``down``-only *fake* that predates ``remove_network`` (task 2).

    Mirrors the executors the earlier Stage-5 suites inject: it exposes only
    ``up``/``down`` and NOT ``remove_network``, so the teardown's ``hasattr``
    guard must skip the egress sweep gracefully.
    """

    def __init__(self, *, down_code: int = 0) -> None:
        self.down_code = down_code
        self.down_calls: list[str] = []

    def up(
        self, compose_file: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:  # pragma: no cover - not exercised
        return CommandResult(code=0, lines=[])

    def down(
        self, compose_file: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        self.down_calls.append(compose_file)
        line = LogLine(step="desprovisionar", stream="stdout", text="Container removed")
        on_log(line)
        return CommandResult(code=self.down_code, lines=[line])


def _provisioner(executor: ComposeExecutor) -> CurationEnvironmentProvisioner:
    """A curation provisioner with only the compose executor injected.

    ``deprovision`` touches none of the other collaborators (inspector/caldera/
    etc.), so leaving them to their lazy production defaults is safe — the fake
    executor is the sole seam the teardown drives, and it never touches Docker.
    """
    return CurationEnvironmentProvisioner(compose_executor=executor)


# ---------------------------------------------------------------------------
# Success — down ok AND egress network removed
# ---------------------------------------------------------------------------


def test_deprovision_removes_egress_network_on_success() -> None:
    """``down`` ok + ``remove_network`` ok ⇒ egress removed, deprovisioned, no residue.

    _Requirements: 10.2_
    """
    executor = _RecordingComposeExecutor(down_code=0, remove_code=0)
    prov = _provisioner(executor)

    result = prov.deprovision(_environment())

    # The teardown ran ``compose down`` AND swept the ephemeral egress network.
    assert executor.calls == [
        _Call(op="down", arg=_COMPOSE_FILE),
        _Call(op="remove_network", arg=EGRESS_NETWORK),
    ]
    # Successful teardown with NO remaining resources, origin coherent.
    assert result.deprovisioned is True
    assert result.remaining_resources == []
    assert result.origin is TranslationSource.HUMAN_CURATION


def test_deprovision_treats_missing_egress_network_as_success() -> None:
    """``remove_network`` failing with *not-found* is NOT a residue (best-effort sweep).

    On the happy path 5.2 already closed the egress window, so the ephemeral
    network no longer exists and ``docker network rm`` fails with a *not-found*
    message — which must be treated as a success, not a remaining resource.

    _Requirements: 10.2_
    """
    executor = _RecordingComposeExecutor(
        down_code=0,
        remove_code=1,
        remove_lines=[("stderr", f"Error: No such network: {EGRESS_NETWORK}")],
    )
    prov = _provisioner(executor)

    result = prov.deprovision(_environment())

    # The sweep was attempted...
    assert _Call(op="remove_network", arg=EGRESS_NETWORK) in executor.calls
    # ...but "already gone" is success, not a residue.
    assert result.deprovisioned is True
    assert result.remaining_resources == []


# ---------------------------------------------------------------------------
# Failure — teardown failure populates remaining_resources
# ---------------------------------------------------------------------------


def test_deprovision_compose_down_failure_reports_target_containers() -> None:
    """``compose down`` failing ⇒ deprovisioned=False with target containers as residue.

    _Requirements: 10.3_
    """
    executor = _RecordingComposeExecutor(
        down_code=1,
        down_lines=[("stderr", "Error response from daemon: conflict")],
        remove_code=0,
    )
    prov = _provisioner(executor)

    result = prov.deprovision(_environment())

    assert result.deprovisioned is False
    # The campaign's target containers remained provisioned.
    for container in _TARGETS:
        assert container in result.remaining_resources
    # A readable cause derived from the down's stderr.
    assert result.message
    assert "conflict" in result.message


def test_deprovision_egress_removal_failure_lists_egress_network() -> None:
    """``remove_network`` failing with a real error ⇒ egress network is a residue.

    A genuine removal failure (e.g. the network is still in use) is NOT a
    *not-found*, so the ephemeral egress network must be reported as a remaining
    resource with a readable cause.

    _Requirements: 10.3_
    """
    executor = _RecordingComposeExecutor(
        down_code=0,
        remove_code=1,
        remove_lines=[("stderr", "Error: network sticks-egress-ephemeral has active endpoints")],
    )
    prov = _provisioner(executor)

    result = prov.deprovision(_environment())

    assert result.deprovisioned is False
    assert EGRESS_NETWORK in result.remaining_resources
    # The target containers were torn down (down succeeded) so they are NOT residue.
    assert all(c not in result.remaining_resources for c in _TARGETS)
    assert result.message
    assert "egresso" in result.message


def test_deprovision_both_failures_report_all_remaining_resources() -> None:
    """Both ``down`` and egress removal failing ⇒ containers AND egress network listed.

    _Requirements: 10.3_
    """
    executor = _RecordingComposeExecutor(
        down_code=1,
        down_lines=[("stderr", "down failed")],
        remove_code=1,
        remove_lines=[("stderr", "network still in use")],
    )
    prov = _provisioner(executor)

    result = prov.deprovision(_environment())

    assert result.deprovisioned is False
    for container in _TARGETS:
        assert container in result.remaining_resources
    assert EGRESS_NETWORK in result.remaining_resources


# ---------------------------------------------------------------------------
# Legacy executor — degrades gracefully without remove_network
# ---------------------------------------------------------------------------


def test_deprovision_degrades_gracefully_for_legacy_executor() -> None:
    """An ``up``/``down``-only executor skips the egress sweep and still succeeds.

    _Requirements: 10.2_
    """
    executor = _LegacyComposeExecutor(down_code=0)
    prov = _provisioner(executor)

    result = prov.deprovision(_environment())

    # The clean ``down`` succeeded; the missing ``remove_network`` was skipped.
    assert executor.down_calls == [_COMPOSE_FILE]
    assert result.deprovisioned is True
    assert result.remaining_resources == []
