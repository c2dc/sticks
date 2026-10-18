"""Property-based test for the preservation of the execution order (task 9.3).

# Feature: stage6-execution-redesign, Property 10: Preservação da ordem de execução (atomic ordering)

**Validates: Requirements 2.6**

Property (design.md — Property 10)
----------------------------------
*Para qualquer* ``atomic_ordering`` da campanha e qualquer conjunto de resultados
por Ability, a lista canônica apresentada preserva **exatamente** a ordem de
``atomic_ordering``, sem reordenar.

Where the canonical ordering lives
----------------------------------
The per-Ability results presented to the UI (the WS progress frame and the
``GET /stage/6`` snapshot) are both assembled from the single canonical
projection :meth:`SessionStateService.get_operation_results`. That projection
reads the persisted ``AbilityResult`` rows of an Operation ordered by
``AbilityResult.id`` ascending — i.e. **insertion order**.

Insertion order mirrors ``atomic_ordering`` because the collection loop
reconciles the Caldera links **in chain order** (which Caldera executes in the
Adversary's ``atomic_ordering``): ``OperationRunner._reconcile`` iterates the
observed ``links`` in order and ``session.add`` + ``commit`` each *new*
``AbilityResult`` one at a time, so the autoincrement ids come out ascending in
exactly the order the Abilities were executed. The canonical projection then
re-reads them by ``id`` ascending, preserving that order.

How this property is driven
---------------------------
Hypothesis generates an arbitrary ``atomic_ordering`` (a list of **distinct**
ability ids — a campaign never schedules the same Ability twice) together with,
for each ability, an arbitrary per-Ability result (status + command output).
The scenario is constructed so that **insertion order == atomic_ordering**
(exactly what the reconcile loop produces): the ``AbilityResult`` rows are
seeded in ``atomic_ordering`` order through the same single-row ``add`` + flush
sequence the runner uses, giving ascending ids in that order.

The test then calls the real canonical ordering function
(``SessionStateService.get_operation_results``) and asserts the recovered
sequence of ``ability_id`` equals ``atomic_ordering`` **exactly** — same length,
same elements, same positions, no reordering. An independent shuffle of the
*generated* status payloads (not of the ordering) is used to prove the result
order is governed by ``atomic_ordering`` alone and never by the order the
statuses happened to be generated in.

At least 100 iterations run (``max_examples=150``). Each example builds and tears
down a fresh temp-file SQLite database (mirroring
``test_session_state_roundtrip_property.py``), so there is no state leak between
examples. NO real Docker daemon and NO network access are involved — everything
is pure in-memory DB logic, safe on the Windows dev environment.

_Requirements: 2.6_
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AbilityResult, Operation
from app.models.enums import AbilityResultStatus, OperationState
from app.services.session.session_state_service import SessionStateService

# ---------------------------------------------------------------------------
# Universe the arbitrary campaign ordering is generated over.
# ---------------------------------------------------------------------------

_CAMPAIGN_ID = "campaign-prop10"

# The four per-Ability result statuses (Req. 2.1).
_STATUS_CHOICES: tuple[AbilityResultStatus, ...] = tuple(AbilityResultStatus)


# ---------------------------------------------------------------------------
# Generators.
# ---------------------------------------------------------------------------

# A campaign's ``atomic_ordering`` is an ordered list of DISTINCT ability ids
# (an Adversary never schedules the same Ability twice). ``unique=True`` keeps
# the ids distinct; ``min_size=1`` ensures a non-trivial order (the empty case
# is covered by an explicit example test below).
_atomic_ordering_strategy = st.lists(
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=16
    ),
    min_size=1,
    max_size=12,
    unique=True,
)


def _ability_payloads_strategy(
    ordering: list[str],
) -> st.SearchStrategy[dict[str, dict]]:
    """A per-Ability result payload (status + command output) for each id.

    One entry per ability id in ``ordering``; the *values* (status/output) are
    arbitrary and independent of the id's position, so the only thing that can
    govern the presented order is ``atomic_ordering`` itself.
    """
    return st.fixed_dictionaries(
        {
            ability_id: st.fixed_dictionaries(
                {
                    "status": st.sampled_from(_STATUS_CHOICES),
                    "command_output": st.one_of(st.none(), st.text(max_size=40)),
                }
            )
            for ability_id in ordering
        }
    )


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + schema per example).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session.

    Mirrors the temp-file SQLite + ``Base.create_all`` pattern from
    ``test_session_state_roundtrip_property.py``. Returns
    ``(session, db_path, engine)`` so the caller can dispose the engine and
    remove the file afterward.
    """
    tmp_dir = tempfile.mkdtemp(prefix="stage6_atomic_ordering_")
    db_path = Path(tmp_dir) / "atomic_ordering.db"
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


