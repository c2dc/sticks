"""Source runners: adapt each ingest service to the orchestrator contract.

Every runner implements ``plan`` (database only: what is missing), ``run``
(executes only that plan) and ``counts`` (cheap totals for state reporting).
The ingest services themselves stay the owners of the persistence rules; the
runners only decide *whether* to download (``needs_fetch``) or just *vectorize*
what is already stored, and surface embedder problems as failures.

Embedder failures: the Docker and Attack Flow services treat vectorization as
best-effort and swallow errors per item. ``GuardedEmbedder`` records those
failures (and stops calling a dead Ollama after a few consecutive errors), so
the runner can raise the embedder error code once the relational work is done.
The relational rows remain stored and the next plan only vectorizes the rest.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import IngestSource
from app.services.ai.embedder import EMBEDDER_UNAVAILABLE, EmbedderError, OllamaEmbedder
from app.services.ai.memory import Embedder, ExperienceMemory
from app.services.ingestion.orchestrator import (
    ProgressCallback,
    ReconcilePlan,
    RunOutcome,
    SourceRunner,
    strip_run_marker,
)

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_EMBED_FAILURES = 3

# Environment variable listing the STIX versions to keep ingested (comma or
# whitespace separated; the token ``latest`` means the newest known version).
STIX_VERSIONS_ENV = "STIX_VERSIONS"
_STIX_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
# How long a ``latest`` resolved by ``run`` is trusted before asking again.
_LATEST_TTL_S = 3600.0


class GuardedEmbedder:
    """Wraps an embedder, recording failures and short-circuiting a dead one."""

    def __init__(
        self,
        inner: Embedder | None = None,
        *,
        max_consecutive_failures: int = _MAX_CONSECUTIVE_EMBED_FAILURES,
    ) -> None:
        self._inner = inner
        self._max = max_consecutive_failures
        self._consecutive = 0
        self.failures = 0
        self.failure_code: str | None = None

    def embed(self, text: str) -> list[float]:
        return self._guarded(lambda inner: inner.embed(text))

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """One vector per text; uses the inner ``embed_many`` (one call) when present."""

        def call(inner: Any) -> list[list[float]]:
            many = getattr(inner, "embed_many", None)
            if callable(many):
                return many(texts)
            return [inner.embed(text) for text in texts]

        return self._guarded(call)

    def _guarded(self, call: Callable[[Any], Any]) -> Any:
        if self._consecutive >= self._max:
            raise EmbedderError(
                self.failure_code or EMBEDDER_UNAVAILABLE,
                "embedding paused after repeated failures.",
            )
        try:
            if self._inner is None:
                self._inner = OllamaEmbedder()
            result = call(self._inner)
        except Exception as exc:
            self._consecutive += 1
            self.failures += 1
            self.failure_code = getattr(exc, "code", None) or EMBEDDER_UNAVAILABLE
            raise
        self._consecutive = 0
        return result

    def raise_if_failed(self, errors: list[str]) -> None:
        """Turn swallowed per-item vector failures into one source failure."""
        if self.failures:
            raise EmbedderError(self.failure_code or EMBEDDER_UNAVAILABLE, "vectorization failed.")
        if errors:
            raise RuntimeError("vectorization failed")


def _meta_keys(session: Session, **filters: str) -> set[str]:
    """Stored ``dedup_key`` values of ``experience_memory`` rows matching ``meta``."""
    from app.models.domain import ExperienceMemory as Row

    statement = select(Row.meta["dedup_key"].as_string())
    for key, value in filters.items():
        statement = statement.where(Row.meta[key].as_string() == value)
    return {key for (key,) in session.execute(statement) if key}


def _count_memory(session: Session, **filters: str) -> int:
    from app.models.domain import ExperienceMemory as Row

    statement = select(func.count()).select_from(Row)
    for key, value in filters.items():
        statement = statement.where(Row.meta[key].as_string() == value)
    return int(session.execute(statement).scalar_one())


def _saved_expected_count(session: Session, source: IngestSource) -> tuple[int | None, int]:
    """``(expected_count, skipped_count)`` saved by the last run of ``source``."""
    from app.models.domain import IngestSourceState

    row = session.get(IngestSourceState, source)
    if row is None:
        return None, 0
    skipped = len(row.skipped) if isinstance(row.skipped, list) else 0
    return row.expected_count, skipped


class AtomicSourceRunner:
    """Atomic Red Team commands plus the curated experience-memory bootstrap."""

    source = IngestSource.ATOMIC

    def __init__(
        self,
        *,
        embedder: Embedder | None = None,
        fetcher: Any = None,
        ref: str = "master",
    ) -> None:
        self._embedder = embedder
        self._fetcher = fetcher
        self._ref = ref

    def plan(self, session: Session) -> ReconcilePlan:
        """Download again when the stored Atomic data cannot be trusted as complete.

        A new download is needed when nothing is stored, when the previous run
        failed for a reason other than the embedder (the relational part is then
        complete and only vectorization resumes), when fewer techniques than
        ``ATOMIC_MIN_TECHNIQUES`` are stored, or when no complete relational
        ingestion was ever recorded (``expected_count`` unknown or above the
        stored rows), or when any stored row still has NULL ``input_arguments``
        (ingested before the column existed; the re-download backfills it
        without duplicating rows or embeddings). Otherwise only the missing vectors are planned; with
        nothing missing the plan is empty and no network is used.
        """
        from app.models.domain import IngestSourceState, TechniqueCommand
        from app.models.enums import IngestStatus
        from app.services.atomic_red_team_ingest_service import (
            _SOURCE,
            atomic_affinity_dedup_key,
            min_techniques,
        )
        from app.services.experience_memory_bootstrap import ExperienceMemoryBootstrap

        rows = session.execute(
            select(
                TechniqueCommand.technique_id,
                TechniqueCommand.executor,
                TechniqueCommand.platform,
                TechniqueCommand.command,
                TechniqueCommand.input_arguments,
            ).where(TechniqueCommand.source == _SOURCE)
        ).all()
        technique_count = len({row[0] for row in rows})
        # NULL (SQL or JSON null) means the row predates ``input_arguments``;
        # ingestion stores ``{}`` for "no arguments", so NULL is a pending
        # backfill that only a new download can complete (Req. 11.7).
        arguments_pending = any(row[4] is None for row in rows)
        expected, _ = _saved_expected_count(session, self.source)
        state = session.get(IngestSourceState, self.source)
        embedder_codes = {EMBEDDER_UNAVAILABLE, "embedding_dim_mismatch"}
        failed_for_other_reason = bool(
            state is not None
            and state.status == IngestStatus.FAILED
            and (strip_run_marker(state.error) or "") not in embedder_codes
        )
        never_complete = expected is None or len(rows) < expected
        needs_fetch = (
            not rows
            or failed_for_other_reason
            or never_complete
            or arguments_pending
            or technique_count < min_techniques()
        )
        existing = _meta_keys(session, source=_SOURCE)
        missing = sum(
            1
            for tid, executor, platform, command, _arguments in rows
            if atomic_affinity_dedup_key(tid, executor or "unknown", platform or "unknown", command)
            not in existing
        )
        bootstrap_missing = ExperienceMemoryBootstrap(
            session, memory=ExperienceMemory(session)
        ).count_missing()
        return ReconcilePlan(
            source=self.source,
            needs_fetch=needs_fetch,
            missing_embed=missing + bootstrap_missing,
            loaded_count=len(rows),
            embedded_count=len(rows) - missing,
            expected_count=expected,
        )

    def run(
        self, session: Session, plan: ReconcilePlan, progress: ProgressCallback
    ) -> RunOutcome | None:
        from app.services.atomic_red_team_ingest_service import (
            AtomicRedTeamIngestService,
        )
        from app.services.experience_memory_bootstrap import ExperienceMemoryBootstrap

        guard = GuardedEmbedder(self._embedder)
        memory = ExperienceMemory(session, embedder=guard)
        service = AtomicRedTeamIngestService(session, memory=memory, fetcher=self._fetcher)
        skipped: list[dict[str, str]] = []
        # No transaction may stay open during the download or the embedding.
        session.commit()
        if plan.needs_fetch:
            summary = service.ingest(ref=self._ref, progress=progress)
            errors = summary.errors
            skipped = summary.skipped
        else:
            _, errors = service.vectorize_missing(progress)
        session.flush()
        if errors:
            # The relational rows are complete and stay stored; the next plan
            # only vectorizes what is missing.
            raise EmbedderError(guard.failure_code or EMBEDDER_UNAVAILABLE, "vectorization failed.")
        ExperienceMemoryBootstrap(session, memory=memory).bootstrap()
        return RunOutcome(skipped=skipped) if skipped else None

    def counts(self, session: Session) -> tuple[int, int]:
        from app.models.domain import TechniqueCommand
        from app.services.atomic_red_team_ingest_service import _SOURCE

        loaded = session.execute(
            select(func.count())
            .select_from(TechniqueCommand)
            .where(TechniqueCommand.source == _SOURCE)
        ).scalar_one()
        return int(loaded), _count_memory(session, source=_SOURCE)


class DockerCatalogSourceRunner:
    """Docker Hub image catalog (text only) and its embeddings."""

    source = IngestSource.DOCKER_CATALOG

    def __init__(self, *, embedder: Embedder | None = None, fetcher: Any = None) -> None:
        self._embedder = embedder
        self._fetcher = fetcher

    def plan(self, session: Session) -> ReconcilePlan:
        from app.models.domain import ImageCatalog
        from app.services.docker_catalog_ingest_service import (
            _SOURCE,
            docker_entry_embedding_content,
        )

        rows = session.execute(
            select(
                ImageCatalog.image,
                ImageCatalog.target_os,
                ImageCatalog.notes,
                ImageCatalog.services,
            )
        ).all()
        existing = _meta_keys(session, source=_SOURCE)
        missing = 0
        for image, target_os, notes, services in rows:
            content = docker_entry_embedding_content(image, target_os, notes or "", services or ())
            if ExperienceMemory.content_dedup_key(content) not in existing:
                missing += 1
        # A catalog download interrupted by a network error leaves a partial
        # table; the failed state tells us the download must be resumed.
        from app.models.domain import IngestSourceState

        state = session.get(IngestSourceState, self.source)
        from app.models.enums import IngestStatus
        from app.services.docker_catalog_ingest_service import (
            DEFAULT_MIN_IMAGES,
            MIN_IMAGES_ENV,
            env_int,
        )

        interrupted = bool(
            state is not None
            and (
                state.status == IngestStatus.FAILED
                or (strip_run_marker(state.error) or "").startswith("network:")
            )
        )
        # A catalog below the configured floor is incomplete (for example the
        # 200 images of the old search) and is fetched again; upserts only add.
        below_floor = len(rows) < env_int(MIN_IMAGES_ENV, DEFAULT_MIN_IMAGES)
        return ReconcilePlan(
            source=self.source,
            needs_fetch=not rows or interrupted or below_floor,
            missing_embed=missing,
            loaded_count=len(rows),
            embedded_count=len(rows) - missing,
        )

    def run(
        self, session: Session, plan: ReconcilePlan, progress: ProgressCallback
    ) -> RunOutcome | None:
        from app.services.docker_catalog_ingest_service import DockerCatalogIngestService

        guard = GuardedEmbedder(self._embedder)
        service = DockerCatalogIngestService(
            session,
            memory=ExperienceMemory(session, embedder=guard),
            fetcher=self._fetcher,
        )
        warnings: list[dict[str, str]] = []
        if plan.needs_fetch:
            summary = service.ingest(progress=progress)
            errors = summary.errors
            warnings = summary.warnings
        else:
            _, errors = service.vectorize_missing(progress)
        session.flush()
        guard.raise_if_failed(errors)
        return RunOutcome(skipped=warnings) if warnings else None

    def counts(self, session: Session) -> tuple[int, int]:
        from app.models.domain import ImageCatalog
        from app.services.docker_catalog_ingest_service import _SOURCE

        loaded = session.execute(select(func.count()).select_from(ImageCatalog)).scalar_one()
        return int(loaded), _count_memory(session, source=_SOURCE)


class AttackFlowSourceRunner:
    """MITRE Attack Flow corpus (``.afb``) and its embeddings."""

    source = IngestSource.ATTACK_FLOW

    def __init__(self, *, embedder: Embedder | None = None, fetcher: Any = None) -> None:
        self._embedder = embedder
        self._fetcher = fetcher

    def plan(self, session: Session) -> ReconcilePlan:
        from app.models.domain import AttackFlowRow
        from app.services.attack_flow.ingest_service import (
            _AFFINITY_KIND,
            attack_flow_dedup_key,
        )

        keys = (
            session.execute(
                select(AttackFlowRow.source_key).where(AttackFlowRow.source_key.is_not(None))
            )
            .scalars()
            .all()
        )
        existing = _meta_keys(session, kind=_AFFINITY_KIND)
        missing = sum(1 for key in keys if attack_flow_dedup_key(key) not in existing)
        expected, skipped_count = _saved_expected_count(session, self.source)
        loaded = len(keys)
        # The listing is only requested when nothing is stored or a previous
        # run learned that more files exist than are stored (files recorded as
        # skipped are accounted for, so they do not trigger endless refetches).
        needs_fetch = loaded == 0 or (expected is not None and loaded + skipped_count < expected)
        return ReconcilePlan(
            source=self.source,
            needs_fetch=needs_fetch,
            missing_embed=missing,
            loaded_count=loaded,
            embedded_count=loaded - missing,
            expected_count=expected,
        )

    def run(
        self, session: Session, plan: ReconcilePlan, progress: ProgressCallback
    ) -> RunOutcome | None:
        from app.services.attack_flow.corpus_fetcher import AttackFlowCorpusFetcher
        from app.services.attack_flow.ingest_service import AttackFlowIngestService

        guard = GuardedEmbedder(self._embedder)
        service = AttackFlowIngestService(
            session,
            memory=ExperienceMemory(session, embedder=guard),
            fetcher=self._fetcher or AttackFlowCorpusFetcher(),
        )
        skipped: list[dict[str, str]] = []
        errors: list[str] = []
        if plan.needs_fetch:
            summary = service.ingest(progress=progress)
            skipped = summary.skipped
            errors.extend(summary.errors)
        # Unchanged files are not revisited by ``ingest``; vectorize any gap.
        _, vector_errors = service.vectorize_missing(progress)
        errors.extend(vector_errors)
        session.flush()
        guard.raise_if_failed(errors)
        return RunOutcome(skipped=skipped) if plan.needs_fetch else None

    def counts(self, session: Session) -> tuple[int, int]:
        from app.models.domain import AttackFlowRow
        from app.services.attack_flow.ingest_service import _AFFINITY_KIND

        loaded = session.execute(
            select(func.count())
            .select_from(AttackFlowRow)
            .where(AttackFlowRow.source_key.is_not(None))
        ).scalar_one()
        return int(loaded), _count_memory(session, kind=_AFFINITY_KIND)


class StixPackagesSourceRunner:
    """MITRE ATT&CK STIX packages, downloaded in memory and persisted in the database.

    Nothing is written to disk: each version is fetched by
    :meth:`StixService.fetch_bundle` and persisted by
    :class:`StixPersistenceService`.

    Versions kept ingested (the *desired set*, a union):

    * (a) ``latest``, the system default: the newest ATT&CK Enterprise version.
      ``run`` resolves it by asking the upstream index (``latest_fetcher``,
      injectable); offline or on any error it falls back to
      ``max(KNOWN_VERSIONS)`` without raising. ``plan`` reads the cached upstream
      index through :class:`StixVersionCatalog` (a stale answer is served and
      refreshed in a daemon thread; on a request thread it never waits for the
      network), else the value resolved earlier or the fallback. ``latest`` counts
      as satisfied only when a persisted version is at least as new, so a release
      newer than everything installed makes the plan non-empty.
      The ``STIX_VERSIONS`` environment variable (comma or whitespace separated,
      ``latest`` allowed) replaces ONLY this item;
    * (b) every version already persisted in ``stix_packages`` (never dropped);
    * (c) the versions the curated seed requires
      (:data:`app.seeds.manifest.REQUIRED_STIX_VERSIONS`, today ``18.0``), which
      ``STIX_VERSIONS`` can never remove;
    * the versions the user asked for through the UI (:meth:`request_version`,
      held in memory only).

    ``plan`` looks at the database only: a version in the desired set without a
    ``stix_packages`` row is missing and makes the plan non-empty. ``run`` is
    idempotent per version, isolates a failing version from the others and
    re-raises the first failure at the end, so the source ends ``failed`` and the
    next run resumes only what is still missing.
    """

    source = IngestSource.STIX_PACKAGES

    def __init__(
        self,
        *,
        embedder: Embedder | None = None,
        stix_service: Any = None,
        versions: Iterable[str] | None = None,
        required_versions: Iterable[str] | None = None,
        latest_fetcher: Callable[[], str] | None = None,
        index_vectors: bool = True,
    ) -> None:
        self._embedder = embedder
        self._stix_service = stix_service
        # ``versions`` overrides item (a) (the ``latest`` default / STIX_VERSIONS).
        self._configured = list(versions) if versions is not None else None
        # ``required_versions`` is item (c); ``None`` means the curated seed's.
        self._required = list(required_versions) if required_versions is not None else None
        # Returns the newest upstream version or raises; ``None`` uses the
        # injected/real StixService (index.json).
        self._latest_fetcher = latest_fetcher
        self._latest_resolved: str | None = None
        self._latest_resolved_at = 0.0
        self._index_vectors = index_vectors
        self._requested: set[str] = set()
        self._lock = threading.Lock()

    # ---- desired set --------------------------------------------------------

    def request_version(self, version: str) -> None:
        """Ask for ``version`` to be ingested on the next run (in memory only)."""
        with self._lock:
            self._requested.add(version)

    def _take_requested(self) -> set[str]:
        with self._lock:
            requested, self._requested = self._requested, set()
        return requested

    def _tokens(self) -> list[str]:
        """Item (a): explicit versions / ``latest`` tokens (``STIX_VERSIONS`` or default)."""
        if self._configured is not None:
            return list(self._configured)
        raw = os.environ.get(STIX_VERSIONS_ENV, "")
        tokens = [token for token in re.split(r"[,\s]+", raw) if token]
        return tokens or ["latest"]

    @staticmethod
    def _fallback_latest() -> str:
        from app.services.stix.stix_service import KNOWN_VERSIONS, _semantic_version_key

        return max(KNOWN_VERSIONS, key=_semantic_version_key)

    def _remembered_latest(self) -> str | None:
        """Latest resolved by ``run`` in the last hour (injected-fetcher path)."""
        if self._latest_resolved is None:
            return None
        if time.monotonic() - self._latest_resolved_at > _LATEST_TTL_S:
            return None
        return self._latest_resolved

    def _plan_latest(self) -> str:
        """``latest`` for ``plan``: cache only, never a network call on the caller.

        Order: a fresh value remembered by ``run`` (injected fetcher), then the
        cached upstream index (stale answers are served and refreshed in the
        background), then ``max(KNOWN_VERSIONS)``. Inside an ingestion thread a
        cold cache is filled inline (short catalog timeout); on any other thread,
        e.g. the HTTP request of ``POST /ingestion/verify``, it never waits.
        """
        remembered = self._remembered_latest()
        if remembered is not None:
            return remembered
        if self._latest_fetcher is None and self._stix_service is None:
            try:
                from app.services.stix.stix_service import get_version_catalog

                in_background = threading.current_thread().name.startswith("ingestion-")
                remote = get_version_catalog().cached_latest(block=in_background)
                if remote is not None and _STIX_VERSION_RE.fullmatch(remote):
                    return remote
            except Exception:  # noqa: BLE001 -- plan never raises because of the upstream
                logger.warning("stix cached latest lookup failed; using the fallback")
        return self._fallback_latest()

    def _resolve_latest(self) -> str:
        """Newest upstream version (network); ``max(KNOWN_VERSIONS)`` on any failure.

        Never raises. A successful answer is remembered for one hour (so a new
        release is noticed without restarting); a failure is not remembered.
        """
        remembered = self._remembered_latest()
        if remembered is not None:
            return remembered
        try:
            fetcher = self._latest_fetcher
            if fetcher is None and self._stix_service is None:
                from app.services.stix.stix_service import get_version_catalog

                catalog = get_version_catalog()
                fetcher = functools.partial(catalog.cached_latest, block=True)
            elif fetcher is None:
                fetcher = self._stix_service.fetch_latest_version
            version = fetcher()
            if not isinstance(version, str) or not _STIX_VERSION_RE.fullmatch(version):
                raise ValueError("invalid latest version")
        except Exception as exc:  # noqa: BLE001 -- offline/any error: use the fallback
            logger.warning(
                "stix latest-version lookup failed (%s); using the built-in newest known version",
                type(exc).__name__,
            )
            return self._fallback_latest()
        self._latest_resolved = version
        self._latest_resolved_at = time.monotonic()
        return version

    def _required_versions(self) -> list[str]:
        """Item (c): versions required by the curated seed (never removable)."""
        if self._required is not None:
            versions = list(self._required)
        else:
            from app.seeds.manifest import REQUIRED_STIX_VERSIONS

            versions = list(REQUIRED_STIX_VERSIONS)
        return [v for v in versions if _STIX_VERSION_RE.fullmatch(v)]

    def _desired(
        self,
        persisted: Iterable[str],
        extra: Iterable[str] = (),
        *,
        latest: str | None = None,
    ) -> list[str]:
        """Union of (a) latest/configured, (b) persisted, (c) seed-required and requested.

        ``latest`` is the already-resolved newest version (``run``); ``None`` means
        "no network": the value resolved earlier in this process or the built-in
        fallback. A ``latest`` token already satisfied by a newer persisted version
        adds nothing.
        """
        from app.services.stix.stix_service import _semantic_version_key

        persisted = list(persisted)
        if latest is None:
            latest = self._plan_latest()
        desired: set[str] = set(persisted) | set(self._required_versions()) | set(extra)
        for token in self._tokens():
            token = token.strip()
            if token.lower() == "latest":
                newer_installed = any(
                    _semantic_version_key(p) >= _semantic_version_key(latest) for p in persisted
                )
                if not newer_installed:
                    desired.add(latest)
            elif _STIX_VERSION_RE.fullmatch(token):
                desired.add(token)
        return sorted(desired, key=_semantic_version_key, reverse=True)

    @staticmethod
    def _persisted(session: Session) -> list[str]:
        from app.services.stix.stix_package_read_service import StixPackageReadService

        return StixPackageReadService(session).list_persisted_versions()

    # ---- runner contract ----------------------------------------------------

    def plan(self, session: Session) -> ReconcilePlan:
        persisted = self._persisted(session)
        with self._lock:
            requested = set(self._requested)
        desired = self._desired(persisted, requested)
        missing = [version for version in desired if version not in persisted]
        return ReconcilePlan(
            source=self.source,
            needs_fetch=bool(missing),
            missing_embed=0,
            loaded_count=len(persisted),
            embedded_count=len(persisted),
            expected_count=len(desired),
        )

    def run(
        self, session: Session, plan: ReconcilePlan, progress: ProgressCallback
    ) -> RunOutcome | None:
        from app.services.stix.stix_persistence_service import StixPersistenceService
        from app.services.stix.stix_service import StixService

        service = self._stix_service if self._stix_service is not None else StixService()
        indexer = None
        if self._index_vectors:
            from app.services.stix.stix_vector_index_service import StixVectorIndexService

            indexer = StixVectorIndexService(
                session,
                memory=ExperienceMemory(session, embedder=GuardedEmbedder(self._embedder)),
            )
        persistence = StixPersistenceService(session, service, vector_indexer=indexer)

        attempted: set[str] = set()
        first_failure: BaseException | None = None
        extra: set[str] = set()
        # Ask the upstream index once per run (only when ``latest`` is wanted).
        latest = (
            self._resolve_latest()
            if "latest" in {t.strip().lower() for t in self._tokens()}
            else None
        )
        while True:
            extra |= self._take_requested()
            persisted = self._persisted(session)
            desired = self._desired(persisted, extra, latest=latest)
            todo = [v for v in desired if v not in persisted and v not in attempted]
            if not todo:
                break
            for version in todo:
                attempted.add(version)
                try:
                    persistence.download_and_persist(version)
                except Exception as exc:  # noqa: BLE001 -- isolate this version
                    session.rollback()
                    logger.warning("stix ingestion of %s failed: %s", version, type(exc).__name__)
                    if first_failure is None:
                        first_failure = exc
                progress(len(attempted), len(desired))
        if first_failure is not None:
            raise first_failure
        return None

    def counts(self, session: Session) -> tuple[int, int]:
        from app.models.domain import StixPackage

        loaded = int(session.execute(select(func.count()).select_from(StixPackage)).scalar_one())
        return loaded, loaded


class CuratedSeedSourceRunner:
    """Curated cases shipped as JSON files in the backend (``app/seeds/data``).

    Offline by construction: ``plan`` compares the database with the light
    manifest (:mod:`app.seeds.manifest`) and never opens the JSON files; only ``run``
    does, through :func:`apply_curated_seed` and the lazy loader. It runs
    before ``atomic`` because the atomic affinity bootstrap derives from the
    curated cases.
    """

    source = IngestSource.CURATED_SEED

    def plan(self, session: Session) -> ReconcilePlan:
        from app.models.domain import Ability, Adversary, AttackFlowRow, Case
        from app.models.enums import FlowOrigin
        from app.seeds.manifest import CAMPAIGN_SLUGS, EXPECTED_COUNTS

        slugs = list(CAMPAIGN_SLUGS)

        def _count(column: Any, *extra: Any) -> int:
            statement = (
                select(func.count()).select_from(column.class_).where(column.in_(slugs), *extra)
            )
            return int(session.execute(statement).scalar_one())

        present = {
            "campaigns": _count(Case.id),
            "abilities": _count(Ability.campaign_id),
            "adversaries": _count(Adversary.campaign_id),
            "attack_flows": _count(
                AttackFlowRow.campaign_id,
                AttackFlowRow.origin == FlowOrigin.HUMAN_CURATION,
            ),
        }
        missing = sum(max(0, EXPECTED_COUNTS[kind] - have) for kind, have in present.items())
        return ReconcilePlan(
            source=self.source,
            missing_rows=missing,
            loaded_count=present["campaigns"],
            embedded_count=present["campaigns"],
            expected_count=EXPECTED_COUNTS["campaigns"],
        )

    def run(
        self, session: Session, plan: ReconcilePlan, progress: ProgressCallback
    ) -> RunOutcome | None:
        from app.seeds.apply import apply_curated_seed

        apply_curated_seed(session)
        session.flush()
        return None

    def counts(self, session: Session) -> tuple[int, int]:
        from app.models.domain import Case
        from app.seeds.manifest import CAMPAIGN_SLUGS

        loaded = int(
            session.execute(
                select(func.count()).select_from(Case).where(Case.id.in_(CAMPAIGN_SLUGS))
            ).scalar_one()
        )
        return loaded, loaded


def build_default_runners(
    *, embedder: Embedder | None = None
) -> Mapping[IngestSource, SourceRunner]:
    """Production runners sharing one (lazily created) embedder."""
    return {
        IngestSource.STIX_PACKAGES: StixPackagesSourceRunner(embedder=embedder),
        IngestSource.CURATED_SEED: CuratedSeedSourceRunner(),
        IngestSource.ATOMIC: AtomicSourceRunner(embedder=embedder),
        IngestSource.DOCKER_CATALOG: DockerCatalogSourceRunner(embedder=embedder),
        IngestSource.ATTACK_FLOW: AttackFlowSourceRunner(embedder=embedder),
    }


__all__ = [
    "STIX_VERSIONS_ENV",
    "AtomicSourceRunner",
    "AttackFlowSourceRunner",
    "CuratedSeedSourceRunner",
    "DockerCatalogSourceRunner",
    "GuardedEmbedder",
    "StixPackagesSourceRunner",
    "build_default_runners",
]
