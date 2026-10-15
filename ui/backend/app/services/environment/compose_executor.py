"""Costura de execução do laboratório Docker em streaming (Estágio 5).

Este módulo define o **ponto de injeção** que o Estágio 5 (Provisionamento do
ambiente) usa para **subir** e **derrubar** o laboratório contido do
``docker/docker-compose.yml`` curado, emitindo cada linha de ``stdout``/``stderr``
para um callback — de modo que o serviço reencaminhe as linhas ao terminal de
logs ao vivo da tela (Req. 2) e componha o resultado como pré-condição de
conclusão (Req. 5) e as mensagens de falha (Req. 7).

Como no restante dos pacotes de serviço (``provisionador``/``operacionalizador``),
a costura é um ``@runtime_checkable Protocol`` e as dependências externas são
**injetáveis**: um *fake* nos testes (sem subprocess, sem daemon Docker) e a
implementação real de produção (:class:`DockerComposeExecutor`, task 1.2) quando
``None``.

DTOs tipados emitidos pela costura:

``LinhaLog``
    Uma linha de log do provisionamento (``dataclass`` *frozen*), para o terminal
    da tela (Req. 2.2, 2.3): a etapa que a emitiu, o fluxo (``comando`` |
    ``stdout`` | ``stderr`` | ``info``) e o texto. O ``timestamp`` é adicionado
    pelo serviço ao bufferizar/transmitir (``seq``/``em`` no ``LinhaLogView``),
    mantendo a costura do executor desacoplada do transporte.

``ResultadoComando``
    O resultado de um comando de ``compose`` (``dataclass`` *frozen*): o código de
    saída (``codigo``) e as linhas emitidas (``linhas``). ``codigo != 0`` indica
    falha (Req. 3.5, 8.4).

``ComposeExecutor``
    O ``@runtime_checkable Protocol`` que define a superfície mínima —
    ``subir(compose_file, *, on_log)`` e ``derrubar(compose_file, *, on_log)`` —
    satisfeita estruturalmente (duck typing) por qualquer objeto que exponha
    estas operações, sem herança explícita.

A implementação de produção :class:`DockerComposeExecutor` (subprocess ``docker
compose``, forma de lista de argumentos, sem shell) é definida na task 1.2.

_Requisitos: 3.1, 3.2, 8.2_
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# DTOs tipados emitidos pela costura (task 1.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinhaLog:
    """Uma linha de log do provisionamento, para o terminal da tela (Req. 2).

    Entrada textual emitida por uma etapa do provisionamento — um comando
    executado, uma linha de ``stdout``/``stderr`` da sua saída, ou uma marcação
    de etapa (``info``) — transmitida ao terminal da tela na ordem em que foi
    emitida (Req. 2.2, 2.3). É **imutável** (*frozen*): o executor empurra cada
    linha por ``on_log`` e o serviço a bufferiza/transmite sem mutá-la.

    O ``timestamp`` **não** vive aqui: é adicionado pelo serviço ao bufferizar e
    transmitir (``seq`` monotônico + ``em`` naive-UTC no ``LinhaLogView`` do
    ``stage_service``), mantendo a costura do executor desacoplada do transporte
    e do relógio.

    Attributes:
        etapa: A etapa do provisionamento que emitiu a linha — por exemplo
            ``"subir_conteineres"``, ``"aguardar_saude"``, ``"carregar_abilities"``
            ou ``"verificar_isolamento"`` (Req. 2.4).
        fluxo: A natureza da linha — ``"comando"`` (o comando executado),
            ``"stdout"`` ou ``"stderr"`` (a saída do comando), ou ``"info"`` (uma
            marcação de etapa). A tela distingue visualmente por este campo
            (Req. 2.4).
        texto: O conteúdo textual da linha (o comando ou a linha de saída).
    """

    etapa: str
    fluxo: str
    texto: str


@dataclass(frozen=True)
class ResultadoComando:
    """Resultado de um comando de ``compose``: código de saída + linhas emitidas.

    Devolvido por :meth:`ComposeExecutor.subir`/:meth:`ComposeExecutor.derrubar`.
    É **imutável** (*frozen*); ``linhas`` carrega as :class:`LinhaLog` emitidas
    durante o comando, na ordem em que saíram (as mesmas já empurradas por
    ``on_log`` em streaming), para que o chamador componha o resultado da etapa.

    Attributes:
        codigo: O código de saída (*exit code*) do comando. ``0`` indica sucesso;
            ``codigo != 0`` indica falha ao subir (Req. 3.5) ou ao derrubar
            (Req. 8.4).
        linhas: As :class:`LinhaLog` emitidas pelo comando, em ordem.
    """

    codigo: int
    linhas: list[LinhaLog] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Costura injetável (task 1.1)
# ---------------------------------------------------------------------------


@runtime_checkable
class ComposeExecutor(Protocol):
    """Executa o laboratório Docker em streaming (ponto de injeção — Req. 3).

    Um ``Protocol`` ``@runtime_checkable`` que define a superfície mínima que o
    Estágio 5 usa para **subir** e **derrubar** o laboratório contido, seguindo o
    precedente das demais costuras injetáveis do pacote (``CalderaLoader``,
    ``ContainerInspector``): qualquer objeto que exponha ``subir`` e ``derrubar``
    com estas assinaturas satisfaz o protocolo (duck typing estrutural), sem
    herança explícita. Os testes injetam um *fake* (sem subprocess, sem daemon
    Docker); a produção injeta a :class:`DockerComposeExecutor` real (task 1.2).

    Ambas as operações recebem o caminho do arquivo de ``compose`` e um callback
    ``on_log`` **keyword-only**, pelo qual cada :class:`LinhaLog` é empurrada em
    tempo quase real (Req. 2.2); devolvem um :class:`ResultadoComando` com o
    código de saída e as linhas emitidas.
    """

    def subir(
        self, compose_file: str, *, on_log: Callable[[LinhaLog], None]
    ) -> ResultadoComando:
        """``docker compose -f <compose_file> up -d --wait``, streamando os logs.

        Usa ``--wait`` para que o Compose aguarde os ``healthcheck`` definidos no
        ``compose``; cada linha de ``stdout``/``stderr`` vira uma
        ``LinhaLog(etapa="subir_conteineres", ...)`` empurrada por ``on_log`` em
        tempo quase real (Req. 2.2). ``codigo != 0`` no :class:`ResultadoComando`
        indica falha ao subir (Req. 3.5).

        Args:
            compose_file: Caminho do ``docker/docker-compose.yml`` curado a subir.
            on_log: Callback invocado para cada :class:`LinhaLog` emitida, na
                ordem em que sai do comando.

        Returns:
            O :class:`ResultadoComando` com o código de saída e as linhas emitidas.
        """
        ...

    def derrubar(
        self, compose_file: str, *, on_log: Callable[[LinhaLog], None]
    ) -> ResultadoComando:
        """``docker compose -f <compose_file> down``, para o desprovisionamento (Req. 8).

        Remove/para os contêineres do laboratório que foram subidos para a
        campanha, liberando os recursos (Req. 8.2); cada linha de saída vira uma
        ``LinhaLog(etapa="desprovisionar", ...)`` empurrada por ``on_log``.
        ``codigo != 0`` indica falha ao derrubar, com os recursos remanescentes
        identificáveis pelas linhas (Req. 8.4).

        Args:
            compose_file: Caminho do ``docker/docker-compose.yml`` curado a derrubar.
            on_log: Callback invocado para cada :class:`LinhaLog` emitida, na
                ordem em que sai do comando.

        Returns:
            O :class:`ResultadoComando` com o código de saída e as linhas emitidas.
        """
        ...


# ---------------------------------------------------------------------------
# Implementação de produção (task 1.2)
# ---------------------------------------------------------------------------

# Nomes de serviço do laboratório de ataque no ``docker/docker-compose.yml``
# curado — as **chaves de serviço** do compose (não os ``container_name``). O
# ``up`` do executor nomeia EXPLICITAMENTE apenas estes quatro serviços, de modo
# que a camada de aplicação da Pipeline_UI nunca sobe junto: o perfil
# ``containers`` (``frontend``/``backend``) só sobe sob ``--profile containers``,
# e ``postgres`` (banco da aplicação) jamais é nomeado aqui (Req. 3.4).
#
# São **constantes do backend**, fixas no código — nunca interpoladas a partir de
# entrada do usuário —, fechando injeção de comando junto com a forma de lista de
# argumentos (sem shell) usada pelo subprocess (steering de segurança).
SERVICOS_LABORATORIO: tuple[str, ...] = (
    "caldera-attacker",
    "kali-attacker",
    "nginx-target",
    "mariadb-target",
)

# Nome de imagem **estavel** por servico do laboratorio (Req. 11.4). Espelha os
# ``image:`` fixados no ``docker/docker-compose.yml``, de modo que o executor
# reconheca uma imagem ja construida independentemente do nome do projeto/pasta
# de build. Sao constantes do backend (nunca interpoladas de entrada do usuario).
IMAGENS_LABORATORIO: dict[str, str] = {
    "caldera-attacker": "sticks-caldera-attacker:latest",
    "kali-attacker": "sticks-kali-attacker:latest",
    "nginx-target": "sticks-nginx-target:latest",
    "mariadb-target": "sticks-mariadb-target:latest",
}


class DockerComposeExecutor:
    """Implementação de produção da costura :class:`ComposeExecutor` (task 1.2).

    Executa o ``docker compose`` contra o ``docker/docker-compose.yml`` curado via
    :mod:`subprocess` na **forma de lista de argumentos** (nunca ``shell=True``):
    o caminho do compose e os nomes de serviço são **constantes do backend**
    (:data:`SERVICOS_LABORATORIO`), fixos no código e jamais interpolados a partir
    de entrada do usuário — o que, somado à ausência de shell, fecha injeção de
    comando (steering de segurança).

    Lê ``stdout``/``stderr`` **linha a linha** e empurra cada linha por ``on_log``
    em tempo quase real (Req. 2.2), de modo que o serviço reencaminhe as linhas ao
    terminal de logs ao vivo da tela (Req. 2). O ``codigo`` do
    :class:`ResultadoComando` é o *exit code* do processo (``codigo != 0`` indica
    falha — Req. 3.5, 8.4).

    O ``subir`` roda ``docker compose -f <compose> up -d --wait`` nomeando
    EXPLICITAMENTE apenas os serviços do laboratório de ataque — nunca o perfil
    ``containers`` (``frontend``/``backend``) nem ``postgres`` (Req. 3.4). Usa
    ``--wait`` para que o próprio Compose aguarde os ``healthcheck`` definidos no
    arquivo antes de retornar. O ``derrubar`` roda ``docker compose -f <compose>
    down`` para o desprovisionamento (Req. 8.2).

    Satisfaz o :class:`ComposeExecutor` estruturalmente (expõe ``subir`` e
    ``derrubar`` com as assinaturas do protocolo), sem herança explícita.

    _Requisitos: 2.2, 3.1, 3.3, 3.4, 8.2_
    """

    def __init__(
        self,
        *,
        docker_bin: str = "docker",
        imagem_existe: Optional[Callable[[str], bool]] = None,
    ) -> None:
        """Monta o executor com o binário ``docker`` e a verificação de imagem.

        Args:
            docker_bin: O executável do Docker a invocar (default ``"docker"``). É
                o caminho do compose e os nomes de serviço continuam sendo
                constantes do backend, nunca de entrada do usuário.
            imagem_existe: Predicado read-only ``(nome_imagem) -> bool`` que diz se
                uma imagem já existe na Docker Engine local (Req. 11.1). Default:
                :meth:`_imagem_existe_docker` (via ``docker image inspect``). Os
                testes injetam um *fake* para exercitar reuso/build sem daemon.
        """
        self._docker_bin = docker_bin
        self._imagem_existe: Callable[[str], bool] = (
            imagem_existe if imagem_existe is not None else self._imagem_existe_docker
        )

    def subir(
        self, compose_file: str, *, on_log: Callable[[LinhaLog], None]
    ) -> ResultadoComando:
        """``docker compose -f <compose_file> up -d --wait <serviços do laboratório>``.

        Nomeia EXPLICITAMENTE apenas os serviços do laboratório de ataque
        (:data:`SERVICOS_LABORATORIO`) — nunca o perfil ``containers`` nem
        ``postgres`` (Req. 3.4). ``--wait`` deixa o Compose aguardar os
        ``healthcheck`` do arquivo. Cada linha de saída vira uma
        ``LinhaLog(etapa="subir_conteineres", ...)`` empurrada por ``on_log`` em
        tempo quase real (Req. 2.2); ``codigo != 0`` indica falha (Req. 3.5).

        Args:
            compose_file: Caminho do ``docker/docker-compose.yml`` curado a subir.
            on_log: Callback invocado para cada :class:`LinhaLog` emitida, em ordem.

        Returns:
            O :class:`ResultadoComando` com o *exit code* e as linhas emitidas.
        """
        # Build-condicional (Req. 11.1-11.3): reusa as imagens ja construidas e so
        # builda os servicos cuja imagem falta. Imagens presentes -> up --no-build
        # (sem internet/rebuild); faltando alguma -> build dos faltantes e dai up.
        faltantes = [
            servico
            for servico in SERVICOS_LABORATORIO
            if not self._imagem_existe(IMAGENS_LABORATORIO[servico])
        ]
        linhas: list[LinhaLog] = []
        if faltantes:
            build_argv = [
                self._docker_bin,
                "compose",
                "-f",
                compose_file,
                "build",
                *faltantes,
            ]
            build = self._executar(
                build_argv, etapa="subir_conteineres", on_log=on_log
            )
            linhas.extend(build.linhas)
            if build.codigo != 0:
                # Falha de build (ex.: pacote/mirror indisponivel): nao sobe.
                return ResultadoComando(codigo=build.codigo, linhas=linhas)

        up_argv = [
            self._docker_bin,
            "compose",
            "-f",
            compose_file,
            "up",
            "-d",
            "--wait",
            "--no-build",
            *SERVICOS_LABORATORIO,
        ]
        up = self._executar(up_argv, etapa="subir_conteineres", on_log=on_log)
        linhas.extend(up.linhas)
        return ResultadoComando(codigo=up.codigo, linhas=linhas)

    def derrubar(
        self, compose_file: str, *, on_log: Callable[[LinhaLog], None]
    ) -> ResultadoComando:
        """``docker compose -f <compose_file> down`` para o desprovisionamento (Req. 8).

        Para/remove os contêineres do laboratório subidos para a campanha,
        liberando os recursos (Req. 8.2); cada linha de saída vira uma
        ``LinhaLog(etapa="desprovisionar", ...)`` empurrada por ``on_log``.
        ``codigo != 0`` indica falha ao derrubar, com os recursos remanescentes
        identificáveis pelas linhas (Req. 8.4).

        Args:
            compose_file: Caminho do ``docker/docker-compose.yml`` curado a derrubar.
            on_log: Callback invocado para cada :class:`LinhaLog` emitida, em ordem.

        Returns:
            O :class:`ResultadoComando` com o *exit code* e as linhas emitidas.
        """
        # ``down`` SEM ``--rmi`` e SEM ``-v``: para/remove os conteineres e a rede
        # do projeto, mas PRESERVA as imagens construidas/reaproveitadas, de modo
        # que a proxima campanha as reuse sem novo build (Req. 8.2, 11.6).
        argv = [
            self._docker_bin,
            "compose",
            "-f",
            compose_file,
            "down",
        ]
        return self._executar(argv, etapa="desprovisionar", on_log=on_log)

    def _imagem_existe_docker(self, nome_imagem: str) -> bool:
        """Diz se ``nome_imagem`` ja existe na Docker Engine local (read-only — Req. 11.1).

        Usa ``docker image inspect <nome>`` na forma de lista de argumentos (sem
        shell): ``returncode == 0`` quando a imagem existe. Inspecao somente de
        leitura — nao cria, baixa nem modifica imagem alguma. Docker ausente
        (``FileNotFoundError``) e tratado como "imagem inexistente" (fail-safe:
        leva ao caminho de build, que entao reporta a ausencia do docker).
        """
        import subprocess

        try:
            r = subprocess.run(  # noqa: S603 — lista fixa, sem shell
                [self._docker_bin, "image", "inspect", nome_imagem],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return False
        return r.returncode == 0

    def _executar(
        self,
        argv: list[str],
        *,
        etapa: str,
        on_log: Callable[[LinhaLog], None],
    ) -> ResultadoComando:
        """Roda ``argv`` (lista de argumentos, sem shell) streamando a saída por ``on_log``.

        Emite primeiro uma ``LinhaLog(fluxo="comando")`` com a linha de comando
        executada, depois lê ``stdout``/``stderr`` **linha a linha** empurrando
        cada uma por ``on_log`` em tempo quase real (Req. 2.2). ``stderr`` é
        redirecionado para o mesmo fluxo de ``stdout`` para preservar a ordem
        relativa das linhas como aparecem no terminal; cada linha é marcada com o
        ``fluxo`` apropriado. Devolve o :class:`ResultadoComando` com o *exit code*
        do processo e todas as linhas emitidas, na ordem.

        Usa a forma de lista de argumentos (``shell=False``, o default do
        :mod:`subprocess`), de modo que ``argv`` jamais passe por um shell — nenhum
        metacaractere é interpretado e não há caminho de injeção de comando.
        """
        import subprocess

        linhas: list[LinhaLog] = []

        def _emitir(fluxo: str, texto: str) -> None:
            linha = LinhaLog(etapa=etapa, fluxo=fluxo, texto=texto)
            linhas.append(linha)
            on_log(linha)

        # Linha "comando": o comando executado, para o terminal (Req. 2.4). Não é
        # um comando de shell — apenas uma representação legível da lista de args.
        _emitir("comando", " ".join(argv))

        try:
            processo = subprocess.Popen(  # noqa: S603 — argv é lista fixa, sem shell
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered: entrega cada linha assim que sai
            )
        except FileNotFoundError as exc:
            # Docker ausente/indisponível: fail-closed com codigo != 0 e a causa
            # como uma linha de stderr, sem derrubar o provisionamento.
            _emitir("stderr", str(exc))
            return ResultadoComando(codigo=127, linhas=linhas)

        # Lê linha a linha enquanto o processo corre, empurrando em tempo quase
        # real. stderr já está em stdout (STDOUT), preservando a ordem do terminal.
        assert processo.stdout is not None
        for bruta in processo.stdout:
            _emitir("stdout", bruta.rstrip("\n"))
        processo.stdout.close()

        codigo = processo.wait()
        return ResultadoComando(codigo=codigo, linhas=linhas)


__all__ = [
    "LinhaLog",
    "ResultadoComando",
    "ComposeExecutor",
    "DockerComposeExecutor",
    "SERVICOS_LABORATORIO",
    "IMAGENS_LABORATORIO",
]
