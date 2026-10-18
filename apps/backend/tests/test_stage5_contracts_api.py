"""Contratos JSON (pt-BR) da API REST do Estágio 5 — Criação do environment (task 6.3).

Cobre o **shape do payload**, a **idempotência** e os **logs no retorno** dos três
endpoints REST do Estágio 5 adicionados/evoluídos nas tasks 6.1/6.2 em
``app/api/campaigns.py``:

* ``GET  /api/campaigns/{campaign}/stage/5`` → :class:`Stage4ProvisioningResponse`
  agora inclui ``health`` (:class:`~app.services.environment.health.HealthSummary`) e
  ``logs`` (lista de :class:`LogLineView`) além de topology/isolation/states
  (task 6.1). Read-only — abrir a tela nunca provisiona (Req. 6.1).
* ``POST /api/campaigns/{campaign}/stage/5/provision`` → :class:`ProvisionStage5Response`
  delega à costura **idempotente** ``EnvironmentStageService.provision_stage5``
  (task 6.2): ``environment`` (Optional :class:`ProvisionedEnvironment`), ``completed``,
  ``motivo`` e ``states`` (Req. 1.1, 1.6).
* ``GET  /api/campaigns/{campaign}/stage/5/logs`` → :class:`Stage5LogsResponse` com
  ``campaign_id`` e ``logs`` (buffer corrente — Req. 2.5).

Reusa o **mesmo molde** de ``test_estagio5_provisionar_endpoints.py`` (SQLite
efêmero por módulo via ``get_db``; provisioner de curation com inspector de
isolation + Caldera *fakes* via ``get_provisioner``; compose/state *fakes*
para o pipeline rodar sem subprocess nem daemon Docker). Importa os colaboradores
*fakes* e a fábrica de sessão daquele file para não duplicá-los.

Buffer de logs determinístico
-----------------------------
Os endpoints agora leem o buffer compartilhado a nível de processo via
``get_log_buffer()``. Para asserções determinísticas sem vazamento entre testes,
cada :class:`TestClient` **sobrescreve** ``get_log_buffer`` com um
:class:`ProvisioningLogBuffer` **novo por teste** — assim o ``seq`` recomeça em 1
e as lines de um teste não contaminam o outro. O ``POST .../provision`` e os
dois ``GET`` resolvem a **mesma** instância sobrescrita (compartilhada dentro do
cliente), de modo que as lines emitidas durante o POST sejam recuperáveis pelos
GETs no mesmo teste.

Logs no retorno
---------------
Os *fakes* de compose do file-base emitem **zero** lines. Para exercitar
"logs no retorno" com conteúdo meaningful, este file usa um
:class:`_FakeComposeComLogs` que emite algumas :class:`LogLine` por ``on_log`` na
step ``subir_conteineres`` — exatamente como o executor real streama a saída do
``docker compose`` —, de modo que o buffer tenha lines pt-BR com ``seq``
monotônico a partir de 1 para assertar.

_Requirements: 1.6, 2.5, 6.1_
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.campaigns import get_provisioner
from app.db.session import get_db
from app.main import create_app
from app.services.environment.compose_executor import LogLine, CommandResult
from app.services.environment.log_buffer import (
    ProvisioningLogBuffer,
    get_log_buffer,
)
from app.services.environment.provisioner import CurationEnvironmentProvisioner

# Reusa o molde do file de endpoints (SQLite efêmero + colaboradores fakes):
# não duplicamos a fixture de sessão nem os inspectors/Caldera/state fakes.
from tests.test_estagio5_provisionar_endpoints import (
    _FakeCaldera,
    _HealthyStateInspector,
    _IsolatedInspector,
    _NotIsolatedInspector,
    session_factory,  # noqa: F401 — fixture reusada (module-scoped SQLite)
)

_SLUG = "shadowray"  # campaign curated real (11 Abilities), Estágio 4 derivável puro.


# ---------------------------------------------------------------------------
# Fake de compose que EMITE lines de log (os fakes do file-base emitem zero).
# ---------------------------------------------------------------------------


class _FakeComposeComLogs:
    """``ComposeExecutor`` *fake* que emite algumas :class:`LogLine` em ``subir``.

    O :class:`_FakeComposeExecutor` do file-base devolve
    ``CommandResult(code=0, lines=[])`` **sem** chamar ``on_log`` — nenhuma
    line chega ao buffer. Para assertar "logs no retorno" (Req. 2.5) com conteúdo
    pt-BR determinístico, este *fake* streama algumas lines pela step
    ``subir_conteineres`` (como o executor real streama a saída do ``docker
    compose``), de modo que o buffer compartilhado tenha lines com ``seq``
    monotônico a partir de 1 para o teste assertar. ``code=0`` deixa o pipeline
    avançar para a saúde → Abilities → isolation.
    """

    LINHAS: tuple[tuple[str, str], ...] = (
        ("command", "docker compose -f docker/docker-compose.yml up -d --wait"),
        ("stdout", "Subindo os contêineres do laboratório de ataque"),
        ("stdout", "Contêineres do laboratório prontos"),
    )

    def up(self, compose_file: str, *, on_log=None) -> CommandResult:
        lines: list[LogLine] = []
        for stream, text in self.LINHAS:
            line = LogLine(step="subir_conteineres", stream=stream, text=text)
            lines.append(line)
            if on_log is not None:
                on_log(line)
        return CommandResult(code=0, lines=lines)

    def down(self, compose_file: str, *, on_log=None) -> CommandResult:
        return CommandResult(code=0, lines=[])


class _ProvisionadorContandoChamadas(CurationEnvironmentProvisioner):
    """Provisionador de curation que **conta** as chamadas a ``provision``.

    Serve à asserção de idempotência (Req. 1.6): com o environment já provisionado e
    o Estágio 5 ``completed``, a costura ``provision_stage5`` **reaproveita** a
    Saída persistida e **não** chama ``provision`` de novo. Um segundo POST deve
    deixar o contador em 1. Delega todo o comportamento à superclasse (pipeline
    real sobre os colaboradores *fakes*), apenas incrementando o contador.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.chamadas_provisionar = 0

    def provision(self, params, *, on_log=lambda _l: None):  # type: ignore[override]
        self.chamadas_provisionar += 1
        return super().provision(params, on_log=on_log)


