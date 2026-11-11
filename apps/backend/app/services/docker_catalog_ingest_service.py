"""Docker image catalog ingestion — fetch Docker Hub, persist idempotently.

This module ingests a curated, **text-only** catalog of Docker Hub image
descriptions into the ``image_catalog`` table (design Component 4, Req. 5.1–5.3,
5.5). It mirrors the shape of
:class:`~app.services.atomic_red_team_ingest_service.AtomicRedTeamIngestService`:
a session-injected orchestrator with an injectable fetch boundary, so the fetch
strategy can be doubled in tests without any network access.

Collaborators and boundaries:

* :class:`DockerHubCatalogFetcher` encapsulates the *fetch strategy* — it calls
  the public Docker Hub API (``https://hub.docker.com``) to list repositories
  and read each repository's text description. It downloads **only text**
  (repository name, description/overview, declared OS/services) and **never**
  pulls image layers (Req. 5.1). The fetch runs in the application layer,
  outside the contained lab, and is injectable so tests supply a no-network
  double.
* :class:`DockerCatalogIngestService` orchestrates the ingest: it asks the
  fetcher for the catalog entries (bounded by ``max_images`` and
  ``max_text_bytes``), upserts each entry into ``image_catalog`` idempotently by
  ``(image, target_os)`` (Req. 5.5), and best-effort vectorizes each
  description+services into the ``experience_memory`` Vector_Store with
  ``metadata.source = "docker_image_catalog"`` (Req. 5.3).

Idempotency follows the table's ``UniqueConstraint(image, target_os)``: each
entry is persisted only after a check-before-insert on that key, so re-running
the ingest reuses the existing row rather than duplicating it (Req. 5.5).

Best-effort vectorization (Req. 5.3): the embedding write goes through
:meth:`ExperienceMemory.store_affinity`, wrapped so that any Vector_Store /
embedder failure is logged and swallowed — it NEVER aborts the relational
persistence that already succeeded.

Caps (Req. 5.2): the ingest stops after ``max_images`` entries (≈10_000) or once
the cumulative text size reaches ``max_text_bytes`` (≈10_000_000), whichever
comes first, so the catalog stays bounded.

Importing this module performs no I/O: the ORM models, settings and the
Experience_Memory are resolved lazily inside the methods, matching the sibling
Session-injected services.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.ai.memory import ExperienceMemory

logger = logging.getLogger(__name__)

# Provenance recorded on every embedding vectorized by this service. It is the
# ``metadata.source`` of the stored affinity (design §Data Models / image_catalog
# note: ``metadata.source = "docker_image_catalog"``).
_SOURCE = "docker_image_catalog"

# The associative-knowledge kind the image embeddings are stored under. Image
# resolution is an ``environment``-phase concern, so the embeddings live under
# the ``environment_affinity`` kind of the Memory_Rule (see
# :data:`app.services.ai.memory.AFFINITY_KINDS`).
_AFFINITY_KIND = "environment_affinity"

# Approximate ingest ceilings (Req. 5.2): at most ~10_000 descriptions and a
# cumulative text budget of ~10 MB. Exceeding either stops the ingest.
_DEFAULT_MAX_IMAGES = 10_000
_DEFAULT_MAX_TEXT_BYTES = 10_000_000

# Public Docker Hub API root. Only text endpoints are used; image layers are
# never pulled (Req. 5.1).
_DOCKER_HUB_API = "https://hub.docker.com"

# The two target operating systems the catalog is keyed by, consistent with the
# ``image_catalog`` / ``ImageResolver`` value set: ``linux`` | ``windows``.
_TARGET_OS_LINUX = "linux"
_TARGET_OS_WINDOWS = "windows"


class DockerCatalogFetchError(RuntimeError):
    """Raised when the Docker Hub catalog cannot be fetched from the public API.

    Signals the fetch-isolation contract (Req. 3.4 / 5.1): the ingest reports
    the fetch failure and the already-persisted ``image_catalog`` is preserved
    unchanged (no ingest write is attempted). The originating exception is
    chained as ``__cause__``.
    """


@dataclass(frozen=True)
class DockerCatalogEntry:
    """One normalized, text-only Docker Hub image description.

    ``image`` is the image reference persisted into ``image_catalog.image``
    (e.g. ``"nginx:1.25"`` or ``"library/nginx"``); ``target_os`` is the OS the
    image is cataloged for (``linux`` | ``windows``); ``services`` is the
    optional list of services/ports the image covers (persisted as the JSON
    ``image_catalog.services``); ``description`` is the free-form overview text
    persisted into ``image_catalog.notes`` and embedded for semantic search.

    No image layers are carried — this is text only (Req. 5.1).
    """

    image: str
    target_os: str
    description: str = ""
    services: tuple[str, ...] = ()

    def text_size(self) -> int:
        """UTF-8 byte size of the text this entry contributes to the budget.

        Counts the image reference, the description and the services — the
        text persisted/embedded — so the ingest can enforce ``max_text_bytes``
        (Req. 5.2).
        """
        joined = f"{self.image}\n{self.description}\n{' '.join(self.services)}"
        return len(joined.encode("utf-8"))


@dataclass(frozen=True)
class DockerCatalogIngestSummary:
    """Outcome of a :meth:`DockerCatalogIngestService.ingest` run.

    ``entries_seen`` counts the fetched entries considered; ``rows_inserted``
    the new ``image_catalog`` rows created (re-ingests leave this at 0 for the
    unchanged entries); ``rows_skipped`` the entries whose ``(image,
    target_os)`` row already existed (idempotent reuse, Req. 5.5);
    ``embeddings_stored`` the new Vector_Store rows written best-effort;
    ``vector_failures`` the entries whose vectorization failed but did NOT abort
    the relational persistence (Req. 5.3); ``capped`` is True when a cap stopped
    the ingest early (Req. 5.2).
    """

    entries_seen: int = 0
    rows_inserted: int = 0
    rows_skipped: int = 0
    embeddings_stored: int = 0
    vector_failures: int = 0
    capped: bool = False
    errors: list[str] = field(default_factory=list)


class DockerHubCatalogFetcher:
    """Fetch strategy for the public Docker Hub API, text only, OUTSIDE the lab.

    Lists repositories and reads each repository's text description via the
    public Docker Hub REST API. It downloads **only text** (name, description,
    declared OS/services) and NEVER pulls image layers (Req. 5.1). The fetch
    runs in the application layer, never inside the contained lab.

    Injectable: :class:`DockerCatalogIngestService` accepts a fetcher so tests
    can double it with in-memory entries and no network access.
    """

    def __init__(
        self,
        *,
        api_root: str = _DOCKER_HUB_API,
        timeout: float = 60.0,
        page_size: int = 100,
    ) -> None:
        self._api_root = api_root.rstrip("/")
        self._timeout = timeout
        self._page_size = page_size

    def fetch_entries(self, *, max_images: int) -> Iterable[DockerCatalogEntry]:
        """Yield up to ``max_images`` text-only catalog entries from Docker Hub.

        Pages the public ``/v2/search/repositories`` API, reading each result's
        name and short description. Only text is retrieved; no image layers are
        pulled (Req. 5.1). Raises :class:`DockerCatalogFetchError` if the API
        cannot be reached, so the caller preserves the persisted catalog
        (Req. 5.1 / 3.4).
        """
        produced = 0
        page = 1
        try:
            while produced < max_images:
                payload = self._get_json(
                    f"{self._api_root}/v2/search/repositories/"
                    f"?page_size={self._page_size}&page={page}&query=library"
                )
                results = payload.get("results")
                if not isinstance(results, list) or not results:
                    break
                for result in results:
                    if produced >= max_images:
                        break
                    entry = self._result_to_entry(result)
                    if entry is None:
                        continue
                    produced += 1
                    yield entry
                page += 1
        except DockerCatalogFetchError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize into the fetch error
            raise DockerCatalogFetchError(
                f"could not fetch the Docker Hub catalog from {self._api_root}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def _get_json(self, url: str) -> dict[str, Any]:
        """GET ``url`` and parse a JSON object; text only, no layers (Req. 5.1)."""
        request = urllib.request.Request(  # noqa: S310 - trusted public API root
            url, headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise DockerCatalogFetchError(
                f"Docker Hub API at {url} did not return a JSON object."
            )
        return document

    @staticmethod
    def _result_to_entry(result: Any) -> Optional[DockerCatalogEntry]:
        """Normalize one Docker Hub search result into a text-only entry.

        Reads the repository name and short description; defaults the target OS
        to ``linux`` (the Docker Hub search API does not expose a reliable OS
        facet for every result). Returns ``None`` for malformed results so one
        bad entry never aborts the fetch.
        """
        if not isinstance(result, dict):
            return None
        name = result.get("repo_name") or result.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        description = result.get("short_description") or result.get("description") or ""
        if not isinstance(description, str):
            description = ""
        return DockerCatalogEntry(
            image=name.strip(),
            target_os=_TARGET_OS_LINUX,
            description=description.strip(),
            services=(),
        )


class DockerCatalogIngestService:
    """Ingest the Docker image catalog into ``image_catalog``, idempotently.

    Session-injected; the Experience_Memory is injected so the best-effort
    embeddings share the request-scoped unit of work, and the fetcher is
    injectable so the fetch strategy can be doubled in tests. See the module
    docstring for the full contract (Req. 5.1–5.3, 5.5).
    """

    def __init__(
        self,
        session: Session,
        *,
        memory: ExperienceMemory,
        fetcher: Optional[DockerHubCatalogFetcher] = None,
    ) -> None:
        self._session = session
        self._memory = memory
        self._fetcher = fetcher or DockerHubCatalogFetcher()

    # ---- public API --------------------------------------------------------

    def ingest(
        self,
        *,
        max_images: int = _DEFAULT_MAX_IMAGES,
        max_text_bytes: int = _DEFAULT_MAX_TEXT_BYTES,
    ) -> DockerCatalogIngestSummary:
        """Fetch, upsert and best-effort vectorize the Docker image catalog.

        Fetches up to ``max_images`` text-only descriptions from the public
        Docker Hub API (no layers, Req. 5.1), upserts each into ``image_catalog``
        idempotently by ``(image, target_os)`` (Req. 5.5), and best-effort
        embeds each description+services into the Vector_Store with
        ``metadata.source = "docker_image_catalog"`` (Req. 5.3). The ingest stops
        once ``max_images`` entries or ``max_text_bytes`` of cumulative text is
        reached (Req. 5.2).

        A fetch failure raises :class:`DockerCatalogFetchError` before any write,
        preserving the persisted catalog. A Vector_Store failure for one entry is
        isolated (logged, counted) and never aborts the relational persistence.
        """
        entries_seen = 0
        rows_inserted = 0
        rows_skipped = 0
        embeddings_stored = 0
        vector_failures = 0
        text_budget = 0
        capped = False
        errors: list[str] = []

        for entry in self._fetcher.fetch_entries(max_images=max_images):
            if entries_seen >= max_images:
                capped = True
                break

            next_budget = text_budget + entry.text_size()
            if next_budget > max_text_bytes:
                # The text budget would be exceeded by this entry: stop before
                # persisting it so the catalog stays within ~10 MB (Req. 5.2).
                capped = True
                break
            text_budget = next_budget
            entries_seen += 1

            inserted = self._upsert_entry(entry)
            if inserted:
                rows_inserted += 1
            else:
                rows_skipped += 1

            stored, error = self._vectorize_entry(entry)
            if stored:
                embeddings_stored += 1
            elif error is not None:
                vector_failures += 1
                errors.append(error)

        return DockerCatalogIngestSummary(
            entries_seen=entries_seen,
            rows_inserted=rows_inserted,
            rows_skipped=rows_skipped,
            embeddings_stored=embeddings_stored,
            vector_failures=vector_failures,
            capped=capped,
            errors=errors,
        )

    # ---- relational persistence (idempotent upsert) ------------------------

    def _upsert_entry(self, entry: DockerCatalogEntry) -> bool:
        """Insert an ``image_catalog`` row if absent; return True if inserted.

        Idempotent via a check-before-insert on the ``(image, target_os)`` key,
        matching the table's :class:`UniqueConstraint` so a re-ingest reuses the
        existing row rather than duplicating it (Req. 5.5). An existing row is
        left untouched (latest-wins is not required for the text-only catalog).
        """
        from app.models.domain import ImageCatalog

        existing = self._session.execute(
            select(ImageCatalog.id).where(
                ImageCatalog.image == entry.image,
                ImageCatalog.target_os == entry.target_os,
            )
        ).first()
        if existing is not None:
            return False

        self._session.add(
            ImageCatalog(
                image=entry.image,
                target_os=entry.target_os,
                services=list(entry.services) if entry.services else None,
                notes=entry.description or None,
            )
        )
        self._session.flush()
        return True

    # ---- vector persistence (best-effort) ----------------------------------

    def _vectorize_entry(
        self, entry: DockerCatalogEntry
    ) -> tuple[bool, Optional[str]]:
        """Best-effort embed ``description+services`` into the Vector_Store.

        Stores one associative row via :meth:`ExperienceMemory.store_affinity`
        with ``metadata.source = "docker_image_catalog"`` (Req. 5.3). The call is
        wrapped in a SAVEPOINT (``session.begin_nested``) so that ANY
        Vector_Store / embedder failure is isolated: only the embedding write is
        rolled back to the savepoint, leaving the ``image_catalog`` row that was
        already persisted for this entry intact. The failure is logged and
        reported, but it NEVER aborts the relational persistence (Req. 5.3).

        Returns ``(stored, error)``: ``(True, None)`` when a new embedding row
        was written, ``(False, None)`` when the embedding was a deduplicated
        no-op, and ``(False, error_message)`` when vectorization failed.
        """
        content = self._embedding_content(entry)
        # SAVEPOINT the embedding write so its failure rolls back ONLY the
        # vector row, never the relational ``image_catalog`` row flushed by
        # :meth:`_upsert_entry` in the same outer transaction (Req. 5.3).
        savepoint = self._session.begin_nested()
        try:
            stored = self._memory.store_affinity(
                kind=_AFFINITY_KIND,
                content=content,
                metadata={
                    "source": _SOURCE,
                    "image": entry.image,
                    "target_os": entry.target_os,
                    "services": list(entry.services),
                },
            )
            savepoint.commit()
            return stored, None
        except Exception as exc:  # noqa: BLE001 - vectorization is best-effort
            # The Vector_Store (embedder/pgvector/session) failed for this entry.
            # Roll back ONLY the savepoint so the relational row is preserved and
            # the ingest continues with the catalog intact (Req. 5.3).
            try:
                savepoint.rollback()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
            message = (
                f"vectorization skipped for image {entry.image!r} "
                f"({entry.target_os}): {type(exc).__name__}: {exc}"
            )
            logger.warning(message)
            return False, message

    @staticmethod
    def _embedding_content(entry: DockerCatalogEntry) -> str:
        """Render the text embedded for semantic technique→image search.

        Combines the image reference, its description and its services so the
        ``environment`` phase can retrieve candidate images by similarity
        (Req. 5.3, 5.4).
        """
        services = ", ".join(entry.services) if entry.services else ""
        return (
            f"docker image {entry.image} ({entry.target_os}): "
            f"{entry.description} services: {services}"
        ).strip()


__all__ = [
    "DockerCatalogEntry",
    "DockerCatalogFetchError",
    "DockerCatalogIngestService",
    "DockerCatalogIngestSummary",
    "DockerHubCatalogFetcher",
]
