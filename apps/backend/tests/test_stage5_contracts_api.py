"""JSON contracts (pt-BR) of the Stage 5 REST API — Environment creation (task 6.3).

Covers the **payload shape**, the **idempotency** and the **logs in the response**
of the three Stage 5 REST endpoints added/evolved in tasks 6.1/6.2 in
``app/api/campaigns.py``:

* ``GET  /api/campaigns/{campaign}/stage/5`` → :class:`Stage4ProvisioningResponse`
  now includes ``health`` (:class:`~app.services.environment.health.HealthSummary`)
  and ``logs`` (list of :class:`LogLineView`) in addition to topology/isolation/states
  (task 6.1). Read-only — opening the screen never provisions (Req. 6.1).
* ``POST /api/campaigns/{campaign}/stage/5/provision`` → :class:`ProvisionStage5Response`
  delegates to the **idempotent** seam ``EnvironmentStageService.provision_stage5``
  (task 6.2): ``environment`` (Optional :class:`ProvisionedEnvironment`), ``completed``,
  ``motivo`` and ``states`` (Req. 1.1, 1.6).
* ``GET  /api/campaigns/{campaign}/stage/5/logs`` → :class:`Stage5LogsResponse` with
  ``campaign_id`` and ``logs`` (current buffer — Req. 2.5).

Reuses the **same template** as ``test_stage5_provision_endpoints.py`` (ephemeral
per-module SQLite via ``get_db``; curation provisioner with isolation inspector +
Caldera *fakes* via ``get_provisioner``; compose/state *fakes* so the pipeline runs
without a subprocess or Docker daemon). It imports the *fake* collaborators and the
session factory from that file so they are not duplicated.

Deterministic log buffer
-------------------------
The endpoints now read the process-level shared buffer via ``get_log_buffer()``.
For deterministic assertions with no leakage between tests, each
:class:`TestClient` **overrides** ``get_log_buffer`` with a fresh
:class:`ProvisioningLogBuffer` **per test** — so ``seq`` restarts at 1 and the
lines of one test do not contaminate another. The ``POST .../provision`` and the
two ``GET`` resolve the **same** overridden instance (shared within the client),
so the lines emitted during the POST are retrievable by the GETs in the same test.

Logs in the response
--------------------
The compose *fakes* of the base file emit **zero** lines. To exercise "logs in the
response" with meaningful content, this file uses a :class:`_FakeComposeWithLogs`
that emits a few :class:`LogLine` via ``on_log`` on the ``subir_conteineres`` step
— exactly as the real executor streams the ``docker compose`` output —, so the
buffer has pt-BR lines with a monotonic ``seq`` starting at 1 to assert against.

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

# Reuses the endpoints-file template (ephemeral SQLite + fake collaborators):
# we do not duplicate the session fixture nor the inspectors/Caldera/state fakes.
from tests.test_stage5_provision_endpoints import (
    _FakeCaldera,
    _HealthyStateInspector,
    _IsolatedInspector,
    _NotIsolatedInspector,
    session_factory,  # noqa: F401 — fixture reusada (module-scoped SQLite)
)

_SLUG = "shadowray"  # real curated campaign (11 Abilities), purely derivable Stage 4.


# ---------------------------------------------------------------------------
# Fake de compose que EMITE lines de log (os fakes do file-base emitem zero).
# ---------------------------------------------------------------------------


class _FakeComposeWithLogs:
    """``ComposeExecutor`` *fake* that emits a few :class:`LogLine` on ``up``.

    The base-file :class:`_FakeComposeExecutor` returns
    ``CommandResult(code=0, lines=[])`` **without** calling ``on_log`` — no
    line reaches the buffer. To assert "logs in the response" (Req. 2.5) with
    deterministic pt-BR content, this *fake* streams a few lines on the
    ``subir_conteineres`` step (as the real executor streams the ``docker
    compose`` output), so the shared buffer has lines with a monotonic ``seq``
    starting at 1 for the test to assert. ``code=0`` lets the pipeline
    advance to health → Abilities → isolation.
    """

    LINES: tuple[tuple[str, str], ...] = (
        ("command", "docker compose -f docker/docker-compose.yml up -d --wait"),
        ("stdout", "Bringing up the attack lab containers"),
        ("stdout", "Lab containers ready"),
    )

    def up(self, compose_file: str, *, on_log=None) -> CommandResult:
        lines: list[LogLine] = []
        for stream, text in self.LINES:
            line = LogLine(step="subir_conteineres", stream=stream, text=text)
            lines.append(line)
            if on_log is not None:
                on_log(line)
        return CommandResult(code=0, lines=lines)

    def down(self, compose_file: str, *, on_log=None) -> CommandResult:
        return CommandResult(code=0, lines=[])


class _CallCountingProvisioner(CurationEnvironmentProvisioner):
    """Curation provisioner that **counts** the calls to ``provision``.

    Serves the idempotency assertion (Req. 1.6): with the environment already
    provisioned and Stage 5 ``completed``, the ``provision_stage5`` seam
    **reuses** the persisted output and does **not** call ``provision`` again. A
    second POST must leave the counter at 1. It delegates all behavior to the
    superclass (real pipeline over the *fake* collaborators), only incrementing
    the counter.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.provision_calls = 0

    def provision(self, params, *, on_log=lambda _l: None):  # type: ignore[override]
        self.provision_calls += 1
        return super().provision(params, on_log=on_log)


