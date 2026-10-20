"""Streaming Docker-lab execution seam (Stage 5).

This module defines the **injection point** that Stage 5 (Environment
provisioning) uses to **up** and **down** the contained lab of the curated
``docker/docker-compose.yml``, emitting each ``stdout``/``stderr`` line to a
callback — so the service forwards the lines to the screen's live log terminal
(Req. 2) and composes the result as a completion precondition (Req. 5) and the
failure messages (Req. 7).

As in the rest of the service packages (``provisioner``/``operationalizer``), the
seam is a ``@runtime_checkable Protocol`` and the external dependencies are
**injectable**: a *fake* in the tests (no subprocess, no Docker daemon) and the
real production implementation (:class:`DockerComposeExecutor`, task 1.2) when
``None``.

Typed DTOs emitted by the seam:

``LogLine``
    One provisioning log line (*frozen* ``dataclass``), for the screen terminal
    (Req. 2.2, 2.3): the step that emitted it, the stream (``comando`` |
    ``stdout`` | ``stderr`` | ``info``) and the text. The ``timestamp`` is added
    by the service when buffering/transmitting (``seq``/``at`` in the
    ``LogLineView``), keeping the executor seam decoupled from the transport.

``CommandResult``
    The result of a ``compose`` command (*frozen* ``dataclass``): the exit code
    (``code``) and the emitted lines (``lines``). ``code != 0`` indicates a
    failure (Req. 3.5, 8.4).

``ComposeExecutor``
    The ``@runtime_checkable Protocol`` that defines the minimal surface —
    ``up(compose_file, *, on_log)`` and ``down(compose_file, *, on_log)`` —
    satisfied structurally (duck typing) by any object that exposes these
    operations, without explicit inheritance.

The production implementation :class:`DockerComposeExecutor` (``docker compose``
subprocess, argument-list form, no shell) is defined in task 1.2.

_Requirements: 3.1, 3.2, 8.2_
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Typed DTOs emitted by the seam (task 1.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogLine:
    """One provisioning log line, for the screen terminal (Req. 2).

    A textual entry emitted by a provisioning step — a command executed, a
    ``stdout``/``stderr`` line of its output, or a step marker (``info``) —
    transmitted to the screen terminal in the order it was emitted (Req. 2.2,
    2.3). It is **immutable** (*frozen*): the executor pushes each line through
    ``on_log`` and the service buffers/transmits it without mutating it.

    The ``timestamp`` does **not** live here: it is added by the service when
    buffering and transmitting (monotonic ``seq`` + naive-UTC ``at`` in the
    ``stage_service`` ``LogLineView``), keeping the executor seam decoupled from
    the transport and the clock.

    Attributes:
        step: The provisioning step that emitted the line — for example
            ``"subir_conteineres"``, ``"aguardar_saude"``, ``"carregar_abilities"``
            or ``"verificar_isolamento"`` (Req. 2.4).
        stream: The nature of the line — ``"comando"`` (the executed command),
            ``"stdout"`` or ``"stderr"`` (the command output), or ``"info"`` (a
            step marker). The screen distinguishes visually by this field
            (Req. 2.4).
        text: The textual content of the line (the command or the output line).
        substage: The Stage-5 sub-stage that emitted the line — ``"5.1"`` (Build
            online) or ``"5.2"`` (Runtime isolated) —, or ``None`` for the
            **inherited** steps (``subir_conteineres``/``aguardar_saude``/
            ``carregar_abilities``/``verificar_isolamento``) that predate the
            sub-stage split (Req. 6.3). It is **optional** with a ``None`` default,
            so every existing ``LogLine(step=..., stream=..., text=...)`` call site
            stays valid unchanged (backward compatibility) and the inherited lines
            carry no sub-stage marker. The ``LogLineView`` of the WS channel
            propagates it to the frontend, preserving the existing order and
            monotonic ``seq``.
    """

    step: str
    stream: str
    text: str
    substage: Optional[str] = None


@dataclass(frozen=True)
class CommandResult:
    """Result of a ``compose`` command: exit code + emitted lines.

    Returned by :meth:`ComposeExecutor.up`/:meth:`ComposeExecutor.down`.
    It is **immutable** (*frozen*); ``lines`` carries the :class:`LogLine` emitted
    during the command, in the order they came out (the same ones already pushed
    through ``on_log`` in streaming), so the caller composes the step result.

    Attributes:
        code: The command's exit code. ``0`` indicates success; ``code != 0``
            indicates a failure on up (Req. 3.5) or on down (Req. 8.4).
        lines: The :class:`LogLine` emitted by the command, in order.
    """

    code: int
    lines: list[LogLine] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Injectable seam (task 1.1)
# ---------------------------------------------------------------------------


@runtime_checkable
class ComposeExecutor(Protocol):
    """Run the Docker lab in streaming (injection point — Req. 3).

    A ``@runtime_checkable Protocol`` that defines the minimal surface Stage 5
    uses to **up** and **down** the contained lab, following the precedent of the
    package's other injectable seams (``CalderaLoader``, ``ContainerInspector``):
    any object that exposes ``up`` and ``down`` with these signatures satisfies
    the protocol (structural duck typing), without explicit inheritance. The tests
    inject a *fake* (no subprocess, no Docker daemon); production injects the real
    :class:`DockerComposeExecutor` (task 1.2).

    Both operations receive the ``compose`` file path and a **keyword-only**
    ``on_log`` callback, through which each :class:`LogLine` is pushed in
    near-real time (Req. 2.2); they return a :class:`CommandResult` with the exit
    code and the emitted lines.
    """

    def up(
        self, compose_file: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        """``docker compose -f <compose_file> up -d --wait``, streaming the logs.

        Uses ``--wait`` so Compose waits on the ``healthcheck`` defined in the
        ``compose``; each ``stdout``/``stderr`` line becomes a
        ``LogLine(step="subir_conteineres", ...)`` pushed through ``on_log`` in
        near-real time (Req. 2.2). ``code != 0`` in the :class:`CommandResult`
        indicates a failure on up (Req. 3.5).

        Args:
            compose_file: Path of the curated ``docker/docker-compose.yml`` to up.
            on_log: Callback invoked for each :class:`LogLine` emitted, in the
                order it comes out of the command.

        Returns:
            The :class:`CommandResult` with the exit code and the emitted lines.
        """
        ...

    def down(
        self, compose_file: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        """``docker compose -f <compose_file> down``, for the deprovisioning (Req. 8).

        Removes/stops the lab containers that were brought up for the campaign,
        freeing the resources (Req. 8.2); each output line becomes a
        ``LogLine(step="desprovisionar", ...)`` pushed through ``on_log``.
        ``code != 0`` indicates a failure on down, with the remaining resources
        identifiable from the lines (Req. 8.4).

        Args:
            compose_file: Path of the curated ``docker/docker-compose.yml`` to down.
            on_log: Callback invoked for each :class:`LogLine` emitted, in the
                order it comes out of the command.

        Returns:
            The :class:`CommandResult` with the exit code and the emitted lines.
        """
        ...

    def attach_network(
        self,
        container: str,
        network: str,
        *,
        on_log: Callable[[LogLine], None],
        ip: Optional[str] = None,
    ) -> CommandResult:
        """``docker network connect [--ip <ip>] <network> <container>`` (mutates the network).

        A **provisioning action** used by Stage-5 sub-stages 5.1/5.2 to attach a
        target container to a network — the egress bridge during the build window
        (5.1) or the ``internal: true`` attack networks when re-isolating (5.2).
        It is **not** a verification step: the isolation gate and the egress probe
        remain read-only (Req. 9.2). Each output line becomes a :class:`LogLine`
        pushed through ``on_log``; ``code != 0`` indicates a failure.

        Args:
            container: The lab container to attach. A backend-controlled value,
                never interpolated from end-user input.
            network: The network to connect the container to. A backend constant
                (egress bridge or attack network), never from end-user input.
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            The :class:`CommandResult` with the exit code and the emitted lines.
        """
        ...

    def detach_network(
        self, container: str, network: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        """``docker network disconnect <network> <container>`` (mutates the network).

        A **provisioning action** used by Stage-5 sub-stages 5.1/5.2 to detach a
        target container from a network — the attack networks before opening the
        egress window (5.1) or the egress bridge before re-isolating (5.2). The
        strict ``detach`` (attack) *before* ``attach`` (egress) order is what
        guarantees the containment invariant: a target is never simultaneously on
        the internet and on the attack networks (Req. 5.2, 5.3). Each output line
        becomes a :class:`LogLine` pushed through ``on_log``; ``code != 0``
        indicates a failure.

        Args:
            container: The lab container to detach. A backend-controlled value,
                never interpolated from end-user input.
            network: The network to disconnect the container from. A backend
                constant, never from end-user input.
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            The :class:`CommandResult` with the exit code and the emitted lines.
        """
        ...

    def exec_in(
        self, container: str, argv: list[str], *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        """``docker exec <container> <argv...>`` (``argv`` is a list; no shell).

        A **provisioning action** used by Stage-5 sub-stages 5.1/5.2 to run the
        host setup inside a target container — online prerequisites during the
        build window (5.1) and the offline setup once egress loss is verified
        (5.2). The ``argv`` is passed as an **argument list** (never ``shell=True``
        at the subprocess layer); the curated setup commands derived from
        ``vulnerabilities`` are handed in as ``["bash", "-lc", <command>]``, so the
        injection surface stays contained to curated (human) content, never
        arbitrary end-user input (security steering). Each output line becomes a
        :class:`LogLine` pushed through ``on_log``; ``code != 0`` indicates a
        failure.

        Args:
            container: The lab container to exec into. A backend-controlled value,
                never interpolated from end-user input.
            argv: The command as an argument list (e.g. ``["bash", "-lc", cmd]``),
                run without a host shell.
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            The :class:`CommandResult` with the exit code and the emitted lines.
        """
        ...

    def create_network(
        self, network: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        """``docker network create <network>`` — create the ephemeral egress network.

        A **provisioning action** used by sub-stage 5.1 to create the egress
        bridge before attaching a target to it. Idempotent (an already-existing
        network is treated as success). ``network`` is a backend constant.
        """
        ...

    def remove_network(
        self, network: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        """``docker network rm <network>`` — remove an ephemeral network (teardown).

        A **teardown action** used by the Stage-6 deprovision to sweep the
        ephemeral egress network (``EGRESS_NETWORK``) that sub-stage 5.1 may have
        created and that could have **survived an error** (Req. 10.2). It is a
        best-effort cleanup: a *not-found* network (already gone) is **not** a
        residue — the deprovision treats it as success. ``network`` is a backend
        constant (never interpolated from end-user input); together with the
        absence of a shell this closes command injection (security steering). Each
        output line becomes a :class:`LogLine` pushed through ``on_log``;
        ``code != 0`` indicates the removal did not succeed (and the network may
        still linger).

        Args:
            network: The network to remove. A backend constant, never from
                end-user input.
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            The :class:`CommandResult` with the exit code and the emitted lines.
        """
        ...


# ---------------------------------------------------------------------------
# Production implementation (task 1.2)
# ---------------------------------------------------------------------------

# Lab-attack service names in the curated ``docker/docker-compose.yml`` — the
# compose **service keys** (not the ``container_name``). The executor's ``up``
# names EXPLICITLY only these four services, so the Pipeline_UI application layer
# is never brought up alongside: the ``containers`` profile
# (``frontend``/``backend``) only comes up under ``--profile containers``, and
# ``postgres`` (the application database) is never named here (Req. 3.4).
#
# They are **backend constants**, fixed in code — never interpolated from user
# input —, closing command injection together with the argument-list form (no
# shell) used by the subprocess (security steering).
LAB_SERVICES: tuple[str, ...] = (
    "caldera-attacker",
    "kali-attacker",
    "nginx-target",
    "mariadb-target",
)

# **Stable** image name per lab service (Req. 11.4). Mirrors the ``image:`` pinned
# in the ``docker/docker-compose.yml``, so the executor recognizes an
# already-built image regardless of the project/build-folder name. They are
# backend constants (never interpolated from user input).
LAB_IMAGES: dict[str, str] = {
    "caldera-attacker": "sticks-caldera-attacker:latest",
    "kali-attacker": "sticks-kali-attacker:latest",
    "nginx-target": "sticks-nginx-target:latest",
    "mariadb-target": "sticks-mariadb-target:latest",
}


class DockerComposeExecutor:
    """Production implementation of the :class:`ComposeExecutor` seam (task 1.2).

    Runs ``docker compose`` against the curated ``docker/docker-compose.yml`` via
    :mod:`subprocess` in the **argument-list form** (never ``shell=True``): the
    compose path and the service names are **backend constants**
    (:data:`LAB_SERVICES`), fixed in code and never interpolated from user input —
    which, together with the absence of a shell, closes command injection
    (security steering).

    It reads ``stdout``/``stderr`` **line by line** and pushes each line through
    ``on_log`` in near-real time (Req. 2.2), so the service forwards the lines to
    the screen's live log terminal (Req. 2). The ``code`` of the
    :class:`CommandResult` is the process exit code (``code != 0`` indicates a
    failure — Req. 3.5, 8.4).

    ``up`` runs ``docker compose -f <compose> up -d --wait`` naming EXPLICITLY
    only the lab-attack services — never the ``containers`` profile
    (``frontend``/``backend``) nor ``postgres`` (Req. 3.4). It uses ``--wait`` so
    Compose itself waits on the ``healthcheck`` defined in the file before
    returning. ``down`` runs ``docker compose -f <compose> down`` for the
    deprovisioning (Req. 8.2).

    It satisfies :class:`ComposeExecutor` structurally (it exposes ``up`` and
    ``down`` with the protocol's signatures), without explicit inheritance.

    _Requirements: 2.2, 3.1, 3.3, 3.4, 8.2_
    """

    def __init__(
        self,
        *,
        docker_bin: str = "docker",
        image_exists: Optional[Callable[[str], bool]] = None,
    ) -> None:
        """Assemble the executor with the ``docker`` binary and the image check.

        Args:
            docker_bin: The Docker executable to invoke (default ``"docker"``). The
                compose path and the service names remain backend constants, never
                from user input.
            image_exists: Read-only predicate ``(image_name) -> bool`` that tells
                whether an image already exists in the local Docker Engine (Req.
                11.1). Default: :meth:`_image_exists_docker` (via ``docker image
                inspect``). The tests inject a *fake* to exercise reuse/build
                without a daemon.
        """
        self._docker_bin = docker_bin
        self._image_exists: Callable[[str], bool] = (
            image_exists if image_exists is not None else self._image_exists_docker
        )

    def up(
        self, compose_file: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        """``docker compose -f <compose_file> up -d --wait <lab services>``.

        Names EXPLICITLY only the lab-attack services (:data:`LAB_SERVICES`) —
        never the ``containers`` profile nor ``postgres`` (Req. 3.4). ``--wait``
        lets Compose wait on the file's ``healthcheck``. Each output line becomes a
        ``LogLine(step="subir_conteineres", ...)`` pushed through ``on_log`` in
        near-real time (Req. 2.2); ``code != 0`` indicates a failure (Req. 3.5).

        Args:
            compose_file: Path of the curated ``docker/docker-compose.yml`` to up.
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            The :class:`CommandResult` with the exit code and the emitted lines.
        """
        # Conditional build (Req. 11.1-11.3): reuses the already-built images and
        # only builds the services whose image is missing. Images present -> up
        # --no-build (no internet/rebuild); some missing -> build the missing ones
        # and then up.
        missing = [
            service
            for service in LAB_SERVICES
            if not self._image_exists(LAB_IMAGES[service])
        ]
        lines: list[LogLine] = []
        if missing:
            build_argv = [
                self._docker_bin,
                "compose",
                "-f",
                compose_file,
                "build",
                *missing,
            ]
            build = self._run(
                build_argv, step="subir_conteineres", on_log=on_log
            )
            lines.extend(build.lines)
            if build.code != 0:
                # Build failure (e.g. package/mirror unavailable): does not up.
                return CommandResult(code=build.code, lines=lines)

        up_argv = [
            self._docker_bin,
            "compose",
            "-f",
            compose_file,
            "up",
            "-d",
            "--wait",
            "--no-build",
            *LAB_SERVICES,
        ]
        up = self._run(up_argv, step="subir_conteineres", on_log=on_log)
        lines.extend(up.lines)
        return CommandResult(code=up.code, lines=lines)

    def down(
        self, compose_file: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        """``docker compose -f <compose_file> down`` for the deprovisioning (Req. 8).

        Stops/removes the lab containers brought up for the campaign, freeing the
        resources (Req. 8.2); each output line becomes a
        ``LogLine(step="desprovisionar", ...)`` pushed through ``on_log``.
        ``code != 0`` indicates a failure on down, with the remaining resources
        identifiable from the lines (Req. 8.4).

        Args:
            compose_file: Path of the curated ``docker/docker-compose.yml`` to down.
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            The :class:`CommandResult` with the exit code and the emitted lines.
        """
        # ``down`` WITHOUT ``--rmi`` and WITHOUT ``-v``: stops/removes the project
        # containers and network, but PRESERVES the built/reused images, so the
        # next campaign reuses them without a new build (Req. 8.2, 11.6).
        argv = [
            self._docker_bin,
            "compose",
            "-f",
            compose_file,
            "down",
        ]
        return self._run(argv, step="desprovisionar", on_log=on_log)

    def attach_network(
        self,
        container: str,
        network: str,
        *,
        on_log: Callable[[LogLine], None],
        ip: Optional[str] = None,
    ) -> CommandResult:
        """``docker network connect [--ip <ip>] <network> <container>`` (argument-list form, no shell).

        Attaches ``container`` to ``network`` — the egress bridge during the build
        window (5.1) or an ``internal: true`` attack network when re-isolating
        (5.2). ``container``/``network`` are backend-controlled values (never
        interpolated from end-user input); together with the absence of a shell
        this closes command injection (security steering). Each output line becomes
        a ``LogLine(step="attach_network", ...)`` pushed through ``on_log``;
        ``code != 0`` indicates a failure.

        Args:
            container: The lab container to attach.
            network: The network to connect the container to.
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            The :class:`CommandResult` with the exit code and the emitted lines.
        """
        argv = [
            self._docker_bin,
            "network",
            "connect",
        ]
        # Restore the host's FIXED compose IP on re-isolation (5.2) so the attack
        # plane topology is byte-for-byte the curated one (abilities target fixed
        # IPs like 192.168.20.30). Without ``--ip`` Docker assigns a dynamic
        # address and the attacker can no longer route to the target.
        if ip:
            argv += ["--ip", ip]
        argv += [network, container]
        return self._run(argv, step="attach_network", on_log=on_log)

    def detach_network(
        self, container: str, network: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        """``docker network disconnect <network> <container>`` (argument-list form, no shell).

        Detaches ``container`` from ``network`` — the attack networks before
        opening the egress window (5.1) or the egress bridge before re-isolating
        (5.2). The strict ``detach`` (attack) before ``attach`` (egress) order
        preserves the containment invariant (Req. 5.2, 5.3). ``container``/
        ``network`` are backend-controlled values (never from end-user input).
        Each output line becomes a ``LogLine(step="detach_network", ...)`` pushed
        through ``on_log``; ``code != 0`` indicates a failure.

        Args:
            container: The lab container to detach.
            network: The network to disconnect the container from.
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            The :class:`CommandResult` with the exit code and the emitted lines.
        """
        argv = [
            self._docker_bin,
            "network",
            "disconnect",
            network,
            container,
        ]
        result = self._run(argv, step="detach_network", on_log=on_log)
        # Idempotent detach: disconnecting from a network the container is NOT
        # attached to is the desired end-state (not attached), not a failure.
        # ``docker network disconnect`` exits non-zero with an "is not connected
        # to network" / "not connected" message in that case; normalize it to
        # success so the egress window's strict ``detach`` (attack) BEFORE
        # ``attach`` (egress) ordering holds even when a given host is not on
        # every lab attack network (each target is attached only to a subset of
        # the three internal networks). This mirrors the deprovision's
        # already-gone handling and never weakens containment: the goal is that
        # the host is NOT on the network, which is exactly what a "not connected"
        # result guarantees.
        if result.code != 0 and self._network_not_connected(result):
            return CommandResult(code=0, lines=list(result.lines))
        return result

    @staticmethod
    def _network_not_connected(result: CommandResult) -> bool:
        """Whether a failed ``detach_network`` only means the container was not attached.

        Inspects the emitted lines for the daemon's "is not connected to network"
        / "not connected" marker. When present, the detach is a no-op success (the
        container already is off the network), so the caller treats it as code 0.
        """
        for line in result.lines:
            lowered = line.text.lower()
            if "is not connected to network" in lowered or "not connected" in lowered:
                return True
        return False

    def exec_in(
        self, container: str, argv: list[str], *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        """``docker exec <container> <argv...>`` (``argv`` is a list; no shell).

        Runs ``argv`` inside ``container`` to apply the host setup — online
        prerequisites during the build window (5.1) and the offline setup once
        egress loss is verified (5.2). The curated setup commands derived from
        ``vulnerabilities`` are handed in as ``["bash", "-lc", <command>]``: the
        list is passed verbatim to ``docker exec`` in the **argument-list form**
        (``shell=False`` at the subprocess layer — see :meth:`_run`), so the
        injection surface stays contained to curated (human) content, never
        arbitrary end-user input (security steering). Each output line becomes a
        ``LogLine(step="exec_in", ...)`` pushed through ``on_log``; ``code != 0``
        indicates a failure.

        Args:
            container: The lab container to exec into.
            argv: The command as an argument list (e.g. ``["bash", "-lc", cmd]``).
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            The :class:`CommandResult` with the exit code and the emitted lines.
        """
        full_argv = [
            self._docker_bin,
            "exec",
            container,
            *argv,
        ]
        return self._run(full_argv, step="exec_in", on_log=on_log)

    def create_network(
        self, network: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        """``docker network create --driver bridge <network>`` (argument-list form, no shell).

        Creates the ephemeral egress network of sub-stage 5.1 — a **bridge**
        network (NOT ``internal: true``) that grants a single target controlled
        internet access while its build window is open. Idempotent: a network
        that already exists makes ``docker network create`` exit non-zero with an
        "already exists" message, which the caller treats as success (the desired
        end-state — the network exists — is already met). ``network`` is a backend
        constant (never from end-user input); the argument-list form (no shell)
        closes command injection (security steering). Each output line becomes a
        ``LogLine(step="create_network", ...)`` pushed through ``on_log``.

        Args:
            network: The network to create.
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            The :class:`CommandResult` with the exit code and the emitted lines.
        """
        argv = [
            self._docker_bin,
            "network",
            "create",
            "--driver",
            "bridge",
            network,
        ]
        result = self._run(argv, step="create_network", on_log=on_log)
        # Idempotent create: an already-existing network is the desired end-state
        # (it exists), not a failure. ``docker network create`` exits non-zero with
        # an "already exists" message in that case; normalize it to success.
        if result.code != 0 and self._network_already_exists(result):
            return CommandResult(code=0, lines=list(result.lines))
        return result

    @staticmethod
    def _network_already_exists(result: CommandResult) -> bool:
        """Whether a failed ``create_network`` only means the network already exists."""
        for line in result.lines:
            lowered = line.text.lower()
            if "already exists" in lowered:
                return True
        return False

    def remove_network(
        self, network: str, *, on_log: Callable[[LogLine], None]
    ) -> CommandResult:
        """``docker network rm <network>`` (argument-list form, no shell).

        Removes the ephemeral egress network during the Stage-6 deprovision
        teardown — it sweeps :data:`~app.services.environment.egress.EGRESS_NETWORK`
        if it survived an error (Req. 10.2). ``network`` is a backend constant
        (never interpolated from end-user input); together with the absence of a
        shell this closes command injection (security steering). Each output line
        becomes a ``LogLine(step="remove_network", ...)`` pushed through
        ``on_log``; ``code != 0`` indicates the removal did not succeed (e.g. the
        network is still in use), which the caller maps to a residue. A *not-found*
        network surfaces as ``code != 0`` too, so the caller treats "already gone"
        as success by inspecting the emitted lines.

        Args:
            network: The network to remove.
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            The :class:`CommandResult` with the exit code and the emitted lines.
        """
        argv = [
            self._docker_bin,
            "network",
            "rm",
            network,
        ]
        return self._run(argv, step="remove_network", on_log=on_log)

    def _image_exists_docker(self, image_name: str) -> bool:
        """Tell whether ``image_name`` already exists in the local Docker Engine (read-only — Req. 11.1).

        Uses ``docker image inspect <name>`` in the argument-list form (no shell):
        ``returncode == 0`` when the image exists. Read-only inspection — it does
        not create, pull or modify any image. A missing Docker
        (``FileNotFoundError``) is treated as "image nonexistent" (fail-safe: it
        leads to the build path, which then reports the absence of docker).
        """
        import subprocess

        try:
            r = subprocess.run(  # noqa: S603 — fixed list, no shell
                [self._docker_bin, "image", "inspect", image_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return False
        return r.returncode == 0

    def _run(
        self,
        argv: list[str],
        *,
        step: str,
        on_log: Callable[[LogLine], None],
    ) -> CommandResult:
        """Run ``argv`` (argument list, no shell) streaming the output through ``on_log``.

        It first emits a ``LogLine(stream="comando")`` with the executed command
        line, then reads ``stdout``/``stderr`` **line by line** pushing each one
        through ``on_log`` in near-real time (Req. 2.2). ``stderr`` is redirected
        to the same ``stdout`` stream to preserve the relative order of the lines
        as they appear in the terminal; each line is tagged with the appropriate
        ``stream``. It returns the :class:`CommandResult` with the process exit
        code and all emitted lines, in order.

        It uses the argument-list form (``shell=False``, the :mod:`subprocess`
        default), so ``argv`` never passes through a shell — no metacharacter is
        interpreted and there is no command-injection path.
        """
        import subprocess

        lines: list[LogLine] = []

        def _emit(stream: str, text: str) -> None:
            line = LogLine(step=step, stream=stream, text=text)
            lines.append(line)
            on_log(line)

        # "comando" line: the executed command, for the terminal (Req. 2.4). It is
        # not a shell command — just a readable representation of the arg list.
        _emit("comando", " ".join(argv))

        try:
            process = subprocess.Popen(  # noqa: S603 — argv is a fixed list, no shell
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered: delivers each line as soon as it comes
            )
        except FileNotFoundError as exc:
            # Docker missing/unavailable: fail-closed with code != 0 and the cause
            # as a stderr line, without bringing down the provisioning.
            _emit("stderr", str(exc))
            return CommandResult(code=127, lines=lines)

        # Read line by line while the process runs, pushing in near-real time.
        # stderr is already in stdout (STDOUT), preserving the terminal order.
        assert process.stdout is not None
        for raw in process.stdout:
            _emit("stdout", raw.rstrip("\n"))
        process.stdout.close()

        code = process.wait()
        return CommandResult(code=code, lines=lines)


__all__ = [
    "LogLine",
    "CommandResult",
    "ComposeExecutor",
    "DockerComposeExecutor",
    "LAB_SERVICES",
    "LAB_IMAGES",
]
