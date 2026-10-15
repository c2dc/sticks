"""Stage-6 integration against the REAL lab Caldera + Docker (task 8.9).

⚠️  LAB-ONLY — **A executar exclusivamente na máquina de laboratório do
usuário. NÃO rodar no ambiente de desenvolvimento nem em CI.**

While ``test_execucao_estagio6.py`` (task 8.2) drives
:class:`~app.services.environment.execucao.ExecucaoStageService` with a **real**
``OperationRunner`` but **fake** externals (Caldera via ``httpx.MockTransport``
and a fake isolation verifier — no network, no daemon), this suite drives the
*real* Stage-6 flow end to end against the lab:

1. **Provisiona o ambiente curado de verdade** — carrega as Abilities ordenadas
   no **Caldera real** em ``http://localhost:8888`` e **verifica o isolamento
   read-only contra o Docker real** (os contêineres do laboratório nas redes
   ``internal: true``) via :class:`CuradoriaProvisionadorDeAmbiente`.
2. **Executa o Estágio 6 de verdade** — ``ExecucaoStageService.executar`` reusa o
   **``OperationRunner`` real** (Caldera real + verificador de isolamento real
   :func:`inspect_isolation`, sem mocks) com os quatro gates satisfeitos
   (confirmação → contenção → isolamento → disponibilidade), execução ÚNICA, e
   persiste ``AbilityResult`` + ``AuditLogEntry`` por comando (Req. 10.3).
3. **Mede a suficiência procedural** — :class:`MedicaoService` deriva as
   :class:`MetricasSuficiencia` dos resultados por Ability + o DAG curado e
   consolida o :class:`RelatorioExecucao`.
4. **Desprovisiona o ambiente** — ``ProvisionadorDeAmbiente.desprovisionar`` é
   invocado ao final, em sucesso **ou** falha das técnicas (Req. 10.8, 12.1).

É a contraparte, para o Estágio 6, do teste de laboratório do Estágio 5
(``test_estagio5_real_lab.py``) e do fim-a-fim (``test_e2e_real_lab.py``).

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

_Requisitos: 10.3, 10.8, 12.1_
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
from app.services.case_service import CaseService
from app.services.containment.isolation import inspect_isolation
from app.services.environment.execucao import ExecucaoStageService
from app.services.environment.medicao import MedicaoService, ResultadoAbility
from app.services.environment.operacionalizacao import PlataformaAmbiente
from app.services.environment.operacionalizador import CuradoriaOperacionalizador
from app.services.environment.provisionador import CuradoriaProvisionadorDeAmbiente
from app.services.dag.dag_service import DagView
from app.services.operation.runner import OperationRunner, RunOutcome

# ---------------------------------------------------------------------------
# The curated case(s) to drive through Stage 6 on the lab machine. Kept to 1–2
# cases per the task ("executar 1–2 casos curados ... reais"). Slugs match the
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
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_estagio6_real_lab_")
    db_path = Path(tmp_dir) / "estagio6_real_lab.db"
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


@pytest.mark.parametrize("caso_slug", _lab_cases())
def test_real_stage6_executes_measures_and_deprovisions(
    caso_slug: str,
    session_factory: sessionmaker[Session],
    real_caldera: CalderaClient,
) -> None:
    """Drive one curated case through Stage 6 against the REAL lab and tear down.

    On the lab machine (hardened compose applied, containment satisfied) this:

    * **provisiona** o ambiente curado de verdade — deriva os
      ``ParametrosDeAmbiente`` reais (Estágio 4, sem rede/IA), carrega as
      Abilities ordenadas no **Caldera real** e **verifica o isolamento** read-only
      contra o **Docker real** (``inspector=None`` → :class:`DockerSdkInspector`);
    * **executa** o Estágio 6 reusando o **``OperationRunner`` real** com os
      quatro gates satisfeitos (contenção + isolamento reais, Req. 10.3) —
      execução ÚNICA, persistindo ``AbilityResult``/``AuditLogEntry`` por comando;
    * **mede** a suficiência procedural dos resultados por Ability + o DAG curado
      e consolida o :class:`RelatorioExecucao`;
    * **desprovisiona** o ambiente ao final, em sucesso ou falha (Req. 10.8,
      12.1), e marca o relatório como ``ambiente_desprovisionado``.

    O corpo usa o **``OperationRunner`` real** (Caldera real) e o verificador de
    isolamento **real** (``inspect_isolation`` sem inspector injetado). Nada aqui
    é mockado; é inerte fora da máquina de laboratório porque os guardas de módulo
    o mantêm *skipped*.
    """
    case_service = CaseService()
    case = case_service.load_case(caso_slug)
    assert case.abilities, f"caso {caso_slug} deve ter Abilities curadas"
    assert case.adversary is not None, f"caso {caso_slug} deve ter Adversary"

    # --- Estágio 4 → parâmetros reais do ambiente curado (sem rede/IA) -------
    operacionalizador = CuradoriaOperacionalizador(case_service=case_service)
    dag_view = DagView(
        caso_id=caso_slug,
        nome=case_service.list_cases().get(caso_slug, caso_slug),
        curado=True,
        pode_avancar=True,
    )
    params = operacionalizador.operacionalizar(caso_slug, dag_view).parametros
    assert params.caso_id == caso_slug
    assert params.plataforma is PlataformaAmbiente.CONTAINER

    # --- Estágio 5 → provisiona de verdade (Abilities no Caldera + isolamento)
    provisionador = CuradoriaProvisionadorDeAmbiente(
        # inspector=None -> inspect_isolation builds the REAL read-only
        # DockerSdkInspector from the ambient Docker environment.
        inspector=None,
        case_service=case_service,
        caldera=real_caldera,  # the REAL Caldera (no mock transport)
    )
    ambiente = provisionador.provisionar(params)
    assert ambiente.origem is TranslationSource.HUMAN_CURATION
    # Na máquina de laboratório endurecida a contenção está satisfeita: o
    # isolamento aprova e as Abilities curadas foram carregadas no Caldera real.
    assert ambiente.isolamento.aprovado is True, (
        "na máquina de laboratório endurecida o isolamento deve aprovar; "
        f"não isolados: {[v.container for v in ambiente.isolamento.nao_isolados]}"
    )
    assert ambiente.abilities_carregadas is True

    session = session_factory()
    desprovisionado = False
    try:
        # --- Estágio 6 → executa de verdade (OperationRunner real, 4 gates) --
        service = ExecucaoStageService(
            session,
            runner_factory=_runner_factory(real_caldera),
            case_service=case_service,
        )

        # Execução ÚNICA, confirmada, contra o Caldera real com contenção e
        # isolamento reais satisfeitos (Req. 10.3).
        resultado = service.executar(caso_slug, confirmado=True)

        # A Operação de fato iniciou e concluiu (COMPLETED + FINISHED): o Estágio
        # 6 só conclui por esse caminho. Qualquer gate que recusasse (contenção/
        # isolamento) deixaria ``iniciada=False``, o que na lab endurecida é um
        # erro de configuração do ambiente, não um resultado válido do teste.
        assert resultado.iniciada is True, (
            "a execução deveria iniciar na lab endurecida; resultado="
            f"{resultado.resultado} mensagem={resultado.mensagem!r}"
        )
        assert resultado.resultado is RunOutcome.COMPLETED
        assert resultado.operacao_id is not None
        assert resultado.total_sucesso >= 0 and resultado.total_falha >= 0
        # Um resultado por Ability executada foi projetado/persistido (Req. 10.2).
        assert resultado.resultados, (
            "a execução real deve projetar resultados por Ability"
        )
        for ar in resultado.resultados:
            assert ar.container_destino, "cada resultado deve ter container-alvo"

        # --- medição da suficiência procedural (Req. 15 reusa o Estágio 6) ----
        medicao = MedicaoService()
        resultados_ability = [
            ResultadoAbility.de_execucao(ar) for ar in resultado.resultados
        ]
        metricas = medicao.medir_do_caso(caso=case, resultados=resultados_ability)
        # Métricas consistentes com os resultados: taxa/cobertura ∈ [0,1] e os
        # sucessos nunca excedem o universo de técnicas do DAG.
        assert 0.0 <= metricas.taxa_sucesso <= 1.0
        assert 0.0 <= metricas.cobertura_procedural <= 1.0
        assert metricas.tecnicas_sucesso <= metricas.total_tecnicas_dag

        # --- desprovisionamento SEMPRE ao final (sucesso OU falha) — Req. 12.1 -
        provisionador.desprovisionar(ambiente)
        desprovisionado = True

        # Consolida o relatório carimbando o ambiente como desprovisionado (Req.
        # 10.8, 12), reusando os resultados por Ability.
        relatorio = medicao.consolidar(
            caso_id=caso_slug,
            metricas=metricas,
            ambiente_desprovisionado=desprovisionado,
        )
        assert relatorio.caso_id == caso_slug
        assert relatorio.ambiente_desprovisionado is True
        assert relatorio.metricas is metricas
    finally:
        # Teardown é incondicional (Req. 10.8, 12.1): mesmo se uma asserção acima
        # falhar, o ambiente é desprovisionado antes de propagar o erro.
        if not desprovisionado:
            provisionador.desprovisionar(ambiente)
        session.close()
