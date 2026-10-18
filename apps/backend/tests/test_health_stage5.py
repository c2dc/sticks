"""Example-based tests for container health verification (task 2.2).

A step ``aguardar_saude`` do pipeline de provisionamento do Estágio 5 confirma,
por **inspeção read-only** (sem daemon Docker), que **todos** os contêineres-target
estão no state ``running`` e — quando o serviço define ``healthcheck`` — com
status ``healthy`` (Req. 5.1). A lógica central (:func:`check_health`) opera
sobre o seam injetável :class:`ContainerStateInspector`, de modo que os testes
substituem o acesso ao Docker por um **fake** que devolve
:class:`ContainerStateInfo` fabricadas.

Seguindo o mesmo padrão de ``test_isolation_property.py`` / ``test_provisionador
_estagio5.py`` (fake inspector + dataclass simples, pura in-memory), estes testes
cobrem:

1. **Todos saudáveis** → ``approved=True`` (running; alguns ``healthy``, outros
   sem healthcheck), sem verdicts reprovados.
2. **Um não ``running``** → ``approved=False`` com uma ``reason`` identificando o
   contêiner ofensor.
3. **Um ``unhealthy``** → ``approved=False`` com ``reason`` que cita o status.
4. **Serviço sem healthcheck** (``health=None``) → ``healthy=None`` e **não
   reprova** (o state ``running`` basta).
5. **Fail-closed**: lista vazia de contêineres → ``approved=False``.
6. **Contêiner ausente** (inspector levanta :class:`ContainerStateNotFound`) →
   ``approved=False`` com ``reason``.

Nada aqui toca rede, daemon Docker ou subprocess; o inspector é um *fake*
in-memory, seguro no environment de dev Windows.

_Requirements: 5.1_
"""

from __future__ import annotations

import pytest

from app.services.environment.health import (
    ContainerStateInfo,
    ContainerStateInspector,
    ContainerStateNotFound,
    HealthSummary,
    HealthVerdict,
    check_health,
)


# ---------------------------------------------------------------------------
# Fake inspector (no Docker daemon, no network, pure in-memory)
# ---------------------------------------------------------------------------


class FakeStateInspector:
    """Inspector read-only *fake* servindo :class:`ContainerStateInfo` fabricadas.

    Satisfaz estruturalmente o :class:`ContainerStateInspector` sem um daemon
    Docker: devolve o state pré-cadastrado por name e levanta
    :class:`ContainerStateNotFound` para qualquer contêiner desconhecido — exatamente
    o contrato que a regra de saúde fail-closed espera (Req. 5.1).
    """

    def __init__(self, states: dict[str, ContainerStateInfo]) -> None:
        self._estados = dict(states)
        self.consultados: list[str] = []

    def get_container_state(self, container: str) -> ContainerStateInfo:
        self.consultados.append(container)
        try:
            return self._estados[container]
        except KeyError as exc:
            raise ContainerStateNotFound(container) from exc


