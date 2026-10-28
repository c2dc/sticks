"""Unit tests for ExperienceMemory retrieve/store robustness (task 5.4).

Cover :mod:`app.services.ai.memory` (:class:`ExperienceMemory`,
:class:`RetrievalParameterError`, :class:`ExperienceStorageError`) against a
*fake embedder* and a *stubbed Session / vector store*, asserting the three
robustness guarantees added in task 5.2:

* **Req 5.4 — out-of-range ``k`` rejected, no snippets.** ``retrieve`` raises
  :class:`RetrievalParameterError` when ``k < 1`` or ``k > 100`` (covering
  ``k=0``, ``k=101`` and negative values), does so *before* touching the
  embedder or the session (no side effects / no snippets), and accepts the
  inclusive bounds ``k=1`` and ``k=100``.
* **Req 5.2 — storage retries 3 times then errors, preserving the record.**
  ``store_outcome`` makes exactly 3 attempts when every attempt fails, then
  raises :class:`ExperienceStorageError` carrying the original
  ``campaign_id`` / ``report`` / ``evaluation`` and ``attempts == 3``; it
  rolls the session back between attempts; and it returns successfully (without
  error) when a later attempt works, making only as many attempts as needed.
* **Req 5.7 — retrieval degrades to an empty set on unavailability.** When the
  embedder or ``session.execute`` raises (the memory is unreachable),
  ``retrieve`` records the skip (a warning naming the campaign) and returns
  ``[]`` so the run proceeds without RAG priming — while a genuine
  :class:`RetrievalParameterError` is still re-raised, never swallowed.

Isolation note: these tests never touch a database or a real embedding
backend. The fake embedder returns a fixed vector; the session stub records the
statements it is asked to execute and yields canned rows (or raises) so the
cosine query is exercised without pgvector. The one real dependency pulled in is
the ORM row class, imported lazily by ``retrieve`` / ``store_outcome`` exactly
as in production.

_Requirements: 5.2, 5.4, 5.7_
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.services.ai.memory import (
    ExperienceMemory,
    ExperienceStorageError,
    RetrievalParameterError,
)

# ---------------------------------------------------------------------------
# Fakes: a fixed-vector embedder and a stubbed Session / vector store.
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """Structural :class:`~app.services.ai.memory.Embedder`: fixed vector.

    Records every text it is asked to embed so tests can assert the embedder
    was (or was not) invoked, and returns a constant small vector so the cosine
    ``order_by`` expression builds without needing a real embedding backend.
    """

    def __init__(self, vector: list[float] | None = None) -> None:
        self._vector = vector if vector is not None else [0.1, 0.2, 0.3]
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return self._vector


class RaisingEmbedder:
    """An embedder whose :meth:`embed` always raises, modelling unavailability."""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or RuntimeError("embedding backend unreachable")
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        raise self._exc


class _FakeRow:
    """Minimal stand-in for an ``ExperienceMemory`` ORM row used by retrieval."""

    def __init__(
        self,
        *,
        content: str,
        meta: Any,
        campaign_id: str,
        stage: str,
        outcome: str,
    ) -> None:
        self.content = content
        self.meta = meta
        self.campaign_id = campaign_id
        self.stage = stage
        self.outcome = outcome


class _FakeScalarResult:
    """What ``session.execute(stmt).scalars()`` returns: an ``.all()`` of rows."""

    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def all(self) -> list[_FakeRow]:
        return list(self._rows)


class _FakeExecuteResult:
    """What ``session.execute(stmt)`` returns: exposes ``.scalars()``."""

    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class FakeSession:
    """Stubbed SQLAlchemy ``Session`` for retrieval and storage paths.

    * ``execute(stmt)`` records the statement and returns the configured rows,
      or raises ``execute_error`` to model the vector store being unreachable.
    * ``add``/``flush`` record writes; ``flush`` can be told to fail a given
      number of leading attempts to exercise the store retry loop.
    * ``rollback`` is counted so tests can assert the retry loop cleans up the
      aborted unit of work between attempts.
    """

    def __init__(
        self,
        *,
        rows: list[_FakeRow] | None = None,
        execute_error: Exception | None = None,
        flush_failures: int = 0,
        flush_error: Exception | None = None,
    ) -> None:
        self._rows = rows or []
        self._execute_error = execute_error
        self._flush_failures_remaining = flush_failures
        self._flush_error = flush_error or RuntimeError("flush failed")

        self.executed_statements: list[Any] = []
        self.added: list[Any] = []
        self.flush_calls = 0
        self.rollback_calls = 0

    # -- retrieval path -----------------------------------------------------
    def execute(self, stmt: Any) -> _FakeExecuteResult:
        self.executed_statements.append(stmt)
        if self._execute_error is not None:
            raise self._execute_error
        return _FakeExecuteResult(self._rows)

    # -- storage path -------------------------------------------------------
    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flush_calls += 1
        if self._flush_failures_remaining > 0:
            self._flush_failures_remaining -= 1
            raise self._flush_error

    def rollback(self) -> None:
        self.rollback_calls += 1


# A representative report / evaluation pair reused across the storage tests.
_REPORT: dict[str, Any] = {
    "verdict_success": True,
    "metrics": {"procedural_sufficiency": 0.83},
}
_EVALUATION: dict[str, Any] = {
    "decision": "finalizar",
    "rationale": "all techniques executed with sufficient coverage",
    "stage": "operacionalizacao",
}


# ===========================================================================
# Requirement 5.4 — out-of-range k rejected, returning no snippets
# ===========================================================================


@pytest.mark.parametrize("k", [0, -1, -50, 101, 1000])
def test_retrieve_out_of_range_k_raises_parameter_error(k: int) -> None:
    """``k`` outside 1..100 raises ``RetrievalParameterError`` (Req 5.4).

    Covers both the low end (``k=0`` and negatives) and the high end
    (``k=101`` and beyond). The message names the offending value so the caller
    can see what was rejected.
    """
    embedder = FakeEmbedder()
    session = FakeSession()
    memory = ExperienceMemory(session, embedder=embedder)

    with pytest.raises(RetrievalParameterError) as excinfo:
        memory.retrieve(campaign_id="camp-1", query="any", k=k)

    assert str(k) in str(excinfo.value)


@pytest.mark.parametrize("k", [0, -1, 101])
def test_retrieve_out_of_range_k_returns_no_snippets_and_no_side_effects(
    k: int,
) -> None:
    """Rejecting ``k`` happens before any embedding or query (Req 5.4).

    No snippet is produced, the embedder is never asked to embed, and the
    session is never asked to execute a statement — the bounds check short
    circuits ahead of all I/O.
    """
    embedder = FakeEmbedder()
    session = FakeSession(rows=[_make_row(campaign_id="other")])
    memory = ExperienceMemory(session, embedder=embedder)

    with pytest.raises(RetrievalParameterError):
        memory.retrieve(campaign_id="camp-1", query="any", k=k)

    assert embedder.calls == [], "embedder must not run for an out-of-range k"
    assert session.executed_statements == [], "no query for an out-of-range k"


def test_retrieve_is_a_value_error_subclass() -> None:
    """``RetrievalParameterError`` is a ``ValueError`` (out-of-range signal)."""
    assert issubclass(RetrievalParameterError, ValueError)


@pytest.mark.parametrize("k", [1, 100])
def test_retrieve_accepts_inclusive_bounds(k: int) -> None:
    """``k=1`` and ``k=100`` pass the bounds check and run the query (Req 5.4).

    The inclusive endpoints are valid, so retrieval proceeds: the embedder is
    invoked and the session executes the cosine statement, returning the stub
    rows projected into the hit shape.
    """
    embedder = FakeEmbedder()
    session = FakeSession(rows=[_make_row(campaign_id="other")])
    memory = ExperienceMemory(session, embedder=embedder)

    hits = memory.retrieve(campaign_id="camp-1", query="technique T1059", k=k)

    assert embedder.calls == ["technique T1059"]
    assert len(session.executed_statements) == 1
    assert len(hits) == 1
    assert hits[0]["campaign_id"] == "other"


def test_retrieve_valid_k_projects_rows_into_hit_shape() -> None:
    """A valid retrieval returns the row-to-hit projection used by callers."""
    embedder = FakeEmbedder()
    row = _make_row(
        content="ran T1059 via cmd",
        meta={"technique": "T1059"},
        campaign_id="other-camp",
        stage="operacionalizacao",
        outcome="sucesso",
    )
    session = FakeSession(rows=[row])
    memory = ExperienceMemory(session, embedder=embedder)

    hits = memory.retrieve(campaign_id="camp-1", query="q", k=5)

    assert hits == [
        {
            "content": "ran T1059 via cmd",
            "metadata": {"technique": "T1059"},
            "campaign_id": "other-camp",
            "stage": "operacionalizacao",
            "outcome": "sucesso",
        }
    ]


# ===========================================================================
# Requirement 5.2 — store retries 3 times then errors, preserving the record
# ===========================================================================


def test_store_outcome_retries_exactly_three_times_then_errors() -> None:
    """Every attempt failing yields exactly 3 flushes then an error (Req 5.2).

    With the session configured so every ``flush`` raises, ``store_outcome``
    must try the full embed-and-flush unit of work three times before giving
    up — no more, no fewer — and then raise ``ExperienceStorageError``.
    """
    embedder = FakeEmbedder()
    # flush_failures large enough that no attempt can ever succeed.
    session = FakeSession(flush_failures=99)
    memory = ExperienceMemory(session, embedder=embedder)

    with pytest.raises(ExperienceStorageError):
        memory.store_outcome("camp-1", _REPORT, _EVALUATION)

    assert session.flush_calls == 3, "storage must attempt exactly 3 times"


def test_store_outcome_error_preserves_original_outcome_record() -> None:
    """Exhausted storage carries the original record on the error (Req 5.2).

    The raised ``ExperienceStorageError`` preserves the exact ``campaign_id``,
    ``report`` and ``evaluation`` passed in, plus ``attempts == 3``, so the
    caller can retry or persist the outcome elsewhere without it being lost.
    """
    embedder = FakeEmbedder()
    session = FakeSession(flush_failures=99)
    memory = ExperienceMemory(session, embedder=embedder)

    with pytest.raises(ExperienceStorageError) as excinfo:
        memory.store_outcome("camp-42", _REPORT, _EVALUATION)

    err = excinfo.value
    assert err.campaign_id == "camp-42"
    assert err.report == _REPORT
    assert err.evaluation == _EVALUATION
    assert err.attempts == 3
    # The error is a RuntimeError subclass and names the campaign.
    assert isinstance(err, RuntimeError)
    assert "camp-42" in str(err)


def test_store_outcome_rolls_back_between_failed_attempts() -> None:
    """A failed attempt rolls the session back before the next try (Req 5.2).

    Three failing flushes produce three rollbacks, so each retry starts from a
    clean session rather than a poisoned transaction.
    """
    embedder = FakeEmbedder()
    session = FakeSession(flush_failures=99)
    memory = ExperienceMemory(session, embedder=embedder)

    with pytest.raises(ExperienceStorageError):
        memory.store_outcome("camp-1", _REPORT, _EVALUATION)

    assert session.rollback_calls == 3


def test_store_outcome_succeeds_when_a_later_attempt_works() -> None:
    """Storage succeeds without error once an attempt flushes cleanly (Req 5.2).

    With the first two flushes failing and the third succeeding,
    ``store_outcome`` returns ``None`` (no error), makes exactly three attempts,
    persists one row, and rolls back only the two failed attempts.
    """
    embedder = FakeEmbedder()
    session = FakeSession(flush_failures=2)
    memory = ExperienceMemory(session, embedder=embedder)

    result = memory.store_outcome("camp-1", _REPORT, _EVALUATION)

    assert result is None
    assert session.flush_calls == 3, "two failures then one success"
    assert session.rollback_calls == 2, "only the failed attempts roll back"
    assert len(session.added) == 3, "each attempt stages its row before flush"


def test_store_outcome_succeeds_first_try_makes_single_attempt() -> None:
    """A clean first attempt persists once with no retries or rollback."""
    embedder = FakeEmbedder()
    session = FakeSession(flush_failures=0)
    memory = ExperienceMemory(session, embedder=embedder)

    result = memory.store_outcome("camp-1", _REPORT, _EVALUATION)

    assert result is None
    assert session.flush_calls == 1
    assert session.rollback_calls == 0


# ===========================================================================
# Requirement 5.7 — retrieval degrades to an empty set on unavailability
# ===========================================================================


def test_retrieve_degrades_to_empty_when_embedder_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An embedder failure degrades retrieval to ``[]`` and records the skip.

    When embedding the query raises (the embedding backend is unreachable),
    ``retrieve`` returns an empty experience set and logs a warning naming the
    campaign so the run proceeds without RAG priming (Req 5.7). The session is
    never queried because embedding failed first.
    """
    embedder = RaisingEmbedder()
    session = FakeSession(rows=[_make_row(campaign_id="other")])
    memory = ExperienceMemory(session, embedder=embedder)

    with caplog.at_level(logging.WARNING):
        hits = memory.retrieve(campaign_id="camp-unavail", query="q", k=5)

    assert hits == []
    assert session.executed_statements == [], "query skipped when embed fails"
    assert any(
        "camp-unavail" in record.getMessage() for record in caplog.records
    ), "the skip must be recorded, naming the campaign"


