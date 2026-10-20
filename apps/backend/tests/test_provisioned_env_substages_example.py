"""Example tests for the Stage-5 sub-stage model extensions (task 6.1).

Task 6 extends the Stage-5 output models **without breaking the existing shape**:

* ``LogLine`` gains an optional ``substage`` field (``"5.1"`` | ``"5.2"`` |
  ``None`` for the inherited steps), defaulting to ``None`` — so every existing
  ``LogLine(step=..., stream=..., text=...)`` call site stays valid and the
  inherited lines carry no sub-stage marker (Req. 6.3).
* ``ProvisionedEnvironment`` gains a ``substages`` :class:`SubStageSummary`
  (``build_online_ok``/``egress_closed``/``egress_lost_verified``/
  ``offline_applied``/``egress_verdicts``), and its :attr:`ready` property now
  **also** requires ``build_online_ok``, ``egress_lost_verified`` and
  ``offline_applied``, in addition to the inherited prerequisites
  (``health.approved``, ``loaded_abilities``, ``isolation.approved``) — Req. 4.6,
  6.3.

These examples assert the **backward compatibility** (``LogLine.substage``
default; ``provision(params)`` with no ``on_log`` still valid) and the **extended
``ready``** (``True`` only when the new sub-stage fields are satisfied). Everything
runs in-memory with the package's injected *fakes* (no subprocess, no Docker
daemon, no network, no AI).

_Requirements: 4.6, 6.3_
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from app.models.enums import TranslationSource
from app.services.campaign_service import CaseService
from app.services.containment.isolation import (
    ContainerNetworkInfo,
    ContainerNotFound,
    NetworkInfo,
    NetworkNotFound,
)
from app.services.environment.compose_executor import (
    CommandResult,
    LogLine,
)
from app.services.environment.health import (
    ContainerStateInfo,
    ContainerStateNotFound,
)
from app.services.environment.operationalization import (
    EnvironmentParameters,
    EnvironmentPlatform,
    TargetHost,
)
from app.services.environment.provisioner import (
    ContainerVerdict,
    CurationEnvironmentProvisioner,
    EgressVerdictView,
    IsolationSummary,
    ProvisionedEnvironment,
    SubStageSummary,
)
from app.services.environment.health import HealthSummary, HealthVerdict
from app.services.environment.topology_builder import HOST_CONTAINERS as _HOST_CONTAINERS


# ---------------------------------------------------------------------------
# Fakes (no network, no Docker daemon, no AI) — mirror the package conventions.
# ---------------------------------------------------------------------------


_SLUG = "shadowray"  # a registered curated slug
_C1 = "nginx"
_C2 = "db"
_NET = "caldera-kali-network"


def _resolver(containers: list[str]) -> list[str]:
    return [_HOST_CONTAINERS.get(c, c) for c in containers]


class FakeCaldera:
    """In-memory Caldera loader (no network)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def load_abilities(self, abilities: list[Mapping[str, Any]]) -> list[Any]:
        self.calls.append("load_abilities")
        return list(abilities)

    def load_adversary(self, adversary: Mapping[str, Any]) -> Any:
        self.calls.append("load_adversary")
        return adversary


@dataclass
class FakeInspector:
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


class FakeStateInspector:
    """In-memory read-only state/health inspector (no Docker daemon)."""

    def __init__(self, states: dict[str, ContainerStateInfo]) -> None:
        self._states = dict(states)

    def get_container_state(self, container: str) -> ContainerStateInfo:
        try:
            return self._states[container]
        except KeyError as exc:
            raise ContainerStateNotFound(container) from exc


class FakeComposeExecutor:
    """In-memory ``ComposeExecutor`` *fake* (no subprocess, no Docker daemon).

    Emits one ``subir_conteineres`` ``stdout`` line through ``on_log`` (an
    inherited step, so **no** ``substage``) and returns ``code=0``.
    """

    def __init__(self) -> None:
        self.up_calls: list[str] = []
        self.down_calls: list[str] = []

    def up(self, compose_file: str, *, on_log: Callable[[LogLine], None]) -> CommandResult:
        self.up_calls.append(compose_file)
        line = LogLine(step="subir_conteineres", stream="stdout", text="Container up")
        on_log(line)
        return CommandResult(code=0, lines=[line])

    def down(self, compose_file: str, *, on_log: Callable[[LogLine], None]) -> CommandResult:
        self.down_calls.append(compose_file)
        line = LogLine(step="desprovisionar", stream="stdout", text="Container removed")
        on_log(line)
        return CommandResult(code=0, lines=[line])


