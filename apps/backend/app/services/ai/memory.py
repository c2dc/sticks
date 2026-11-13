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

The two-memory rule (design §F) — both directions
--------------------------------------------------
STICKS has two distinct memories and a strict rule about what goes where:

* **(a) Vector memory — this module.** The pgvector ``ExperienceMemory`` holds
  *associative / similarity-retrieved* knowledge: Atomic Red Team command ↔ tool
  affinities, what the curated campaigns teach about targets/images/vulnerability
  patterns that worked, and past-run experiences. Retrieval is **top-k cosine,
  excluding the running campaign** (see :meth:`ExperienceMemory.retrieve`). The
  curated campaigns feed this knowledge; they never process a Run.
* **(b) Structured memory — NOT this module.** Exact/factual data lives in normal
  tables / JSON / config, never in the vector store: the **fixed infrastructure**
  (the Caldera engine, the static lab / isolable network structure), the
  **resource limits** (:class:`app.services.environment.resource_gate.ResourceLimits`),
  and the validated inventory of what works.

So: do **not** put factual/exact facts (Caldera, resource limits, validated
inventory) in this vector store, and do **not** put associative knowledge
(command↔tool affinities, "what worked" experiences) into rigid tables — they
belong here.

Embedder transport
------------------
The injected :class:`Embedder` embeds over Ollama (``nomic-embed-text``, 768d)
with ``OLLAMA_BASE_URL`` defaulting to the IPv4 literal
``http://127.0.0.1:11434`` — deliberately **not** ``localhost``, because IPv6
name resolution on Windows made the embedding HTTP client hang. The default is
set in the repository-root ``.env``; keep it as the IPv4 literal.

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

import contextlib
import hashlib
import logging
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

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

# The kinds of associative knowledge stored by :meth:`ExperienceMemory.store_affinity`
# (carried on ``metadata.kind``): command↔tool affinities (Atomic Red Team /
# threat intel), environment affinities ("what worked" for a target), past run
# outcomes, and per-campaign STIX version context (identity / description /
# techniques of a persisted ``Stix_Package_Version``, indexed so an agent can
# retrieve STIX context by similarity). Bounded here so the Memory_Rule routing
# stays explicit (Req. 6.4, 7.3, 8.3, 10.3, 13.4).
AFFINITY_KINDS = (
    "command_affinity",
    "environment_affinity",
    "run_outcome",
    "stix_context",
    "attack_flow",
)

