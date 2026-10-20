"""Property test — containment invariant of the egress window (task 3.1).

# Feature: stage5-target-provisioning, Property 1: Invariante de contenção —
# nunca egresso e redes de ataque juntos. Para qualquer sequência de hosts e
# conjuntos de redes de ataque, ao abrir/encerrar a janela de egresso via
# ``DockerEgressWindow`` o conjunto de redes anexadas de um alvo NUNCA contém, em
# nenhum instante, a rede de egresso (``EGRESS_NETWORK``) E uma rede de ataque ao
# mesmo tempo; e toda transição que abre a janela faz ``detach`` das redes de
# ataque ANTES do ``attach`` da rede de egresso.

Validates: Requirements 5.1, 5.2, 5.3

Esta é uma property test de *feature* (``stage5-target-provisioning`` é uma
feature, não um bugfix): a propriedade é esperada **valer** sobre a implementação
atual de :class:`~app.services.environment.egress.DockerEgressWindow`.

Nenhum Docker nem rede pública é tocado: o seam ``ComposeExecutor`` é substituído
por um **fake** (:class:`_RecordingExecutor`) que mantém, por contêiner, o
conjunto de redes anexadas e registra a **ordem** de cada ``attach``/``detach``.
Após **cada** operação de rede o fake reavalia o invariante de contenção sobre o
estado corrente — logo, mesmo um instante intermediário em que egresso e ataque
coexistissem seria capturado. O teste gera sequências aleatórias de hosts e
conjuntos de redes de ataque e exercita ``open``/``close`` sobre elas.

As asserções cobrem:

1. **Invariante em todo instante** — em nenhum momento o conjunto anexado de um
   alvo contém a rede de egresso e uma rede de ataque juntas (verificado pelo
   fake a cada passo e novamente ao final).
2. **detach antes de attach** — ao abrir a janela, todos os ``detach`` das redes
   de ataque precedem o ``attach`` da rede de egresso no histórico ordenado.
3. **Estado coerente** — após ``open`` o alvo está só na rede de egresso; após
   ``close`` o alvo não está em nenhuma rede.

_Requirements: 5.1, 5.2, 5.3_
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.environment.compose_executor import CommandResult, LogLine
from app.services.environment.egress import (
    EGRESS_NETWORK,
    DockerEgressWindow,
)

# The lab attack networks (the three ``internal: true`` compose networks). The
# generator draws subsets/orderings of these so the property covers one, two or
# all three attack networks per host, in any order.
_ATTACK_NETWORKS: tuple[str, ...] = (
    "caldera-kali-network",
    "kali-nginx-network",
    "nginx-db-network",
)


# ---------------------------------------------------------------------------
# Fake ComposeExecutor — records attach/detach order and checks the invariant
# after EVERY network mutation. No Docker, no subprocess, no network.
# ---------------------------------------------------------------------------
@dataclass
class _RecordingExecutor:
    """A fake ``ComposeExecutor`` that tracks each container's attached networks.

    It satisfies the ``ComposeExecutor`` surface the window uses
    (``attach_network``/``detach_network``) structurally. After each mutation it
    asserts the containment invariant on the *current* attached set, so an
    intermediate instant where egress and an attack network coexist would fail the
    test immediately — not just the final state.

    ``events`` records an ordered, replayable history of ``("attach"|"detach",
    container, network)`` tuples for the detach-before-attach assertion.
    """

    # container -> set of currently attached networks
    attached: dict[str, set[str]] = field(default_factory=dict)
    # ordered history of ("attach"/"detach", container, network)
    events: list[tuple[str, str, str]] = field(default_factory=list)

    def _check_invariant(self, container: str) -> None:
        nets = self.attached.get(container, set())
        has_egress = EGRESS_NETWORK in nets
        has_attack = any(n in _ATTACK_NETWORKS for n in nets)
        # Property 1 core: never egress AND an attack network attached together.
        assert not (has_egress and has_attack), (
            f"invariante de contenção violado em {container!r}: "
            f"egresso e rede de ataque anexados juntos -> {sorted(nets)}"
        )

    def attach_network(
        self, container: str, network: str, *, on_log, ip=None
    ) -> CommandResult:
        self.attached.setdefault(container, set()).add(network)
        self.events.append(("attach", container, network))
        line = LogLine(step="egresso", stream="info", text=f"attach {network}")
        on_log(line)
        self._check_invariant(container)
        return CommandResult(code=0, lines=[line])

    def detach_network(
        self, container: str, network: str, *, on_log
    ) -> CommandResult:
        self.attached.setdefault(container, set()).discard(network)
        self.events.append(("detach", container, network))
        line = LogLine(step="egresso", stream="info", text=f"detach {network}")
        on_log(line)
        self._check_invariant(container)
        return CommandResult(code=0, lines=[line])


# ---------------------------------------------------------------------------
# Generators — a sequence of (host, attack-networks) operations. The same host
# may recur so re-open/close cycles are exercised; each host starts attached to
# its drawn attack networks (the lab's steady state before 5.1).
# ---------------------------------------------------------------------------
_hosts = st.sampled_from(["nginx-target", "mariadb-target", "kali-attacker"])
_attack_subsets = st.lists(
    st.sampled_from(_ATTACK_NETWORKS), min_size=1, max_size=3, unique=True
)
_operations = st.lists(
    st.tuples(_hosts, _attack_subsets), min_size=1, max_size=15
)


@settings(max_examples=100)
@given(operations=_operations)
def test_egress_window_never_mixes_egress_and_attack(
    operations: list[tuple[str, list[str]]],
) -> None:
    # Feature: stage5-target-provisioning, Property 1: Invariante de contenção —
    # nunca egresso e redes de ataque juntos.
    executor = _RecordingExecutor()
    window = DockerEgressWindow(executor)

    for container, attack_networks in operations:
        # Seed the steady state: the host is attached to its attack networks
        # before the egress window opens (as it is in the isolated lab).
        executor.attached[container] = set(attack_networks)
        executor._check_invariant(container)

        before = len(executor.events)

        opened = window.open(container, attack_networks, on_log=lambda _l: None)
        assert opened.opened is True

        # (3) After open: the host is attached ONLY to the egress network.
        assert executor.attached[container] == {EGRESS_NETWORK}

        # (2) detach-before-attach: within this open() the egress attach must come
        # strictly after every attack-network detach for this container.
        window_events = [
            e for e in executor.events[before:] if e[1] == container
        ]
        egress_attach_idx = next(
            i
            for i, e in enumerate(window_events)
            if e[0] == "attach" and e[2] == EGRESS_NETWORK
        )
        attack_detach_idxs = [
            i
            for i, e in enumerate(window_events)
            if e[0] == "detach" and e[2] in _ATTACK_NETWORKS
        ]
        assert all(idx < egress_attach_idx for idx in attack_detach_idxs), (
            "detach das redes de ataque deve preceder o attach do egresso"
        )
        # Every drawn attack network was detached before egress was attached.
        assert len(attack_detach_idxs) == len(set(attack_networks))

        closed = window.close(container, on_log=lambda _l: None)
        assert closed.closed is True

        # (3) After close: the host carries no network until 5.2 reattaches it.
        assert executor.attached[container] == set()

    # (1) Final sweep: the invariant holds for every container that was touched.
    for container in executor.attached:
        executor._check_invariant(container)