def _healthy_state_inspector(containers: list[str]) -> FakeStateInspector:
    return FakeStateInspector(
        {
            c: ContainerStateInfo(name=c, running=True, health="healthy")
            for c in _resolver(containers)
        }
    )


def _isolated_inspector(containers: list[str]) -> FakeInspector:
    insp = FakeInspector()
    insp.networks[_NET] = NetworkInfo(name=_NET, internal=True, subnets=())
    for c in _resolver(containers):
        insp.containers[c] = ContainerNetworkInfo(name=c, network_names=(_NET,), dns=())
    return insp


def _params(containers: list[str]) -> EnvironmentParameters:
    hosts = [TargetHost(label=f"h{i}", container=c) for i, c in enumerate(containers)]
    return EnvironmentParameters(campaign_id=_SLUG, hosts=hosts)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_case(data_dir: Path, slug: str) -> None:
    """Seed a minimal curated case with a 2-ability ordered adversary."""
    abilities = [
        {
            "ability_id": f"{slug}-ab-1",
            "name": "first",
            "technique_id": "T1190",
            "executors": [{"name": "sh", "platform": "linux", "command": "echo 1"}],
        },
        {
            "ability_id": f"{slug}-ab-2",
            "name": "second",
            "technique_id": "T1059",
            "executors": [{"name": "sh", "platform": "linux", "command": "echo 2"}],
        },
    ]
    _write_json(data_dir / "api" / f"{slug}_dag-ability.json", abilities)
    _write_json(
        data_dir / "api" / f"{slug}_dag-adversary.json",
        {
            "id": f"{slug}-adv",
            "name": "Seed Adversary",
            "description": "seed",
            "atomic_ordering": [f"{slug}-ab-2", f"{slug}-ab-1"],
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


@pytest.fixture()
def data_dir() -> Iterator[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_stage5_substages_"))
    (tmp / "api").mkdir(parents=True, exist_ok=True)
    (tmp / "dag").mkdir(parents=True, exist_ok=True)
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _curation(data_dir: Path, containers: list[str]) -> CurationEnvironmentProvisioner:
    _seed_case(data_dir, _SLUG)
    return CurationEnvironmentProvisioner(
        inspector=_isolated_inspector(containers),
        case_service=CaseService(data_dir=data_dir),
        caldera=FakeCaldera(),
        compose_executor=FakeComposeExecutor(),
        state_inspector=_healthy_state_inspector(containers),
    )


# ---------------------------------------------------------------------------
# 1. LogLine.substage defaults to None for the inherited steps.
# ---------------------------------------------------------------------------


def test_logline_substage_defaults_to_none() -> None:
    """An inherited ``LogLine`` built without ``substage`` keeps ``substage=None``.
    _Requirements: 6.3_"""
    # The exact call shape used across the inherited steps (no ``substage`` kwarg).
    line = LogLine(step="subir_conteineres", stream="stdout", text="Container up")
    assert line.substage is None

    # A sub-stage line can still carry an explicit marker when the orchestration
    # (task 7) sets it — the field accepts "5.1"/"5.2".
    marked = LogLine(step="build_online", stream="info", text="5.1", substage="5.1")
    assert marked.substage == "5.1"


def test_provision_inherited_lines_have_no_substage(data_dir: Path) -> None:
    """The lines emitted by the inherited pipeline carry ``substage is None``.
    _Requirements: 6.3_"""
    containers = [_C1, _C2]
    prov = _curation(data_dir, containers)

    lines: list[LogLine] = []
    prov.provision(_params(containers), on_log=lines.append)

    # All emitted lines belong to the inherited steps -> no sub-stage marker.
    assert lines  # the pipeline emitted at least one line
    assert all(l.substage is None for l in lines)


# ---------------------------------------------------------------------------
# 2. provision(params) with no on_log stays valid (backward compatibility).
# ---------------------------------------------------------------------------


def test_provision_without_on_log_is_valid(data_dir: Path) -> None:
    """Calling ``provision(params)`` with **no** ``on_log`` still works (Req. 9.2).
    _Requirements: 6.3_"""
    containers = [_C1, _C2]
    prov = _curation(data_dir, containers)

    # No on_log argument: the keyword-only no-op default keeps the call valid.
    environment = prov.provision(_params(containers))

    assert isinstance(environment, ProvisionedEnvironment)
    assert environment.campaign_id == _SLUG
    assert environment.platform is EnvironmentPlatform.CONTAINER
    assert environment.origin is TranslationSource.HUMAN_CURATION
    # The extended output always carries the sub-stage summary shape.
    assert isinstance(environment.substages, SubStageSummary)


# ---------------------------------------------------------------------------
# 3. ready is True only when the new sub-stage fields are satisfied.
# ---------------------------------------------------------------------------


def _env(
    *,
    health_ok: bool,
    abilities_ok: bool,
    isolation_ok: bool,
    build_online_ok: bool,
    egress_lost_verified: bool,
    offline_applied: bool,
) -> ProvisionedEnvironment:
    """A ProvisionedEnvironment with every gate set independently (no pipeline)."""
    return ProvisionedEnvironment(
        campaign_id=_SLUG,
        target_containers=[_C1, _C2],
        health=HealthSummary(
            approved=health_ok,
            verdicts=[
                HealthVerdict(container=c, running=True, healthy=health_ok)
                for c in (_C1, _C2)
            ],
        ),
        loaded_abilities=abilities_ok,
        isolation=IsolationSummary(
            approved=isolation_ok,
            verdicts=[ContainerVerdict(container=c, isolated=isolation_ok) for c in (_C1, _C2)],
        ),
        substages=SubStageSummary(
            build_online_ok=build_online_ok,
            egress_closed=build_online_ok,
            egress_lost_verified=egress_lost_verified,
            offline_applied=offline_applied,
        ),
    )


def test_ready_true_only_with_all_gates_including_substages() -> None:
    """``ready`` is ``True`` only when inherited **and** sub-stage gates all pass.
    _Requirements: 4.6, 6.3_"""
    # All gates satisfied -> ready.
    full = _env(
        health_ok=True,
        abilities_ok=True,
        isolation_ok=True,
        build_online_ok=True,
        egress_lost_verified=True,
        offline_applied=True,
    )
    assert full.ready is True

    # Each new sub-stage gate, when missing, flips ready to False even though the
    # three inherited prerequisites pass.
    for missing in ("build_online_ok", "egress_lost_verified", "offline_applied"):
        env = _env(
            health_ok=True,
            abilities_ok=True,
            isolation_ok=True,
            build_online_ok=(missing != "build_online_ok"),
            egress_lost_verified=(missing != "egress_lost_verified"),
            offline_applied=(missing != "offline_applied"),
        )
        assert env.ready is False, f"ready must require substages.{missing}"


def test_ready_false_when_inherited_prereq_fails_even_with_substages() -> None:
    """A failing inherited prerequisite keeps ``ready`` ``False`` (fail-closed).
    _Requirements: 4.6, 6.3_"""
    for missing in ("health_ok", "abilities_ok", "isolation_ok"):
        env = _env(
            health_ok=(missing != "health_ok"),
            abilities_ok=(missing != "abilities_ok"),
            isolation_ok=(missing != "isolation_ok"),
            build_online_ok=True,
            egress_lost_verified=True,
            offline_applied=True,
        )
        assert env.ready is False, f"ready must still require {missing}"


def test_default_substage_summary_is_not_approved() -> None:
    """A fresh ``SubStageSummary`` defaults to not-approved (so ``ready`` stays False).
    _Requirements: 6.3_"""
    summary = SubStageSummary()
    assert summary.build_online_ok is False
    assert summary.egress_closed is False
    assert summary.egress_lost_verified is False
    assert summary.offline_applied is False
    assert summary.egress_verdicts == []

    # A ProvisionedEnvironment built with the inherited prerequisites ok but the
    # DEFAULT (empty) sub-stage summary is NOT ready.
    env = ProvisionedEnvironment(
        campaign_id=_SLUG,
        target_containers=[_C1, _C2],
        health=HealthSummary(approved=True),
        loaded_abilities=True,
        isolation=IsolationSummary(approved=True),
    )
    assert env.ready is False


def test_egress_verdict_view_shape() -> None:
    """``EgressVerdictView`` carries the per-host egress probe outcome.
    _Requirements: 4.6_"""
    verdict = EgressVerdictView(
        container=_resolver([_C1])[0],
        egress_lost=True,
        conclusive=True,
        reason="external endpoint unreachable",
    )
    assert verdict.egress_lost is True
    assert verdict.conclusive is True
    # Defaults are fail-closed (egress present, not conclusive).
    default = EgressVerdictView(container="x")
    assert default.egress_lost is False
    assert default.conclusive is False
    assert default.reason == ""
