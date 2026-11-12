"""Atomic Red Team ingestion — fetch GitHub, parse atomics, persist idempotently.

This module ingests the official ``redcanaryco/atomic-red-team`` GitHub
repository into the Command_Base (design Component 7, Req. 7.1–7.3, 7.6, 7.7).
It has two collaborators and one injectable boundary:

* :class:`AtomicRepoFetcher` encapsulates the *fetch strategy* — a shallow
  ``git clone`` of the repository or, as a fallback, a download of the GitHub
  tarball. The fetch runs in the application layer, **outside** the contained
  lab (Req. 7.1), and is injectable so tests can double it without any network.
* :class:`AtomicRedTeamIngestService` orchestrates the ingest: it asks the
  fetcher for a local checkout, parses each ``atomics/T####/T####.yaml`` atomic
  (reading ``atomic_tests[].executor.command`` normalized per platform and
  executor), persists the technique→command rows into the relational
  ``technique_commands`` table (the exact half of the Command_Base, Req. 7.2)
  and the command↔tool affinities into the ``experience_memory`` pgvector store
  (the associative half, Req. 7.3).

Idempotency follows the design: each ``technique_commands`` row is keyed by
``UniqueConstraint(technique_id, command_hash, source)`` and inserted only after
a check-before-insert on that key, so re-running the ingest never duplicates a
command for a technique (Req. 7.6). Affinities are deduplicated through the
memory's ``metadata.dedup_key`` (a deterministic hash of the affinity content),
consistent with how the planned ``ExperienceMemory.store_affinity`` of task 12.1
will store them.

Robustness: a single malformed
atomic YAML is isolated (logged and skipped), never aborting the rest of the
ingest. A fetch failure raises :class:`AtomicFetchError` before any write is
attempted, so the already-persisted Command_Base index is preserved unchanged
(Req. 7.7).

Importing this module performs no I/O: the ORM models, settings and the
Experience_Memory are resolved lazily inside the methods, matching the other
Session-injected services (Req. 1.7).
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.ai.memory import ExperienceMemory

logger = logging.getLogger(__name__)

# Provenance recorded on every row/affinity ingested by this service. It is the
# ``source`` component of the idempotency key (technique_id, command_hash,
# source) and the affinity ``metadata.source``.
_SOURCE = "atomic_red_team"

# The official repository ingested as the source of truth (Req. 7.1).
_REPO_OWNER = "redcanaryco"
_REPO_NAME = "atomic-red-team"
_REPO_URL = f"https://github.com/{_REPO_OWNER}/{_REPO_NAME}.git"

# Only directories/files matching the ATT&CK technique id shape carry atomics,
# e.g. ``atomics/T1059/T1059.yaml`` (and sub-technique ids like ``T1059.001``).
_TECHNIQUE_ID_RE = re.compile(r"^T\d{4}(?:\.\d{3})?$")

# Sentinel campaign id under which the campaign-agnostic command affinities are
# stored. ``ExperienceMemory.retrieve`` excludes the *running* campaign from its
# results; a running campaign is never this sentinel, so every stored affinity
# stays eligible for the Command_Base similarity fallback. It mirrors the
# ``_NO_CAMPAIGN`` sentinel in :mod:`app.services.command_base`.
_AFFINITY_CAMPAIGN_ID = "__command_base_no_campaign__"


ProgressCallback = Callable[[int, int], None]


def atomic_affinity_dedup_key(
    technique_id: str, executor: str, platform: str, command: str
) -> str:
    """Deterministic ``metadata.dedup_key`` of one Atomic command affinity."""
    return hashlib.sha256(
        f"{_SOURCE}|{technique_id}|{executor}|{platform}|{command}".encode("utf-8")
    ).hexdigest()


class AtomicFetchError(RuntimeError):
    """Raised when the Atomic Red Team repository cannot be fetched from GitHub.

    Signals Requirement 7.7: the ingest reports the fetch failure and the
    already-persisted Command_Base index is preserved unchanged (no ingest
    transaction is opened). The originating exception is chained as ``__cause__``
    and the attempted ``ref`` is carried for diagnostics.
    """

    def __init__(self, message: str, *, ref: str) -> None:
        super().__init__(message)
        self.ref = ref


@dataclass(frozen=True)
class AtomicIngestSummary:
    """Outcome of an :meth:`AtomicRedTeamIngestService.ingest` run.

    ``techniques`` counts the technique directories parsed; ``commands_parsed``
    the concrete commands extracted; ``commands_inserted`` the new
    ``technique_commands`` rows created (re-ingests leave this at 0 for the
    unchanged commands); ``affinities_inserted`` the new experience-memory
    affinity rows; ``skipped_atomics`` the atomics isolated because their YAML
    was malformed (Req. 7.6, 7.7).
    """

    techniques: int = 0
    commands_parsed: int = 0
    commands_inserted: int = 0
    affinities_inserted: int = 0
    skipped_atomics: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _ParsedCommand:
    """One normalized command extracted from an atomic's ``atomic_tests``."""

    technique_id: str
    command: str
    executor: str
    platform: str


