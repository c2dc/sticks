"""Property-based test for Stage-5 isolation as a conclusion pre-condition (task 7.4).

# Feature: pipeline-seis-estagios, Property 8: Isolamento é pré-condição de
# conclusão do Estágio 5. Para qualquer ambiente provisionado, o Estágio 5 só
# conclui quando o resultado geral de isolamento é aprovado (todos os
# contêineres-alvo exclusivamente em redes ``internal: true``, sem bridge externo
# e sem DNS externo); qualquer reprovação mantém o Estágio 5 não concluído e
# identifica o contêiner não isolado com a razão.

Validates: Requirements 9.6, 9.7, 14.1, 14.2, 14.3

This test drives ``CuradoriaProvisionadorDeAmbiente.provisionar`` through a FAKE
read-only :class:`ContainerInspector` (the same ``FakeInspector`` shape used by
the example tests in ``test_provisionador_estagio5.py``), serving fabricated
``ContainerNetworkInfo`` / ``NetworkInfo`` from Hypothesis-generated data. NO real
Docker daemon, NO network and NO AI are touched — everything is pure in-memory,
so it is safe on the Windows dev environment. The curated case is seeded into a
temp ``data_dir`` injected into the ``CaseService`` and the Caldera loader is a
no-network fake, so the only variable under test is the isolation verdict.

Per target container the generators produce one of three network configs:
- **isolated**: attached only to an ``internal: true`` lab network, acceptable
  (loopback/internal) DNS -> SHOULD be isolated;
- **with external bridge**: additionally attached to ``local-network`` /
  ``bridge`` / ``host`` -> SHOULD NOT be isolated (Req. 14.2);
- **with external DNS**: an external resolver such as ``8.8.8.8`` -> SHOULD NOT
  be isolated (Req. 14.2).

An INDEPENDENT oracle (``_expected_isolated``) recomputes isolation directly from
the generated config kind — the assertions never trust the implementation to
decide what "isolated" means. We assert that ``ambiente.isolamento.aprovado`` is
True IFF **every** target container is isolated per the oracle, that the
per-container verdicts match the oracle in order, and that each non-isolated
container is reported with at least one human-readable reason (Req. 9.7, 14.3).

_Requisitos: 9.6, 9.7, 14.1, 14.2, 14.3_
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.case_service import CaseService
from app.services.containment.isolation import (
    ContainerNetworkInfo,
    ContainerNotFound,
    NetworkInfo,
    NetworkNotFound,
)
from app.services.containment.subnets import INTERNAL_NETWORK_SUBNETS
from app.services.environment.compose_executor import ResultadoComando
from app.services.environment.operacionalizacao import (
    HostAlvo,
    ParametrosDeAmbiente,
)
from app.services.environment.provisionador import CuradoriaProvisionadorDeAmbiente
from app.services.environment.saude import ContainerStateInfo


# ---------------------------------------------------------------------------
# Fakes (no network, no Docker daemon, no AI) — same shape as the example tests
# ---------------------------------------------------------------------------


@dataclass
class FakeInspector:
    """In-memory read-only :class:`ContainerInspector` (no Docker daemon).

    Mirrors the ``FakeInspector`` of ``test_provisionador_estagio5.py``: it just
    looks up dictionaries populated from generated data. Unknown containers /
    networks raise the same lookup errors the real SDK adapter raises, so the
    fail-closed paths are exercised too.
    """

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


class FakeCaldera:
    """No-network Caldera loader so ``provisionar`` never touches a daemon."""

    def load_abilities(self, abilities: list[object]) -> list[object]:
        return list(abilities)

    def load_adversary(self, adversary: object) -> object:
        return adversary


class FakeComposeExecutor:
    """No-subprocess ``ComposeExecutor`` so ``provisionar`` never runs ``docker``.

    A etapa ``subir_conteineres`` do pipeline (task 3.2) chama o ``ComposeExecutor``
    injetado; sem este *fake* a curadoria construiria o ``DockerComposeExecutor``
    real e tocaria o daemon. ``codigo=0`` deixa o pipeline avançar até a etapa de
    isolamento — a única variável sob teste nesta propriedade.
    """

    def subir(self, compose_file: str, *, on_log: object = None) -> ResultadoComando:
        return ResultadoComando(codigo=0, linhas=[])

    def derrubar(self, compose_file: str, *, on_log: object = None) -> ResultadoComando:
        return ResultadoComando(codigo=0, linhas=[])


class FakeStateInspector:
    """No-daemon ``ContainerStateInspector`` reporting every container healthy.

    A etapa ``aguardar_saude`` (task 3.2) usa o inspector de estado injetado; este
    *fake* reporta todos os contêineres-alvo ``running`` + ``healthy`` para que o
    pipeline alcance a etapa de isolamento sem tocar o daemon Docker.
    """

    def get_container_state(self, container: str) -> ContainerStateInfo:
        return ContainerStateInfo(name=container, running=True, health="healthy")


# ---------------------------------------------------------------------------
# Curated-case temp fixtures (read-only w.r.t. real data) — mirrors the example
# ---------------------------------------------------------------------------

_SLUG = "shadowray"  # a registered curated slug (CURATED_CASES)
# A genuinely-internal lab network (internal: true, known subnet).
_NET = "caldera-kali-network"
_NET_SUBNET = INTERNAL_NETWORK_SUBNETS[_NET]

# DNS pools: loopback/internal are acceptable; the external pool breaks isolation.
_ACCEPTABLE_DNS = ("127.0.0.1", "192.168.10.10", "192.168.20.20")
_EXTERNAL_DNS = ("8.8.8.8", "1.1.1.1", "9.9.9.9", "resolver.example.com")
# Known external bridge names the isolation rule always rejects (Req. 14.2).
_EXTERNAL_BRIDGES = ("local-network", "bridge", "host")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_case(data_dir: Path, slug: str) -> None:
    """Seed a minimal curated case so ``provisionar`` can load the abilities."""
    _write_json(
        data_dir / "api" / f"{slug}_dag-ability.json",
        [
            {
                "ability_id": f"{slug}-ab-1",
                "name": "first",
                "technique_id": "T1190",
                "executors": [
                    {"name": "sh", "platform": "linux", "command": "echo 1"}
                ],
            }
        ],
    )
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


# ---------------------------------------------------------------------------
# Generated per-container config: one of three network kinds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenContainer:
    """A generated target container with a chosen network configuration.

    ``kind`` picks the config shape the fake inspector serves:
    - ``"isolated"``: only the internal lab network + acceptable DNS;
    - ``"bridge"``: additionally attached to an external bridge (Req. 14.2);
    - ``"external_dns"``: an external DNS resolver configured (Req. 14.2).
    """

    name: str
    kind: str
    bridge: str
    external_dns: str


def _container_strategy() -> st.SearchStrategy[GenContainer]:
    return st.builds(
        GenContainer,
        name=st.uuids().map(lambda u: f"alvo-{u.hex[:8]}"),
        kind=st.sampled_from(("isolated", "bridge", "external_dns")),
        bridge=st.sampled_from(_EXTERNAL_BRIDGES),
        external_dns=st.sampled_from(_EXTERNAL_DNS),
    )


def _unique_containers() -> st.SearchStrategy[list[GenContainer]]:
    return st.lists(
        _container_strategy(),
        min_size=1,
        max_size=5,
        unique_by=lambda c: c.name,
    )


def _expected_isolated(container: GenContainer) -> bool:
    """Independent oracle: a container is isolated IFF its kind is ``isolated``."""
    return container.kind == "isolated"


def _build_inspector(containers: list[GenContainer]) -> FakeInspector:
    """Populate a FakeInspector from generated containers (bridges stay unregistered).

    The internal lab network is registered as ``internal: true`` with a known
    subnet; external bridge names are intentionally NOT registered (the rule
    short-circuits on the name), matching the real inspector behaviour.
    """
    inspector = FakeInspector()
    inspector.networks[_NET] = NetworkInfo(
        name=_NET, internal=True, subnets=(_NET_SUBNET,)
    )
    for c in containers:
        network_names: tuple[str, ...] = (_NET,)
        dns: tuple[str, ...] = ("127.0.0.1",)
        if c.kind == "bridge":
            network_names = (_NET, c.bridge)
        elif c.kind == "external_dns":
            dns = ("127.0.0.1", c.external_dns)
        inspector.containers[c.name] = ContainerNetworkInfo(
            name=c.name, network_names=network_names, dns=dns
        )
    return inspector


def _params(containers: list[str]) -> ParametrosDeAmbiente:
    hosts = [HostAlvo(rotulo=f"h{i}", container=c) for i, c in enumerate(containers)]
    return ParametrosDeAmbiente(caso_id=_SLUG, hosts=hosts)


# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(containers=_unique_containers())
def test_property8_isolation_is_precondition_of_stage5(
    containers: list[GenContainer],
) -> None:
    """Stage-5 concludes (``isolamento.aprovado``) IFF every target is isolated.

    # Feature: pipeline-seis-estagios, Property 8: Isolamento é pré-condição de
    # conclusão do Estágio 5.
    Validates: Requirements 9.6, 9.7, 14.1, 14.2, 14.3
    """
    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_estagio5_prop_"))
    try:
        _seed_case(tmp, _SLUG)
        names = [c.name for c in containers]
        prov = CuradoriaProvisionadorDeAmbiente(
            inspector=_build_inspector(containers),
            case_service=CaseService(data_dir=tmp),
            caldera=FakeCaldera(),
            compose_executor=FakeComposeExecutor(),
            state_inspector=FakeStateInspector(),
        )

        ambiente = prov.provisionar(_params(names))

        # Independent oracle: expected per-container and overall verdicts.
        expected_isolated = {c.name: _expected_isolated(c) for c in containers}
        expected_all = all(expected_isolated.values())

        isolamento = ambiente.isolamento
        # Overall conclusion gate: approved IFF every target container isolated.
        assert isolamento.aprovado is expected_all
        assert ambiente.containers_alvo == names

        # Per-container verdicts agree with the oracle, in the given order.
        assert [v.container for v in isolamento.veredictos] == names
        for verdict in isolamento.veredictos:
            assert verdict.isolado is expected_isolated[verdict.container]
            if verdict.isolado:
                assert verdict.razoes == []
            else:
                # Req. 9.7 / 14.3: a non-isolated container is reported with reason.
                assert verdict.razoes != []

        if not expected_all:
            # Stage 5 does NOT conclude; offending set matches the oracle exactly.
            assert isolamento.aprovado is False
            nao_isolados = {v.container for v in isolamento.nao_isolados}
            expected_offending = {
                name for name, ok in expected_isolated.items() if not ok
            }
            assert nao_isolados == expected_offending
            assert nao_isolados  # at least one offender identified
            for verdict in isolamento.nao_isolados:
                assert verdict.razoes != []
        else:
            assert isolamento.aprovado is True
            assert isolamento.nao_isolados == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _isolated_only() -> st.SearchStrategy[list[GenContainer]]:
    """A set built to be fully isolated by construction (targeted direction)."""
    return st.lists(
        st.builds(
            lambda u: GenContainer(
                name=f"iso-{u.hex[:8]}",
                kind="isolated",
                bridge="local-network",
                external_dns="8.8.8.8",
            ),
            st.uuids(),
        ),
        min_size=1,
        max_size=4,
        unique_by=lambda c: c.name,
    )


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(containers=_isolated_only())
def test_property8_all_isolated_targets_conclude(
    containers: list[GenContainer],
) -> None:
    """A set built fully isolated always concludes Stage 5 (targeted direction).

    # Feature: pipeline-seis-estagios, Property 8: Isolamento é pré-condição de
    # conclusão do Estágio 5.
    Validates: Requirements 9.6, 14.1, 14.3
    """
    # Oracle sanity: everything built here is isolated by construction.
    assert all(_expected_isolated(c) for c in containers)

    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_estagio5_prop_"))
    try:
        _seed_case(tmp, _SLUG)
        names = [c.name for c in containers]
        prov = CuradoriaProvisionadorDeAmbiente(
            inspector=_build_inspector(containers),
            case_service=CaseService(data_dir=tmp),
            caldera=FakeCaldera(),
            compose_executor=FakeComposeExecutor(),
            state_inspector=FakeStateInspector(),
        )

        ambiente = prov.provisionar(_params(names))

        assert ambiente.isolamento.aprovado is True
        assert ambiente.isolamento.nao_isolados == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
