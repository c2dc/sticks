"""Property test da conclusão automática do Estágio 4 — parte environment (task 4.3).

# Feature: pipeline-cinco-stages, Property 9: Conclusão automática dos
# Estágios 2 e 4. Para um campaign curated, assim que o Estágio 3 conclui e o
# environment é verificado isolated, o Estágio 4 (Provisionamento do Ambiente)
# auto-conclui. Respeita o gate sequencial e não ocorre sem o material curated.

Validates: Requirements 9.2, 9.3, 9.4

Esta é uma property test de *feature* (a spec ``pipeline-cinco-stages`` é uma
feature, não um bugfix): a propriedade é esperada **valer** sobre a
implementação atual de
:meth:`~app.services.environment.stage_service.EnvironmentStageService.auto_complete_stage4`.

Propriedade (design.md — Property 9, parte environment)
----------------------------------------------------
Dirige o gate, a auto-conclusão idempotente e os caminhos de não-conclusão do
Estágio 4 sobre uma distribuição **arbitrária** de states dos ``StageRun`` de
um campaign, injetando um :class:`EnvironmentProvisioner` **falso e controlável**
(cujo ``prepare`` devolve um ``ProvisioningResult`` com ``ready``/
``isolated`` escolhidos, ou levanta ``RuntimeError`` para simular o inspector/
daemon Docker indisponível) e um ``compose_available`` injetável. Nenhum
caminho toca o Docker ou a rede — tudo é lógica pura de BD sobre SQLite efêmero,
seguro no environment de dev.

1. **Gate (Req. 9.3).** Para qualquer distribuição de states em que o Estágio 3
   **não** está ``completed``, ``auto_complete_stage4`` devolve
   ``completed=False`` / ``blocked_by_gate=True`` e **não** persiste nenhuma
   line ``stage=4`` concluída — independentemente de material/isolation.
2. **Auto-conclusão (Req. 9.2).** Quando o Estágio 3 está ``completed``, o
   material de environment está disponível (``compose_available() is True``) e o
   provisioner reporta ready/isolated, o Estágio 4 auto-conclui: o ``StageRun``
   de ``(campaign, stage=4)`` fica ``completed``/100; e é **idempotente** —
   chamar ``N`` vezes (``N`` dirigido por Hypothesis) produz exatamente a mesma
   line única (mesmo ``id``, sem acúmulo).
3. **Não isolated / sem material / indisponível (Req. 9.4, 5.6).** Quando o
   provisioner reporta **não** isolated ⇒ não conclui (nenhuma line
   ``stage=4`` concluída); quando ``compose_available() is False`` ⇒
   ``missing_material=True``, não conclui; quando ``prepare`` levanta
   ``RuntimeError`` (inspector indisponível, fail-closed) ⇒
   ``isolation_unavailable=True``, não conclui.

Isolamento
----------
Cada exemplo constrói e descarta um engine SQLite **efêmero** (file
temporário com o esquema completo via ``Base.metadata.create_all``, igual às
fixtures de ``test_backfill_idempotente_property.py`` / ``test_stage_blocking_property.py``).
O state dos Estágios é semeado diretamente como lines ``StageRun``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import StageRun
from app.models.enums import StageState
from app.services.containment import IsolationResult
from app.services.environment.provisioner import ProvisioningResult
from app.services.environment.stage_service import EnvironmentStageService
from app.services.environment.topology_builder import TopologyResponse
from app.services.stage.stage_service import (
    ESTAGIO_OPERACIONALIZACAO,
    ESTAGIO_PROVISIONAMENTO,
    VALID_STAGES,
)

_CASO = "campaign-prop9-environment"


# ---------------------------------------------------------------------------
# Provisionador falso e controlável — satisfaz ``EnvironmentProvisioner``
# estruturalmente (expõe ``prepare``), sem tocar no Docker/rede.
# ---------------------------------------------------------------------------


class _ProvisionadorFalso:
    """Test double de :class:`EnvironmentProvisioner` com veredicto escolhido.

    ``prepare`` devolve um :class:`ProvisioningResult` com ``ready``/
    ``isolated`` iguais a ``isolated`` (a curation define ``ready == isolated``);
    quando ``indisponivel`` é ``True`` levanta ``RuntimeError`` para simular o
    inspector/daemon Docker ausente (caminho fail-closed do Req. 5.6). Conta
    quantas vezes foi chamado, para asserir que a idempotência não repete a
    verificação após complete.
    """

    def __init__(self, *, isolated: bool, indisponivel: bool = False) -> None:
        self._isolado = isolated
        self._indisponivel = indisponivel
        self.chamadas = 0

    def prepare(self, campaign: str) -> ProvisioningResult:
        self.chamadas += 1
        if self._indisponivel:
            raise RuntimeError(
                "Docker indisponível (fake); não é possível inspecionar isolation"
            )
        isolation = IsolationResult(passed=self._isolado, verdicts=())
        topology = TopologyResponse(
            campaign_id=campaign,
            name=campaign,
            isolated=self._isolado,
            hosts=(),
            networks=(),
            mermaid="",
            compose_file=None,
        )
        return ProvisioningResult(
            campaign_id=campaign,
            ready=self._isolado,
            isolated=self._isolado,
            isolation=isolation,
            topology=topology,
        )


# ---------------------------------------------------------------------------
# Sessão efêmera (engine SQLite em file temporário + esquema por exemplo).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Cria um engine SQLite efêmero + esquema e devolve ``(session, db_path, engine)``."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline5_env_auto_prop_")
    db_path = Path(tmp_dir) / "env_auto.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

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
    return factory(), db_path, engine


def _teardown(session: Session, db_path: Path, engine: object) -> None:
    session.close()
    engine.dispose()  # type: ignore[attr-defined]
    try:
        db_path.unlink()
        db_path.parent.rmdir()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Geradores de state dos Estágios.
# ---------------------------------------------------------------------------

# Estados possíveis de um Estágio, mais ``None`` para modelar "sem line"
# (que o serviço trata como ``nao_iniciado``).
_STATE_CHOICES: tuple[Optional[StageState], ...] = (
    None,
    StageState.NOT_STARTED,
    StageState.IN_PROGRESS,
    StageState.COMPLETED,
    StageState.ERROR,
)

# Estados do Estágio 3 que NÃO são ``completed`` (para dirigir o gate fechado).
_NON_COMPLETED_CHOICES: tuple[Optional[StageState], ...] = tuple(
    s for s in _STATE_CHOICES if s is not StageState.COMPLETED
)


def _seed_stage_runs(
    session: Session,
    campaign_id: str,
    states: dict[int, Optional[StageState]],
) -> None:
    """Persiste uma line ``StageRun`` por Estágio cujo state não é ``None``."""
    for stage, state in states.items():
        if state is None:
            continue
        session.add(
            StageRun(
                campaign_id=campaign_id,
                stage=stage,
                state=state,
                progress=100 if state is StageState.COMPLETED else 0,
            )
        )
    session.commit()


def _estagio4_rows(session: Session, campaign_id: str) -> list[StageRun]:
    """Todas as lines ``StageRun`` de ``(campaign_id, stage=4)``."""
    return list(
        session.execute(
            select(StageRun)
            .where(StageRun.campaign_id == campaign_id)
            .where(StageRun.stage == ESTAGIO_PROVISIONAMENTO)
            .order_by(StageRun.id.asc())
        )
        .scalars()
        .all()
    )


def _count_estagio4_concluido(session: Session, campaign_id: str) -> int:
    """Quantas lines ``stage=4`` concluídas existem para ``campaign_id``."""
    return (
        session.execute(
            select(func.count())
            .select_from(StageRun)
            .where(StageRun.campaign_id == campaign_id)
            .where(StageRun.stage == ESTAGIO_PROVISIONAMENTO)
            .where(StageRun.state == StageState.COMPLETED)
        ).scalar_one()
    )


# A estratégia sorteia um state por Estágio em VALID_STAGES, exceto o Estágio 3
# e o Estágio 4, que são controlados por cada teste.
_OTHER_STAGES: tuple[int, ...] = tuple(
    e
    for e in VALID_STAGES
    if e not in (ESTAGIO_OPERACIONALIZACAO, ESTAGIO_PROVISIONAMENTO)
)


@st.composite
def _other_stage_states(draw) -> dict[int, Optional[StageState]]:
    """Distribuição arbitrária de states para os Estágios que não o 3 nem o 4."""
    return {
        stage: draw(st.sampled_from(_STATE_CHOICES)) for stage in _OTHER_STAGES
    }


# ===========================================================================
# Propriedade 9.1 — Gate (Req. 9.3): Estágio 3 não concluído ⇒ não conclui.
# ===========================================================================


@settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    estagio3=st.sampled_from(_NON_COMPLETED_CHOICES),
    estagio4=st.sampled_from(_STATE_CHOICES),
    outros=_other_stage_states(),
    isolated=st.booleans(),
    material=st.booleans(),
)
def test_property9_gate_bloqueia_sem_estagio3_concluido(
    estagio3: Optional[StageState],
    estagio4: Optional[StageState],
    outros: dict[int, Optional[StageState]],
    isolated: bool,
    material: bool,
) -> None:
    """Estágio 3 não concluído ⇒ gate fechado, não conclui nesta chamada.

    Para qualquer distribuição de states onde o Estágio 3 não é ``completed``, o
    gate sequencial tem precedência: a auto-conclusão devolve ``completed=False``
    / ``blocked_by_gate=True`` e não persiste nenhuma line ``stage=4``
    *nova* — independentemente de material/isolation, e sem chamar o
    provisioner. Nota: o gate é avaliado antes da idempotência, então o
    result é "blocked" mesmo que já exista uma line ``stage=4``
    concluída de antes (sem gate liberado o Backend não reafirma a conclusão).

    # Feature: pipeline-cinco-stages, Property 9 (parte environment)
    Validates: Requirements 9.2, 9.3, 9.4
    """
    assert estagio3 is not StageState.COMPLETED

    session, db_path, engine = _build_ephemeral_session()
    try:
        states = dict(outros)
        states[ESTAGIO_OPERACIONALIZACAO] = estagio3
        states[ESTAGIO_PROVISIONAMENTO] = estagio4
        _seed_stage_runs(session, _CASO, states)

        # Linhas stage=4 concluídas presentes ANTES da chamada (semeadas), para
        # distingui-las de qualquer conclusão feita por esta chamada.
        concluidas_antes = _count_estagio4_concluido(session, _CASO)

        provisioner = _ProvisionadorFalso(isolated=isolated)
        service = EnvironmentStageService(
            session,
            provisioner=provisioner,
            compose_available=lambda: material,
        )

        result = service.auto_complete_stage4(_CASO)

        # Gate fechado tem precedência: não conclui, sinaliza o bloqueio, não
        # chama o provisioner e não cria nenhuma line stage=4 nova.
        assert result.completed is False
        assert result.blocked_by_gate is True
        assert provisioner.chamadas == 0
        # Nenhuma conclusão foi feita por esta chamada: a contagem permanece a
        # mesma de antes (0 quando estagio4 não era completed; 1 quando já era).
        assert _count_estagio4_concluido(session, _CASO) == concluidas_antes
        assert concluidas_antes == (
            1 if estagio4 is StageState.COMPLETED else 0
        )
    finally:
        _teardown(session, db_path, engine)


# ===========================================================================
# Propriedade 9.2 — Auto-conclusão idempotente (Req. 9.2).
# ===========================================================================


@settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    estagio4_inicial=st.sampled_from(_NON_COMPLETED_CHOICES),
    outros=_other_stage_states(),
    n=st.integers(min_value=1, max_value=6),
)
def test_property9_auto_conclui_e_idempotente(
    estagio4_inicial: Optional[StageState],
    outros: dict[int, Optional[StageState]],
    n: int,
) -> None:
    """Estágio 3 concluído + material + isolated ⇒ auto-conclui; N chamadas == 1.

    Com o gate liberado (Estágio 3 ``completed``), material disponível e o
    provisioner reportando ready/isolated, o Estágio 4 auto-conclui: a line
    ``(campaign, stage=4)`` fica ``completed``/100. Chamar ``N`` vezes é
    idempotente — exatamente uma line ``stage=4``, com ``id`` estável.

    # Feature: pipeline-cinco-stages, Property 9 (parte environment)
    Validates: Requirements 9.2, 9.3
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        states = dict(outros)
        states[ESTAGIO_OPERACIONALIZACAO] = StageState.COMPLETED
        states[ESTAGIO_PROVISIONAMENTO] = estagio4_inicial
        _seed_stage_runs(session, _CASO, states)

        provisioner = _ProvisionadorFalso(isolated=True)
        service = EnvironmentStageService(
            session,
            provisioner=provisioner,
            compose_available=lambda: True,
        )

        # 1ª chamada: auto-conclui o Estágio 4.
        primeiro = service.auto_complete_stage4(_CASO)
        assert primeiro.completed is True
        assert primeiro.isolated is True
        assert primeiro.blocked_by_gate is False
        assert primeiro.missing_material is False
        assert primeiro.stage_run_id is not None

        rows_1 = _estagio4_rows(session, _CASO)
        assert len(rows_1) == 1
        line = rows_1[0]
        assert line.state is StageState.COMPLETED
        assert line.progress == 100
        id_estavel = line.id

        # Chamadas 2..N são no-ops idempotentes: mesma line única, mesmo id.
        for _ in range(n - 1):
            repetido = service.auto_complete_stage4(_CASO)
            assert repetido.completed is True

        rows_n = _estagio4_rows(session, _CASO)
        assert len(rows_n) == 1
        assert rows_n[0].id == id_estavel
        assert rows_n[0].state is StageState.COMPLETED
        assert rows_n[0].progress == 100
        assert _count_estagio4_concluido(session, _CASO) == 1

        # O provisioner não é reinvocado depois de concluído (idempotência não
        # repete a verificação): chamado só na 1ª vez.
        assert provisioner.chamadas == 1
    finally:
        _teardown(session, db_path, engine)


