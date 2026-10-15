"""Property tests da escolha de DAG e do gate sequencial do Estágio 2 (task 5.5).

# Feature: pipeline-cinco-estagios, Property 9: Escolher um DAG conclui o
# Estágio 2 (Geração do DAG) e libera o Estágio 3.
# Feature: pipeline-cinco-estagios, Property 10: Sem DAG, o avanço além do
# Estágio 2 permanece bloqueado.

Validates: Requirements 4.7, 6.2, 6.3, 6.5

Estas são property tests de *feature* (a spec ``pipeline-cinco-estagios`` é uma
feature, não um bugfix): as propriedades são esperadas **valer** sobre a
implementação atual de
:meth:`~app.services.dag.dag_service.DagService.escolher_dag` e do gate
sequencial de :class:`~app.services.stage.stage_service.StageService`.

No pipeline de seis Estágios, a Geração do DAG é o **Estágio 3** (sua Entrada são
os atributos da campanha do Estágio 2 — Seleção da campanha): ``escolher_dag``
grava o ``dag_escolhido_id`` no ``StageRun`` de ``estagio=3`` e o conclui,
liberando o Estágio 4 (Operacionalização). ``EscolhaDagResult.estagio`` é
:data:`ESTAGIO_GERACAO_DAG` (``== 3``).

Propriedades (design.md — Properties 9 e 10)
--------------------------------------------
(a) *Escolha válida conclui o Estágio 3 e é idempotente* (Property 9):
    após ``sincronizar_dags_curados(slug)``, escolher um ``dag_id`` válido e
    persistido do Caso faz ``escolher_dag(slug, dag_id)`` devolver
    ``sucesso=True``; o ``StageRun`` de ``estagio=3`` fica ``concluido`` com
    ``dag_escolhido_id == dag_id``; repetir a mesma escolha ``N`` vezes é
    idempotente — mesmo ``stage_run_id``, exatamente uma linha de ``estagio=3``,
    mesmo ``dag_escolhido_id`` (Req. 4.7, 6.3).

(b) *dag_id inexistente/alheio é recusado sem tocar o Estágio 3*:
    um ``dag_id`` que não existe, ou que pertence a **outro** Caso, é recusado
    com ``falha == DAG_NAO_ENCONTRADO`` e o ``StageRun`` de ``estagio=3`` fica
    inalterado (ou ausente) (Req. 6.3 — erro).

(c) *Campanha sem DAG persistido recusa com SEM_DAG* (gate do Req. 6.2):
    sem nenhum ``Dag`` persistido para a Campanha, ``escolher_dag`` recusa com
    ``falha == SEM_DAG`` e nada é concluído.

(d) *Gate sequencial do Estágio 4* (Property 10):
    enquanto o Estágio 3 (Geração do DAG) não estiver ``concluido``, o Estágio 4
    permanece bloqueado (``is_stage_startable(caso, 4)`` é ``False`` e a view
    reporta ``bloqueado=True``); após uma escolha bem-sucedida (Estágio 3
    ``concluido``), o Estágio 4 torna-se iniciável (Req. 6.2, 6.3).

Estratégia / isolamento
-----------------------
Cada exemplo aponta um :class:`CaseService` para um diretório de dados
**temporário** com o layout ``<data_dir>/{api,dag}/`` (igual ao real), no qual
escrevemos um ``{slug}_dag.json`` gerado, e constrói um ``DagService`` + um
``StageService`` sobre a **mesma** ``Session`` SQLite **efêmera** (engine em
arquivo temporário com o esquema completo via ``Base.metadata.create_all``,
igual às fixtures de ``test_backfill_idempotente_property.py`` e
``test_stage_blocking_property.py``). Os DAGs gerados são acíclicos por
construção. Nada aqui toca a rede nem um cliente de IA (não há um neste caminho).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

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
from app.services.dag.dag_service import (
    ESTAGIO_GERACAO_DAG,
    DagService,
    EscolhaDagFalha,
)
from app.services.dag.gerador import CuradoriaGeradorDeDag
from app.services.stage.stage_service import StageService

# Um slug de caso curado real: a geração de curadoria só produz um DAG para
# slugs curados (``CaseService.try_load_case``).
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guarda: mantém o slug do fixture curado

# Um segundo slug curado, para o cenário "dag_id de outra Campanha" em (b).
_OTHER_SLUG = "apt41_dust"
assert _OTHER_SLUG in CURATED_CASES
assert _OTHER_SLUG != _CURATED_SLUG


# ---------------------------------------------------------------------------
# Fixture de banco efêmero (igual a test_backfill_idempotente_property.py).
# ---------------------------------------------------------------------------


def _make_session_factory() -> tuple[sessionmaker[Session], str]:
    """Cria um engine SQLite efêmero e devolve ``(factory, tmp_dir)``."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_escolha_dag_prop_")
    db_path = Path(tmp_dir) / "escolha_dag.db"
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
) -> None:
    """Escreve os fixtures de ability + DAG para ``slug`` sob ``data_dir``."""
    api_dir = data_dir / "api"
    dag_dir = data_dir / "dag"
    api_dir.mkdir(parents=True, exist_ok=True)
    dag_dir.mkdir(parents=True, exist_ok=True)

    (api_dir / f"{slug}_dag-ability.json").write_text("[]", encoding="utf-8")

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


