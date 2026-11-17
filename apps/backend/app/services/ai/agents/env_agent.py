"""Environment phase agent — ``AiEnvironmentProvisioner`` behind ``EnvironmentProvisioner``.

This module hosts the AI-origin implementation of the Environment phase
(Simulation-environment creation) seam. :class:`AiEnvironmentProvisioner`
satisfies the :class:`~app.services.environment.provisioner.EnvironmentProvisioner`
``@runtime_checkable`` ``Protocol`` **structurally** — it exposes
``origin``/``provision``/``deprovision`` (the Environment phase seam) plus ``prepare`` (the
Operationalization phase compatibility operation) with the **exact** names, parameter lists,
parameter types and return types the seam declares (Req. 2.3) — so it is
injectable in place of the
:class:`~app.services.environment.provisioner.CurationEnvironmentProvisioner`
with **no** downstream change (Req. 2.7, Property 6).

The guiding principle is unchanged: **the agent proposes, the existing safety
machinery disposes** (Req. 8, 9.3). The AI agent produces the compose/topology
from the :class:`~app.services.environment.operationalization.EnvironmentParameters`
(operationalization phase output) and drives the contained lab through the project's single lab
seams — the :func:`~app.services.ai.tools.tool_compose` /
:func:`~app.services.ai.tools.tool_caldera_load` wrappers over the unchanged
``ComposeExecutor`` / ``CalderaClient`` machinery — but it **reuses the Environment phase
containment/isolation machinery unchanged**: the read-only, fail-closed isolation
gate (:func:`~app.services.containment.inspect_isolation`, exposed by the curation
provisioner's verified pipeline) remains the **unchanged completion
pre-condition**. An AI-proposed environment that is **not** isolated does **not**
complete Environment phase (``ProvisionedEnvironment.ready`` stays ``False``), exactly like
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
the Operationalization phase operationalization artifact in the state, calls ``provision`` and
writes the ``ProvisionedEnvironment`` into the ``artifacts.environment`` slot of
the returned state delta (Req. 2.4, 11.5). The **persistence** of the
``agent_phase_outputs`` row (``origin=gerado_ia``) is kept **factored** out of the
node into :func:`build_phase_output` / :func:`persist_phase_output` so the
session-bound ``AgentRunService`` (tasks 15.x) can drive it with its injected
``Session`` — the node itself stays pure (no DB, no ``Session``) so the StateGraph
can run it and the checkpointer can snapshot its result. Building that row (and
the full ``AgentRunService`` / ``build_graph``) remains out of scope here.

_Requirements: 2.3, 2.4, 2.5, 8.3, 9.3, 11.5_
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.models.enums import AgentPhase, TranslationSource
from app.services.environment.compose_executor import LogLine
from app.services.environment.image_resolver import (
    ImageNeed,
    ImageResolution,
    ImageResolver,
    detect_target_os,
)
from app.services.environment.operationalization import (
    EnvironmentParameters,
    EnvironmentPlatform,
)
from app.services.environment.provisioner import (
    DeprovisioningResult,
    EnvironmentProvisioner,
    IsolationSummary,
    ProvisionedEnvironment,
    ProvisioningResult,
    SubphaseSummary,
)
from app.services.environment.resource_gate import (
    ProposedTarget,
    ResourceGate,
    build_resource_plan,
)
from app.services.environment.vm_confirmation_gate import VmConfirmationGate

if TYPE_CHECKING:  # pragma: no cover - typing only, no import-time cost
    from app.models.domain import AgentPhaseOutput
    from app.services.ai.state import AgentState
    from app.services.environment.provisioner import CurationEnvironmentProvisioner


__all__ = [
    "AiEnvironmentProvisioner",
    "build_phase_output",
    "detect_params_target_os",
    "node",
    "persist_phase_output",
    "resolve_image_for_params",
]


class AiEnvironmentProvisioner:
    """Environment phase agent — proposes/creates the contained environment, stamps ``gerado_ia``.

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
    ``ComposeExecutor`` / ``CalderaClient`` seams (Req. 9.3). The Environment phase
    **containment/isolation machinery is reused unchanged**: this agent delegates
    the ordered, observable provisioning pipeline — ``subir_conteineres`` →
    ``aguardar_saude`` → subphases build_online/runtime_isolated → the read-only, fail-closed
    ``verificar_isolamento`` gate — to the curation provisioner's verified pipeline
    (an **injected** collaborator, not a parent), then **re-stamps** the output's
    origin to ``AI_GENERATED``. The isolation gate therefore remains the
    **unchanged completion pre-condition** (Req. 8.3, 9.3): a non-isolated
    AI-proposed environment keeps ``ProvisionedEnvironment.ready`` ``False`` and
    does **not** complete Environment phase, exactly like curation.

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
        chat: Any | None = None,
        memory: Any | None = None,
        pipeline: CurationEnvironmentProvisioner | None = None,
        compose_tool: Callable[..., dict] | None = None,
        caldera_tool: Callable[..., dict] | None = None,
        resource_gate: ResourceGate | None = None,
        vm_confirmation_gate: VmConfirmationGate | None = None,
        trace_sink: list[dict] | None = None,
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
                carrying the **unchanged** Environment phase containment/isolation pipeline
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
            resource_gate: Optional deterministic, code-level host-resource
                authority (:class:`~app.services.environment.resource_gate.ResourceGate`).
                ``None`` defaults to a :class:`ResourceGate` with the design's
                locked limits. Like containment, the gate is a **code-level
                authority NOT entrusted to the agent**: :meth:`provision` builds a
                :class:`~app.services.environment.resource_gate.ResourcePlan` from
                the agent's proposed targets and evaluates it **before** any
                container is created; a rejected plan fails the environment phase
                with the exceeded-limit reasons and **no** container is created
                (design §D, task 6.2). Injectable so a caller can tighten/loosen
                the policy; the tests inject a gate with fabricated limits.
            vm_confirmation_gate: Optional code-level
                :class:`~app.services.environment.vm_confirmation_gate.VmConfirmationGate`.
                ``None`` defaults to a :class:`VmConfirmationGate`. Like
                containment and the ResourceGate, it is a **code-level authority
                NOT entrusted to the agent** (design §5): the application ALWAYS
                provisions a container (Container_Preference, Req. 14.6); if a
                campaign would require a VM, the gate requests the Researcher's
                explicit confirmation (Req. 14.7), records the VM as a future
                contract not executed (Req. 14.8) and keeps the effective platform
                CONTAINER with 0 VMs — it NEVER provisions a VM. Injectable so
                tests can substitute it.
        """
        self._chat = chat
        self._memory = memory
        self._pipeline = pipeline
        self._compose_tool = compose_tool
        self._caldera_tool = caldera_tool
        # The ResourceGate is a code-level authority (design §D/§F): the agent may
        # PROPOSE targets but the gate in code admits or rejects the plan — exactly
        # like the containment/isolation gate. Default to the design's locked
        # limits when not injected.
        self._resource_gate = resource_gate if resource_gate is not None else ResourceGate()
        # The VM_Confirmation_Gate is likewise a code-level authority NOT entrusted
        # to the agent (design §5): the application ALWAYS provisions a container
        # (Container_Preference, Req. 14.6). If a campaign would require a VM, the
        # gate requests the Researcher's explicit confirmation (Req. 14.7), records
        # the VM as a future contract not executed (Req. 14.8) and keeps the
        # effective platform CONTAINER with 0 VMs — it NEVER provisions a VM. The
        # gate is pure/deterministic; injectable so tests can substitute it.
        self._vm_confirmation_gate = (
            vm_confirmation_gate if vm_confirmation_gate is not None else VmConfirmationGate()
        )
        # Optional Audit_Trace sink injected by the LangGraph ``node`` wrapper,
        # carried as an instance attribute — NOT a ``provision`` parameter — so the
        # ``@runtime_checkable`` ``EnvironmentProvisioner`` seam signature
        # (``provision(params)``) is byte-for-byte unchanged (design §A). When
        # ``None`` no trace is captured and behavior is unchanged.
        self._trace_sink = trace_sink

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

    def _get_pipeline(self) -> CurationEnvironmentProvisioner:
        """Return the Environment phase pipeline, building the production one lazily.

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

    # -- ResourceGate planning (agent proposes; gate disposes) ------------

    @staticmethod
    def _proposed_targets(params: EnvironmentParameters) -> list[ProposedTarget]:
        """Map the agent's proposed targets to :class:`ProposedTarget`s for the gate.

        Projects the Operationalization phase :class:`EnvironmentParameters` the agent proposes —
        each :class:`~app.services.environment.operationalization.TargetHost` in
        ``params.hosts`` is one container the plan would provision — into the plain
        :class:`~app.services.environment.resource_gate.ProposedTarget` carriers
        :func:`~app.services.environment.resource_gate.build_resource_plan`
        aggregates into the :class:`ResourcePlan` the gate evaluates **before** any
        container is created (design §D, task 6.2).

        The per-host RAM/disk requests are not modeled on the Operationalization phase artifact yet,
        so each target contributes the gate's zero default for RAM/disk; the
        container **count** (one per proposed host) is the figure the gate bounds
        against ``max_containers``. A target name falls back to its container (or
        label) so the plan stays traceable.

        Args:
            params: The Operationalization phase output carrying the agent's proposed target hosts.

        Returns:
            One :class:`ProposedTarget` per proposed host, in order.
        """
        return [ProposedTarget(name=host.container or host.label) for host in params.hosts]

    # -- fixed-vs-agent-decided split (design §E) -------------------------

    # The fixed execution engine (design §E / §F): Caldera is the single,
    # system-imposed attack engine — compiled once from source and reused, NOT a
    # per-run agent decision. Surfaced verbatim in the trace so the audit shows
    # exactly where the system imposed the standard (task 6.1/6.3).
    _CALDERA_ENGINE = "caldera (compiled from source, reused)"

    def _fixed_infrastructure(self, params: EnvironmentParameters) -> dict:
        """Build the ``fixed_infrastructure`` block of the design §E trace.

        The part of the environment the **system** fixes — NOT the agent's to
        decide (design §E / §F): the fixed Caldera execution engine, the
        **isolable network structure** the system guarantees (the ``internal:
        true`` networks with no host/internet gateway, surfaced from the Operationalization phase
        ``network_context`` + the container platform) and the code-level
        :class:`ResourceLimits` the :class:`ResourceGate` enforces. Reading the
        limits off the gate keeps the trace honest about the exact policy the gate
        adjudicated against.

        Args:
            params: The Operationalization phase output carrying the platform + network context.

        Returns:
            A JSON-native ``fixed_infrastructure`` mapping for the trace.
        """
        limits = self._resource_gate.limits
        return {
            "execution_engine": self._CALDERA_ENGINE,
            # The system-guaranteed isolable network structure the agent decides
            # targets WITHIN — the ``internal: true`` segments with no host/internet
            # gateway. The per-node ``network_context`` notes describe that fixed
            # structure; the platform is container-only in this spec.
            "isolable_network_structure": {
                "internal": True,
                "platform": params.platform.value,
                "network_context": list(params.network_context),
            },
            "resource_limits": {
                "max_containers": limits.max_containers,
                "max_ram_gb": limits.max_ram_gb,
                "max_disk_gb": limits.max_disk_gb,
            },
        }

    @staticmethod
    def _agent_decided(params: EnvironmentParameters) -> dict:
        """Build the ``agent_decided`` block of the design §E trace.

        The agent's **core value** — the campaign's target scenario it designs
        WITHIN the fixed isolable structure (design §E): the **attacker node**
        (Kali or another, per campaign) and the **targets** (which systems, and
        the **vulnerabilities to install** on each — the host ``vulnerabilities``
        of the Operationalization phase parameters).

        The attacker node is the proposed host flagged as the attacker
        (``host_type`` naming ``attacker``/``kali``/``source``); when none is
        explicitly flagged it stays the explicit ``None`` sentinel (serialized to
        "not available") rather than guessing. Each remaining proposed host is one
        target system carrying its proposed vulnerabilities.

        Args:
            params: The Operationalization phase output carrying the agent's proposed hosts.

        Returns:
            A JSON-native ``agent_decided`` mapping for the trace.
        """

        def _is_attacker(host) -> bool:
            kind = (host.host_type or "").lower()
            return any(token in kind for token in ("attacker", "kali", "source"))

        attacker = next(
            (h for h in params.hosts if _is_attacker(h)),
            None,
        )
        attacker_node = (attacker.container or attacker.label) if attacker is not None else None
        targets = [
            {
                "system": host.container or host.label,
                "vulnerabilities": list(host.vulnerabilities),
            }
            for host in params.hosts
            if host is not attacker
        ]
        return {"attacker_node": attacker_node, "targets": targets}

    # -- EnvironmentProvisioner seam --------------------------------------

    def origin(self) -> TranslationSource:
        """Stamp ``gerado_ia`` on the Environment phase output (Req. 2.4, 16.3).

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
        (operationalization phase output) and drives the contained lab through the lab-control tool
        wrappers over the unchanged ``ComposeExecutor`` / ``CalderaClient`` seams
        (Req. 9.3). The ordered, observable provisioning pipeline —
        ``subir_conteineres`` → ``aguardar_saude`` → the build_online/runtime_isolated subphases → the
        read-only, fail-closed ``verificar_isolamento`` gate — is **reused
        unchanged** from the curation provisioner's verified pipeline (an injected
        collaborator), each step emitting :class:`LogLine` through ``on_log`` (Req.
        2).

        The isolation gate remains the **unchanged completion pre-condition** (Req.
        8.3, 9.3): the reused pipeline runs
        :func:`~app.services.containment.inspect_isolation` fail-closed, so an
        AI-proposed environment that is **not** isolated keeps
        :attr:`ProvisionedEnvironment.ready` ``False`` and does **not** complete
        Environment phase — exactly like curation. This agent does not re-implement, weaken
        or bypass that gate; it only **re-stamps** the produced output's
        :attr:`~ProvisionedEnvironment.origin` to
        :attr:`~app.models.enums.TranslationSource.AI_GENERATED` (Req. 2.4),
        preserving every verdict, summary and ``failed_step``/``error_message`` the
        pipeline computed (Req. 2.7).

        ``on_log`` is **keyword-only** with a *no-op* default, matching the seam so
        existing callers (``provision(params)``) stay valid unchanged.

        Args:
            params: The Operationalization phase output — target containers and platform of the
                environment to provision.
            on_log: Callback invoked for each :class:`LogLine` emitted by the
                steps, in order; *no-op* default for compatibility.

        Returns:
            The :class:`ProvisionedEnvironment` with ``origin = AI_GENERATED`` —
            ``ready is True`` when every prerequisite (including the unchanged
            isolation gate) passed, or carrying the pipeline's
            ``failed_step``/``error_message`` when one failed, keeping Environment phase
            incomplete (Req. 7.1, 8.3).
        """
        # --- ResourceGate: code-level authority, BEFORE any container ----------
        # The agent only PROPOSES targets; the gate in code admits or rejects the
        # plan (design §D/§F) — exactly like containment. Build the ResourcePlan
        # from the proposed targets and evaluate it BEFORE calling the pipeline, so
        # a rejected plan creates NO container (task 6.2, Req. 3.1, 12.2). The
        # verdict is recorded in the trace's ``processing.gates.resource_gate`` on
        # BOTH paths so the audit shows the gate ran.
        resource_verdict = self._resource_gate.evaluate(
            build_resource_plan(self._proposed_targets(params))
        )

        # --- VM_Confirmation_Gate: container always; VM is a future contract ---
        # Code-level authority NOT entrusted to the agent (design §5): the gate
        # adjudicates whether the campaign would require a VM. The application
        # ALWAYS provisions a container (Container_Preference, Req. 14.6); if a VM
        # would be required, the gate requests the Researcher's explicit
        # confirmation (Req. 14.7), records the VM as a future contract not
        # executed (Req. 14.8) and keeps the effective platform CONTAINER with 0
        # VMs. It NEVER provisions a VM and NEVER blocks the container path, so it
        # is evaluated unconditionally and the record is stamped on the output on
        # BOTH paths (resource-gate rejection and the normal pipeline).
        vm_confirmation = self._vm_confirmation_gate.evaluate(params)

        # The provisioned environment, captured so the trace's ``finally`` block
        # can surface the isolation verdict + subphases (design §E ``gates`` /
        # ``subphases``) on the approval path. It stays ``None`` on the
        # ResourceGate-rejection path (the pipeline is never called, so no
        # isolation/subphase outcome exists) and when ``provision`` raises before
        # the pipeline returns — the trace then carries the fail-closed empty
        # verdicts the serialization layer surfaces as "not approved".
        environment: ProvisionedEnvironment | None = None

        try:
            if not resource_verdict.approved:
                # Rejected: fail the environment phase with the exceeded-limit
                # reasons recorded in the trace and create NO container — the
                # pipeline is NOT called, so no ``subir_conteineres`` runs. The
                # output carries the AI origin and ``failed_step="resource_gate"``
                # with the reasons in ``error_message``, keeping ``ready`` False
                # (design §D). This mirrors the isolation gate's fail-closed block.
                # The effective platform is ALWAYS CONTAINER (Container_Preference,
                # Req. 14.6) — never ``params.platform`` which could carry a VM
                # marker. The ``vm_confirmation`` record carries the future-contract
                # verdict so the audit shows a VM request (if any) was recorded and
                # not executed (Req. 14.7, 14.8), even on the rejection path.
                environment = ProvisionedEnvironment(
                    campaign_id=params.campaign_id,
                    platform=EnvironmentPlatform.CONTAINER,
                    target_containers=[t.name for t in self._proposed_targets(params)],
                    vm_confirmation=vm_confirmation,
                    origin=self.origin(),
                    failed_step="resource_gate",
                    error_message="; ".join(resource_verdict.reasons),
                )
                return environment

            # Re-stamp ONLY the origin (Req. 2.4); every verdict, summary, health,
            # isolation, subphase outcome and failed_step/error_message the
            # unchanged pipeline computed is preserved verbatim (Req. 2.7).
            # ``ready`` is derived from those preserved fields, so the isolation
            # gate stays the unchanged completion pre-condition (Req. 8.3, 9.3).
            # Re-stamp the origin (Req. 2.4), force the effective platform to
            # CONTAINER (Container_Preference — Req. 14.6; the gate never lets a VM
            # become the effective platform) and attach the VM_Confirmation_Gate
            # record (Req. 14.7, 14.8). Every other verdict, summary, health,
            # isolation, subphase outcome and failed_step/error_message the
            # unchanged pipeline computed is preserved verbatim (Req. 2.7).
            environment = (
                self._get_pipeline()
                .provision(params, on_log=on_log)
                .model_copy(
                    update={
                        "origin": self.origin(),
                        "platform": EnvironmentPlatform.CONTAINER,
                        "vm_confirmation": vm_confirmation,
                    }
                )
            )
            return environment
        finally:
            # Append the AgentTrace for the environment phase (Req. 1.3, 2.2, 2.3)
            # whether provisioning completed or raised — so ``_fail_run`` finds the
            # partial trace captured up to the failure (design §A). The AI
            # environment agent drives the contained lab through the unchanged lab
            # seams rather than a direct LLM call, so the LLM processing elements
            # are recorded with an explicit ``None`` sentinel the serialization
            # layer maps to "not available".
            if self._trace_sink is not None:
                # The isolation + subphase summaries the reused pipeline computed
                # (design §E). They are the fail-closed defaults (not approved,
                # empty verdicts) whenever the pipeline was NOT reached — the
                # ResourceGate rejection path and any raise before it returned — so
                # the trace never implies an isolation check that did not run.
                isolation = environment.isolation if environment is not None else IsolationSummary()
                subphases = environment.subphases if environment is not None else SubphaseSummary()
                self._trace_sink.append(
                    {
                        "phase": AgentPhase.ENVIRONMENT.value,
                        "iteration": None,
                        "agent_input": {
                            "modeling": params.model_dump(mode="json"),
                            "retrieved_experience": None,
                            "attack_context": None,
                            "prior_artifacts": {
                                "operationalization_parameters": params.model_dump(mode="json")
                            },
                        },
                        "processing": {
                            "llm_prompt": None,
                            "llm_response": None,
                            "reasoning": None,
                            "tools_invoked": ["tool_compose", "tool_caldera_load"],
                            "repair_attempts": 0,
                            "repair_hints": None,
                            # The fixed-vs-agent-decided split (design §E): what the
                            # SYSTEM fixed (Caldera engine, isolable network
                            # structure, ResourceLimits) vs. what the AGENT decided
                            # (attacker node, targets + vulnerabilities) — so the
                            # audit shows exactly where the agent had freedom and
                            # where the system imposed the standard.
                            "fixed_infrastructure": self._fixed_infrastructure(params),
                            "agent_decided": self._agent_decided(params),
                            # The two gate verdicts (design §E ``gates`` block):
                            #   * resource_gate — the code-level host-resource
                            #     authority recorded on BOTH paths (task 6.2); the
                            #     exceeded-limit reasons when it rejected, empty
                            #     when it approved.
                            #   * isolation — the read-only, fail-closed isolation
                            #     gate (``containment.inspect_isolation`` →
                            #     ``IsolationSummary``) the reused pipeline ran
                            #     (agent proposes, gate disposes). Its per-container
                            #     verdicts are surfaced verbatim; fail-closed empty
                            #     when the pipeline was never reached.
                            "gates": {
                                "resource_gate": {
                                    "approved": resource_verdict.approved,
                                    "reasons": list(resource_verdict.reasons),
                                },
                                "isolation": {
                                    "approved": isolation.approved,
                                    "verdicts": [
                                        v.model_dump(mode="json") for v in isolation.verdicts
                                    ],
                                },
                            },
                            # The provisioner sub-phases (design §E ``subphases``):
                            # the ``SubphaseSummary`` (build online → close egress →
                            # verify egress lost → apply offline setup) plus the
                            # per-host egress verdicts, surfaced verbatim from what
                            # the pipeline produced (fail-closed empty when not
                            # reached).
                            "subphases": subphases.model_dump(mode="json"),
                        },
                    }
                )

    def deprovision(
        self,
        environment: ProvisionedEnvironment,
        *,
        on_log: Callable[[LogLine], None] = lambda _l: None,
    ) -> DeprovisioningResult:
        """Remove the provisioned environment (teardown at the end of Execution phase — Req. 8, 12).

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
            environment: The Environment phase output (:class:`ProvisionedEnvironment`) whose
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
        """Verify the environment of the *slug* ``campaign`` (operationalization phase compat.).

        Kept for the
        existing topology endpoints, matching the seam signature. It reuses the
        curation pipeline's **read-only** verification unchanged — it creates/starts
        nothing, only verifies the isolation of the existing environment and
        exposes the topology (Req. 9.3). The returned
        :class:`ProvisioningResult` carries no origin field, so it is passed through
        verbatim; the AI origin is stamped on the Environment phase artifacts produced by
        :meth:`provision` / :meth:`deprovision`.
        """
        return self._get_pipeline().prepare(campaign)


# Structural conformance guard (Req. 2.3, Property 6): a module-level assertion
# that any ``AiEnvironmentProvisioner`` instance satisfies the runtime-checkable
# ``EnvironmentProvisioner`` Protocol. ``_SEAM`` documents the intended type; the
# constructor is import-safe (all collaborators are optional and resolved lazily),
# so this touches no network, subprocess or daemon at import (Req. 7.4, 1.7).
_SEAM: EnvironmentProvisioner = AiEnvironmentProvisioner()
assert isinstance(_SEAM, EnvironmentProvisioner)  # noqa: S101 -- import-time structural guard of the Protocol seam; must stay a hard check


# ---------------------------------------------------------------------------
# LangGraph node wrapper + factored persistence hook (task 11.3)
# ---------------------------------------------------------------------------


def _read_environment_parameters(state: AgentState) -> EnvironmentParameters:
    """Read the Operationalization phase :class:`EnvironmentParameters` out of the agent state.

    The Operationalization phase operationalization node writes the serialized
    :class:`~app.services.environment.operationalizer.ProducedOperationalization`
    into ``state["artifacts"]["operationalization"]`` (its ``parameters`` field is
    the :class:`EnvironmentParameters` — the 2nd Operationalization phase artifact that drives
    Environment phase). This reconstructs the typed parameters from that serialized slot so
    the node can hand them to ``provision``.

    Raises:
        KeyError: when no operationalization artifact is present in the state —
            Environment phase cannot provision without the Operationalization phase parameters. The error
            names the missing slot so the Run_Service can record the failing node
            cause (Req. 14) without leaking any artifact content.
    """
    artifacts = state.get("artifacts") or {}
    operationalization = artifacts.get("operationalization")
    if not operationalization:
        raise KeyError(
            "artifacts.operationalization is missing; the Environment phase environment node "
            "requires the Operationalization phase operationalization output to provision."
        )
    # The parameters may already be a mapping (serialized model_dump) or, when a
    # node handed the live model forward, a model instance — accept both.
    parameters = operationalization.get("parameters")
    if parameters is None:
        raise KeyError(
            "artifacts.operationalization.parameters is missing; the Environment phase "
            "environment node requires the derived EnvironmentParameters."
        )
    if isinstance(parameters, EnvironmentParameters):
        return parameters
    return EnvironmentParameters.model_validate(parameters)


# ---------------------------------------------------------------------------
# Image resolution seam — detect Target_OS + resolve image (Req. 14.1-14.5)
# ---------------------------------------------------------------------------
#
# These module-level helpers wire the Environment phase env path to
# :func:`~app.services.environment.image_resolver.detect_target_os` and
# :class:`~app.services.environment.image_resolver.ImageResolver` **additively**,
# without changing the existing ``provision``/``node``/persistence flow. The env
# agent receives the concrete operationalization commands as the Operationalization phase
# :class:`EnvironmentParameters` (Req. 14.1); :func:`detect_params_target_os`
# detects the ``Target_OS`` from them (Req. 14.2); and
# :func:`resolve_image_for_params` resolves an image from the Docker_Image_Catalog
# FIRST (semantic then relational) → Docker Hub online only as the LAST-resort
# fallback (Req. 5.4, 14.4), with the Docker Hub lookup injected in the backend
# provisioning layer so the online call never crosses into the contained lab
# (Req. 14.5). They are available to the env path and to the tests; the effective
# container provisioning continues to reuse the existing pipeline (design
# Component 11).


def detect_params_target_os(params: EnvironmentParameters) -> str:
    """Detect the ``Target_OS`` (``linux`` | ``windows``) of ``params`` (Req. 14.1, 14.2).

    The env agent receives the concrete operationalization commands as the Operationalization phase
    :class:`EnvironmentParameters` (Req. 14.1). This projects their OS signal — the
    ``host_type`` of each target host, the inferred services and the per-node
    ``network_context`` notes — into platform/command tokens and delegates to
    :func:`~app.services.environment.image_resolver.detect_target_os`, which
    classifies Windows vs. Linux (windows tokens → windows; else linux — Req.
    14.2). The detection is **total**: a parameter set with no OS signal still
    yields a value in ``{linux, windows}`` (Linux default), consistent with
    :meth:`ProvisioningMapService._derive_target_os`.

    Args:
        params: The Operationalization phase output carrying the hosts/services/network context the
            concrete operationalization commands produced.

    Returns:
        ``"windows"`` when a Windows signal is present among the parameters, else
        ``"linux"``.
    """
    platforms: list[str] = []
    commands: list[str] = []
    for host in params.hosts:
        if host.host_type:
            platforms.append(host.host_type)
        commands.extend(host.vulnerabilities)
    commands.extend(service.description for service in params.services)
    commands.extend(params.network_context)
    return detect_target_os(platforms=platforms, commands=commands)


def resolve_image_for_params(
    params: EnvironmentParameters,
    resolver: ImageResolver,
) -> ImageResolution:
    """Resolve the container image for ``params`` via ``resolver`` (Req. 14.3, 14.4).

    Detects the ``Target_OS`` from the concrete operationalization commands
    (:func:`detect_params_target_os`) and builds the
    :class:`~app.services.environment.image_resolver.ImageNeed` — the target OS
    plus the services the environment must provide (the inferred
    :class:`~app.services.environment.operationalization.RequiredService`
    descriptions) — then asks the injected :class:`ImageResolver` to resolve it
    from the Docker_Image_Catalog FIRST (semantic then relational), falling back
    to Docker Hub online only when the catalog has no adequate candidate (Req.
    5.4). The resolver's Docker Hub lookup lives in the backend provisioning
    layer, so the online call never crosses into the contained lab (Req. 14.5).

    Args:
        params: The Operationalization phase output carrying the detected OS signal + services.
        resolver: The session-injected :class:`ImageResolver` (wired with the
            backend Docker Hub lookup in production, or offline-by-construction).

    Returns:
        The :class:`~app.services.environment.image_resolver.ImageResolution` with
        the resolved image and its source (``docker_hub`` | ``image_catalog``).

    Raises:
        ImageUnavailableError: when neither the catalog nor Docker Hub covers the
            need — the env agent surfaces it as a phase insufficiency the
            evaluator may route an environment refinement for (Req. 5.4, 14.4).
    """
    target_os = detect_params_target_os(params)
    services = tuple(
        service.description
        for service in params.services
        if service.description and service.description.strip()
    )
    return resolver.resolve(ImageNeed(target_os=target_os, services=services))


def node(state: AgentState) -> dict:
    """LangGraph node wrapping :class:`AiEnvironmentProvisioner` (Req. 2.4, 11.5).

    Builds the provisioner **lazily** (so importing this module and wiring the
    graph open no network/daemon — Req. 7.4), reads the Operationalization phase
    :class:`EnvironmentParameters` from the operationalization artifact in
    ``state``, calls :meth:`AiEnvironmentProvisioner.provision` and writes the
    resulting :class:`ProvisionedEnvironment` (``origin=gerado_ia``) into the
    ``artifacts.environment`` slot of the returned state delta. The unchanged
    read-only, fail-closed isolation gate inside ``provision`` stays the Environment phase
    completion pre-condition — a non-isolated environment is written back with
    ``ready is False`` and Environment phase does not complete (Req. 8.3, 9.3).

    The node returns **only** the delta it owns — the merged ``artifacts`` (with
    ``environment`` added, preserving the ``flow``/``operationalization`` the
    earlier nodes wrote) and an append-only ``history`` entry. It performs **no**
    persistence and holds **no** ``Session``: persisting the
    ``agent_phase_outputs`` row (``origin=gerado_ia``) is kept factored into
    :func:`build_phase_output` / :func:`persist_phase_output`, which the
    session-bound ``AgentRunService`` (tasks 15.x) drives after this node runs.
    This keeps the node pure so the StateGraph can run it and the PostgreSQL
    checkpointer can snapshot its result (Req. 4.1, 11.5).

    Args:
        state: The shared :class:`~app.services.ai.state.AgentState`; must carry
            ``artifacts.operationalization`` (the Operationalization phase output).

    Returns:
        A state delta ``{"artifacts": {...}, "history": [...]}`` — the merged
        ``artifacts`` including the serialized ``environment`` and a single
        append-only ``history`` trace entry for this node.
    """
    params = _read_environment_parameters(state)

    # Local trace sink injected into the provisioner: it appends its AgentTrace at
    # the provisioning call site; we stamp the current iteration and return it
    # under the append-only ``traces`` channel (design §A). The
    # ``Annotated[list[dict], add]`` reducer merges it with the
    # Attack flow/operationalization traces. The sink is passed through the constructor
    # (not ``provision``) so the seam signature is unchanged.
    sink: list[dict] = []
    provisioner = AiEnvironmentProvisioner(trace_sink=sink)
    environment = provisioner.provision(params)

    iteration = state.get("iteration", 0)
    for trace in sink:
        if trace.get("iteration") is None:
            trace["iteration"] = iteration

    prior_artifacts = dict(state.get("artifacts") or {})
    # ``mode="json"`` serializes the enums (origin/platform) to their plain string
    # values so the payload is a JSON-native dict — safe for the state snapshot the
    # checkpointer persists and for the ``agent_phase_outputs.payload`` JSON column.
    environment_payload = environment.model_dump(mode="json")

    return {
        "artifacts": {**prior_artifacts, "environment": environment_payload},
        "history": [
            {
                "node": "environment",
                "phase": AgentPhase.ENVIRONMENT.value,
                "origin": provisioner.origin().value,
                "ready": environment.ready,
                "iteration": state.get("iteration"),
            }
        ],
        "traces": sink,
    }


def build_phase_output(
    state: AgentState,
    *,
    iteration_id: int,
    params_id: int | None = None,
) -> AgentPhaseOutput:
    """Build the Environment phase ``agent_phase_outputs`` row for the environment artifact.

    Factored out of :func:`node` so the session-bound ``AgentRunService`` (tasks
    15.x) can persist it with its injected ``Session`` — the node stays pure
    (Req. 11.5). The row is stamped ``phase = ENVIRONMENT`` and
    ``origin = gerado_ia`` (the value the existing portable enum/CHECK already
    accepts, so persisting needs no schema migration — Req. 2.4, 2.5, 11.8), its
    ``payload`` carrying the serialized ``ProvisionedEnvironment`` the node wrote
    into ``artifacts.environment``.

    Only ``iteration_id`` is required (the FK to the owning iteration); ``flow_id``
    stays ``None`` (the environment phase links no ``AttackFlowRow``), and ``params_id`` is
    accepted optionally so the caller can link the ``EnvironmentParameters`` row
    when it has persisted one.

    Args:
        state: The agent state after :func:`node` ran — must carry the serialized
            ``artifacts.environment`` written by the node.
        iteration_id: FK of the owning ``agent_iterations`` row.
        params_id: Optional FK of the linked ``environment_parameters`` row.

    Returns:
        An unsaved :class:`~app.models.domain.AgentPhaseOutput` the caller adds to
        its ``Session`` and commits inside the per-iteration transaction.

    Raises:
        KeyError: when ``artifacts.environment`` is absent (the node did not run
            or produced no environment).
    """
    artifacts = state.get("artifacts") or {}
    payload = artifacts.get("environment")
    if payload is None:
        raise KeyError(
            "artifacts.environment is missing; build_phase_output must run after "
            "the Environment phase environment node has written its artifact."
        )

    # Local import keeps this module import-light (no SQLAlchemy model import at
    # module import time — Req. 1.7); the row is built only when a run persists.
    from app.models.domain import AgentPhaseOutput

    return AgentPhaseOutput(
        iteration_id=iteration_id,
        phase=AgentPhase.ENVIRONMENT,
        origin=TranslationSource.AI_GENERATED,
        payload=payload,
        flow_id=None,
        params_id=params_id,
    )


def persist_phase_output(
    session: Any,
    state: AgentState,
    *,
    iteration_id: int,
    params_id: int | None = None,
    trace: dict | None = None,
) -> AgentPhaseOutput:
    """Persist the Environment phase ``agent_phase_outputs`` row via an injected ``Session``.

    The thin persistence hook the ``AgentRunService`` (tasks 15.x) calls after the
    node runs: it builds the row with :func:`build_phase_output` and adds it to the
    caller's ``Session``. It deliberately does **not** commit — the Run_Service
    persists the whole iteration (iteration row + its phase outputs + the optional
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
        trace: The matching :class:`~app.services.ai.state.AgentTrace` dict the
            Run_Service looked up from ``final_state["traces"]`` by
            ``(phase, iteration)``. When supplied, one 1:1 ``agent_phase_traces``
            row is written for the just-flushed phase output in the SAME flush,
            inside the Run_Service's existing SAVEPOINT (Req. 2.1, 2.5). Absent
            processing elements are carried with the explicit ``None`` sentinel.

    Returns:
        The added (not yet committed) :class:`~app.models.domain.AgentPhaseOutput`.
    """
    phase_output = build_phase_output(state, iteration_id=iteration_id, params_id=params_id)
    session.add(phase_output)
    # Flush so the phase output's id is assigned for the 1:1 trace FK; the write
    # stays inside the Run_Service's per-iteration SAVEPOINT (no commit here).
    session.flush()

    # Write the 1:1 Audit_Trace row for this phase output in the same flush
    # (Req. 2.1, 2.5).
    from app.services.ai.agents._trace import persist_phase_trace

    persist_phase_trace(session, phase_output, trace)
    return phase_output
