"""Environment phase services — Environment Provisioning.

The ``environment`` package implements the Environment Provisioning component
described in the design (the environment phase). It is split into focused modules:

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

_Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 9.2, 9.3, 9.4, 10.2_
"""

from __future__ import annotations

from app.services.environment.completion import (
    DbReportSink,
    ExecutionCompletionResult,
    ExecutionCompletionService,
    ReportSink,
)
from app.services.environment.execution import (
    ExecutionAbilityResult,
    ExecutionPhaseService,
    ExecutionResult,
    RunnerFactory,
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
from app.services.environment.log_buffer import (
    DEFAULT_MAX_LINES,
    ProvisioningLogBuffer,
)
from app.services.environment.measurement import (
    AbilityMeasurementResult,
    MeasurementService,
)
from app.services.environment.operationalization import (
    EnvironmentParameters,
    EnvironmentPlatform,
    RequiredService,
    TargetHost,
)
from app.services.environment.operationalizer import (
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
from app.services.environment.report import (
    ExecutionReport,
    SufficiencyMetrics,
    TechniqueMetric,
)
from app.services.environment.resource_gate import (
    DockerSdkHostResourceInspector,
    HostResourceInspector,
    HostResources,
    ProposedTarget,
    ResourceGate,
    ResourceGateVerdict,
    ResourceLimits,
    ResourcePlan,
    build_resource_plan,
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
    "DEFAULT_MAX_LINES",
    "HOST_CONTAINERS",
    # topology_builder (reusable pure construction)
    "HOST_ROLES",
    "NETWORK_MEMBERS",
    # measurement (procedural-sufficiency measurement + report — the execution phase, task 8.3)
    "AbilityMeasurementResult",
    # provisioner (seam + the environment phase DTOs + curation)
    "CalderaLoader",
    # health (read-only target-container health check — the environment phase, task 2.1)
    "ContainerStateInfo",
    "ContainerStateInspector",
    "ContainerStateNotFound",
    "ContainerVerdict",
    "CurationEnvironmentProvisioner",
    "DbReportSink",
    "DeprovisioningResult",
    "DockerSdkHostResourceInspector",
    "DockerSdkStateInspector",
    "EnvironmentParameters",
    # operationalization (environment-parameter DTOs — the operationalization phase, task 5.1)
    "EnvironmentPlatform",
    "EnvironmentProvisioner",
    "ExecutionAbilityResult",
    "ExecutionCompletionResult",
    "ExecutionCompletionService",
    "ExecutionPhaseService",
    "ExecutionReport",
    "ExecutionResult",
    "HealthSummary",
    "HealthVerdict",
    "HostResourceInspector",
    "HostResources",
    "HostView",
    "IsolationSummary",
    "MeasurementService",
    "NetworkView",
    # operationalizer (seam + dual-Output DTO of the operationalization phase — task 5.2)
    "OperationalizerByOrigin",
    "ProducedOperationalization",
    "ProposedTarget",
    "ProvisionedEnvironment",
    # log_buffer (per-campaign provisioning log buffer — the environment phase, task 4.2)
    "ProvisioningLogBuffer",
    "ProvisioningResult",
    # completion (deprovisioning + the execution phase completion — task 8.4)
    "ReportSink",
    "RequiredService",
    "ResourceGate",
    "ResourceGateVerdict",
    # resource_gate (deterministic host-resource authority — Axis 3, task 5.1)
    "ResourceLimits",
    "ResourcePlan",
    # execution (the execution phase Execution seam reusing OperationRunner — task 8.2)
    "RunnerFactory",
    "SufficiencyMetrics",
    "TargetHost",
    # report (metric and report DTOs — the execution phase, task 8.1)
    "TechniqueMetric",
    "TopologyResponse",
    "build_hosts",
    "build_mermaid",
    "build_networks",
    "build_resource_plan",
    "check_health",
    "compose_path",
    "container_names",
    "inspect_health",
    "ips_for_host",
]
