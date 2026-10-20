"""Example tests — each new ``failed_step`` and the safe state (task 7.3).

One concrete example per new sub-stage failure point of
:meth:`~app.services.environment.provisioner.CurationEnvironmentProvisioner.provision`.
For each case the pipeline must stop at the right step, report the right
``failed_step``, keep Stage 5 **not** complete (``ready is False``, so Stage 6
stays blocked), and — for the egress-relevant failures — the fail-safe must have
closed the egress window so no target is left reachable from the internet.

The new ``failed_step`` values exercised here:

- ``build_online`` — ``EgressWindow.open`` fails (and the nice-to-have variant
  where ``TargetSetup.apply(ONLINE)`` returns ``code != 0``).
- ``encerrar_egresso`` — ``EgressWindow.close`` fails.
- ``verificar_egresso`` — ``egress_lost(verdicts)`` is ``False`` (a probe reports
  egress still present).
- ``setup_offline`` — ``TargetSetup.apply(OFFLINE)`` returns ``code != 0``.
- ``verificar_isolamento`` — the final isolation gate reproves.

Follows the recording-fake pattern of ``tests/test_provision_order_property.py``:
fakes for ``EgressWindow``/``EgressProber``/``TargetSetup``/``ComposeExecutor``,
a fake Caldera, a fake isolated inspector, a fake state inspector and a seeded
``CaseService`` over a ``tmp`` ``data_dir``. The sub-stage seams are injected so
the real orchestration runs; no Docker, public network or Caldera is touched.

Validates: Requirements 8.1, 8.2, 8.5

_Requirements: 8.1, 8.2, 8.5_
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from app.services.campaign_service import CaseService
from app.services.containment.isolation import (
    ContainerNetworkInfo,
    ContainerNotFound,
    NetworkInfo,
    NetworkNotFound,
)
from app.services.environment.compose_executor import CommandResult, LogLine
from app.services.environment.egress import EgressVerdict, EgressWindowResult
from app.services.environment.operationalization import (
    EnvironmentParameters,
    TargetHost,
)
from app.services.environment.provisioner import CurationEnvironmentProvisioner
from app.services.environment.target_setup import ClassifiedCommand, SetupPhase
from app.services.environment.topology_builder import HOST_CONTAINERS as _HOST_CONTAINERS


_SLUG = "shadowray"
_NET = "caldera-kali-network"
# A single target host for the examples (keeps the asserts about the fail-safe
# unambiguous — exactly one window should be opened/closed).
_HOST = "nginx"
_CONTAINER = _HOST_CONTAINERS.get(_HOST, _HOST)


# ---------------------------------------------------------------------------
# Recording fakes (same shape as the order property test).
# ---------------------------------------------------------------------------


@dataclass
class _FakeEgressWindow:
    """Fake ``EgressWindow`` recording ``open``/``close`` calls per container.

    ``open_fail``/``close_fail`` script a failure at either point so the examples
    can drive ``build_online`` (open) and ``encerrar_egresso`` (close).
    """

    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    open_fail: frozenset[str] = frozenset()
    close_fail: frozenset[str] = frozenset()

    def open(
        self, container: str, attack_networks: list[str], *, on_log
    ) -> EgressWindowResult:
        self.opened.append(container)
        on_log(LogLine(step="build_online", stream="info", text="open", substage="5.1"))
        if container in self.open_fail:
            return EgressWindowResult(
                container=container, opened=False, reason="falha ao abrir a janela"
            )
        return EgressWindowResult(container=container, opened=True)

    def close(self, container: str, *, on_log) -> EgressWindowResult:
        self.closed.append(container)
        on_log(
            LogLine(step="encerrar_egresso", stream="info", text="close", substage="5.1")
        )
        if container in self.close_fail:
            return EgressWindowResult(
                container=container, closed=False, reason="falha ao encerrar a janela"
            )
        return EgressWindowResult(container=container, closed=True)


@dataclass
class _FakeTargetSetup:
    """Fake ``TargetSetup`` scripting ONLINE/OFFLINE failures per container."""

    online_fail: frozenset[str] = frozenset()
    offline_fail: frozenset[str] = frozenset()

    def apply(
        self,
        container: str,
        commands: list[ClassifiedCommand],
        phase: SetupPhase,
        *,
        on_log,
        best_effort: bool = False,
    ) -> CommandResult:
        if phase is SetupPhase.ONLINE:
            fail = container in self.online_fail
        else:
            fail = container in self.offline_fail
        return CommandResult(code=1 if fail else 0, lines=[])


@dataclass
class _FakeEgressProber:
    """Fake ``EgressProber`` — ``lost_fail`` containers report egress present."""

    lost_fail: frozenset[str] = frozenset()

    def probe(self, container: str, *, on_log) -> EgressVerdict:
        if container in self.lost_fail:
            return EgressVerdict(
                container=container,
                egress_lost=False,
                conclusive=True,
                reason="egresso ainda presente",
            )
        return EgressVerdict(container=container, egress_lost=True, conclusive=True)


@dataclass
class _FakeComposeExecutor:
    """Fake ``ComposeExecutor`` — ``up``/``down`` plus the ``reisolar`` attaches.

    Exposing ``attach_network``/``detach_network``/``exec_in`` makes
    ``_substages_supported`` ``True`` so the real 5.1/5.2 orchestration runs.
    """

    def up(self, compose_file: str, *, on_log) -> CommandResult:
        on_log(LogLine(step="subir_conteineres", stream="stdout", text="up"))
        return CommandResult(code=0, lines=[])

    def down(self, compose_file: str, *, on_log) -> CommandResult:
        on_log(LogLine(step="desprovisionar", stream="stdout", text="down"))
        return CommandResult(code=0, lines=[])

    def attach_network(self, container: str, network: str, *, on_log, ip=None) -> CommandResult:
        return CommandResult(code=0, lines=[])

    def detach_network(self, container: str, network: str, *, on_log) -> CommandResult:
        return CommandResult(code=0, lines=[])

    def exec_in(self, container: str, argv: list[str], *, on_log) -> CommandResult:
        return CommandResult(code=0, lines=[])


@dataclass
class _FakeCaldera:
    """In-memory Caldera loader (no network)."""

    calls: list[str] = field(default_factory=list)

    def load_abilities(self, abilities: list[Mapping[str, Any]]) -> list[Any]:
        self.calls.append("load_abilities")
        return list(abilities)

    def load_adversary(self, adversary: Mapping[str, Any]) -> Any:
        self.calls.append("load_adversary")
        return adversary


@dataclass
class _FakeInspector:
    """In-memory read-only container/network inspector (no Docker daemon)."""

    containers: dict[str, ContainerNetworkInfo] = field(default_factory=dict)
    networks: dict[str, NetworkInfo] = field(default_factory=dict)

    def get_container_network_info(self, container: str) -> ContainerNetworkInfo:
        try:
            return self.containers[container]
        except KeyError as exc:
            raise ContainerNotFound(container) from exc

    def get_network_info(self, network_name: str) -> NetworkInfo:
        try:
            return self.networks[network_name]
        except KeyError as exc:
            raise NetworkNotFound(network_name) from exc


class _FakeStateInspector:
    """In-memory read-only state/health inspector (always running+healthy)."""

    def __init__(self, containers: list[str]) -> None:
        from app.services.environment.health import ContainerStateInfo

        self._states = {
            c: ContainerStateInfo(name=c, running=True, health="healthy")
            for c in containers
        }

    def get_container_state(self, container: str):
        from app.services.environment.health import ContainerStateNotFound

        try:
            return self._states[container]
        except KeyError as exc:
            raise ContainerStateNotFound(container) from exc


def _isolated_inspector(containers: list[str]) -> _FakeInspector:
    """An inspector where every target is attached only to an ``internal`` net."""
    insp = _FakeInspector()
    insp.networks[_NET] = NetworkInfo(name=_NET, internal=True, subnets=())
    for c in containers:
        insp.containers[c] = ContainerNetworkInfo(
            name=c, network_names=(_NET,), dns=()
        )
    return insp


def _non_isolated_inspector(containers: list[str]) -> _FakeInspector:
    """An inspector where the attached network is NOT internal (reproves)."""
    insp = _FakeInspector()
    insp.networks[_NET] = NetworkInfo(name=_NET, internal=False, subnets=())
    for c in containers:
        insp.containers[c] = ContainerNetworkInfo(
            name=c, network_names=(_NET,), dns=()
        )
    return insp


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_case(data_dir: Path, slug: str) -> None:
    abilities = [
        {
            "ability_id": f"{slug}-ab-1",
            "name": "first",
            "technique_id": "T1190",
            "executors": [{"name": "sh", "platform": "linux", "command": "echo 1"}],
        },
    ]
    _write_json(data_dir / "api" / f"{slug}_dag-ability.json", abilities)
    _write_json(
        data_dir / "api" / f"{slug}_dag-adversary.json",
        {
            "id": f"{slug}-adv",
            "name": "Seed Adversary",
            "description": "seed",
            "atomic_ordering": [f"{slug}-ab-1"],
        },
    )
    _write_json(
        data_dir / "dag" / f"{slug}_dag.json",
        {
            "campaign_name": "Seed",
            "structural_nodes": [
                {"node_id": "n0", "node_index": 0, "technique_id": "T1190"}
            ],
            "metadata": {"total_techniques": 1},
        },
    )


def _params() -> EnvironmentParameters:
    """A single host carrying both an ONLINE and an OFFLINE curated command."""
    return EnvironmentParameters(
        campaign_id=_SLUG,
        hosts=[
            TargetHost(
                label="h0",
                container=_HOST,
                vulnerabilities=["apt-get install -y nmap", "useradd attacker"],
            )
        ],
    )


def _run(
    *,
    window: _FakeEgressWindow,
    setup: _FakeTargetSetup,
    prober: _FakeEgressProber,
    inspector: _FakeInspector,
):
    """Run ``provision`` with the given seams over a seeded tmp data_dir."""
    containers = [_CONTAINER]
    tmp = Path(tempfile.mkdtemp(prefix="stage5_failed_steps_"))
    try:
        _seed_case(tmp, _SLUG)
        prov = CurationEnvironmentProvisioner(
            inspector=inspector,
            case_service=CaseService(data_dir=tmp),
            caldera=_FakeCaldera(),
            compose_executor=_FakeComposeExecutor(),
            state_inspector=_FakeStateInspector(containers),
            egress_window=window,
            egress_prober=prober,
            target_setup=setup,
        )
        return prov.provision(_params())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# One example per new failed_step.
# ---------------------------------------------------------------------------


def test_build_online_via_open_fail() -> None:
    """``EgressWindow.open`` fails ⇒ ``build_online`` and the window is closed."""
    window = _FakeEgressWindow(open_fail=frozenset({_CONTAINER}))
    environment = _run(
        window=window,
        setup=_FakeTargetSetup(),
        prober=_FakeEgressProber(),
        inspector=_isolated_inspector([_CONTAINER]),
    )

    assert environment.failed_step == "build_online"
    assert environment.ready is False
    # Stage 6 blocked: the sub-stage gates stay un-approved.
    assert environment.substages.build_online_ok is False
    # Fail-safe closed the window: the open attempt is matched by a close.
    assert window.opened == [_CONTAINER]
    assert _CONTAINER in window.closed


def test_build_online_via_online_apply_fail() -> None:
    """``TargetSetup.apply(ONLINE)`` ``code != 0`` ⇒ ``build_online`` (nice-to-have).

    The window opened, the online apply failed; the window must still end closed
    (the orchestration closes it then runs the fail-safe) — no egress left open.
    """
    window = _FakeEgressWindow()
    environment = _run(
        window=window,
        setup=_FakeTargetSetup(online_fail=frozenset({_CONTAINER})),
        prober=_FakeEgressProber(),
        inspector=_isolated_inspector([_CONTAINER]),
    )

    assert environment.failed_step == "build_online"
    assert environment.ready is False
    assert window.opened == [_CONTAINER]
    assert _CONTAINER in window.closed


def test_encerrar_egresso_via_close_fail() -> None:
    """``EgressWindow.close`` fails ⇒ ``encerrar_egresso`` and the fail-safe runs.

    The first close reports failure; the fail-safe then issues a second close so
    the host is never left with egress open — hence two recorded close calls.
    """
    window = _FakeEgressWindow(close_fail=frozenset({_CONTAINER}))
    environment = _run(
        window=window,
        setup=_FakeTargetSetup(),
        prober=_FakeEgressProber(),
        inspector=_isolated_inspector([_CONTAINER]),
    )

    assert environment.failed_step == "encerrar_egresso"
    assert environment.ready is False
    assert window.opened == [_CONTAINER]
    # The scripted close + the fail-safe close: the window is closed, not left open.
    assert window.closed.count(_CONTAINER) >= 1


def test_verificar_egresso_via_probe_present() -> None:
    """A probe reports egress still present ⇒ ``verificar_egresso`` reproves.

    5.1 completed (window opened and closed), so no egress is left open; 5.2's
    positive egress-loss gate then fails fail-closed.
    """
    window = _FakeEgressWindow()
    environment = _run(
        window=window,
        setup=_FakeTargetSetup(),
        prober=_FakeEgressProber(lost_fail=frozenset({_CONTAINER})),
        inspector=_isolated_inspector([_CONTAINER]),
    )

    assert environment.failed_step == "verificar_egresso"
    assert environment.ready is False
    # 5.1 finished cleanly: the window was opened and closed.
    assert window.opened == [_CONTAINER]
    assert _CONTAINER in window.closed
    # The egress-loss gate was NOT marked verified.
    assert environment.substages.egress_lost_verified is False


def test_setup_offline_via_offline_apply_fail() -> None:
    """``TargetSetup.apply(OFFLINE)`` ``code != 0`` ⇒ ``setup_offline``."""
    window = _FakeEgressWindow()
    environment = _run(
        window=window,
        setup=_FakeTargetSetup(offline_fail=frozenset({_CONTAINER})),
        prober=_FakeEgressProber(),
        inspector=_isolated_inspector([_CONTAINER]),
    )

    assert environment.failed_step == "setup_offline"
    assert environment.ready is False
    # Egress loss was verified before the offline setup was attempted.
    assert environment.substages.egress_lost_verified is True
    assert environment.substages.offline_applied is False
    # The window was closed during 5.1 — nothing left open.
    assert _CONTAINER in window.closed


def test_verificar_isolamento_via_gate_reproves() -> None:
    """The final isolation gate reproves ⇒ ``verificar_isolamento``.

    All sub-stages pass (window opened/closed, egress verified, offline applied),
    but the read-only isolation gate finds a non-internal network and reproves.
    """
    window = _FakeEgressWindow()
    environment = _run(
        window=window,
        setup=_FakeTargetSetup(),
        prober=_FakeEgressProber(),
        inspector=_non_isolated_inspector([_CONTAINER]),
    )

    assert environment.failed_step == "verificar_isolamento"
    assert environment.ready is False
    assert environment.isolation.approved is False
    # The sub-stages had completed, so no egress remains open from 5.1.
    assert environment.substages.offline_applied is True
    assert _CONTAINER in window.closed
