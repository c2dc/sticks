"""Example-based tests for ``DockerComposeExecutor`` with a fake subprocess (task 1.3).

A costura de execução do laboratório (:class:`DockerComposeExecutor`) roda o
``docker compose`` via :mod:`subprocess` na **forma de lista de argumentos** (sem
shell), lê ``stdout``/``stderr`` line a line e empurra cada :class:`LogLine`
por ``on_log`` em tempo quase real; o ``code`` do :class:`CommandResult` é o
*exit code* do processo.

Estes testes substituem ``subprocess.Popen`` por um **fake** (sem subprocess
real, sem daemon Docker) que roteia lines pré-roteirizadas e devolve um *exit
code* configurável, cobrindo:

1. **Sucesso** (``code == 0``): as lines chegam ao ``on_log`` na ordem.
2. **Falha ao subir** (``code != 0``): o *exit code* propaga como failure
   (Req. 3.5).
3. **Ordem do streaming**: ``on_log`` recebe a line ``command`` primeiro, depois
   as lines de ``stdout`` na ordem de emissão (Req. 2.3).
4. **Argv sem a camada de aplicação**: o ``up`` nomeia **apenas** os quatro
   serviços do laboratório de ataque — nunca ``frontend``/``backend``/``postgres``
   nem o perfil ``containers`` (Req. 3.4).

Nada aqui toca rede, daemon Docker ou shell; o fake é injetado por ``monkeypatch``
no símbolo ``subprocess.Popen`` que o executor resolve ao rodar.

_Requirements: 2.3, 3.4, 3.5_
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator

import pytest

from app.services.environment.compose_executor import (
    LAB_SERVICES,
    ComposeExecutor,
    DockerComposeExecutor,
    LogLine,
)

# ---------------------------------------------------------------------------
# Fake subprocess (no real process, no Docker daemon, no shell)
# ---------------------------------------------------------------------------


# Serviços da camada de aplicação da Pipeline_UI que o ``up`` do laboratório de
# ataque jamais deve nomear (Req. 3.4) — nem o perfil que os agrupa.
_APP_SERVICES: tuple[str, ...] = ("frontend", "backend", "postgres")
_APP_PROFILE = "containers"


class _FakeStdout:
    """``stdout`` falso: iterável das lines roteirizadas, com ``close()`` no-op.

    Espelha o objeto-file do ``Popen.stdout`` real (``text=True``,
    line-buffered): produz cada line terminada em ``\\n`` na iteração e expõe um
    ``close()`` — que o executor chama após drenar as lines.
    """

    def __init__(self, lines: list[str]) -> None:
        self._iter = iter([f"{line}\n" for line in lines])
        self.closed = False

    def __iter__(self) -> _FakeStdout:
        return self

    def __next__(self) -> str:
        return next(self._iter)

    def close(self) -> None:
        self.closed = True


class _FakePopen:
    """Fake de ``subprocess.Popen``: roteia lines pré-roteirizadas + exit code.

    Grava o ``argv`` recebido para asserção, expõe ``stdout`` como um
    :class:`_FakeStdout` das lines pré-roteirizadas (cada uma terminada em
    ``\\n``, como o ``text=True``/line-buffered real entrega) e devolve o *exit
    code* configurado em :meth:`wait`. Não cria processo algum.
    """

    #: ``argv`` da última instância criada, para asserção do command montado.
    ultimo_argv: list[str] | None = None

    def __init__(
        self,
        argv: list[str],
        *,
        _linhas: list[str],
        _codigo: int,
        **_kwargs: object,
    ) -> None:
        type(self).ultimo_argv = list(argv)
        self.stdout = _FakeStdout(_linhas)
        self._codigo = _codigo

    def wait(self) -> int:
        return self._codigo


def _fake_popen_factory(lines: list[str], code: int) -> Callable[..., _FakePopen]:
    """Fábrica de ``Popen`` falso ligando ``lines``/``code`` roteirizados."""

    def _factory(argv: list[str], **kwargs: object) -> _FakePopen:
        return _FakePopen(argv, _linhas=lines, _codigo=code, **kwargs)

    return _factory


@pytest.fixture()
def coletor() -> Iterator[list[LogLine]]:
    """Um coletor (lista) que ``on_log`` preenche, na ordem de emissão."""
    yield []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_executor_satisfies_the_protocol() -> None:
    """``DockerComposeExecutor`` satisfaz estruturalmente o ``ComposeExecutor``."""
    assert isinstance(DockerComposeExecutor(), ComposeExecutor)


def test_bring_up_success_routes_lines_in_order_with_code_zero(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """Sucesso: ``code == 0`` e as lines de stdout chegam ao ``on_log`` em ordem.
    _Requirements: 2.3, 3.5_"""
    output = ["Creating network", "Container caldera-attacker Started", "Waiting..."]
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory(output, 0))

    result = DockerComposeExecutor(image_exists=lambda _n: True).up(
        "docker/docker-compose.yml", on_log=coletor.append
    )

    assert result.code == 0
    # As lines de stdout coletadas (fora a line "command") preservam a ordem.
    stdout_texts = [line.text for line in coletor if line.stream == "stdout"]
    assert stdout_texts == output
    # ``result.lines`` espelha o que foi empurrado por ``on_log``.
    assert result.lines == coletor


def test_bring_up_failure_propagates_nonzero_exit_code(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """Falha ao subir: um ``code != 0`` propaga como failure (Req. 3.5).
    _Requirements: 3.5_"""
    output = ["Container nginx-target Error", "dependency failed to start"]
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory(output, 1))

    result = DockerComposeExecutor(image_exists=lambda _n: True).up(
        "docker/docker-compose.yml", on_log=coletor.append
    )

    assert result.code == 1
    # Mesmo em failure, as lines emitidas são roteadas (para a tela/console).
    assert [line.text for line in coletor if line.stream == "stdout"] == output


def test_bring_up_streaming_order_command_before_stdout(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """Ordem do streaming: a line ``command`` sai primeiro, depois os ``stdout``.
    _Requirements: 2.3_"""
    output = ["line-1", "line-2", "line-3"]
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory(output, 0))

    DockerComposeExecutor(image_exists=lambda _n: True).up(
        "docker/docker-compose.yml", on_log=coletor.append
    )

    fluxos = [line.stream for line in coletor]
    # Exatamente uma line de comando, e ela é a PRIMEIRA.
    assert fluxos[0] == "comando"
    assert fluxos.count("comando") == 1
    # Em seguida, as lines de stdout na ordem exata de emissão.
    assert [line.text for line in coletor[1:]] == output
    # Toda a sequência é da step "subir_conteineres".
    assert all(line.step == "subir_conteineres" for line in coletor)


def test_bring_up_argv_names_only_lab_services(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """Argv do ``subir``: só os serviços do laboratório; nunca a camada de aplicação.
    _Requirements: 3.4_"""
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory([], 0))

    DockerComposeExecutor(image_exists=lambda _n: True).up(
        "docker/docker-compose.yml", on_log=coletor.append
    )

    argv = _FakePopen.ultimo_argv
    assert argv is not None
    # Forma de lista de argumentos esperada, sem shell. Imagens presentes =>
    # ``up --no-build`` (reuso, Req. 11.2).
    assert argv[:8] == [
        "docker",
        "compose",
        "-f",
        "docker/docker-compose.yml",
        "up",
        "-d",
        "--wait",
        "--no-build",
    ]
    # Nomeia EXATAMENTE os quatro serviços do laboratório de ataque, em ordem.
    assert argv[8:] == list(LAB_SERVICES)

    # NÃO inclui nenhum serviço da camada de aplicação nem o perfil que os agrupa.
    for app_service in _APP_SERVICES:
        assert app_service not in argv
    assert _APP_PROFILE not in argv
    assert "--profile" not in argv


def test_bring_up_docker_absent_fail_closed_code_127(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """Docker ausente (``FileNotFoundError``) → fail-closed com ``code`` 127.
    _Requirements: 3.5_"""

    def _raise(argv: list[str], **kwargs: object) -> None:
        raise FileNotFoundError("docker não encontrado no PATH")

    monkeypatch.setattr(subprocess, "Popen", _raise)

    result = DockerComposeExecutor(image_exists=lambda _n: True).up(
        "docker/docker-compose.yml", on_log=coletor.append
    )

    assert result.code == 127
    # A causa é reportada como uma line de stderr, após a line "comando".
    assert coletor[0].stream == "comando"
    assert any(line.stream == "stderr" for line in coletor)


# ---------------------------------------------------------------------------
# Reuso de imagem e build condicional (task 12.5 — Req. 11.1, 11.2, 11.3, 11.6)
# ---------------------------------------------------------------------------


class _GravadorPopen:
    """Fake de ``Popen`` que grava TODOS os argvs (build + up em sequencia).

    Diferente de :class:`_FakePopen` (que guarda so o ultimo argv), este coletor
    registra cada command montado, de modo que os testes de build-condicional
    assertem a sequencia build -> up (ou apenas up) e o content de cada um.
    """

    def __init__(self, code: int = 0) -> None:
        self.argvs: list[list[str]] = []
        self._codigo = code

    def __call__(self, argv, **kwargs):
        self.argvs.append(list(argv))
        return _FakePopen(argv, _linhas=[], _codigo=self._codigo)


def test_bring_up_all_images_present_uses_no_build(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """Imagens presentes -> ``up --no-build`` e NENHUMA chamada de ``build`` (Req. 11.2).
    _Requirements: 11.1, 11.2_"""
    grav = _GravadorPopen(code=0)
    monkeypatch.setattr(subprocess, "Popen", grav)

    DockerComposeExecutor(image_exists=lambda _n: True).up(
        "docker/docker-compose.yml", on_log=coletor.append
    )

    # Exatamente um command: o ``up`` (sem build), com ``--no-build``.
    assert len(grav.argvs) == 1
    up = grav.argvs[0]
    assert "build" not in up
    assert "up" in up and "--no-build" in up
    assert up[-4:] == list(LAB_SERVICES)


def test_bring_up_missing_image_builds_first_then_brings_up(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """Falta imagem -> ``build`` dos faltantes primeiro, depois ``up`` (Req. 11.3).
    _Requirements: 11.1, 11.3_"""
    grav = _GravadorPopen(code=0)
    monkeypatch.setattr(subprocess, "Popen", grav)

    # Só a imagem do kali falta; as demais existem.
    def _existe(name: str) -> bool:
        return "kali-attacker" not in name

    DockerComposeExecutor(image_exists=_existe).up(
        "docker/docker-compose.yml", on_log=coletor.append
    )

    assert len(grav.argvs) == 2, "esperava build seguido de up"
    build, up = grav.argvs
    # 1) build nomeia APENAS o servico faltante (kali-attacker).
    assert "build" in build
    assert "kali-attacker" in build
    assert build[build.index("build") + 1 :] == ["kali-attacker"]
    # 2) up em seguida, com --no-build e todos os services.
    assert "up" in up and "--no-build" in up
    assert up[-4:] == list(LAB_SERVICES)


def test_bring_up_build_failure_does_not_bring_up(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """Build failure (code != 0) -> NAO executa o ``up`` e propaga a failure (Req. 11.3).
    _Requirements: 11.3_"""
    grav = _GravadorPopen(code=1)  # build retorna != 0
    monkeypatch.setattr(subprocess, "Popen", grav)

    result = DockerComposeExecutor(image_exists=lambda _n: False).up(
        "docker/docker-compose.yml", on_log=coletor.append
    )

    assert result.code == 1
    # Houve build (ao menos um) e NENHUM up — a subida nao ocorre apos build falho.
    assert any("build" in a for a in grav.argvs)
    assert all("up" not in a for a in grav.argvs)


def test_derrubar_nunca_inclui_rmi_preserva_imagem(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """``derrubar`` -> ``compose down`` SEM ``--rmi`` (preserva a imagem — Req. 11.6).
    _Requirements: 8.2, 11.6_"""
    grav = _GravadorPopen(code=0)
    monkeypatch.setattr(subprocess, "Popen", grav)

    DockerComposeExecutor().down("docker/docker-compose.yml", on_log=coletor.append)

    assert len(grav.argvs) == 1
    down = grav.argvs[0]
    assert "down" in down
    # NUNCA remove imagens nem volumes de dados persistentes.
    assert "--rmi" not in down
    assert "-v" not in down and "--volumes" not in down


# ---------------------------------------------------------------------------
# Caldera fixed-engine contract (task 6.1 — design §E, Req. 12.2)
# ---------------------------------------------------------------------------
#
# Caldera is the FIXED, audited execution engine (C2): not an agent decision and
# not an optional target. These example tests RECORD the "compiled once from
# source, reused" idempotency of the Caldera image in the provisioning path —
# WITHOUT adding any new execution machinery. They pin the recorded contract
# (`CALDERA_SERVICE`/`CALDERA_ENGINE_IMAGE`) against the lab image map and assert
# the already-implemented `up` (build-once when missing, else `--no-build`) and
# `down` (preserves the image) behavior for the engine specifically.


def test_caldera_engine_contract_pins_the_fixed_image() -> None:
    """A Caldera é a engine FIXA, fixada à imagem estável, dentro do mapa do lab.
    _Requirements: 12.2_"""
    from app.services.environment.compose_executor import (
        CALDERA_ENGINE_IMAGE,
        CALDERA_SERVICE,
        LAB_IMAGES,
        LAB_SERVICES,
    )

    # O serviço da engine é um serviço do laboratório (não uma peça à parte).
    assert CALDERA_SERVICE == "caldera-attacker"
    assert CALDERA_SERVICE in LAB_SERVICES
    # A imagem registrada da engine é EXATAMENTE a pinada no mapa do lab —
    # construída uma vez a partir do fonte (`ops/docker/.docker/caldera`).
    assert CALDERA_ENGINE_IMAGE == "sticks-caldera-attacker:latest"
    assert LAB_IMAGES[CALDERA_SERVICE] == CALDERA_ENGINE_IMAGE


def test_caldera_image_present_is_reused_never_rebuilt(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """Imagem da Caldera já construída -> reuso (``up --no-build``, sem ``build``).
    _Requirements: 12.2, 11.2_"""

    grav = _GravadorPopen(code=0)
    monkeypatch.setattr(subprocess, "Popen", grav)

    # A imagem da engine (e as demais) já existem no engine local.
    DockerComposeExecutor(image_exists=lambda _n: True).up(
        "docker/docker-compose.yml", on_log=coletor.append
    )

    # Nenhum ``build`` foi emitido — a engine fixa é reusada da imagem em cache.
    assert all("build" not in a for a in grav.argvs)
    assert len(grav.argvs) == 1 and "--no-build" in grav.argvs[0]


def test_caldera_image_missing_is_built_once_from_source(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """Imagem da Caldera ausente -> construída uma vez (do fonte) e então ``up``.
    _Requirements: 12.2, 11.3_"""
    from app.services.environment.compose_executor import CALDERA_SERVICE

    grav = _GravadorPopen(code=0)
    monkeypatch.setattr(subprocess, "Popen", grav)

    # Só a imagem da engine Caldera falta; as demais já existem (reuso).
    def _existe(name: str) -> bool:
        return "caldera-attacker" not in name

    DockerComposeExecutor(image_exists=_existe).up(
        "docker/docker-compose.yml", on_log=coletor.append
    )

    assert len(grav.argvs) == 2, "esperava build (da engine) seguido de up"
    build, up = grav.argvs
    # O ``build`` nomeia APENAS o serviço da engine fixa (compilada do fonte).
    assert "build" in build
    assert build[build.index("build") + 1 :] == [CALDERA_SERVICE]
    # Em seguida, ``up --no-build`` reusa tudo (a engine recém-construída inclusa).
    assert "up" in up and "--no-build" in up


def test_caldera_image_survives_teardown_for_reuse(
    monkeypatch: pytest.MonkeyPatch, coletor: list[LogLine]
) -> None:
    """``down`` preserva a imagem da engine (sem ``--rmi``) -> reuso no próximo Run.
    _Requirements: 12.2, 11.6_"""
    grav = _GravadorPopen(code=0)
    monkeypatch.setattr(subprocess, "Popen", grav)

    DockerComposeExecutor().down("docker/docker-compose.yml", on_log=coletor.append)

    assert len(grav.argvs) == 1
    down = grav.argvs[0]
    assert "down" in down
    # A imagem da engine fixa NÃO é removida no teardown (idempotente, cacheada).
    assert "--rmi" not in down
