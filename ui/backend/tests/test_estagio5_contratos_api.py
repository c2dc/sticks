"""Contratos JSON (pt-BR) da API REST do Estágio 5 — Criação do ambiente (task 6.3).

Cobre o **shape do payload**, a **idempotência** e os **logs no retorno** dos três
endpoints REST do Estágio 5 adicionados/evoluídos nas tasks 6.1/6.2 em
``app/api/casos.py``:

* ``GET  /api/casos/{caso}/estagio/5`` → :class:`Stage4ProvisionamentoResponse`
  agora inclui ``saude`` (:class:`~app.services.environment.saude.ResumoSaude`) e
  ``logs`` (lista de :class:`LinhaLogView`) além de topologia/isolamento/estados
  (task 6.1). Read-only — abrir a tela nunca provisiona (Req. 6.1).
* ``POST /api/casos/{caso}/estagio/5/provisionar`` → :class:`ProvisionarEstagio5Response`
  delega à costura **idempotente** ``EnvironmentStageService.provisionar_estagio5``
  (task 6.2): ``ambiente`` (Optional :class:`AmbienteProvisionado`), ``concluido``,
  ``motivo`` e ``estados`` (Req. 1.1, 1.6).
* ``GET  /api/casos/{caso}/estagio/5/logs`` → :class:`Estagio5LogsResponse` com
  ``caso_id`` e ``logs`` (buffer corrente — Req. 2.5).

Reusa o **mesmo molde** de ``test_estagio5_provisionar_endpoints.py`` (SQLite
efêmero por módulo via ``get_db``; provisionador de curadoria com inspector de
isolamento + Caldera *fakes* via ``get_provisionador``; compose/estado *fakes*
para o pipeline rodar sem subprocess nem daemon Docker). Importa os colaboradores
*fakes* e a fábrica de sessão daquele arquivo para não duplicá-los.

Buffer de logs determinístico
-----------------------------
Os endpoints agora leem o buffer compartilhado a nível de processo via
``get_log_buffer()``. Para asserções determinísticas sem vazamento entre testes,
cada :class:`TestClient` **sobrescreve** ``get_log_buffer`` com um
:class:`ProvisioningLogBuffer` **novo por teste** — assim o ``seq`` recomeça em 1
e as linhas de um teste não contaminam o outro. O ``POST .../provisionar`` e os
dois ``GET`` resolvem a **mesma** instância sobrescrita (compartilhada dentro do
cliente), de modo que as linhas emitidas durante o POST sejam recuperáveis pelos
GETs no mesmo teste.

Logs no retorno
---------------
Os *fakes* de compose do arquivo-base emitem **zero** linhas. Para exercitar
"logs no retorno" com conteúdo meaningful, este arquivo usa um
:class:`_FakeComposeComLogs` que emite algumas :class:`LinhaLog` por ``on_log`` na
etapa ``subir_conteineres`` — exatamente como o executor real streama a saída do
``docker compose`` —, de modo que o buffer tenha linhas pt-BR com ``seq``
monotônico a partir de 1 para assertar.

_Requisitos: 1.6, 2.5, 6.1_
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.casos import get_provisionador
from app.db.session import get_db
from app.main import create_app
from app.services.environment.compose_executor import LinhaLog, ResultadoComando
from app.services.environment.log_buffer import (
    ProvisioningLogBuffer,
    get_log_buffer,
)
from app.services.environment.provisionador import CuradoriaProvisionadorDeAmbiente

# Reusa o molde do arquivo de endpoints (SQLite efêmero + colaboradores fakes):
# não duplicamos a fixture de sessão nem os inspectors/Caldera/estado fakes.
from tests.test_estagio5_provisionar_endpoints import (
    _FakeCaldera,
    _HealthyStateInspector,
    _IsolatedInspector,
    _NotIsolatedInspector,
    session_factory,  # noqa: F401 — fixture reusada (module-scoped SQLite)
)

_SLUG = "shadowray"  # Caso curado real (11 Abilities), Estágio 4 derivável puro.


# ---------------------------------------------------------------------------
# Fake de compose que EMITE linhas de log (os fakes do arquivo-base emitem zero).
# ---------------------------------------------------------------------------


class _FakeComposeComLogs:
    """``ComposeExecutor`` *fake* que emite algumas :class:`LinhaLog` em ``subir``.

    O :class:`_FakeComposeExecutor` do arquivo-base devolve
    ``ResultadoComando(codigo=0, linhas=[])`` **sem** chamar ``on_log`` — nenhuma
    linha chega ao buffer. Para assertar "logs no retorno" (Req. 2.5) com conteúdo
    pt-BR determinístico, este *fake* streama algumas linhas pela etapa
    ``subir_conteineres`` (como o executor real streama a saída do ``docker
    compose``), de modo que o buffer compartilhado tenha linhas com ``seq``
    monotônico a partir de 1 para o teste assertar. ``codigo=0`` deixa o pipeline
    avançar para a saúde → Abilities → isolamento.
    """

    LINHAS: tuple[tuple[str, str], ...] = (
        ("comando", "docker compose -f docker/docker-compose.yml up -d --wait"),
        ("stdout", "Subindo os contêineres do laboratório de ataque"),
        ("stdout", "Contêineres do laboratório prontos"),
    )

    def subir(self, compose_file: str, *, on_log=None) -> ResultadoComando:
        linhas: list[LinhaLog] = []
        for fluxo, texto in self.LINHAS:
            linha = LinhaLog(etapa="subir_conteineres", fluxo=fluxo, texto=texto)
            linhas.append(linha)
            if on_log is not None:
                on_log(linha)
        return ResultadoComando(codigo=0, linhas=linhas)

    def derrubar(self, compose_file: str, *, on_log=None) -> ResultadoComando:
        return ResultadoComando(codigo=0, linhas=[])


class _ProvisionadorContandoChamadas(CuradoriaProvisionadorDeAmbiente):
    """Provisionador de curadoria que **conta** as chamadas a ``provisionar``.

    Serve à asserção de idempotência (Req. 1.6): com o ambiente já provisionado e
    o Estágio 5 ``concluido``, a costura ``provisionar_estagio5`` **reaproveita** a
    Saída persistida e **não** chama ``provisionar`` de novo. Um segundo POST deve
    deixar o contador em 1. Delega todo o comportamento à superclasse (pipeline
    real sobre os colaboradores *fakes*), apenas incrementando o contador.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.chamadas_provisionar = 0

    def provisionar(self, params, *, on_log=lambda _l: None):  # type: ignore[override]
        self.chamadas_provisionar += 1
        return super().provisionar(params, on_log=on_log)