def _estagio_dag_rows(session: Session, caso: str) -> list[StageRun]:
    """Todas as linhas de ``StageRun`` de ``estagio=3`` (DAG) do Caso, por ``id``."""
    return (
        session.execute(
            select(StageRun)
            .where(StageRun.caso_id == caso)
            .where(StageRun.estagio == ESTAGIO_GERACAO_DAG)
            .order_by(StageRun.id.asc())
        )
        .scalars()
        .all()
    )


def _count_estagio_dag(session: Session, caso: str) -> int:
    """Quantidade de linhas de ``StageRun`` de ``estagio=3`` (DAG) do Caso."""
    return (
        session.execute(
            select(func.count())
            .select_from(StageRun)
            .where(StageRun.caso_id == caso)
            .where(StageRun.estagio == ESTAGIO_GERACAO_DAG)
        )
        .scalar_one()
    )


# ===========================================================================
# Property 9 — escolha válida conclui o Estágio 2, é idempotente, e libera o
# Estágio 3 (gate — Property 10, parte "depois").
# ===========================================================================


# Feature: pipeline-cinco-estagios, Property 9: Escolher um DAG conclui o
# Estágio 2 e libera o Estágio 3.
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(structural_nodes=_acyclic_dag(), repeticoes=st.integers(min_value=1, max_value=5))
def test_property9_escolha_valida_conclui_estagio2_e_e_idempotente(
    structural_nodes: list[dict], repeticoes: int
) -> None:
    """Escolher um ``dag_id`` válido conclui o Estágio 2; repetir é idempotente.

    Após o backfill, escolher um DAG persistido do Caso devolve ``sucesso=True``;
    o ``StageRun`` de ``estagio=3`` fica ``concluido`` (progresso 100) com
    ``dag_escolhido_id == dag_id``; repetir a mesma escolha ``N`` vezes mantém o
    mesmo ``stage_run_id``, exatamente uma linha de ``estagio=3`` e o mesmo
    ``dag_escolhido_id`` (Req. 4.7, 6.3). Antes da escolha o Estágio 4 está
    bloqueado; depois, torna-se iniciável (gate — Req. 6.2, 6.3).

    Validates: Requirements 4.7, 6.2, 6.3
    """
    data_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_escolha_dag_data_"))
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        _write_case_fixture(data_dir, _CURATED_SLUG, structural_nodes)
        case_service = CaseService(data_dir=data_dir)
        dag_service = DagService(
            session=session,
            case_service=case_service,
            gerador=CuradoriaGeradorDeDag(case_service),
        )
        stage_service = StageService(session)

        # Backfill: persiste exatamente um Dag curadoria_humana do Caso. É a
        # *escolha* que conclui o Estágio 3 (Geração do DAG) e libera o Estágio 4
        # (Operacionalização) pelo gate sequencial.
        resumos = dag_service.sincronizar_dags_curados(_CURATED_SLUG)
        assert len(resumos) == 1
        dag_id = resumos[0].id
        assert resumos[0].origem == OrigemDag.CURADORIA_HUMANA

        # (d) — ANTES da escolha: o Estágio 4 está bloqueado (gate sequencial),
        # pois o Estágio 3 (Geração do DAG) ainda não está concluido (Req. 6.2).
        assert stage_service.is_stage_startable(_CURATED_SLUG, 4) is False
        view4_antes = stage_service.get_stage_state(_CURATED_SLUG, 4)
        assert view4_antes.bloqueado is True
        assert view4_antes.iniciavel is False

        # 1ª escolha: sucesso, conclui o Estágio 3 com dag_escolhido_id = dag_id.
        resultado = dag_service.escolher_dag(_CURATED_SLUG, dag_id)
        assert resultado.sucesso is True
        assert resultado.falha is None
        assert resultado.estagio == ESTAGIO_GERACAO_DAG
        assert resultado.stage_run_id is not None
        primeiro_stage_run_id = resultado.stage_run_id

        linhas = _estagio_dag_rows(session, _CURATED_SLUG)
        assert len(linhas) == 1
        linha = linhas[0]
        assert linha.id == primeiro_stage_run_id
        assert linha.estado == StageState.COMPLETED
        assert linha.progresso == 100
        assert linha.dag_escolhido_id == dag_id

        # (d) — DEPOIS da escolha: o Estágio 3 está concluido ⇒ Estágio 4 liberado.
        assert stage_service.is_stage_startable(_CURATED_SLUG, 4) is True
        view4_depois = stage_service.get_stage_state(_CURATED_SLUG, 4)
        assert view4_depois.bloqueado is False
        assert view4_depois.iniciavel is True

        # Idempotência: repetir a MESMA escolha N vezes não muda o estado — mesmo
        # stage_run_id, exatamente uma linha de estagio=3, mesmo dag_escolhido_id.
        for _ in range(repeticoes):
            repetido = dag_service.escolher_dag(_CURATED_SLUG, dag_id)
            assert repetido.sucesso is True
            assert repetido.stage_run_id == primeiro_stage_run_id

        assert _count_estagio_dag(session, _CURATED_SLUG) == 1
        linha_final = _estagio_dag_rows(session, _CURATED_SLUG)[0]
        assert linha_final.id == primeiro_stage_run_id
        assert linha_final.dag_escolhido_id == dag_id
        assert linha_final.estado == StageState.COMPLETED
        assert linha_final.progresso == 100
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _rmtree(data_dir)
        _rmtree(Path(db_tmp_dir))


