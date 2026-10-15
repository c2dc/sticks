"""Serviços do Estágio 4 — Provisionamento do Ambiente.

O pacote ``environment`` implementa o componente de Provisionamento do Ambiente
descrito no design (Estágio 4). Está dividido em dois módulos de responsabilidade
focada:

- ``topologia_builder``: a **fonte única** da lógica de construção da topologia
  do laboratório contido — o mapa estático de hosts/papéis/redes (derivado de
  ``docker/docker-compose.yml`` e do mapa de containment), os modelos tipados
  (``HostView``/``NetworkView``/``TopologyResponse``) e os construtores puros
  (``construir_hosts``/``construir_redes``/``montar_mermaid``). Read-only, sem
  Docker nem rede. ``app/api/topologia.py`` reimporta destes nomes, de modo que o
  endpoint ``GET /api/casos/{caso}/topologia`` continue idêntico sem duplicar o
  mapa do laboratório.
- ``provisionador``: a **costura** ``ProvisionadorDeAmbiente`` (um
  ``@runtime_checkable Protocol``), o resultado tipado ``ResultadoProvisionamento``
  e a implementação de curadoria ``CuradoriaProvisionadorDeAmbiente`` — que
  **verifica** o isolamento do ambiente existente (reusando
  ``app/services/containment`` ``inspect_isolation``) e **expõe** a topologia,
  sem iniciar/criar nada. A costura é desenhada para uma futura
  ``AiProvisionadorDeAmbiente`` encaixar sem redesenho (Req. 10.2).
- ``stage_service``: o serviço *session-bound* ``EnvironmentStageService``, que
  **conclui automaticamente** o Estágio 4 de um Caso curado — respeitando o gate
  sequencial (Estágio 3 ``concluido``), exigindo o material curado de ambiente e
  **verificando o isolamento** via ``provisionador.preparar`` — e persiste o
  ``StageRun`` de ``estagio=4`` como ``concluido`` de forma idempotente e
  transacional (Req. 5.2, 5.3, 5.6, 9.2, 9.3, 9.4).

_Requisitos: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 9.2, 9.3, 9.4, 10.2_
"""

from __future__ import annotations

from app.services.environment.execucao import (
    ExecucaoAbilityResult,
    ExecucaoResult,
    ExecucaoStageService,
    RunnerFactory,
)
from app.services.environment.operacionalizacao import (
    HostAlvo,
    ParametrosDeAmbiente,
    PlataformaAmbiente,
    ServicoRequerido,
)
from app.services.environment.operacionalizador import (
    CuradoriaOperacionalizador,
    OperacionalizacaoProduzida,
    OperacionalizadorPorOrigem,
)
from app.services.environment.provisionador import (
    AmbienteProvisionado,
    CalderaLoader,
    CuradoriaProvisionadorDeAmbiente,
    DesprovisionamentoResult,
    IsolationResumo,
    ProvisionadorDeAmbiente,
    ResultadoProvisionamento,
    VereditoContainer,
)
from app.services.environment.saude import (
    ContainerStateInfo,
    ContainerStateInspector,
    ContainerStateNotFound,
    DockerSdkStateInspector,
    ResumoSaude,
    VereditoSaude,
    inspect_saude,
    verificar_saude,
)
from app.services.environment.conclusao import (
    ConclusaoEstagio6Result,
    ConclusaoEstagio6Service,
    RelatorioSink,
)
from app.services.environment.log_buffer import (
    DEFAULT_MAX_LINHAS,
    ProvisioningLogBuffer,
)
from app.services.environment.medicao import (
    MedicaoService,
    ResultadoAbility,
)
from app.services.environment.relatorio import (
    MetricasSuficiencia,
    MetricaTecnica,
    RelatorioExecucao,
)
from app.services.environment.stage_service import (
    EnvironmentStageService,
    ProvisionamentoAutoResult,
    ProvisionamentoEstagio5Result,
)
from app.services.environment.topologia_builder import (
    HOST_CONTAINERS,
    HOST_ROLES,
    NETWORK_MEMBERS,
    HostView,
    NetworkView,
    TopologyResponse,
    compose_path,
    construir_hosts,
    construir_redes,
    container_names,
    ips_for_host,
    montar_mermaid,
)

__all__ = [
    # operacionalizacao (DTOs de parâmetros de ambiente — Estágio 4, task 5.1)
    "PlataformaAmbiente",
    "HostAlvo",
    "ServicoRequerido",
    "ParametrosDeAmbiente",
    # operacionalizador (costura + DTO da Saída dupla do Estágio 4 — task 5.2)
    "OperacionalizadorPorOrigem",
    "OperacionalizacaoProduzida",
    "CuradoriaOperacionalizador",
    # relatorio (DTOs de métricas e relatório — Estágio 6, task 8.1)
    "MetricaTecnica",
    "MetricasSuficiencia",
    "RelatorioExecucao",
    # medicao (medição de suficiência procedural + relatório — Estágio 6, task 8.3)
    "ResultadoAbility",
    "MedicaoService",
    # execucao (costura da Execução do Estágio 6 reusando OperationRunner — task 8.2)
    "RunnerFactory",
    "ExecucaoAbilityResult",
    "ExecucaoResult",
    "ExecucaoStageService",
    # provisionador (costura + DTOs do Estágio 5 + curadoria)
    "CalderaLoader",
    "ProvisionadorDeAmbiente",
    "ResultadoProvisionamento",
    "CuradoriaProvisionadorDeAmbiente",
    "VereditoContainer",
    "IsolationResumo",
    "AmbienteProvisionado",
    "DesprovisionamentoResult",
    # saude (verificação read-only de saúde dos contêineres-alvo — Estágio 5, task 2.1)
    "ContainerStateInfo",
    "ContainerStateNotFound",
    "ContainerStateInspector",
    "VereditoSaude",
    "ResumoSaude",
    "verificar_saude",
    "DockerSdkStateInspector",
    "inspect_saude",
    # conclusao (desprovisionamento + conclusão do Estágio 6 — task 8.4)
    "RelatorioSink",
    "ConclusaoEstagio6Result",
    "ConclusaoEstagio6Service",
    # log_buffer (buffer de logs de provisionamento por caso — Estágio 5, task 4.2)
    "ProvisioningLogBuffer",
    "DEFAULT_MAX_LINHAS",
    # stage_service (auto-conclusão do Estágio 4 — task 4.2; provisionamento
    # ativo do Estágio 5 — task 5.1)
    "EnvironmentStageService",
    "ProvisionamentoAutoResult",
    "ProvisionamentoEstagio5Result",
    # topologia_builder (construção pura reusável)
    "HOST_ROLES",
    "HOST_CONTAINERS",
    "NETWORK_MEMBERS",
    "HostView",
    "NetworkView",
    "TopologyResponse",
    "ips_for_host",
    "container_names",
    "construir_hosts",
    "construir_redes",
    "montar_mermaid",
    "compose_path",
]
