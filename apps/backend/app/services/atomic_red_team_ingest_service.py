"""Atomic Red Team ingestion — download, parse, persist, then vectorize.

This module ingests the official ``redcanaryco/atomic-red-team`` GitHub
repository into the Command_Base (design Component 7, Req. 7.1-7.3, 7.6, 7.7).

Collaborators and the single injectable boundary:

* :class:`AtomicRepoFetcher` downloads the repository tarball from
  ``codeload.github.com`` (identifiable ``User-Agent``, redirects, timeout,
  retries with backoff for 5xx / 429 / timeouts) and reads it as a **stream**
  (``tarfile`` mode ``r|gz``). Only the members ``*/atomics/T####[.###]/T####[.###].yaml``
  are kept (``atomics/Indexes/`` and every other file are ignored) and they are
  handed over as :class:`AtomicDocument` bytes. The repository is never extracted
  to disk. The fetch runs in the application layer, outside the contained lab
  (Req. 7.1). The fetcher is injectable: a double only needs
  ``fetch_documents(ref=...) -> list[AtomicDocument]``.
* :class:`AtomicRedTeamIngestService` runs in two phases.

  1. **Relational**: every document is parsed (a malformed or unreadable one is
     isolated and reported in ``skipped``), the count is checked for plausibility
     and ALL ``technique_commands`` rows are persisted, idempotently by
     ``(technique_id, command_hash, source)``, in committed batches.
  2. **Vector**: the ``command_affinity`` rows of ``experience_memory`` are
     created in batches (one ``/api/embed`` call per batch) with failure
     isolation per batch and per entry. If the embedder fails, the relational
     data stays complete and :meth:`AtomicRedTeamIngestService.vectorize_missing`
     resumes later without downloading again.

Disk usage: by default nothing is written to disk (everything is parsed from
memory). When ``INGEST_WORK_DIR`` is set to a valid absolute directory (for
example one with an antivirus exception) the selected YAML files are staged in a
unique ``atomic_red_team_<uuid>`` subfolder of it, parsed from there (falling
back to the in-memory bytes if a file cannot be read) and the subfolder is
removed at the end, on success and on failure.

Parser decisions: tests whose executor has no ``command`` (``manual`` executors
with ``steps``) are discarded. ``#{argument}`` placeholders are kept literally
(the stored command is the upstream text; there is no column for a derived
resolved form; substitution happens on read). Each test's ``input_arguments``
map is stored normalized in ``technique_commands.input_arguments``; a malformed
map becomes "no arguments" without failing the technique. ``command_hash`` is
always computed over the original command text. Every ``supported_platforms``
entry is kept as the platform token, so ``containers`` and ``iaas:*`` commands
are ingested too. Commands are unique per technique by hash; the first
platform/executor seen for that command is the one stored.

Plausibility: a fetch that yields zero techniques, or fewer than
``ATOMIC_MIN_TECHNIQUES`` (default 100), raises
:class:`ImplausibleAtomicCountError` BEFORE any write, so the source ends
``failed`` (``implausible_atomic_count``) and never ``ready``.

Importing this module performs no I/O (Req. 1.7).
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import logging
import re
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.ai.memory import ExperienceMemory
from app.services.ingestion.workdir import read_env_int, scratch_dir

logger = logging.getLogger(__name__)

# Provenance recorded on every row/affinity ingested by this service. It is the
# ``source`` component of the idempotency key (technique_id, command_hash,
# source) and the affinity ``metadata.source``.
_SOURCE = "atomic_red_team"

# The official repository ingested as the source of truth (Req. 7.1).
_REPO_OWNER = "redcanaryco"
_REPO_NAME = "atomic-red-team"
_TARBALL_URL = f"https://codeload.github.com/{_REPO_OWNER}/{_REPO_NAME}/tar.gz/refs/heads/"
USER_AGENT = "sticks-ingest/1.0 (+https://github.com/c2dc/sticks)"

#: Plausibility floor: minimum number of techniques a complete fetch yields.
MIN_TECHNIQUES_ENV = "ATOMIC_MIN_TECHNIQUES"
DEFAULT_MIN_TECHNIQUES = 100

#: Failure code reported when the fetched repository looks incomplete.
IMPLAUSIBLE_COUNT_CODE = "implausible_atomic_count"

# Only ``<top>/atomics/T####/T####.yaml`` members carry atomics (also
# sub-techniques such as ``T1059.001``); ``atomics/Indexes/`` never matches.
_MEMBER_RE = re.compile(r"^[^/]+/atomics/(T\d{4}(?:\.\d{3})?)/\1\.yaml$")

_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 30.0

# Sentinel campaign id under which the campaign-agnostic command affinities are
# stored (mirrors ``_NO_CAMPAIGN`` in :mod:`app.services.command_base`).
_AFFINITY_CAMPAIGN_ID = "__command_base_no_campaign__"

_AFFINITY_KIND = "command_affinity"
_ROW_BATCH_SIZE = 500
_EMBED_BATCH_SIZE = 32
_MAX_REPORTED_ERRORS = 50


ProgressCallback = Callable[[int, int], None]


def atomic_affinity_dedup_key(technique_id: str, executor: str, platform: str, command: str) -> str:
    """Deterministic ``metadata.dedup_key`` of one Atomic command affinity."""
    return hashlib.sha256(
        f"{_SOURCE}|{technique_id}|{executor}|{platform}|{command}".encode()
    ).hexdigest()


def min_techniques() -> int:
    """Configured plausibility floor (``ATOMIC_MIN_TECHNIQUES``, default 100)."""
    return read_env_int(MIN_TECHNIQUES_ENV, DEFAULT_MIN_TECHNIQUES)


class AtomicFetchError(RuntimeError):
    """Raised when the Atomic Red Team repository cannot be fetched from GitHub.

    Signals Requirement 7.7: the ingest reports the fetch failure and the
    already-persisted Command_Base index is preserved unchanged (no write is
    attempted). The originating exception is chained as ``__cause__`` and the
    attempted ``ref`` is carried for diagnostics.
    """

    def __init__(self, message: str, *, ref: str) -> None:
        super().__init__(message)
        self.ref = ref


class ImplausibleAtomicCountError(RuntimeError):
    """The fetched repository has zero techniques or fewer than the floor."""

    code = IMPLAUSIBLE_COUNT_CODE

    def __init__(self, techniques: int, minimum: int) -> None:
        super().__init__(
            f"{IMPLAUSIBLE_COUNT_CODE}: {techniques} techniques parsed, "
            f"expected at least {max(minimum, 1)}."
        )
        self.techniques = techniques
        self.minimum = minimum


@dataclass(frozen=True)
class AtomicDocument:
    """One ``atomics/T####/T####.yaml`` file, as bytes (or why it is unusable)."""

    name: str
    data: bytes = b""
    error: str | None = None