class AtomicRepoFetcher:
    """Fetch strategy for ``redcanaryco/atomic-red-team``, OUTSIDE the lab.

    Resolves a local checkout of the repository and returns the path to its
    ``atomics/`` directory. The default strategy is a shallow ``git clone``;
    when ``git`` is unavailable or the clone fails it falls back to downloading
    and extracting the GitHub tarball for the requested ref. Both strategies run
    in the application layer, never inside the contained lab (Req. 7.1).

    Injectable: :class:`AtomicRedTeamIngestService` accepts a fetcher so tests
    can double it with a local fixture tree and no network access.
    """

    def __init__(self, *, repo_url: str = _REPO_URL, timeout: float = 120.0) -> None:
        self._repo_url = repo_url
        self._timeout = timeout

    def fetch_atomics_dir(self, *, ref: str, dest: Path) -> Path:
        """Return the ``atomics/`` directory of a fresh checkout under ``dest``.

        Tries a shallow ``git clone`` first, then the tarball download. Raises
        :class:`AtomicFetchError` if neither strategy yields an ``atomics/``
        directory, so the caller can preserve the persisted index (Req. 7.7).
        """
        last_exc: Optional[Exception] = None

        try:
            return self._clone(ref=ref, dest=dest)
        except Exception as exc:  # noqa: BLE001 - fall back to the tarball
            last_exc = exc
            logger.warning(
                "Shallow git clone of %s@%s failed (%s: %s); falling back to the "
                "GitHub tarball.",
                self._repo_url,
                ref,
                type(exc).__name__,
                exc,
            )

        try:
            return self._tarball(ref=ref, dest=dest)
        except Exception as exc:  # noqa: BLE001 - both strategies exhausted
            last_exc = exc

        raise AtomicFetchError(
            f"could not fetch {self._repo_url}@{ref}: neither the shallow git "
            f"clone nor the GitHub tarball download succeeded.",
            ref=ref,
        ) from last_exc

    def _clone(self, *, ref: str, dest: Path) -> Path:
        """Shallow-clone the repository at ``ref`` into ``dest`` and locate atomics."""
        checkout = dest / "repo"
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                ref,
                self._repo_url,
                str(checkout),
            ],
            check=True,
            capture_output=True,
            timeout=self._timeout,
        )
        return self._locate_atomics(checkout)

    def _tarball(self, *, ref: str, dest: Path) -> Path:
        """Download and extract the GitHub tarball for ``ref`` and locate atomics."""
        url = (
            f"https://github.com/{_REPO_OWNER}/{_REPO_NAME}/archive/refs/heads/"
            f"{ref}.tar.gz"
        )
        archive = dest / "atomic.tar.gz"
        with urllib.request.urlopen(url, timeout=self._timeout) as response:  # noqa: S310
            archive.write_bytes(response.read())

        extract_root = dest / "extracted"
        extract_root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(extract_root)  # noqa: S202 - trusted first-party source

        # The tarball extracts into a single ``atomic-red-team-<ref>/`` top dir.
        for child in extract_root.iterdir():
            if child.is_dir():
                try:
                    return self._locate_atomics(child)
                except FileNotFoundError:
                    continue
        return self._locate_atomics(extract_root)

    @staticmethod
    def _locate_atomics(checkout: Path) -> Path:
        """Return ``checkout/atomics`` or raise if the layout is unexpected."""
        atomics = checkout / "atomics"
        if not atomics.is_dir():
            raise FileNotFoundError(
                f"no 'atomics/' directory found under the checkout at {checkout}."
            )
        return atomics


