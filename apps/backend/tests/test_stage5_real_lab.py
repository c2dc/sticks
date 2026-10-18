"""Stage-5 integration against the REAL lab Caldera + Docker (task 7.6).

⚠️  LAB-ONLY — **A execute exclusivamente na máquina de laboratório do
usuário. NÃO rodar no environment de desenvolvimento nem em CI.**

While ``test_provisionador_estagio5.py`` (task 7.2) drives
``CurationEnvironmentProvisioner.provision`` with a **fake** Caldera loader
and a **fake** read-only container inspector (no network, no daemon), this suite
provisions the **curated environment for real**: it loads the ordered Abilities
into the **real Caldera** at ``http://localhost:8888`` and **verifies isolation
read-only against the real Docker daemon** (the lab containers on the
``internal: true`` networks). It is the Stage-5 counterpart of the end-to-end
lab test ``test_e2e_real_lab.py`` (task 15.3).

Why it is skipped by default
----------------------------
The current dev environment (Windows, no lab Caldera + no hardened Docker attack
environment) cannot satisfy containment. Per the design's *"Decisão registrada —
endurecimento via compose separado (Opção A)"*, isolation only passes on the
hardened lab machine, where ``docker/docker-compose.hardened.yml`` keeps the
target containers (``kali``, ``nginx``, ``db``) **exclusively** on the
``internal: true`` networks, with no ``local-network`` bridge and no external
DNS.

This test is therefore guarded **two ways** so it never runs in dev/CI:

1. The ``@pytest.mark.lab`` marker — ``addopts = -m "not lab"`` in
   ``pyproject.toml`` deselects it by default.
2. ``@pytest.mark.skipif(os.getenv("PIPELINE_UI_LAB") != "1", ...)`` — even if
   the marker filter is overridden, it stays **skipped** unless the researcher
   explicitly opts in on the lab machine.

It uses the **REAL** :class:`CalderaClient` (no mock transport) and the **REAL**
read-only Docker isolation inspector (:class:`DockerSdkInspector` via
``inspector=None``, which :func:`inspect_isolation` builds lazily from the
ambient Docker environment) — no mocks, no fakes for the external systems. See
``LAB.md`` for how to bring the hardened stack up.

How to run it on the lab machine
--------------------------------
1. Apply the hardened compose (lab-only reference — see the design decision):
   ``docker compose -f docker/docker-compose.yml -f docker/docker-compose.hardened.yml up -d``
2. Confirm Caldera answers at ``http://localhost:8888`` (header ``KEY``).
3. From ``ui/backend`` with the venv active:
   ``$env:PIPELINE_UI_LAB = "1"; pytest -m lab``

_Requirements: 9.4, 9.5, 9.6_
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from app.models.enums import TranslationSource
from app.services.caldera import CalderaClient
from app.services.campaign_service import CaseService
from app.services.dag.dag_service import DagView
from app.services.environment.operationalization import (
    EnvironmentParameters,
    EnvironmentPlatform,
)
from app.services.environment.operationalizer import CurationOperationalizer
from app.services.environment.provisioner import CurationEnvironmentProvisioner

# ---------------------------------------------------------------------------
# The curated case(s) to provision against the real lab. Kept to 1–2 cases, and
# overridable via PIPELINE_UI_LAB_CASES (comma-separated slugs) — the same knob
# the end-to-end lab test uses — without editing this file.
# ---------------------------------------------------------------------------

_DEFAULT_LAB_CASES: tuple[str, ...] = ("shadowray",)


def _lab_cases() -> list[str]:
    """The curated case slugs to provision against the real lab (1–2, overridable)."""
    override = os.getenv("PIPELINE_UI_LAB_CASES", "").strip()
    if override:
        return [slug.strip() for slug in override.split(",") if slug.strip()]
    return list(_DEFAULT_LAB_CASES)


# Both guards: the ``lab`` marker (deselected by default via pyproject addopts)
# and a skipif on the opt-in env var, so the test is inert in dev/CI regardless
# of how pytest is invoked.
pytestmark = [
    pytest.mark.lab,
    pytest.mark.skipif(
        os.getenv("PIPELINE_UI_LAB") != "1",
        reason=(
            "requires the real lab Caldera + hardened Docker environment "
            "(task 7.6); set PIPELINE_UI_LAB=1 on the lab machine to run"
        ),
    ),
]


@pytest.fixture(scope="module")
def real_caldera() -> Iterator[CalderaClient]:
    """The REAL CalderaClient pointed at localhost:8888 (no mock transport).

    Verifies availability up front: if the lab Caldera is not answering, the
    whole Stage-5 lab run is skipped with a clear message rather than failing —
    the researcher simply hasn't brought the hardened stack up yet.
    """
    client = CalderaClient()  # real settings -> http://localhost:8888, header KEY
    availability = client.check_availability()
    if not availability.available:
        client.close()
        pytest.skip(
            "lab Caldera não respondeu em http://localhost:8888 — suba a stack "
            "endurecida (docker-compose.hardened.yml) antes de rodar o teste de "
            "laboratório do Estágio 5"
        )
    try:
        yield client
    finally:
        client.close()


def _derive_parameters(caso_slug: str) -> EnvironmentParameters:
    """Derive the real curated ``EnvironmentParameters`` of a case from disk.

    Reuses the real :class:`CurationOperationalizer` (Stage-4), which reads the
    curated DAG nodes from ``sticks/data`` and resolves the target containers /
    services with no network and no AI. The ``DagView`` only signals the curated
    path; the derivation itself reads the curated nodes directly from disk.
    """
    case_service = CaseService()
    operacionalizador = CurationOperationalizer(case_service=case_service)
    dag_view = DagView(
        campaign_id=caso_slug,
        name=case_service.list_cases().get(caso_slug, caso_slug),
        curated=True,
        can_advance=True,
    )
    produzido = operacionalizador.operationalize(caso_slug, dag_view)
    return produzido.parameters


@pytest.mark.parametrize("caso_slug", _lab_cases())
def test_real_provisionar_loads_abilities_and_verifies_isolation(
    caso_slug: str,
    real_caldera: CalderaClient,
) -> None:
    """Provision the curated environment against the REAL lab and verify isolation.

    On the lab machine (hardened compose applied, containment satisfied) this:

    * derives the real curated ``EnvironmentParameters`` from the case's curated
      DAG nodes on disk (no network, no AI — Req. 9.4);
    * loads the ordered Abilities + Adversary into the **real Caldera** in DAG
      order via ``CalderaClient.load_abilities``/``load_adversary`` (Req. 9.5);
    * **verifies isolation read-only against the real Docker daemon** — the
      ``inspector=None`` default makes ``inspect_isolation`` build the real
      :class:`DockerSdkInspector`, which only inspects (never starts/creates/
      modifies containers or networks — Req. 9.6).

    The body uses the REAL :class:`CalderaClient` and the REAL read-only Docker
    inspector (no mocks). It is inert off the lab machine because the module
    guards keep it skipped.
    """
    params = _derive_parameters(caso_slug)
    assert params.campaign_id == caso_slug
    assert params.platform is EnvironmentPlatform.CONTAINER

    provisioner = CurationEnvironmentProvisioner(
        # inspector=None -> inspect_isolation builds the REAL read-only
        # DockerSdkInspector from the ambient Docker environment (Req. 9.6).
        inspector=None,
        case_service=CaseService(),
        caldera=real_caldera,  # the REAL Caldera (no mock transport) — Req. 9.5
    )

    environment = provisioner.provision(params)

    # Saída tipada do Estágio 5 carimbada com a origin de curation (Req. 9.4).
    assert environment.campaign_id == caso_slug
    assert environment.platform is EnvironmentPlatform.CONTAINER
    assert environment.origin is TranslationSource.HUMAN_CURATION
    assert environment.compose_file is not None

    # The real lab environment uses the curated docker-compose containers
    # (apenas contêineres — Req. 9.4): at least one target container resolved.
    assert environment.target_containers, (
        "o environment curated deve resolver ao menos um contêiner-target"
    )

    # Abilities ordenadas carregadas no Caldera real na ordem do DAG (Req. 9.5).
    assert environment.loaded_abilities is True, (
        "as Abilities curadas devem ter sido carregadas no Caldera real"
    )

    # Isolamento verificado read-only contra o Docker real ANTES de complete
    # (Req. 9.6): na máquina de laboratório endurecida todos os contêineres-target
    # estão isolados, então o result geral aprova e cada verdict é isolated.
    assert environment.isolation.approved is True, (
        "na máquina de laboratório endurecida o isolation deve aprovar; "
        f"não isolados: {[v.container for v in environment.isolation.non_isolated]}"
    )
    assert environment.isolation.verdicts, "deve haver verdict por contêiner-target"
    for verdict in environment.isolation.verdicts:
        assert verdict.isolated is True
        assert verdict.reasons == []  # nenhum motivo de failure quando isolated
