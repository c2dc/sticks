"""EnvironmentStageService — Stage 4 (auto-completion) + Stage 5 (active provisioning).

This *session-bound* service orchestrates the environment for the six-stage
pipeline. It covers two complementary roles:

* **Stage 4 — auto-completion (task 4.2).** :meth:`~EnvironmentStageService.auto_complete_stage4`
  automatically completes a curated Campaign's Stage 4 when Stage 3 is
  ``completed``, the curated material exists and the isolation is **verified**
  (read-only). Preserved with no behavior change.
* **Stage 5 — active provisioning (task 5.1).**
  :meth:`~EnvironmentStageService.provision_stage5` triggers, **with no manual
  action** (Req. 1.1), the provisioning pipeline of the
  :class:`EnvironmentProvisioner` seam (bring up containers → await health →
  load Abilities → verify isolation), forwards each log line to the shared
  :class:`ProvisioningLogBuffer` — which the WebSocket channel reads for the
  screen's live terminal (Req. 2.2) — and updates the Stage 5 ``StageRun`` with
  the per-step progress and, on failure, ``failed_step`` + ``error_message``
  (Req. 7.1). Stage 5 **completion** happens **only** when the three
  prerequisites pass (health + Abilities + isolation — ``environment.ready``,
  Req. 5.5), completing the ``StageRun`` to ``completed``/100 and releasing Stage
  6 through the sequential gate (task 5.3 — Req. 6.2, 6.4); the idempotency
  (task 5.2) reuses an already-persisted ready environment.

Stage 4 (Environment provisioning) of a **curated** Campaign requires no action
from the Researcher: as soon as Stage 3 (Operationalization) is ``completed`` and
the curated environment is available and **verified isolated**, the Backend marks
the ``stage=4`` ``StageRun`` (:data:`STAGE_PROVISIONING`) as ``completed``
automatically (Req. 5.3, 9.2). If the isolation **cannot** be verified, Stage 4
does not complete and Stage 5 stays blocked by the sequential gate (Req. 5.6,
9.3).

This service is, for the environment, the counterpart of the DAG auto-completion
(``DagService.sync_curated_dags`` + the Stage 2 completion): the same *service
with injected ``Session``* seam, the same *idempotent upsert* of the ``StageRun``
by ``(campaign_id, stage)`` and the same *transactional pattern* (a single
``commit``; ``rollback`` preserves the previous state on persistence failure) as
:class:`~app.services.session.session_state_service.SessionStateService` and
:class:`~app.services.dag.dag_service.DagService`.

Design decisions
----------------

**Sequential gate (Req. 9.3).** The auto-completion only advances when the
Campaign's Stage 3 ``StageRun`` is ``completed``. While it is not, the method does
nothing — Stage 4 stays incomplete (blocked), exactly as
:meth:`StageService.is_stage_startable` derives. The decision depends **only** on
the previous stage's state, never on Stage 4 itself, keeping the gate purely
sequential.

**Curated environment material (Req. 9.4).** The auto-completion does **not**
happen for a Campaign without the corresponding environment material. The
"environment configuration file" of the curated Campaigns is the lab's
``docker/docker-compose.yml``, whose path is resolved by
:func:`...topology_builder.compose_path` (returns ``None`` when the file does not
exist). Without that material, Stage 4 does not complete. The file's availability
is injectable (``compose_available``) so the tests exercise the "no material" path
without touching the filesystem.

**Isolation verification (Req. 5.6).** With the gate released and the material
present, the service calls :meth:`CurationEnvironmentProvisioner.prepare` and only
completes Stage 4 when ``result.ready``/``isolated`` is ``True``. Otherwise Stage
4 stays incomplete (and Stage 5 blocked), and the reason is available in the
:class:`ProvisioningResult` for the screen to display.

**Inspector unavailable (Docker absent in dev/CI) — fail-closed.** In production,
when no inspector is injected, :func:`inspect_isolation` builds the real Docker
adapter lazily and **raises ``RuntimeError``** if the daemon/SDK is unavailable
(the typical case of the Windows development environment). In that case the
isolation **genuinely cannot be verified**. Decision: **do not complete** Stage 4
(fail-closed), instead of completing assuming isolation. This preserves the
security contract of Req. 5.6/9.2 — "the Backend marks completed **after
verifying** the isolation" — without silently weakening it. The impossibility to
verify is signaled explicitly in the typed result (``isolation_unavailable=True``
+ ``message``), so the API/screen layer shows "environment not verified" and Stage
5 stays blocked. In an environment with Docker (or with an injected inspector —
production or tests), the verification runs normally and, if isolated, Stage 4
auto-completes.

Idempotency / transactionality
-------------------------------
The completion is an *absolute upsert* of the ``(campaign_id, stage=4)``
``StageRun`` to ``completed``/progress 100 in a single transaction. Re-running
produces exactly the same persisted state (no new line, no divergent field). A
``rollback`` on a persistence failure leaves the previous state intact, with no
partial mutation — the same pattern as ``choose_dag`` /
``persist_stage_completion``.

_Requirements: 5.2, 5.3, 5.6, 9.2, 9.3, 9.4_
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
from typing import Callable, Optional

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.domain import ProvisionedEnvironmentRow, StageRun
from app.models.enums import PersistedEnvironmentPlatform, StageState
from app.services.caldera.client import (
    DEFAULT_GROUP,
    CalderaApiError,
    CalderaClient,
    CalderaUnavailable,
)
from app.services.containment import ContainerInspector
from app.services.environment.compose_executor import LogLine
from app.services.environment.log_buffer import ProvisioningLogBuffer
from app.services.environment.operationalization import EnvironmentParameters
from app.services.environment.provisioner import (
    ProvisionedEnvironment,
    CurationEnvironmentProvisioner,
    EnvironmentProvisioner,
)
from app.services.environment.topology_builder import compose_path
from app.services.stage.stage_service import (
    STAGE_ENVIRONMENT,
    STAGE_OPERATIONALIZATION,
    STAGE_PROVISIONING,
)


logger = logging.getLogger(__name__)


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp (portable between PostgreSQL and the test SQLite)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class AutoProvisioningResult(BaseModel):
    """Typed result of :meth:`EnvironmentStageService.auto_complete_stage4`.

    Instead of raising exceptions, the auto-completion returns this typed result
    (same contract as ``DagChoiceResult`` / ``StageCompletionResult``):
    ``completed`` indicates whether Stage 4 moved to ``completed`` by this call (or
    already was, idempotently); the other fields explain **why** it did not
    complete, so the API/screen layer can surface the reason and keep Stage 5
    blocked.

    Attributes
    ----------
    campaign_id:
        The Campaign whose Stage 4 auto-completion was attempted.
    completed:
        ``True`` when, by the end of the call, the Campaign's Stage 4 ``StageRun``
        is ``completed`` (progress 100) — by this call or already from before
        (idempotency). ``False`` on any path that does **not** complete (gate not
        released, missing material, not isolated, isolation not verifiable, or
        persistence failure).
    blocked_by_gate:
        ``True`` when the Campaign's Stage 3 is **not** ``completed``; Stage 4
        stays blocked and nothing is persisted (Req. 9.3).
    missing_material:
        ``True`` when there is no curated environment material available for the
        Campaign (``compose`` missing); the auto-completion does not happen
        (Req. 9.4).
    isolated:
        ``True`` iff the isolation verification ran and passed. ``False`` when it
        did not run (gate/material/unavailable) or failed.
    isolation_unavailable:
        ``True`` when the isolation **could not be verified** because the Docker
        inspector/daemon is unavailable (fail-closed): Stage 4 does **not**
        complete (Req. 5.6). Distinct from ``isolated=False`` due to a provably
        non-isolated environment.
    persistence_failed:
        ``True`` when the verification approved but the ``commit`` failed; the
        previous state was preserved unchanged (rollback).
    message:
        Text explaining the result (reason for the non-completion), for the API/UI
        to surface; ``None`` when it completed without caveats.
    stage_run_id:
        The id of the Stage 4 ``StageRun``, when completed by this call; ``None``
        otherwise.
    """

    campaign_id: str
    completed: bool = False
    blocked_by_gate: bool = False
    missing_material: bool = False
    isolated: bool = False
    isolation_unavailable: bool = False
    persistence_failed: bool = False
    message: Optional[str] = None
    stage_run_id: Optional[int] = None


class ProvisioningStage5Result(BaseModel):
    """Typed result of :meth:`EnvironmentStageService.provision_stage5` (task 5.1).

    The **active** Stage 5 provisioning (bring up the lab → await health → load
    Abilities → verify isolation) is triggered automatically on entering Stage 5
    (Req. 1.1). As with the other *session-bound* seams, instead of raising on
    expected paths the orchestration returns this typed result, carrying what the
    API/screen layer needs to reflect the progress and the (non-)completion and to
    keep Stage 6 blocked while the environment is not ready (Req. 7.1).

    .. note::
        The orchestration covers the **automatic trigger** (task 5.1), the
        forwarding of the log lines to the buffer (which feeds the channel —
        Req. 2.2), the idempotency (task 5.2 — reuse an already-persisted ready
        environment) and the ``StageRun`` update according to the pipeline verdict
        (Req. 7.1): ``em_andamento`` at the start; ``failed_step`` +
        ``error_message`` + ``erro`` on failure; and — now (task 5.3) — the
        **completion** to ``completed``/progress 100 **only when the three
        prerequisites pass** (``environment.ready`` — health + Abilities +
        isolation, Req. 5.5). Since Stage 5 only becomes ``COMPLETED`` on that
        path, the sequential gate of :meth:`StageService.is_stage_startable` keeps
        Stage 6 blocked while Stage 5 has not completed (Req. 6.2, 6.4). The
        persistence of the :class:`ProvisionedEnvironment` as portable JSON
        (Req. 6.3) accompanies the completion in the same transaction.

    Attributes
    ----------
    campaign_id:
        The Campaign whose Stage 5 provisioning was triggered.
    blocked_by_gate:
        ``True`` when Stage 4 (Operationalization) is **not** ``completed``; the
        sequential gate prevents the provisioning and **nothing** is brought
        up/persisted (Req. 1.1 — Stage 5 only starts with Stage 4 completed).
    ready:
        ``True`` iff the pipeline walked all the steps and the three prerequisites
        passed (``environment.ready``) — healthy containers, loaded Abilities and
        approved isolation (Req. 5.5). When ``True``, the Stage 5 ``StageRun`` is
        **completed** (``completed``/progress 100) by this call (task 5.3), which —
        through the sequential gate — releases Stage 6 (Req. 6.2, 6.4); when
        ``False``, Stage 5 does **not** complete and Stage 6 stays blocked.
    failed_step:
        The step that failed (``"subir_conteineres"`` / ``"aguardar_saude"`` /
        ``"carregar_abilities"`` / ``"verificar_isolamento"``) or ``None`` when the
        pipeline completed without a failure (Req. 7.1).
    error_message:
        Readable cause of the step failure, **derived from the last error line**
        the failed step left in the log buffer — the same text the screen's live
        terminal/console shows —, guaranteeing by construction the screen ↔ console
        consistency (task 5.4 — Req. 7.1, 7.5). It is the **same** value written to
        ``StageRun.error_message``. It falls back to the provisioner's
        ``environment.error_message`` when the step left no error line in the
        buffer; ``None`` on success.
    environment:
        The pipeline's typed :class:`ProvisionedEnvironment` output — present on
        all paths that **ran** the provisioning (even on failure, with the partial
        summaries and ``failed_step``); ``None`` only when the gate blocked and the
        pipeline did not run.
    persistence_failed:
        ``True`` when the ``StageRun`` update (``em_andamento`` or ``erro``) or the
        output persistence (``ProvisionedEnvironmentRow``) could not be saved; the
        previous state was preserved (rollback).
    reused:
        ``True`` when the environment **had already** been provisioned
        successfully for the Campaign and remained provisioned — a ``ready``
        persisted :class:`ProvisionedEnvironmentRow` **and** the Stage 5
        ``StageRun`` already ``completed`` — and, therefore, the provisioning was
        **reused** (Req. 1.6): the typed output is rebuilt from the persisted JSON,
        ``ready=True``, **without** bringing up a new environment
        (``provisioner.provision`` is not called). ``False`` on all other paths
        (the pipeline actually ran).
    stage_run_id:
        The id of the Stage 5 ``StageRun`` updated by this call; ``None`` when the
        gate blocked or the persistence failed.
    """

    campaign_id: str
    blocked_by_gate: bool = False
    ready: bool = False
    failed_step: Optional[str] = None
    error_message: Optional[str] = None
    environment: Optional[ProvisionedEnvironment] = None
    persistence_failed: bool = False
    reused: bool = False
    stage_run_id: Optional[int] = None


class AgentHealthCause(str, enum.Enum):
    """The specific blocking cause of the Stage 5 agent-health gate (Req. 4.6).

    When the environment cannot be guaranteed healthy, the gate reports **which**
    specific condition blocked the advance, so the Stage 5 screen surfaces the
    precise cause (Req. 4.2/4.3/4.5/4.6) — never a generic failure.

    Members
    -------
    NONE:
        No blocking cause — the gate released the advance (≥ 1 ``alive=true``
        agent in the expected group).
    NO_ACTIVE_AGENT:
        No agent with ``alive=true`` was found in the expected group (Req. 4.2).
    ORPHAN:
        No active agent **and** at least one orphan (``alive=false``) agent is
        registered in the expected group (Req. 4.3) — the typical "rebuilt image
        left a dead agent behind" case that would stall the Operation silently.
    POINTING_DIVERGENCE:
        The configured endpoint/route or host/container name does not match the
        running environment: a Caldera API error (e.g. 404) or a non-resolvable
        host maps here (Req. 4.4/4.5).
    """

    NONE = "none"
    NO_ACTIVE_AGENT = "no_active_agent"
    ORPHAN = "orphan"
    POINTING_DIVERGENCE = "pointing_divergence"


class AgentHealthResult(BaseModel):
    """Typed verdict of the Stage 5 agent-health gate (Req. 4.1/4.2/4.3/4.4/4.6).

    The gate consults the Caldera API (``CalderaClient.list_agents``) and decides
    whether Stage 5 may release the advance to Stage 6 based **only** on the
    agents of the expected group: it requires **≥ 1 agent with ``alive=true``**
    (Req. 4.1), detects **orphans** (``alive=false``) left by rebuilt images
    (Req. 4.3) and blocks the advance with a **specific cause** (absence of active
    agent, orphan present, or pointing divergence — Req. 4.2/4.4/4.6).

    Like the other *session-bound* seams it is a typed result rather than an
    exception: the API/screen layer reads it to surface the cause on the Stage 5
    screen and keep Stage 6 blocked. Stage 6 consumes the **same** verdict
    **read-only** (no re-run — Req. 5.1/5.2).

    Attributes
    ----------
    campaign_id:
        The Campaign whose agent health was checked.
    group:
        The expected agent group the gate required agents to be registered under.
    released:
        ``True`` iff the advance to Stage 6 is released — i.e. there is **≥ 1**
        agent with ``alive=true`` in the expected group (Req. 4.1). ``False`` on
        every blocking path.
    active_agents:
        The number of ``alive=true`` agents found in the expected group.
    orphan_paws:
        The ``paw`` identifiers of the ``alive=false`` agents found in the
        expected group — always signalled, even when the advance is released, so
        the screen can warn about leftover dead agents (Req. 4.3).
    cause:
        The specific blocking cause (:class:`AgentHealthCause`); ``NONE`` when
        released (Req. 4.6).
    message:
        pt-BR text explaining the verdict for the Stage 5 screen; ``None`` when
        released without caveats.
    """

    campaign_id: str
    group: str
    released: bool = False
    active_agents: int = 0
    orphan_paws: list[str] = Field(default_factory=list)
    cause: AgentHealthCause = AgentHealthCause.NONE
    message: Optional[str] = None


# Portuguese messages per non-completion path (for the API/UI to surface).
_MSG_GATE = (
    "O Estágio 3 (Operacionalização) deste Caso ainda não está concluído; o "
    "Estágio 4 (Provisionamento do Ambiente) permanece bloqueado."
)
_MSG_NO_MATERIAL = (
    "Este Caso não possui arquivo de configuração de ambiente disponível; o "
    "Estágio 4 não é concluído automaticamente."
)
_MSG_NOT_ISOLATED = (
    "O ambiente deste Caso não pôde ser verificado como isolado; o Estágio 4 "
    "não é concluído e o Estágio 5 permanece bloqueado."
)
_MSG_ISOLATION_UNAVAILABLE = (
    "Não foi possível verificar o isolamento do ambiente (Docker indisponível); "
    "o Estágio 4 não é concluído sem verificação de isolamento."
)
_MSG_PERSISTENCE_FAILURE = (
    "A conclusão automática do Estágio 4 não foi salva: falha ao persistir na "
    "base de dados. O estado anterior foi preservado sem alteração."
)

# Portuguese messages of the active Stage 5 provisioning (task 5.1).
_MSG_GATE_STAGE5 = (
    "O Estágio 4 (Operacionalização) deste Caso ainda não está concluído; o "
    "Estágio 5 (Criação do ambiente de simulação) não é provisionado e permanece "
    "bloqueado."
)
_MSG_PERSISTENCE_FAILURE_STAGE5 = (
    "O andamento do provisionamento do Estágio 5 não foi salvo: falha ao "
    "persistir na base de dados. O estado anterior foi preservado sem alteração."
)
_MSG_PERSISTENCE_FAILURE_OUTPUT_STAGE5 = (
    "A Saída do provisionamento do Estágio 5 não foi salva: falha ao persistir "
    "na base de dados. O estado anterior foi preservado sem alteração."
)

# Portuguese messages of the Stage 5 agent-health gate (task 8.1 — Req. 4). The
# cause is always surfaced on the Stage 5 screen (not the Stage 6 one — Req. 4.6).
_MSG_AGENTS_NO_ACTIVE = (
    "Nenhum agente ativo (alive=true) foi encontrado no grupo esperado do "
    "Caldera; o avanço para o Estágio 6 permanece bloqueado até haver pelo menos "
    "1 agente ativo."
)
_MSG_AGENTS_ORPHAN = (
    "Há agente(s) órfão(s) (alive=false) registrado(s) no grupo esperado do "
    "Caldera e nenhum agente ativo; o avanço para o Estágio 6 permanece "
    "bloqueado até haver pelo menos 1 agente ativo."
)
_MSG_AGENTS_DIVERGENCE = (
    "O apontamento do Caldera (rota/endpoint ou nome de host/contêiner) não "
    "corresponde ao ambiente em execução; o avanço para o Estágio 6 permanece "
    "bloqueado por divergência de apontamento."
)

# Aggregate progress per pipeline step (per-step progress — task 5.1). The four
# pipeline steps map to increasing progress milestones. The milestone is derived
# from the ``failed_step`` on failure (how far the step got before failing). When
# the pipeline walks everything and the three prerequisites pass
# (``environment.ready``), Stage 5 completes at 100 (task 5.3 — Req. 5.5); the
# defensive path "walked everything but not ``ready``" stays at 90 without
# completing.
_PROGRESS_BY_STEP: dict[str, int] = {
    "subir_conteineres": 10,
    "aguardar_saude": 40,
    "carregar_abilities": 60,
    "verificar_isolamento": 80,
}

# Streams considered "error line" when deriving the ``StageRun`` ``error_message``
# from the terminal's last line (task 5.4 — Req. 7.5). The ``compose`` executor
# and the pipeline steps (``_check_health``, ``carregar_abilities``,
# ``verificar_isolamento``) emit the failure **cause** as a ``stderr`` line in the
# buffer (the same buffer the screen console renders). We also accept ``erro`` for
# robustness — it is the label the screen uses to color those lines —, so the
# derivation follows any error stream actually emitted.
_ERROR_STREAMS: tuple[str, ...] = ("stderr", "erro")


class EnvironmentStageService:
    """Automatically complete the Stage 4 of curated Campaigns (task 4.2).

    Same mold as the other *session-bound* services (``DagService`` /
    ``SessionStateService``): it receives an injected
    :class:`~sqlalchemy.orm.Session` and an :class:`EnvironmentProvisioner`
    (curation by default). The Docker inspector is **injectable** and passed on to
    the curation provisioner; when ``None``, the isolation verification uses the
    real Docker adapter (which fails closed if the daemon is absent — see the
    "inspector unavailable" decision in the module).

    Parameters
    ----------
    session:
        Open :class:`~sqlalchemy.orm.Session`, injected by the caller. The service
        does not open or close it; it ``commit``s on completion and ``rollback``s
        on failure, preserving the previous state.
    provisioner:
        :class:`EnvironmentProvisioner` that prepares/verifies the environment.
        Default: :class:`CurationEnvironmentProvisioner` with the injected
        ``inspector``.
    inspector:
        **Read-only** :class:`~app.services.containment.ContainerInspector` used in
        the isolation verification. ``None`` in production (the real adapter is
        built lazily only when the verification runs); the tests inject a fake
        inspector. Ignored when ``provisioner`` is provided explicitly.
    compose_available:
        Callable that tells whether the curated environment material is available
        (Req. 9.4). Default: ``lambda: compose_path() is not None`` — the lab's
        ``docker/docker-compose.yml``. Injectable in the tests to exercise the "no
        material" path without touching the disk.
    caldera_client:
        :class:`~app.services.caldera.client.CalderaClient` used by the agent-
        health gate (task 8.1 — Req. 4) to list the Caldera agents of the expected
        group. ``None`` in production: it is built **lazily** (via
        :meth:`_get_caldera_client`) only when :meth:`verify_agents_health` runs,
        so importing this module never opens a socket. The tests inject a client
        backed by :class:`httpx.MockTransport`.
    agent_group:
        The agent group the health gate requires agents to be registered under
        (Req. 4.1/4.3). Defaults to the pipeline convention
        :data:`~app.services.caldera.client.DEFAULT_GROUP` (``"red"``), matching
        ``CalderaClient.create_operation``'s default target group.
    """

    def __init__(
        self,
        session: Session,
        *,
        provisioner: Optional[EnvironmentProvisioner] = None,
        inspector: Optional[ContainerInspector] = None,
        compose_available: Optional[Callable[[], bool]] = None,
        log_buffer: Optional[ProvisioningLogBuffer] = None,
        params_provider: Optional[Callable[[str], EnvironmentParameters]] = None,
        caldera_client: Optional[CalderaClient] = None,
        agent_group: str = DEFAULT_GROUP,
    ) -> None:
        self._session = session
        # Caldera client for the agent-health gate (task 8.1 — Req. 4). Injectable
        # (tests pass a MockTransport-backed client); when ``None`` it is built
        # lazily on first use so importing this module never opens a socket, the
        # same contract as :class:`CalderaClient` itself.
        self._caldera_client = caldera_client
        self._agent_group = agent_group
        self._provisioner: EnvironmentProvisioner = (
            provisioner
            if provisioner is not None
            else CurationEnvironmentProvisioner(inspector=inspector)
        )
        self._compose_available: Callable[[], bool] = (
            compose_available
            if compose_available is not None
            else (lambda: compose_path() is not None)
        )
        # Provisioning log buffer (task 5.1). In production, the caller/wiring seam
        # (task 6.x) injects the **process-level shared instance** — the same one
        # the WebSocket channel reads (``ws/channels``, task 4.3), so forwarding
        # ``on_log`` to the buffer feeds the screen's live terminal (Req. 2.2).
        # When ``None``, it creates a fresh own buffer: the provisioning still
        # buffers (nothing breaks), but the lines do not reach the shared channel —
        # useful in the tests that exercise the service in isolation and for calls
        # without a live terminal.
        self._log_buffer: ProvisioningLogBuffer = (
            log_buffer if log_buffer is not None else ProvisioningLogBuffer()
        )
        # Provider of the ``EnvironmentParameters`` (Stage 5 input — the Stage 4
        # output). Injectable in the tests to avoid touching disk/the
        # operationalization seam; in production (``None``) the same canonical path
        # that ``POST .../stage/5/provision`` uses is resolved **lazily** in
        # :meth:`_params_from_campaign` (no network and no AI — Req. 11.3, 11.4), so
        # importing this module never touches the operationalization.
        self._params_provider = params_provider

    @property
    def provisioner(self) -> EnvironmentProvisioner:
        """The :class:`EnvironmentProvisioner` used to prepare/verify."""
        return self._provisioner

    @property
    def log_buffer(self) -> ProvisioningLogBuffer:
        """The :class:`ProvisioningLogBuffer` that receives the provisioning lines.

        It is the same buffer the WebSocket channel (``ws/channels``) reads to feed
        the screen's live terminal (Req. 2.2); in production, the process-level
        shared instance is injected in the constructor.
        """
        return self._log_buffer

    def persisted_environment(self, campaign: str) -> Optional[ProvisionedEnvironment]:
        """Rebuild the Campaign's persisted :class:`ProvisionedEnvironment` output, if any.

        The Stage 5 read (``GET .../stage/5``, task 6.1) uses this **read-only**
        helper to get the
        :class:`~app.services.environment.health.HealthSummary` of a
        previous/current provisioning without bringing up containers: it loads the
        Campaign's latest :class:`ProvisionedEnvironmentRow` (portable JSON, Req.
        6.3) and rebuilds the model via ``model_validate``. It returns ``None`` when
        there is no persisted output for the Campaign yet — in that case the screen
        shows an empty (not approved) health summary without provisioning (Req.
        6.1).
        """
        row = self._environment_row(campaign)
        if row is None:
            return None
        return self._safe_environment_from_row(row)

    def _safe_environment_from_row(
        self, row: ProvisionedEnvironmentRow
    ) -> Optional[ProvisionedEnvironment]:
        """Rebuild a ProvisionedEnvironment from a persisted row, tolerating
        stale/incompatible JSON.

        A row persisted by an older schema (e.g. pre-English-rename, with
        Portuguese keys) no longer validates against the current model. Rather
        than raising a 500, we treat such a row as having no usable persisted
        output: the caller then falls back to (re)provisioning, which rewrites
        the row in the current format. Returns None on invalid/obsolete content.
        """
        try:
            return ProvisionedEnvironment.model_validate(row.content)
        except ValidationError as exc:
            logger.warning(
                "Discarding stale persisted environment for campaign %r "
                "(id=%s): incompatible content schema (%d error(s)).",
                getattr(row, "campaign_id", None),
                getattr(row, "id", None),
                exc.error_count(),
            )
            return None

    def auto_complete_stage4(self, campaign: str) -> AutoProvisioningResult:
        """Try to automatically complete the Stage 4 of the curated Campaign ``campaign``.

        Flow (all paths are read-only except the final completion):

        1. **Sequential gate (Req. 9.3).** If the Campaign's Stage 3 is **not**
           ``completed``, it does nothing and returns ``blocked_by_gate=True``.
        2. **Idempotency.** If Stage 4 is already ``completed``, it returns
           ``completed=True`` without re-persisting (the state is already the
           desired one).
        3. **Curated material (Req. 9.4).** If there is no environment material
           available, it returns ``missing_material=True`` without completing.
        4. **Isolation verification (Req. 5.6).** It calls
           ``provisioner.prepare(campaign)``. If the Docker inspector/daemon is
           unavailable (``RuntimeError``), it returns
           ``isolation_unavailable=True`` **without** completing (fail-closed). If
           it ran but the environment is not isolated, it returns
           ``isolated=False`` without completing.
        5. **Completion.** Isolated ⇒ *upsert* of the ``(campaign, 4)`` ``StageRun``
           to ``completed``/100 in a single transaction. A ``commit`` failure ⇒
           ``rollback`` + ``persistence_failed=True`` (previous state preserved).

        It does not raise on expected paths — the reason is always a typed signal
        in the result, like ``DagChoiceResult`` / ``StageCompletionResult``.
        """
        states = self._states_map(campaign)

        # (1) Sequential gate (Req. 9.3): only proceeds with Stage 3 completed.
        if states.get(STAGE_OPERATIONALIZATION) is not StageState.COMPLETED:
            return AutoProvisioningResult(
                campaign_id=campaign,
                completed=False,
                blocked_by_gate=True,
                message=_MSG_GATE,
            )

        # (2) Idempotency: already completed ⇒ nothing to re-persist.
        if states.get(STAGE_PROVISIONING) is StageState.COMPLETED:
            row = self._stage_run_row(campaign, STAGE_PROVISIONING)
            return AutoProvisioningResult(
                campaign_id=campaign,
                completed=True,
                isolated=True,
                stage_run_id=(row.id if row is not None else None),
            )

        # (3) Curated environment material available (Req. 9.4).
        if not self._compose_available():
            return AutoProvisioningResult(
                campaign_id=campaign,
                completed=False,
                missing_material=True,
                message=_MSG_NO_MATERIAL,
            )

        # (4) Isolation verification (Req. 5.6). Fail-closed when Docker is
        # unavailable: the isolation could not be verified, so we do not complete
        # (preserves the security contract without weakening it).
        try:
            result = self._provisioner.prepare(campaign)
        except RuntimeError:
            return AutoProvisioningResult(
                campaign_id=campaign,
                completed=False,
                isolated=False,
                isolation_unavailable=True,
                message=_MSG_ISOLATION_UNAVAILABLE,
            )

        if not result.ready:
            # Environment provably not isolated ⇒ Stage 4 does not complete; the
            # topology/reason already accompany the result for the screen (Req. 5.6).
            return AutoProvisioningResult(
                campaign_id=campaign,
                completed=False,
                isolated=False,
                message=_MSG_NOT_ISOLATED,
            )

        # (5) Isolated ⇒ complete Stage 4 (absolute upsert, transactional).
        stage_run = self._get_or_create_stage_run(
            campaign, stage=STAGE_PROVISIONING
        )
        stage_run.state = StageState.COMPLETED
        stage_run.progress = 100
        stage_run.error_message = None
        stage_run.failed_step = None
        stage_run.updated_at = _utcnow()

        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()
            return AutoProvisioningResult(
                campaign_id=campaign,
                completed=False,
                isolated=True,
                persistence_failed=True,
                message=_MSG_PERSISTENCE_FAILURE,
            )

        self._session.refresh(stage_run)
        return AutoProvisioningResult(
            campaign_id=campaign,
            completed=True,
            isolated=True,
            stage_run_id=stage_run.id,
        )

    def provision_stage5(self, campaign: str) -> ProvisioningStage5Result:
        """Trigger the **active** Stage 5 provisioning of the Campaign ``campaign`` (task 5.1).

        On entering Stage 5 (Simulation-environment creation), the Backend brings
        up the environment **on its own** — with no manual "provision" action (Req.
        1.1). This orchestration drives the :class:`EnvironmentProvisioner` seam's
        step pipeline (bring up containers → await health → load Abilities → verify
        isolation), forwarding each
        :class:`~app.services.environment.compose_executor.LogLine` emitted to the
        shared :class:`ProvisioningLogBuffer` — which the WebSocket channel reads
        to feed the screen's live terminal (Req. 2.2) — and updating the
        ``(campaign, stage=5)`` ``StageRun`` with the per-step progress and, on
        failure, ``failed_step`` + ``error_message`` (Req. 7.1).

        Flow:

        1. **Sequential gate (Req. 1.1).** If Stage 4 (Operationalization) is
           **not** ``completed``, it brings up nothing and returns
           ``blocked_by_gate=True`` — Stage 5 only starts with Stage 4 completed,
           exactly as :meth:`StageService.is_stage_startable` derives.
        2. **Stage 5 input.** It resolves the Campaign's
           :class:`~app.services.environment.operationalization.EnvironmentParameters`
           (the Stage 4 output) through the same canonical path the
           ``POST .../stage/5/provision`` endpoint uses (Req. 1.2).
        3. **``em_andamento``.** It marks the Stage 5 ``StageRun`` as
           ``em_andamento`` (progress 0, clearing ``failed_step``/``error_message``
           of a previous run) and **clears** the Campaign's log buffer
           (``log_buffer.clear``), so a fresh run restarts the ``seq`` numbering at
           1 (separating the previous run's live terminal from the new one — Req.
           2.5).
        4. **Streaming pipeline.** It calls
           :meth:`EnvironmentProvisioner.provision` passing an ``on_log`` that
           appends each :class:`LogLine` to the buffer (``log_buffer.append``) —
           the channel (task 4.3) reads the same buffer, so the lines reach the
           screen terminal (Req. 2.2).
        5. **``StageRun`` update according to the pipeline verdict (Req. 7.1).**

           * **Failure** (``environment.failed_step`` set): ``state = erro``,
             ``failed_step``/``error_message`` mirroring the output, progress at
             the failed step's milestone — Stage 5 does **not** complete and Stage
             6 stays blocked.
           * **Ready** (``environment.ready`` — the **three** prerequisites passed:
             health + Abilities + isolation, Req. 5.5): the ``StageRun`` is
             **completed** — ``state = completed``, progress 100, clearing
             ``failed_step``/``error_message`` — and the
             :class:`ProvisionedEnvironment` output is persisted as portable JSON in
             the same transaction (Req. 6.3). Only here does Stage 5 move to
             ``COMPLETED``, which — through the sequential gate of
             :meth:`StageService.is_stage_startable` — releases Stage 6 (Req. 6.2);
             while Stage 5 has not completed, Stage 6 stays blocked (Req. 6.4).

           The update is transactional (one ``commit``); ``rollback`` on a
           persistence failure preserves the previous state and returns
           ``persistence_failed=True``.

        It does not raise on expected paths — the reason is always a typed signal
        in the :class:`ProvisioningStage5Result`, like ``auto_complete_stage4``.
        """
        states = self._states_map(campaign)

        # (1) Sequential gate (Req. 1.1): Stage 5 only starts with Stage 4
        # (Operationalization) completed. While it is not, it brings up/persists
        # nothing.
        if states.get(STAGE_OPERATIONALIZATION) is not StageState.COMPLETED:
            return ProvisioningStage5Result(
                campaign_id=campaign,
                blocked_by_gate=True,
                error_message=_MSG_GATE_STAGE5,
            )

        # (1b) Idempotency (Req. 1.6): if the environment **has already** been
        # provisioned successfully for the Campaign and remains provisioned —
        # there is a ``ready`` persisted :class:`ProvisionedEnvironmentRow` **and**
        # the Stage 5 ``StageRun`` is already ``completed`` —, it **reuses** the
        # output without bringing up a new environment: it rebuilds the
        # :class:`ProvisionedEnvironment` from the persisted JSON and returns
        # ``ready=True`` **without** calling ``provisioner.provision`` nor
        # clearing/re-running. The completed ``StageRun`` confirms that the
        # completion (task 5.3) approved the three prerequisites — "ready+persisted"
        # is the reuse signal.
        if states.get(STAGE_ENVIRONMENT) is StageState.COMPLETED:
            row = self._environment_row(campaign)
            environment = (
                self._safe_environment_from_row(row)
                if row is not None and row.ready
                else None
            )
            if environment is not None:
                run = self._stage_run_row(campaign, STAGE_ENVIRONMENT)
                return ProvisioningStage5Result(
                    campaign_id=campaign,
                    ready=True,
                    environment=environment,
                    reused=True,
                    stage_run_id=(run.id if run is not None else None),
                )

        # (2) Stage 5 input: the EnvironmentParameters (Stage 4 output).
        params = self._params_from_campaign(campaign)

        # (3) Marks em_andamento and resets the Campaign's log buffer for the new
        # run (seq restarts at 1). Persists the start transactionally.
        stage_run = self._get_or_create_stage_run(campaign, stage=STAGE_ENVIRONMENT)
        stage_run.state = StageState.IN_PROGRESS
        stage_run.progress = 0
        stage_run.error_message = None
        stage_run.failed_step = None
        stage_run.updated_at = _utcnow()
        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()
            return ProvisioningStage5Result(
                campaign_id=campaign,
                persistence_failed=True,
                error_message=_MSG_PERSISTENCE_FAILURE_STAGE5,
            )

        self._log_buffer.clear(campaign)

        # (4) Streaming pipeline. The ``on_log`` forwards each line to the shared
        # buffer — the WebSocket channel (task 4.3) reads the same buffer and feeds
        # the screen's live terminal (Req. 2.2).
        def _on_log(line: LogLine) -> None:
            self._log_buffer.append(campaign, line)

        environment = self._provisioner.provision(params, on_log=_on_log)

        # Failure message shown on the screen/checklist and returned in the result.
        # On success it is ``None`` (``environment.error_message`` is ``None``); on
        # failure it is **overwritten** in the branch below by the console's last
        # error line (task 5.4 — Req. 7.5), so both the ``StageRun`` and the
        # :class:`ProvisioningStage5Result` carry the **same** text the live
        # terminal shows for the failed step.
        screen_error_message = environment.error_message

        # (5) Update the StageRun according to the pipeline verdict (Req. 7.1, 5.5,
        # 6.2, 6.4).
        if environment.failed_step is not None:
            # Some step failed ⇒ Stage 5 does **not** complete: ``erro`` with the
            # step/cause, progress at the failed step's milestone. Since Stage 5
            # does not become ``completed``, the sequential gate of
            # :meth:`StageService.is_stage_startable` keeps Stage 6 blocked
            # (``is_stage_startable(campaign, 6)`` is only ``True`` with Stage 5
            # ``COMPLETED``) — Req. 6.4.
            stage_run.state = StageState.ERROR
            stage_run.failed_step = environment.failed_step
            # Screen ↔ console consistency (task 5.4 — Req. 7.5): it derives the
            # ``StageRun`` ``error_message`` from the **last error line** the failed
            # step left in the same buffer the console renders (fed by ``_on_log``
            # above), guaranteeing by construction that the checklist/screen shows
            # exactly the terminal's text. It falls back to the
            # ``environment.error_message`` (already computed by the provisioner)
            # when the step left no error line in the buffer, so nothing regresses.
            screen_error_message = self._last_error_line_message(
                campaign, environment.failed_step, fallback=environment.error_message
            )
            stage_run.error_message = screen_error_message
            stage_run.progress = _PROGRESS_BY_STEP.get(environment.failed_step, 0)
        elif environment.ready:
            # The **three** prerequisites passed — ``environment.ready`` encodes
            # exactly ``failed_step is None`` **and** approved health **and** loaded
            # Abilities **and** approved isolation (Req. 5.5): it completes Stage 5
            # (``completed``/progress 100, clearing
            # ``failed_step``/``error_message``). Only here does Stage 5 move to
            # ``COMPLETED``, which — through the sequential gate — releases Stage 6
            # (Req. 6.2, 6.4).
            stage_run.state = StageState.COMPLETED
            stage_run.failed_step = None
            stage_run.error_message = None
            stage_run.progress = 100
        else:
            # Defensive path: the pipeline walked all the steps without a
            # ``failed_step`` but ``ready`` is ``False`` (some prerequisite did not
            # approve without recording a failure step). We do not complete — it
            # keeps ``em_andamento`` with advanced progress, so Stage 6 stays
            # blocked until the three prerequisites pass (Req. 6.4).
            stage_run.state = StageState.IN_PROGRESS
            stage_run.failed_step = None
            stage_run.error_message = None
            stage_run.progress = 90
        stage_run.updated_at = _utcnow()

        # (5b) Output persistence (Req. 6.3): when the pipeline becomes **ready**
        # (the three prerequisites passed), it persists the
        # :class:`ProvisionedEnvironment` as portable JSON associated with the
        # Campaign — a *latest-row-wins* upsert in a single row per Campaign — so
        # Stage 6 and the screen consume the output after the completion and the
        # idempotency (step 1b) reuses it. It is done in the **same transaction**
        # as the ``StageRun`` (a single ``commit``); a ``rollback`` on a
        # persistence failure preserves the previous state.
        if environment.ready:
            self._persist_environment(campaign, environment)

        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()
            return ProvisioningStage5Result(
                campaign_id=campaign,
                ready=environment.ready,
                failed_step=environment.failed_step,
                error_message=screen_error_message,
                environment=environment,
                persistence_failed=True,
            )

        self._session.refresh(stage_run)
        return ProvisioningStage5Result(
            campaign_id=campaign,
            ready=environment.ready,
            failed_step=environment.failed_step,
            error_message=screen_error_message,
            environment=environment,
            stage_run_id=stage_run.id,
        )

    # Intent alias: the Stage 5 provisioning is **started** with no manual action
    # (Req. 1.1). Exposes the same behavior with a name that reads better in the
    # wiring seam that triggers it on entering Stage 5.
    def start_provisioning(self, campaign: str) -> ProvisioningStage5Result:
        """Alias of :meth:`provision_stage5` — triggers the provisioning (Req. 1.1)."""
        return self.provision_stage5(campaign)

    # ---- agent-health gate (task 8.1 — Req. 4) ---------------------------

    def verify_agents_health(
        self, campaign: str, *, group: Optional[str] = None
    ) -> AgentHealthResult:
        """Verify the Caldera agents' health for the Stage 5 → Stage 6 gate (task 8.1).

        This is the environment check that **belongs to Stage 5** (Req. 5.1): it
        consults the running Caldera via :meth:`CalderaClient.list_agents` — using
        the **current v2 route** and the running environment's group name
        (Req. 4.4) — and derives a typed verdict that gates the advance to Stage 6.

        Decision (derived **only** from the expected group's agents):

        * **Release** iff there is **≥ 1** agent with ``alive=true`` in the group
          (Req. 4.1). Orphans may coexist with a live agent — their presence is
          still signalled in ``orphan_paws`` (Req. 4.3) but does **not** block
          when an active agent exists.
        * **Block — orphan (Req. 4.3):** no active agent **and** at least one
          orphan (``alive=false``) in the group ⇒ the typical "rebuilt image left a
          dead agent behind" case; the advance is blocked with a visible cause so
          it never stalls silently.
        * **Block — no active agent (Req. 4.2):** no active agent and no orphan ⇒
          the group simply has no usable agent.
        * **Block — pointing divergence (Req. 4.4/4.5):** a :class:`CalderaApiError`
          (e.g. 404 on the route) **or** a :class:`CalderaUnavailable`
          (non-resolvable host / connection failure) maps to a pointing-divergence
          block — the configured endpoint/host does not match the running
          environment.

        The verdict is **read-only** and side-effect-free (no DB write, no Caldera
        mutation), so Stage 6 can consume the **same** result without re-running
        the check (Req. 5.1, 5.2). The cause is surfaced on the **Stage 5** screen,
        never on the Stage 6 one (Req. 4.6).

        Parameters
        ----------
        campaign:
            The Campaign whose agent health is checked (carried in the result).
        group:
            The expected agent group; defaults to the service's ``agent_group``
            (the pipeline's ``"red"`` convention — Req. 4.1/4.3).

        Returns
        -------
        AgentHealthResult
            The typed verdict (released + active count + orphans + specific cause +
            pt-BR message). Never raises on the expected Caldera error paths — they
            map to a ``POINTING_DIVERGENCE`` block.
        """
        expected_group = group if group is not None else self._agent_group

        # Current v2 route + running-environment group name (Req. 4.4). A route/
        # host mismatch surfaces as a typed Caldera error, mapped to a pointing-
        # divergence block (Req. 4.5) — fail-closed, never released.
        try:
            agents = self._get_caldera_client().list_agents(expected_group)
        except (CalderaApiError, CalderaUnavailable):
            return AgentHealthResult(
                campaign_id=campaign,
                group=expected_group,
                released=False,
                active_agents=0,
                orphan_paws=[],
                cause=AgentHealthCause.POINTING_DIVERGENCE,
                message=_MSG_AGENTS_DIVERGENCE,
            )

        # ``list_agents(group)`` already filtered to the expected group.
        active = [agent for agent in agents if agent.alive]
        orphans = sorted(agent.paw for agent in agents if not agent.alive)

        # Release iff ≥ 1 active agent in the group (Req. 4.1). Orphans are always
        # signalled, even when released (Req. 4.3).
        if active:
            return AgentHealthResult(
                campaign_id=campaign,
                group=expected_group,
                released=True,
                active_agents=len(active),
                orphan_paws=orphans,
                cause=AgentHealthCause.NONE,
                message=None,
            )

        # No active agent ⇒ blocked. Distinguish the orphan case (Req. 4.3) from the
        # plain absence of any agent (Req. 4.2) so the Stage 5 screen shows the
        # precise cause (Req. 4.6).
        if orphans:
            cause = AgentHealthCause.ORPHAN
            message = _MSG_AGENTS_ORPHAN
        else:
            cause = AgentHealthCause.NO_ACTIVE_AGENT
            message = _MSG_AGENTS_NO_ACTIVE

        return AgentHealthResult(
            campaign_id=campaign,
            group=expected_group,
            released=False,
            active_agents=0,
            orphan_paws=orphans,
            cause=cause,
            message=message,
        )

    def _get_caldera_client(self) -> CalderaClient:
        """Return the agent-health :class:`CalderaClient`, building it lazily.

        In production (no injected client) a real client is created on first use —
        so importing this module never opens a socket, mirroring
        :class:`CalderaClient`'s own lazy-connect contract. The tests inject a
        client backed by :class:`httpx.MockTransport`.
        """
        if self._caldera_client is None:
            self._caldera_client = CalderaClient()
        return self._caldera_client

    def _last_error_line_message(
        self, campaign: str, step: str, *, fallback: Optional[str] = None
    ) -> Optional[str]:
        """Derive the ``StageRun`` ``error_message`` from the console's last error line (task 5.4 — Req. 7.5).

        It guarantees by construction the **screen ↔ console consistency** (Req.
        7.5): the failure message the checklist/screen shows
        (``StageRun.error_message``) is the **text of the very line** the live
        terminal shows for the failed step — and not a text derived in parallel
        that could diverge.

        It reads the Campaign's current log buffer
        (:meth:`ProvisioningLogBuffer.read` — the **same** buffer the WebSocket
        channel/console renders, fed by ``on_log`` during the pipeline), filters
        the lines emitted by the failed ``step`` and returns the ``text`` of the
        **last** error line (``stream in`` :data:`_ERROR_STREAMS`). The ``compose``
        executor and all the pipeline steps emit the failure cause as a ``stderr``
        line in the buffer, so the step's last error line carries exactly the cause
        the console shows.

        Fail-safe (defensive): when the failed step left **no** error line in the
        buffer (e.g. an isolated buffer the channel did not feed, or a step with no
        ``stderr``), it returns the ``fallback`` — typically the
        ``environment.error_message`` already computed by the provisioner — so
        nothing regresses: the message is still present, just not provably
        identical to the console line.

        Parameters
        ----------
        campaign:
            The Campaign whose log buffer is read.
        step:
            The step that failed (``environment.failed_step``) —
            ``"subir_conteineres"`` / ``"aguardar_saude"`` / ``"carregar_abilities"``
            / ``"verificar_isolamento"``.
        fallback:
            Message used when the step left no error line in the buffer (default
            ``None``) — usually ``environment.error_message``.

        Returns
        -------
        Optional[str]
            The ``text`` of the step's last error line in the buffer, or
            ``fallback`` when there is none.
        """
        last_error: Optional[str] = None
        for line in self._log_buffer.read(campaign):
            if line.step == step and line.stream in _ERROR_STREAMS:
                last_error = line.text
        return last_error if last_error is not None else fallback

    def _params_from_campaign(self, campaign: str) -> EnvironmentParameters:
        """Resolve the Campaign's :class:`EnvironmentParameters` (Stage 5 input).

        When a ``params_provider`` is injected (tests/seam), it delegates to it. In
        production (``None``), it resolves through the **same canonical path** the
        ``POST .../stage/5/provision`` endpoint uses — the curated DAG (Stage 3
        output) operationalized by :class:`CurationOperationalizer` — imported
        **lazily** here so as not to couple this module's import to the
        operationalization seam nor touch disk/network on import. No network and no
        AI on any path (Req. 11.3, 11.4).
        """
        if self._params_provider is not None:
            return self._params_provider(campaign)

        # Local import: avoids coupling this module's import to the
        # operationalization seam and keeps the import cheap/side-effect-free.
        from app.services.campaign_service import CaseService
        from app.services.dag.dag_service import DagService
        from app.services.environment.operationalizer import (
            CurationOperationalizer,
        )
        from app.services.stage.stage_service import StageService

        case_service = CaseService()
        stage_service = StageService(self._session, case_service=case_service)
        dag = DagService(session=self._session, case_service=case_service).get_dag(
            campaign
        )
        operationalizer = CurationOperationalizer(
            stage_service=stage_service, case_service=case_service
        )
        return operationalizer.operationalize(campaign, dag).parameters

    # ---- helpers (pure reads over StageRun) ------------------------------

    def _states_map(self, campaign_id: str) -> dict[int, StageState]:
        """Map ``stage -> StageState`` of the Campaign, from the ``StageRun``.

        Same read as ``StageService._stage_states_map``: a single snapshot drives
        the gate decision and the idempotency. Stages with no line are absent from
        the map; the callers treat them as ``nao_iniciado``. If there is more than
        one line for the same Stage, the one with the greatest ``id`` (most recent)
        prevails.
        """
        stmt = (
            select(StageRun.stage, StageRun.state)
            .where(StageRun.campaign_id == campaign_id)
            .order_by(StageRun.id.asc())
        )
        states: dict[int, StageState] = {}
        for stage, state in self._session.execute(stmt).all():
            states[stage] = state
        return states

    def _stage_run_row(self, campaign_id: str, stage: int) -> Optional[StageRun]:
        """Latest ``StageRun`` line of ``(campaign_id, stage)``, if any."""
        stmt = (
            select(StageRun)
            .where(StageRun.campaign_id == campaign_id)
            .where(StageRun.stage == stage)
            .order_by(StageRun.id.asc())
        )
        rows = self._session.execute(stmt).scalars().all()
        return rows[-1] if rows else None

    def _environment_row(self, campaign_id: str) -> Optional[ProvisionedEnvironmentRow]:
        """Latest persisted :class:`ProvisionedEnvironmentRow` of ``campaign_id``, if any.

        The Stage 5 output is persisted as portable JSON associated with the
        Campaign (Req. 6.3). The idempotency (Req. 1.6) reads this row to reuse the
        ready environment without bringing up a new one. If there is more than one
        row for the same Campaign (there should not be, given the *latest-row-wins*
        upsert), the one with the greatest ``id`` (most recent) prevails — the same
        pattern as ``_stage_run_row``.
        """
        stmt = (
            select(ProvisionedEnvironmentRow)
            .where(ProvisionedEnvironmentRow.campaign_id == campaign_id)
            .order_by(ProvisionedEnvironmentRow.id.asc())
        )
        rows = self._session.execute(stmt).scalars().all()
        return rows[-1] if rows else None

    def _persist_environment(
        self, campaign_id: str, environment: ProvisionedEnvironment
    ) -> ProvisionedEnvironmentRow:
        """Upsert the Stage 5 output as the Campaign's portable JSON (Req. 6.3).

        It serializes the :class:`ProvisionedEnvironment` via
        ``model_dump(mode="json")`` — the portable form that
        ``ProvisionedEnvironment.model_validate`` rebuilds in the idempotency (step
        1b) — and writes ``platform``/``origin`` as portable enums and ``ready`` as
        the approval verdict. It keeps **a single row per Campaign** (*latest-row-
        wins* upsert, the same upsert contract as the ``StageRun``): it updates the
        existing row when there is one, creates it when there is not.

        The row is only **added/updated** in the ``Session``; the ``commit`` is the
        caller's (``provision_stage5``), so the output persistence and the
        ``StageRun`` one share the **same transaction** — a ``rollback`` on failure
        preserves the previous state with no partial mutation.
        """
        now = _utcnow()
        content = environment.model_dump(mode="json")
        platform = PersistedEnvironmentPlatform(environment.platform.value)

        row = self._environment_row(campaign_id)
        if row is None:
            row = ProvisionedEnvironmentRow(campaign_id=campaign_id, created_at=now)
            self._session.add(row)
        row.platform = platform
        row.content = content
        row.origin = environment.origin
        row.ready = environment.ready
        row.updated_at = now
        return row

    def _get_or_create_stage_run(self, campaign_id: str, *, stage: int) -> StageRun:
        """Return the ``(campaign_id, stage)`` ``StageRun``, creating it if absent.

        One row per ``(campaign_id, stage)`` (the same upsert contract as
        ``StageService`` / ``SessionStateService`` / ``DagService``), so the
        completion updates the existing run instead of accumulating lines.

        If there is more than one line for the same ``(campaign_id, stage)``
        (residue of concurrent runs), the one with the **greatest ``id``** (most
        recent) prevails — *latest-row-wins*, consistent with :meth:`_states_map`
        and :meth:`_stage_run_row` (which the state read uses), so the update lands
        on the same line the UI reads.
        """
        stmt = (
            select(StageRun)
            .where(StageRun.campaign_id == campaign_id)
            .where(StageRun.stage == stage)
            .order_by(StageRun.id.desc())
        )
        stage_run = self._session.execute(stmt).scalars().first()
        if stage_run is None:
            stage_run = StageRun(campaign_id=campaign_id, stage=stage)
            self._session.add(stage_run)
        return stage_run


__all__ = [
    "EnvironmentStageService",
    "AutoProvisioningResult",
    "ProvisioningStage5Result",
    "AgentHealthResult",
    "AgentHealthCause",
]
