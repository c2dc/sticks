"""Example test for the fail-closed egress probe (task 4.2).

Validates: Requirements 5.4, 5.5 — a perda de egresso é **positivamente
verificada** (nunca presumida) e o portão é **fail-closed**: egresso perdido ⇒
aprova; egresso presente ⇒ reprova; inconclusivo ⇒ reprova.

Cobre, com exemplos concretos, as três situações da sonda e do portão puro
:func:`~app.services.environment.egress.egress_lost`:

- **Egresso perdido** — o ``exec`` read-only sai com ``code != 0`` (endpoint
  inalcançável) ⇒ :class:`DockerEgressProber` devolve
  ``egress_lost=True, conclusive=True`` e ``egress_lost([...])`` **aprova**
  (``True``). O mesmo veredito escrito à mão também aprova.
- **Egresso presente** — o ``exec`` sai com ``code == 0`` (endpoint alcançado) ⇒
  o prober devolve ``egress_lost=False, conclusive=True`` e o portão **reprova**
  (``False``).
- **Inconclusivo** — um :class:`EgressVerdict` com ``conclusive=False`` (mesmo com
  ``egress_lost=True``) ⇒ o portão **reprova** (``False``), fail-closed.

O ``ComposeExecutor`` é um **fake** (:class:`_FakeComposeExecutor`) que registra
cada ``exec_in`` e devolve códigos roteirizados — nenhum daemon Docker, nenhum
subprocess, nenhuma rede pública. A implementação de produção
``DockerComposeExecutor.exec_in`` não é tocada aqui.

_Requirements: 5.4, 5.5
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.services.environment.compose_executor import CommandResult, LogLine
from app.services.environment.egress import (
    DockerEgressProber,
    EgressVerdict,
    egress_lost,
)


# ---------------------------------------------------------------------------
# Fake ComposeExecutor recording exec_in calls (no Docker daemon, no shell)
# ---------------------------------------------------------------------------


@dataclass
class _ExecCall:
    """One recorded ``exec_in`` invocation: the container and the argv passed."""

    container: str
    argv: list[str]


class _FakeComposeExecutor:
    """Records each ``exec_in`` call and returns a single scripted exit code.

    Satisfies the ``exec_in`` surface structurally: it exposes ``exec_in`` with
    the protocol signature and nothing else is needed by ``DockerEgressProber``.
    Each call emits a ``LogLine`` through ``on_log`` (so the aggregation path is
    exercised) and returns a :class:`CommandResult` whose ``code`` is the scripted
    ``code`` (default ``0``).
    """

    def __init__(self, code: int = 0) -> None:
        self.calls: list[_ExecCall] = []
        self._code = code

    def exec_in(
        self,
        container: str,
        argv: list[str],
        *,
        on_log: Callable[[LogLine], None],
    ) -> CommandResult:
        self.calls.append(_ExecCall(container=container, argv=list(argv)))
        line = LogLine(step="verificar_egresso", stream="comando", text=" ".join(argv))
        on_log(line)
        return CommandResult(code=self._code, lines=[line])


# ---------------------------------------------------------------------------
# Egress lost — probe exec code != 0 ⇒ lost+conclusive ⇒ gate approves
# ---------------------------------------------------------------------------


def test_prober_reports_lost_and_conclusive_when_exec_fails() -> None:
    """exec code != 0 (endpoint inalcançável) ⇒ egress_lost=True, conclusive=True."""
    fake = _FakeComposeExecutor(code=1)
    prober = DockerEgressProber(fake)

    verdict = prober.probe("nginx-target", on_log=lambda _l: None)

    assert verdict.container == "nginx-target"
    assert verdict.egress_lost is True
    assert verdict.conclusive is True
    # Read-only probe actually ran: exactly one exec_in call was recorded.
    assert len(fake.calls) == 1
    assert fake.calls[0].container == "nginx-target"


def test_gate_approves_when_all_verdicts_lost_and_conclusive() -> None:
    """egresso perdido para todos ⇒ egress_lost([...]) aprova (True)."""
    verdicts = [
        EgressVerdict(container="nginx-target", egress_lost=True, conclusive=True),
        EgressVerdict(container="mariadb-target", egress_lost=True, conclusive=True),
    ]

    assert egress_lost(verdicts) is True


def test_prober_verdict_feeds_approving_gate() -> None:
    """O veredito perdido+conclusivo da sonda alimenta um portão que aprova."""
    fake = _FakeComposeExecutor(code=7)
    prober = DockerEgressProber(fake)

    verdict = prober.probe("kali-attacker", on_log=lambda _l: None)

    assert egress_lost([verdict]) is True


# ---------------------------------------------------------------------------
# Egress present — probe exec code == 0 ⇒ present+conclusive ⇒ gate reproves
# ---------------------------------------------------------------------------


def test_prober_reports_present_when_exec_succeeds() -> None:
    """exec code == 0 (endpoint alcançado) ⇒ egress_lost=False, conclusive=True."""
    fake = _FakeComposeExecutor(code=0)
    prober = DockerEgressProber(fake)

    verdict = prober.probe("nginx-target", on_log=lambda _l: None)

    assert verdict.egress_lost is False
    assert verdict.conclusive is True


def test_gate_reproves_when_any_verdict_still_has_egress() -> None:
    """egresso presente em um host ⇒ o portão reprova (False)."""
    verdicts = [
        EgressVerdict(container="nginx-target", egress_lost=True, conclusive=True),
        EgressVerdict(container="mariadb-target", egress_lost=False, conclusive=True),
    ]

    assert egress_lost(verdicts) is False


def test_prober_present_verdict_feeds_reproving_gate() -> None:
    """O veredito presente da sonda alimenta um portão que reprova."""
    fake = _FakeComposeExecutor(code=0)
    prober = DockerEgressProber(fake)

    verdict = prober.probe("nginx-target", on_log=lambda _l: None)

    assert egress_lost([verdict]) is False


# ---------------------------------------------------------------------------
# Inconclusive — conclusive=False (even with egress_lost=True) ⇒ gate reproves
# ---------------------------------------------------------------------------


def test_gate_reproves_inconclusive_even_when_lost_flag_is_true() -> None:
    """conclusive=False ⇒ reprova (fail-closed), mesmo com egress_lost=True."""
    verdicts = [
        EgressVerdict(container="nginx-target", egress_lost=True, conclusive=False),
    ]

    assert egress_lost(verdicts) is False


def test_gate_reproves_when_one_of_many_is_inconclusive() -> None:
    """Um único veredito inconclusivo reprova todo o lote (fail-closed)."""
    verdicts = [
        EgressVerdict(container="nginx-target", egress_lost=True, conclusive=True),
        EgressVerdict(container="mariadb-target", egress_lost=True, conclusive=False),
    ]

    assert egress_lost(verdicts) is False


def test_gate_reproves_empty_batch() -> None:
    """Lista vazia reprova (fail-closed): "nenhum host verificado" não aprova."""
    assert egress_lost([]) is False
