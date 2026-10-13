"""Property test da conclusão automática do Estágio 4 — parte ambiente (task 4.3).

# Feature: pipeline-cinco-estagios, Property 9: Conclusão automática dos
# Estágios 2 e 4. Para um Caso curado, assim que o Estágio 3 conclui e o
# ambiente é verificado isolado, o Estágio 4 (Provisionamento do Ambiente)
# auto-conclui. Respeita o gate sequencial e não ocorre sem o material curado.

Validates: Requirements 9.2, 9.3, 9.4

Esta é uma property test de *feature* (a spec ``pipeline-cinco-estagios`` é uma
feature, não um bugfix): a propriedade é esperada **valer** sobre a
implementação atual de
:meth:`~app.services.environment.stage_service.EnvironmentStageService.auto_concluir_estagio4`.

Propriedade (design.md — Property 9, parte ambiente)
----------------------------------------------------
Dirige o gate, a auto-conclusão idempotente e os caminhos de não-conclusão do
Estágio 4 sobre uma distribuição **arbitrária** de estados dos ``StageRun`` de
um Caso, injetando um :class:`ProvisionadorDeAmbiente` **falso e controlável**
(cujo ``preparar`` devolve um ``ResultadoProvisionamento`` com ``pronto``/
``isolado`` escolhidos, ou levanta ``RuntimeError`` para simular o inspector/
daemon Docker indisponível) e um ``compose_disponivel`` injetável. Nenhum
caminho toca o Docker ou a rede — tudo é lógica pura de BD sobre SQLite efêmero,
seguro no ambiente de dev.

1. **Gate (Req. 9.3).** Para qualquer distribuição de estados em que o Estágio 3
   **não** está ``concluido``, ``auto_concluir_estagio4`` devolve
   ``concluido=False`` / ``bloqueado_pelo_gate=True`` e **não** persiste nenhuma
   linha ``estagio=4`` concluída — independentemente de material/isolamento.
2. **Auto-conclusão (Req. 9.2).** Quando o Estágio 3 está ``concluido``, o
   material de ambiente está disponível (``compose_disponivel() is True``) e o
   provisionador reporta pronto/isolado, o Estágio 4 auto-conclui: o ``StageRun``
   de ``(caso, estagio=4)`` fica ``concluido``/100; e é **idempotente** —
   chamar ``N`` vezes (``N`` dirigido por Hypothesis) produz exatamente a mesma
   linha única (mesmo ``id``, sem acúmulo).
3. **Não isolado / sem material / indisponível (Req. 9.4, 5.6).** Quando o
   provisionador reporta **não** isolado ⇒ não conclui (nenhuma linha
   ``estagio=4`` concluída); quando ``compose_disponivel() is False`` ⇒
   ``material_ausente=True``, não conclui; quando ``preparar`` levanta
   ``RuntimeError`` (inspector indisponível, fail-closed) ⇒
   ``isolamento_indisponivel=True``, não conclui.

Isolamento
----------
Cada exemplo constrói e descarta um engine SQLite **efêmero** (arquivo
temporário com o esquema completo via ``Base.metadata.create_all``, igual às
fixtures de ``test_backfill_idempotente_property.py`` / ``test_stage_blocking_property.py``).
O estado dos Estágios é semeado diretamente como linhas ``StageRun``.
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
from app.services.environment.provisionador import ResultadoProvisionamento
from app.services.environment.stage_service import EnvironmentStageService
from app.services.environment.topologia_builder import TopologyResponse
from app.services.stage.stage_service import (
    ESTAGIO_OPERACIONALIZACAO,
    ESTAGIO_PROVISIONAMENTO,
    VALID_STAGES,
)

_CASO = "caso-prop9-ambiente"


# ---------------------------------------------------------------------------
# Provisionador falso e controlável — satisfaz ``ProvisionadorDeAmbiente``
# estruturalmente (expõe ``preparar``), sem tocar no Docker/rede.
# ---------------------------------------------------------------------------


class _ProvisionadorFalso:
    """Test double de :class:`ProvisionadorDeAmbiente` com veredicto escolhido.

    ``preparar`` devolve um :class:`ResultadoProvisionamento` com ``pronto``/
    ``isolado`` iguais a ``isolado`` (a curadoria define ``pronto == isolado``);
    quando ``indisponivel`` é ``True`` levanta ``RuntimeError`` para simular o
    inspector/daemon Docker ausente (caminho fail-closed do Req. 5.6). Conta
    quantas vezes foi chamado, para asserir que a idempotência não repete a
    verificação após concluir.
    """

    def __init__(self, *, isolado: bool, indisponivel: bool = False) -> None:
        self._isolado = isolado
        self._indisponivel = indisponivel
        self.chamadas = 0

    def preparar(self, caso: str) -> ResultadoProvisionamento:
        self.chamadas += 1
        if self._indisponivel:
            raise RuntimeError(
                "Docker indisponível (fake); não é possível inspecionar isolamento"
            )
        isolamento = IsolationResult(passed=self._isolado, verdicts=())
        topologia = TopologyResponse(
            caso_id=caso,
            nome=caso,
            isolado=self._isolado,
            hosts=(),
            redes=(),
            mermaid="",
            compose_file=None,
        )
        return ResultadoProvisionamento(
            caso_id=caso,
            pronto=self._isolado,
            isolado=self._isolado,
            isolamento=isolamento,
            topologia=topologia,
        )


# ---------------------------------------------------------------------------
# Sessão efêmera (engine SQLite em arquivo temporário + esquema por exemplo).
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
# Geradores de estado dos Estágios.
# ---------------------------------------------------------------------------

# Estados possíveis de um Estágio, mais ``None`` para modelar "sem linha"
# (que o serviço trata como ``nao_iniciado``).
_STATE_CHOICES: tuple[Optional[StageState], ...] = (
    None,
    StageState.NOT_STARTED,
    StageState.IN_PROGRESS,
    StageState.COMPLETED,
    StageState.ERROR,
)

# Estados do Estágio 3 que NÃO são ``concluido`` (para dirigir o gate fechado).
_NON_COMPLETED_CHOICES: tuple[Optional[StageState], ...] = tuple(
    s for s in _STATE_CHOICES if s is not StageState.COMPLETED
)


def _seed_stage_runs(
    session: Session,
    caso_id: str,
    states: dict[int, Optional[StageState]],
) -> None:
    """Persiste uma linha ``StageRun`` por Estágio cujo estado não é ``None``."""
    for estagio, state in states.items():
        if state is None:
            continue
        session.add(
            StageRun(
                caso_id=caso_id,
                estagio=estagio,
                estado=state,
                progresso=100 if state is StageState.COMPLETED else 0,
            )
        )
    session.commit()


def _estagio4_rows(session: Session, caso_id: str) -> list[StageRun]:
    """Todas as linhas ``StageRun`` de ``(caso_id, estagio=4)``."""
    return list(
        session.execute(
            select(StageRun)
            .where(StageRun.caso_id == caso_id)
            .where(StageRun.estagio == ESTAGIO_PROVISIONAMENTO)
            .order_by(StageRun.id.asc())
        )
        .scalars()
        .all()
    )


def _count_estagio4_concluido(session: Session, caso_id: str) -> int:
    """Quantas linhas ``estagio=4`` concluídas existem para ``caso_id``."""
    return (
        session.execute(
            select(func.count())
            .select_from(StageRun)
            .where(StageRun.caso_id == caso_id)
            .where(StageRun.estagio == ESTAGIO_PROVISIONAMENTO)
            .where(StageRun.estado == StageState.COMPLETED)
        ).scalar_one()
    )


# A estratégia sorteia um estado por Estágio em VALID_STAGES, exceto o Estágio 3
# e o Estágio 4, que são controlados por cada teste.
_OTHER_STAGES: tuple[int, ...] = tuple(
    e
    for e in VALID_STAGES
    if e not in (ESTAGIO_OPERACIONALIZACAO, ESTAGIO_PROVISIONAMENTO)
)


@st.composite
def _other_stage_states(draw) -> dict[int, Optional[StageState]]:
    """Distribuição arbitrária de estados para os Estágios que não o 3 nem o 4."""
    return {
        estagio: draw(st.sampled_from(_STATE_CHOICES)) for estagio in _OTHER_STAGES
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
    isolado=st.booleans(),
    material=st.booleans(),
)
def test_property9_gate_bloqueia_sem_estagio3_concluido(
    estagio3: Optional[StageState],
    estagio4: Optional[StageState],
    outros: dict[int, Optional[StageState]],
    isolado: bool,
    material: bool,
) -> None:
    """Estágio 3 não concluído ⇒ gate fechado, não conclui nesta chamada.

    Para qualquer distribuição de estados onde o Estágio 3 não é ``concluido``, o
    gate sequencial tem precedência: a auto-conclusão devolve ``concluido=False``
    / ``bloqueado_pelo_gate=True`` e não persiste nenhuma linha ``estagio=4``
    *nova* — independentemente de material/isolamento, e sem chamar o
    provisionador. Nota: o gate é avaliado antes da idempotência, então o
    resultado é "bloqueado" mesmo que já exista uma linha ``estagio=4``
    concluída de antes (sem gate liberado o Backend não reafirma a conclusão).

    # Feature: pipeline-cinco-estagios, Property 9 (parte ambiente)
    Validates: Requirements 9.2, 9.3, 9.4
    """
    assert estagio3 is not StageState.COMPLETED

    session, db_path, engine = _build_ephemeral_session()
    try:
        states = dict(outros)
        states[ESTAGIO_OPERACIONALIZACAO] = estagio3
        states[ESTAGIO_PROVISIONAMENTO] = estagio4
        _seed_stage_runs(session, _CASO, states)

        # Linhas estagio=4 concluídas presentes ANTES da chamada (semeadas), para
        # distingui-las de qualquer conclusão feita por esta chamada.
        concluidas_antes = _count_estagio4_concluido(session, _CASO)

        provisionador = _ProvisionadorFalso(isolado=isolado)
        service = EnvironmentStageService(
            session,
            provisionador=provisionador,
            compose_disponivel=lambda: material,
        )

        resultado = service.auto_concluir_estagio4(_CASO)

        # Gate fechado tem precedência: não conclui, sinaliza o bloqueio, não
        # chama o provisionador e não cria nenhuma linha estagio=4 nova.
        assert resultado.concluido is False
        assert resultado.bloqueado_pelo_gate is True
        assert provisionador.chamadas == 0
        # Nenhuma conclusão foi feita por esta chamada: a contagem permanece a
        # mesma de antes (0 quando estagio4 não era concluido; 1 quando já era).
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
    """Estágio 3 concluído + material + isolado ⇒ auto-conclui; N chamadas == 1.

    Com o gate liberado (Estágio 3 ``concluido``), material disponível e o
    provisionador reportando pronto/isolado, o Estágio 4 auto-conclui: a linha
    ``(caso, estagio=4)`` fica ``concluido``/100. Chamar ``N`` vezes é
    idempotente — exatamente uma linha ``estagio=4``, com ``id`` estável.

    # Feature: pipeline-cinco-estagios, Property 9 (parte ambiente)
    Validates: Requirements 9.2, 9.3
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        states = dict(outros)
        states[ESTAGIO_OPERACIONALIZACAO] = StageState.COMPLETED
        states[ESTAGIO_PROVISIONAMENTO] = estagio4_inicial
        _seed_stage_runs(session, _CASO, states)

        provisionador = _ProvisionadorFalso(isolado=True)
        service = EnvironmentStageService(
            session,
            provisionador=provisionador,
            compose_disponivel=lambda: True,
        )

        # 1ª chamada: auto-conclui o Estágio 4.
        primeiro = service.auto_concluir_estagio4(_CASO)
        assert primeiro.concluido is True
        assert primeiro.isolado is True
        assert primeiro.bloqueado_pelo_gate is False
        assert primeiro.material_ausente is False
        assert primeiro.stage_run_id is not None

        rows_1 = _estagio4_rows(session, _CASO)
        assert len(rows_1) == 1
        linha = rows_1[0]
        assert linha.estado is StageState.COMPLETED
        assert linha.progresso == 100
        id_estavel = linha.id

        # Chamadas 2..N são no-ops idempotentes: mesma linha única, mesmo id.
        for _ in range(n - 1):
            repetido = service.auto_concluir_estagio4(_CASO)
            assert repetido.concluido is True

        rows_n = _estagio4_rows(session, _CASO)
        assert len(rows_n) == 1
        assert rows_n[0].id == id_estavel
        assert rows_n[0].estado is StageState.COMPLETED
        assert rows_n[0].progresso == 100
        assert _count_estagio4_concluido(session, _CASO) == 1

        # O provisionador não é reinvocado depois de concluído (idempotência não
        # repete a verificação): chamado só na 1ª vez.
        assert provisionador.chamadas == 1
    finally:
        _teardown(session, db_path, engine)


