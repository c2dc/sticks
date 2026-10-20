"""Property test — egress loss positively verified before any offline setup (task 4.1).

# Feature: stage5-target-provisioning, Property 2: Perda de egresso positivamente
# verificada antes de qualquer setup offline. Para todo host-alvo, nenhum comando
# de setup offline é aplicado antes de a sonda de egresso devolver, para TODOS os
# hosts, um veredito CONCLUSIVO de egresso perdido; um único veredito inconclusivo
# ou de egresso presente impede o setup offline e reprova o Estágio 5 (fail-closed).

Validates: Requirements 3.2, 3.5, 5.4, 5.5

Esta é uma property test de *feature* (``stage5-target-provisioning`` é uma
feature, não um bugfix): a propriedade é esperada **valer** sobre o gate puro
:func:`~app.services.environment.egress.egress_lost`, que a orquestração 5.2
consulta antes de aplicar qualquer setup offline.

Nenhum Docker nem rede pública é tocado: a sonda ``EgressProber`` é substituída por
um **fake** (:class:`_ScriptedProber`) que devolve vereditos roteirizados, e o
setup offline por um **fake** (:class:`_RecordingSetup`) que apenas registra em
quais hosts foi aplicado. Um pequeno driver (:func:`_run_substage_52`) replica a
regra de ordenação do design — *verificar egresso* via :func:`egress_lost` **antes**
de qualquer *setup offline* —, de modo que a propriedade verifique a ordenação
inspecionando o histórico do fake, não a implementação da orquestração completa (que
é da task 7).

Os geradores produzem, por host, um dos três estados de veredito possíveis:

* **perdido/conclusivo** — ``egress_lost=True, conclusive=True`` (único estado que
  aprova);
* **presente** — ``egress_lost=False, conclusive=True`` (egresso ainda presente);
* **inconclusivo** — ``conclusive=False`` (tratado como egresso presente,
  fail-closed), com ``egress_lost`` em qualquer valor para cobrir o caso perverso de
  "perdido mas inconclusivo".

As asserções cobrem:

1. **Gate conclusivo** — ``egress_lost(verdicts)`` é ``True`` se e só se há ao menos
   um host e TODOS os vereditos são perdido-e-conclusivo (oráculo independente).
2. **Nenhum setup offline sem verificação** — o setup offline só é aplicado quando o
   gate aprova; se reprova, NENHUM host recebe setup offline.
3. **Cobertura total quando aprova** — quando o gate aprova, o setup offline é
   aplicado a exatamente todos os hosts, uma vez cada.
4. **Fail-closed do inconclusivo** — um único veredito inconclusivo (mesmo com
   ``egress_lost=True``) impede o setup offline.
5. **Fail-closed da lista vazia** — sem hosts/vereditos, o gate reprova e nada é
   aplicado.

_Requirements: 3.2, 3.5, 5.4, 5.5
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.environment.egress import EgressVerdict, egress_lost


# ---------------------------------------------------------------------------
# Verdict states the generator draws per host. Modelled as an enum so the drawn
# plan is readable and the three fail-closed-relevant states are all exercised.
# ---------------------------------------------------------------------------
class _VerdictKind(Enum):
    LOST_CONCLUSIVE = "lost_conclusive"  # egresso perdido e conclusivo => aprova
    PRESENT = "present"  # egresso presente (conclusivo) => reprova
    INCONCLUSIVE = "inconclusive"  # inconclusivo => reprova (fail-closed)


def _verdict_for(container: str, kind: _VerdictKind) -> EgressVerdict:
    """Build the :class:`EgressVerdict` for a drawn kind.

    The ``INCONCLUSIVE`` kind deliberately sets ``egress_lost=True`` while keeping
    ``conclusive=False`` to exercise the perverse "lost but inconclusive" case the
    fail-closed rule must still reprove.
    """
    if kind is _VerdictKind.LOST_CONCLUSIVE:
        return EgressVerdict(
            container=container, egress_lost=True, conclusive=True
        )
    if kind is _VerdictKind.PRESENT:
        return EgressVerdict(
            container=container, egress_lost=False, conclusive=True
        )
    # INCONCLUSIVE: conclusive=False, lost flag set to the perverse True.
    return EgressVerdict(container=container, egress_lost=True, conclusive=False)


# ---------------------------------------------------------------------------
# Fakes — a scripted prober (returns the drawn verdict) and a recording offline
# setup (only records which hosts it was applied to). No Docker, no network.
# ---------------------------------------------------------------------------
@dataclass
class _ScriptedProber:
    """A fake ``EgressProber`` returning a scripted verdict per container."""

    verdicts: dict[str, EgressVerdict]

    def probe(self, container: str, *, on_log) -> EgressVerdict:
        return self.verdicts[container]


@dataclass
class _RecordingSetup:
    """A fake offline ``TargetSetup`` that records the hosts it was applied to.

    It stands in for ``TargetSetup.apply(..., phase=OFFLINE)``: the property only
    needs to know *whether and to which hosts* the offline setup ran, so the fake
    records the container names in call order and performs no real work.
    """

    applied: list[str] = field(default_factory=list)

    def apply_offline(self, container: str) -> None:
        self.applied.append(container)


# ---------------------------------------------------------------------------
# Driver — replicates the design's ordering rule for sub-stage 5.2: probe every
# host, consult the pure ``egress_lost`` gate, and ONLY on approval apply the
# offline setup. Mirrors the design's provision() snippet without pulling in the
# full orchestration (task 7).
# ---------------------------------------------------------------------------
def _run_substage_52(
    containers: list[str],
    prober: _ScriptedProber,
    setup: _RecordingSetup,
) -> bool:
    """Run probe -> egress_lost gate -> (only if approved) offline setup.

    Returns ``True`` when the gate approved and the offline setup ran for every
    host, ``False`` when the gate reproved and no offline setup ran.
    """
    verdicts = [prober.probe(c, on_log=lambda _l: None) for c in containers]
    # verificar_egresso: positive, fail-closed gate BEFORE any offline setup.
    if not egress_lost(verdicts):
        return False
    # setup_offline: only reached once egress loss is verified for ALL hosts.
    for container in containers:
        setup.apply_offline(container)
    return True


# ---------------------------------------------------------------------------
# Generators — a plan of (host, verdict-kind) with unique host names, 0..6 hosts
# (0 exercises the empty-list fail-closed branch).
# ---------------------------------------------------------------------------
_hosts = st.sampled_from(
    [
        "nginx-target",
        "mariadb-target",
        "kali-attacker",
        "caldera-attacker",
        "host-e",
        "host-f",
    ]
)
_kinds = st.sampled_from(list(_VerdictKind))
_plans = st.lists(
    st.tuples(_hosts, _kinds),
    max_size=6,
    unique_by=lambda pair: pair[0],
)


def _oracle_approves(kinds: list[_VerdictKind]) -> bool:
    """Independent oracle: approve iff non-empty and every kind is lost-conclusive."""
    return bool(kinds) and all(
        k is _VerdictKind.LOST_CONCLUSIVE for k in kinds
    )


@settings(max_examples=100)
@given(plan=_plans)
def test_no_offline_setup_without_conclusive_egress_loss(
    plan: list[tuple[str, _VerdictKind]],
) -> None:
    # Feature: stage5-target-provisioning, Property 2: Perda de egresso
    # positivamente verificada antes de qualquer setup offline.
    containers = [host for host, _ in plan]
    kinds = [kind for _, kind in plan]
    verdicts = {host: _verdict_for(host, kind) for host, kind in plan}

    prober = _ScriptedProber(verdicts)
    setup = _RecordingSetup()

    approved = _run_substage_52(containers, prober, setup)

    gate = egress_lost([verdicts[c] for c in containers])
    oracle = _oracle_approves(kinds)

    # (1) The gate matches the independent oracle: approve iff non-empty and ALL
    # verdicts are lost-and-conclusive.
    assert gate is oracle
    assert approved is gate

    if gate:
        # (3) When approved, offline setup ran for exactly every host, once each.
        assert setup.applied == containers
    else:
        # (2) When reproved, NO host received offline setup — the positive
        # verification is a hard precondition of any offline setup.
        assert setup.applied == []

    # (4) Fail-closed on inconclusive: any inconclusive verdict reproves and so
    # blocks all offline setup, even when its egress_lost flag is True.
    if any(k is _VerdictKind.INCONCLUSIVE for k in kinds):
        assert gate is False
        assert setup.applied == []

    # (5) Fail-closed on empty: no hosts => no approval => nothing applied.
    if not containers:
        assert gate is False
        assert setup.applied == []
