"""Testes de serviço da orquestração ativa do Estágio 5 (task 5.5).

Exercita :meth:`~app.services.environment.stage_service.EnvironmentStageService.provisionar_estagio5`
— o **provisionamento ativo e automático** do Estágio 5 (Criação do ambiente de
simulação), disparado sem ação manual ao iniciar o Estágio (Req. 1.1) — de ponta
a ponta sobre um engine SQLite **efêmero** (mesma costura das fixtures de
``test_provisionamento_auto_property.py``), semeando linhas ``StageRun`` por Caso.

Em vez de tocar o Docker, o disco ou a costura real de operacionalização, injeta:

* um :class:`_FakeProvisionador` controlável — satisfaz estruturalmente o
  :class:`~app.services.environment.provisionador.ProvisionadorDeAmbiente`,
  emite :class:`~app.services.environment.compose_executor.LinhaLog` via ``on_log``
  (na ordem) e devolve um :class:`~app.services.environment.provisionador.AmbienteProvisionado`
  configurável (pronto ou com ``etapa_falha``), **contando** as chamadas a
  ``provisionar`` (para asserir a idempotência);
* um ``params_provider`` *lambda* que resolve os
  :class:`~app.services.environment.operacionalizacao.ParametrosDeAmbiente` sem
  tocar a operacionalização nem o disco;
* uma instância **real** de
  :class:`~app.services.environment.log_buffer.ProvisioningLogBuffer`, de modo que
  as asserções inspecionem o buffer que o canal WebSocket lê (Req. 2.5, 2.7).

Nenhum caminho toca o daemon Docker, a rede ou a IA — tudo é lógica pura de BD +
*fakes* em memória, seguro no ambiente de dev.

Cobertura (cada teste cita os requisitos que satisfaz):

1. Provisionamento automático (Req. 1.1): Estágio 4 concluído + ambiente pronto
   ⇒ o pipeline roda sem ação manual, conclui o Estágio 5 e devolve ``pronto``.
2. Gate (Req. 1.1): Estágio 4 não concluído ⇒ ``bloqueado_pelo_gate``, sem subir
   nem concluir nada.
3. Conclusão só com os três pré-requisitos (Req. 5.5): ambiente com ``etapa_falha``
   ⇒ o Estágio 5 **não** conclui (``ERROR``), com ``etapa_falha``/``mensagem_erro``.
4. Falha bloqueia o Estágio 6 (Req. 6.4): após falha o Estágio 6 não é iniciável;
   após sucesso ele é liberado.
5. Buffer e recuperação (Req. 7.1, 7.3): as linhas emitidas chegam ao buffer em
   ordem de ``seq`` (monotônico desde 1), uma execução fresca limpa o buffer
   anterior (``seq`` recomeça em 1), e ``novas_desde`` devolve só o delta.
6. Idempotência (Req. 1.6): após sucesso + Saída persistida, uma segunda chamada
   **reaproveita** sem chamar ``provisionar`` de novo (``reaproveitado``), com o
   ``AmbienteProvisionado`` reconstruído do JSON persistido.
7. Consistência tela ↔ console (Req. 7.5, apoia 7.3): a ``mensagem_erro`` do
   ``StageRun`` é o ``texto`` da última linha de erro da etapa que falhou no
   buffer.

_Requisitos: 1.1, 1.6, 5.5, 6.4, 7.1, 7.3_
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable, Optional

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AmbienteProvisionadoRow, StageRun
from app.models.enums import StageState, TranslationSource
from app.services.environment.compose_executor import LinhaLog
from app.services.environment.log_buffer import ProvisioningLogBuffer
from app.services.environment.operacionalizacao import (
    HostAlvo,
    ParametrosDeAmbiente,
    PlataformaAmbiente,
)
from app.services.environment.provisionador import (
    AmbienteProvisionado,
    IsolationResumo,
    ProvisionadorDeAmbiente,
    VereditoContainer,
)
from app.services.environment.saude import ResumoSaude, VereditoSaude
from app.services.environment.stage_service import EnvironmentStageService
from app.services.stage.stage_service import (
    ESTAGIO_AMBIENTE,
    ESTAGIO_EXECUCAO,
    ESTAGIO_OPERACIONALIZACAO,
    StageService,
)

_CASO = "shadowray"
_C1 = "sticks-nginx-target"
_C2 = "sticks-mariadb-target"


# ---------------------------------------------------------------------------
# Fake ProvisionadorDeAmbiente — controlável, conta chamadas, emite LinhaLog.
# ---------------------------------------------------------------------------


class _FakeProvisionador:
    """Test double de :class:`ProvisionadorDeAmbiente` para o serviço do Estágio 5.

    Satisfaz estruturalmente o ``@runtime_checkable`` ``ProvisionadorDeAmbiente``
    (expõe ``origem``/``provisionar``/``desprovisionar``/``preparar``), sem jamais
    tocar o Docker, a rede ou a IA. Em ``provisionar``:

    * emite as :class:`LinhaLog` configuradas (``linhas``) por ``on_log``, na
      ordem — exatamente como o provisionador real empurra as linhas de cada
      etapa ao buffer/console (Req. 2.2); e
    * devolve o :class:`AmbienteProvisionado` configurado (``ambiente``) — pronto
      ou com ``etapa_falha`` —, sem repetir a lógica do provisionador real.

    Conta ``provisionar_calls`` para asserir que a idempotência (Req. 1.6) **não**
    reinvoca o provisionamento quando o ambiente já está pronto e persistido.
    """

    def __init__(
        self,
        ambiente: AmbienteProvisionado,
        *,
        linhas: Optional[list[LinhaLog]] = None,
    ) -> None:
        self._ambiente = ambiente
        self._linhas = list(linhas or [])
        self.provisionar_calls = 0

    def origem(self) -> TranslationSource:
        return TranslationSource.HUMAN_CURATION

    def provisionar(
        self,
        params: ParametrosDeAmbiente,
        *,
        on_log: Callable[[LinhaLog], None] = lambda _l: None,
    ) -> AmbienteProvisionado:
        self.provisionar_calls += 1
        for linha in self._linhas:
            on_log(linha)
        return self._ambiente

    def desprovisionar(self, ambiente, *, on_log=lambda _l: None):  # pragma: no cover
        raise AssertionError("desprovisionar não deve ser chamado neste teste")

    def preparar(self, caso: str):  # pragma: no cover
        raise AssertionError("preparar não deve ser chamado no provisionamento ativo")


# ---------------------------------------------------------------------------
# Builders de AmbienteProvisionado (Saída do Estágio 5).
# ---------------------------------------------------------------------------


def _ambiente_pronto(containers: list[str]) -> AmbienteProvisionado:
    """Uma Saída **pronta**: saúde + Abilities + isolamento aprovados (Req. 5.5)."""
    return AmbienteProvisionado(
        caso_id=_CASO,
        plataforma=PlataformaAmbiente.CONTAINER,
        containers_alvo=containers,
        compose_file="docker/docker-compose.yml",
        saude=ResumoSaude(
            aprovado=True,
            veredictos=[
                VereditoSaude(container=c, rodando=True, saudavel=True)
                for c in containers
            ],
        ),
        abilities_carregadas=True,
        isolamento=IsolationResumo(
            aprovado=True,
            veredictos=[
                VereditoContainer(container=c, isolado=True) for c in containers
            ],
        ),
        origem=TranslationSource.HUMAN_CURATION,
    )


def _ambiente_falho(
    containers: list[str],
    *,
    etapa_falha: str = "aguardar_saude",
    mensagem_erro: str = "o contêiner alvo não está saudável",
) -> AmbienteProvisionado:
    """Uma Saída com falha de etapa: o Estágio 5 não deve concluir (Req. 7.1)."""
    return AmbienteProvisionado(
        caso_id=_CASO,
        plataforma=PlataformaAmbiente.CONTAINER,
        containers_alvo=containers,
        compose_file="docker/docker-compose.yml",
        saude=ResumoSaude(
            aprovado=False,
            veredictos=[
                VereditoSaude(
                    container=containers[0],
                    rodando=True,
                    saudavel=False,
                    razao="healthcheck reportou status 'unhealthy'",
                )
            ],
        ),
        abilities_carregadas=False,
        isolamento=IsolationResumo(aprovado=False),
        origem=TranslationSource.HUMAN_CURATION,
        etapa_falha=etapa_falha,
        mensagem_erro=mensagem_erro,
    )


# ---------------------------------------------------------------------------
# Sessão efêmera (engine SQLite em arquivo temporário + esquema por teste).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Cria um engine SQLite efêmero + esquema completo e devolve ``(session, db_path, engine)``.

    O esquema é criado via ``Base.metadata.create_all`` (igual às fixtures de
    ``test_provisionamento_auto_property.py``), de modo que a tabela
    ``ambiente_provisionado`` (modelo da task 5.2) exista — a persistência da
    Saída e a idempotência (Req. 1.6) dependem dela.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline5_env_service_")
    db_path = Path(tmp_dir) / "env_service.db"
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


@pytest.fixture()
def session() -> "object":
    """Fornece uma ``Session`` SQLite efêmera, descartando o arquivo ao final."""
    sess, db_path, engine = _build_ephemeral_session()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()  # type: ignore[attr-defined]
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


def _seed_stage(
    sess: Session, caso_id: str, estagio: int, estado: StageState
) -> None:
    """Semeia uma linha ``StageRun`` para ``(caso_id, estagio)`` no estado dado."""
    sess.add(
        StageRun(
            caso_id=caso_id,
            estagio=estagio,
            estado=estado,
            progresso=100 if estado is StageState.COMPLETED else 0,
        )
    )
    sess.commit()


def _stage5_row(sess: Session, caso_id: str) -> Optional[StageRun]:
    """A linha ``StageRun`` de ``(caso_id, estagio=5)``, se houver."""
    return (
        sess.execute(
            select(StageRun)
            .where(StageRun.caso_id == caso_id)
            .where(StageRun.estagio == ESTAGIO_AMBIENTE)
            .order_by(StageRun.id.asc())
        )
        .scalars()
        .first()
    )


def _params() -> ParametrosDeAmbiente:
    """``ParametrosDeAmbiente`` com dois hosts resolvidos (Entrada do Estágio 5)."""
    return ParametrosDeAmbiente(
        caso_id=_CASO,
        hosts=[
            HostAlvo(rotulo="alvo-1", container=_C1),
            HostAlvo(rotulo="alvo-2", container=_C2),
        ],
    )


def _make_service(
    sess: Session,
    provisionador: ProvisionadorDeAmbiente,
    buffer: ProvisioningLogBuffer,
) -> EnvironmentStageService:
    """Monta o serviço com o provisionador/buffer injetados e um ``params_provider`` *lambda*."""
    return EnvironmentStageService(
        sess,
        provisionador=provisionador,
        log_buffer=buffer,
        params_provider=lambda _caso: _params(),
    )


# ===========================================================================
# 1. Provisionamento automático (Req. 1.1)
# ===========================================================================


def test_provisionamento_automatico_conclui_estagio5(session: Session) -> None:
    """Estágio 4 concluído + ambiente pronto ⇒ pipeline roda sem ação manual e conclui.

    O provisionamento é disparado por ``provisionar_estagio5`` sem nenhuma ação
    manual de "provisionar" (Req. 1.1): o fake provisionador roda o pipeline e
    devolve um ambiente pronto, o serviço conclui o Estágio 5 (``StageRun``
    estagio=5 → ``COMPLETED``/100) e devolve ``pronto=True``.

    _Requisitos: 1.1_
    """
    containers = [_C1, _C2]
    _seed_stage(session, _CASO, ESTAGIO_OPERACIONALIZACAO, StageState.COMPLETED)
    prov = _FakeProvisionador(_ambiente_pronto(containers))
    service = _make_service(session, prov, ProvisioningLogBuffer())

    resultado = service.provisionar_estagio5(_CASO)

    # O pipeline rodou de fato (sem ação manual) e concluiu.
    assert prov.provisionar_calls == 1
    assert resultado.bloqueado_pelo_gate is False
    assert resultado.pronto is True
    assert resultado.reaproveitado is False
    assert resultado.etapa_falha is None
    assert resultado.mensagem_erro is None
    assert resultado.ambiente is not None
    assert resultado.ambiente.containers_alvo == containers

    # O StageRun do Estágio 5 foi concluído em 100.
    row = _stage5_row(session, _CASO)
    assert row is not None
    assert row.estado is StageState.COMPLETED
    assert row.progresso == 100
    assert row.etapa_falha is None
    assert row.mensagem_erro is None


# ===========================================================================
# 2. Gate — Estágio 4 não concluído (Req. 1.1)
# ===========================================================================


def test_gate_bloqueia_sem_estagio4_concluido(session: Session) -> None:
    """Estágio 4 não concluído ⇒ ``bloqueado_pelo_gate``, provisionar NÃO chamado.

    O gate sequencial impede o provisionamento: o fake ``provisionar`` nunca é
    chamado e nenhuma linha ``estagio=5`` é concluída.

    _Requisitos: 1.1_
    """
    containers = [_C1, _C2]
    # Estágio 4 em andamento (não concluído) + Estágio 5 ainda não iniciado.
    _seed_stage(session, _CASO, ESTAGIO_OPERACIONALIZACAO, StageState.IN_PROGRESS)
    prov = _FakeProvisionador(_ambiente_pronto(containers))
    service = _make_service(session, prov, ProvisioningLogBuffer())

    resultado = service.provisionar_estagio5(_CASO)

    assert resultado.bloqueado_pelo_gate is True
    assert resultado.pronto is False
    assert prov.provisionar_calls == 0
    # Nenhuma linha estagio=5 concluída.
    row = _stage5_row(session, _CASO)
    assert row is None or row.estado is not StageState.COMPLETED


# ===========================================================================
# 3. Conclusão só com os três pré-requisitos (Req. 5.5)
# ===========================================================================


def test_conclui_somente_com_tres_prerequisitos(session: Session) -> None:
    """Ambiente com ``etapa_falha`` ⇒ Estágio 5 NÃO conclui (``ERROR``).

    Quando o pipeline retorna um ambiente **não pronto** com ``etapa_falha``
    (p.ex. ``aguardar_saude``), o Estágio 5 não é concluído: o ``StageRun``
    estagio=5 fica em ``ERROR`` com ``etapa_falha`` + ``mensagem_erro``.

    _Requisitos: 5.5_
    """
    containers = [_C1, _C2]
    _seed_stage(session, _CASO, ESTAGIO_OPERACIONALIZACAO, StageState.COMPLETED)
    ambiente = _ambiente_falho(
        containers, etapa_falha="aguardar_saude", mensagem_erro="saúde reprovada"
    )
    prov = _FakeProvisionador(ambiente)
    service = _make_service(session, prov, ProvisioningLogBuffer())

    resultado = service.provisionar_estagio5(_CASO)

    assert resultado.pronto is False
    assert resultado.etapa_falha == "aguardar_saude"
    assert resultado.mensagem_erro is not None and resultado.mensagem_erro != ""

    row = _stage5_row(session, _CASO)
    assert row is not None
    assert row.estado is StageState.ERROR
    assert row.estado is not StageState.COMPLETED
    assert row.etapa_falha == "aguardar_saude"
    assert row.mensagem_erro is not None


# ===========================================================================
# 4. Falha bloqueia o Estágio 6 (Req. 6.4)
# ===========================================================================


def test_falha_bloqueia_estagio6(session: Session) -> None:
    """Após falha o Estágio 6 não é iniciável; após sucesso ele é liberado.

    O gate sequencial de :meth:`StageService.is_stage_startable` depende apenas do
    estado do Estágio 5: enquanto o Estágio 5 não estiver ``COMPLETED``, o Estágio
    6 segue bloqueado (Req. 6.4); concluído, ele é liberado.

    _Requisitos: 6.4_
    """
    containers = [_C1, _C2]
    _seed_stage(session, _CASO, ESTAGIO_OPERACIONALIZACAO, StageState.COMPLETED)
    stage_service = StageService(session)

    # Falha ⇒ Estágio 5 em ERROR ⇒ Estágio 6 bloqueado.
    prov_falha = _FakeProvisionador(_ambiente_falho(containers))
    service_falha = _make_service(session, prov_falha, ProvisioningLogBuffer())
    resultado_falha = service_falha.provisionar_estagio5(_CASO)
    assert resultado_falha.pronto is False
    assert stage_service.is_stage_startable(_CASO, ESTAGIO_EXECUCAO) is False

    # Sucesso ⇒ Estágio 5 COMPLETED ⇒ Estágio 6 liberado.
    prov_ok = _FakeProvisionador(_ambiente_pronto(containers))
    service_ok = _make_service(session, prov_ok, ProvisioningLogBuffer())
    resultado_ok = service_ok.provisionar_estagio5(_CASO)
    assert resultado_ok.pronto is True
    assert stage_service.is_stage_startable(_CASO, ESTAGIO_EXECUCAO) is True


# ===========================================================================
# 5. Buffer e recuperação (Req. 7.1, 7.3)
# ===========================================================================


def test_buffer_recebe_linhas_em_ordem_e_limpa_entre_execucoes(
    session: Session,
) -> None:
    """As linhas chegam ao buffer em ordem de ``seq`` (desde 1); uma execução fresca limpa.

    O fake provisionador emite várias :class:`LinhaLog` via ``on_log``; o serviço
    as encaminha ao :class:`ProvisioningLogBuffer` injetado. ``ler(caso)`` as
    devolve em ordem de ``seq`` monotônico começando em 1. Uma segunda execução
    (fresca) limpa o buffer: as linhas da execução anterior **não** vazam e o
    ``seq`` recomeça em 1. ``novas_desde(caso, k)`` devolve só o delta (``seq>k``).

    _Requisitos: 7.1, 7.3_
    """
    containers = [_C1, _C2]
    _seed_stage(session, _CASO, ESTAGIO_OPERACIONALIZACAO, StageState.COMPLETED)
    buffer = ProvisioningLogBuffer()

    linhas_run1 = [
        LinhaLog(etapa="subir_conteineres", fluxo="comando", texto="docker compose up"),
        LinhaLog(etapa="subir_conteineres", fluxo="stdout", texto="Container criado"),
        LinhaLog(etapa="aguardar_saude", fluxo="info", texto="verificando saúde"),
        LinhaLog(etapa="verificar_isolamento", fluxo="info", texto="isolamento ok"),
    ]
    # Primeira execução: falha (não concluída) para permitir uma segunda execução
    # fresca (uma vez concluída, a idempotência reaproveitaria sem rodar o pipeline).
    prov1 = _FakeProvisionador(
        _ambiente_falho(containers, etapa_falha="verificar_isolamento"),
        linhas=linhas_run1,
    )
    service1 = _make_service(session, prov1, buffer)
    service1.provisionar_estagio5(_CASO)

    bufferizadas = buffer.ler(_CASO)
    # Todas as linhas emitidas chegaram ao buffer, em ordem, seq monotônico desde 1.
    assert [l.texto for l in bufferizadas] == [l.texto for l in linhas_run1]
    assert [l.seq for l in bufferizadas] == [1, 2, 3, 4]
    # Ordem de seq estritamente crescente (monotônico).
    seqs = [l.seq for l in bufferizadas]
    assert seqs == sorted(seqs)

    # novas_desde devolve apenas o delta (seq > k).
    assert [l.seq for l in buffer.novas_desde(_CASO, 2)] == [3, 4]
    assert buffer.novas_desde(_CASO, 4) == []

    # Segunda execução fresca: o buffer é limpo, seq recomeça em 1, e as linhas
    # da execução anterior não vazam.
    linhas_run2 = [
        LinhaLog(etapa="subir_conteineres", fluxo="stdout", texto="nova execução"),
        LinhaLog(etapa="aguardar_saude", fluxo="stdout", texto="saudável"),
    ]
    prov2 = _FakeProvisionador(_ambiente_pronto(containers), linhas=linhas_run2)
    service2 = _make_service(session, prov2, buffer)
    service2.provisionar_estagio5(_CASO)

    bufferizadas2 = buffer.ler(_CASO)
    assert [l.texto for l in bufferizadas2] == ["nova execução", "saudável"]
    assert [l.seq for l in bufferizadas2] == [1, 2]  # seq reiniciou em 1
    # Nenhuma linha da primeira execução sobreviveu.
    textos2 = {l.texto for l in bufferizadas2}
    assert textos2.isdisjoint({l.texto for l in linhas_run1})


# ===========================================================================
# 6. Idempotência (Req. 1.6)
# ===========================================================================


def test_idempotencia_reaproveita_sem_rodar_pipeline(session: Session) -> None:
    """Após sucesso + Saída persistida, a 2ª chamada reaproveita sem chamar provisionar.

    A primeira chamada conclui o Estágio 5 e persiste a Saída pronta
    (``AmbienteProvisionadoRow``). A segunda chamada **reaproveita**: o fake
    ``provisionar`` NÃO é chamado de novo (``provisionar_calls`` permanece 1),
    ``reaproveitado=True``, ``pronto=True`` e o ``AmbienteProvisionado`` é
    reconstruído do JSON persistido.

    _Requisitos: 1.6_
    """
    containers = [_C1, _C2]
    _seed_stage(session, _CASO, ESTAGIO_OPERACIONALIZACAO, StageState.COMPLETED)
    prov = _FakeProvisionador(_ambiente_pronto(containers))
    service = _make_service(session, prov, ProvisioningLogBuffer())

    # 1ª chamada: roda o pipeline, conclui e persiste a Saída.
    primeiro = service.provisionar_estagio5(_CASO)
    assert primeiro.pronto is True
    assert primeiro.reaproveitado is False
    assert prov.provisionar_calls == 1
    # A Saída foi persistida como linha pronta.
    row = (
        session.execute(
            select(AmbienteProvisionadoRow).where(
                AmbienteProvisionadoRow.caso_id == _CASO
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    assert row.pronto is True

    # 2ª chamada: reaproveita sem rodar o pipeline de novo.
    segundo = service.provisionar_estagio5(_CASO)
    assert segundo.reaproveitado is True
    assert segundo.pronto is True
    assert prov.provisionar_calls == 1  # provisionar NÃO foi chamado de novo
    # O AmbienteProvisionado foi reconstruído do JSON persistido (igual ao original).
    assert segundo.ambiente is not None
    assert segundo.ambiente.containers_alvo == containers
    assert segundo.ambiente.pronto is True
    assert segundo.ambiente.origem is TranslationSource.HUMAN_CURATION


# ===========================================================================
# 7. Consistência mensagem_erro ↔ console (Req. 7.5, apoia 7.3)
# ===========================================================================


def test_mensagem_erro_derivada_da_ultima_linha_de_erro_do_console(
    session: Session,
) -> None:
    """A ``mensagem_erro`` do ``StageRun`` é o texto da última linha de erro da etapa.

    Quando uma etapa falha, o serviço deriva a ``mensagem_erro`` da **última**
    linha ``stderr``/``erro`` que a etapa que falhou deixou no buffer — o mesmo
    texto que o terminal ao vivo/console mostra —, garantindo por construção a
    consistência tela ↔ console.

    _Requisitos: 7.3_
    """
    containers = [_C1, _C2]
    _seed_stage(session, _CASO, ESTAGIO_OPERACIONALIZACAO, StageState.COMPLETED)
    buffer = ProvisioningLogBuffer()

    etapa = "aguardar_saude"
    ultima_linha_erro = "healthcheck reportou status 'unhealthy' para sticks-nginx-target"
    linhas = [
        LinhaLog(etapa="subir_conteineres", fluxo="stdout", texto="Container up"),
        LinhaLog(etapa=etapa, fluxo="info", texto="aguardando saúde..."),
        LinhaLog(etapa=etapa, fluxo="stderr", texto="primeiro erro intermediário"),
        LinhaLog(etapa=etapa, fluxo="stderr", texto=ultima_linha_erro),
        # Uma linha de erro de OUTRA etapa não deve ser escolhida.
        LinhaLog(etapa="subir_conteineres", fluxo="stderr", texto="erro de outra etapa"),
    ]
    ambiente = _ambiente_falho(
        containers, etapa_falha=etapa, mensagem_erro="fallback do provisionador"
    )
    prov = _FakeProvisionador(ambiente, linhas=linhas)
    service = _make_service(session, prov, buffer)

    resultado = service.provisionar_estagio5(_CASO)

    # A mensagem_erro é exatamente o texto da última linha de erro DA ETAPA que falhou.
    assert resultado.mensagem_erro == ultima_linha_erro
    row = _stage5_row(session, _CASO)
    assert row is not None
    assert row.mensagem_erro == ultima_linha_erro

    # E essa linha está, de fato, no buffer que o console renderiza.
    textos_buffer = [l.texto for l in buffer.ler(_CASO)]
    assert ultima_linha_erro in textos_buffer