# Sentinel campaign id anchoring the campaign-agnostic affinities stored by
# :meth:`ExperienceMemory.store_affinity`. ``retrieve`` excludes the *running*
# campaign from its results; a running campaign is never this sentinel, so every
# stored affinity stays eligible for the similarity fallback. It mirrors the
# ``_AFFINITY_CAMPAIGN_ID`` used by
# :mod:`app.services.atomic_red_team_ingest_service` and the ``_NO_CAMPAIGN``
# sentinel in :mod:`app.services.command_base`.
_AFFINITY_CAMPAIGN_ID = "__command_base_no_campaign__"


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

    def __init__(self, session: Session, *, embedder: Embedder | None = None):
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
                "ExperienceMemory requires an 'embedder' to embed text; none was injected."
            )
        return self._embedder

    def retrieve(self, *, campaign_id: str, query: str, k: int = 5) -> list[dict[str, Any]]:
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
                .order_by(ExperienceMemoryRow.embedding.cosine_distance(query_embedding))
                .limit(k)
            )
            rows = self._session.execute(stmt).scalars().all()
            return [self._row_to_hit(row) for row in rows]
        except RetrievalParameterError:
            # A caller/embedder-provided out-of-range signal is a real error and
            # must not be swallowed as an availability degradation.
            raise
        except Exception as exc:  # noqa: BLE001 -- degrade on any unavailability
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
        last_exc: Exception | None = None

        for attempt in range(1, _STORE_MAX_ATTEMPTS + 1):
            try:
                self._store_once(campaign_id, report, evaluation)
                return
            except Exception as exc:  # noqa: BLE001 -- retry on any store failure
                last_exc = exc
                logger.warning(
                    "Storing curation outcome for campaign %s failed on attempt %d/%d (%s: %s).",
                    campaign_id,
                    attempt,
                    _STORE_MAX_ATTEMPTS,
                    type(exc).__name__,
                    exc,
                )
                # Roll back the aborted unit of work so the next attempt starts
                # from a clean session state rather than a poisoned transaction.
                with contextlib.suppress(Exception):  # best-effort cleanup
                    self._session.rollback()

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

    # ----- associative affinities (the (a) direction of the Memory_Rule) -----

    def store_affinity(
        self,
        *,
        kind: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        campaign_id: str | None = None,
        stage: Any = None,
        outcome: str = "success",
        dedup_key: str | None = None,
    ) -> bool:
        """Store one associative-knowledge row in ``experience_memory`` (idempotent).

        This is the canonical storage primitive for the *associative* half of the
        Memory_Rule (design §F, Req. 6.4): command↔tool affinities, environment
        affinities and past-run outcomes. It encapsulates exactly what the Atomic
        Red Team ingest (task 11.1) did inline — ensure the sentinel campaign
        anchor, embed the ``content`` with the injected embedder
        (``nomic-embed-text`` in production), and insert a row carrying
        ``metadata.kind`` and a deterministic ``metadata.dedup_key`` — so both the
        ingest and the :class:`ExperienceMemoryBootstrap` share one path.

        Idempotency (Req. 6.2, 7.6): the ``dedup_key`` defaults to the SHA-256 of
        ``content`` and is checked before insert. If a row with that
        ``metadata.dedup_key`` already exists this is a no-op and returns
        ``False``; a new row returns ``True``. Every stored row has an embedding.

        ``kind`` must be one of :data:`AFFINITY_KINDS`. ``campaign_id`` defaults
        to the campaign-agnostic sentinel anchor; ``stage`` defaults to the
        operationalization stage; ``outcome`` is bounded to ``sucesso | falha`` by
        the table CHECK. Any extra ``metadata`` is merged into the stored
        ``metadata`` alongside ``kind`` and ``dedup_key``.
        """
        if kind not in AFFINITY_KINDS:
            raise ValueError(f"store_affinity kind={kind!r} is not one of {AFFINITY_KINDS}.")

        resolved_key = dedup_key or self.content_dedup_key(content)

        # Check-before-insert on the deterministic dedup key so a re-run never
        # duplicates an equivalent affinity (Req. 6.2).
        if self._affinity_exists(resolved_key):
            return False

        from app.models.domain import ExperienceMemory as ExperienceMemoryRow

        resolved_campaign = campaign_id or _AFFINITY_CAMPAIGN_ID
        self._ensure_affinity_campaign(resolved_campaign)

        resolved_stage = self._resolve_affinity_stage(stage)

        meta: dict[str, Any] = dict(metadata or {})
        meta["kind"] = kind
        meta["dedup_key"] = resolved_key

        embedding = self._resolve_embedder().embed(content)
        self._session.add(
            ExperienceMemoryRow(
                campaign_id=resolved_campaign,
                stage=resolved_stage,
                outcome=outcome,
                content=content,
                embedding=embedding,
                meta=meta,
            )
        )
        self._session.flush()
        return True

    def store_affinities(
        self,
        *,
        kind: str,
        entries: Sequence[tuple[str, dict[str, Any], str]],
        campaign_id: str | None = None,
        stage: Any = None,
        outcome: str = "success",
    ) -> int:
        """Batch form of :meth:`store_affinity`; returns the number of rows inserted.

        ``entries`` are ``(content, metadata, dedup_key)`` tuples. Entries whose
        ``dedup_key`` is already stored (or repeated inside the batch) are
        skipped, the remaining contents are embedded with ONE ``embed_many``
        call when the injected embedder offers it (else one ``embed`` per text)
        and inserted with a single flush. Any embedder error propagates before
        anything is added, so a failed batch leaves the session untouched and the
        caller may retry the entries one by one with :meth:`store_affinity`.
        """
        if kind not in AFFINITY_KINDS:
            raise ValueError(f"store_affinities kind={kind!r} is not one of {AFFINITY_KINDS}.")
        if not entries:
            return 0

        from app.models.domain import ExperienceMemory as ExperienceMemoryRow

        keys = [key for _, _, key in entries]
        stored = {
            key
            for (key,) in self._session.execute(
                select(ExperienceMemoryRow.meta["dedup_key"].as_string()).where(
                    ExperienceMemoryRow.meta["dedup_key"].as_string().in_(keys)
                )
            )
        }
        pending: list[tuple[str, dict[str, Any], str]] = []
        for content, metadata, key in entries:
            if key in stored:
                continue
            stored.add(key)
            pending.append((content, metadata, key))
        if not pending:
            return 0

        embedder = self._resolve_embedder()
        texts = [content for content, _, _ in pending]
        many = getattr(embedder, "embed_many", None)
        vectors = many(texts) if callable(many) else [embedder.embed(t) for t in texts]
        if len(vectors) != len(pending):
            raise ValueError("embedder returned a different number of vectors.")

        resolved_campaign = campaign_id or _AFFINITY_CAMPAIGN_ID
        self._ensure_affinity_campaign(resolved_campaign)
        resolved_stage = self._resolve_affinity_stage(stage)
        self._session.add_all(
            ExperienceMemoryRow(
                campaign_id=resolved_campaign,
                stage=resolved_stage,
                outcome=outcome,
                content=content,
                embedding=vector,
                meta={**metadata, "kind": kind, "dedup_key": key},
            )
            for (content, metadata, key), vector in zip(pending, vectors, strict=False)
        )
        self._session.flush()
        return len(pending)

    @staticmethod
    def content_dedup_key(content: str) -> str:
        """Deterministic SHA-256 of ``content`` — the default affinity dedup key.

        A stable hash of the stored snippet so two equivalent affinities collapse
        to one row regardless of insertion order (Req. 6.2).
        """
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _affinity_exists(self, dedup_key: str) -> bool:
        """True if an affinity row with ``metadata.dedup_key`` is already stored.

        Uses SQLAlchemy's portable JSON element access
        (``meta["dedup_key"].as_string()``), which renders to the right operator
        on both PostgreSQL (the product database) and the SQLite used by the unit
        tests, so the dedup check is dialect-agnostic — matching
        :meth:`AtomicRedTeamIngestService._affinity_exists`.
        """
        from app.models.domain import ExperienceMemory as ExperienceMemoryRow

        row = self._session.execute(
            select(ExperienceMemoryRow.id).where(
                ExperienceMemoryRow.meta["dedup_key"].as_string() == dedup_key
            )
        ).first()
        return row is not None

    def _ensure_affinity_campaign(self, campaign_id: str) -> None:
        """Ensure the anchor campaign row exists to satisfy the non-null FK.

        Affinities are campaign-agnostic, but ``experience_memory`` carries a
        non-null FK to ``campaigns.id``. A single sentinel campaign row anchors
        every campaign-agnostic affinity; retrieval excludes the *running*
        campaign, which is never this sentinel, so the affinities stay eligible
        for the similarity fallback. Mirrors
        :meth:`AtomicRedTeamIngestService._ensure_affinity_campaign`. When a real
        ``campaign_id`` is passed it is assumed to already exist (its row is not
        created here).
        """
        from app.models.domain import Case as CampaignRow

        if campaign_id != _AFFINITY_CAMPAIGN_ID:
            return
        existing = self._session.get(CampaignRow, campaign_id)
        if existing is not None:
            return
        self._session.add(
            CampaignRow(
                id=campaign_id,
                name="Command_Base affinities (synthetic anchor)",
            )
        )
        self._session.flush()

    @staticmethod
    def _resolve_affinity_stage(stage: Any) -> Any:
        """Coerce an affinity ``stage`` to an :class:`AgentStage`, defaulting it.

        Accepts an :class:`AgentStage`, its string value, or ``None`` (defaulting
        to the operationalization stage), mirroring :meth:`_resolve_stage`.
        """
        from app.models.enums import AgentStage

        if isinstance(stage, AgentStage):
            return stage
        if isinstance(stage, str):
            try:
                return AgentStage(stage)
            except ValueError:
                pass
        return AgentStage.OPERATIONALIZATION

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

        The ``experience_memory`` table constrains ``outcome`` to exactly the
        two terminal ``AbilityResultStatus`` values (``success | failure``),
        enforced by ``ck_experience_memory_outcome`` (English, matching
        migration ``b2c3d4e5f6a7``). A passing verdict stores ``success``,
        anything else ``failure`` — the finalized curation outcome (success OR
        failure) is always persisted so future runs learn from it (Req. 5.1).
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
    def _build_metadata(report: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any]:
        """Assemble the retrieval-filtering metadata (technique, verdict, metrics)."""
        return {
            "verdict_success": report.get("verdict_success", report.get("success_verdict")),
            "metrics": report.get("metrics"),
            "decision": evaluation.get("decision"),
        }
