"""Property test da conclusão automática do Estágio 2 — parte DAG (task 5.2).

# Feature: pipeline-cinco-estagios, Property 9: Conclusão automática dos
# Estágios 2 e 4. Para um Caso curado, assim que o Estágio 1 (Modelagem
# Estrutural) conclui e há um DAG curado derivável, o Estágio 2 (Geração do DAG)
# auto-conclui carregando o DAG. Respeita o gate sequencial e não ocorre sem o
# material curado (DAG ausente).

Validates: Requirements 9.1, 9.3, 9.4

Esta é uma property test de *feature* (a spec ``pipeline-cinco-estagios`` é uma
feature, não um bugfix): a propriedade é esperada **valer** sobre a
implementação atual de
:meth:`~app.services.dag.dag_service.DagService.auto_concluir_estagio2`.

Propriedade (design.md — Property 9, parte DAG)
-----------------------------------------------
Dirige o gate, a auto-conclusão idempotente e o caminho de não-conclusão do
Estágio 2 sobre uma distribuição **arbitrária** de estados dos ``StageRun`` de
um Caso. O estado do Estágio 1 (Modelagem) é semeado diretamente como linha
``StageRun`` para isolar o gate de forma determinística; a derivação do DAG usa
o :class:`CuradoriaGeradorDeDag` **real** sobre um ``{slug}_dag.json`` escrito
num data dir temporário. Nenhum caminho toca a rede nem um cliente de IA (não há
um neste caminho) — tudo é lógica pura de BD sobre SQLite efêmero.

1. **Gate (Req. 9.3).** Para qualquer distribuição de estados em que o Estágio 1
   **não** está ``concluido``, ``auto_concluir_estagio2`` devolve
   ``concluido=False`` / ``bloqueado_pelo_gate=True`` e **não** persiste nenhuma
   linha ``estagio=2`` concluída — independentemente de haver DAG curado.
2. **Auto-conclusão (Req. 9.1).** Quando o Estágio 1 está ``concluido`` e há um
   DAG curado derivável, o Estágio 2 auto-conclui carregando o DAG: o
   ``StageRun`` de ``(caso, estagio=2)`` fica ``concluido``/100; e é
   **idempotente** — chamar ``N`` vezes (``N`` dirigido por Hypothesis) produz
   exatamente a mesma linha única (mesmo ``id``, sem acúmulo), e o backfill não
   duplica a linha ``Dag`` (uma única entidade curada).
3. **Sem DAG (Req. 9.4).** Com o Estágio 1 ``concluido`` mas **sem** DAG curado
   derivável (data dir sem ``{slug}_dag.json``), ``auto_concluir_estagio2``
   devolve ``sem_dag=True``, não conclui, o avanço para o Estágio 3 permanece
   bloqueado, e o backfill é idempotente (nenhuma linha ``Dag`` é persistida).

Estratégia / isolamento
-----------------------
Cada exemplo aponta um :class:`CaseService` para um diretório de dados
**temporário** com o layout ``<data_dir>/{api,dag}/`` (igual ao real), no qual
escrevemos (ou não) um ``{slug}_dag.json`` gerado, e constrói um ``DagService``
sobre uma ``Session`` SQLite **efêmera** (engine em arquivo temporário com o
esquema completo via ``Base.metadata.create_all``, igual às fixtures de
``test_backfill_idempotente_property.py`` / ``test_escolha_dag_property.py``). O
``DagService`` recebe o ``CuradoriaGeradorDeDag`` real sobre o mesmo
``CaseService``; o ``_obter_modelagem`` lê o Estágio 1 concluído (``read_stage1``,
somente-leitura) a partir do mesmo fixture. Os DAGs gerados são acíclicos por
construção.
"""

from __future__ import annotations