# ---------------------------------------------------------------------------
# Harness: TestClient with DB + provisioner + log buffer overridden.
# ---------------------------------------------------------------------------


def _make_client(
    session_factory: sessionmaker[Session],  # noqa: F811 — fixture reusada
    *,
    inspector: object,
    caldera: object,
    provisioner: CurationEnvironmentProvisioner | None = None,
    buffer: ProvisioningLogBuffer | None = None,
) -> TestClient:
    """Builds a :class:`TestClient` with DB + provisioner + buffer overridden.

    Reuses the same mechanism as the base file (``get_db`` → ephemeral SQLite;
    ``get_provisioner`` → curation with isolation/Caldera *fakes* + compose/
    state *fakes*), but:

    * uses :class:`_FakeComposeWithLogs` (emits lines) instead of the silent compose;
    * **overrides** ``get_log_buffer`` with a :class:`ProvisioningLogBuffer`
      **fresh per test**, so ``seq`` restarts at 1 and the lines do not leak
      between tests — and the POST and the two GET share the same instance.
    """
    app = create_app()
    log_buffer = buffer if buffer is not None else ProvisioningLogBuffer()

    def _get_db_override():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def _get_provisioner_override() -> CurationEnvironmentProvisioner:
        if provisioner is not None:
            return provisioner
        return CurationEnvironmentProvisioner(
            inspector=inspector,  # type: ignore[arg-type]
            caldera=caldera,  # type: ignore[arg-type]
            compose_executor=_FakeComposeWithLogs(),  # type: ignore[arg-type]
            state_inspector=_HealthyStateInspector(),  # type: ignore[arg-type]
        )

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_provisioner] = _get_provisioner_override
    app.dependency_overrides[get_log_buffer] = lambda: log_buffer
    return TestClient(app)


def _new_provisioner_fakes(
    *, inspector: object, caldera: object
) -> _CallCountingProvisioner:
    """Builds the counting provisioner around the *fake* collaborators."""
    return _CallCountingProvisioner(
        inspector=inspector,  # type: ignore[arg-type]
        caldera=caldera,  # type: ignore[arg-type]
        compose_executor=_FakeComposeWithLogs(),  # type: ignore[arg-type]
        state_inspector=_HealthyStateInspector(),  # type: ignore[arg-type]
    )


# ===========================================================================
# (1) GET /stage/5 — shape do payload: health + logs + topology/isolation/states
# ===========================================================================


