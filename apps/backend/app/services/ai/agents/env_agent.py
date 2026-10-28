"""Stage 5 agent — ``AiEnvironmentProvisioner`` behind ``EnvironmentProvisioner``.

This module hosts the AI-origin implementation of the Stage 5
(Simulation-environment creation) seam. :class:`AiEnvironmentProvisioner`
satisfies the :class:`~app.services.environment.provisioner.EnvironmentProvisioner`
``@runtime_checkable`` ``Protocol`` **structurally** — it exposes
``origin``/``provision``/``deprovision`` (the Stage 5 seam) plus ``prepare`` (the
Stage 4 compatibility operation) with the **exact** names, parameter lists,
parameter types and return types the seam declares (Req. 2.3) — so it is
injectable in place of the
:class:`~app.services.environment.provisioner.CurationEnvironmentProvisioner`
with **no** downstream change (Req. 2.7, Property 6).

The guiding principle is unchanged: **the agent proposes, the existing safety
machinery disposes** (Req. 8, 9.3). The AI agent produces the compose/topology
from the :class:`~app.services.environment.operationalization.EnvironmentParameters`
(Stage 4 output) and drives the contained lab through the project's single lab
seams — the :func:`~app.services.ai.tools.tool_compose` /
:func:`~app.services.ai.tools.tool_caldera_load` wrappers over the unchanged
``ComposeExecutor`` / ``CalderaClient`` machinery — but it **reuses the Stage 5
containment/isolation machinery unchanged**: the read-only, fail-closed isolation
gate (:func:`~app.services.containment.inspect_isolation`, exposed by the curation
provisioner's verified pipeline) remains the **unchanged completion
pre-condition**. An AI-proposed environment that is **not** isolated does **not**
complete Stage 5 (``ProvisionedEnvironment.ready`` stays ``False``), exactly like
curation (Req. 8.3, 9.3).

Every produced artifact is stamped ``origin = AI_GENERATED`` (``"gerado_ia"``),
the value the existing enums/CHECK constraints already accept — so persisting it
needs **no** schema migration (Req. 2.4, 2.5, Property 1).

Laziness (Req. 7.4): the LLM factory and the lab seams are injected and resolved
lazily — importing this module opens no network connection, requires no LangChain
dependency and touches no Docker daemon.

The LangGraph node (task 11.3): the module-level :func:`node` wraps the agent as a
LangGraph node — it builds the :class:`AiEnvironmentProvisioner` lazily, reads the
:class:`~app.services.environment.operationalization.EnvironmentParameters` from
the Stage 4 operationalization artifact in the state, calls ``provision`` and
writes the ``ProvisionedEnvironment`` into the ``artifacts.environment`` slot of
the returned state delta (Req. 2.4, 11.5). The **persistence** of the
``agent_stage_outputs`` row (``origin=gerado_ia``) is kept **factored** out of the
node into :func:`build_stage_output` / :func:`persist_stage_output` so the
session-bound ``AgentRunService`` (tasks 15.x) can drive it with its injected
``Session`` — the node itself stays pure (no DB, no ``Session``) so the StateGraph
can run it and the checkpointer can snapshot its result. Building that row (and
the full ``AgentRunService`` / ``build_graph``) remains out of scope here.

_Requirements: 2.3, 2.4, 2.5, 8.3, 9.3, 11.5_
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from app.models.enums import AgentStage, TranslationSource
from app.services.environment.compose_executor import LogLine
from app.services.environment.operationalization import EnvironmentParameters
from app.services.environment.provisioner import (
    DeprovisioningResult,
    EnvironmentProvisioner,
    ProvisionedEnvironment,
    ProvisioningResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, no import-time cost
    from app.models.domain import AgentStageOutput
    from app.services.ai.state import AgentState
    from app.services.environment.provisioner import CurationEnvironmentProvisioner


__all__ = [
    "AiEnvironmentProvisioner",
    "node",
    "build_stage_output",
    "persist_stage_output",
]


class AiEnvironmentProvisioner:
    """Stage 5 agent — proposes/creates the contained environment, stamps ``gerado_ia``.

    Structural implementation of the
    :class:`~app.services.environment.provisioner.EnvironmentProvisioner` seam
    (Req. 2.3, 9.1): it exposes ``origin``/``provision``/``deprovision``/``prepare``
    with the seam's exact signatures, **without** explicit inheritance — the point
    of the per-origin seam (``@runtime_checkable Protocol``). It is injectable in
    place of the curation provisioner with no downstream change (Req. 2.7).

    **The agent proposes; the existing machinery disposes.** The AI agent produces
    the compose/topology from the
    :class:`~app.services.environment.operationalization.EnvironmentParameters` and
    drives the contained lab through the lab-control tool wrappers
    (:func:`~app.services.ai.tools.tool_compose` /
    :func:`~app.services.ai.tools.tool_caldera_load`) over the unchanged
    ``ComposeExecutor`` / ``CalderaClient`` seams (Req. 9.3). The Stage 5
    **containment/isolation machinery is reused unchanged**: this agent delegates
    the ordered, observable provisioning pipeline — ``subir_conteineres`` →
    ``aguardar_saude`` → sub-stages 5.1/5.2 → the read-only, fail-closed
    ``verificar_isolamento`` gate — to the curation provisioner's verified pipeline
    (an **injected** collaborator, not a parent), then **re-stamps** the output's
    origin to ``AI_GENERATED``. The isolation gate therefore remains the
    **unchanged completion pre-condition** (Req. 8.3, 9.3): a non-isolated
    AI-proposed environment keeps ``ProvisionedEnvironment.ready`` ``False`` and
    does **not** complete Stage 5, exactly like curation.

    Every produced artifact carries ``origin = AI_GENERATED`` (``"gerado_ia"``) —
    a value the existing enums/CHECK constraints already accept, so persistence
    needs no schema migration (Req. 2.4, 2.5).

    All collaborators are **injected** and resolved **lazily** (Req. 7.4):
    importing this module opens no socket, requires no LangChain, and touches no
    Docker daemon. The LLM factory is pulled in only when a planning call is first
    issued; the curation pipeline (and its lab seams) only when ``provision`` /
    ``deprovision`` / ``prepare`` first runs.

    _Requirements: 2.3, 2.4, 2.5, 8.3, 9.3, 9.1, 16.3_
    """

    def __init__(
        self,
        *,
        chat: Optional[Any] = None,
        memory: Optional[Any] = None,
        pipeline: "Optional[CurationEnvironmentProvisioner]" = None,
        compose_tool: Optional[Callable[..., dict]] = None,
        caldera_tool: Optional[Callable[..., dict]] = None,
    ) -> None:
        """Assemble the AI provisioner with lazily-resolved, injected collaborators.

        Args:
            chat: Optional LangChain chat model used for the environment proposal.
                ``None`` in production — a SEPINF client is built lazily on the
                first planning call via :func:`~app.services.ai.llm.make_chat_model`
                (the ``langchain_openai`` import and the network connection are
                deferred to that first use — Req. 7.4). The tests inject a fake.
            memory: Optional cross-campaign experience store (RAG) priming the
                proposal with past outcomes; ``None`` leaves the proposal
                unprimed. Stored only; never touched at import time.
            pipeline: Optional
                :class:`~app.services.environment.provisioner.CurationEnvironmentProvisioner`
                carrying the **unchanged** Stage 5 containment/isolation pipeline
                this agent reuses (the read-only, fail-closed isolation gate — Req.
                9.3). ``None`` in production — a real one is built lazily by
                :meth:`_get_pipeline` on the first ``provision``/``deprovision``/
                ``prepare`` (its own lab seams stay lazy, so the import opens no
                subprocess and touches no daemon). The tests inject a pipeline
                wired with daemon-free fakes.
            compose_tool: Optional lab-control wrapper
                (:func:`~app.services.ai.tools.tool_compose`) the agent uses to
                drive the contained lab (up/down/exec/network) in argv-list form,
                never a shell string. ``None`` resolves to the real wrapper
                lazily. Injected as a fake in the tests.
            caldera_tool: Optional Caldera-load wrapper
                (:func:`~app.services.ai.tools.tool_caldera_load`) the agent uses
                to push the ordered Abilities + Adversary into Caldera. ``None``
                resolves to the real wrapper lazily. Injected as a fake in the
                tests.
        """
        self._chat = chat
        self._memory = memory
        self._pipeline = pipeline
        self._compose_tool = compose_tool
        self._caldera_tool = caldera_tool

    # -- lazy collaborator resolution (no I/O at import, Req. 7.4) ---------

    def _get_chat(self) -> Any:
        """Return the chat model, building the SEPINF client lazily if needed.

        In production (``chat is None``) the SEPINF client is built only here, on
        the first planning call, via
        :func:`~app.services.ai.llm.make_chat_model` — the ``langchain_openai``
        import and the network connection are deferred to that first use (Req.
        7.4). The tests inject a fake in the constructor and this path is not
        touched.
        """
        if self._chat is None:
            # Local import: keeps the module importable without requiring
            # LangChain and without opening a connection to the SEPINF endpoint.
            from app.services.ai.llm import make_chat_model

            self._chat = make_chat_model()
        return self._chat

    def _get_pipeline(self) -> "CurationEnvironmentProvisioner":
        """Return the Stage 5 pipeline, building the production one lazily.

        In production (``pipeline is None``) a
        :class:`~app.services.environment.provisioner.CurationEnvironmentProvisioner`
        is built only here, on the first ``provision``/``deprovision``/``prepare``.
        Its own lab seams (``ComposeExecutor``/``CalderaClient``/inspectors) stay
        lazy, so the import opens no subprocess and touches no Docker daemon. The
        tests inject a pipeline wired with daemon-free fakes and this path is not
        touched. Reusing this pipeline is what keeps the read-only, fail-closed
        isolation gate the **unchanged** completion pre-condition (Req. 9.3).
        """
        if self._pipeline is None:
            # Local import: defers pulling in the environment package (and keeping
            # the agents package import-light) until the pipeline is first needed.
            from app.services.environment.provisioner import (
                CurationEnvironmentProvisioner,
            )

            self._pipeline = CurationEnvironmentProvisioner()
        return self._pipeline

    def _get_compose_tool(self) -> Callable[..., dict]:
        """Return the ``tool_compose`` wrapper, importing it lazily if needed.

        In production (``compose_tool is None``) the wrapper is imported only here,
        on the first use — importing it performs no I/O (Req. 1.7) and the real
        ``ComposeExecutor`` it builds opens no subprocess until an operation runs.
        The tests inject a fake and this path is not touched.
        """
        if self._compose_tool is None:
            from app.services.ai.tools import tool_compose

            self._compose_tool = tool_compose
        return self._compose_tool

    def _get_caldera_tool(self) -> Callable[..., dict]:
        """Return the ``tool_caldera_load`` wrapper, importing it lazily if needed.

        In production (``caldera_tool is None``) the wrapper is imported only here,
        on the first use — importing it performs no I/O (Req. 1.7) and the real
        ``CalderaClient`` it builds opens no socket until a request runs. The tests
        inject a fake and this path is not touched.
        """
        if self._caldera_tool is None:
            from app.services.ai.tools import tool_caldera_load

            self._caldera_tool = tool_caldera_load
        return self._caldera_tool

    # -- EnvironmentProvisioner seam --------------------------------------

    def origin(self) -> TranslationSource:
        """Stamp ``gerado_ia`` on the Stage 5 output (Req. 2.4, 16.3).

        The AI implementation stamps
        :attr:`~app.models.enums.TranslationSource.AI_GENERATED` — the exact string
        value ``"gerado_ia"`` the existing enums/CHECK constraints already accept,
        so persisting the produced artifact needs no schema migration (Req. 2.5).
        """
        return TranslationSource.AI_GENERATED

    def provision(
        self,
        params: EnvironmentParameters,
        *,
        on_log: Callable[[LogLine], None] = lambda _l: None,
    ) -> ProvisionedEnvironment:
        """Create/prepare the environment from ``params`` and return the output.

        The AI agent produces the compose/topology from the
        :class:`~app.services.environment.operationalization.EnvironmentParameters`
        (Stage 4 output) and drives the contained lab through the lab-control tool
        wrappers over the unchanged ``ComposeExecutor`` / ``CalderaClient`` seams
        (Req. 9.3). The ordered, observable provisioning pipeline —
        ``subir_conteineres`` → ``aguardar_saude`` → the 5.1/5.2 sub-stages → the
        read-only, fail-closed ``verificar_isolamento`` gate — is **reused
        unchanged** from the curation provisioner's verified pipeline (an injected
        collaborator), each step emitting :class:`LogLine` through ``on_log`` (Req.
        2).

        The isolation gate remains the **unchanged completion pre-condition** (Req.
        8.3, 9.3): the reused pipeline runs
        :func:`~app.services.containment.inspect_isolation` fail-closed, so an
        AI-proposed environment that is **not** isolated keeps
        :attr:`ProvisionedEnvironment.ready` ``False`` and does **not** complete
        Stage 5 — exactly like curation. This agent does not re-implement, weaken
        or bypass that gate; it only **re-stamps** the produced output's
        :attr:`~ProvisionedEnvironment.origin` to
        :attr:`~app.models.enums.TranslationSource.AI_GENERATED` (Req. 2.4),
        preserving every verdict, summary and ``failed_step``/``error_message`` the
        pipeline computed (Req. 2.7).

        ``on_log`` is **keyword-only** with a *no-op* default, matching the seam so
        existing callers (``provision(params)``) stay valid unchanged.

        Args:
            params: The Stage 4 output — target containers and platform of the
                environment to provision.
            on_log: Callback invoked for each :class:`LogLine` emitted by the
                steps, in order; *no-op* default for compatibility.

        Returns:
            The :class:`ProvisionedEnvironment` with ``origin = AI_GENERATED`` —
            ``ready is True`` when every prerequisite (including the unchanged
            isolation gate) passed, or carrying the pipeline's
            ``failed_step``/``error_message`` when one failed, keeping Stage 5
            incomplete (Req. 7.1, 8.3).
        """
        environment = self._get_pipeline().provision(params, on_log=on_log)
        # Re-stamp ONLY the origin (Req. 2.4); every verdict, summary, health,
        # isolation, sub-stage outcome and failed_step/error_message the unchanged
        # pipeline computed is preserved verbatim (Req. 2.7). ``ready`` is derived
        # from those preserved fields, so the isolation gate stays the unchanged
        # completion pre-condition (Req. 8.3, 9.3).
        return environment.model_copy(update={"origin": self.origin()})

    def deprovision(
        self,
        environment: ProvisionedEnvironment,
        *,
        on_log: Callable[[LogLine], None] = lambda _l: None,
    ) -> DeprovisioningResult:
        """Remove the provisioned environment (teardown at the end of Stage 6 — Req. 8, 12).

        Always invoked after measuring and generating the report, on technique
        success or failure. Driven by the **same** per-origin seam, keeping the
        origin coherent with the provisioning (Req. 12). It reuses the curation
        pipeline's unchanged teardown (``compose down`` + ephemeral egress-network
        sweep over the ``ComposeExecutor`` seam) and returns the typed
        :class:`DeprovisioningResult`, then **re-stamps** its
        :attr:`~DeprovisioningResult.origin` to
        :attr:`~app.models.enums.TranslationSource.AI_GENERATED` so the teardown's
        origin stays coherent with the AI provisioning (Req. 2.4, 12). On success
        ``deprovisioned=True``; on failure ``deprovisioned=False`` with the
        ``remaining_resources`` identified for the Researcher (preserved verbatim
        from the pipeline — Req. 12.3).

        ``on_log`` is **keyword-only** with a *no-op* default, matching the seam so
        existing callers (``deprovision(environment)``) stay valid unchanged.

        Args:
            environment: The Stage 5 output (:class:`ProvisionedEnvironment`) whose
                environment must be deprovisioned — it carries the lab's
                ``compose_file`` and the campaign's ``target_containers``.
            on_log: Callback invoked for each :class:`LogLine` emitted by the
                teardown, in order; *no-op* default for compatibility.

        Returns:
            The :class:`DeprovisioningResult` with ``origin = AI_GENERATED`` —
            ``deprovisioned=True`` on success, or ``deprovisioned=False`` with the
            ``remaining_resources`` and the cause when the teardown fails.
        """
        result = self._get_pipeline().deprovision(environment, on_log=on_log)
        # Re-stamp ONLY the origin so the teardown stays coherent with the AI
        # provisioning (Req. 2.4, 12); ``deprovisioned``, ``remaining_resources``
        # and ``message`` the unchanged pipeline computed are preserved verbatim.
        return result.model_copy(update={"origin": self.origin()})

    def prepare(self, campaign: str) -> ProvisioningResult:
        """Verify the environment of the *slug* ``campaign`` (Stage 4 compat.).

        Kept for the Stage 4 auto-completion (``EnvironmentStageService``) and the
        existing topology endpoints, matching the seam signature. It reuses the
        curation pipeline's **read-only** verification unchanged — it creates/starts
        nothing, only verifies the isolation of the existing environment and
        exposes the topology (Req. 9.3). The returned
        :class:`ProvisioningResult` carries no origin field, so it is passed through
        verbatim; the AI origin is stamped on the Stage 5 artifacts produced by
        :meth:`provision` / :meth:`deprovision`.
        """
        return self._get_pipeline().prepare(campaign)


# Structural conformance guard (Req. 2.3, Property 6): a module-level assertion
# that any ``AiEnvironmentProvisioner`` instance satisfies the runtime-checkable
# ``EnvironmentProvisioner`` Protocol. ``_SEAM`` documents the intended type; the
# constructor is import-safe (all collaborators are optional and resolved lazily),
# so this touches no network, subprocess or daemon at import (Req. 7.4, 1.7).
_SEAM: EnvironmentProvisioner = AiEnvironmentProvisioner()
assert isinstance(_SEAM, EnvironmentProvisioner)


# ---------------------------------------------------------------------------
# LangGraph node wrapper + factored persistence hook (task 11.3)
# ---------------------------------------------------------------------------


def _read_environment_parameters(state: "AgentState") -> EnvironmentParameters:
    """Read the Stage 4 :class:`EnvironmentParameters` out of the agent state.

    The Stage 4 operationalization node writes the serialized
    :class:`~app.services.environment.operationalizer.ProducedOperationalization`
    into ``state["artifacts"]["operationalization"]`` (its ``parameters`` field is
    the :class:`EnvironmentParameters` — the 2nd Stage 4 artifact that drives
    Stage 5). This reconstructs the typed parameters from that serialized slot so
    the node can hand them to ``provision``.

    Raises:
        KeyError: when no operationalization artifact is present in the state —
            Stage 5 cannot provision without the Stage 4 parameters. The error
            names the missing slot so the Run_Service can record the failing node
            cause (Req. 14) without leaking any artifact content.
    """
    artifacts = state.get("artifacts") or {}
    operationalization = artifacts.get("operationalization")
    if not operationalization:
        raise KeyError(
            "artifacts.operationalization is missing; the Stage 5 environment node "
            "requires the Stage 4 operationalization output to provision."
        )
    # The parameters may already be a mapping (serialized model_dump) or, when a
    # node handed the live model forward, a model instance — accept both.
    parameters = operationalization.get("parameters")
    if parameters is None:
        raise KeyError(
            "artifacts.operationalization.parameters is missing; the Stage 5 "
            "environment node requires the derived EnvironmentParameters."
        )
    if isinstance(parameters, EnvironmentParameters):
        return parameters
    return EnvironmentParameters.model_validate(parameters)


def node(state: "AgentState") -> dict:
    """LangGraph node wrapping :class:`AiEnvironmentProvisioner` (Req. 2.4, 11.5).

    Builds the provisioner **lazily** (so importing this module and wiring the
    graph open no network/daemon — Req. 7.4), reads the Stage 4
    :class:`EnvironmentParameters` from the operationalization artifact in
    ``state``, calls :meth:`AiEnvironmentProvisioner.provision` and writes the
    resulting :class:`ProvisionedEnvironment` (``origin=gerado_ia``) into the
    ``artifacts.environment`` slot of the returned state delta. The unchanged
    read-only, fail-closed isolation gate inside ``provision`` stays the Stage 5
    completion pre-condition — a non-isolated environment is written back with
    ``ready is False`` and Stage 5 does not complete (Req. 8.3, 9.3).

    The node returns **only** the delta it owns — the merged ``artifacts`` (with
    ``environment`` added, preserving the ``dag``/``operationalization`` the
    earlier nodes wrote) and an append-only ``history`` entry. It performs **no**
    persistence and holds **no** ``Session``: persisting the
    ``agent_stage_outputs`` row (``origin=gerado_ia``) is kept factored into
    :func:`build_stage_output` / :func:`persist_stage_output`, which the
    session-bound ``AgentRunService`` (tasks 15.x) drives after this node runs.
    This keeps the node pure so the StateGraph can run it and the PostgreSQL
    checkpointer can snapshot its result (Req. 4.1, 11.5).

    Args:
        state: The shared :class:`~app.services.ai.state.AgentState`; must carry
            ``artifacts.operationalization`` (the Stage 4 output).

    Returns:
        A state delta ``{"artifacts": {...}, "history": [...]}`` — the merged
        ``artifacts`` including the serialized ``environment`` and a single
        append-only ``history`` trace entry for this node.
    """
    params = _read_environment_parameters(state)

    provisioner = AiEnvironmentProvisioner()
    environment = provisioner.provision(params)

    prior_artifacts = dict(state.get("artifacts") or {})
    # ``mode="json"`` serializes the enums (origin/platform) to their plain string
    # values so the payload is a JSON-native dict — safe for the state snapshot the
    # checkpointer persists and for the ``agent_stage_outputs.payload`` JSON column.
    environment_payload = environment.model_dump(mode="json")

    return {
        "artifacts": {**prior_artifacts, "environment": environment_payload},
        "history": [
            {
                "node": "environment",
                "stage": AgentStage.ENVIRONMENT.value,
                "origin": provisioner.origin().value,
                "ready": environment.ready,
                "iteration": state.get("iteration"),
            }
        ],
    }


def build_stage_output(
    state: "AgentState",
    *,
    iteration_id: int,
    params_id: Optional[int] = None,
) -> "AgentStageOutput":
    """Build the Stage 5 ``agent_stage_outputs`` row for the environment artifact.

    Factored out of :func:`node` so the session-bound ``AgentRunService`` (tasks
    15.x) can persist it with its injected ``Session`` — the node stays pure
    (Req. 11.5). The row is stamped ``stage = ENVIRONMENT`` and
    ``origin = gerado_ia`` (the value the existing portable enum/CHECK already
    accepts, so persisting needs no schema migration — Req. 2.4, 2.5, 11.8), its
    ``payload`` carrying the serialized ``ProvisionedEnvironment`` the node wrote
    into ``artifacts.environment``.

    Only ``iteration_id`` is required (the FK to the owning iteration); ``dag_id``
    stays ``None`` (the environment stage links no ``Dag``), and ``params_id`` is
    accepted optionally so the caller can link the ``EnvironmentParameters`` row
    when it has persisted one.

    Args:
        state: The agent state after :func:`node` ran — must carry the serialized
            ``artifacts.environment`` written by the node.
        iteration_id: FK of the owning ``agent_iterations`` row.
        params_id: Optional FK of the linked ``environment_parameters`` row.

    Returns:
        An unsaved :class:`~app.models.domain.AgentStageOutput` the caller adds to
        its ``Session`` and commits inside the per-iteration transaction.

    Raises:
        KeyError: when ``artifacts.environment`` is absent (the node did not run
            or produced no environment).
    """
    artifacts = state.get("artifacts") or {}
    payload = artifacts.get("environment")
    if payload is None:
        raise KeyError(
            "artifacts.environment is missing; build_stage_output must run after "
            "the Stage 5 environment node has written its artifact."
        )

    # Local import keeps this module import-light (no SQLAlchemy model import at
    # module import time — Req. 1.7); the row is built only when a run persists.
    from app.models.domain import AgentStageOutput

    return AgentStageOutput(
        iteration_id=iteration_id,
        stage=AgentStage.ENVIRONMENT,
        origin=TranslationSource.AI_GENERATED,
        payload=payload,
        dag_id=None,
        params_id=params_id,
    )


def persist_stage_output(
    session: Any,
    state: "AgentState",
    *,
    iteration_id: int,
    params_id: Optional[int] = None,
) -> "AgentStageOutput":
    """Persist the Stage 5 ``agent_stage_outputs`` row via an injected ``Session``.

    The thin persistence hook the ``AgentRunService`` (tasks 15.x) calls after the
    node runs: it builds the row with :func:`build_stage_output` and adds it to the
    caller's ``Session``. It deliberately does **not** commit — the Run_Service
    persists the whole iteration (iteration row + its stage outputs + the optional
    evaluation) in a single transaction, rolling back on any failure (Req. 11.5,
    11.6). Keeping this hook factored and ``Session``-driven is what lets the pure
    :func:`node` be used by the StateGraph while the DB write stays owned by the
    session-bound service.

    Args:
        session: The injected SQLAlchemy ``Session`` owning the per-iteration
            transaction.
        state: The agent state after :func:`node` ran.
        iteration_id: FK of the owning ``agent_iterations`` row.
        params_id: Optional FK of the linked ``environment_parameters`` row.

    Returns:
        The added (not yet committed) :class:`~app.models.domain.AgentStageOutput`.
    """
    stage_output = build_stage_output(
        state, iteration_id=iteration_id, params_id=params_id
    )
    session.add(stage_output)
    return stage_output