import json
import os
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
from app.models.domain import Dag as DagModel
from app.models.domain import StageRun
from app.models.enums import OrigemDag, StageState
from app.services.case_service import CURATED_CASES, CaseService
from app.services.dag.dag_service import DagService
from app.services.dag.gerador import CuradoriaGeradorDeDag
from app.services.stage.stage_service import (
    ESTAGIO_GERACAO_DAG,
    ESTAGIO_MODELAGEM,
    VALID_STAGES,
    StageService,
)

# Um slug de caso curado real: a geração de curadoria só produz um DAG para
# slugs curados (``CaseService.try_load_case``).
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guarda: mantém o slug do fixture curado


# ---------------------------------------------------------------------------
# Fixture de banco efêmero (igual a test_escolha_dag_property.py).
# ---------------------------------------------------------------------------


def _make_session_factory() -> tuple[sessionmaker[Session], str]:
    """Cria um engine SQLite efêmero e devolve ``(factory, tmp_dir)``."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline5_dag_auto_prop_")
    db_path = Path(tmp_dir) / "dag_auto.db"
    settings = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings)
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
    return factory, tmp_dir


def _rmtree(base: Path) -> None:
    """Remove um diretório recursivamente (teardown dos temporários)."""
    for root, _dirs, files in os.walk(base, topdown=False):
        for name in files:
            os.remove(os.path.join(root, name))
        os.rmdir(root)


# ---------------------------------------------------------------------------
# Construtor de fixture — escreve um {slug}_dag.json gerado num data dir
# temporário com o layout <data_dir>/{api,dag}/, igual ao real. Sem rede/IA.
# ---------------------------------------------------------------------------


def _write_case_fixture(
    data_dir: Path,
    slug: str,
    structural_nodes: list[dict],
    *,
    com_dag: bool = True,
) -> None:
    """Escreve os fixtures de ability (+ DAG, se ``com_dag``) para ``slug``.

    Com ``com_dag=False`` NÃO escreve ``{slug}_dag.json`` — modela um Caso curado
    sem DAG derivável (``try_load_case`` falha e o gerador devolve lista vazia).
    A ability vazia é sempre escrita (ambiente de fixture consistente).
    """
    api_dir = data_dir / "api"
    dag_dir = data_dir / "dag"
    api_dir.mkdir(parents=True, exist_ok=True)
    dag_dir.mkdir(parents=True, exist_ok=True)

    (api_dir / f"{slug}_dag-ability.json").write_text("[]", encoding="utf-8")

    if not com_dag:
        return

    dag_payload = {
        "campaign_name": slug,
        "metadata": {"validation": {"is_dag": True}},
        "structural_nodes": structural_nodes,
    }
    (dag_dir / f"{slug}_dag.json").write_text(
        json.dumps(dag_payload), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Estratégia Hypothesis — um DAG acíclico via child_nodes (anterior->posterior).
# ---------------------------------------------------------------------------


@st.composite
def _acyclic_dag(draw) -> list[dict]:
    """Sorteia os ``structural_nodes`` de um DAG acíclico."""
    n = draw(st.integers(min_value=1, max_value=8))
    node_ids = [f"node-{i:02d}" for i in range(n)]

    children: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for i in range(n):
        for j in range(i + 1, n):
            if draw(st.booleans()):
                children[node_ids[i]].append(node_ids[j])

    return [
        {
            "node_id": node_ids[i],
            "node_index": i,
            "technique_id": f"T{1000 + i}",
            "technique_name": f"Technique {i}",
            "tactic": f"tactic-{i}",
            "child_nodes": children[node_ids[i]],
            "parent_nodes": [],
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Semeadura de estados dos Estágios e consultas auxiliares.
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

# Estados do Estágio 1 que NÃO são ``concluido`` (para dirigir o gate fechado).
_NON_COMPLETED_CHOICES: tuple[Optional[StageState], ...] = tuple(
    s for s in _STATE_CHOICES if s is not StageState.COMPLETED
)

# Os Estágios semeados arbitrariamente (todos menos o 1 e o 2, controlados pelo
# teste).
_OTHER_STAGES: tuple[int, ...] = tuple(
    e for e in VALID_STAGES if e not in (ESTAGIO_MODELAGEM, ESTAGIO_GERACAO_DAG)
)


@st.composite
def _other_stage_states(draw) -> dict[int, Optional[StageState]]:
    """Distribuição arbitrária de estados para os Estágios que não o 1 nem o 2."""
    return {
        estagio: draw(st.sampled_from(_STATE_CHOICES)) for estagio in _OTHER_STAGES
    }


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


def _estagio2_rows(session: Session, caso_id: str) -> list[StageRun]:
    """Todas as linhas ``StageRun`` de ``(caso_id, estagio=2)``, por ``id``."""
    return list(
        session.execute(
            select(StageRun)
            .where(StageRun.caso_id == caso_id)
            .where(StageRun.estagio == ESTAGIO_GERACAO_DAG)
            .order_by(StageRun.id.asc())
        )
        .scalars()
        .all()
    )


def _count_estagio2_concluido(session: Session, caso_id: str) -> int:
    """Quantas linhas ``estagio=2`` concluídas existem para ``caso_id``."""
    return (
        session.execute(
            select(func.count())
            .select_from(StageRun)
            .where(StageRun.caso_id == caso_id)
            .where(StageRun.estagio == ESTAGIO_GERACAO_DAG)
            .where(StageRun.estado == StageState.COMPLETED)
        ).scalar_one()
    )


def _count_dags(session: Session, caso_id: str) -> int:
    """Quantas linhas ``Dag`` persistidas existem para ``caso_id``."""
    return (
        session.execute(
            select(func.count())
            .select_from(DagModel)
            .where(DagModel.caso_id == caso_id)
        ).scalar_one()
    )


def _build_dag_service(
    session: Session, data_dir: Path
) -> tuple[DagService, StageService]:
    """Monta um ``DagService`` + ``StageService`` sobre a mesma Session/fixture."""
    case_service = CaseService(data_dir=data_dir)
    dag_service = DagService(
        session=session,
        case_service=case_service,
        gerador=CuradoriaGeradorDeDag(case_service),
    )
    stage_service = StageService(session, case_service=case_service)
    return dag_service, stage_service


# ===========================================================================
# Propriedade 9.1 — Gate (Req. 9.3): Estágio 1 não concluído ⇒ não conclui.
# ===========================================================================


# Feature: pipeline-cinco-estagios, Property 9 (parte DAG)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    estagio1=st.sampled_from(_NON_COMPLETED_CHOICES),
    estagio2=st.sampled_from(_STATE_CHOICES),
    outros=_other_stage_states(),
    structural_nodes=_acyclic_dag(),
)
def test_property9_gate_bloqueia_sem_estagio1_concluido(
    estagio1: Optional[StageState],
    estagio2: Optional[StageState],
    outros: dict[int, Optional[StageState]],
    structural_nodes: list[dict],
) -> None:
    """Estágio 1 não concluído ⇒ gate fechado, não conclui o Estágio 2.

    Para qualquer distribuição de estados onde o Estágio 1 (Modelagem) não é
    ``concluido``, o gate sequencial tem precedência: ``auto_concluir_estagio2``
    devolve ``concluido=False`` / ``bloqueado_pelo_gate=True`` e não persiste
    nenhuma linha ``estagio=2`` *nova* — mesmo havendo um DAG curado derivável no
    data dir. O gate é avaliado antes da idempotência, então o resultado é
    "bloqueado" mesmo que já exista uma linha ``estagio=2`` concluída de antes.

    Validates: Requirements 9.1, 9.3, 9.4
    """
    assert estagio1 is not StageState.COMPLETED

    data_dir = Path(tempfile.mkdtemp(prefix="pipeline5_dag_auto_data_"))
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        # Há DAG curado no disco — para provar que o gate tem precedência mesmo
        # com material disponível.
        _write_case_fixture(data_dir, _CURATED_SLUG, structural_nodes)
        dag_service, _stage_service = _build_dag_service(session, data_dir)

        states = dict(outros)
        states[ESTAGIO_MODELAGEM] = estagio1
        states[ESTAGIO_GERACAO_DAG] = estagio2
        _seed_stage_runs(session, _CURATED_SLUG, states)

        concluidas_antes = _count_estagio2_concluido(session, _CURATED_SLUG)

        resultado = dag_service.auto_concluir_estagio2(_CURATED_SLUG)

        # Gate fechado tem precedência: não conclui e sinaliza o bloqueio.
        assert resultado.concluido is False
        assert resultado.bloqueado_pelo_gate is True
        assert resultado.sem_dag is False
        # Nenhuma conclusão foi feita por esta chamada: a contagem permanece a
        # mesma de antes (0 quando estagio2 não era concluido; 1 quando já era).
        assert _count_estagio2_concluido(session, _CURATED_SLUG) == concluidas_antes
        assert concluidas_antes == (
            1 if estagio2 is StageState.COMPLETED else 0
        )
        # O gate fecha antes do backfill: nenhum Dag é persistido.
        assert _count_dags(session, _CURATED_SLUG) == 0
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _rmtree(data_dir)
        _rmtree(Path(db_tmp_dir))


# ===========================================================================
# Propriedade 9.2 — Auto-conclusão idempotente carregando o DAG (Req. 9.1).
# ===========================================================================


# Feature: pipeline-cinco-estagios, Property 9 (parte DAG)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    estagio2_inicial=st.sampled_from(_NON_COMPLETED_CHOICES),
    outros=_other_stage_states(),
    structural_nodes=_acyclic_dag(),
    n=st.integers(min_value=1, max_value=6),
)
def test_property9_auto_conclui_carregando_dag_e_idempotente(
    estagio2_inicial: Optional[StageState],
    outros: dict[int, Optional[StageState]],
    structural_nodes: list[dict],
    n: int,
) -> None:
    """Estágio 1 concluído + DAG curado ⇒ auto-conclui; N chamadas == 1.

    Com o gate liberado (Estágio 1 ``concluido``) e um DAG curado derivável, o
    Estágio 2 auto-conclui carregando o DAG: a linha ``(caso, estagio=2)`` fica
    ``concluido``/100. Chamar ``N`` vezes é idempotente — exatamente uma linha
    ``estagio=2`` com ``id`` estável, e o backfill não duplica a linha ``Dag``
    (uma única entidade curada).

    Validates: Requirements 9.1, 9.3
    """
    data_dir = Path(tempfile.mkdtemp(prefix="pipeline5_dag_auto_data_"))
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        _write_case_fixture(data_dir, _CURATED_SLUG, structural_nodes)
        dag_service, stage_service = _build_dag_service(session, data_dir)

        states = dict(outros)
        states[ESTAGIO_MODELAGEM] = StageState.COMPLETED
        states[ESTAGIO_GERACAO_DAG] = estagio2_inicial
        _seed_stage_runs(session, _CURATED_SLUG, states)

        # ANTES: o Estágio 3 está bloqueado enquanto o Estágio 2 não concluir.
        assert stage_service.is_stage_startable(_CURATED_SLUG, 3) is False

        # 1ª chamada: auto-conclui o Estágio 2 carregando o DAG curado.
        primeiro = dag_service.auto_concluir_estagio2(_CURATED_SLUG)
        assert primeiro.concluido is True
        assert primeiro.bloqueado_pelo_gate is False
        assert primeiro.sem_dag is False
        assert primeiro.total_dags == 1
        assert primeiro.stage_run_id is not None

        rows_1 = _estagio2_rows(session, _CURATED_SLUG)
        assert len(rows_1) == 1
        linha = rows_1[0]
        assert linha.estado is StageState.COMPLETED
        assert linha.progresso == 100
        id_estavel = linha.id

        # DEPOIS: o Estágio 2 concluído ⇒ Estágio 3 liberado (gate sequencial).
        assert stage_service.is_stage_startable(_CURATED_SLUG, 3) is True

        # Chamadas 2..N são no-ops idempotentes: mesma linha única, mesmo id, e
        # o backfill continua com exatamente um Dag persistido.
        for _ in range(n - 1):
            repetido = dag_service.auto_concluir_estagio2(_CURATED_SLUG)
            assert repetido.concluido is True
            assert repetido.stage_run_id == id_estavel

        rows_n = _estagio2_rows(session, _CURATED_SLUG)
        assert len(rows_n) == 1
        assert rows_n[0].id == id_estavel
        assert rows_n[0].estado is StageState.COMPLETED
        assert rows_n[0].progresso == 100
        assert _count_estagio2_concluido(session, _CURATED_SLUG) == 1
        # Backfill idempotente: exatamente um Dag curado, sem duplicar.
        assert _count_dags(session, _CURATED_SLUG) == 1
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _rmtree(data_dir)
        _rmtree(Path(db_tmp_dir))


# ===========================================================================
# Propriedade 9.3 — Sem DAG derivável ⇒ não conclui, avanço bloqueado (Req. 9.4).
# ===========================================================================


# Feature: pipeline-cinco-estagios, Property 9 (parte DAG)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    estagio2_inicial=st.sampled_from(_NON_COMPLETED_CHOICES),
    outros=_other_stage_states(),
    n=st.integers(min_value=1, max_value=6),
)
def test_property9_sem_dag_nao_conclui_e_avanco_bloqueado(
    estagio2_inicial: Optional[StageState],
    outros: dict[int, Optional[StageState]],
    n: int,
) -> None:
    """Estágio 1 concluído mas SEM DAG derivável ⇒ ``sem_dag``, não conclui.

    Com o gate liberado (Estágio 1 ``concluido``) mas sem DAG curado derivável (o
    data dir não tem ``{slug}_dag.json``), ``auto_concluir_estagio2`` devolve
    ``sem_dag=True``, não conclui a linha ``estagio=2``, e o avanço para o
    Estágio 3 permanece bloqueado. O backfill é idempotente: nenhuma linha
    ``Dag`` é persistida, mesmo após ``N`` chamadas.

    Validates: Requirements 9.3, 9.4
    """
    data_dir = Path(tempfile.mkdtemp(prefix="pipeline5_dag_auto_data_"))
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        # Caso curado SEM DAG derivável: não escreve ``{slug}_dag.json``.
        _write_case_fixture(data_dir, _CURATED_SLUG, [], com_dag=False)
        dag_service, stage_service = _build_dag_service(session, data_dir)

        states = dict(outros)
        states[ESTAGIO_MODELAGEM] = StageState.COMPLETED
        states[ESTAGIO_GERACAO_DAG] = estagio2_inicial
        _seed_stage_runs(session, _CURATED_SLUG, states)

        for _ in range(n):
            resultado = dag_service.auto_concluir_estagio2(_CURATED_SLUG)
            # Sem DAG derivável: não conclui, sinaliza ``sem_dag``, gate liberado.
            assert resultado.concluido is False
            assert resultado.sem_dag is True
            assert resultado.bloqueado_pelo_gate is False
            assert resultado.total_dags == 0
            assert resultado.stage_run_id is None

        # Nada concluído: nenhuma linha ``estagio=2`` concluída e o Estágio 3
        # permanece bloqueado pelo gate sequencial (Req. 9.4).
        assert _count_estagio2_concluido(session, _CURATED_SLUG) == 0
        assert stage_service.is_stage_startable(_CURATED_SLUG, 3) is False
        # Backfill idempotente: nenhum Dag inválido é persistido.
        assert _count_dags(session, _CURATED_SLUG) == 0
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _rmtree(data_dir)
        _rmtree(Path(db_tmp_dir))
