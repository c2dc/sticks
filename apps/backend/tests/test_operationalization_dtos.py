"""Example (non-property) tests for the Stage 4 environment-parameter DTOs
(task 5.1).

Verificam, por exemplos diretos, os DTOs novos da 2ª Saída do Estágio 4
(Operacionalização) definidos em
``app.services.environment.operationalization`` (Req. 8.3, 8.4, 17.1, 17.2):

1. :class:`EnvironmentPlatform` — ``CONTAINER`` serializa para ``"conteiner"`` e
   é o **único** membro implementado nesta spec; ``maquina_virtual`` fica como
   placeholder comentado (Req. 17.1, 17.2).
2. :class:`TargetHost` / :class:`RequiredService` — campos e defaults.
3. :class:`EnvironmentParameters` — defaults (``platform=CONTAINER``,
   ``origin=curadoria_humana`` reusando :class:`TranslationSource`, coleções
   vazias) e serialização pt-BR.

_Requirements: 8.3, 8.4, 17.1, 17.2_
"""

from __future__ import annotations

import enum

from app.models.enums import TranslationSource
from app.services.environment import (
    EnvironmentParameters,
    EnvironmentPlatform,
    RequiredService,
    TargetHost,
)


def test_environment_platform_is_str_enum() -> None:
    assert issubclass(EnvironmentPlatform, str)
    assert issubclass(EnvironmentPlatform, enum.Enum)


def test_platform_container_value() -> None:
    assert EnvironmentPlatform.CONTAINER.value == "container"


def test_platform_consolidated_declares_container_and_vm() -> None:
    # The consolidated ``EnvironmentPlatform`` now declares BOTH members:
    # ``CONTAINER`` is exercised this phase and ``VM`` (``virtual_machine``) is
    # declared-but-not-exercised so the portable CHECK admits it without a
    # schema migration (Req. 17.2).
    assert [m.name for m in EnvironmentPlatform] == ["CONTAINER", "VM"]
    assert EnvironmentPlatform.VM.value == "virtual_machine"


def test_host_alvo_defaults() -> None:
    host = TargetHost(label="host_a")
    assert host.label == "host_a"
    assert host.address is None
    assert host.container is None
    assert host.host_type is None
    assert host.vulnerabilities == []


def test_host_alvo_full() -> None:
    host = TargetHost(
        label="host_b",
        address="192.168.20.30",
        container="victim_web",
        host_type="linux",
        vulnerabilities=["flask na porta 5000"],
    )
    assert host.address == "192.168.20.30"
    assert host.container == "victim_web"
    assert host.host_type == "linux"
    assert host.vulnerabilities == ["flask na porta 5000"]


def test_servico_requerido_defaults() -> None:
    servico = RequiredService(description="ssh root login")
    assert servico.description == "ssh root login"
    assert servico.origin_technique is None


def test_environment_parameters_defaults() -> None:
    params = EnvironmentParameters(campaign_id="apt-x")
    assert params.campaign_id == "apt-x"
    assert params.platform is EnvironmentPlatform.CONTAINER
    assert params.hosts == []
    assert params.services == []
    assert params.network_context == []
    assert params.origin is TranslationSource.HUMAN_CURATION


def test_environment_parameters_serializes_english() -> None:
    params = EnvironmentParameters(
        campaign_id="apt-x",
        hosts=[TargetHost(label="host_a", address="192.168.20.30")],
        services=[RequiredService(description="flask na porta 5000", origin_technique="T1190")],
        network_context=["segmento interno 192.168.20.0/24"],
    )
    dumped = params.model_dump(mode="json")
    assert dumped["platform"] == "container"
    assert dumped["origin"] == "human_curation"
    assert dumped["hosts"][0]["label"] == "host_a"
    assert dumped["services"][0]["origin_technique"] == "T1190"
    assert dumped["network_context"] == ["segmento interno 192.168.20.0/24"]
