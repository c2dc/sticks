"""Stage-6 integration against the REAL lab Caldera + Docker (task 8.9).

⚠️  LAB-ONLY — **A execute exclusivamente na máquina de laboratório do
usuário. NÃO rodar no environment de desenvolvimento nem em CI.**

While ``test_execution_stage6.py`` (task 8.2) drives
:class:`~app.services.environment.execution.ExecutionStageService` with a **real**
``OperationRunner`` but **fake** externals (Caldera via ``httpx.MockTransport``
and a fake isolation verifier — no network, no daemon), this suite drives the
*real* Stage-6 flow end to end against the lab:

1. **Provisiona o environment curated de verdade** — carrega as Abilities ordenadas
   no **Caldera real** em ``http://localhost:8888`` e **verifica o isolation
   read-only contra o Docker real** (os contêineres do laboratório nas networks
   ``internal: true``) via :class:`CurationEnvironmentProvisioner`.
2. **Executa o Estágio 6 de verdade** — ``ExecutionStageService.execute`` reusa o
   **``OperationRunner`` real** (Caldera real + verificador de isolation real
   :func:`inspect_isolation`, sem mocks) com os quatro gates satisfeitos
   (confirmação → contenção → isolation → disponibilidade), execução ÚNICA, e
   persiste ``AbilityResult`` + ``AuditLogEntry`` por command (Req. 10.3).
3. **Mede a suficiência procedural** — :class:`MeasurementService` deriva as
   :class:`SufficiencyMetrics` dos results por Ability + o DAG curated e
   consolida o :class:`ExecutionReport`.
4. **Desprovisiona o environment** — ``EnvironmentProvisioner.deprovision`` é
   invocado ao final, em success **ou** failure das técnicas (Req. 10.8, 12.1).

É a contraparte, para o Estágio 6, do teste de laboratório do Estágio 5
(``test_stage5_real_lab.py``) e do fim-a-fim (``test_e2e_real_lab.py``).

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
read-only Docker isolation inspector (:func:`inspect_isolation` with no injected
inspector, which builds a read-only :class:`DockerSdkInspector` from the ambient
Docker environment) — no mocks, no fakes for the external systems. See
``LAB.md`` for how to bring the hardened stack up.

How to run it on the lab machine
--------------------------------
1. Apply the hardened compose (lab-only reference — see the design decision):
   ``docker compose -f docker/docker-compose.yml -f docker/docker-compose.hardened.yml up -d``
2. Confirm Caldera answers at ``http://localhost:8888`` (header ``KEY``).
3. From ``ui/backend`` with the venv active:
   ``$env:PIPELINE_UI_LAB = "1"; pytest -m lab``

_Requirements: 10.3, 10.8, 12.1_
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.enums import TranslationSource
from app.services.audit import AuditLogger
from app.services.caldera import CalderaClient
from app.services.campaign_service import CaseService
from app.services.containment.isolation import inspect_isolation
from app.services.environment.execution import ExecutionStageService
from app.services.environment.measurement import MeasurementService, AbilityMeasurementResult
from app.services.environment.operationalization import EnvironmentPlatform
from app.services.environment.operationalizer import CurationOperationalizer
from app.services.environment.provisioner import CurationEnvironmentProvisioner
from app.services.dag.dag_service import DagView
from app.services.operation.runner import OperationRunner, RunOutcome

# ---------------------------------------------------------------------------
# The curated case(s) to drive through Stage 6 on the lab machine. Kept to 1–2
# cases per the task ("execute 1–2 campaigns curados ... reais"). Slugs match the
# CaseService registry / the files under sticks/data. A researcher may override
# the selection via PIPELINE_UI_LAB_CASES (comma-separated slugs) — the same
# knob the Stage-5 and end-to-end lab tests use — without editing this file.
# ---------------------------------------------------------------------------

_DEFAULT_LAB_CASES: tuple[str, ...] = ("shadowray",)


def _lab_cases() -> list[str]:
    """The curated case slugs to run against the real lab (1–2, overridable)."""
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
            "(task 8.9); set PIPELINE_UI_LAB=1 on the lab machine to run"
        ),
    ),
]


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A temp-file SQLite engine + schema for the lab run.

    Uses SQLite so the run is self-contained; on the lab machine the researcher
    may point ``PIPELINE_UI_DATABASE_URL`` at the real PostgreSQL if they want
    the audit trail / results persisted there instead — the service below writes
    to whatever engine this fixture builds.
    """
    db_url = os.getenv("PIPELINE_UI_DATABASE_URL")
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_stage6_real_lab_")
    db_path = Path(tmp_dir) / "stage6_real_lab.db"
    settings_obj = Settings(
        database_url=db_url or f"sqlite:///{db_path.as_posix()}"
    )

    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)

    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    try:
        yield factory
    finally:
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
            os.rmdir(db_path.parent)


