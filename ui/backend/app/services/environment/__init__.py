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

from app.services.environment.provisionador import (
    CuradoriaProvisionadorDeAmbiente,
    ProvisionadorDeAmbiente,
    ResultadoProvisionamento,
)
from app.services.environment.stage_service import (
    EnvironmentStageService,
    ProvisionamentoAutoResult,
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
    # provisionador (costura + curadoria)
    "ProvisionadorDeAmbiente",
    "ResultadoProvisionamento",
    "CuradoriaProvisionadorDeAmbiente",
    # stage_service (auto-conclusão do Estágio 4 — task 4.2)
    "EnvironmentStageService",
    "ProvisionamentoAutoResult",
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
