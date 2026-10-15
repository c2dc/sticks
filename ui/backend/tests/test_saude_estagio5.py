"""Example-based tests for container health verification (task 2.2).

A etapa ``aguardar_saude`` do pipeline de provisionamento do Estágio 5 confirma,
por **inspeção read-only** (sem daemon Docker), que **todos** os contêineres-alvo
estão no estado ``running`` e — quando o serviço define ``healthcheck`` — com
status ``healthy`` (Req. 5.1). A lógica central (:func:`verificar_saude`) opera
sobre o seam injetável :class:`ContainerStateInspector`, de modo que os testes
substituem o acesso ao Docker por um **fake** que devolve
:class:`ContainerStateInfo` fabricadas.

Seguindo o mesmo padrão de ``test_isolation_property.py`` / ``test_provisionador
_estagio5.py`` (fake inspector + dataclass simples, pura in-memory), estes testes
cobrem:

1. **Todos saudáveis** → ``aprovado=True`` (running; alguns ``healthy``, outros
   sem healthcheck), sem veredictos reprovados.
2. **Um não ``running``** → ``aprovado=False`` com uma ``razao`` identificando o
   contêiner ofensor.
3. **Um ``unhealthy``** → ``aprovado=False`` com ``razao`` que cita o status.
4. **Serviço sem healthcheck** (``health=None``) → ``saudavel=None`` e **não
   reprova** (o estado ``running`` basta).
5. **Fail-closed**: lista vazia de contêineres → ``aprovado=False``.
6. **Contêiner ausente** (inspector levanta :class:`ContainerStateNotFound`) →
   ``aprovado=False`` com ``razao``.

Nada aqui toca rede, daemon Docker ou subprocess; o inspector é um *fake*
in-memory, seguro no ambiente de dev Windows.

_Requisitos: 5.1_
"""

from __future__ import annotations

import pytest

from app.services.environment.saude import (
    ContainerStateInfo,
    ContainerStateInspector,
    ContainerStateNotFound,
    ResumoSaude,
    VereditoSaude,
    verificar_saude,
)


# ---------------------------------------------------------------------------
# Fake inspector (no Docker daemon, no network, pure in-memory)
# ---------------------------------------------------------------------------


class FakeStateInspector:
    """Inspector read-only *fake* servindo :class:`ContainerStateInfo` fabricadas.

    Satisfaz estruturalmente o :class:`ContainerStateInspector` sem um daemon
    Docker: devolve o estado pré-cadastrado por nome e levanta
    :class:`ContainerStateNotFound` para qualquer contêiner desconhecido — exatamente
    o contrato que a regra de saúde fail-closed espera (Req. 5.1).
    """

    def __init__(self, estados: dict[str, ContainerStateInfo]) -> None:
        self._estados = dict(estados)
        self.consultados: list[str] = []

    def get_container_state(self, container: str) -> ContainerStateInfo:
        self.consultados.append(container)
        try:
            return self._estados[container]
        except KeyError as exc:
            raise ContainerStateNotFound(container) from exc