def _veredito_de(resumo: HealthSummary, container: str) -> HealthVerdict:
    """Localiza o verdict de ``container`` no resumo (failure o teste se ausente)."""
    for v in resumo.verdicts:
        if v.container == container:
            return v
    raise AssertionError(f"verdict de {container!r} ausente em {resumo!r}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fake_satisfies_the_inspector_protocol() -> None:
    """O *fake* satisfaz estruturalmente o ``ContainerStateInspector``."""
    assert isinstance(FakeStateInspector({}), ContainerStateInspector)


def test_todos_saudaveis_aprovado() -> None:
    """Todos running (uns ``healthy``, outros sem healthcheck) → ``approved=True``.
    _Requirements: 5.1_"""
    inspector = FakeStateInspector(
        {
            "caldera-attacker": ContainerStateInfo(
                name="caldera-attacker", running=True, health="healthy"
            ),
            "nginx-target": ContainerStateInfo(
                name="nginx-target", running=True, health="healthy"
            ),
            # Serviço sem healthcheck: running basta, healthy=None.
            "kali-attacker": ContainerStateInfo(
                name="kali-attacker", running=True, health=None
            ),
        }
    )

    resumo = check_health(
        ["caldera-attacker", "nginx-target", "kali-attacker"], inspector
    )

    assert resumo.approved is True
    assert resumo.unhealthy == []
    assert len(resumo.verdicts) == 3
    assert all(v.approved for v in resumo.verdicts)
    assert all(v.reason is None for v in resumo.verdicts)


def test_um_nao_running_reprova_com_razao() -> None:
    """Um contêiner fora de ``running`` → ``approved=False`` com razão nele.
    _Requirements: 5.1_"""
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

    resumo = check_health(["caldera-attacker", "nginx-target"], inspector)

    assert resumo.approved is False
    ofensor = _veredito_de(resumo, "nginx-target")
    assert ofensor.approved is False
    assert ofensor.running is False
    assert ofensor.reason is not None and ofensor.reason != ""
    assert "running" in ofensor.reason
    # Só o ofensor reprova; o saudável continua approved.
    assert [v.container for v in resumo.unhealthy] == ["nginx-target"]
    assert _veredito_de(resumo, "caldera-attacker").approved is True


def test_um_unhealthy_reprova_com_razao() -> None:
    """Um contêiner running porém ``unhealthy`` → ``approved=False`` com razão.
    _Requirements: 5.1_"""
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

    resumo = check_health(["caldera-attacker", "mariadb-target"], inspector)

    assert resumo.approved is False
    ofensor = _veredito_de(resumo, "mariadb-target")
    assert ofensor.approved is False
    assert ofensor.running is True
    assert ofensor.healthy is False
    assert ofensor.reason is not None and "unhealthy" in ofensor.reason
    assert [v.container for v in resumo.unhealthy] == ["mariadb-target"]


def test_sem_healthcheck_saudavel_none_nao_reprova() -> None:
    """Serviço sem healthcheck (``health=None``) → ``healthy=None`` e aprova.
    _Requirements: 5.1_"""
    inspector = FakeStateInspector(
        {
            "kali-attacker": ContainerStateInfo(
                name="kali-attacker", running=True, health=None
            ),
        }
    )

    resumo = check_health(["kali-attacker"], inspector)

    assert resumo.approved is True
    verdict = _veredito_de(resumo, "kali-attacker")
    assert verdict.running is True
    assert verdict.healthy is None  # sem healthcheck: nada a exigir
    assert verdict.approved is True
    assert verdict.reason is None


def test_lista_vazia_reprova_fail_closed() -> None:
    """Fail-closed: lista vazia de contêineres → ``approved=False``.
    _Requirements: 5.1_"""
    inspector = FakeStateInspector({})

    resumo = check_health([], inspector)

    assert resumo.approved is False
    assert resumo.verdicts == []
    # Nada a provar saudável: nenhum contêiner foi consultado.
    assert inspector.consultados == []


def test_container_ausente_reprova_com_razao() -> None:
    """Contêiner ausente (``ContainerStateNotFound``) → ``approved=False`` com razão.
    _Requirements: 5.1_"""
    inspector = FakeStateInspector(
        {
            "caldera-attacker": ContainerStateInfo(
                name="caldera-attacker", running=True, health="healthy"
            ),
            # "nginx-target" propositalmente ausente do inspector.
        }
    )

    resumo = check_health(["caldera-attacker", "nginx-target"], inspector)

    assert resumo.approved is False
    ofensor = _veredito_de(resumo, "nginx-target")
    assert ofensor.approved is False
    assert ofensor.running is False
    assert ofensor.reason is not None and ofensor.reason != ""
    assert "not found" in ofensor.reason
    assert [v.container for v in resumo.unhealthy] == ["nginx-target"]
