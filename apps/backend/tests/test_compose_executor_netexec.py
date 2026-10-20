"""Example-based tests for the ``ComposeExecutor`` network/``exec`` operations (task 2.1).

A spec irmã já entrega o ``ComposeExecutor`` com ``up``/``down`` em streaming
(forma de lista de argumentos, sem shell). Esta spec o **estende** com as
operações mínimas que as sub-etapas 5.1/5.2 exigem:

``attach_network(container, network, *, on_log)``
    ``docker network connect <network> <container>`` — anexa um alvo à rede de
    egresso (5.1) ou às redes de ataque ``internal: true`` ao re-isolar (5.2).
``detach_network(container, network, *, on_log)``
    ``docker network disconnect <network> <container>`` — desanexa um alvo, com a
    ordem estrita ``detach`` (ataque) antes de ``attach`` (egresso) que preserva
    o invariante de contenção.
``exec_in(container, argv, *, on_log)``
    ``docker exec <container> <argv...>`` — roda o setup dentro do alvo; ``argv``
    é uma **lista** (ex.: ``["bash", "-lc", <comando>]``), nunca uma string de
    shell.

Estes testes cobrem **o contrato das novas operações** por dois ângulos
complementares, nenhum deles tocando rede, daemon Docker ou shell:

1. **Fake da costura** (`_FakeComposeExecutor`): registra cada chamada de
   ``attach``/``detach``/``exec_in`` com seus argumentos e emite ``LogLine`` por
   ``on_log``. Assere a **forma de lista** de ``argv`` em ``exec_in`` (sem shell)
   e a emissão de ``LogLine`` na ordem; o fake satisfaz estruturalmente o
   ``ComposeExecutor`` (duck typing), como os seams injetáveis do pacote.
2. **Produção** (`DockerComposeExecutor`): com ``subprocess.Popen`` trocado por
   um fake, assere que os ``argv`` montados são listas de argumentos
   (``docker network connect|disconnect`` e ``docker exec`` com ``argv`` espalhado
   como itens separados — sem shell, sem metacaracteres interpretados) e que cada
   operação emite ``LogLine`` por ``on_log``.

_Requirements: 2.3, 3.1, 3.3_
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Callable

import pytest

from app.services.environment.compose_executor import (
    CommandResult,
    ComposeExecutor,
    DockerComposeExecutor,
    LogLine,
)


# ---------------------------------------------------------------------------
# Fake da costura (sem subprocess, sem daemon Docker, sem shell)
# ---------------------------------------------------------------------------


@dataclass
class _Call:
    """Uma chamada registrada ao fake: a operação e seus argumentos.

    ``op`` é ``"attach"``/``"detach"``/``"exec_in"``; ``container`` é o alvo;
    ``network`` fica preenchido para ``attach``/``detach``; ``argv`` fica
    preenchido (lista) para ``exec_in`` — a asserção da **forma de lista** se
    apoia neste campo.
    """

    op: str
    container: str
    network: str | None = None
    argv: list[str] | None = None


class _FakeComposeExecutor:
    """Fake injetável que registra attach/detach/exec_in e emite ``LogLine``.

    Satisfaz estruturalmente o :class:`ComposeExecutor` (expõe ``up``/``down`` e
    as novas operações com as assinaturas do protocolo), sem herança explícita e
    sem tocar rede/daemon/shell. Cada operação:

    * registra a chamada (operação + argumentos) em :attr:`calls`, para asserção
      da ordem e da **forma de lista** de ``argv`` em ``exec_in``;
    * emite uma ``LogLine(stream="comando")`` com a representação do command e
      devolve um :class:`CommandResult` com ``code=0`` e as lines emitidas.
    """

    def __init__(self) -> None:
        self.calls: list[_Call] = []

    # --- operações herdadas (não exercitadas aqui, presentes p/ o protocolo) ---
    def up(
        self, compose_file: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:  # pragma: no cover - não exercitado neste arquivo
        return CommandResult(code=0, lines=[])

    def down(
        self, compose_file: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:  # pragma: no cover - não exercitado neste arquivo
        return CommandResult(code=0, lines=[])

    # --- novas operações (contrato desta spec) ---
    def attach_network(
        self, container: str, network: str, *, on_log: Callable[[LogLine], None], ip=None
    ) -> CommandResult:
        self.calls.append(_Call(op="attach", container=container, network=network))
        line = LogLine(
            step="attach_network",
            stream="comando",
            text=f"network connect {network} {container}",
        )
        on_log(line)
        return CommandResult(code=0, lines=[line])

    def detach_network(
        self, container: str, network: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        self.calls.append(_Call(op="detach", container=container, network=network))
        line = LogLine(
            step="detach_network",
            stream="comando",
            text=f"network disconnect {network} {container}",
        )
        on_log(line)
        return CommandResult(code=0, lines=[line])

    def exec_in(
        self, container: str, argv: list[str], *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        # Registra ``argv`` como lista — a asserção da "forma de lista" se apoia nisto.
        self.calls.append(_Call(op="exec_in", container=container, argv=list(argv)))
        line = LogLine(
            step="exec_in",
            stream="comando",
            text=f"exec {container} {' '.join(argv)}",
        )
        on_log(line)
        return CommandResult(code=0, lines=[line])

    def remove_network(
        self, network: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:  # pragma: no cover - não exercitado neste arquivo
        line = LogLine(
            step="remove_network",
            stream="comando",
            text=f"network rm {network}",
        )
        on_log(line)
        return CommandResult(code=0, lines=[line])

    def create_network(
        self, network: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:  # pragma: no cover - não exercitado neste arquivo
        line = LogLine(
            step="create_network",
            stream="comando",
            text=f"network create {network}",
        )
        on_log(line)
        return CommandResult(code=0, lines=[line])


@pytest.fixture()
def coletor() -> Iterator[list[LogLine]]:
    """Um coletor (lista) que ``on_log`` preenche, na ordem de emissão."""
    yield []


# ---------------------------------------------------------------------------
# Fake de subprocess (reusado para asserir a forma de lista na produção)
# ---------------------------------------------------------------------------


class _GravadorPopen:
    """Fake de ``subprocess.Popen`` que grava cada ``argv`` recebido.

    Expõe um ``stdout`` iterável (vazio por padrão) com ``close()`` no-op e um
    ``wait()`` que devolve o *exit code* configurado — espelhando o objeto-file
    real sem criar processo algum. Grava a **lista** de argumentos de cada
    chamada em :attr:`argvs`, de modo que os testes assertem a forma de lista
    (sem shell) dos commands montados.
    """

    def __init__(self, code: int = 0, lines: list[str] | None = None) -> None:
        self.argvs: list[list[str]] = []
        self._codigo = code
        self._linhas = lines or []

    def __call__(self, argv, **kwargs):
        self.argvs.append(list(argv))
        return _GravadorPopen._Processo(self._linhas, self._codigo)

    class _Processo:
        def __init__(self, linhas: list[str], codigo: int) -> None:
            self.stdout = _GravadorPopen._Stdout(linhas)
            self._codigo = codigo

        def wait(self) -> int:
            return self._codigo

    class _Stdout:
        def __init__(self, linhas: list[str]) -> None:
            self._iter = iter([f"{l}\n" for l in linhas])
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self) -> str:
            return next(self._iter)

        def close(self) -> None:
            self.closed = True


# ---------------------------------------------------------------------------
# 1) Contrato pelo fake da costura
# ---------------------------------------------------------------------------


def test_fake_satisfies_the_compose_executor_protocol() -> None:
    """O fake satisfaz estruturalmente o ``ComposeExecutor`` (duck typing)."""
    assert isinstance(_FakeComposeExecutor(), ComposeExecutor)


def test_fake_records_attach_detach_arguments_and_emits_logline(
    coletor: list[LogLine],
) -> None:
    """``attach``/``detach`` registram (container, network) e emitem ``LogLine``.
    _Requirements: 3.1, 3.3_"""
    fake = _FakeComposeExecutor()

    r_detach = fake.detach_network(
        "nginx-target", "sticks-attack", on_log=coletor.append
    )
    r_attach = fake.attach_network(
        "nginx-target", "sticks-egress-ephemeral", on_log=coletor.append
    )

    # Argumentos registrados, na ordem (detach de ataque ANTES de attach de egresso).
    assert fake.calls == [
        _Call(op="detach", container="nginx-target", network="sticks-attack"),
        _Call(
            op="attach",
            container="nginx-target",
            network="sticks-egress-ephemeral",
        ),
    ]
    # Cada operação emitiu uma ``LogLine`` por ``on_log``, na ordem.
    assert len(coletor) == 2
    assert coletor[0].step == "detach_network"
    assert coletor[1].step == "attach_network"
    assert all(l.stream == "comando" for l in coletor)
    # O ``CommandResult`` espelha a line emitida.
    assert r_detach.lines == [coletor[0]]
    assert r_attach.lines == [coletor[1]]
    assert r_detach.code == 0 and r_attach.code == 0


def test_fake_exec_in_receives_argv_as_a_list_not_a_shell_string(
    coletor: list[LogLine],
) -> None:
    """``exec_in`` recebe ``argv`` como **lista** (sem shell) e emite ``LogLine``.
    _Requirements: 2.3, 3.3_"""
    fake = _FakeComposeExecutor()
    comando_curado = "useradd -m attacker || true"
    argv = ["bash", "-lc", comando_curado]

    result = fake.exec_in("mariadb-target", argv, on_log=coletor.append)

    assert len(fake.calls) == 1
    chamada = fake.calls[0]
    assert chamada.op == "exec_in"
    assert chamada.container == "mariadb-target"
    # Forma de LISTA: três itens exatos; o comando curado é UM item (não quebrado
    # por espaços), prova de que não houve interpretação por um shell do host.
    assert isinstance(chamada.argv, list)
    assert chamada.argv == ["bash", "-lc", comando_curado]
    assert chamada.argv[2] == comando_curado
    # ``LogLine`` emitida por ``on_log``.
    assert len(coletor) == 1
    assert coletor[0].step == "exec_in"
    assert result.lines == [coletor[0]]
    assert result.code == 0


# ---------------------------------------------------------------------------
# 2) Contrato na produção (DockerComposeExecutor) — forma de lista, sem shell
# ---------------------------------------------------------------------------


def test_production_satisfies_the_protocol_with_new_operations() -> None:
    """``DockerComposeExecutor`` satisfaz o ``ComposeExecutor`` com as novas ops."""
    assert isinstance(DockerComposeExecutor(), ComposeExecutor)


def test_production_attach_network_builds_connect_argv_list(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """``attach_network`` → ``docker network connect <network> <container>`` (lista).
    _Requirements: 3.1_"""
    grav = _GravadorPopen(code=0, lines=["conectado"])
    monkeypatch.setattr(subprocess, "Popen", grav)

    result = DockerComposeExecutor().attach_network(
        "nginx-target", "sticks-egress-ephemeral", on_log=coletor.append
    )

    assert len(grav.argvs) == 1
    assert grav.argvs[0] == [
        "docker",
        "network",
        "connect",
        "sticks-egress-ephemeral",
        "nginx-target",
    ]
    assert result.code == 0
    # Emite a line "comando" primeiro, depois o stdout do command.
    assert coletor[0].stream == "comando"
    assert any(l.stream == "stdout" for l in coletor)


def test_production_detach_network_builds_disconnect_argv_list(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """``detach_network`` → ``docker network disconnect <network> <container>`` (lista).
    _Requirements: 3.1_"""
    grav = _GravadorPopen(code=0, lines=["desconectado"])
    monkeypatch.setattr(subprocess, "Popen", grav)

    result = DockerComposeExecutor().detach_network(
        "nginx-target", "sticks-attack", on_log=coletor.append
    )

    assert len(grav.argvs) == 1
    assert grav.argvs[0] == [
        "docker",
        "network",
        "disconnect",
        "sticks-attack",
        "nginx-target",
    ]
    assert result.code == 0
    assert coletor[0].stream == "comando"


def test_production_exec_in_spreads_argv_as_list_without_shell(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """``exec_in`` → ``docker exec <container> <argv...>`` com ``argv`` espalhado.
    _Requirements: 2.3, 3.3_"""
    grav = _GravadorPopen(code=0, lines=["ok"])
    monkeypatch.setattr(subprocess, "Popen", grav)

    comando_curado = "cat > /tmp/backdoor.py << 'EOF'\nprint('x')\nEOF"
    result = DockerComposeExecutor().exec_in(
        "mariadb-target",
        ["bash", "-lc", comando_curado],
        on_log=coletor.append,
    )

    assert len(grav.argvs) == 1
    argv = grav.argvs[0]
    # Forma de lista: cada item é um argumento separado; o comando curado é UM
    # único item (não dividido por espaços/metacaracteres) — prova de que não há
    # shell do host interpretando a string.
    assert argv == [
        "docker",
        "exec",
        "mariadb-target",
        "bash",
        "-lc",
        comando_curado,
    ]
    assert argv[-1] == comando_curado
    assert result.code == 0
    assert coletor[0].stream == "comando"


def test_production_new_operations_fail_closed_when_docker_absent(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """Docker ausente (``FileNotFoundError``) → ``code`` 127 nas novas operações.
    _Requirements: 3.1, 3.3_"""

    def _raise(argv: list[str], **kwargs: object) -> None:
        raise FileNotFoundError("docker não encontrado no PATH")

    monkeypatch.setattr(subprocess, "Popen", _raise)
    executor = DockerComposeExecutor()

    r_attach = executor.attach_network("c", "n", on_log=coletor.append)
    r_detach = executor.detach_network("c", "n", on_log=coletor.append)
    r_exec = executor.exec_in("c", ["bash", "-lc", "true"], on_log=coletor.append)

    assert r_attach.code == 127
    assert r_detach.code == 127
    assert r_exec.code == 127
    # Cada falha reporta a causa como uma line de stderr.
    assert any(l.stream == "stderr" for l in coletor)