# ---------------------------------------------------------------------------
# Harness: TestClient com DB + provisionador + buffer de logs sobrescritos.
# ---------------------------------------------------------------------------


def _make_client(
    session_factory: sessionmaker[Session],  # noqa: F811 — fixture reusada
    *,
    inspector: object,
    caldera: object,
    provisionador: CuradoriaProvisionadorDeAmbiente | None = None,
    buffer: ProvisioningLogBuffer | None = None,
) -> TestClient:
    """Monta um :class:`TestClient` com DB + provisionador + buffer sobrescritos.

    Reusa a mesma mecânica do arquivo-base (``get_db`` → SQLite efêmero;
    ``get_provisionador`` → curadoria com isolamento/Caldera *fakes* + compose/
    estado *fakes*), mas:

    * usa o :class:`_FakeComposeComLogs` (emite linhas) no lugar do compose mudo;
    * **sobrescreve** ``get_log_buffer`` com um :class:`ProvisioningLogBuffer`
      **novo por teste**, de modo que o ``seq`` recomece em 1 e as linhas não
      vazem entre testes — e o POST e os dois GET compartilhem a mesma instância.
    """
    app = create_app()
    log_buffer = buffer if buffer is not None else ProvisioningLogBuffer()

    def _get_db_override():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def _get_provisionador_override() -> CuradoriaProvisionadorDeAmbiente:
        if provisionador is not None:
            return provisionador
        return CuradoriaProvisionadorDeAmbiente(
            inspector=inspector,  # type: ignore[arg-type]
            caldera=caldera,  # type: ignore[arg-type]
            compose_executor=_FakeComposeComLogs(),  # type: ignore[arg-type]
            state_inspector=_HealthyStateInspector(),  # type: ignore[arg-type]
        )

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_provisionador] = _get_provisionador_override
    app.dependency_overrides[get_log_buffer] = lambda: log_buffer
    return TestClient(app)