# ===========================================================================
# Propriedade 9.3 — Não isolado / sem material / indisponível (Req. 9.4, 5.6).
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
    """Mesmo com o gate liberado, não conclui sem isolamento verificado nem material.

    Com o Estágio 3 ``concluido``, exercita os três caminhos de não-conclusão:

    * ``nao_isolado``: provisionador reporta não isolado ⇒ ``isolado=False``,
      não conclui, nenhuma linha ``estagio=4`` concluída (Req. 5.6).
    * ``sem_material``: ``compose_disponivel() is False`` ⇒ ``material_ausente``,
      não conclui, e o provisionador nem é chamado (Req. 9.4).
    * ``indisponivel``: ``preparar`` levanta ``RuntimeError`` (inspector/daemon
      Docker ausente) ⇒ ``isolamento_indisponivel``, não conclui (fail-closed).

    Em todos, o Estágio 5 permanece bloqueável porque o Estágio 4 não concluiu.

    # Feature: pipeline-cinco-estagios, Property 9 (parte ambiente)
    Validates: Requirements 9.4
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        states = dict(outros)
        states[ESTAGIO_OPERACIONALIZACAO] = StageState.COMPLETED
        states[ESTAGIO_PROVISIONAMENTO] = estagio4_inicial
        _seed_stage_runs(session, _CASO, states)

        material = caminho != "sem_material"
        provisionador = _ProvisionadorFalso(
            isolado=(caminho != "nao_isolado"),
            indisponivel=(caminho == "indisponivel"),
        )
        service = EnvironmentStageService(
            session,
            provisionador=provisionador,
            compose_disponivel=lambda: material,
        )

        resultado = service.auto_concluir_estagio4(_CASO)

        # Nenhum caminho conclui o Estágio 4.
        assert resultado.concluido is False
        assert resultado.bloqueado_pelo_gate is False
        assert _count_estagio4_concluido(session, _CASO) == 0

        if caminho == "sem_material":
            assert resultado.material_ausente is True
            # Sem material, a verificação (potencialmente custosa) nem roda.
            assert provisionador.chamadas == 0
        elif caminho == "nao_isolado":
            assert resultado.material_ausente is False
            assert resultado.isolado is False
            assert resultado.isolamento_indisponivel is False
            assert provisionador.chamadas == 1
        else:  # indisponivel
            assert resultado.isolamento_indisponivel is True
            assert resultado.isolado is False
            assert provisionador.chamadas == 1
    finally:
        _teardown(session, db_path, engine)