# ===========================================================================
# Propriedade 9.3 — Não isolated / sem material / indisponível (Req. 9.4, 5.6).
# ===========================================================================


@settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    estagio4_inicial=st.sampled_from(_NON_COMPLETED_CHOICES),
    outros=_other_stage_states(),
    caminho=st.sampled_from(("nao_isolado", "sem_material", "indisponivel")),
)
def test_property9_nao_conclui_sem_isolamento_ou_material(
    estagio4_inicial: Optional[StageState],
    outros: dict[int, Optional[StageState]],
    caminho: str,
) -> None:
    """Mesmo com o gate liberado, não conclui sem isolation verificado nem material.

    Com o Estágio 3 ``completed``, exercita os três caminhos de não-conclusão:

    * ``nao_isolado``: provisioner reporta não isolated ⇒ ``isolated=False``,
      não conclui, nenhuma line ``stage=4`` concluída (Req. 5.6).
    * ``sem_material``: ``compose_available() is False`` ⇒ ``missing_material``,
      não conclui, e o provisioner nem é chamado (Req. 9.4).
    * ``indisponivel``: ``prepare`` levanta ``RuntimeError`` (inspector/daemon
      Docker ausente) ⇒ ``isolation_unavailable``, não conclui (fail-closed).

    Em todos, o Estágio 5 permanece bloqueável porque o Estágio 4 não concluiu.

    # Feature: pipeline-cinco-stages, Property 9 (parte environment)
    Validates: Requirements 9.4
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        states = dict(outros)
        states[ESTAGIO_OPERACIONALIZACAO] = StageState.COMPLETED
        states[ESTAGIO_PROVISIONAMENTO] = estagio4_inicial
        _seed_stage_runs(session, _CASO, states)

        material = caminho != "sem_material"
        provisioner = _ProvisionadorFalso(
            isolated=(caminho != "nao_isolado"),
            indisponivel=(caminho == "indisponivel"),
        )
        service = EnvironmentStageService(
            session,
            provisioner=provisioner,
            compose_available=lambda: material,
        )

        result = service.auto_complete_stage4(_CASO)

        # Nenhum caminho conclui o Estágio 4.
        assert result.completed is False
        assert result.blocked_by_gate is False
        assert _count_estagio4_concluido(session, _CASO) == 0

        if caminho == "sem_material":
            assert result.missing_material is True
            # Sem material, a verificação (potencialmente custosa) nem roda.
            assert provisioner.chamadas == 0
        elif caminho == "nao_isolado":
            assert result.missing_material is False
            assert result.isolated is False
            assert result.isolation_unavailable is False
            assert provisioner.chamadas == 1
        else:  # indisponivel
            assert result.isolation_unavailable is True
            assert result.isolated is False
            assert provisioner.chamadas == 1
    finally:
        _teardown(session, db_path, engine)
