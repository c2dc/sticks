"""Property-based test for Property 3 of the pipeline-seis-estagios design (task 1.4).

# Feature: pipeline-seis-estagios, Property 3: Derivação de estado por StageRun
# em 1..6. Para todo conjunto de linhas ``StageRun`` (``estagio`` em 1..6,
# estados arbitrários), a derivação de estado por Estágio reflete exatamente o
# ``StageRun`` correspondente (e ``nao_iniciado``/0 quando não há linha),
# produzindo exatamente um StageStateView por Estágio em 1..6 — um por número de
# Estágio, na ordem de ``VALID_STAGES`` (Req. 1.2), com a Entrada/Saída (estado +
# progresso) de cada Estágio registrada de forma explícita a partir da sua
# linha persistida (Req. 1.4).

Validates: Requirements 1.2, 1.4

Esta spec reestrutura o pipeline de cinco para SEIS Estágios: a alavanca é
ampliar ``VALID_STAGES`` para ``(1, 2, 3, 4, 5, 6)`` em ``stage_service.py``,
mantendo as funções de derivação (``get_stage_states`` /
``_build_stage_state_view``) *range-agnósticas*. Este é um teste ``feature``
(``pipeline-seis-estagios`` é feature spec, não bugfix): a propriedade DEVE valer
na implementação atual do :class:`~app.services.stage.stage_service.StageService`.

A propriedade exercita a derivação de estado por Estágio sobre o range 1..6:
``get_stage_states`` deve devolver exatamente um ``StageStateView`` por número de
Estágio (1..6, Req. 1.2), e o estado/progresso de cada um deve espelhar
exatamente a sua linha ``StageRun`` — a Entrada/Saída persistida do Estágio
(Req. 1.4) — ou cair para ``nao_iniciado`` / 0 quando não há linha.

Como a propriedade é dirigida
------------------------------
A derivação de estado é *lógica pura sobre as linhas ``StageRun`` persistidas*:
o estado (e o progresso) de um Estágio é lido da sua linha ``StageRun``,
defaultando para ``nao_iniciado`` / 0 quando ainda não há linha. O gerador
produz, para cada Estágio em ``VALID_STAGES`` (importado do serviço — sem
literais 5/6 hardcoded), um mapeamento parcial arbitrário ``estagio ->
(StageState, progresso)``; um Estágio pode ser omitido inteiramente para modelar
"sem linha StageRun". O Hypothesis amostra distribuições parciais variadas — de
"nenhuma linha" a "todos os Estágios presentes" — com pelo menos 100 iterações.

Um oráculo INDEPENDENTE (``_expected_derivation``) recomputa, direto do
mapeamento gerado, o ``(estado, progresso)`` esperado por Estágio — as asserções
nunca confiam na implementação para decidir qual é o estado derivado. Asseguramos:

* ``get_stage_states`` devolve EXATAMENTE ``len(VALID_STAGES)`` ``StageStateView``,
  um por Estágio, na ordem de ``VALID_STAGES`` (sem faltar, sobrar nem duplicar
  número de Estágio) — a identificação por número 1..6 (Req. 1.2);
* para um Estágio com linha semeada, o seu ``estado`` / ``progresso`` espelham a
  linha persistida (a Entrada/Saída do Estágio — Req. 1.4);
* para um Estágio SEM linha, ``estado == nao_iniciado`` e ``progresso == 0``;
* a leitura por Estágio :meth:`StageService.get_stage_state` concorda com a visão
  agregada (mesma derivação) para cada Estágio em 1..6.

Cada exemplo cria e destrói um banco SQLite temporário próprio (espelhando
``test_stage_state_derivation_property.py`` / ``test_stage_blocking_property.py``),
sem vazamento de estado entre exemplos. NENHUM daemon Docker real e NENHUM acesso
à rede estão envolvidos — tudo é lógica de banco pura, seguro no ambiente de
desenvolvimento Windows.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import StageRun
from app.models.enums import StageState
from app.services.stage.stage_service import (
    VALID_STAGES,
    StageService,
)

# ---------------------------------------------------------------------------
# Generators for a partial mapping ``estagio -> (StageState, progresso)``.
# ---------------------------------------------------------------------------

# Os quatro estados persistíveis de uma linha de Estágio. "Sem linha" é modelado
# simplesmente omitindo o Estágio do mapeamento — o serviço deve então derivar
# ``nao_iniciado`` / 0.
_STATE_VALUES: tuple[StageState, ...] = (
    StageState.NOT_STARTED,
    StageState.IN_PROGRESS,
    StageState.COMPLETED,
    StageState.ERROR,
)

# Uma linha semeada = (estado, progresso). O progresso é um inteiro livre 0..100
# para a propriedade provar que o ``progresso`` derivado espelha a linha
# verbatim, não apenas um default {0, 100}.
_row_strategy = st.tuples(
    st.sampled_from(_STATE_VALUES),
    st.integers(min_value=0, max_value=100),
)

# Um mapeamento parcial sobre VALID_STAGES: cada Estágio é independentemente ou
# ausente (None → sem linha) ou presente com uma linha (estado, progresso).
# Dirigir as chaves por VALID_STAGES (não por 1..6 hardcoded) mantém o teste
# atado à fonte única do range do pipeline (ampliado para 1..6 nesta spec).
_distribution_strategy = st.fixed_dictionaries(
    {
        estagio: st.one_of(st.none(), _row_strategy)
        for estagio in VALID_STAGES
    }
)


# ---------------------------------------------------------------------------
# Independent oracle — recomputes the expected (estado, progresso) per stage.
# ---------------------------------------------------------------------------


def _expected_derivation(
    distribution: dict[int, Optional[tuple[StageState, int]]],
) -> dict[int, tuple[StageState, int]]:
    """Recompute the expected per-stage (estado, progresso) WITHOUT the impl.

    A seeded stage derives exactly its row's ``(estado, progresso)``; an absent
    stage derives ``(nao_iniciado, 0)``.
    """
    expected: dict[int, tuple[StageState, int]] = {}
    for estagio in VALID_STAGES:
        row = distribution.get(estagio)
        if row is None:
            expected[estagio] = (StageState.NOT_STARTED, 0)
        else:
            expected[estagio] = row
    return expected


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + schema per example).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session.

    Espelha o padrão temp-file SQLite + ``Base.create_all`` de
    ``test_stage_state_derivation_property.py``. Devolve ``(session, db_path,
    engine)`` para o chamador dispor do engine e remover o arquivo depois.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_seis_state_deriv_")
    db_path = Path(tmp_dir) / "state_deriv.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)

    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    return session_factory(), db_path, engine


def _seed_distribution(
    session: Session,
    caso_id: str,
    distribution: dict[int, Optional[tuple[StageState, int]]],
) -> None:
    """Persist one StageRun per stage whose generated row is not ``None``.

    Uma entrada ``None`` significa "sem linha" — o Estágio é deixado ausente para
    o serviço exercitar o default de linha faltante (``nao_iniciado`` / 0). O
    ``estado`` / ``progresso`` persistidos são exatamente os valores gerados, de
    modo que a propriedade pode afirmar que a derivação os espelha verbatim.
    """
    for estagio, row in distribution.items():
        if row is None:
            continue
        estado, progresso = row
        session.add(
            StageRun(
                caso_id=caso_id,
                estagio=estagio,
                estado=estado,
                progresso=progresso,
            )
        )
    session.commit()


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------


# ``deadline=None``: cada exemplo cria e destrói um banco SQLite temporário cujo
# timing de I/O varia no Windows e pode exceder o deadline por exemplo do
# Hypothesis. O banco por exemplo é intencional (sem vazamento entre
# distribuições), então desligamos o deadline em vez de enfraquecer o isolamento.
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(distribution=_distribution_strategy)
def test_property3_stage_state_derived_exactly_from_stage_run_seis(
    distribution: dict[int, Optional[tuple[StageState, int]]],
) -> None:
    """Cada Estágio em 1..6 deriva exatamente a sua StageRun (ou nao_iniciado/0 quando ausente).

    # Feature: pipeline-seis-estagios, Property 3: Derivação de estado por
    # StageRun em 1..6.
    Validates: Requirements 1.2, 1.4
    """
    session, db_path, engine = _build_ephemeral_session()
    caso_id = "caso-seis-prop3-deriv"
    try:
        _seed_distribution(session, caso_id, distribution)
        service = StageService(session)

        expected = _expected_derivation(distribution)

        case_states = service.get_stage_states(caso_id)

        # 1) Exatamente um StageStateView por Estágio, na ordem de VALID_STAGES
        #    (1..6) — sem faltar, sobrar nem duplicar número de Estágio (Req. 1.2).
        assert [v.estagio for v in case_states.estagios] == list(VALID_STAGES)
        assert len(case_states.estagios) == len(VALID_STAGES)

        views_by_stage = {v.estagio: v for v in case_states.estagios}

        for estagio in VALID_STAGES:
            exp_estado, exp_progresso = expected[estagio]
            view = views_by_stage[estagio]

            # 2) O estado/progresso derivados espelham a linha exatamente — a
            #    Entrada/Saída persistida do Estágio (Req. 1.4) — ou caem para
            #    nao_iniciado/0 quando não há linha.
            assert view.estado is exp_estado, (
                f"estagio={estagio} estado derivado {view.estado!r}, esperado "
                f"{exp_estado!r} para distribuição {distribution!r}"
            )
            assert view.progresso == exp_progresso, (
                f"estagio={estagio} progresso derivado {view.progresso}, "
                f"esperado {exp_progresso} para distribuição {distribution!r}"
            )

            # 3) A leitura por Estágio concorda com a visão agregada (mesma
            #    derivação) para cada Estágio em 1..6.
            single = service.get_stage_state(caso_id, estagio)
            assert single.estagio == estagio
            assert single.estado is exp_estado
            assert single.progresso == exp_progresso

        # 4) concluido_total sse todo Estágio derivou concluido — corolário
        #    direto da derivação por Estágio sobre todo o range 1..6.
        expected_total = all(
            exp[0] is StageState.COMPLETED for exp in expected.values()
        )
        assert case_states.concluido_total is expected_total
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    present_stage=st.sampled_from(VALID_STAGES),
    estado=st.sampled_from(_STATE_VALUES),
    progresso=st.integers(min_value=0, max_value=100),
)
def test_property3_absent_stages_default_to_nao_iniciado_zero_seis(
    present_stage: int,
    estado: StageState,
    progresso: int,
) -> None:
    """Só o Estágio semeado reflete a sua linha; todo outro Estágio é nao_iniciado/0.

    Semeia um único Estágio e afirma que exatamente esse Estágio deriva o
    ``(estado, progresso)`` semeado, enquanto todos os outros Estágios em 1..6 —
    que não têm linha — derivam ``nao_iniciado`` / 0. Isola o default de linha
    faltante da derivação semeada, cobrindo os seis números de Estágio (Req. 1.2).

    # Feature: pipeline-seis-estagios, Property 3: Derivação de estado por
    # StageRun em 1..6.
    Validates: Requirements 1.2, 1.4
    """
    session, db_path, engine = _build_ephemeral_session()
    caso_id = "caso-seis-prop3-deriv-single"
    try:
        distribution: dict[int, Optional[tuple[StageState, int]]] = {
            estagio: None for estagio in VALID_STAGES
        }
        distribution[present_stage] = (estado, progresso)
        _seed_distribution(session, caso_id, distribution)
        service = StageService(session)

        case_states = service.get_stage_states(caso_id)
        assert [v.estagio for v in case_states.estagios] == list(VALID_STAGES)
        views_by_stage = {v.estagio: v for v in case_states.estagios}

        for estagio in VALID_STAGES:
            view = views_by_stage[estagio]
            if estagio == present_stage:
                assert view.estado is estado
                assert view.progresso == progresso
            else:
                assert view.estado is StageState.NOT_STARTED
                assert view.progresso == 0
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
