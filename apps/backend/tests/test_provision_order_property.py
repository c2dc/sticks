"""Property test — strict order of sub-stage 5.1 before 5.2 (task 7.1).

# Feature: stage5-target-provisioning, Property 3: Ordem estrita 5.1 antes de
# 5.2. Para qualquer roteiro de sucesso/falha dos seams injetados, a sub-etapa
# 5.2 (reisolar / verificar_egresso / setup_offline / carregar_abilities) só
# começa DEPOIS que a sub-etapa 5.1 (build_online + encerrar_egresso) concluiu
# para TODOS os hosts; e se a 5.1 falha para qualquer host, a 5.2 NUNCA executa.

Validates: Requirements 1.2, 1.3, 2.6

Esta é uma property test de *feature* (``stage5-target-provisioning`` é uma
feature, não um bugfix): a propriedade deve **valer** sobre a orquestração atual
de :meth:`~app.services.environment.provisioner.CurationEnvironmentProvisioner.provision`.

Nenhum Docker, rede pública ou Caldera real é tocado. Todos os seams novos
(``EgressWindow``, ``TargetSetup``, ``EgressProber``) e o ``ComposeExecutor``
(usado pelo ``reisolar`` via ``attach_network``) são **fakes** que escrevem numa
**lista de eventos compartilhada e ordenada**. O Hypothesis roteiriza, por host,
se a abertura/encerramento da janela e a aplicação do setup online têm sucesso,
e se a sonda de egresso aprova — cobrindo o caminho feliz e cada ponto de falha
da 5.1 e da 5.2.

As asserções, sobre o histórico de chamadas:

1. **5.2 depois de 5.1 para TODOS** — nenhum evento da 5.2 (``reisolar`` em
   diante) aparece antes que o último evento da 5.1 (``build_online``/
   ``encerrar_egresso``) do último host tenha ocorrido.
2. **5.1 falha ⇒ 5.2 não roda** — se qualquer host faz a 5.1 falhar, o histórico
   não contém NENHUM evento da 5.2.
3. **Marcação de sub-etapa coerente** — o ``failed_step`` reportado pertence à
   sub-etapa correta e o ``ProvisionedEnvironment`` não fica ``ready`` numa falha.

_Requirements: 1.2, 1.3, 2.6_
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.campaign_service import CaseService
from app.services.containment.isolation import (
    ContainerNetworkInfo,
    ContainerNotFound,
    NetworkInfo,
    NetworkNotFound,
)
from app.services.environment.compose_executor import CommandResult, LogLine
from app.services.environment.egress import EGRESS_NETWORK, EgressVerdict, EgressWindowResult
from app.services.environment.operationalization import (
    EnvironmentParameters,
    TargetHost,
)
from app.services.environment.provisioner import CurationEnvironmentProvisioner
from app.services.environment.target_setup import (
    ClassifiedCommand,
    SetupPhase,
)
from app.services.environment.topology_builder import HOST_CONTAINERS as _HOST_CONTAINERS


_SLUG = "shadowray"
_NET = "caldera-kali-network"

# The 5.2 steps — their presence in the shared history means sub-stage 5.2 ran.
_STAGE_52_STEPS = frozenset(
    {"reisolar", "verificar_egresso", "setup_offline", "carregar_abilities"}
)
# The 5.1 steps.
_STAGE_51_STEPS = frozenset({"build_online", "encerrar_egresso"})


def _resolver(containers: list[str]) -> list[str]:
    return [_HOST_CONTAINERS.get(c, c) for c in containers]


# ---------------------------------------------------------------------------
# Recording fakes — all write into a single shared, ordered event list so the
# relative order of 5.1 vs 5.2 operations is observable.
# ---------------------------------------------------------------------------


@dataclass
class _Recorder:
    """A single ordered history of ``(phase, step, container)`` events."""

    events: list[tuple[str, str, str]] = field(default_factory=list)

    def record(self, phase: str, step: str, container: str) -> None:
        self.events.append((phase, step, container))


@dataclass
class _FakeEgressWindow:
    """Fake ``EgressWindow`` recording ``open``/``close`` and scripting failures.

    ``open_fail``/``close_fail`` are sets of containers whose ``open``/``close``
    must fail, so Hypothesis can inject a 5.1 failure at either point.
    """

    recorder: _Recorder
    open_fail: frozenset[str] = frozenset()
    close_fail: frozenset[str] = frozenset()

    def open(
        self, container: str, attack_networks: list[str], *, on_log
    ) -> EgressWindowResult:
        self.recorder.record("5.1", "build_online", container)
        on_log(LogLine(step="build_online", stream="info", text="open", substage="5.1"))
        if container in self.open_fail:
            return EgressWindowResult(
                container=container, opened=False, reason="falha ao abrir"
            )
        return EgressWindowResult(container=container, opened=True)

    def close(self, container: str, *, on_log) -> EgressWindowResult:
        self.recorder.record("5.1", "encerrar_egresso", container)
        on_log(
            LogLine(step="encerrar_egresso", stream="info", text="close", substage="5.1")
        )
        if container in self.close_fail:
            return EgressWindowResult(
                container=container, closed=False, reason="falha ao encerrar"
            )
        return EgressWindowResult(container=container, closed=True)


@dataclass
class _FakeTargetSetup:
    """Fake ``TargetSetup`` recording the phase it ran and scripting failures.

    ``online_fail``/``offline_fail`` are sets of containers whose ONLINE/OFFLINE
    application must fail.
    """

    recorder: _Recorder
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
            self.recorder.record("5.1", "build_online", container)
            fail = container in self.online_fail
        else:
            self.recorder.record("5.2", "setup_offline", container)
            fail = container in self.offline_fail
        # Best-effort setup never aborts (idempotent/redundant steps collide
        # benignly); strict mode still propagates the scripted failure.
        if fail and best_effort:
            return CommandResult(code=0, lines=[])
        return CommandResult(code=1 if fail else 0, lines=[])


@dataclass
class _FakeEgressProber:
    """Fake ``EgressProber`` recording the probe and scripting the verdict.

    ``lost_fail`` are the containers whose probe reports egress still present
    (reproving ``verificar_egresso``).
    """

    recorder: _Recorder
    lost_fail: frozenset[str] = frozenset()

    def probe(self, container: str, *, on_log) -> EgressVerdict:
        self.recorder.record("5.2", "verificar_egresso", container)
        if container in self.lost_fail:
            return EgressVerdict(
                container=container, egress_lost=False, conclusive=True
            )
        return EgressVerdict(container=container, egress_lost=True, conclusive=True)


@dataclass
class _FakeComposeExecutor:
    """Fake ``ComposeExecutor`` — ``up``/``down`` plus the ``reisolar`` attaches.

    Its ``attach_network`` records a ``5.2``/``reisolar`` event (the first 5.2
    operation in the pipeline). ``up``/``down`` are inherited steps (no substage).
    """

    recorder: _Recorder

    def up(self, compose_file: str, *, on_log) -> CommandResult:
        on_log(LogLine(step="subir_conteineres", stream="stdout", text="up"))
        return CommandResult(code=0, lines=[])

    def down(self, compose_file: str, *, on_log) -> CommandResult:
        on_log(LogLine(step="desprovisionar", stream="stdout", text="down"))
        return CommandResult(code=0, lines=[])

    def attach_network(self, container: str, network: str, *, on_log, ip=None) -> CommandResult:
        self.recorder.record("5.2", "reisolar", container)
        return CommandResult(code=0, lines=[])

    def detach_network(self, container: str, network: str, *, on_log) -> CommandResult:
        # Used by the fail-safe path (close window) via DockerEgressWindow — but the
        # window itself is faked, so this is only a defensive no-op for parity.
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
    insp = _FakeInspector()
    insp.networks[_NET] = NetworkInfo(name=_NET, internal=True, subnets=())
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


def _params(containers: list[str]) -> EnvironmentParameters:
    hosts = [
        TargetHost(
            label=f"h{i}",
            container=c,
            # A mix of online/offline curated commands so both phases have work.
            vulnerabilities=["apt-get install -y nmap", "useradd attacker"],
        )
        for i, c in enumerate(containers)
    ]
    return EnvironmentParameters(campaign_id=_SLUG, hosts=hosts)


# ---------------------------------------------------------------------------
# Generators — the hosts and a per-run failure script.
# ---------------------------------------------------------------------------

_HOST_CHOICES = ["nginx", "db"]
_hosts_strategy = st.lists(
    st.sampled_from(_HOST_CHOICES), min_size=1, max_size=2, unique=True
)
# Where (if anywhere) the pipeline is scripted to fail. ``"none"`` is the happy
# path; the 5.1 failure points must prevent any 5.2 event.
_failpoint = st.sampled_from(
    # none = happy path. open/close are fatal 5.1 points; probe is a fatal 5.2
    # point. online/offline setup are BEST-EFFORT (non-fatal), so they are not
    # failure points anymore and are intentionally excluded here.
    ["none", "open", "close", "probe"]
)
# Which host index (if present) carries the scripted failure.
_fail_index = st.integers(min_value=0, max_value=1)


@settings(max_examples=100, deadline=None)
@given(hosts=_hosts_strategy, failpoint=_failpoint, fail_index=_fail_index)
def test_stage_52_never_runs_before_51_completed_for_all_hosts(
    hosts: list[str], failpoint: str, fail_index: int
) -> None:
    # Feature: stage5-target-provisioning, Property 3: Ordem estrita 5.1 antes de 5.2.
    containers = _resolver(hosts)
    # The host carrying the scripted failure (clamped to the available hosts).
    target = containers[fail_index % len(containers)]
    fail_set = frozenset({target}) if failpoint != "none" else frozenset()

    recorder = _Recorder()
    compose = _FakeComposeExecutor(recorder)
    window = _FakeEgressWindow(
        recorder,
        open_fail=fail_set if failpoint == "open" else frozenset(),
        close_fail=fail_set if failpoint == "close" else frozenset(),
    )
    setup = _FakeTargetSetup(
        recorder,
        online_fail=fail_set if failpoint == "online" else frozenset(),
        offline_fail=fail_set if failpoint == "offline" else frozenset(),
    )
    prober = _FakeEgressProber(
        recorder, lost_fail=fail_set if failpoint == "probe" else frozenset()
    )

    tmp = Path(tempfile.mkdtemp(prefix="stage5_order_prop_"))
    try:
        _seed_case(tmp, _SLUG)
        prov = CurationEnvironmentProvisioner(
            inspector=_isolated_inspector(containers),
            case_service=CaseService(data_dir=tmp),
            caldera=_FakeCaldera(),
            compose_executor=compose,
            state_inspector=_FakeStateInspector(containers),
            egress_window=window,
            egress_prober=prober,
            target_setup=setup,
        )
        environment = prov.provision(_params(hosts))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    events = recorder.events

    # Index of the FIRST 5.2 event (if any) and the LAST 5.1 event (if any).
    first_52 = next(
        (i for i, (ph, _s, _c) in enumerate(events) if ph == "5.2"), None
    )
    last_51 = next(
        (
            i
            for i in range(len(events) - 1, -1, -1)
            if events[i][0] == "5.1"
        ),
        None,
    )

    # 5.1 must have attempted for the FIRST host at least (the pipeline reached
    # sub-stage 5.1 after health passed).
    assert any(ph == "5.1" for ph, _s, _c in events), (
        "a sub-etapa 5.1 deveria ter iniciado após a saúde passar"
    )

    is_51_failure = failpoint in ("open", "close")

    if is_51_failure:
        # (2) A 5.1 falha ⇒ a 5.2 NUNCA roda: nenhum evento de 5.2 no histórico.
        assert first_52 is None, (
            f"5.2 executou apesar de a 5.1 ter falhado em {failpoint!r}: {events}"
        )
        assert environment.failed_step in _STAGE_51_STEPS
        assert environment.ready is False
    else:
        # (1) 5.2 só começa depois que a 5.1 concluiu para TODOS os hosts: o
        # primeiro evento de 5.2 vem estritamente após o último de 5.1.
        if first_52 is not None:
            assert last_51 is not None
            assert last_51 < first_52, (
                f"um evento de 5.2 ocorreu antes de a 5.1 concluir: {events}"
            )
            # E a 5.1 (build_online) foi exercida para TODOS os hosts antes de
            # qualquer evento de 5.2.
            hosts_with_51_before_52 = {
                c for (ph, _s, c) in events[:first_52] if ph == "5.1"
            }
            assert set(containers).issubset(hosts_with_51_before_52), (
                f"a 5.2 iniciou sem a 5.1 ter rodado para todos os hosts: {events}"
            )

        if failpoint in ("probe",):
            # A 5.2 iniciou (reisolar/verificar_egresso rodaram) e então falhou.
            assert first_52 is not None
            assert environment.failed_step in _STAGE_52_STEPS
            assert environment.ready is False
        else:
            # Happy path: completed through the final isolation gate.
            assert environment.failed_step is None
            assert environment.ready is True


@settings(max_examples=100, deadline=None)
@given(hosts=_hosts_strategy)
def test_51_failure_on_any_host_blocks_all_of_52(hosts: list[str]) -> None:
    # Feature: stage5-target-provisioning, Property 3: Ordem estrita 5.1 antes de 5.2.
    #
    # Reforça a cláusula "se a 5.1 falha, a 5.2 não executa" fazendo a abertura da
    # janela de egresso falhar para o PRIMEIRO host: nenhum evento de 5.2 pode
    # aparecer, independentemente de quantos hosts existam.
    containers = _resolver(hosts)
    recorder = _Recorder()
    window = _FakeEgressWindow(recorder, open_fail=frozenset({containers[0]}))
    setup = _FakeTargetSetup(recorder)
    prober = _FakeEgressProber(recorder)
    compose = _FakeComposeExecutor(recorder)

    tmp = Path(tempfile.mkdtemp(prefix="stage5_order_prop_fail_"))
    try:
        _seed_case(tmp, _SLUG)
        prov = CurationEnvironmentProvisioner(
            inspector=_isolated_inspector(containers),
            case_service=CaseService(data_dir=tmp),
            caldera=_FakeCaldera(),
            compose_executor=compose,
            state_inspector=_FakeStateInspector(containers),
            egress_window=window,
            egress_prober=prober,
            target_setup=setup,
        )
        environment = prov.provision(_params(hosts))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    assert all(ph != "5.2" for ph, _s, _c in recorder.events), (
        f"a 5.2 executou apesar da falha de 5.1 no 1º host: {recorder.events}"
    )
    assert environment.failed_step == "build_online"
    assert environment.ready is False