def test_retrieve_degrades_to_empty_when_session_execute_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A vector-store (``session.execute``) failure degrades to ``[]`` (Req 5.7).

    The embedder succeeds but the vector store is unreachable: ``retrieve``
    swallows the unavailability, returns an empty set, and records the skip. The
    embedder having run confirms the failure is specifically the query, not the
    embedding.
    """
    embedder = FakeEmbedder()
    session = FakeSession(execute_error=RuntimeError("pgvector unreachable"))
    memory = ExperienceMemory(session, embedder=embedder)

    with caplog.at_level(logging.WARNING):
        hits = memory.retrieve(campaign_id="camp-db-down", query="q", k=5)

    assert hits == []
    assert embedder.calls == ["q"], "embedding ran before the query failed"
    assert any(
        "camp-db-down" in record.getMessage() for record in caplog.records
    )


def test_retrieve_parameter_error_is_not_swallowed_as_unavailability() -> None:
    """An out-of-range ``k`` is re-raised, never degraded to ``[]`` (Req 5.4/5.7).

    The graceful-degradation ``except`` must not mask a genuine parameter error:
    even though the embedder here would also fail, the bounds check fires first
    and the ``RetrievalParameterError`` propagates rather than being turned into
    an empty result.
    """
    embedder = RaisingEmbedder()
    session = FakeSession(execute_error=RuntimeError("unreachable"))
    memory = ExperienceMemory(session, embedder=embedder)

    with pytest.raises(RetrievalParameterError):
        memory.retrieve(campaign_id="camp-1", query="q", k=0)


def test_retrieve_missing_embedder_degrades_to_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing injected embedder is treated as unavailability (Req 5.7).

    ``_resolve_embedder`` raises when no embedder was injected; for a valid
    ``k`` that failure falls under the unavailability degradation, so retrieval
    returns ``[]`` and records the skip rather than propagating.
    """
    session = FakeSession(rows=[_make_row(campaign_id="other")])
    memory = ExperienceMemory(session, embedder=None)

    with caplog.at_level(logging.WARNING):
        hits = memory.retrieve(campaign_id="camp-no-embed", query="q", k=5)

    assert hits == []
    assert any(
        "camp-no-embed" in record.getMessage() for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(
    *,
    content: str = "snippet",
    meta: Any = None,
    campaign_id: str = "other",
    stage: str = "operacionalizacao",
    outcome: str = "sucesso",
) -> _FakeRow:
    """Build a fake retrieval row with sensible defaults."""
    return _FakeRow(
        content=content,
        meta=meta if meta is not None else {"technique": "T1059"},
        campaign_id=campaign_id,
        stage=stage,
        outcome=outcome,
    )