# ===========================================================================
# Property (b) — dag_id inexistente/alheio é recusado (DAG_NAO_ENCONTRADO) sem
# alterar o StageRun do Estágio 2.
# ===========================================================================


# Feature: pipeline-cinco-estagios, Property 9: Escolher um DAG conclui o
# Estágio 2 e libera o Estágio 3 (vertente negativa — dag alheio/inexistente).
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    structural_nodes=_acyclic_dag(),
    other_nodes=_acyclic_dag(),
    desloca=st.integers(min_value=1, max_value=1000),
)
def test_propertyb_dag_inexistente_ou_alheio_e_recusado(
    structural_nodes: list[dict], other_nodes: list[dict], desloca: int
) -> None:
    """``dag_id`` inexistente/alheio ⇒ ``DAG_NAO_ENCONTRADO``; Estágio 2 intacto.

    Com um DAG persistido do Caso-alvo (para passar do gate ``SEM_DAG``) e um DAG
    de **outra** Campanha, escolher o ``dag_id`` da outra Campanha — ou um
    ``dag_id`` inexistente — é recusado com ``falha == DAG_NAO_ENCONTRADO`` e o
    ``StageRun`` de ``estagio=3`` do Caso-alvo fica inalterado (não concluído por
    nenhuma escolha válida).

    Validates: Requirements 6.3
    """
    data_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_escolha_dag_data_"))
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        _write_case_fixture(data_dir, _CURATED_SLUG, structural_nodes)
        _write_case_fixture(data_dir, _OTHER_SLUG, other_nodes)
        case_service = CaseService(data_dir=data_dir)
        dag_service = DagService(
            session=session,
            case_service=case_service,
            gerador=CuradoriaGeradorDeDag(case_service),
        )

        # Persiste um DAG para cada Campanha. O Caso-alvo TEM um DAG (passa do
        # gate SEM_DAG), mas o dag_id escolhido será o da OUTRA Campanha.
        resumos_alvo = dag_service.sincronizar_dags_curados(_CURATED_SLUG)
        resumos_outro = dag_service.sincronizar_dags_curados(_OTHER_SLUG)
        assert len(resumos_alvo) == 1
        assert len(resumos_outro) == 1
        dag_id_alheio = resumos_outro[0].id

        # dag_id inexistente: um id acima do máximo persistido.
        max_id = session.execute(select(func.max(DagModel.id))).scalar_one()
        dag_id_inexistente = max_id + desloca

        for dag_id in (dag_id_alheio, dag_id_inexistente):
            resultado = dag_service.escolher_dag(_CURATED_SLUG, dag_id)
            assert resultado.sucesso is False
            assert resultado.falha == EscolhaDagFalha.DAG_NAO_ENCONTRADO
            assert resultado.stage_run_id is None

        # O StageRun do Estágio 3 (Geração do DAG) do Caso-alvo NÃO foi
        # criado/alterado: nenhuma escolha válida ocorreu, logo não há linha
        # concluída de estagio=3. (O backfill materializa os atributos da
        # campanha, mas isso não cria uma linha de estagio=3 nem a conclui.)
        linhas_alvo = _estagio_dag_rows(session, _CURATED_SLUG)
        assert linhas_alvo == []
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _rmtree(data_dir)
        _rmtree(Path(db_tmp_dir))


