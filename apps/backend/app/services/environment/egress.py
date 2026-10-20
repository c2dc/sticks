"""Egress-window seam of Stage 5 — one way at a time (containment skeleton).

This module hosts the **containment-critical** seams that keep a target host
from ever being, at the same instant, (a) reachable from the internet **and**
(b) attached to the lab attack networks alongside the other targets (Req. 5.1).

Sub-stage **5.1 (Build/online)** needs a *controlled* egress window to download
and install the heavy prerequisites each host needs; sub-stage **5.2
(Runtime/isolated)** must then re-isolate the host and **positively verify** the
loss of egress before any offline setup runs. The pieces live here:

``EGRESS_NETWORK``
    The name of the ephemeral **bridge** network (NOT ``internal: true``) that
    grants a single target internet access during the 5.1 window. It only exists
    for the duration of the window and is torn down afterwards (and swept in the
    deprovision — Req. 10).

``EgressWindowResult``
    The typed, *frozen* outcome of opening/closing the window for a container.

``EgressWindow``
    The ``@runtime_checkable Protocol`` that opens and closes the egress window
    of a single target, **one way at a time**: ``open`` strictly runs
    ``detach(attack networks)`` **before** ``attach(EGRESS_NETWORK)`` — it never
    leaves the two together — and ``close`` runs ``detach(EGRESS_NETWORK)``. The
    strict ordering is exactly what preserves the containment invariant (Req.
    5.1, 5.2, 5.3).

``DockerEgressWindow``
    The production implementation, driving the
    :class:`~app.services.environment.compose_executor.ComposeExecutor`
    ``detach_network``/``attach_network`` operations. It is **injectable**: the
    tests inject a fake that records the attach/detach order, so no test touches
    the Docker daemon or the public network.

The ``EgressProber`` / ``EgressVerdict`` / ``egress_lost`` pieces (positive,
read-only verification of egress loss) belong to task 4 and will be added to
this same module in a dedicated section below — ``egress.py`` is structured so
that addition is purely additive and never has to touch the ``EgressWindow``
pieces defined here.

_Requirements: 5.1, 5.2, 5.3_
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable

from app.services.containment.subnets import (
    INTERNAL_NETWORK_SUBNETS,
    STATIC_IP_CONTAINERS,
)
from app.services.environment.compose_executor import ComposeExecutor, LogLine
from app.services.environment.topology_builder import (
    HOST_CONTAINERS,
    NETWORK_MEMBERS,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The ephemeral egress network used only during the 5.1 build window. It is a
#: **bridge** network (NOT ``internal: true``), so a target attached to it can
#: reach the internet; it only ever exists while a single host's egress window
#: is open, and the deprovision sweeps it if it survived an error (Req. 10). It
#: is a backend constant, never interpolated from user input.
EGRESS_NETWORK: str = "sticks-egress-ephemeral"


#: Docker Compose prefixes every network it creates with the project name
#: (``name: sticks`` in ``infra/docker/docker-compose.yml``), so the three
#: ``internal: true`` attack networks exist in the Docker Engine as
#: ``sticks-caldera-kali-network`` / ``sticks-kali-nginx-network`` /
#: ``sticks-nginx-db-network`` — NOT under their bare compose keys. The isolation
#: source of truth (:data:`INTERNAL_NETWORK_SUBNETS`) keeps the bare logical
#: names (used for subnet cross-checks); the network **operations**
#: (attach/detach) must address the real Docker names, so we resolve them with
#: this prefix. It mirrors :data:`EGRESS_NETWORK`, which is already prefixed, and
#: the ``HOST_CONTAINERS`` real-container mapping.
LAB_PROJECT_PREFIX: str = "sticks-"


def _real_network_name(logical: str) -> str:
    """Resolve a compose network key to its real Docker name (project-prefixed).

    Idempotent: a name that already carries the prefix is returned unchanged, so
    callers can pass either the logical key or the real name.
    """
    return logical if logical.startswith(LAB_PROJECT_PREFIX) else f"{LAB_PROJECT_PREFIX}{logical}"


def attack_networks_for(containers: list[str]) -> list[str]:
    """Return the lab **attack** networks a target must be re-isolated onto.

    The attack networks are the three ``internal: true`` compose networks of the
    lab (``caldera-kali-network``/``kali-nginx-network``/``nginx-db-network``),
    the single source of truth being
    :data:`~app.services.containment.subnets.INTERNAL_NETWORK_SUBNETS`. They are
    **backend constants** — never derived from user input — and are the networks
    the egress window must ``detach`` before opening egress and that 5.2 reattaches
    when it re-isolates the host.

    Args:
        containers: The target container names (accepted for signature parity with
            the orchestration; the lab's attack networks are fixed and shared by
            all targets, so the value does not depend on the specific containers).

    Returns:
        The attack network names, in a stable order.
    """
    return [_real_network_name(name) for name in INTERNAL_NETWORK_SUBNETS]


# Reverse map: real container name -> logical host (e.g. "sticks-nginx-target"
# -> "nginx"), so re-isolation can look the host up in the compose topology.
_CONTAINER_TO_HOST: dict[str, str] = {c: h for h, c in HOST_CONTAINERS.items()}

# Per-logical-network subnet prefix (first three octets), to pick the host's
# fixed IP that belongs to each network from STATIC_IP_CONTAINERS.
def _subnet_prefix(logical_network: str) -> str:
    cidr = INTERNAL_NETWORK_SUBNETS.get(logical_network, "")
    octets = cidr.split("/")[0].split(".")
    return ".".join(octets[:3]) + "." if len(octets) >= 3 else ""


def attack_networks_with_ip_for(container: str) -> list[tuple[str, Optional[str]]]:
    """Resolve the (real network, fixed IP) pairs to re-isolate ``container`` onto.

    Restores the EXACT curated compose topology for a target: only the attack
    networks the host actually belongs to (:data:`NETWORK_MEMBERS`), each paired
    with the host's FIXED compose IP on that network (derived from
    :data:`STATIC_IP_CONTAINERS` by subnet). This is what sub-stage 5.2
    (``reisolar``) uses so the host comes back on ``192.168.20.30`` /
    ``192.168.30.30`` (etc.) — the addresses the abilities target — instead of a
    dynamic Docker address on every lab network. Networks are returned under
    their real (project-prefixed) Docker names.

    Falls back to all attack networks with no fixed IP for an unknown container
    (defensive — never raises), preserving the previous best-effort behavior.
    """
    host = _CONTAINER_TO_HOST.get(container)
    if host is None:
        return [(net, None) for net in attack_networks_for([container])]

    host_ips = [ip for ip, name in STATIC_IP_CONTAINERS.items() if name == host]
    pairs: list[tuple[str, Optional[str]]] = []
    for logical_net, members in NETWORK_MEMBERS.items():
        if host not in members:
            continue
        prefix = _subnet_prefix(logical_net)
        ip = next((i for i in host_ips if prefix and i.startswith(prefix)), None)
        pairs.append((_real_network_name(logical_net), ip))
    return pairs


# ---------------------------------------------------------------------------
# EgressWindow seam (task 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EgressWindowResult:
    """Typed outcome of opening/closing the egress window for one container.

    *Frozen* (immutable): the orchestration reads ``opened``/``closed`` to decide
    whether the window transition succeeded, and ``reason`` to compose the
    ``failed_step`` cause (Req. 8.1) when it did not.

    Attributes:
        container: The target container the window was operated on.
        opened: ``True`` when the window was opened — the target is now attached to
            :data:`EGRESS_NETWORK` and **detached** from the attack networks (never
            both at once). ``False`` otherwise.
        closed: ``True`` when the window was closed — the target was detached from
            :data:`EGRESS_NETWORK`. After this the target carries no attack network
            until 5.2 reattaches it.
        reason: A human-readable cause when the operation did not fully succeed,
            ``None`` on success.
    """

    container: str
    opened: bool = False
    closed: bool = False
    reason: Optional[str] = None


@runtime_checkable
class EgressWindow(Protocol):
    """Open/close a target's egress window, **one way at a time** (Req. 5.2, 5.3).

    A ``@runtime_checkable Protocol`` — like the package's other injectable seams
    (``ComposeExecutor``, ``ContainerInspector``) — so any object exposing ``open``
    and ``close`` with these signatures satisfies it structurally, without explicit
    inheritance. The tests inject a fake that records the attach/detach order;
    production injects :class:`DockerEgressWindow`.

    The containment invariant (Req. 5.1) is enforced by the **strict order** of the
    network mutations: ``open`` must ``detach`` the attack networks **before** it
    ``attach``es :data:`EGRESS_NETWORK`, so the two are never attached together at
    any instant.
    """

    def open(
        self,
        container: str,
        attack_networks: list[str],
        *,
        on_log: Callable[[LogLine], None],
    ) -> EgressWindowResult:
        """``detach(attack networks)`` then ``attach(EGRESS_NETWORK)`` — never both.

        Opens the controlled egress window for ``container``: it first detaches the
        target from every attack network and only then attaches it to
        :data:`EGRESS_NETWORK`. It NEVER leaves the target attached to both at the
        same time (Req. 5.1, 5.2).

        Args:
            container: The target container to open the window for.
            attack_networks: The attack networks the target must be detached from
                before egress is attached (from :func:`attack_networks_for`).
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            An :class:`EgressWindowResult` with ``opened=True`` on success, or a
            ``reason`` on failure.
        """
        ...

    def close(
        self, container: str, *, on_log: Callable[[LogLine], None]
    ) -> EgressWindowResult:
        """``detach(EGRESS_NETWORK)`` — end the window (Req. 2.5, 5.3).

        Detaches ``container`` from :data:`EGRESS_NETWORK`, ending its internet
        access. After this the target carries no attack network until 5.2 reattaches
        it, so the containment invariant holds across the whole transition.

        Args:
            container: The target container to close the window for.
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            An :class:`EgressWindowResult` with ``closed=True`` on success, or a
            ``reason`` on failure.
        """
        ...


class DockerEgressWindow:
    """Production :class:`EgressWindow` driving a :class:`ComposeExecutor` (task 3).

    Opens the egress window by driving the executor's ``detach_network`` for every
    attack network **before** a single ``attach_network`` to :data:`EGRESS_NETWORK`,
    and closes it by ``detach_network`` from :data:`EGRESS_NETWORK`. The strict
    ``detach``-before-``attach`` order is what guarantees the containment invariant
    (Req. 5.1): the target is never on an attack network and the egress bridge at the
    same instant.

    The executor is **injectable** (a fake in the tests). The network names are
    backend constants (:data:`EGRESS_NETWORK` and the lab attack networks), never
    interpolated from user input, so there is no command-injection path (security
    steering).

    It satisfies :class:`EgressWindow` structurally (it exposes ``open`` and
    ``close`` with the protocol's signatures), without explicit inheritance.

    _Requirements: 5.1, 5.2, 5.3_
    """

    def __init__(self, executor: ComposeExecutor) -> None:
        """Assemble the window with the Compose executor that mutates the networks.

        Args:
            executor: The :class:`ComposeExecutor` whose ``attach_network`` /
                ``detach_network`` operations drive the real Docker network changes.
        """
        self._executor = executor

    def open(
        self,
        container: str,
        attack_networks: list[str],
        *,
        on_log: Callable[[LogLine], None],
    ) -> EgressWindowResult:
        """Detach every attack network, then attach :data:`EGRESS_NETWORK`.

        The detaches run **first and in full** (one way at a time); only after the
        target is off every attack network does the single attach to the egress
        bridge run. Any non-zero result short-circuits with ``opened=False`` and a
        ``reason`` — and, because egress is attached **last**, a failure during the
        detaches leaves the target with *fewer* networks, never with egress plus an
        attack network.
        """
        # 1) detach(attack networks) — strictly before any egress attach (Req. 5.2).
        for network in attack_networks:
            result = self._executor.detach_network(container, network, on_log=on_log)
            if result.code != 0:
                return EgressWindowResult(
                    container=container,
                    opened=False,
                    reason=(
                        f"falha ao desanexar a rede de ataque {network!r} de "
                        f"{container!r} (code={result.code})"
                    ),
                )

        # 2) ensure the ephemeral egress bridge exists before attaching to it.
        # Nothing else creates it (it lives only for the duration of the window
        # and is swept by the deprovision), so the window is responsible for
        # bringing it up. ``create_network`` is idempotent (an already-existing
        # network is a success). Older executors without ``create_network`` are
        # tolerated (the attach below then surfaces any real problem).
        creator = getattr(self._executor, "create_network", None)
        if creator is not None:
            created = creator(EGRESS_NETWORK, on_log=on_log)
            if created.code != 0:
                return EgressWindowResult(
                    container=container,
                    opened=False,
                    reason=(
                        f"falha ao criar a rede de egresso {EGRESS_NETWORK!r} "
                        f"(code={created.code})"
                    ),
                )

        # 3) attach(EGRESS_NETWORK) — only now, with no attack network attached.
        attached = self._executor.attach_network(
            container, EGRESS_NETWORK, on_log=on_log
        )
        if attached.code != 0:
            return EgressWindowResult(
                container=container,
                opened=False,
                reason=(
                    f"falha ao anexar a rede de egresso {EGRESS_NETWORK!r} a "
                    f"{container!r} (code={attached.code})"
                ),
            )

        return EgressWindowResult(container=container, opened=True)

    def close(
        self, container: str, *, on_log: Callable[[LogLine], None]
    ) -> EgressWindowResult:
        """Detach :data:`EGRESS_NETWORK`, ending the window (Req. 2.5, 5.3)."""
        result = self._executor.detach_network(
            container, EGRESS_NETWORK, on_log=on_log
        )
        if result.code != 0:
            return EgressWindowResult(
                container=container,
                closed=False,
                reason=(
                    f"falha ao desanexar a rede de egresso {EGRESS_NETWORK!r} de "
                    f"{container!r} (code={result.code})"
                ),
            )
        return EgressWindowResult(container=container, closed=True)


# ---------------------------------------------------------------------------
# EgressProber seam (task 4) — positive, read-only verification of egress loss.
# Kept in its own section so it never has to touch the EgressWindow pieces above.
# ---------------------------------------------------------------------------

#: The fixed external endpoint the read-only probe tries (and MUST fail) to reach
#: to prove a target has lost egress. A public IP (Cloudflare's resolver), a
#: **backend constant** — never interpolated from user input, so there is no
#: command-injection path. The probe only *reads* the reachability result; it
#: installs nothing and writes no payload.
PROBE_ENDPOINT: str = "1.1.1.1"

#: Short timeout (seconds) for the read-only reachability attempt. Kept small so a
#: lost-egress host fails fast (no route / timeout) instead of hanging the window.
PROBE_TIMEOUT_SECONDS: int = 3


@dataclass(frozen=True)
class EgressVerdict:
    """Typed, *frozen* outcome of the read-only egress probe for one container.

    The probe tries to reach a fixed external endpoint from **inside** the target
    and MUST fail: a failure to reach it means egress was lost (the positive
    verification Req. 5.4/5.5 demand), a success means the target still has
    internet, and an inconclusive run (the probe itself could not execute) is
    treated as *egress present* (fail-closed).

    Attributes:
        container: The target container the probe was run against.
        egress_lost: ``True`` when the external reachability attempt **failed**
            (no route / timeout) — i.e. egress was lost, which *approves* the host.
            ``False`` when the endpoint was reached (egress still present).
        conclusive: ``True`` when the probe actually ran and produced a trustworthy
            reachability result. ``False`` ⇒ **inconclusive** (daemon unavailable,
            ``exec`` error, unparsable output) ⇒ treated as egress present
            (fail-closed): :func:`egress_lost` reproves it even if ``egress_lost``
            happened to be ``True``.
        reason: A human-readable cause (e.g. why it was inconclusive, or the
            endpoint that was reached), ``None`` when not applicable.
    """

    container: str
    egress_lost: bool = False
    conclusive: bool = False
    reason: Optional[str] = None


@runtime_checkable
class EgressProber(Protocol):
    """Read-only probe of egress loss — tries an external endpoint; MUST fail.

    A ``@runtime_checkable Protocol`` — like the package's other injectable seams
    (``ComposeExecutor``, ``EgressWindow``) — so any object exposing ``probe`` with
    this signature satisfies it structurally, without explicit inheritance. The
    tests inject a fake returning scripted verdicts; production injects
    :class:`DockerEgressProber`.

    The probe is **read-only**: it only *reads* whether a fixed external endpoint
    is reachable from inside the target (:data:`PROBE_ENDPOINT`, short timeout). It
    installs nothing and writes no payload, so it never mutates the target. The
    loss of egress is **positively verified**, never presumed (Req. 3.2, 5.4, 5.5).
    """

    def probe(
        self, container: str, *, on_log: Callable[[LogLine], None]
    ) -> EgressVerdict:
        """Try to reach the fixed external endpoint from ``container``; MUST fail.

        Args:
            container: The target container to probe from.
            on_log: Callback invoked for each :class:`LogLine` emitted, in order.

        Returns:
            An :class:`EgressVerdict`: ``egress_lost=True, conclusive=True`` when
            the endpoint was unreachable (egress lost ⇒ approve); ``egress_lost=
            False, conclusive=True`` when it was reached (egress present ⇒ reprove);
            ``conclusive=False`` when the probe could not run (inconclusive ⇒
            treated as egress present, fail-closed).
        """
        ...


def egress_lost(verdicts: list[EgressVerdict]) -> bool:
    """Approve only when **every** target lost egress **conclusively** (fail-closed).

    This is the pure gate 5.2 consults before any offline setup runs: it returns
    ``True`` only when there is at least one verdict and **all** verdicts report
    both ``egress_lost`` and ``conclusive``. Consequences, all fail-closed:

    * An **empty** list reproves (``bool([]) is False``) — "no hosts verified" is
      never an approval.
    * A single host still with egress (``egress_lost=False``) reproves the batch.
    * A single **inconclusive** verdict (``conclusive=False``) reproves the batch,
      even if its ``egress_lost`` flag is ``True`` — inconclusive counts as egress
      present (Req. 5.4, 5.5).

    Args:
        verdicts: The per-host :class:`EgressVerdict` of the probe, one per target.

    Returns:
        ``True`` only when egress loss is positively verified for every host.
    """
    return bool(verdicts) and all(v.egress_lost and v.conclusive for v in verdicts)


class DockerEgressProber:
    """Production :class:`EgressProber` driving a :class:`ComposeExecutor` (task 4).

    Runs a **read-only** reachability attempt inside the target via the executor's
    ``exec_in`` — it installs nothing and writes no payload, it only *reads* whether
    :data:`PROBE_ENDPOINT` is reachable. The command is a fixed argument list
    (``bash -lc`` with a backend-constant probe), never interpolated from end-user
    input, so there is no command-injection path (security steering).

    The verdict mapping is **fail-closed**:

    * ``exec`` exit ``code != 0`` ⇒ the endpoint was **unreachable** ⇒
      ``egress_lost=True, conclusive=True`` (approve).
    * ``exec`` exit ``code == 0`` ⇒ the endpoint was **reached** ⇒
      ``egress_lost=False, conclusive=True`` (reprove: egress still present).
    * The probe could not run at all (e.g. ``exec`` failed to launch — surfaced by
      the executor) ⇒ ``conclusive=False`` ⇒ treated as egress present (reprove).

    The executor is **injectable** (a fake in the tests). It satisfies
    :class:`EgressProber` structurally (it exposes ``probe`` with the protocol's
    signature), without explicit inheritance.

    _Requirements: 3.2, 5.4, 5.5_
    """

    def __init__(self, executor: ComposeExecutor) -> None:
        """Assemble the prober with the Compose executor that runs the read-only probe.

        Args:
            executor: The :class:`ComposeExecutor` whose ``exec_in`` runs the
                read-only reachability attempt inside the target.
        """
        self._executor = executor

    def probe(
        self, container: str, *, on_log: Callable[[LogLine], None]
    ) -> EgressVerdict:
        """Run the read-only reachability attempt; a failure means egress lost.

        The probe opens a short-timeout TCP connection to :data:`PROBE_ENDPOINT`
        from inside the target and reports only its exit status. A non-zero exit
        (unreachable / timeout) is the expected, approving outcome; a zero exit
        means the target still reaches the internet.
        """
        # A fixed, read-only command: open a short-timeout TCP connection to the
        # backend-constant endpoint and read only its exit status. It installs
        # nothing and writes no payload. ``bash -lc`` keeps the argument-list form
        # the executor passes to ``docker exec`` without a host shell.
        probe_cmd = (
            f"timeout {PROBE_TIMEOUT_SECONDS} "
            f"bash -c '</dev/tcp/{PROBE_ENDPOINT}/443'"
        )
        argv = ["bash", "-lc", probe_cmd]
        result = self._executor.exec_in(container, argv, on_log=on_log)

        if result.code == 0:
            # Reached the external endpoint: egress is still present ⇒ reprove.
            return EgressVerdict(
                container=container,
                egress_lost=False,
                conclusive=True,
                reason=(
                    f"egresso ainda presente: {container!r} alcançou "
                    f"{PROBE_ENDPOINT!r}"
                ),
            )

        # Non-zero exit: the endpoint was unreachable (no route / timeout) ⇒ egress
        # lost ⇒ approve. The result is conclusive — the probe ran and produced a
        # reachability answer.
        return EgressVerdict(
            container=container,
            egress_lost=True,
            conclusive=True,
            reason=(
                f"egresso perdido: {container!r} não alcançou "
                f"{PROBE_ENDPOINT!r} (code={result.code})"
            ),
        )


__all__ = [
    "EGRESS_NETWORK",
    "attack_networks_for",
    "attack_networks_with_ip_for",
    "EgressWindowResult",
    "EgressWindow",
    "DockerEgressWindow",
    "PROBE_ENDPOINT",
    "PROBE_TIMEOUT_SECONDS",
    "EgressVerdict",
    "EgressProber",
    "egress_lost",
    "DockerEgressProber",
]