@dataclass(frozen=True)
class AtomicIngestSummary:
    """Outcome of an :meth:`AtomicRedTeamIngestService.ingest` run.

    ``techniques`` counts the technique documents parsed; ``commands_parsed`` the
    command entries extracted (one per supported platform); ``expected_count`` the
    unique commands (per technique) the repository holds; ``commands_inserted``
    the new ``technique_commands`` rows (0 on an unchanged re-ingest);
    ``affinities_inserted`` the new experience-memory affinity rows;
    ``skipped_atomics`` the documents isolated (listed in ``skipped`` as
    ``{"item", "reason"}``); ``errors`` the vectorization failures.
    """

    techniques: int = 0
    commands_parsed: int = 0
    expected_count: int = 0
    commands_inserted: int = 0
    affinities_inserted: int = 0
    skipped_atomics: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _ParsedCommand:
    """One normalized command extracted from an atomic's ``atomic_tests``."""

    technique_id: str
    command: str
    executor: str
    platform: str
    input_arguments: dict[str, dict[str, str | None]] | None = None


def _optional_text(value: Any) -> str | None:
    """Scalar value as text; ``None`` for null, containers or anything unusable."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def normalize_input_arguments(
    raw: Any,
) -> dict[str, dict[str, str | None]] | None:
    """Normalize an Atomic ``input_arguments`` map (Req. 11.1).

    Returns ``{name: {"default", "description", "type"}}`` (each value a string
    or ``None``). A missing, empty or malformed map (not a mapping, or with no
    usable entry) yields ``None``, i.e. "no arguments", never an error. Entries
    whose name is not a non-empty string or whose body is not a mapping are
    dropped individually.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    result: dict[str, dict[str, str | None]] = {}
    for name, body in raw.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(body, dict):
            continue
        result[name] = {
            "default": _optional_text(body.get("default")),
            "description": _optional_text(body.get("description")),
            "type": _optional_text(body.get("type")),
        }
    return result or None


