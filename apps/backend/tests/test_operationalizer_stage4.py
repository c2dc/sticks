"""Example-based tests for ``CurationOperationalizer`` (task 5.2).

The Stage-4 (operationalization) curated seam must, on
``operationalize(campaign, dag)``, produce the **two separate typed artifacts**
(Req. 8.4, 8.7):

1. **Ordered Abilities** — reusing ``StageService.assemble_stage2`` (the
   ``Stage2Output`` with ``atomic_ordering`` and
   ``translation_source=curadoria_humana``) — Req. 8.2, 8.5, 8.6.
2. **``EnvironmentParameters``** — DERIVED from the curated DAG nodes
   (Req. 8.3): ``attacker_commands`` → ``parse_command`` → internal destinations
   → ``container_for_ip`` resolves ``address``/``container``;
   ``target_vuln_host_a``/``_b`` → ``vulnerabilities``; ``source_host_type``/
   ``target_host_type`` → ``host_type``; ``network_context`` → notes; services
   inferred from the commands; ``platform = CONTAINER``.

Everything here is pure/in-memory: a temp curated-case ``data_dir`` injected into
the ``CaseService``. No real network, no AI (Req. 11.3, 11.4).

_Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 11.3, 11.4, 16.2_
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.models.enums import TranslationSource
from app.services.campaign_service import CaseService
from app.services.dag.dag_service import DagView
from app.services.environment.operationalization import (
    EnvironmentParameters,
    EnvironmentPlatform,
)
from app.services.environment.operationalizer import (
    CurationOperationalizer,
    ProducedOperationalization,
    OperationalizerByOrigin,
)

_SLUG = "shadowray"  # a registered curated slug (CURATED_CASES)

# A lab-internal target IP that resolves to a known container (192.168.20.30 -> nginx).
_INTERNAL_IP = "192.168.20.30"
_INTERNAL_CONTAINER = "nginx"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_case(
    data_dir: Path,
    slug: str,
    *,
    structural_nodes: list[dict] | None = None,
    with_abilities: bool = True,
    with_adversary: bool = True,
) -> None:
    """Seed a minimal curated case (abilities + adversary + dag) under ``data_dir``."""
    abilities = (
        [
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
        if with_abilities
        else []
    )
    _write_json(data_dir / "api" / f"{slug}_dag-ability.json", abilities)

    if with_adversary:
        _write_json(
            data_dir / "api" / f"{slug}_dag-adversary.json",
            {
                "id": f"{slug}-adv",
                "name": "Seed Adversary",
                "description": "seed",
                # Reversed order on purpose: ordering must follow atomic_ordering.
                "atomic_ordering": [f"{slug}-ab-2", f"{slug}-ab-1"],
            },
        )

    nodes = structural_nodes if structural_nodes is not None else [
        {
            "node_id": "n0",
            "node_index": 0,
            "technique_id": "T1190",
            "attacker_commands": [f'curl "http://{_INTERNAL_IP}:5000/?cmd=id"'],
            "target_vuln_host_a": [
                "apt-get install -y python3 && pip install Flask && "
                "flask run --host 0.0.0.0 --port 5000"
            ],
            "target_vuln_host_b": [],
            "source_host_type": "kali",
            "target_host_type": "macos",
            "network_context": "Attacker can directly access Host A",
        }
    ]
    _write_json(
        data_dir / "dag" / f"{slug}_dag.json",
        {
            "campaign_name": "Seed",
            "structural_nodes": nodes,
            "metadata": {"total_techniques": len(nodes)},
        },
    )


@pytest.fixture()
def data_dir() -> Iterator[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_estagio4_"))
    (tmp / "api").mkdir(parents=True, exist_ok=True)
    (tmp / "dag").mkdir(parents=True, exist_ok=True)
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _stub_dag(campaign: str = _SLUG) -> DagView:
    """A minimal curated ``DagView`` (operationalize reads nodes from disk)."""
    return DagView(campaign_id=campaign, name="Seed", curated=True, can_advance=True)


def _operacionalizador(data_dir: Path) -> CurationOperationalizer:
    return CurationOperationalizer(case_service=CaseService(data_dir=data_dir))


# ---------------------------------------------------------------------------
# Protocol conformance + origin stamping (Req. 8.6, 16.2)
# ---------------------------------------------------------------------------


def test_curadoria_satisfies_the_protocol(data_dir: Path) -> None:
    """``CurationOperationalizer`` satisfies the Stage-4 Protocol (Req. 8.6, 16.2)."""
    op = _operacionalizador(data_dir)
    assert isinstance(op, OperationalizerByOrigin)
    assert op.origin() is TranslationSource.HUMAN_CURATION


def test_a_stub_ai_impl_also_satisfies_the_protocol() -> None:
    """A different-origin stub also satisfies the seam (extensibility — Req. 16.2, 16.4)."""

    class StubGeradoIa:
        def origin(self) -> TranslationSource:
            return TranslationSource.HUMAN_CURATION  # placeholder; AI value out of scope

        def operationalize(self, campaign: str, dag: DagView) -> ProducedOperationalization:
            return ProducedOperationalization(
                campaign_id=campaign, parameters=EnvironmentParameters(campaign_id=campaign)
            )

    assert isinstance(StubGeradoIa(), OperationalizerByOrigin)


# ---------------------------------------------------------------------------
# Dual typed Saída: Abilities + EnvironmentParameters separated (Req. 8.4, 8.7)
# ---------------------------------------------------------------------------


def test_produces_two_separate_typed_artifacts(data_dir: Path) -> None:
    """Saída dupla: Abilities ordenadas E EnvironmentParameters, em campos distintos.
    _Requirements: 8.2, 8.4, 8.5, 8.7, 8.8_"""
    _seed_case(data_dir, _SLUG)
    produzida = _operacionalizador(data_dir).operationalize(_SLUG, _stub_dag())

    assert isinstance(produzida, ProducedOperationalization)
    assert produzida.campaign_id == _SLUG
    assert produzida.origin is TranslationSource.HUMAN_CURATION

    # Artifact 1 — ordered Abilities (reused Stage2Output).
    assert produzida.abilities is not None
    assert produzida.abilities.translation_source is TranslationSource.HUMAN_CURATION
    # atomic_ordering drives the order (ab-2 before ab-1).
    assert produzida.abilities.ordering == [f"{_SLUG}-ab-2", f"{_SLUG}-ab-1"]

    # Artifact 2 — EnvironmentParameters (separate typed object).
    assert isinstance(produzida.parameters, EnvironmentParameters)
    assert produzida.parameters.platform is EnvironmentPlatform.CONTAINER
    assert produzida.parameters.origin is TranslationSource.HUMAN_CURATION


# ---------------------------------------------------------------------------
# Derivation of EnvironmentParameters from curated nodes (Req. 8.3, 17.1)
# ---------------------------------------------------------------------------


def test_derives_host_address_and_container_from_commands(data_dir: Path) -> None:
    """attacker_commands -> parse_command -> container_for_ip resolves address/container.
    _Requirements: 8.3_"""
    _seed_case(data_dir, _SLUG)
    params = _operacionalizador(data_dir).operationalize(_SLUG, _stub_dag()).parameters

    assert len(params.hosts) == 1
    host = params.hosts[0]
    assert host.address == _INTERNAL_IP
    assert host.container == _INTERNAL_CONTAINER


def test_derives_host_type_and_vulnerabilities_and_network_notes(data_dir: Path) -> None:
    """target_host_type -> host_type; target_vuln_host_a -> vulnerabilities; network_context -> notas.
    _Requirements: 8.3_"""
    _seed_case(data_dir, _SLUG)
    params = _operacionalizador(data_dir).operationalize(_SLUG, _stub_dag()).parameters

    host = params.hosts[0]
    assert host.host_type == "macos"
    assert len(host.vulnerabilities) == 1
    assert "flask" in host.vulnerabilities[0].lower()

    assert params.network_context == ["Attacker can directly access Host A"]


def test_infers_services_from_commands(data_dir: Path) -> None:
    """Services are inferred from the commands (Flask/HTTP here) with technique origin.
    _Requirements: 8.3_"""
    _seed_case(data_dir, _SLUG)
    params = _operacionalizador(data_dir).operationalize(_SLUG, _stub_dag()).parameters

    descricoes = [s.description for s in params.services]
    assert any("Flask" in d or "HTTP" in d for d in descricoes)
    # The inferred service carries the originating technique id.
    assert any(s.origin_technique == "T1190" for s in params.services)


def test_resolves_second_internal_host_from_nested_ssh(data_dir: Path) -> None:
    """Nested ssh to a second internal host yields a second resolved host (host_b).
    _Requirements: 8.3_"""
    nodes = [
        {
            "node_id": "n0",
            "technique_id": "T1021",
            # Nested ssh: outer host 192.168.20.30 (nginx), inner 192.168.30.40 (db).
            "attacker_commands": [
                "ssh root@192.168.20.30 'ssh root@192.168.30.40 \"id\"'"
            ],
            "target_vuln_host_a": ["setup a"],
            "target_vuln_host_b": ["setup b"],
            "target_host_type": "linux",
            "network_context": "nested hop",
        }
    ]
    _seed_case(data_dir, _SLUG, structural_nodes=nodes)
    params = _operacionalizador(data_dir).operationalize(_SLUG, _stub_dag()).parameters

    rotulos = {h.label: h for h in params.hosts}
    assert "host_a" in rotulos and "host_b" in rotulos
    assert rotulos["host_a"].container == "nginx"
    assert rotulos["host_b"].container == "db"
    assert rotulos["host_a"].vulnerabilities == ["setup a"]
    assert rotulos["host_b"].vulnerabilities == ["setup b"]


def test_merges_same_target_container_across_nodes(data_dir: Path) -> None:
    """Two nodes hitting the same container collapse to one host, merging vulns.
    _Requirements: 8.3_"""
    nodes = [
        {
            "node_id": "n0",
            "technique_id": "T1190",
            "attacker_commands": [f'curl "http://{_INTERNAL_IP}/a"'],
            "target_vuln_host_a": ["setup A"],
            "target_host_type": "macos",
        },
        {
            "node_id": "n1",
            "technique_id": "T1059",
            "attacker_commands": [f'curl "http://{_INTERNAL_IP}/b"'],
            "target_vuln_host_a": ["setup B"],
            "target_host_type": "macos",
        },
    ]
    _seed_case(data_dir, _SLUG, structural_nodes=nodes)
    params = _operacionalizador(data_dir).operationalize(_SLUG, _stub_dag()).parameters

    nginx_hosts = [h for h in params.hosts if h.container == _INTERNAL_CONTAINER]
    assert len(nginx_hosts) == 1
    assert nginx_hosts[0].vulnerabilities == ["setup A", "setup B"]


# ---------------------------------------------------------------------------
# No network / no AI; graceful empties (Req. 11.3, 11.4)
# ---------------------------------------------------------------------------


def test_delegates_abilities_to_injected_stage_service(data_dir: Path) -> None:
    """When a StageService is injected, the Abilities come from assemble_stage2.
    _Requirements: 8.2, 8.5_"""
    from app.services.stage.stage_service import Stage2Output, Stage2Result

    sentinel = Stage2Output(ordering=["sentinel-id"], has_abilities=True)
    calls: list[str] = []

    class FakeStageService:
        """Minimal session-less stand-in exposing the surface used by the seam."""

        _case_service = CaseService(data_dir=data_dir)

        def assemble_stage2(self, slug: str) -> Stage2Result:
            calls.append(slug)
            from app.services.stage.stage_service import Stage2Input

            return Stage2Result(
                campaign_id=slug,
                input=Stage2Input(campaign_id=slug, name=slug),
                output=sentinel,
            )

    _seed_case(data_dir, _SLUG)
    op = CurationOperationalizer(stage_service=FakeStageService())  # type: ignore[arg-type]

    produzida = op.operationalize(_SLUG, _stub_dag())

    # The Abilities artifact is exactly what the injected service returned.
    assert produzida.abilities is sentinel
    assert calls == [_SLUG]
    # Parameters are still derived from the (shared) curated DAG on disk.
    assert produzida.parameters.hosts and produzida.parameters.hosts[0].container == _INTERNAL_CONTAINER


def test_missing_dag_yields_empty_parameters_without_raising(data_dir: Path) -> None:
    """A case without a curated DAG yields empty parameters (no raise).
    _Requirements: 11.3, 11.4_"""
    # Seed abilities/adversary but NO dag file.
    _write_json(data_dir / "api" / f"{_SLUG}_dag-ability.json", [])
    produzida = _operacionalizador(data_dir).operationalize(_SLUG, _stub_dag())

    assert produzida.parameters.hosts == []
    assert produzida.parameters.services == []
    assert produzida.parameters.network_context == []
    assert produzida.parameters.platform is EnvironmentPlatform.CONTAINER