def test_get_stage5_shape_includes_health_logs_and_topology(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """GET /stage/5: payload includes ``health`` (approved/verdicts) and ``logs`` (list).

    O contrato pt-BR da task 6.1 acrescenta ``health`` (:class:`HealthSummary` com
    ``approved``/``verdicts``) and ``logs`` (list of :class:`LogLineView`) to the
    already-existing payload (topology/isolation/states). The read is **read-only**:
    with no prior provisioning, ``health`` comes empty (not approved) and ``logs``
    is an empty list — opening the screen never brings up containers (Req. 6.1).
    _Requirements: 6.1, 2.5_"""
    # Its own curated slug, never provisioned by another test, so the
    # read-only read observes the initial state (empty health + empty logs) in the
    # module-scoped SQLite — independent of test order.
    slug = "outer_space"
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    resp = client.get(f"/api/campaigns/{slug}/stage/5")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Pre-existing fields remain present (topology + isolation + states).
    assert body["campaign_id"] == slug
    assert "isolated" in body
    assert isinstance(body["hosts"], list)
    assert isinstance(body["networks"], list)
    assert "mermaid" in body
    numbers = [e["stage"] for e in body["states"]["stages"]]
    assert numbers == [1, 2, 3, 4, 5, 6]

    # DELTA (task 6.1): ``health`` with the HealthSummary shape and ``logs`` as a list.
    assert "health" in body
    health = body["health"]
    assert set(("approved", "verdicts")) <= set(health.keys())
    assert isinstance(health["approved"], bool)
    assert isinstance(health["verdicts"], list)
    assert isinstance(body["logs"], list)

    # Read-only: with no prior provisioning, health not approved and empty logs.
    assert health["approved"] is False
    assert body["logs"] == []


# ===========================================================================
# (2) POST /stage/5/provision — payload shape (happy path, completes)
# ===========================================================================


def test_post_provision_shape_happy_path(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """POST /provision: ``environment``/``completed``/``motivo``/``states`` in the shape.

    Happy path (isolated + Abilities loaded + healthy) → ``completed=True``,
    ``motivo=None`` and Stage 5 ``completed`` in the states. The ``environment`` carries
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

    # Envelope shape: the four contract keys exist.
    assert set(("environment", "completed", "reason", "states")) <= set(body.keys())

    environment = body["environment"]
    assert environment is not None
    assert environment["campaign_id"] == _SLUG
    assert environment["platform"] == "conteiner"
    assert environment["target_containers"], "target containers must be resolved"
    assert environment["isolation"]["approved"] is True
    assert environment["health"]["approved"] is True
    assert environment["loaded_abilities"] is True
    assert environment["compose_file"]
    assert environment["origin"] == "curadoria_humana"

    # Happy path: completed, with no blocking reason.
    assert body["completed"] is True
    assert body["reason"] is None

    # The per-Stage state reflects the completion of Stage 5.
    stage5 = next(e for e in body["states"]["stages"] if e["stage"] == 5)
    assert stage5["state"] == "concluido"


# ===========================================================================
# (3) Idempotency (Req. 1.6): POST twice → reuses (provisions only once)
# ===========================================================================


def test_post_provision_idempotent_reuses_environment(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """POST duas vezes: o second reaproveita (não re-provisiona); environment idêntico.

    Com um provisioner contador injetado, o first POST conclui o Estágio 5 e
    o second **reaproveita** a Saída persistida — ``provision`` é chamado uma
    exactly once across the two POSTs (Req. 1.6). Both return ``completed=True`` and the
    **mesmo** environment (mesmos ``target_containers``).
    _Requirements: 1.6_"""
    # Slug curated próprio: o first POST deve **rodar** o pipeline (não reusar
    # state de outro teste no SQLite module-scoped), para que o second exerça o
    # reaproveitamento com o contador em 1.
    slug = "apt41_dust"
    provisioner = _new_provisioner_fakes(
        inspector=_IsolatedInspector(), caldera=_FakeCaldera()
    )
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
        provisioner=provisioner,
    )

    first = client.post(f"/api/campaigns/{slug}/stage/5/provision")
    assert first.status_code == 200, first.text
    b1 = first.json()
    assert b1["completed"] is True

    second = client.post(f"/api/campaigns/{slug}/stage/5/provision")
    assert second.status_code == 200, second.text
    b2 = second.json()
    assert b2["completed"] is True

    # Reuse: the real provisioning ran exactly once across the two POSTs.
    assert provisioner.provision_calls == 1

    # The returned environment is the same (same target containers).
    assert b2["environment"] is not None
    assert b2["environment"]["target_containers"] == b1["environment"]["target_containers"]


# ===========================================================================
# (4) Logs no retorno (Req. 2.5): POST emite lines → GET /logs e GET /stage/5
# ===========================================================================


def test_logs_in_response_after_provision(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """After a POST that emits lines, GET /logs and GET /stage/5 carry the logs.

    O ``_FakeComposeWithLogs`` emite lines na step ``subir_conteineres``; o buffer
    compartilhado (sobrescrito por teste) as acumula. O ``GET .../stage/5/logs``
    returns ``logs`` as a **non-empty** list with the pt-BR shape of
    :class:`LogLineView` — ``seq`` monotônico a partir de 1, ``step``, ``stream``,
    ``text``, ``em`` — and ``GET .../stage/5`` also carries those lines.
    _Requirements: 2.5_"""
    # Its own curated slug: the POST must **run** the pipeline (emitting lines), and
    # not reuse the persisted state of another test in the module-scoped SQLite.
    slug = "c0010"
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    prov = client.post(f"/api/campaigns/{slug}/stage/5/provision")
    assert prov.status_code == 200, prov.text

    # GET /logs: envelope + non-empty list with the LogLineView shape.
    logs_resp = client.get(f"/api/campaigns/{slug}/stage/5/logs")
    assert logs_resp.status_code == 200, logs_resp.text
    logs_body = logs_resp.json()
    assert logs_body["campaign_id"] == slug
    lines = logs_body["logs"]
    assert isinstance(lines, list) and lines, "o buffer deve ter lines"

    chaves = set(("seq", "step", "stream", "text", "at"))
    for line in lines:
        assert chaves <= set(line.keys())

    # ``seq`` monotonic from 1 (the service clears the buffer before running).
    seqs = [line["seq"] for line in lines]
    assert seqs[0] == 1
    assert seqs == sorted(seqs)
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))

    # The subir_conteineres lines emitted by the fake are present.
    etapas = {line["step"] for line in lines}
    assert "subir_conteineres" in etapas
    texts = [line["text"] for line in lines]
    assert any("lab" in t for t in texts)

    # GET /stage/5 also carries the same lines (same shared buffer).
    stage_resp = client.get(f"/api/campaigns/{slug}/stage/5")
    assert stage_resp.status_code == 200, stage_resp.text
    stage_logs = stage_resp.json()["logs"]
    assert isinstance(stage_logs, list) and stage_logs
    assert [l["seq"] for l in stage_logs] == seqs


def test_get_logs_unknown_slug_404(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """Unknown slug on the logs endpoint is 404 (campaigns.py error convention).
    _Requirements: 2.5_"""
    client = _make_client(
        session_factory,
        inspector=_IsolatedInspector(),
        caldera=_FakeCaldera(),
    )
    resp = client.get("/api/campaigns/nao-existe/stage/5/logs")
    assert resp.status_code == 404, resp.text


# ===========================================================================
# (5) Failure path: not isolated → does not complete, pt-BR motivo, stage 5 != completed
# ===========================================================================


def test_post_provision_not_isolated_does_not_complete_with_reason(
    session_factory: sessionmaker[Session],  # noqa: F811
) -> None:
    """Not isolated → ``completed=False``, pt-BR ``motivo``, Stage 5 != completed.

    With a not-isolated inspector, the pipeline fails at the isolation step: the
    environment is still returned for display (``isolation.approved=False`` with
    o(s) contêiner(es) offender(es)), mas o Estágio 5 **não** conclui e o
    ``motivo`` carries the pt-BR cause surfacing the not-isolated container. The state of
    Stage 5 stays different from ``completed`` (Stage 6 blocked).
    _Requirements: 6.1_"""
    # Distinct curated slug: this test never completes Stage 5, isolating it from the
    # persisted happy-path state in the module-scoped SQLite.
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
    assert non_isolated, "at least one container must be flagged not isolated"

    assert body["completed"] is False
    assert body["reason"], "a pt-BR not-isolation reason must be reported"
    offender = non_isolated[0]["container"]
    assert offender in body["reason"]

    stage5 = next(e for e in body["states"]["stages"] if e["stage"] == 5)
    assert stage5["state"] != "concluido"