# ===========================================================================
# Property (c) — Campanha sem DAG persistido recusa com SEM_DAG e o avanço além
# do Estágio 3 permanece bloqueado (Property 10 — Req. 6.2).
# ===========================================================================


# Feature: pipeline-cinco-estagios, Property 10: Sem DAG, o avanço além do
# Estágio 3 permanece bloqueado.
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(dag_id=st.integers(min_value=1, max_value=10_000))
def test_property10_sem_dag_recusa_e_estagio4_bloqueado(dag_id: int) -> None:
    """Sem DAG persistido ⇒ ``SEM_DAG`` e o Estágio 4 permanece bloqueado.

    Com uma Campanha sem nenhum ``Dag`` persistido, ``escolher_dag`` recusa com
    ``falha == SEM_DAG`` para qualquer ``dag_id`` e nada é concluído; sem o
    Estágio 3 (Geração do DAG) concluído, o gate sequencial mantém o Estágio 4
    (Operacionalização) bloqueado (Req. 6.2).

    Validates: Requirements 6.2
    """
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        dag_service = DagService(session=session)
        stage_service = StageService(session)

        # Nenhum backfill: a Campanha não tem DAG persistido (gate Req. 6.2).
        resultado = dag_service.escolher_dag(_CURATED_SLUG, dag_id)
        assert resultado.sucesso is False
        assert resultado.falha == EscolhaDagFalha.SEM_DAG
        assert resultado.stage_run_id is None

        # Nada foi concluído: não há linha de estagio=3, e o Estágio 4 segue
        # bloqueado pelo gate sequencial (o Estágio 3 não concluiu).
        assert _estagio_dag_rows(session, _CURATED_SLUG) == []
        assert stage_service.is_stage_startable(_CURATED_SLUG, 4) is False
        view4 = stage_service.get_stage_state(_CURATED_SLUG, 4)
        assert view4.bloqueado is True
        assert view4.iniciavel is False
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _rmtree(Path(db_tmp_dir))