@pytest.fixture(scope="module")
def real_caldera() -> Iterator[CalderaClient]:
    """The REAL CalderaClient pointed at localhost:8888 (no mock transport).

    Verifies availability up front: if the lab Caldera is not answering, the
    whole Stage-6 lab run is skipped with a clear message rather than failing —
    the researcher simply hasn't brought the hardened stack up yet.
    """
    client = CalderaClient()  # real settings -> http://localhost:8888, header KEY
    availability = client.check_availability()
    if not availability.available:
        client.close()
        pytest.skip(
            "lab Caldera não respondeu em http://localhost:8888 — suba a stack "
            "endurecida (docker-compose.hardened.yml) antes de rodar o teste de "
            "laboratório do Estágio 6"
        )
    try:
        yield client
    finally:
        client.close()


def _runner_factory(caldera: CalderaClient):
    """Factory ``(session) -> OperationRunner`` wiring the REAL externals.

    Builds the runner with the REAL Caldera client and the REAL read-only Docker
    isolation verifier (:func:`inspect_isolation` with no injected inspector,
    which lazily builds a :class:`DockerSdkInspector` from the ambient Docker
    environment). No mocks — exactly the production wiring the API layer uses.
    """

    def _factory(session: Session) -> OperationRunner:
        return OperationRunner(
            session=session,
            caldera=caldera,
            audit_logger=AuditLogger(session),
            isolation_verifier=inspect_isolation,
        )

    return _factory