def _novo_provisionador_fakes(
    *, inspector: object, caldera: object
) -> _ProvisionadorContandoChamadas:
    """Constrói o provisionador contador em torno dos colaboradores *fakes*."""
    return _ProvisionadorContandoChamadas(
        inspector=inspector,  # type: ignore[arg-type]
        caldera=caldera,  # type: ignore[arg-type]
        compose_executor=_FakeComposeComLogs(),  # type: ignore[arg-type]
        state_inspector=_HealthyStateInspector(),  # type: ignore[arg-type]
    )


# ===========================================================================
# (1) GET /estagio/5 — shape do payload: saude + logs + topologia/isolamento/estados
# ===========================================================================


def test_get_estagio5_shape_inclui_saude_logs_e_topologia(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """GET /estagio/5: payload inclui ``saude`` (aprovado/veredictos) e ``logs`` (lista).

    O contrato pt-BR da task 6.1 acrescenta ``saude`` (:class:`ResumoSaude` com
    ``aprovado``/``veredictos``) e ``logs`` (lista de :class:`LinhaLogView`) ao
    payload já existente (topologia/isolamento/estados). A leitura é **read-only**:
    sem um provisionamento anterior, ``saude`` vem vazio (não aprovado) e ``logs``
    é uma lista vazia — abrir a tela nunca sobe contêineres (Req. 6.1).
    _Requisitos: 6.1, 2.5_"""
    # Slug curado próprio e nunca provisionado por outro teste, de modo que a
    # leitura read-only observe o estado inicial (saúde vazia + logs vazios) no
    # SQLite module-scoped — independente da ordem dos testes.
    slug = "outer_space"
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    resp = client.get(f"/api/casos/{slug}/estagio/5")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Campos pré-existentes seguem presentes (topologia + isolamento + estados).
    assert body["caso_id"] == slug
    assert "isolado" in body
    assert isinstance(body["hosts"], list)
    assert isinstance(body["redes"], list)
    assert "mermaid" in body
    numeros = [e["estagio"] for e in body["estados"]["estagios"]]
    assert numeros == [1, 2, 3, 4, 5, 6]

    # DELTA (task 6.1): ``saude`` com o shape ResumoSaude e ``logs`` como lista.
    assert "saude" in body
    saude = body["saude"]
    assert set(("aprovado", "veredictos")) <= set(saude.keys())
    assert isinstance(saude["aprovado"], bool)
    assert isinstance(saude["veredictos"], list)
    assert isinstance(body["logs"], list)

    # Read-only: sem provisionamento anterior, saúde não aprovada e logs vazios.
    assert saude["aprovado"] is False
    assert body["logs"] == []


# ===========================================================================
# (2) POST /estagio/5/provisionar — shape do payload (caminho feliz, conclui)
# ===========================================================================


def test_post_provisionar_shape_caminho_feliz(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """POST /provisionar: ``ambiente``/``concluido``/``motivo``/``estados`` no shape.

    Caminho feliz (isolado + Abilities carregadas + saudável) → ``concluido=True``,
    ``motivo=None`` e o Estágio 5 ``concluido`` nos estados. O ``ambiente`` carrega
    o :class:`AmbienteProvisionado` completo — ``containers_alvo``, ``isolamento``,
    ``saude``, ``abilities_carregadas``, ``compose_file`` e ``origem``.
    _Requisitos: 1.1, 6.1_"""
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    resp = client.post(f"/api/casos/{_SLUG}/estagio/5/provisionar")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Shape do envelope: as quatro chaves do contrato existem.
    assert set(("ambiente", "concluido", "motivo", "estados")) <= set(body.keys())

    ambiente = body["ambiente"]
    assert ambiente is not None
    assert ambiente["caso_id"] == _SLUG
    assert ambiente["plataforma"] == "conteiner"
    assert ambiente["containers_alvo"], "contêineres-alvo devem ser resolvidos"
    assert ambiente["isolamento"]["aprovado"] is True
    assert ambiente["saude"]["aprovado"] is True
    assert ambiente["abilities_carregadas"] is True
    assert ambiente["compose_file"]
    assert ambiente["origem"] == "curadoria_humana"

    # Caminho feliz: concluído, sem motivo de bloqueio.
    assert body["concluido"] is True
    assert body["motivo"] is None

    # O estado por Estágio reflete a conclusão do Estágio 5.
    estagio5 = next(e for e in body["estados"]["estagios"] if e["estagio"] == 5)
    assert estagio5["estado"] == "concluido"


# ===========================================================================
# (3) Idempotência (Req. 1.6): POST duas vezes → reaproveita (provisiona 1x só)
# ===========================================================================


def test_post_provisionar_idempotente_reaproveita_ambiente(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """POST duas vezes: o segundo reaproveita (não re-provisiona); ambiente idêntico.

    Com um provisionador contador injetado, o primeiro POST conclui o Estágio 5 e
    o segundo **reaproveita** a Saída persistida — ``provisionar`` é chamado uma
    única vez nos dois POSTs (Req. 1.6). Ambos devolvem ``concluido=True`` e o
    **mesmo** ambiente (mesmos ``containers_alvo``).
    _Requisitos: 1.6_"""
    # Slug curado próprio: o primeiro POST deve **rodar** o pipeline (não reusar
    # estado de outro teste no SQLite module-scoped), para que o segundo exerça o
    # reaproveitamento com o contador em 1.
    slug = "apt41_dust"
    provisionador = _novo_provisionador_fakes(
        inspector=_IsolatedInspector(), caldera=_FakeCaldera()
    )
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
        provisionador=provisionador,
    )

    primeiro = client.post(f"/api/casos/{slug}/estagio/5/provisionar")
    assert primeiro.status_code == 200, primeiro.text
    b1 = primeiro.json()
    assert b1["concluido"] is True

    segundo = client.post(f"/api/casos/{slug}/estagio/5/provisionar")
    assert segundo.status_code == 200, segundo.text
    b2 = segundo.json()
    assert b2["concluido"] is True

    # Reaproveitamento: o provisionamento real rodou uma única vez nos dois POSTs.
    assert provisionador.chamadas_provisionar == 1

    # O ambiente devolvido é o mesmo (mesmos contêineres-alvo).
    assert b2["ambiente"] is not None
    assert b2["ambiente"]["containers_alvo"] == b1["ambiente"]["containers_alvo"]


# ===========================================================================
# (4) Logs no retorno (Req. 2.5): POST emite linhas → GET /logs e GET /estagio/5
# ===========================================================================


def test_logs_no_retorno_apos_provisionar(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """Após um POST que emite linhas, GET /logs e GET /estagio/5 carregam os logs.

    O ``_FakeComposeComLogs`` emite linhas na etapa ``subir_conteineres``; o buffer
    compartilhado (sobrescrito por teste) as acumula. O ``GET .../estagio/5/logs``
    devolve ``logs`` como lista **não vazia** com o shape pt-BR de
    :class:`LinhaLogView` — ``seq`` monotônico a partir de 1, ``etapa``, ``fluxo``,
    ``texto``, ``em`` — e o ``GET .../estagio/5`` também carrega essas linhas.
    _Requisitos: 2.5_"""
    # Slug curado próprio: o POST deve **rodar** o pipeline (emitindo linhas), e
    # não reusar o estado persistido de outro teste no SQLite module-scoped.
    slug = "c0010"
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    prov = client.post(f"/api/casos/{slug}/estagio/5/provisionar")
    assert prov.status_code == 200, prov.text

    # GET /logs: envelope + lista não vazia com o shape LinhaLogView.
    logs_resp = client.get(f"/api/casos/{slug}/estagio/5/logs")
    assert logs_resp.status_code == 200, logs_resp.text
    logs_body = logs_resp.json()
    assert logs_body["caso_id"] == slug
    linhas = logs_body["logs"]
    assert isinstance(linhas, list) and linhas, "o buffer deve ter linhas"

    chaves = set(("seq", "etapa", "fluxo", "texto", "em"))
    for linha in linhas:
        assert chaves <= set(linha.keys())

    # ``seq`` monotônico a partir de 1 (o serviço limpa o buffer antes de rodar).
    seqs = [linha["seq"] for linha in linhas]
    assert seqs[0] == 1
    assert seqs == sorted(seqs)
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))

    # As linhas de subir_conteineres emitidas pelo fake estão presentes.
    etapas = {linha["etapa"] for linha in linhas}
    assert "subir_conteineres" in etapas
    textos = [linha["texto"] for linha in linhas]
    assert any("laboratório" in t for t in textos)

    # GET /estagio/5 também carrega as mesmas linhas (mesmo buffer compartilhado).
    estagio_resp = client.get(f"/api/casos/{slug}/estagio/5")
    assert estagio_resp.status_code == 200, estagio_resp.text
    estagio_logs = estagio_resp.json()["logs"]
    assert isinstance(estagio_logs, list) and estagio_logs
    assert [l["seq"] for l in estagio_logs] == seqs


def test_get_logs_slug_desconhecido_404(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """Slug desconhecido no endpoint de logs é 404 (convenção de erro de casos.py).
    _Requisitos: 2.5_"""
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    resp = client.get("/api/casos/nao-existe/estagio/5/logs")
    assert resp.status_code == 404, resp.text


# ===========================================================================
# (5) Caminho de falha: não isolado → não conclui, motivo pt-BR, estágio 5 ≠ concluído
# ===========================================================================


def test_post_provisionar_nao_isolado_nao_conclui_com_motivo(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """Não isolado → ``concluido=False``, ``motivo`` pt-BR, Estágio 5 ≠ concluído.

    Com um inspector não isolado, o pipeline reprova na etapa de isolamento: o
    ambiente ainda é devolvido para exibição (``isolamento.aprovado=False`` com
    o(s) contêiner(es) ofensor(es)), mas o Estágio 5 **não** conclui e o
    ``motivo`` traz a causa pt-BR surfaçando o contêiner não isolado. O estado do
    Estágio 5 permanece diferente de ``concluido`` (Estágio 6 bloqueado).
    _Requisitos: 6.1_"""
    # Slug curado distinto: este teste nunca conclui o Estágio 5, isolando-o do
    # estado persistido do caminho feliz no SQLite module-scoped.
    slug = "costaricto"
    client = _make_client(
        session_factory,
        inspector=_NotIsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    resp = client.post(f"/api/casos/{slug}/estagio/5/provisionar")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    ambiente = body["ambiente"]
    assert ambiente is not None
    assert ambiente["isolamento"]["aprovado"] is False
    nao_isolados = [v for v in ambiente["isolamento"]["veredictos"] if not v["isolado"]]
    assert nao_isolados, "ao menos um contêiner deve ser sinalizado não isolado"

    assert body["concluido"] is False
    assert body["motivo"], "uma razão pt-BR de não isolamento deve ser reportada"
    ofensor = nao_isolados[0]["container"]
    assert ofensor in body["motivo"]

    estagio5 = next(e for e in body["estados"]["estagios"] if e["estagio"] == 5)
    assert estagio5["estado"] != "concluido"
