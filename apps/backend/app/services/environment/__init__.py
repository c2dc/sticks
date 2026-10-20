"""Stage 4 services — Environment Provisioning.

The ``environment`` package implements the Environment Provisioning component
described in the design (Stage 4). It is split into focused modules:

- ``topology_builder``: the **single source** of the contained-lab topology
  building logic — the static host/role/network map (derived from
  ``docker/docker-compose.yml`` and the containment map), the typed models
  (``HostView``/``NetworkView``/``TopologyResponse``) and the pure builders
  (``build_hosts``/``build_networks``/``build_mermaid``). Read-only, no Docker
  and no network. ``app/api/topology.py`` re-imports these names so the
  ``GET /api/campaigns/{campaign}/topology`` endpoint stays identical without
  duplicating the lab map.
- ``provisioner``: the ``EnvironmentProvisioner`` seam (a ``@runtime_checkable
  Protocol``), the typed result ``ProvisioningResult`` and the curation
  implementation ``CurationEnvironmentProvisioner`` — which **verifies** the
  isolation of the existing environment (reusing ``app/services/containment``
  ``inspect_isolation``) and **exposes** the topology, without starting/creating
  anything. The seam is designed so a future ``AiEnvironmentProvisioner`` fits
  with no redesign (Req. 10.2).
- ``stage_service``: the *session-bound* ``EnvironmentStageService``, which
  **auto-completes** a curated Campaign's Stage 4 — respecting the sequential
  gate (Stage 3 completed), requiring the curated environment material and
  **verifying isolation** via ``provisioner.prepare`` — and persists the
  ``stage=4`` ``StageRun`` as completed idempotently and transactionally
  (Req. 5.2, 5.3, 5.6, 9.2, 9.3, 9.4).

_Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 9.2, 9.3, 9.4, 10.2_
"""

from __future__ import annotations

from app.services.environment.execution import (
    ExecutionAbilityResult,
    ExecutionResult,
    ExecutionStageService,
    RunnerFactory,
)
from app.services.environment.operationalization import (
    EnvironmentParameters,
    EnvironmentPlatform,
    RequiredService,
    TargetHost,
)
from app.services.environment.operationalizer import (
    CurationOperationalizer,
    OperationalizerByOrigin,
    ProducedOperationalization,
)
from app.services.environment.provisioner import (
    CalderaLoader,
    ContainerVerdict,
    CurationEnvironmentProvisioner,
    DeprovisioningResult,
    EnvironmentProvisioner,
    IsolationSummary,
    ProvisionedEnvironment,
    ProvisioningResult,
)
from app.services.environment.health import (
    ContainerStateInfo,
    ContainerStateInspector,
    ContainerStateNotFound,
    DockerSdkStateInspector,
    HealthSummary,
    HealthVerdict,
    check_health,
    inspect_health,
)
from app.services.environment.completion import (
    CompletionStage6Result,
    CompletionStage6Service,
    DbReportSink,
    ReportSink,
)
from app.services.environment.log_buffer import (
    DEFAULT_MAX_LINES,
    ProvisioningLogBuffer,
)
from app.services.environment.measurement import (
    AbilityMeasurementResult,
    MeasurementService,
)
from app.services.environment.report import (
    ExecutionReport,
    SufficiencyMetrics,
    TechniqueMetric,
)
from app.services.environment.stage_service import (
    AutoProvisioningResult,
    EnvironmentStageService,
    ProvisioningStage5Result,
)
from app.services.environment.topology_builder import (
    HOST_CONTAINERS,
    HOST_ROLES,
    NETWORK_MEMBERS,
    HostView,
    NetworkView,
    TopologyResponse,
    build_hosts,
    build_mermaid,
    build_networks,
    compose_path,
    container_names,
    ips_for_host,
)

__all__ = [
    # operationalization (environment-parameter DTOs — Stage 4, task 5.1)
    "EnvironmentPlatform",
    "TargetHost",
    "RequiredService",
    "EnvironmentParameters",
    # operationalizer (seam + dual-Output DTO of Stage 4 — task 5.2)
    "OperationalizerByOrigin",
    "ProducedOperationalization",
    "CurationOperationalizer",
    # report (metric and report DTOs — Stage 6, task 8.1)
    "TechniqueMetric",
    "SufficiencyMetrics",
    "ExecutionReport",
    # measurement (procedural-sufficiency measurement + report — Stage 6, task 8.3)
    "AbilityMeasurementResult",
    "MeasurementService",
    # execution (Stage 6 Execution seam reusing OperationRunner — task 8.2)
    "RunnerFactory",
    "ExecutionAbilityResult",
    "ExecutionResult",
    "ExecutionStageService",
    # provisioner (seam + Stage 5 DTOs + curation)
    "CalderaLoader",
    "EnvironmentProvisioner",
    "ProvisioningResult",
    "CurationEnvironmentProvisioner",
    "ContainerVerdict",
    "IsolationSummary",
    "ProvisionedEnvironment",
    "DeprovisioningResult",
    # health (read-only target-container health check — Stage 5, task 2.1)
    "ContainerStateInfo",
    "ContainerStateNotFound",
    "ContainerStateInspector",
    "HealthVerdict",
    "HealthSummary",
    "check_health",
    "DockerSdkStateInspector",
    "inspect_health",
    # completion (deprovisioning + Stage 6 completion — task 8.4)
    "ReportSink",
    "DbReportSink",
    "CompletionStage6Result",
    "CompletionStage6Service",
    # log_buffer (per-campaign provisioning log buffer — Stage 5, task 4.2)
    "ProvisioningLogBuffer",
    "DEFAULT_MAX_LINES",
    # stage_service (Stage 4 auto-completion — task 4.2; active Stage 5
    # provisioning — task 5.1)
    "EnvironmentStageService",
    "AutoProvisioningResult",
    "ProvisioningStage5Result",
    # topology_builder (reusable pure construction)
    "HOST_ROLES",
    "HOST_CONTAINERS",
    "NETWORK_MEMBERS",
    "HostView",
    "NetworkView",
    "TopologyResponse",
    "ips_for_host",
    "container_names",
    "build_hosts",
    "build_networks",
    "build_mermaid",
    "compose_path",
]