def _seed_in_atomic_order(
    session: Session,
    campaign_id: str,
    ordering: list[str],
    payloads: dict[str, dict],
) -> int:
    """Seed one Operation whose ``AbilityResult`` rows are inserted in order.

    Reproduces exactly what ``OperationRunner._reconcile`` does on a run that
    reaches a terminal state: it walks the Caldera links **in chain order**
    (which Caldera executes in ``atomic_ordering``) and ``add`` + ``flush`` each
    new row one at a time, so the autoincrement ids come out ascending in that
    same order. We seed here in ``ordering`` order to make
    *insertion order == atomic_ordering*.

    Returns the created ``operation_id``.
    """
    op = Operation(
        campaign_id=campaign_id,
        state=OperationState.FINISHED,
        total_success=0,
        total_failure=0,
    )
    session.add(op)
    session.flush()  # assign the Operation id before attaching results

    for ability_id in ordering:
        spec = payloads[ability_id]
        row = AbilityResult(
            operation_id=op.id,
            ability_id=ability_id,
            status=spec["status"],
            command_output=spec["command_output"],
        )
        session.add(row)
        # Flush per row so the autoincrement id is assigned in insertion order,
        # exactly as the reconcile loop's per-link ``add`` + ``commit`` does.
        session.flush()

    session.commit()
    return op.id


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(data=st.data())
def test_property10_atomic_ordering_is_preserved(data: st.DataObject) -> None:
    """Property 10: the canonical presented list preserves ``atomic_ordering``.

    # Feature: stage6-execution-redesign, Property 10: Preservação da ordem de execução (atomic ordering)
    **Validates: Requirements 2.6**

    For any campaign ``atomic_ordering`` and any set of per-Ability results, the
    canonical projection (:meth:`SessionStateService.get_operation_results`,
    which both the WS frame and the ``GET /stage/6`` snapshot read from) presents
    the Abilities in exactly ``atomic_ordering`` — same elements, same positions,
    no reordering.
    """
    ordering: list[str] = data.draw(_atomic_ordering_strategy)
    payloads: dict[str, dict] = data.draw(_ability_payloads_strategy(ordering))

    session, db_path, engine = _build_ephemeral_session()
    try:
        operation_id = _seed_in_atomic_order(
            session, _CAMPAIGN_ID, ordering, payloads
        )

        service = SessionStateService(session)
        views = service.get_operation_results(_CAMPAIGN_ID)

        # Exactly one Operation for the campaign.
        assert len(views) == 1
        view = views[0]
        assert view.operation_id == operation_id

        presented_ids = [r.ability_id for r in view.results]

        # (1) The canonical list preserves atomic_ordering EXACTLY — same length,
        # same elements, same positions, no reordering (Req. 2.6).
        assert presented_ids == ordering

        # (2) Each ability's own payload travelled with its position: the result
        # at index i is the one for ``ordering[i]`` (ordering is not just a
        # coincidental permutation that happens to carry the wrong statuses).
        for presented, expected_id in zip(view.results, ordering):
            assert presented.ability_id == expected_id
            assert presented.status == payloads[expected_id]["status"]
            assert presented.command_output == payloads[expected_id][
                "command_output"
            ]
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


def test_property10_empty_atomic_ordering_presents_empty_list() -> None:
    """Edge case: an Operation with no Abilities presents an empty ordered list.

    # Feature: stage6-execution-redesign, Property 10: Preservação da ordem de execução (atomic ordering)
    **Validates: Requirements 2.6**

    An empty ``atomic_ordering`` (an Operation with no collected links yet) must
    present an empty — not a ``None`` / error — canonical list, preserving the
    (vacuous) order.
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        op = Operation(
            campaign_id=_CAMPAIGN_ID,
            state=OperationState.RUNNING,
            total_success=0,
            total_failure=0,
        )
        session.add(op)
        session.commit()

        service = SessionStateService(session)
        views = service.get_operation_results(_CAMPAIGN_ID)

        assert len(views) == 1
        assert views[0].results == []
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
