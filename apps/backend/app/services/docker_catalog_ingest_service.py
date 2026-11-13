"""Docker image catalog ingestion — fetch Docker Hub, persist idempotently.

This module ingests a curated, **text-only** catalog of Docker Hub image
descriptions into the ``image_catalog`` table (design Component 4, Req. 5.1-5.3,
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

import contextlib
import json
import logging
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

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


ProgressCallback = Callable[[int, int], None]


def docker_entry_embedding_content(
    image: str, target_os: str, description: str, services: Iterable[str]
) -> str:
    """Text embedded for one catalog entry (shared with the orchestrator plan)."""
    joined = ", ".join(services)
    return f"docker image {image} ({target_os}): {description} services: {joined}".strip()


class DockerCatalogFetchError(RuntimeError):
    """Raised when the Docker Hub catalog cannot be fetched from the public API.

    Signals the fetch-isolation contract (Req. 3.4 / 5.1): the ingest reports
    the fetch failure and the already-persisted ``image_catalog`` is preserved
    unchanged (no ingest write is attempted). The originating exception is
    chained as ``__cause__``.

    The original cause is preserved in the message and attributes: HTTP
    ``status`` (when any), the request ``url`` path (no query string, no
    secrets) and the ``message`` field of the response body.
    """

    def __init__(
        self,
        text: str = "",
        *,
        status: int | None = None,
        url: str = "",
        message: str = "",
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(text)
        self.status = status
        self.url = url
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after


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
    # Non-fatal fetch problems (a query failed but others delivered images).
    warnings: list[dict[str, str]] = field(default_factory=list)


#: Search terms used to find popular NON-official images relevant to the lab.
#: The anonymous Hub API serves at most 200 results per query (offset limit),
#: so a curated list of terms is used instead of one huge ``library`` query.
CURATED_SEARCH_TERMS: tuple[str, ...] = (
    "nginx",
    "httpd",
    "apache",
    "python",
    "node",
    "redis",
    "mysql",
    "mariadb",
    "postgres",
    "mongodb",
    "ubuntu",
    "debian",
    "alpine",
    "centos",
    "php",
    "wordpress",
    "tomcat",
    "jenkins",
    "grafana",
    "elasticsearch",
    "rabbitmq",
    "dotnet",
    "samba",
    "openssh",
    "ftp",
    "vsftpd",
    "proftpd",
    "bind9",
    "dns",
    "openldap",
    "smb",
    "postfix",
    "dovecot",
    "drupal",
    "joomla",
    "gitlab",
    "nextcloud",
    "prometheus",
    "kibana",
    "logstash",
    "memcached",
    "couchdb",
    "traefik",
    "haproxy",
    "squid",
    "mosquitto",
    "vault",
    "consul",
    "sonarqube",
    "phpmyadmin",
    "jupyter",
    "metasploit",
    "kali",
)

#: Environment variables read by the runner/fetcher (as the other project vars).
MIN_IMAGES_ENV = "DOCKER_CATALOG_MIN_IMAGES"
MIN_PULLS_ENV = "DOCKER_CATALOG_MIN_PULLS"
DEFAULT_MIN_IMAGES = 150
DEFAULT_MIN_PULLS = 100_000

USER_AGENT = "sticks-ingest/1.0 (+https://github.com/c2dc/sticks)"

_MAX_BACKOFF_SECONDS = 30.0
_MAX_RETRY_AFTER_SECONDS = 60.0
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_FAILED_QUERIES = 3


def env_int(name: str, default: int) -> int:
    """Non-negative int from the environment; ``default`` when unset/invalid."""
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


class _PaginationLimitReached(DockerCatalogFetchError):
    """Anonymous pagination limit (HTTP 403): the normal end of a query."""


class DockerHubCatalogFetcher:
    """Fetch strategy for the public Docker Hub API, text only, OUTSIDE the lab.

    Strategy (verified against the real API; the anonymous limit is 200 results
    per query, so ``next`` is never followed):

    1. **Official images** from ``/v2/repositories/library/?page_size=100``,
       queried twice (``ordering=name`` and ``ordering=-name``); the union by
       name covers the whole official namespace (181 images at the time of
       writing). The image reference is the plain name (``nginx``).
    2. **Popular non-official images** from ``/v2/search/repositories/`` for a
       curated list of lab-relevant terms (:data:`CURATED_SEARCH_TERMS`), pages
       1-2 with ``page_size=100``; results below ``min_pull_count`` pulls or
       without description are dropped. Entries are deduplicated by name.

    All entries are cataloged as ``linux``: Docker Hub has no official Windows
    images in the ``library`` namespace (Microsoft publishes on
    ``mcr.microsoft.com``).

    Robustness: HTTP 429/5xx and timeouts are retried (up to ``max_attempts``)
    with exponential backoff plus jitter, honoring ``Retry-After`` /
    ``x-ratelimit-reset``; the anonymous pagination 403 (or an empty page) ends a
    query normally. A query failing unrecoverably is recorded in
    :attr:`warnings` and the others continue; the fetch only raises
    :class:`DockerCatalogFetchError` when no image was obtained at all.
    """

    def __init__(
        self,
        *,
        api_root: str = _DOCKER_HUB_API,
        timeout: float = 60.0,
        page_size: int = 100,
        search_terms: Iterable[str] | None = None,
        min_pull_count: int | None = None,
        max_attempts: int = 4,
        request_delay: float = 0.3,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._api_root = api_root.rstrip("/")
        self._timeout = timeout
        self._page_size = min(max(page_size, 1), 100)
        self._terms = tuple(search_terms) if search_terms is not None else CURATED_SEARCH_TERMS
        self._min_pulls = (
            min_pull_count
            if min_pull_count is not None
            else env_int(MIN_PULLS_ENV, DEFAULT_MIN_PULLS)
        )
        self._max_attempts = max(max_attempts, 1)
        self._request_delay = request_delay
        self._sleep = sleep
        self._clock = clock
        #: Non-fatal problems of the last fetch, as ``{"item", "reason"}``.
        self.warnings: list[dict[str, str]] = []

    # ---- public API --------------------------------------------------------

    def fetch_entries(self, *, max_images: int) -> Iterable[DockerCatalogEntry]:
        """Yield up to ``max_images`` text-only catalog entries from Docker Hub.

        Only text is retrieved; no image layers are pulled (Req. 5.1). Raises
        :class:`DockerCatalogFetchError` (with status, URL and message) only
        when a query failed and no image at all was obtained.
        """
        self.warnings = []
        seen: set[str] = set()
        produced = 0
        failures: list[DockerCatalogFetchError] = []

        for label, urls, official in self._queries():
            if produced >= max_images or len(failures) >= _MAX_FAILED_QUERIES:
                break
            try:
                for result in self._iter_results(label, urls):
                    if produced >= max_images:
                        break
                    entry = self._result_to_entry(result, official=official)
                    if entry is None or entry.image in seen:
                        continue
                    if not official and not self._is_popular(result):
                        continue
                    seen.add(entry.image)
                    produced += 1
                    yield entry
            except DockerCatalogFetchError as exc:
                failures.append(exc)
                logger.warning("docker catalog query %s failed: %s", label, exc)
                self.warnings.append({"item": label, "reason": str(exc)[:200]})

        if failures and produced == 0:
            raise failures[0]

    # ---- queries -----------------------------------------------------------

    def _queries(self) -> Iterable[tuple[str, list[str], bool]]:
        """``(label, page urls, is_official)`` for every query, in order."""
        for ordering in ("name", "-name"):
            yield (
                f"official ordering={ordering}",
                [f"{self._api_root}/v2/repositories/library/?page_size=100&ordering={ordering}"],
                True,
            )
        for term in self._terms:
            yield (
                f"search {term}",
                [
                    f"{self._api_root}/v2/search/repositories/"
                    f"?page_size=100&page={page}&query={urllib.parse.quote(term)}"
                    for page in (1, 2)
                ],
                False,
            )

    def _iter_results(self, label: str, urls: list[str]) -> Iterable[Any]:
        """Results of every page of a query; an empty page / pagination 403 ends it."""
        for index, url in enumerate(urls, start=1):
            try:
                payload = self._get_json(url, f"{label}, page {index}")
            except _PaginationLimitReached:
                return
            results = payload.get("results")
            if not isinstance(results, list) or not results:
                return
            yield from results
            if len(results) < 100:
                return

    def _is_popular(self, result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        pulls = result.get("pull_count")
        return isinstance(pulls, int) and pulls >= self._min_pulls

    # ---- HTTP --------------------------------------------------------------

    def _get_json(self, url: str, context: str) -> dict[str, Any]:
        """GET ``url`` with retries; text only, no layers (Req. 5.1)."""
        path = url.split(self._api_root, 1)[-1].split("?", 1)[0]
        last: DockerCatalogFetchError | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._sleep(self._request_delay)
            wait: float | None = None
            try:
                return self._request(url, path, context)
            except DockerCatalogFetchError as exc:
                if isinstance(exc, _PaginationLimitReached) or not exc.retryable:
                    raise
                last, wait = exc, exc.retry_after
            if attempt < self._max_attempts:
                delay = wait if wait is not None else self._backoff(attempt)
                self._sleep(min(delay, _MAX_RETRY_AFTER_SECONDS))
        assert last is not None  # noqa: S101 -- type narrowing; the retry loop always sets last before reaching here
        raise last

    def _backoff(self, attempt: int) -> float:
        base = min(2.0 ** (attempt - 1), _MAX_BACKOFF_SECONDS)
        return base + random.uniform(0, base / 2)  # noqa: S311 -- retry jitter, not a security-sensitive value

    def _request(self, url: str, path: str, context: str) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 -- trusted public API root
            url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310 -- URL is built from the trusted public API root
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            message = self._error_message(exc)
            text = f"HTTP {exc.code} on {path} ({context})" + (f": {message}" if message else "")
            if exc.code == 403 and "pagination" in message.lower():
                raise _PaginationLimitReached(
                    text, status=exc.code, url=path, message=message
                ) from exc
            raise DockerCatalogFetchError(
                text,
                status=exc.code,
                url=path,
                message=message,
                retryable=exc.code in _RETRYABLE_STATUS,
                retry_after=self._retry_after(exc.headers),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DockerCatalogFetchError(
                f"{type(exc).__name__} on {path} ({context}): {exc}",
                url=path,
                retryable=True,
            ) from exc
        try:
            document = json.loads(raw)
        except ValueError as exc:
            raise DockerCatalogFetchError(f"invalid JSON on {path} ({context})", url=path) from exc
        if not isinstance(document, dict):
            raise DockerCatalogFetchError(
                f"Docker Hub API at {path} did not return a JSON object.", url=path
            )
        return document

    @staticmethod
    def _error_message(exc: urllib.error.HTTPError) -> str:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 -- body is optional
            return ""
        if isinstance(body, dict):
            for key in ("message", "detail", "errinfo"):
                value = body.get(key)
                if isinstance(value, str):
                    return value[:200]
        return ""

    def _retry_after(self, headers: Any) -> float | None:
        """Seconds to wait from ``Retry-After`` / ``x-ratelimit-reset``, if present."""
        if headers is None:
            return None
        value = headers.get("Retry-After")
        if value is not None:
            try:
                return max(float(value), 0.0)
            except ValueError:
                return None
        reset = headers.get("x-ratelimit-reset")
        if reset is not None:
            try:
                number = float(reset)
            except ValueError:
                return None
            # Either an epoch timestamp or a number of seconds.
            return max(number - self._clock(), 0.0) if number > 1e9 else max(number, 0.0)
        return None

    @staticmethod
    def _result_to_entry(result: Any, *, official: bool = False) -> DockerCatalogEntry | None:
        """Normalize one Docker Hub result into a text-only entry.

        Entries without a description are dropped (useless for semantic search).
        The target OS is always ``linux`` (no official Windows images on the Hub).
        """
        if not isinstance(result, dict):
            return None
        name = result.get("repo_name") or result.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        description = result.get("short_description") or result.get("description") or ""
        if not isinstance(description, str) or not description.strip():
            return None
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
    docstring for the full contract (Req. 5.1-5.3, 5.5).
    """

    def __init__(
        self,
        session: Session,
        *,
        memory: ExperienceMemory,
        fetcher: DockerHubCatalogFetcher | None = None,
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
        progress: ProgressCallback | None = None,
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
            if progress is not None and entries_seen % 100 == 0:
                progress(entries_seen, 0)

        return DockerCatalogIngestSummary(
            entries_seen=entries_seen,
            rows_inserted=rows_inserted,
            rows_skipped=rows_skipped,
            embeddings_stored=embeddings_stored,
            vector_failures=vector_failures,
            capped=capped,
            errors=errors,
            warnings=list(getattr(self._fetcher, "warnings", None) or []),
        )

    def vectorize_missing(self, progress: ProgressCallback | None = None) -> tuple[int, list[str]]:
        """Embed the stored catalog rows that have no embedding yet (no network).

        Returns ``(embeddings_stored, errors)``; failures are isolated per row
        exactly as in :meth:`ingest`.
        """
        from app.models.domain import ImageCatalog

        rows = self._session.execute(select(ImageCatalog).order_by(ImageCatalog.id)).scalars().all()
        stored_count = 0
        errors: list[str] = []
        for index, row in enumerate(rows, start=1):
            entry = DockerCatalogEntry(
                image=row.image,
                target_os=row.target_os,
                description=row.notes or "",
                services=tuple(row.services or ()),
            )
            stored, error = self._vectorize_entry(entry)
            if stored:
                stored_count += 1
            elif error is not None:
                errors.append(error)
            if progress is not None and index % 100 == 0:
                progress(index, 0)
        return stored_count, errors

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

    def _vectorize_entry(self, entry: DockerCatalogEntry) -> tuple[bool, str | None]:
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
        except Exception as exc:  # noqa: BLE001 -- vectorization is best-effort
            # The Vector_Store (embedder/pgvector/session) failed for this entry.
            # Roll back ONLY the savepoint so the relational row is preserved and
            # the ingest continues with the catalog intact (Req. 5.3).
            with contextlib.suppress(Exception):  # best-effort cleanup
                savepoint.rollback()
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
        return docker_entry_embedding_content(
            entry.image, entry.target_os, entry.description, entry.services
        )


__all__ = [
    "CURATED_SEARCH_TERMS",
    "DEFAULT_MIN_IMAGES",
    "MIN_IMAGES_ENV",
    "DockerCatalogEntry",
    "DockerCatalogFetchError",
    "DockerCatalogIngestService",
    "DockerCatalogIngestSummary",
    "DockerHubCatalogFetcher",
    "env_int",
]
