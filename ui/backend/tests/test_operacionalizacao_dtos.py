"""Example (non-property) tests for the Stage 4 environment-parameter DTOs
(task 5.1).

Verificam, por exemplos diretos, os DTOs novos da 2ª Saída do Estágio 4
(Operacionalização) definidos em
``app.services.environment.operacionalizacao`` (Req. 8.3, 8.4, 17.1, 17.2):

1. :class:`PlataformaAmbiente` — ``CONTAINER`` serializa para ``"conteiner"`` e
   é o **único** membro implementado nesta spec; ``maquina_virtual`` fica como
   placeholder comentado (Req. 17.1, 17.2).
2. :class:`HostAlvo` / :class:`ServicoRequerido` — campos e defaults.
3. :class:`ParametrosDeAmbiente` — defaults (``plataforma=CONTAINER``,
   ``origem=curadoria_humana`` reusando :class:`TranslationSource`, coleções
   vazias) e serialização pt-BR.

_Requisitos: 8.3, 8.4, 17.1, 17.2_
"""

from __future__ import annotations

import enum

from app.models.enums import TranslationSource
from app.services.environment import (
    HostAlvo,
    ParametrosDeAmbiente,
    PlataformaAmbiente,
    ServicoRequerido,
)


def test_plataforma_ambiente_is_str_enum() -> None:
    assert issubclass(PlataformaAmbiente, str)
    assert issubclass(PlataformaAmbiente, enum.Enum)


def test_plataforma_container_value() -> None:
    assert PlataformaAmbiente.CONTAINER.value == "conteiner"


def test_plataforma_only_container_implemented() -> None:
    # ``maquina_virtual`` é um placeholder comentado (Req. 17.2) e NÃO deve
    # existir como membro do enum nesta spec.
    assert [m.name for m in PlataformaAmbiente] == ["CONTAINER"]
    assert not hasattr(PlataformaAmbiente, "VM")


def test_host_alvo_defaults() -> None:
    host = HostAlvo(rotulo="host_a")
    assert host.rotulo == "host_a"
    assert host.endereco is None
    assert host.container is None
    assert host.host_type is None
    assert host.vulnerabilidades == []


def test_host_alvo_full() -> None:
    host = HostAlvo(
        rotulo="host_b",
        endereco="192.168.20.30",
        container="victim_web",
        host_type="linux",
        vulnerabilidades=["flask na porta 5000"],
    )
    assert host.endereco == "192.168.20.30"
    assert host.container == "victim_web"
    assert host.host_type == "linux"
    assert host.vulnerabilidades == ["flask na porta 5000"]


def test_servico_requerido_defaults() -> None:
    servico = ServicoRequerido(descricao="ssh root login")
    assert servico.descricao == "ssh root login"
    assert servico.tecnica_origem is None


def test_parametros_de_ambiente_defaults() -> None:
    params = ParametrosDeAmbiente(caso_id="apt-x")
    assert params.caso_id == "apt-x"
    assert params.plataforma is PlataformaAmbiente.CONTAINER
    assert params.hosts == []
    assert params.servicos == []
    assert params.network_context == []
    assert params.origem is TranslationSource.HUMAN_CURATION


def test_parametros_de_ambiente_serializes_pt_br() -> None:
    params = ParametrosDeAmbiente(
        caso_id="apt-x",
        hosts=[HostAlvo(rotulo="host_a", endereco="192.168.20.30")],
        servicos=[ServicoRequerido(descricao="flask na porta 5000", tecnica_origem="T1190")],
        network_context=["segmento interno 192.168.20.0/24"],
    )
    dumped = params.model_dump(mode="json")
    assert dumped["plataforma"] == "conteiner"
    assert dumped["origem"] == "curadoria_humana"
    assert dumped["hosts"][0]["rotulo"] == "host_a"
    assert dumped["servicos"][0]["tecnica_origem"] == "T1190"
    assert dumped["network_context"] == ["segmento interno 192.168.20.0/24"]
