"""Property-based test for the incremental collection loop (task 3.7).

# Feature: stage6-execution-redesign, Property 2: Loop de coleta para no primeiro terminal e respeita o clamp de intervalo

*Para qualquer* sequência de snapshots de estado (zero ou mais não-terminais seguidos de
um terminal), o laço de coleta de :meth:`OperationRunner._collect_until_terminal`
reconsulta ``CalderaClient.get_operation`` até observar o **primeiro** estado terminal e
então **para** (sem polls após o terminal); e *para qualquer* intervalo configurado, o
intervalo efetivo dormido entre polls é ``clamp(valor, 1, 10)`` segundos.

**Validates: Requirements 3.1**

Esta é uma propriedade de ``feature`` (a spec stage6-execution-redesign é feature, não
bugfix): espera-se que ela **valha** sobre a implementação atual do
:class:`~app.services.operation.runner.OperationRunner`, cujo loop incremental foi
implementado na task 3.5.

A propriedade é exercida diretamente sobre o método privado
``_collect_until_terminal`` — o coração do loop de coleta — porque é exatamente esse
método que a propriedade descreve: parada no primeiro terminal e respeito ao clamp de
intervalo. Todas as dependências externas são injetadas (sem Caldera/Docker reais):

* **Caldera** é um fake :class:`_ScriptedCaldera` que serve uma sequência pré-definida de
  :class:`OperationStateResult` (zero ou mais não-terminais + um terminal) e **conta** as
  chamadas a ``get_operation`` — é assim que provamos "para no primeiro terminal":
  o número de chamadas é exatamente ``índice do primeiro terminal + 1`` e nenhuma chamada
  acontece depois.
* **clock/sleeper** são injetados: o relógio é constante (o orçamento de timeout nunca
  decorre e a reverificação periódica de isolamento nunca dispara, isolando a variável da
  propriedade), e o ``sleeper`` **captura** cada duração dormida para asserir o clamp.
* **isolamento** é um verificador fake que sempre passa, com intervalo de reverificação
  grande o bastante para nunca disparar sob o relógio constante.
* O banco é um **SQLite efêmero em arquivo temporário** criado por exemplo (o Hypothesis
  reexecuta o corpo por exemplo) e descartado num ``finally``, para que a reconciliação de
  ``AbilityResult`` seja real e nenhum estado ``.db`` vaze entre exemplos.

Um **oráculo independente** recomputa, a partir dos insumos gerados, tanto o número
esperado de polls (``índice do primeiro terminal + 1``) quanto o intervalo clampeado
esperado (``clamp(valor, 1, 10)``), de modo que as asserções nunca confiam na
implementação para decidir o resultado esperado.

_Requirements: 3.1_
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AbilityResult, Operation
from app.models.enums import AbilityResultStatus, OperationState
from app.services.audit.audit_logger import AuditLogger
from app.services.caldera import CalderaClient, OperationLink, OperationStateResult
from app.services.caldera.client import TERMINAL_OPERATION_STATES
from app.services.containment.isolation import ContainerIsolationVerdict, IsolationResult
from app.services.operation.runner import OperationRunner

# The three terminal Caldera states (single source of truth lives in the client).
_TERMINAL_STATES: tuple[str, ...] = tuple(sorted(TERMINAL_OPERATION_STATES))

# A pool of plausible NON-terminal Caldera operation states the chain passes
# through while the links are still advancing. None of these is in
# TERMINAL_OPERATION_STATES, so each must make the loop poll again.
_NON_TERMINAL_STATES: tuple[str, ...] = ("running", "paused", "run_one_link", "")


def _isolated_verifier(containers: list[str], _inspector: object) -> IsolationResult:
    """A fake isolation verifier: every requested container is isolated.

    Mirrors :func:`inspect_isolation`'s signature ``(containers, inspector)`` so
    it drops straight into the runner via ``isolation_verifier=``. Keeping the
    isolation gate open isolates Property 2's variables (terminal detection and
    interval clamp) from the periodic isolation re-check.
    """
    verdicts = tuple(
        ContainerIsolationVerdict(container=name, isolated=True, reasons=())
        for name in containers
    )
    return IsolationResult(passed=True, verdicts=verdicts)


def _link(ability_index: int, status_code: int | None) -> OperationLink:
    """Build one per-Ability link for a snapshot's chain.

    ``status_code`` follows the Caldera sentinels the runner understands
    (``None`` -> pendente, ``-3`` -> em_execucao, ``0`` -> sucesso, outro ->
    falha); the derived :class:`AbilityResultStatus` is computed via
    :meth:`OperationLink.from_payload`-equivalent derivation on construction.
    """
    from app.services.caldera.client import _derive_link_status

    return OperationLink(
        link_id=f"link-{ability_index}",
        ability_id=f"ability-{ability_index}",
        command=f"cmd-{ability_index}",
        paw="kali",
        status_code=status_code,
        status=_derive_link_status(status_code),
        output=None,
    )


def _snapshot(state: str, link_count: int, status_code: int | None) -> OperationStateResult:
    """Build an :class:`OperationStateResult` with ``link_count`` links at ``state``."""
    links = tuple(_link(i, status_code) for i in range(link_count))
    return OperationStateResult(operation_id="op-test", state=state, links=links)


@dataclass
class _ScriptedCaldera:
    """A fake CalderaClient that serves a scripted snapshot sequence.

    ``get_operation`` returns the next :class:`OperationStateResult` from
    ``snapshots`` and records the operation id of every call in ``calls`` — the
    call count is exactly how Property 2 proves the loop stops at the first
    terminal (no poll happens after it). If the loop ever polls past the end of
    the scripted sequence (which would mean it did not stop at the terminal), the
    missing snapshot raises, failing the test loudly.
    """

    snapshots: tuple[OperationStateResult, ...]
    calls: list[str] = field(default_factory=list)

    def get_operation(self, operation_id: str) -> OperationStateResult:
        index = len(self.calls)
        self.calls.append(operation_id)
        if index >= len(self.snapshots):
            raise AssertionError(
                "o loop consultou get_operation além do primeiro terminal "
                f"(poll #{index + 1}, só havia {len(self.snapshots)} snapshots)"
            )
        return self.snapshots[index]


@dataclass
class _CapturingSleeper:
    """A sleeper that captures every duration instead of actually sleeping."""

    durations: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session.

    Mirrors the temp-file SQLite + ``Base.create_all`` pattern from
    ``test_operation_runner_property.py``. Returns ``(session, db_path, engine)``
    so the caller can dispose the engine and remove the file afterward.
    """
    tmp_dir = tempfile.mkdtemp(prefix="stage6_collect_terminal_")
    db_path = Path(tmp_dir) / "collect.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)

    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    return session_factory(), db_path, engine