# ---------------------------------------------------------------------------
# Harness: TestClient com DB + provisioner + buffer de logs sobrescritos.
# ---------------------------------------------------------------------------


def _make_client(
    session_factory: sessionmaker[Session],  # noqa: F811 — fixture reusada
    *,
    inspector: object,
    caldera: object,
    provisioner: CurationEnvironmentProvisioner | None = None,
    buffer: ProvisioningLogBuffer | None = None,
) -> TestClient:
    """Monta um :class:`TestClient` com DB + provisioner + buffer sobrescritos.

    Reusa a mesma mecânica do file-base (``get_db`` → SQLite efêmero;
    ``get_provisioner`` → curation com isolation/Caldera *fakes* + compose/
    state *fakes*), mas:

    * usa o :class:`_FakeComposeComLogs` (emite lines) no lugar do compose mudo;
    * **sobrescreve** ``get_log_buffer`` com um :class:`ProvisioningLogBuffer`
      **novo por teste**, de modo que o ``seq`` recomece em 1 e as lines não
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

    def _get_provisionador_override() -> CurationEnvironmentProvisioner:
        if provisioner is not None:
            return provisioner
        return CurationEnvironmentProvisioner(
            inspector=inspector,  # type: ignore[arg-type]
            caldera=caldera,  # type: ignore[arg-type]
            compose_executor=_FakeComposeComLogs(),  # type: ignore[arg-type]
            state_inspector=_HealthyStateInspector(),  # type: ignore[arg-type]
        )

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_provisioner] = _get_provisionador_override
    app.dependency_overrides[get_log_buffer] = lambda: log_buffer
    return TestClient(app)


def _novo_provisionador_fakes(
    *, inspector: object, caldera: object
) -> _ProvisionadorContandoChamadas:
    """Constrói o provisioner contador em torno dos colaboradores *fakes*."""
    return _ProvisionadorContandoChamadas(
        inspector=inspector,  # type: ignore[arg-type]
        caldera=caldera,  # type: ignore[arg-type]
        compose_executor=_FakeComposeComLogs(),  # type: ignore[arg-type]
        state_inspector=_HealthyStateInspector(),  # type: ignore[arg-type]
    )


# ===========================================================================
# (1) GET /stage/5 — shape do payload: health + logs + topology/isolation/states
# ===========================================================================


def test_get_estagio5_shape_inclui_saude_logs_e_topologia(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """GET /stage/5: payload inclui ``health`` (approved/verdicts) e ``logs`` (lista).

    O contrato pt-BR da task 6.1 acrescenta ``health`` (:class:`HealthSummary` com
    ``approved``/``verdicts``) e ``logs`` (lista de :class:`LogLineView`) ao
    payload já existente (topology/isolation/states). A leitura é **read-only**:
    sem um provisionamento anterior, ``health`` vem vazio (não approved) e ``logs``
    é uma lista vazia — abrir a tela nunca sobe contêineres (Req. 6.1).
    _Requirements: 6.1, 2.5_"""
    # Slug curated próprio e nunca provisionado por outro teste, de modo que a
    # leitura read-only observe o state inicial (saúde vazia + logs vazios) no
    # SQLite module-scoped — independente da ordem dos testes.
    slug = "outer_space"
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    resp = client.get(f"/api/campaigns/{slug}/stage/5")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Campos pré-existentes seguem presentes (topology + isolation + states).
    assert body["campaign_id"] == slug
    assert "isolated" in body
    assert isinstance(body["hosts"], list)
    assert isinstance(body["networks"], list)
    assert "mermaid" in body
    numeros = [e["stage"] for e in body["states"]["stages"]]
    assert numeros == [1, 2, 3, 4, 5, 6]

    # DELTA (task 6.1): ``health`` com o shape HealthSummary e ``logs`` como lista.
    assert "health" in body
    health = body["health"]
    assert set(("approved", "verdicts")) <= set(health.keys())
    assert isinstance(health["approved"], bool)
    assert isinstance(health["verdicts"], list)
    assert isinstance(body["logs"], list)

    # Read-only: sem provisionamento anterior, saúde não aprovada e logs vazios.
    assert health["approved"] is False
    assert body["logs"] == []


# ===========================================================================
# (2) POST /stage/5/provision — shape do payload (caminho feliz, conclui)
# ===========================================================================


def test_post_provisionar_shape_caminho_feliz(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """POST /provision: ``environment``/``completed``/``motivo``/``states`` no shape.

    Caminho feliz (isolated + Abilities carregadas + saudável) → ``completed=True``,
    ``motivo=None`` e o Estágio 5 ``completed`` nodes states. O ``environment`` carrega
    o :class:`ProvisionedEnvironment` completo — ``target_containers``, ``isolation``,
    ``health``, ``loaded_abilities``, ``compose_file`` e ``origin``.
    _Requirements: 1.1, 6.1_"""
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    resp = client.post(f"/api/campaigns/{_SLUG}/stage/5/provision")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Shape do envelope: as quatro chaves do contrato existem.
    assert set(("environment", "completed", "reason", "states")) <= set(body.keys())

    environment = body["environment"]
    assert environment is not None
    assert environment["campaign_id"] == _SLUG
    assert environment["platform"] == "conteiner"
    assert environment["target_containers"], "contêineres-target devem ser resolvidos"
    assert environment["isolation"]["approved"] is True
    assert environment["health"]["approved"] is True
    assert environment["loaded_abilities"] is True
    assert environment["compose_file"]
    assert environment["origin"] == "curadoria_humana"

    # Caminho feliz: concluído, sem motivo de bloqueio.
    assert body["completed"] is True
    assert body["reason"] is None

    # O state por Estágio reflete a conclusão do Estágio 5.
    estagio5 = next(e for e in body["states"]["stages"] if e["stage"] == 5)
    assert estagio5["state"] == "concluido"


# ===========================================================================
# (3) Idempotência (Req. 1.6): POST duas vezes → reaproveita (provisiona 1x só)
# ===========================================================================


def test_post_provisionar_idempotente_reaproveita_ambiente(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """POST duas vezes: o segundo reaproveita (não re-provisiona); environment idêntico.

    Com um provisioner contador injetado, o primeiro POST conclui o Estágio 5 e
    o segundo **reaproveita** a Saída persistida — ``provision`` é chamado uma
    única vez nodes dois POSTs (Req. 1.6). Ambos devolvem ``completed=True`` e o
    **mesmo** environment (mesmos ``target_containers``).
    _Requirements: 1.6_"""
    # Slug curated próprio: o primeiro POST deve **rodar** o pipeline (não reusar
    # state de outro teste no SQLite module-scoped), para que o segundo exerça o
    # reaproveitamento com o contador em 1.
    slug = "apt41_dust"
    provisioner = _novo_provisionador_fakes(
        inspector=_IsolatedInspector(), caldera=_FakeCaldera()
    )
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
        provisioner=provisioner,
    )

    primeiro = client.post(f"/api/campaigns/{slug}/stage/5/provision")
    assert primeiro.status_code == 200, primeiro.text
    b1 = primeiro.json()
    assert b1["completed"] is True

    segundo = client.post(f"/api/campaigns/{slug}/stage/5/provision")
    assert segundo.status_code == 200, segundo.text
    b2 = segundo.json()
    assert b2["completed"] is True

    # Reaproveitamento: o provisionamento real rodou uma única vez nodes dois POSTs.
    assert provisioner.chamadas_provisionar == 1

    # O environment devolvido é o mesmo (mesmos contêineres-target).
    assert b2["environment"] is not None
    assert b2["environment"]["target_containers"] == b1["environment"]["target_containers"]


# ===========================================================================
# (4) Logs no retorno (Req. 2.5): POST emite lines → GET /logs e GET /stage/5
# ===========================================================================


def test_logs_no_retorno_apos_provisionar(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """Após um POST que emite lines, GET /logs e GET /stage/5 carregam os logs.

    O ``_FakeComposeComLogs`` emite lines na step ``subir_conteineres``; o buffer
    compartilhado (sobrescrito por teste) as acumula. O ``GET .../stage/5/logs``
    devolve ``logs`` como lista **não vazia** com o shape pt-BR de
    :class:`LogLineView` — ``seq`` monotônico a partir de 1, ``step``, ``stream``,
    ``text``, ``em`` — e o ``GET .../stage/5`` também carrega essas lines.
    _Requirements: 2.5_"""
    # Slug curated próprio: o POST deve **rodar** o pipeline (emitindo lines), e
    # não reusar o state persistido de outro teste no SQLite module-scoped.
    slug = "c0010"
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    prov = client.post(f"/api/campaigns/{slug}/stage/5/provision")
    assert prov.status_code == 200, prov.text

    # GET /logs: envelope + lista não vazia com o shape LogLineView.
    logs_resp = client.get(f"/api/campaigns/{slug}/stage/5/logs")
    assert logs_resp.status_code == 200, logs_resp.text
    logs_body = logs_resp.json()
    assert logs_body["campaign_id"] == slug
    lines = logs_body["logs"]
    assert isinstance(lines, list) and lines, "o buffer deve ter lines"

    chaves = set(("seq", "step", "stream", "text", "at"))
    for line in lines:
        assert chaves <= set(line.keys())

    # ``seq`` monotônico a partir de 1 (o serviço limpa o buffer antes de rodar).
    seqs = [line["seq"] for line in lines]
    assert seqs[0] == 1
    assert seqs == sorted(seqs)
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))

    # As lines de subir_conteineres emitidas pelo fake estão presentes.
    etapas = {line["step"] for line in lines}
    assert "subir_conteineres" in etapas
    textos = [line["text"] for line in lines]
    assert any("laboratório" in t for t in textos)

    # GET /stage/5 também carrega as mesmas lines (mesmo buffer compartilhado).
    estagio_resp = client.get(f"/api/campaigns/{slug}/stage/5")
    assert estagio_resp.status_code == 200, estagio_resp.text
    estagio_logs = estagio_resp.json()["logs"]
    assert isinstance(estagio_logs, list) and estagio_logs
    assert [l["seq"] for l in estagio_logs] == seqs


def test_get_logs_slug_desconhecido_404(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """Slug desconhecido no endpoint de logs é 404 (convenção de erro de campaigns.py).
    _Requirements: 2.5_"""
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    resp = client.get("/api/campaigns/nao-existe/stage/5/logs")
    assert resp.status_code == 404, resp.text


# ===========================================================================
# (5) Caminho de failure: não isolated → não conclui, motivo pt-BR, estágio 5 ≠ concluído
# ===========================================================================


def test_post_provisionar_nao_isolado_nao_conclui_com_motivo(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """Não isolated → ``completed=False``, ``motivo`` pt-BR, Estágio 5 ≠ concluído.

    Com um inspector não isolated, o pipeline reprova na step de isolation: o
    environment ainda é devolvido para exibição (``isolation.approved=False`` com
    o(s) contêiner(es) ofensor(es)), mas o Estágio 5 **não** conclui e o
    ``motivo`` traz a causa pt-BR surfaçando o contêiner não isolated. O state do
    Estágio 5 permanece diferente de ``completed`` (Estágio 6 blocked).
    _Requirements: 6.1_"""
    # Slug curated distinto: este teste nunca conclui o Estágio 5, isolando-o do
    # state persistido do caminho feliz no SQLite module-scoped.
    slug = "costaricto"
    client = _make_client(
        session_factory,
        inspector=_NotIsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    resp = client.post(f"/api/campaigns/{slug}/stage/5/provision")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    environment = body["environment"]
    assert environment is not None
    assert environment["isolation"]["approved"] is False
    non_isolated = [v for v in environment["isolation"]["verdicts"] if not v["isolated"]]
    assert non_isolated, "ao menos um contêiner deve ser sinalizado não isolated"

    assert body["completed"] is False
    assert body["reason"], "uma razão pt-BR de não isolation deve ser reportada"
    ofensor = non_isolated[0]["container"]
    assert ofensor in body["reason"]

    estagio5 = next(e for e in body["states"]["stages"] if e["stage"] == 5)
    assert estagio5["state"] != "concluido"