class AtomicRepoFetcher:
    """Streaming tarball fetcher for ``redcanaryco/atomic-red-team``.

    ``fetch_documents`` downloads
    ``https://codeload.github.com/redcanaryco/atomic-red-team/tar.gz/refs/heads/<ref>``
    and returns only the technique YAML files, read straight into memory. A 5xx /
    429 response, a timeout, a connection error or a truncated archive is retried
    with exponential backoff; other HTTP errors (for example 404 for an unknown
    ref) fail immediately. ``sleep`` and ``opener`` are injectable for tests.
    """

    def __init__(
        self,
        *,
        timeout: float = 60.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._timeout = timeout
        self._retries = max(0, retries)
        self._sleep = sleep
        self._opener = opener or urllib.request.urlopen

    def fetch_documents(self, *, ref: str) -> list[AtomicDocument]:
        """Download ``ref`` and return its technique YAML files (never touches disk)."""
        url = _TARBALL_URL + urllib.parse.quote(ref, safe="/")
        last_exc: BaseException | None = None
        for attempt in range(self._retries + 1):
            try:
                return self._download(url)
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code not in _RETRYABLE_STATUS:
                    break
            except (
                urllib.error.URLError,
                OSError,  # includes TimeoutError / ConnectionError
                http.client.HTTPException,
                tarfile.TarError,  # includes tarfile.ReadError
                gzip.BadGzipFile,
                EOFError,
                zlib.error,
            ) as exc:
                last_exc = exc
            if attempt < self._retries:
                delay = min(_MAX_BACKOFF_SECONDS, 2.0**attempt)
                logger.warning(
                    "atomic tarball fetch failed (%s: %s); retry %d/%d in %.0f s",
                    type(last_exc).__name__,
                    last_exc,
                    attempt + 1,
                    self._retries,
                    delay,
                )
                self._sleep(delay)
        raise AtomicFetchError(
            f"could not download {url}: {type(last_exc).__name__}: {last_exc}", ref=ref
        ) from last_exc

    def _download(self, url: str) -> list[AtomicDocument]:
        request = urllib.request.Request(  # noqa: S310 -- URL comes from the pinned release, not user input
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/x-gzip"}
        )
        documents: list[AtomicDocument] = []
        with self._opener(request, timeout=self._timeout) as response:
            counter = _CountingReader(response)
            # GzipFile (not tarfile's own "r|gz") so that a truncated stream raises
            # EOFError and the CRC/size trailer is verified once fully drained.
            gz = gzip.GzipFile(fileobj=counter, mode="rb")
            with tarfile.open(fileobj=gz, mode="r|") as archive:
                for member in archive:
                    match = _MEMBER_RE.match(member.name)
                    if match is None or not member.isfile():
                        continue
                    name = match.group(1)
                    if member.size > _MAX_DOCUMENT_BYTES:
                        documents.append(AtomicDocument(name, error="file_too_large"))
                        continue
                    handle = archive.extractfile(member)
                    if handle is None:
                        documents.append(AtomicDocument(name, error="unreadable_member"))
                        continue
                    documents.append(AtomicDocument(name, data=handle.read()))
            # Drain to EOF: validates the gzip trailer (CRC/size). A truncated
            # download raises EOFError / BadGzipFile / zlib.error (retryable).
            while gz.read(1024 * 1024):
                pass
            expected = _content_length(response)
            if expected is not None and counter.count != expected:
                raise EOFError(
                    f"truncated download: read {counter.count} bytes, Content-Length {expected}"
                )
        return documents


class _CountingReader:
    """Minimal read-only wrapper counting the bytes consumed from ``raw``."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self.count += len(data)
        return data


def _content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("Content-Length")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class AtomicRedTeamIngestService:
    """Ingest Atomic Red Team into the Command_Base, idempotently.

    Session-injected; the Experience_Memory is injected so the associative
    affinities share the unit of work, and the fetcher is injectable so the
    fetch strategy can be doubled in tests. See the module docstring.
    """

    def __init__(
        self,
        session: Session,
        *,
        memory: ExperienceMemory,
        fetcher: Any | None = None,
    ) -> None:
        self._session = session
        self._memory = memory
        self._fetcher = fetcher or AtomicRepoFetcher()

    # ---- public API --------------------------------------------------------

    def ingest(
        self, *, ref: str = "master", progress: ProgressCallback | None = None
    ) -> AtomicIngestSummary:
        """Fetch, parse, persist the relational rows, then vectorize them.

        A fetch failure raises :class:`AtomicFetchError` and an implausible
        count raises :class:`ImplausibleAtomicCountError`, both before any write.
        Once the relational phase is done ``progress(total, total)`` reports the
        expected unique commands; vectorization failures are returned in
        ``errors`` and the relational data remains complete.
        """
        documents = self._fetcher.fetch_documents(ref=ref)
        commands, techniques, parsed_count, skipped = self._parse_documents(documents)

        minimum = min_techniques()
        if techniques == 0 or techniques < minimum:
            logger.warning(
                "atomic fetch looks incomplete: %d techniques (floor %d), %d skipped",
                techniques,
                minimum,
                len(skipped),
            )
            raise ImplausibleAtomicCountError(techniques, minimum)

        inserted = self._persist_commands(commands, progress)
        total = len(commands)
        if progress is not None:
            # Saves expected_count and commits: the relational part is complete.
            progress(total, total)

        affinities, errors = self.vectorize_missing(progress)
        return AtomicIngestSummary(
            techniques=techniques,
            commands_parsed=parsed_count,
            expected_count=total,
            commands_inserted=inserted,
            affinities_inserted=affinities,
            skipped_atomics=len(skipped),
            skipped=skipped,
            errors=errors,
        )

    def vectorize_missing(self, progress: ProgressCallback | None = None) -> tuple[int, list[str]]:
        """Embed the stored commands that have no affinity yet (no network download).

        Works in batches of :data:`_EMBED_BATCH_SIZE` (one embedding call each).
        A failing batch is retried entry by entry so one bad entry never blocks
        the others. Returns ``(inserted, errors)``; embedder errors never
        propagate, so the caller decides the final state.
        """
        from app.models.domain import ExperienceMemory as ExperienceMemoryRow
        from app.models.domain import TechniqueCommand

        existing = {
            key
            for (key,) in self._session.execute(
                select(ExperienceMemoryRow.meta["dedup_key"].as_string()).where(
                    ExperienceMemoryRow.meta["source"].as_string() == _SOURCE
                )
            )
            if key
        }
        rows = (
            self._session.execute(
                select(TechniqueCommand)
                .where(TechniqueCommand.source == _SOURCE)
                .order_by(TechniqueCommand.id)
            )
            .scalars()
            .all()
        )

        pending: list[tuple[str, dict[str, Any], str]] = []
        for row in rows:
            item = _ParsedCommand(
                technique_id=row.technique_id,
                command=row.command,
                executor=row.executor or "unknown",
                platform=row.platform or "unknown",
            )
            key = atomic_affinity_dedup_key(
                item.technique_id, item.executor, item.platform, item.command
            )
            if key not in existing:
                pending.append(self._affinity_entry(item, key))

        total = len(rows)
        done = total - len(pending)
        inserted = 0
        errors: list[str] = []
        for batch in _chunks(pending, _EMBED_BATCH_SIZE):
            inserted += self._store_batch(batch, errors)
            done += len(batch)
            if progress is not None:
                progress(done, total)
        return inserted, errors

    # ---- affinities --------------------------------------------------------

    @staticmethod
    def _affinity_entry(item: _ParsedCommand, key: str) -> tuple[str, dict[str, Any], str]:
        content = (
            f"technique {item.technique_id} executor {item.executor} "
            f"platform {item.platform}: {item.command}"
        )
        metadata = {
            "source": _SOURCE,
            "technique_id": item.technique_id,
            "command": item.command,
            "executor": item.executor,
            "platform": item.platform,
        }
        return content, metadata, key

    def _store_batch(
        self, batch: Sequence[tuple[str, dict[str, Any], str]], errors: list[str]
    ) -> int:
        """Store one batch; on failure retry entry by entry, recording the errors."""
        try:
            with self._session.begin_nested():
                return self._memory.store_affinities(
                    kind=_AFFINITY_KIND,
                    entries=batch,
                    campaign_id=_AFFINITY_CAMPAIGN_ID,
                )
        except Exception as exc:  # noqa: BLE001 -- isolate, then go entry by entry
            logger.warning(
                "affinity batch of %d failed (%s); retrying entry by entry",
                len(batch),
                getattr(exc, "code", type(exc).__name__),
            )
        stored = 0
        for content, metadata, key in batch:
            try:
                with self._session.begin_nested():
                    if self._memory.store_affinity(
                        kind=_AFFINITY_KIND,
                        content=content,
                        metadata=metadata,
                        campaign_id=_AFFINITY_CAMPAIGN_ID,
                        dedup_key=key,
                    ):
                        stored += 1
            except Exception as exc:  # noqa: BLE001 -- per-entry isolation
                if len(errors) < _MAX_REPORTED_ERRORS:
                    code = getattr(exc, "code", type(exc).__name__)
                    errors.append(f"{metadata.get('technique_id')}: {code}")
        return stored

    # ---- parsing -----------------------------------------------------------

    def _parse_documents(
        self, documents: Sequence[AtomicDocument]
    ) -> tuple[list[_ParsedCommand], int, int, list[dict[str, str]]]:
        """Parse every document, isolating failures.

        Returns ``(unique_commands, techniques, parsed_entries, skipped)``.
        Uses ``INGEST_WORK_DIR`` staging when configured, memory otherwise.
        """
        skipped: list[dict[str, str]] = []
        unique: dict[tuple[str, str], _ParsedCommand] = {}
        techniques = 0
        parsed_entries = 0

        with scratch_dir("atomic_red_team", only_configured=True) as spool_dir:
            for document in sorted(documents, key=lambda d: d.name):
                if document.error is not None:
                    skipped.append({"item": document.name, "reason": document.error})
                    continue
                data = self._spool_and_read(document, spool_dir)
                parsed, error = self._parse_atomic(data, document.name)
                if error is not None:
                    skipped.append({"item": document.name, "reason": error[:200]})
                    logger.warning("Skipping atomic %s: %s", document.name, error)
                    continue
                techniques += 1
                parsed_entries += len(parsed)
                for item in parsed:
                    unique.setdefault((item.technique_id, self._command_hash(item.command)), item)
        return list(unique.values()), techniques, parsed_entries, skipped

    @staticmethod
    def _spool_and_read(document: AtomicDocument, spool_dir: Path | None) -> bytes:
        """Round-trip the file through the work directory when one is configured."""
        if spool_dir is None:
            return document.data
        target = spool_dir / document.name / f"{document.name}.yaml"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(document.data)
            return target.read_bytes()
        except OSError as exc:
            logger.warning(
                "work directory unusable for %s (%s); parsing from memory.",
                document.name,
                exc,
            )
            return document.data

    def _parse_atomic(
        self, data: bytes, technique_id: str
    ) -> tuple[list[_ParsedCommand], str | None]:
        """Parse one atomic YAML into normalized commands.

        Returns ``(commands, None)`` or ``([], reason)`` when the document is
        malformed/unreadable so the caller can skip it without aborting.
        """
        # Imported lazily so importing this module never requires PyYAML (Req. 1.7).
        import yaml

        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        try:
            document = yaml.load(data.decode("utf-8-sig"), Loader=loader)  # noqa: S506 -- loader is a restricted SafeLoader subclass
        except (yaml.YAMLError, ValueError) as exc:
            return [], f"{type(exc).__name__}: {exc}"

        if not isinstance(document, dict):
            return [], "atomic document is not a YAML mapping."

        # Prefer the technique id declared in the document; fall back to the file
        # name so a mismatched/absent ``attack_technique`` never drops the atomic.
        declared = document.get("attack_technique")
        resolved = (
            declared.strip() if isinstance(declared, str) and declared.strip() else technique_id
        )

        atomic_tests = document.get("atomic_tests")
        if not isinstance(atomic_tests, list):
            return [], "atomic document has no 'atomic_tests' list."

        return self._extract_commands(resolved, atomic_tests), None

    @classmethod
    def _extract_commands(cls, technique_id: str, atomic_tests: list[Any]) -> list[_ParsedCommand]:
        """Extract ``atomic_tests[].executor.command`` normalized per platform.

        One :class:`_ParsedCommand` per supported platform. Tests without a
        runnable command (``manual`` executor with ``steps``) are discarded.
        """
        commands: list[_ParsedCommand] = []
        for test in atomic_tests:
            if not isinstance(test, dict):
                continue
            executor = test.get("executor")
            if not isinstance(executor, dict):
                continue
            command = executor.get("command")
            if not isinstance(command, str) or not command.strip():
                continue

            executor_name = cls._normalize_token(executor.get("name")) or "unknown"
            platforms = test.get("supported_platforms")
            if not isinstance(platforms, list) or not platforms:
                platforms = ["unknown"]

            normalized_command = cls._normalize_command(command)
            # ``{}`` (not NULL) marks "processed, no usable arguments", so that
            # NULL only means "ingested before the column existed" and the
            # reconciliation can tell the two apart (Req. 11.7).
            arguments = normalize_input_arguments(test.get("input_arguments")) or {}
            for platform in platforms:
                commands.append(
                    _ParsedCommand(
                        technique_id=technique_id,
                        command=normalized_command,
                        executor=executor_name,
                        platform=cls._normalize_token(platform) or "unknown",
                        input_arguments=arguments,
                    )
                )
        return commands

    @staticmethod
    def _normalize_command(command: str) -> str:
        """Normalize line endings and surrounding whitespace for a stable hash."""
        return command.replace("\r\n", "\n").replace("\r", "\n").strip()

    @staticmethod
    def _normalize_token(value: Any) -> str:
        """Lowercase/strip a platform or executor token for stable storage."""
        if not isinstance(value, str):
            return ""
        return value.strip().lower()

    @staticmethod
    def _command_hash(command: str) -> str:
        """Deterministic SHA-256 of the normalized command (idempotency key part)."""
        return hashlib.sha256(command.encode("utf-8")).hexdigest()

    # ---- relational persistence (exact half) -------------------------------

    def _persist_commands(
        self,
        commands: Sequence[_ParsedCommand],
        progress: ProgressCallback | None = None,
    ) -> int:
        """Insert the missing ``technique_commands`` rows in committed batches.

        Idempotent on ``(technique_id, command_hash, source)``: the existing keys
        are loaded once and only new rows are inserted (Req. 7.6). Returns the
        number of rows inserted. ``progress(done, 0)`` runs after each batch
        (heartbeat plus commit; ``total=0`` so ``expected_count`` is untouched).
        """
        from app.models.domain import TechniqueCommand

        existing_rows = {
            (row.technique_id, row.command_hash): row
            for row in self._session.execute(
                select(TechniqueCommand).where(TechniqueCommand.source == _SOURCE)
            ).scalars()
        }
        existing = set(existing_rows)
        # Existing rows keep their identity; only ``input_arguments`` is refreshed
        # (upsert), so a re-ingest fills rows ingested before the column existed
        # without duplicating them (Req. 11.7).
        seen: set[tuple[str, str]] = set()
        for item in commands:
            key = (item.technique_id, self._command_hash(item.command))
            seen.add(key)
            row = existing_rows.get(key)
            if row is not None and row.input_arguments != item.input_arguments:
                row.input_arguments = item.input_arguments
        # Rows no longer present upstream can never be filled; mark them as
        # processed so they do not keep the reconciliation re-downloading.
        for key, row in existing_rows.items():
            if key not in seen and row.input_arguments is None:
                row.input_arguments = {}
        new_rows = [
            TechniqueCommand(
                technique_id=item.technique_id,
                command=item.command,
                executor=item.executor,
                platform=item.platform,
                source=_SOURCE,
                command_hash=self._command_hash(item.command),
                input_arguments=item.input_arguments,
            )
            for item in commands
            if (item.technique_id, self._command_hash(item.command)) not in existing
        ]
        done = 0
        for batch in _chunks(new_rows, _ROW_BATCH_SIZE):
            self._session.add_all(batch)
            self._session.flush()
            done += len(batch)
            if progress is not None:
                progress(done, 0)
        return len(new_rows)


__all__ = [
    "DEFAULT_MIN_TECHNIQUES",
    "IMPLAUSIBLE_COUNT_CODE",
    "MIN_TECHNIQUES_ENV",
    "AtomicDocument",
    "AtomicFetchError",
    "AtomicIngestSummary",
    "AtomicRedTeamIngestService",
    "AtomicRepoFetcher",
    "ImplausibleAtomicCountError",
    "atomic_affinity_dedup_key",
    "min_techniques",
    "normalize_input_arguments",
]