def _count(session: Session, model: type) -> int:
    """Count all rows of ``model`` in the (per-example) database."""
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def _clamp(value: float, low: float, high: float) -> float:
    """Independent oracle for the interval clamp (never calls the implementation)."""
    return max(low, min(high, float(value)))


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    num_non_terminal=st.integers(min_value=0, max_value=8),
    non_terminal_states=st.lists(
        st.sampled_from(_NON_TERMINAL_STATES), min_size=0, max_size=8
    ),
    terminal_state=st.sampled_from(_TERMINAL_STATES),
    link_count=st.integers(min_value=0, max_value=4),
    configured_interval=st.floats(
        min_value=-5.0, max_value=25.0, allow_nan=False, allow_infinity=False
    ),
)
def test_property2_stops_at_first_terminal_and_clamps_interval(
    num_non_terminal: int,
    non_terminal_states: list[str],
    terminal_state: str,
    link_count: int,
    configured_interval: float,
) -> None:
    """Property 2: para no primeiro terminal e dorme o intervalo clampeado.

    # Feature: stage6-execution-redesign, Property 2: Loop de coleta para no primeiro terminal e respeita o clamp de intervalo
    Validates: Requirements 3.1

    For a scripted sequence of ``m`` non-terminal snapshots followed by exactly
    one terminal snapshot, the loop:

    * (a) polls ``get_operation`` exactly ``m + 1`` times — the index of the
      first terminal plus one — and never polls after the terminal (the scripted
      caldera raises if it would);
    * (b) sleeps exactly ``m`` times (once after each non-terminal poll, none
      after the terminal), and every captured sleep duration equals
      ``clamp(configured_interval, 1, 10)``.
    """
    # Build the scripted sequence: ``m`` non-terminal snapshots (cycling through
    # the generated non-terminal states) + one terminal snapshot. ``m`` is the
    # index of the first terminal, so the oracle expects ``m + 1`` polls.
    m = num_non_terminal
    non_terminal_pool = non_terminal_states or [_NON_TERMINAL_STATES[0]]
    snapshots: list[OperationStateResult] = [
        _snapshot(non_terminal_pool[i % len(non_terminal_pool)], link_count, status_code=-3)
        for i in range(m)
    ]
    # The terminal snapshot carries finished (status 0 -> SUCCESS) links so the
    # final reconcile persists a terminal AbilityResult per link.
    snapshots.append(_snapshot(terminal_state, link_count, status_code=0))

    session, db_path, engine = _build_ephemeral_session()
    caldera = _ScriptedCaldera(snapshots=tuple(snapshots))
    sleeper = _CapturingSleeper()
    # Constant clock: the collection budget never elapses (deadline unreachable)
    # and the periodic isolation re-check never fires, so the loop stops ONLY on
    # the first terminal state — exactly the variable under test.
    frozen_now = 1_000.0

    try:
        # Persist the Operation the loop reconciles into (FK target for the rows).
        operation = Operation(
            campaign_id="shadowray",
            state=OperationState.RUNNING,
            total_success=0,
            total_failure=0,
        )
        session.add(operation)
        session.commit()
        session.refresh(operation)

        runner = OperationRunner(
            session=session,
            caldera=caldera,  # type: ignore[arg-type]
            audit_logger=AuditLogger(session),
            isolation_verifier=_isolated_verifier,
            collection_interval=configured_interval,
            # A generous timeout; with the constant clock it never elapses, so
            # termination is driven purely by the first terminal state.
            collection_timeout=10_000.0,
            # Large re-check interval so the periodic isolation check never fires
            # under the constant clock (last_isolation_check starts at the clock).
            isolation_recheck_interval=30.0,
            clock=lambda: frozen_now,
            sleeper=sleeper,
        )

        collection = runner._collect_until_terminal(
            operation_id=operation.id,
            started_at=frozen_now,
            caldera_operation_id="op-test",
            target_label="nginx (192.168.20.30)",
            containers=["nginx"],
        )

        # --- (a) stops at the FIRST terminal: exactly m + 1 polls, none after. ---
        expected_polls = m + 1
        assert len(caldera.calls) == expected_polls
        # Every poll targeted the Caldera operation id threaded into the loop.
        assert all(op_id == "op-test" for op_id in caldera.calls)
        # The loop reports it reached a terminal state (did not time out).
        assert collection.reached_terminal is True
        assert collection.aborted is False
        assert collection.isolation_broken is False

        # --- (b) sleeps once per non-terminal poll, each the clamped interval. ---
        assert len(sleeper.durations) == m
        expected_interval = _clamp(configured_interval, 1.0, 10.0)
        assert all(d == expected_interval for d in sleeper.durations)
        # The effective interval is always within the required 1..10s band.
        assert all(1.0 <= d <= 10.0 for d in sleeper.durations)

        # --- The terminal reconcile persisted one AbilityResult per link. ---
        assert _count(session, AbilityResult) == link_count
        assert len(collection.ability_results) == link_count
        # Terminal links were status 0 -> SUCCESS, so the tally is all successes.
        assert collection.total_success == link_count
        assert collection.total_failure == 0
        assert all(
            r.status is AbilityResultStatus.SUCCESS
            for r in collection.ability_results
        )
    finally:
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)