def _veredito_de(resumo: ResumoSaude, container: str) -> VereditoSaude:
    """Localiza o veredito de ``container`` no resumo (falha o teste se ausente)."""
    for v in resumo.veredictos:
        if v.container == container:
            return v
    raise AssertionError(f"veredito de {container!r} ausente em {resumo!r}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fake_satisfies_the_inspector_protocol() -> None:
    """O *fake* satisfaz estruturalmente o ``ContainerStateInspector``."""
    assert isinstance(FakeStateInspector({}), ContainerStateInspector)


def test_todos_saudaveis_aprovado() -> None:
    """Todos running (uns ``healthy``, outros sem healthcheck) → ``aprovado=True``.
    _Requisitos: 5.1_"""
    inspector = FakeStateInspector(
        {
            "caldera-attacker": ContainerStateInfo(
                name="caldera-attacker", running=True, health="healthy"
            ),
            "nginx-target": ContainerStateInfo(
                name="nginx-target", running=True, health="healthy"
            ),
            # Serviço sem healthcheck: running basta, saudavel=None.
            "kali-attacker": ContainerStateInfo(
                name="kali-attacker", running=True, health=None
            ),
        }
    )

    resumo = verificar_saude(
        ["caldera-attacker", "nginx-target", "kali-attacker"], inspector
    )

    assert resumo.aprovado is True
    assert resumo.nao_saudaveis == []
    assert len(resumo.veredictos) == 3
    assert all(v.aprovado for v in resumo.veredictos)
    assert all(v.razao is None for v in resumo.veredictos)


def test_um_nao_running_reprova_com_razao() -> None:
    """Um contêiner fora de ``running`` → ``aprovado=False`` com razão nele.
    _Requisitos: 5.1_"""
    inspector = FakeStateInspector(
        {
            "caldera-attacker": ContainerStateInfo(
                name="caldera-attacker", running=True, health="healthy"
            ),
            "nginx-target": ContainerStateInfo(
                name="nginx-target", running=False, health=None
            ),
        }
    )

    resumo = verificar_saude(["caldera-attacker", "nginx-target"], inspector)

    assert resumo.aprovado is False
    ofensor = _veredito_de(resumo, "nginx-target")
    assert ofensor.aprovado is False
    assert ofensor.rodando is False
    assert ofensor.razao is not None and ofensor.razao != ""
    assert "running" in ofensor.razao
    # Só o ofensor reprova; o saudável continua aprovado.
    assert [v.container for v in resumo.nao_saudaveis] == ["nginx-target"]
    assert _veredito_de(resumo, "caldera-attacker").aprovado is True


def test_um_unhealthy_reprova_com_razao() -> None:
    """Um contêiner running porém ``unhealthy`` → ``aprovado=False`` com razão.
    _Requisitos: 5.1_"""
    inspector = FakeStateInspector(
        {
            "caldera-attacker": ContainerStateInfo(
                name="caldera-attacker", running=True, health="healthy"
            ),
            "mariadb-target": ContainerStateInfo(
                name="mariadb-target", running=True, health="unhealthy"
            ),
        }
    )

    resumo = verificar_saude(["caldera-attacker", "mariadb-target"], inspector)

    assert resumo.aprovado is False
    ofensor = _veredito_de(resumo, "mariadb-target")
    assert ofensor.aprovado is False
    assert ofensor.rodando is True
    assert ofensor.saudavel is False
    assert ofensor.razao is not None and "unhealthy" in ofensor.razao
    assert [v.container for v in resumo.nao_saudaveis] == ["mariadb-target"]


def test_sem_healthcheck_saudavel_none_nao_reprova() -> None:
    """Serviço sem healthcheck (``health=None``) → ``saudavel=None`` e aprova.
    _Requisitos: 5.1_"""
    inspector = FakeStateInspector(
        {
            "kali-attacker": ContainerStateInfo(
                name="kali-attacker", running=True, health=None
            ),
        }
    )

    resumo = verificar_saude(["kali-attacker"], inspector)

    assert resumo.aprovado is True
    veredito = _veredito_de(resumo, "kali-attacker")
    assert veredito.rodando is True
    assert veredito.saudavel is None  # sem healthcheck: nada a exigir
    assert veredito.aprovado is True
    assert veredito.razao is None


def test_lista_vazia_reprova_fail_closed() -> None:
    """Fail-closed: lista vazia de contêineres → ``aprovado=False``.
    _Requisitos: 5.1_"""
    inspector = FakeStateInspector({})

    resumo = verificar_saude([], inspector)

    assert resumo.aprovado is False
    assert resumo.veredictos == []
    # Nada a provar saudável: nenhum contêiner foi consultado.
    assert inspector.consultados == []


def test_container_ausente_reprova_com_razao() -> None:
    """Contêiner ausente (``ContainerStateNotFound``) → ``aprovado=False`` com razão.
    _Requisitos: 5.1_"""
    inspector = FakeStateInspector(
        {
            "caldera-attacker": ContainerStateInfo(
                name="caldera-attacker", running=True, health="healthy"
            ),
            # "nginx-target" propositalmente ausente do inspector.
        }
    )

    resumo = verificar_saude(["caldera-attacker", "nginx-target"], inspector)

    assert resumo.aprovado is False
    ofensor = _veredito_de(resumo, "nginx-target")
    assert ofensor.aprovado is False
    assert ofensor.rodando is False
    assert ofensor.razao is not None and ofensor.razao != ""
    assert "encontrado" in ofensor.razao
    assert [v.container for v in resumo.nao_saudaveis] == ["nginx-target"]
