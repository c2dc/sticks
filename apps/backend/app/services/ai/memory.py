"""pgvector experience memory (long-term RAG).

Hosts :class:`ExperienceMemory`, a Session-injected store over the existing
PostgreSQL ``experience_memory`` pgvector table, plus the lazy,
``@runtime_checkable`` :class:`Embedder` Protocol that turns a snippet of text
into an embedding vector.

The store implements the across-campaign learning described in the design: each
finalized curation outcome (success or failure) is embedded and persisted, and
new proposals are primed by retrieving the most cosine-similar past curations of
*other* campaigns. Learning happens only through the loop + RAG; model weights
are never modified (Requirement 5.6).

Importing this module performs no I/O: the ORM models, the settings and the
embedder are all resolved lazily inside the methods, so there is no network or
database access at import and no circular import with :mod:`app.models`
(Requirement 1.7).

The store is defensive about its two robustness contracts: retrieval rejects an
out-of-range ``k`` (:class:`RetrievalParameterError`, Requirement 5.4) and
degrades to an empty experience set when the memory is unreachable (Requirement
5.7); storage retries up to 3 times and, on exhaustion, raises
:class:`ExperienceStorageError` carrying the original outcome record
(Requirement 5.2).
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Bounds for the retrieval top-k parameter (Requirement 5.4): callers may ask
# for between 1 and 100 snippets inclusive; anything outside is rejected.
_MIN_K = 1
_MAX_K = 100

# How many times storing an outcome is attempted before the operation is
# reported as failed (Requirement 5.2): one initial try plus retries, 3 total.
_STORE_MAX_ATTEMPTS = 3


class RetrievalParameterError(ValueError):
    """Raised when the retrieval ``k`` is outside the supported 1..100 range.

    Signals Requirement 5.4: the request is rejected and no snippets are
    returned when ``k < 1`` or ``k > 100``.
    """


class ExperienceStorageError(RuntimeError):
    """Raised when storing an outcome fails after exhausting all retries.

    Signals Requirement 5.2: after 3 failed attempts the storage operation is
    reported as failed while the original outcome record is preserved on the
    exception so the caller can retry or persist it elsewhere.
    """

    def __init__(
        self,
        message: str,
        *,
        campaign_id: str,
        report: dict[str, Any],
        evaluation: dict[str, Any],
        attempts: int,
    ) -> None:
        super().__init__(message)
        # Preserve the original outcome record so it is not lost when storage
        # fails (Requirement 5.2).
        self.campaign_id = campaign_id
        self.report = report
        self.evaluation = evaluation
        self.attempts = attempts


@runtime_checkable
class Embedder(Protocol):
    """Lazy embedding provider (SEPINF or a sentence-transformer). Injected.

    Any object exposing an ``embed(text) -> list[float]`` method structurally
    satisfies this Protocol; being ``@runtime_checkable`` lets callers assert
    ``isinstance(obj, Embedder)`` without importing a concrete implementation.
    """

    def embed(self, text: str) -> list[float]:
        """Return the embedding vector for ``text``."""
        ...


class ExperienceMemory:
    """RAG store over the ``experience_memory`` pgvector table.

    Session-injected, embedder lazy. Stores each curation outcome
    (success/failure) as an embedded snippet and retrieves the most similar past
    curations to prime new proposals. This is the ACROSS-campaign learning; no
    fine-tuning, no model-weight modification (Requirement 5.6).
    """

    def __init__(self, session: Session, *, embedder: Optional[Embedder] = None):
        self._session = session
        self._embedder = embedder

    def _resolve_embedder(self) -> Embedder:
        """Return the injected embedder, failing fast (name-only) if absent.

        The embedder is resolved lazily so that constructing an
        :class:`ExperienceMemory` (and importing this module) never needs an
        embedding backend; it is only required when text must actually be
        embedded for retrieval or storage.
        """
        if self._embedder is None:
            raise ValueError(
                "ExperienceMemory requires an 'embedder' to embed text; none "
                "was injected."
            )
        return self._embedder

    def retrieve(
        self, *, campaign_id: str, query: str, k: int = 5
    ) -> list[dict[str, Any]]:
        """Cosine-similarity top-k over pgvector, excluding the current campaign.

        Embeds ``query`` and returns at most ``k`` past curation snippets ordered
        by ascending cosine distance (i.e. descending cosine similarity),
        omitting any snippet whose ``campaign_id`` equals the running campaign so
        the agent learns from OTHER campaigns' experience (Requirements 5.3, 5.5).

        The ordering is a parameterized cosine query — the ORM renders
        ``embedding <=> :query_embedding`` via pgvector's ``cosine_distance`` and
        binds both the embedding and ``campaign_id`` as parameters.

        ``k`` must be between 1 and 100 inclusive; out-of-range values raise
        :class:`RetrievalParameterError` and return no snippets (Requirement
        5.4). If the experience memory is unreachable, retrieval degrades
        gracefully: the method records the skip and returns an empty experience
        set so the agent can still propose without RAG priming (Requirement
        5.7).
        """
        # Validate bounds BEFORE any embedding or I/O so an out-of-range request
        # is rejected without side effects and returns no snippets (Req 5.4).
        if k < _MIN_K or k > _MAX_K:
            raise RetrievalParameterError(
                f"retrieval parameter k={k} is out of range; it must be an "
                f"integer between {_MIN_K} and {_MAX_K} (inclusive)."
            )

        # Imported lazily to avoid import-time I/O and any circular import with
        # app.models (Requirement 1.7).
        from app.models.domain import ExperienceMemory as ExperienceMemoryRow

        try:
            query_embedding = self._resolve_embedder().embed(query)

            stmt = (
                select(ExperienceMemoryRow)
                .where(ExperienceMemoryRow.campaign_id != campaign_id)
                .order_by(
                    ExperienceMemoryRow.embedding.cosine_distance(query_embedding)
                )
                .limit(k)
            )
            rows = self._session.execute(stmt).scalars().all()
            return [self._row_to_hit(row) for row in rows]
        except RetrievalParameterError:
            # A caller/embedder-provided out-of-range signal is a real error and
            # must not be swallowed as an availability degradation.
            raise
        except Exception as exc:  # noqa: BLE001 - degrade on any unavailability
            # The experience memory (embedder/pgvector/session) is unreachable:
            # continue with an empty experience set and record the skip so the
            # run proceeds without RAG priming (Requirement 5.7).
            logger.warning(
                "Experience retrieval skipped for campaign %s due to "
                "unavailability (%s: %s); continuing with an empty experience "
                "set.",
                campaign_id,
                type(exc).__name__,
                exc,
            )
            return []

    def store_outcome(
        self, campaign_id: str, report: dict[str, Any], evaluation: dict[str, Any]
    ) -> None:
        """Embed and upsert the curation outcome (success or failure) for RAG.

        Builds a human-readable snippet from the Stage 6 ``report`` and the
        evaluator ``evaluation``, embeds it, and persists one
        ``experience_memory`` row carrying the ``campaign_id``, the ``stage``, the
        ``outcome`` (``sucesso``/``falha``), the embedding and the filtering
        metadata (Requirement 5.1).

        Storage is retried up to 3 attempts total on failure; if every attempt
        fails the method raises :class:`ExperienceStorageError` indicating the
        storage operation failed, carrying the original ``campaign_id``,
        ``report`` and ``evaluation`` so the outcome record is preserved
        (Requirement 5.2).
        """
        last_exc: Optional[Exception] = None

        for attempt in range(1, _STORE_MAX_ATTEMPTS + 1):
            try:
                self._store_once(campaign_id, report, evaluation)
                return
            except Exception as exc:  # noqa: BLE001 - retry on any store failure
                last_exc = exc
                logger.warning(
                    "Storing curation outcome for campaign %s failed on attempt "
                    "%d/%d (%s: %s).",
                    campaign_id,
                    attempt,
                    _STORE_MAX_ATTEMPTS,
                    type(exc).__name__,
                    exc,
                )
                # Roll back the aborted unit of work so the next attempt starts
                # from a clean session state rather than a poisoned transaction.
                try:
                    self._session.rollback()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass

        # All retries exhausted: report the failure while preserving the
        # original outcome record on the exception (Requirement 5.2).
        raise ExperienceStorageError(
            f"failed to store curation outcome for campaign {campaign_id} after "
            f"{_STORE_MAX_ATTEMPTS} attempts.",
            campaign_id=campaign_id,
            report=report,
            evaluation=evaluation,
            attempts=_STORE_MAX_ATTEMPTS,
        ) from last_exc

    def _store_once(
        self, campaign_id: str, report: dict[str, Any], evaluation: dict[str, Any]
    ) -> None:
        """Embed and persist a single ``experience_memory`` row (one attempt).

        Factored out of :meth:`store_outcome` so the retry loop can re-run the
        full embed-and-flush unit of work on each attempt (Requirement 5.2).
        """
        from app.models.domain import ExperienceMemory as ExperienceMemoryRow

        content = self._build_content(report, evaluation)
        embedding = self._resolve_embedder().embed(content)

        row = ExperienceMemoryRow(
            campaign_id=campaign_id,
            stage=self._resolve_stage(evaluation),
            outcome=self._resolve_outcome(report),
            content=content,
            embedding=embedding,
            meta=self._build_metadata(report, evaluation),
        )
        self._session.add(row)
        self._session.flush()

    # ----- helpers -----------------------------------------------------------

    @staticmethod
    def _row_to_hit(row: Any) -> dict[str, Any]:
        """Project a persisted row into the retrieval hit shape used by callers."""
        return {
            "content": row.content,
            "metadata": row.meta,
            "campaign_id": row.campaign_id,
            "stage": row.stage,
            "outcome": row.outcome,
        }

    @staticmethod
    def _build_content(report: dict[str, Any], evaluation: dict[str, Any]) -> str:
        """Render the human-readable snippet embedded and stored for RAG.

        Prefers an explicit rationale/summary from the evaluation, falling back
        to a compact serialization of the report so the snippet is never empty.
        """
        rationale = evaluation.get("rationale")
        if isinstance(rationale, str) and rationale.strip():
            return rationale
        return str(report)

    @staticmethod
    def _resolve_outcome(report: dict[str, Any]) -> str:
        """Map the report's success verdict to the stored outcome value.

        The ``experience_memory`` table constrains ``outcome`` to exactly
        ``sucesso | falha`` (matching ``AbilityResultStatus`` values).
        """
        from app.models.enums import AbilityResultStatus

        verdict = report.get("verdict_success", report.get("success_verdict"))
        if verdict:
            return AbilityResultStatus.SUCCESS.value
        return AbilityResultStatus.FAILURE.value

    @staticmethod
    def _resolve_stage(evaluation: dict[str, Any]) -> Any:
        """Resolve the agent stage the outcome belongs to.

        Uses the evaluator's refine target when present, otherwise the stored
        ``stage`` value, defaulting to the operationalization stage.
        """
        from app.models.enums import AgentStage

        stage = evaluation.get("refine_stage") or evaluation.get("stage")
        if isinstance(stage, AgentStage):
            return stage
        if isinstance(stage, str):
            try:
                return AgentStage(stage)
            except ValueError:
                pass
        return AgentStage.OPERATIONALIZATION

    @staticmethod
    def _build_metadata(
        report: dict[str, Any], evaluation: dict[str, Any]
    ) -> dict[str, Any]:
        """Assemble the retrieval-filtering metadata (technique, verdict, metrics)."""
        return {
            "verdict_success": report.get(
                "verdict_success", report.get("success_verdict")
            ),
            "metrics": report.get("metrics"),
            "decision": evaluation.get("decision"),
        }