@pytest.mark.parametrize("campaign_slug", _lab_cases())
def test_real_stage6_executes_measures_and_deprovisions(
    campaign_slug: str,
    session_factory: sessionmaker[Session],
    real_caldera: CalderaClient,
) -> None:
    """Drive one curated case through Stage 6 against the REAL lab and tear down.

    On the lab machine (hardened compose applied, containment satisfied) this:

    * **provisiona** o environment curated de verdade — deriva os
      ``EnvironmentParameters`` reais (Estágio 4, sem rede/IA), carrega as
      Abilities ordenadas no **Caldera real** e **verifica o isolation** read-only
      contra o **Docker real** (``inspector=None`` → :class:`DockerSdkInspector`);
    * **executa** o Estágio 6 reusando o **``OperationRunner`` real** com os
      quatro gates satisfeitos (contenção + isolation reais, Req. 10.3) —
      execução ÚNICA, persistindo ``AbilityResult``/``AuditLogEntry`` por command;
    * **mede** a suficiência procedural dos results por Ability + o DAG curated
      e consolida o :class:`ExecutionReport`;
    * **desprovisiona** o environment ao final, em success ou failure (Req. 10.8,
      12.1), e marca o relatório como ``environment_deprovisioned``.

    O corpo usa o **``OperationRunner`` real** (Caldera real) e o verificador de
    isolation **real** (``inspect_isolation`` sem inspector injetado). Nada aqui
    é mockado; é inerte fora da máquina de laboratório porque os guardas de módulo
    o mantêm *skipped*.
    """
    case_service = CaseService()
    case = case_service.load_case(campaign_slug)
    assert case.abilities, f"campaign {campaign_slug} deve ter Abilities curadas"
    assert case.adversary is not None, f"campaign {campaign_slug} deve ter Adversary"

    # --- Estágio 4 → parâmetros reais do environment curated (sem rede/IA) -------
    operationalizer = CurationOperationalizer(case_service=case_service)
    dag_view = DagView(
        campaign_id=campaign_slug,
        name=case_service.list_cases().get(campaign_slug, campaign_slug),
        curated=True,
        can_advance=True,
    )
    params = operationalizer.operationalize(campaign_slug, dag_view).parameters
    assert params.campaign_id == campaign_slug
    assert params.platform is EnvironmentPlatform.CONTAINER

    # --- Estágio 5 → provisiona de verdade (Abilities no Caldera + isolation)
    provisioner = CurationEnvironmentProvisioner(
        # inspector=None -> inspect_isolation builds the REAL read-only
        # DockerSdkInspector from the ambient Docker environment.
        inspector=None,
        case_service=case_service,
        caldera=real_caldera,  # the REAL Caldera (no mock transport)
    )
    environment = provisioner.provision(params)
    assert environment.origin is TranslationSource.HUMAN_CURATION
    # Na máquina de laboratório endurecida a contenção está satisfeita: o
    # isolation aprova e as Abilities curadas foram carregadas no Caldera real.
    assert environment.isolation.approved is True, (
        "na máquina de laboratório endurecida o isolation deve aprovar; "
        f"não isolados: {[v.container for v in environment.isolation.non_isolated]}"
    )
    assert environment.loaded_abilities is True

    session = session_factory()
    deprovisioned = False
    try:
        # --- Estágio 6 → executa de verdade (OperationRunner real, 4 gates) --
        service = ExecutionStageService(
            session,
            runner_factory=_runner_factory(real_caldera),
            case_service=case_service,
        )

        # Execução ÚNICA, confirmada, contra o Caldera real com contenção e
        # isolation reais satisfeitos (Req. 10.3).
        result = service.execute(campaign_slug, confirmed=True)

        # A Operação de fato iniciou e concluiu (COMPLETED + FINISHED): o Estágio
        # 6 só conclui por esse path_. Qualquer gate que recusasse (contenção/
        # isolation) deixaria ``started=False``, o que na lab endurecida é um
        # erro de configuração do environment, não um result válido do teste.
        assert result.started is True, (
            "a execução deveria iniciar na lab endurecida; result="
            f"{result.result} mensagem={result.message!r}"
        )
        assert result.result is RunOutcome.COMPLETED
        assert result.operation_id is not None
        assert result.total_success >= 0 and result.total_failure >= 0
        # Um result por Ability executada foi projetado/persistido (Req. 10.2).
        assert result.results, (
            "a execução real deve projetar results por Ability"
        )
        for ar in result.results:
            assert ar.target_container, "cada result deve ter container-target"

        # --- medição da suficiência procedural (Req. 15 reusa o Estágio 6) ----
        measurement = MeasurementService()
        ability_results = [
            AbilityMeasurementResult.from_execution(ar) for ar in result.results
        ]
        metrics = measurement.measure_from_campaign(campaign=case, results=ability_results)
        # Métricas consistentes com os results: taxa/cobertura ∈ [0,1] e os
        # sucessos nunca excedem o universo de técnicas do DAG.
        assert 0.0 <= metrics.success_rate <= 1.0
        assert 0.0 <= metrics.procedural_coverage <= 1.0
        assert metrics.successful_techniques <= metrics.total_dag_techniques

        # --- deprovisioning ALWAYS at the end (success OR failure) — Req. 12.1 -
        provisioner.deprovision(environment)
        deprovisioned = True

        # Consolida o relatório carimbando o environment como deprovisioned (Req.
        # 10.8, 12), reusando os results por Ability.
        report = measurement.consolidate(
            campaign_id=campaign_slug,
            metrics=metrics,
            environment_deprovisioned=deprovisioned,
        )
        assert report.campaign_id == campaign_slug
        assert report.environment_deprovisioned is True
        assert report.metrics is metrics
    finally:
        # Teardown é incondicional (Req. 10.8, 12.1): mesmo se uma asserção acima
        # falhar, o environment é deprovisioned antes de propagar o erro.
        if not deprovisioned:
            provisioner.deprovision(environment)
        session.close()