class AtomicRedTeamIngestService:
    """Ingest Atomic Red Team into the Command_Base, idempotently.

    Session-injected; the Experience_Memory is injected so the associative
    affinities share the request-scoped unit of work, and the fetcher is
    injectable so the fetch strategy can be doubled in tests. See the module
    docstring for the full contract (Req. 7.1–7.3, 7.6, 7.7).
    """

    def __init__(
        self,
        session: Session,
        *,
        memory: ExperienceMemory,
        fetcher: Optional[AtomicRepoFetcher] = None,
    ) -> None:
        self._session = session
        self._memory = memory
        self._fetcher = fetcher or AtomicRepoFetcher()

    # ---- public API --------------------------------------------------------

    def vectorize_missing(self, progress: Optional[ProgressCallback] = None) -> int:
        """Embed the stored commands that have no affinity yet (no network).

        Used by the ingestion orchestrator when the relational rows are present
        but some affinities are missing. Returns the number of affinities
        inserted. Embedder failures propagate to the caller.
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
        rows = self._session.execute(
            select(TechniqueCommand)
            .where(TechniqueCommand.source == _SOURCE)
            .order_by(TechniqueCommand.id)
        ).scalars().all()

        inserted = 0
        for index, row in enumerate(rows, start=1):
            item = _ParsedCommand(
                technique_id=row.technique_id,
                command=row.command,
                executor=row.executor or "unknown",
                platform=row.platform or "unknown",
            )
            key = atomic_affinity_dedup_key(
                item.technique_id, item.executor, item.platform, item.command
            )
            if key not in existing and self._persist_affinity(item):
                inserted += 1
            if progress is not None and index % 50 == 0:
                progress(index, 0)
        return inserted

    def ingest(
        self, *, ref: str = "master", progress: Optional[ProgressCallback] = None
    ) -> AtomicIngestSummary:
        """Fetch, parse and persist the Atomic Red Team Command_Base index.

        Clones/downloads ``redcanaryco/atomic-red-team`` at ``ref`` OUTSIDE the
        lab, parses every ``atomics/T####/T####.yaml`` atomic, and persists
        technique→commands into ``technique_commands`` (Req. 7.2) plus
        command↔tool affinities into ``experience_memory`` (Req. 7.3).
        Idempotent by ``(technique_id, command_hash, source)`` — re-ingesting
        does not duplicate (Req. 7.6). A fetch failure raises
        :class:`AtomicFetchError` before any write, preserving the persisted
        index (Req. 7.7). A malformed atomic YAML is isolated (log + skip),
        never aborting the rest of the ingest.
        """
        # Fetch BEFORE opening any write so a fetch failure leaves the persisted
        # Command_Base index untouched (Req. 7.7). The checkout lives in a temp
        # directory that is removed when the context exits.
        with tempfile.TemporaryDirectory(prefix="atomic_red_team_") as tmp:
            atomics_dir = self._fetcher.fetch_atomics_dir(ref=ref, dest=Path(tmp))
            return self._ingest_from_dir(atomics_dir, progress)

    # ---- orchestration -----------------------------------------------------

    def _ingest_from_dir(
        self, atomics_dir: Path, progress: Optional[ProgressCallback] = None
    ) -> AtomicIngestSummary:
        """Parse and persist every atomic under ``atomics_dir`` with isolation."""
        techniques = 0
        commands_parsed = 0
        commands_inserted = 0
        affinities_inserted = 0
        skipped = 0
        errors: list[str] = []

        for yaml_path in self._iter_atomic_files(atomics_dir):
            technique_id = yaml_path.stem
            parsed, error = self._parse_atomic(yaml_path, technique_id)
            if error is not None:
                # One malformed atomic is isolated (log + skip), never aborting
                # the rest of the ingest — same pattern as load_all_cases_safe.
                skipped += 1
                errors.append(error)
                logger.warning("Skipping malformed atomic %s: %s", yaml_path, error)
                continue

            techniques += 1
            commands_parsed += len(parsed)
            for item in parsed:
                if self._persist_command(item):
                    commands_inserted += 1
                if self._persist_affinity(item):
                    affinities_inserted += 1
            if progress is not None and techniques % 20 == 0:
                progress(techniques, 0)

        return AtomicIngestSummary(
            techniques=techniques,
            commands_parsed=commands_parsed,
            commands_inserted=commands_inserted,
            affinities_inserted=affinities_inserted,
            skipped_atomics=skipped,
            errors=errors,
        )

    @staticmethod
    def _iter_atomic_files(atomics_dir: Path) -> list[Path]:
        """Yield each ``atomics/T####/T####.yaml`` path, sorted for determinism."""
        files: list[Path] = []
        for technique_dir in sorted(atomics_dir.iterdir()):
            if not technique_dir.is_dir():
                continue
            technique_id = technique_dir.name
            if not _TECHNIQUE_ID_RE.match(technique_id):
                continue
            atomic = technique_dir / f"{technique_id}.yaml"
            if atomic.is_file():
                files.append(atomic)
        return files

    # ---- parsing -----------------------------------------------------------

    def _parse_atomic(
        self, yaml_path: Path, technique_id: str
    ) -> tuple[list[_ParsedCommand], Optional[str]]:
        """Parse one atomic YAML into normalized commands, isolating failures.

        Returns ``(commands, None)`` on success or ``([], error_message)`` when
        the YAML is malformed/unreadable so the caller can log-and-skip this
        atomic without aborting the ingest (Req. 7.6 isolation).
        """
        # Imported lazily so importing this module never requires PyYAML and the
        # dependency surface stays minimal (Req. 1.7).
        import yaml

        try:
            raw = yaml_path.read_text(encoding="utf-8")
            document = yaml.safe_load(raw)
        except (OSError, yaml.YAMLError) as exc:
            return [], f"{type(exc).__name__}: {exc}"

        if not isinstance(document, dict):
            return [], "atomic document is not a YAML mapping."

        # Prefer the technique id declared in the document; fall back to the file
        # stem so a mismatched/absent ``attack_technique`` never drops the atomic.
        declared = document.get("attack_technique")
        resolved_technique = declared if isinstance(declared, str) and declared.strip() else technique_id

        atomic_tests = document.get("atomic_tests")
        if not isinstance(atomic_tests, list):
            return [], "atomic document has no 'atomic_tests' list."

        commands = self._extract_commands(resolved_technique, atomic_tests)
        return commands, None

    @classmethod
    def _extract_commands(
        cls, technique_id: str, atomic_tests: list[Any]
    ) -> list[_ParsedCommand]:
        """Extract ``atomic_tests[].executor.command`` normalized per platform.

        Each atomic test carries one ``executor`` (with ``name`` and
        ``command``) and a ``supported_platforms`` list. One
        :class:`_ParsedCommand` is produced per supported platform so the
        relational mapping records the command for each platform it targets
        (Req. 7.2). Tests without a runnable command are skipped.
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
                # A test without an executable command (e.g. ``manual`` executor)
                # contributes no relational command mapping.
                continue

            executor_name = cls._normalize_token(executor.get("name")) or "unknown"
            platforms = test.get("supported_platforms")
            if not isinstance(platforms, list) or not platforms:
                platforms = ["unknown"]

            normalized_command = cls._normalize_command(command)
            for platform in platforms:
                platform_token = cls._normalize_token(platform) or "unknown"
                commands.append(
                    _ParsedCommand(
                        technique_id=technique_id,
                        command=normalized_command,
                        executor=executor_name,
                        platform=platform_token,
                    )
                )
        return commands

    @staticmethod
    def _normalize_command(command: str) -> str:
        """Normalize a command string for a stable idempotency hash.

        Strips surrounding whitespace and normalizes line endings so trivially
        different encodings of the same command collapse to one row.
        """
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

    def _persist_command(self, item: _ParsedCommand) -> bool:
        """Insert a ``technique_commands`` row if absent; return True if inserted.

        Idempotent via a check-before-insert on the
        ``(technique_id, command_hash, source)`` key, matching the table's
        :class:`UniqueConstraint` so a re-ingest reuses the existing row rather
        than duplicating it (Req. 7.6).
        """
        from app.models.domain import TechniqueCommand

        command_hash = self._command_hash(item.command)
        existing = self._session.execute(
            select(TechniqueCommand.id).where(
                TechniqueCommand.technique_id == item.technique_id,
                TechniqueCommand.command_hash == command_hash,
                TechniqueCommand.source == _SOURCE,
            )
        ).first()
        if existing is not None:
            return False

        self._session.add(
            TechniqueCommand(
                technique_id=item.technique_id,
                command=item.command,
                executor=item.executor,
                platform=item.platform,
                source=_SOURCE,
                command_hash=command_hash,
            )
        )
        self._session.flush()
        return True

    # ---- vector persistence (associative half) -----------------------------

    def _persist_affinity(self, item: _ParsedCommand) -> bool:
        """Store a command↔tool affinity in ``experience_memory`` if absent.

        The affinity is associative knowledge, so it lives in the pgvector store
        per the Memory_Rule (Req. 7.3). It is deduplicated by a deterministic
        ``metadata.dedup_key`` (hash of the affinity content) checked before
        insert, consistent with the planned ``store_affinity`` of task 12.1
        (``metadata.kind = "command_affinity"``). Returns True if a new affinity
        row was inserted.
        """
        from app.models.domain import ExperienceMemory as ExperienceMemoryRow

        content = (
            f"technique {item.technique_id} executor {item.executor} "
            f"platform {item.platform}: {item.command}"
        )
        dedup_key = atomic_affinity_dedup_key(
            item.technique_id, item.executor, item.platform, item.command
        )

        if self._affinity_exists(dedup_key):
            return False

        self._ensure_affinity_campaign()

        from app.models.enums import AgentStage

        embedding = self._memory._resolve_embedder().embed(content)  # noqa: SLF001
        self._session.add(
            ExperienceMemoryRow(
                campaign_id=_AFFINITY_CAMPAIGN_ID,
                stage=AgentStage.OPERATIONALIZATION,
                # The outcome column is bounded to ``success | failure`` by the
                # table CHECK; an ingested affinity records a successful mapping.
                outcome="success",
                content=content,
                embedding=embedding,
                meta={
                    "kind": "command_affinity",
                    "dedup_key": dedup_key,
                    "source": _SOURCE,
                    "technique_id": item.technique_id,
                    "command": item.command,
                    "executor": item.executor,
                    "platform": item.platform,
                },
            )
        )
        self._session.flush()
        return True

    def _affinity_exists(self, dedup_key: str) -> bool:
        """True if a ``command_affinity`` row with ``dedup_key`` is already stored.

        Uses SQLAlchemy's portable JSON element access
        (``meta["dedup_key"].as_string()``), which renders to the right operator
        on both PostgreSQL (the product database) and the SQLite used by the
        unit tests, so the dedup check is dialect-agnostic.
        """
        from app.models.domain import ExperienceMemory as ExperienceMemoryRow

        row = self._session.execute(
            select(ExperienceMemoryRow.id).where(
                ExperienceMemoryRow.meta["dedup_key"].as_string() == dedup_key
            )
        ).first()
        return row is not None

    def _ensure_affinity_campaign(self) -> None:
        """Ensure the sentinel campaign row exists to satisfy the FK.

        Command affinities are campaign-agnostic, but ``experience_memory``
        carries a non-null FK to ``campaigns.id``. A single sentinel campaign row
        anchors every affinity; retrieval excludes the *running* campaign, which
        is never this sentinel, so the affinities stay eligible for the
        similarity fallback.
        """
        from app.models.domain import Case as CampaignRow

        existing = self._session.get(CampaignRow, _AFFINITY_CAMPAIGN_ID)
        if existing is not None:
            return
        self._session.add(
            CampaignRow(
                id=_AFFINITY_CAMPAIGN_ID,
                name="Command_Base affinities (synthetic anchor)",
            )
        )
        self._session.flush()
