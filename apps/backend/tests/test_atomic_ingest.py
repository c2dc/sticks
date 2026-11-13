"""Deterministic tests for the Atomic Red Team ingestion (task 40.3).

No network, no Hypothesis: the fetcher is either an injected double or the real
:class:`AtomicRepoFetcher` driven by a fake HTTP transport that serves a
synthetic ``.tar.gz`` built in memory. The database is a temporary SQLite file
and the embedder is a controllable fake.

Covered: tarball member selection, retries and ``User-Agent``; parser decisions
(``manual`` executors, multi-line commands, several platforms, literal
``#{argument}``, malformed YAML isolated in ``skipped``); the two persistence
phases and their resumption; the real regression (a run interrupted after one
row must never end ``ready`` with one row); plausibility; counts; the empty plan
without network; ``INGEST_WORK_DIR``; idempotency; ``progress``/heartbeat.

**Validates: Requirements 7.1, 7.3, 7.5, 7.6, 7.7, 7.10**
"""

from __future__ import annotations

import datetime as dt
import io
import itertools
import logging
import tarfile
import threading
import time
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import ExperienceMemory as ExperienceMemoryRow
from app.models.domain import IngestSourceState, TechniqueCommand
from app.models.enums import IngestSource, IngestStatus
from app.services.ai.embedder import EMBEDDER_UNAVAILABLE, EmbedderError
from app.services.ai.memory import ExperienceMemory
from app.services.atomic_red_team_ingest_service import (
    USER_AGENT,
    AtomicDocument,
    AtomicFetchError,
    AtomicRedTeamIngestService,
    AtomicRepoFetcher,
    ImplausibleAtomicCountError,
)
from app.services.ingestion.orchestrator import IngestionOrchestrator
from app.services.ingestion.runners import AtomicSourceRunner

TOP = "atomic-red-team-master"


# ---------------------------------------------------------------------------
# Synthetic repository
# ---------------------------------------------------------------------------


def build_tarball(members: dict[str, bytes | None]) -> bytes:
    """A gzip tarball in memory; a ``None`` value is a directory entry."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
                continue
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def atomic_yaml(technique: str, tests: list[dict[str, Any]]) -> bytes:
    return yaml.safe_dump(
        {"attack_technique": technique, "atomic_tests": tests}, sort_keys=False
    ).encode("utf-8")


def run_test(name: str, platforms: list[str], executor: str, command: str) -> dict[str, Any]:
    return {
        "name": name,
        "supported_platforms": platforms,
        "executor": {"name": executor, "command": command},
    }


def repo_members() -> dict[str, bytes | None]:
    """Several atomics plus noise the fetcher must ignore.

    Techniques parsed: 4 (T1003.001, T1033, T1059.003, T1082); T1999 is malformed.
    Command entries: 1 + 3 + 1 + 1 = 6; unique (technique, command): 5, because
    ``whoami`` is listed for two platforms.
    """
    return {
        f"{TOP}/": None,
        f"{TOP}/atomics/": None,
        f"{TOP}/README.md": b"# readme",
        f"{TOP}/atomics/Indexes/index.yaml": b"T1033:\n  x: 1\n",
        f"{TOP}/atomics/Indexes/Indexes-CSV/windows-index.csv": b"a,b",
        f"{TOP}/atomics/T1003.001/T1003.001.yaml": atomic_yaml(
            "T1003.001",
            [
                run_test(
                    "multi-line",
                    ["windows"],
                    "powershell",
                    "Write-Host a\r\nWrite-Host b\r\n",
                )
            ],
        ),
        f"{TOP}/atomics/T1003.001/T1003.001.md": b"docs",
        f"{TOP}/atomics/T1003.001/src/helper.yaml": b"not: an atomic",
        f"{TOP}/atomics/T1033/T1033.yaml": atomic_yaml(
            "T1033",
            [
                run_test("two platforms", ["linux", "macos"], "sh", "whoami"),
                run_test("bash one", ["linux"], "bash", "id -un"),
            ],
        ),
        f"{TOP}/atomics/T1059.003/T1059.003.yaml": atomic_yaml(
            "T1059.003", [run_test("cmd", ["windows"], "command_prompt", "echo hi")]
        ),
        f"{TOP}/atomics/T1082/T1082.yaml": atomic_yaml(
            "T1082",
            [
                {
                    "name": "manual only",
                    "supported_platforms": ["linux"],
                    "executor": {"name": "manual", "steps": "Open the thing by hand."},
                },
                run_test("literal arg", ["linux"], "sh", "cat #{input_file}"),
            ],
        ),
        # Malformed YAML: isolated in ``skipped``, never aborts the rest.
        f"{TOP}/atomics/T1999/T1999.yaml": b"attack_technique: T1999\natomic_tests: [oops\n  - : :\n",
        # Names that look similar but are not technique files.
        f"{TOP}/atomics/T1003.001/T1003.002.yaml": atomic_yaml("T1003.002", []),
        f"{TOP}/atomics/T10031/T10031.yaml": atomic_yaml("T10031", []),
        f"{TOP}/docs/T1033/T1033.yaml": atomic_yaml("T1033", []),
        "other-root-file.yaml": b"a: 1",
    }


EXPECTED_TECHNIQUES = {"T1003.001", "T1033", "T1059.003", "T1082", "T1999"}
EXPECTED_UNIQUE = 5
EXPECTED_PARSED = 6


def many_members(count: int, poison_index: int | None = None) -> dict[str, bytes | None]:
    """``count`` techniques with one command each (``echo <i>``)."""
    members: dict[str, bytes | None] = {}
    for index in range(count):
        technique = f"T{1000 + index:04d}"
        text = "echo POISON" if index == poison_index else f"echo {index}"
        members[f"{TOP}/atomics/{technique}/{technique}.yaml"] = atomic_yaml(
            technique, [run_test("t", ["linux"], "sh", text)]
        )
    return members


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeTransport:
    """Fake ``urlopen``: serves queued bodies or raises queued exceptions."""

    def __init__(self, *responses: bytes | BaseException) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float | None = None) -> io.BytesIO:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, BaseException):
            raise item
        return io.BytesIO(item)


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, "x", {}, io.BytesIO(b""))  # type: ignore[arg-type] -- test double stands in for the real collaborator


class FakeEmbedder:
    """Deterministic embedder with failure switches; records batch sizes."""

    def __init__(self) -> None:
        self.fail = False
        self.poison: str | None = None
        self.batches: list[int] = []
        self.singles = 0

    def _error(self) -> EmbedderError:
        return EmbedderError(EMBEDDER_UNAVAILABLE, "embedder is down.")

    def _vector(self, text: str) -> list[float]:
        value = float(len(text) % 7)
        return [value, value, value]

    def embed(self, text: str) -> list[float]:
        self.singles += 1
        if self.fail or (self.poison and self.poison in text):
            raise self._error()
        return self._vector(text)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(len(texts))
        if self.fail or (self.poison and any(self.poison in t for t in texts)):
            raise self._error()
        return [self._vector(t) for t in texts]


class StaticFetcher:
    """Injectable fetcher returning fixed documents; counts the calls."""

    def __init__(self, documents: list[AtomicDocument]) -> None:
        self.documents = documents
        self.calls = 0

    def fetch_documents(self, *, ref: str) -> list[AtomicDocument]:
        self.calls += 1
        return list(self.documents)


def documents_of(members: dict[str, bytes | None]) -> list[AtomicDocument]:
    """Run the real fetcher over an in-memory tarball (no network)."""
    transport = FakeTransport(build_tarball(members))
    return AtomicRepoFetcher(opener=transport, sleep=lambda _s: None).fetch_documents(ref="master")


# ---------------------------------------------------------------------------
# Database harness
# ---------------------------------------------------------------------------


class Db:
    def __init__(self, tmp_path: Path) -> None:
        self.engine = create_db_engine(
            Settings(database_url=f"sqlite:///{(tmp_path / 'atomic.db').as_posix()}")
        )
        import_models()
        Base.metadata.create_all(bind=self.engine)
        self.factory: sessionmaker[Session] = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False, future=True
        )

    def count(self, model: Any) -> int:
        with self.factory() as session:
            return int(session.execute(select(func.count()).select_from(model)).scalar_one())

    def atomic_rows(self) -> list[TechniqueCommand]:
        with self.factory() as session:
            return list(
                session.execute(
                    select(TechniqueCommand).where(TechniqueCommand.source == "atomic_red_team")
                ).scalars()
            )

    def affinities(self) -> int:
        with self.factory() as session:
            rows = session.execute(select(ExperienceMemoryRow.meta)).scalars().all()
        return sum(1 for meta in rows if meta.get("source") == "atomic_red_team")

    def state_row(self) -> IngestSourceState:
        with self.factory() as session:
            return session.get(IngestSourceState, IngestSource.ATOMIC)

    def set_state(self, **values: Any) -> None:
        with self.factory() as session:
            row = session.get(IngestSourceState, IngestSource.ATOMIC)
            if row is None:
                row = IngestSourceState(source=IngestSource.ATOMIC, status=IngestStatus.PENDING)
                session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            session.commit()


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[Db]:
    database = Db(tmp_path)
    try:
        yield database
    finally:
        database.engine.dispose()


def make_orchestrator(
    db: Db, fetcher: Any, embedder: Any, *, heartbeat_interval: float = 20.0
) -> IngestionOrchestrator:
    return IngestionOrchestrator(
        db.factory,
        {IngestSource.ATOMIC: AtomicSourceRunner(embedder=embedder, fetcher=fetcher)},
        heartbeat_interval=heartbeat_interval,
    )


def status_of(orchestrator: IngestionOrchestrator) -> Any:
    return next(s for s in orchestrator.status() if s.source == IngestSource.ATOMIC)


def make_service(
    db: Db, session: Session, fetcher: Any, embedder: Any
) -> AtomicRedTeamIngestService:
    return AtomicRedTeamIngestService(
        session, memory=ExperienceMemory(session, embedder=embedder), fetcher=fetcher
    )


# ---------------------------------------------------------------------------
# (a) AtomicRepoFetcher over a synthetic tarball and a fake transport
# ---------------------------------------------------------------------------


def test_fetcher_reads_only_technique_yaml_members() -> None:
    transport = FakeTransport(build_tarball(repo_members()))
    fetcher = AtomicRepoFetcher(opener=transport, sleep=lambda _s: None)

    documents = fetcher.fetch_documents(ref="master")

    assert {d.name for d in documents} == EXPECTED_TECHNIQUES
    assert all(d.error is None and d.data for d in documents)
    assert len(transport.requests) == 1


def test_fetcher_sends_identifiable_user_agent_and_codeload_url() -> None:
    transport = FakeTransport(build_tarball(repo_members()))
    AtomicRepoFetcher(opener=transport, sleep=lambda _s: None).fetch_documents(ref="master")

    request = transport.requests[0]
    assert request.full_url == (
        "https://codeload.github.com/redcanaryco/atomic-red-team/tar.gz/refs/heads/master"
    )
    assert request.get_header("User-agent") == USER_AGENT
    assert USER_AGENT.startswith("sticks-ingest/1.0")


def test_fetcher_retries_5xx_and_429_with_backoff_then_succeeds() -> None:
    sleeps: list[float] = []
    transport = FakeTransport(http_error(503), http_error(429), build_tarball(repo_members()))
    fetcher = AtomicRepoFetcher(opener=transport, sleep=sleeps.append)

    documents = fetcher.fetch_documents(ref="master")

    assert len(documents) == len(EXPECTED_TECHNIQUES)
    assert len(transport.requests) == 3
    assert sleeps == [1.0, 2.0]


def test_fetcher_retries_a_corrupt_archive() -> None:
    good = build_tarball(repo_members())
    transport = FakeTransport(b"<html>not a tarball</html>", good)
    fetcher = AtomicRepoFetcher(opener=transport, sleep=lambda _s: None)

    assert {d.name for d in fetcher.fetch_documents(ref="master")} == EXPECTED_TECHNIQUES
    assert len(transport.requests) == 2


def test_fetcher_does_not_retry_404_and_gives_up_after_retries() -> None:
    not_found = FakeTransport(http_error(404))
    with pytest.raises(AtomicFetchError) as info:
        AtomicRepoFetcher(opener=not_found, sleep=lambda _s: None).fetch_documents(ref="nope")
    assert len(not_found.requests) == 1
    assert info.value.ref == "nope"

    broken = FakeTransport(http_error(500))
    with pytest.raises(AtomicFetchError):
        AtomicRepoFetcher(opener=broken, retries=2, sleep=lambda _s: None).fetch_documents(
            ref="master"
        )
    assert len(broken.requests) == 3


class _SizedResponse(io.BytesIO):
    """BytesIO exposing a (possibly lying) ``Content-Length`` header."""

    def __init__(self, body: bytes, content_length: int | None) -> None:
        super().__init__(body)
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}


class _SizedTransport:
    """Fake ``urlopen`` serving ``(body, content_length)`` pairs in order."""

    def __init__(self, *responses: tuple[bytes, int | None]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, request: Any, timeout: float | None = None) -> _SizedResponse:
        self.calls += 1
        body, length = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return _SizedResponse(body, length)


def _truncation_cases() -> dict[str, tuple[bytes, int | None]]:
    full = build_tarball(repo_members())
    return {
        "half": (full[: len(full) // 2], None),
        "before_gzip_trailer": (full[:-8], None),
        "content_length_larger": (full, len(full) + 100),
    }


@pytest.mark.parametrize("case", ["half", "before_gzip_trailer", "content_length_larger"])
def test_truncated_download_is_retried_then_raises_and_never_returns_partial(case: str) -> None:
    transport = _SizedTransport(_truncation_cases()[case])
    sleeps: list[float] = []
    fetcher = AtomicRepoFetcher(opener=transport, retries=2, sleep=sleeps.append)

    with pytest.raises(AtomicFetchError) as info:
        fetcher.fetch_documents(ref="master")

    assert transport.calls == 3
    assert sleeps == [1.0, 2.0]
    assert info.value.ref == "master"


@pytest.mark.parametrize("case", ["half", "before_gzip_trailer", "content_length_larger"])
def test_retry_after_truncation_succeeds_with_intact_tarball(case: str) -> None:
    full = build_tarball(repo_members())
    transport = _SizedTransport(_truncation_cases()[case], (full, len(full)))
    fetcher = AtomicRepoFetcher(opener=transport, sleep=lambda _s: None)

    documents = fetcher.fetch_documents(ref="master")

    assert transport.calls == 2
    assert {d.name for d in documents} == EXPECTED_TECHNIQUES


def test_intact_tarball_with_matching_content_length_passes() -> None:
    full = build_tarball(repo_members())
    transport = _SizedTransport((full, len(full)))

    documents = AtomicRepoFetcher(opener=transport, sleep=lambda _s: None).fetch_documents(
        ref="master"
    )

    assert transport.calls == 1
    assert {d.name for d in documents} == EXPECTED_TECHNIQUES


def test_fetcher_reports_oversized_member_without_reading_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.atomic_red_team_ingest_service as module

    monkeypatch.setattr(module, "_MAX_DOCUMENT_BYTES", 10)
    documents = documents_of({f"{TOP}/atomics/T1033/T1033.yaml": b"x" * 50})

    assert documents == [AtomicDocument("T1033", error="file_too_large")]


# ---------------------------------------------------------------------------
# (a)/(e) Parsing decisions through the whole ingest
# ---------------------------------------------------------------------------


def ingest_main_repo(db: Db, embedder: FakeEmbedder | None = None) -> Any:
    with db.factory() as session:
        service = make_service(
            db, session, StaticFetcher(documents_of(repo_members())), embedder or FakeEmbedder()
        )
        summary = service.ingest(ref="master")
        session.commit()
    return summary


def test_parser_decisions_and_counts(db: Db) -> None:
    summary = ingest_main_repo(db)

    assert summary.techniques == 4
    assert summary.commands_parsed == EXPECTED_PARSED
    assert summary.expected_count == EXPECTED_UNIQUE
    assert summary.commands_inserted == EXPECTED_UNIQUE
    assert summary.affinities_inserted == EXPECTED_UNIQUE
    assert summary.errors == []
    # Malformed YAML is isolated, with a reason, and does not stop the others.
    assert summary.skipped_atomics == 1
    assert [s["item"] for s in summary.skipped] == ["T1999"]
    assert summary.skipped[0]["reason"]

    rows = {(r.technique_id, r.command): r for r in db.atomic_rows()}
    assert len(rows) == EXPECTED_UNIQUE
    assert {r.executor for r in rows.values()} == {"powershell", "sh", "bash", "command_prompt"}
    # Multi-line command keeps its lines, normalized to LF and stripped.
    assert ("T1003.001", "Write-Host a\nWrite-Host b") in rows
    # Several platforms: one row per unique command, first platform wins.
    assert rows[("T1033", "whoami")].platform == "linux"
    assert rows[("T1033", "id -un")].executor == "bash"
    # ``#{argument}`` stays literal.
    assert ("T1082", "cat #{input_file}") in rows
    # The ``manual`` executor without ``command`` is discarded.
    assert [r for r in rows if r[0] == "T1082"] == [("T1082", "cat #{input_file}")]
    assert not any(r[0] == "T1999" for r in rows)
    assert db.affinities() == EXPECTED_UNIQUE


def test_document_without_atomic_tests_or_mapping_is_skipped(db: Db) -> None:
    documents = [
        *documents_of(repo_members()),
        AtomicDocument("T1500", b"- a\n- b\n"),
        AtomicDocument("T1501", b"attack_technique: T1501\n"),
        AtomicDocument("T1502", error="unreadable_member"),
    ]
    with db.factory() as session:
        summary = make_service(db, session, StaticFetcher(documents), FakeEmbedder()).ingest()
        session.commit()

    assert {s["item"] for s in summary.skipped} == {"T1999", "T1500", "T1501", "T1502"}
    assert summary.techniques == 4


# ---------------------------------------------------------------------------
# (h) Two runs do not duplicate
# ---------------------------------------------------------------------------


def test_two_ingests_do_not_duplicate(db: Db) -> None:
    ingest_main_repo(db)
    first = (db.count(TechniqueCommand), db.count(ExperienceMemoryRow))

    second = ingest_main_repo(db)

    assert (db.count(TechniqueCommand), db.count(ExperienceMemoryRow)) == first
    assert second.commands_inserted == 0
    assert second.affinities_inserted == 0
    assert second.expected_count == EXPECTED_UNIQUE


# ---------------------------------------------------------------------------
# (b) Two phases: relational first, vectors resumable, batch fallback
# ---------------------------------------------------------------------------


def test_relational_phase_is_complete_even_if_embedder_fails(db: Db) -> None:
    embedder = FakeEmbedder()
    embedder.fail = True

    summary = ingest_main_repo(db, embedder)

    assert db.count(TechniqueCommand) == EXPECTED_UNIQUE
    assert summary.commands_inserted == EXPECTED_UNIQUE
    assert summary.affinities_inserted == 0
    assert len(summary.errors) == EXPECTED_UNIQUE
    assert db.affinities() == 0

    # Resuming vectorizes exactly what is missing, without a fetcher.
    embedder.fail = False
    with db.factory() as session:
        service = make_service(db, session, None, embedder)
        inserted, errors = service.vectorize_missing()
        session.commit()
    assert (inserted, errors) == (EXPECTED_UNIQUE, [])
    assert db.affinities() == EXPECTED_UNIQUE


def test_embedder_failure_becomes_failed_then_rerun_without_network_is_ready(db: Db) -> None:
    fetcher = StaticFetcher(documents_of(repo_members()))
    embedder = FakeEmbedder()
    embedder.fail = True
    orchestrator = make_orchestrator(db, fetcher, embedder)

    orchestrator.run_source(IngestSource.ATOMIC)

    state = status_of(orchestrator)
    assert state.status == IngestStatus.FAILED
    assert state.error == EMBEDDER_UNAVAILABLE
    assert (state.loaded_count, state.embedded_count) == (EXPECTED_UNIQUE, 0)
    assert state.expected_count == EXPECTED_UNIQUE
    assert fetcher.calls == 1

    embedder.fail = False
    orchestrator.run_source(IngestSource.ATOMIC)

    state = status_of(orchestrator)
    assert state.status == IngestStatus.READY
    assert state.error is None
    assert state.loaded_count == state.embedded_count == EXPECTED_UNIQUE
    assert fetcher.calls == 1  # the second run used no network
    assert db.count(TechniqueCommand) == EXPECTED_UNIQUE
    assert db.affinities() == EXPECTED_UNIQUE


def test_embedding_batches_are_32_wide(db: Db) -> None:
    embedder = FakeEmbedder()
    with db.factory() as session:
        service = make_service(db, session, StaticFetcher(documents_of(many_members(40))), embedder)
        summary = service.ingest()
        session.commit()

    assert embedder.batches == [32, 8]
    assert summary.affinities_inserted == 40


def test_failed_batch_is_redone_entry_by_entry(db: Db) -> None:
    embedder = FakeEmbedder()
    embedder.poison = "POISON"
    documents = documents_of(many_members(40, poison_index=3))
    with db.factory() as session:
        summary = make_service(db, session, StaticFetcher(documents), embedder).ingest()
        session.commit()

    assert db.count(TechniqueCommand) == 40
    assert embedder.batches == [32, 8]  # the first batch failed as a whole
    assert embedder.singles == 32  # ... and was redone one entry at a time
    assert summary.affinities_inserted == 39
    assert summary.errors == [f"T1003: {EMBEDDER_UNAVAILABLE}"]
    assert db.affinities() == 39

    embedder.poison = None
    with db.factory() as session:
        inserted, errors = make_service(db, session, None, embedder).vectorize_missing()
        session.commit()
    assert (inserted, errors) == (1, [])
    assert db.affinities() == 40


# ---------------------------------------------------------------------------
# (c) The real regression: interrupted after one row must not become ready
# ---------------------------------------------------------------------------


def seed_one_row(db: Db) -> None:
    from app.services.atomic_red_team_ingest_service import AtomicRedTeamIngestService as S

    with db.factory() as session:
        session.add(
            TechniqueCommand(
                technique_id="T1033",
                command="whoami",
                executor="sh",
                platform="linux",
                source="atomic_red_team",
                command_hash=S._command_hash("whoami"),
                input_arguments={},
            )
        )
        session.commit()


@pytest.mark.parametrize("expected", [None, 3])
def test_plan_redownloads_when_one_row_and_no_complete_state(db: Db, expected: int | None) -> None:
    seed_one_row(db)
    db.set_state(status=IngestStatus.FAILED, error=EMBEDDER_UNAVAILABLE, expected_count=expected)
    runner = AtomicSourceRunner(embedder=FakeEmbedder(), fetcher=StaticFetcher([]))

    with db.factory() as session:
        plan = runner.plan(session)

    assert plan.needs_fetch is True
    assert plan.loaded_count == 1
    assert plan.is_empty is False


def test_plan_only_vectorizes_when_relational_part_is_known_complete(db: Db) -> None:
    seed_one_row(db)
    db.set_state(status=IngestStatus.FAILED, error=EMBEDDER_UNAVAILABLE, expected_count=1)
    runner = AtomicSourceRunner(embedder=FakeEmbedder(), fetcher=StaticFetcher([]))

    with db.factory() as session:
        plan = runner.plan(session)

    assert plan.needs_fetch is False
    assert plan.missing_embed >= 1


def test_plan_redownloads_after_failure_for_a_non_embedder_cause(db: Db) -> None:
    ingest_main_repo(db)
    db.set_state(status=IngestStatus.FAILED, error="network: AtomicFetchError", expected_count=5)
    runner = AtomicSourceRunner(embedder=FakeEmbedder(), fetcher=StaticFetcher([]))

    with db.factory() as session:
        assert runner.plan(session).needs_fetch is True


def test_plan_redownloads_when_techniques_are_below_the_floor(
    db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest_main_repo(db)
    db.set_state(status=IngestStatus.READY, error=None, expected_count=EXPECTED_UNIQUE)
    runner = AtomicSourceRunner(embedder=FakeEmbedder(), fetcher=StaticFetcher([]))
    with db.factory() as session:
        assert runner.plan(session).is_empty is True

    monkeypatch.setenv("ATOMIC_MIN_TECHNIQUES", "100")
    with db.factory() as session:
        assert runner.plan(session).needs_fetch is True


def test_reingest_backfills_null_input_arguments_without_duplicating(db: Db) -> None:
    """Rows ingested before the column existed are refilled in place (Req. 11.7)."""
    fetcher = StaticFetcher(documents_of(repo_members()))
    orchestrator = make_orchestrator(db, fetcher, FakeEmbedder())
    orchestrator.run_source(IngestSource.ATOMIC)
    runner = orchestrator.runner(IngestSource.ATOMIC)
    with db.factory() as session:
        assert all(r.input_arguments is not None for r in session.query(TechniqueCommand))
        assert runner.plan(session).is_empty is True

    with db.factory() as session:
        for row in session.query(TechniqueCommand):
            row.input_arguments = None
        session.commit()
    embeddings = db.count(ExperienceMemoryRow)
    with db.factory() as session:
        assert runner.plan(session).needs_fetch is True

    orchestrator.run_source(IngestSource.ATOMIC)

    assert db.count(TechniqueCommand) == EXPECTED_UNIQUE
    assert db.count(ExperienceMemoryRow) == embeddings
    with db.factory() as session:
        assert all(r.input_arguments is not None for r in session.query(TechniqueCommand))
        assert runner.plan(session).is_empty is True


def test_interrupted_run_with_one_row_is_never_ready_and_the_next_run_downloads_again(
    db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The embedder fails right after the first row is written (real regression)."""
    fetcher = StaticFetcher(documents_of(repo_members()))
    embedder = FakeEmbedder()
    orchestrator = make_orchestrator(db, fetcher, embedder)
    original = AtomicRedTeamIngestService._persist_commands

    def one_row_then_embedder_error(self: Any, commands: Any, progress: Any = None) -> int:
        original(self, list(commands)[:1], progress)
        raise EmbedderError(EMBEDDER_UNAVAILABLE, "failed on the first call.")

    with monkeypatch.context() as patch:
        patch.setattr(AtomicRedTeamIngestService, "_persist_commands", one_row_then_embedder_error)
        orchestrator.run_source(IngestSource.ATOMIC)

    state = status_of(orchestrator)
    assert state.status == IngestStatus.FAILED
    assert state.error == EMBEDDER_UNAVAILABLE
    assert db.count(TechniqueCommand) == 1
    assert state.expected_count is None  # no complete relational ingestion recorded
    assert fetcher.calls == 1

    # Verification must not call it complete: the plan reprocesses (downloads).
    runner = orchestrator.runner(IngestSource.ATOMIC)
    with db.factory() as session:
        plan = runner.plan(session)
    assert plan.needs_fetch is True

    orchestrator.run_source(IngestSource.ATOMIC)

    state = status_of(orchestrator)
    assert fetcher.calls == 2
    assert state.status == IngestStatus.READY
    assert db.count(TechniqueCommand) == EXPECTED_UNIQUE
    assert state.loaded_count == state.embedded_count == state.expected_count == EXPECTED_UNIQUE


def test_failed_state_with_one_stored_row_is_not_marked_ready_by_verify(db: Db) -> None:
    seed_one_row(db)
    db.set_state(status=IngestStatus.FAILED, error=EMBEDDER_UNAVAILABLE, expected_count=None)
    fetcher = StaticFetcher(documents_of(repo_members()))
    orchestrator = make_orchestrator(db, fetcher, FakeEmbedder())

    orchestrator.verify_all()

    assert status_of(orchestrator).status != IngestStatus.READY
    orchestrator.run_source(IngestSource.ATOMIC)
    assert fetcher.calls == 1
    assert db.count(TechniqueCommand) == EXPECTED_UNIQUE
    assert status_of(orchestrator).status == IngestStatus.READY


# ---------------------------------------------------------------------------
# (d) Plausibility
# ---------------------------------------------------------------------------


def test_zero_techniques_fail_as_implausible_and_write_nothing(db: Db) -> None:
    only_noise = {k: v for k, v in repo_members().items() if "/atomics/T" not in k}
    fetcher = StaticFetcher(documents_of(only_noise))
    assert fetcher.documents == []
    orchestrator = make_orchestrator(db, fetcher, FakeEmbedder())

    orchestrator.run_source(IngestSource.ATOMIC)

    state = status_of(orchestrator)
    assert state.status == IngestStatus.FAILED
    assert state.error == "implausible_atomic_count"
    assert db.count(TechniqueCommand) == 0
    assert db.count(ExperienceMemoryRow) == 0


def test_below_floor_fails_and_nothing_is_written(db: Db, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATOMIC_MIN_TECHNIQUES", "5")
    fetcher = StaticFetcher(documents_of(repo_members()))  # 4 parsed techniques
    orchestrator = make_orchestrator(db, fetcher, FakeEmbedder())

    orchestrator.run_source(IngestSource.ATOMIC)

    state = status_of(orchestrator)
    assert (state.status, state.error) == (IngestStatus.FAILED, "implausible_atomic_count")
    assert db.count(TechniqueCommand) == 0
    assert db.count(ExperienceMemoryRow) == 0

    with db.factory() as session, pytest.raises(ImplausibleAtomicCountError) as info:
        make_service(db, session, fetcher, FakeEmbedder()).ingest()
    assert (info.value.techniques, info.value.minimum) == (4, 5)
    assert info.value.code == "implausible_atomic_count"


def test_floor_equal_to_count_passes_and_zero_floor_accepts_small_fakes(
    db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATOMIC_MIN_TECHNIQUES", "4")
    orchestrator = make_orchestrator(
        db, StaticFetcher(documents_of(repo_members())), FakeEmbedder()
    )
    orchestrator.run_source(IngestSource.ATOMIC)
    assert status_of(orchestrator).status == IngestStatus.READY

    monkeypatch.setenv("ATOMIC_MIN_TECHNIQUES", "0")
    with db.factory() as session:
        summary = make_service(
            db, session, StaticFetcher(documents_of(many_members(2))), FakeEmbedder()
        ).ingest()
    assert summary.techniques == 2


# ---------------------------------------------------------------------------
# (e)/(f) Counts and the empty plan without network
# ---------------------------------------------------------------------------


def test_ready_state_counts_and_empty_plan_uses_no_network(db: Db) -> None:
    transport = FakeTransport(build_tarball(repo_members()))
    fetcher = AtomicRepoFetcher(opener=transport, sleep=lambda _s: None)
    orchestrator = make_orchestrator(db, fetcher, FakeEmbedder())

    orchestrator.run_source(IngestSource.ATOMIC)

    state = status_of(orchestrator)
    assert state.status == IngestStatus.READY
    assert state.expected_count == EXPECTED_UNIQUE
    assert state.loaded_count == EXPECTED_UNIQUE
    assert state.embedded_count == EXPECTED_UNIQUE
    assert state.skipped_count == 1
    assert [s["item"] for s in db.state_row().skipped] == ["T1999"]
    assert len(transport.requests) == 1

    runner = orchestrator.runner(IngestSource.ATOMIC)
    with db.factory() as session:
        plan = runner.plan(session)
    assert plan.is_empty is True
    assert plan.expected_count == EXPECTED_UNIQUE

    for _ in range(2):
        orchestrator.verify_all()
        orchestrator.run_source(IngestSource.ATOMIC)
    assert len(transport.requests) == 1
    assert db.count(TechniqueCommand) == EXPECTED_UNIQUE
    assert status_of(orchestrator).status == IngestStatus.READY


def test_plan_redownloads_when_stored_rows_are_below_expected_count(db: Db) -> None:
    ingest_main_repo(db)
    db.set_state(status=IngestStatus.READY, error=None, expected_count=EXPECTED_UNIQUE + 3)
    runner = AtomicSourceRunner(embedder=FakeEmbedder(), fetcher=StaticFetcher([]))

    with db.factory() as session:
        assert runner.plan(session).needs_fetch is True


# ---------------------------------------------------------------------------
# (g) INGEST_WORK_DIR
# ---------------------------------------------------------------------------


class ParseSpy:
    """Wraps ``_parse_atomic`` to record the work directory content while parsing."""

    def __init__(self, work: Path, monkeypatch: pytest.MonkeyPatch, *, fail_on: str | None = None):
        self.work = work
        self.seen: list[list[str]] = []
        original = AtomicRedTeamIngestService._parse_atomic
        spy = self

        def wrapper(self_: Any, data: bytes, technique_id: str) -> Any:
            spy.seen.append(sorted(p.name for p in spy.work.iterdir()) if spy.work.exists() else [])
            if fail_on and technique_id == fail_on:
                raise RuntimeError("boom")
            return original(self_, data, technique_id)

        monkeypatch.setattr(AtomicRedTeamIngestService, "_parse_atomic", wrapper)


def test_valid_work_dir_uses_unique_subfolder_and_removes_it_on_success(
    db: Db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    monkeypatch.setenv("INGEST_WORK_DIR", str(work))
    spy = ParseSpy(work, monkeypatch)

    with db.factory() as session:
        summary = make_service(
            db, session, StaticFetcher(documents_of(repo_members())), FakeEmbedder()
        ).ingest()
        session.commit()

    assert summary.expected_count == EXPECTED_UNIQUE
    assert spy.seen, "the parser ran"
    during = spy.seen[0]
    assert len(during) == 1 and during[0].startswith("atomic_red_team_")
    assert list(work.iterdir()) == []  # the subfolder is gone


def test_valid_work_dir_is_cleaned_when_parsing_fails(
    db: Db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    monkeypatch.setenv("INGEST_WORK_DIR", str(work))
    ParseSpy(work, monkeypatch, fail_on="T1059.003")

    with db.factory() as session:
        service = make_service(
            db, session, StaticFetcher(documents_of(repo_members())), FakeEmbedder()
        )
        with pytest.raises(RuntimeError, match="boom"):
            service.ingest()

    assert list(work.iterdir()) == []
    assert db.count(TechniqueCommand) == 0


def test_valid_work_dir_is_cleaned_when_the_ingest_is_implausible(
    db: Db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    monkeypatch.setenv("INGEST_WORK_DIR", str(work))
    monkeypatch.setenv("ATOMIC_MIN_TECHNIQUES", "50")

    with db.factory() as session:
        service = make_service(
            db, session, StaticFetcher(documents_of(repo_members())), FakeEmbedder()
        )
        with pytest.raises(ImplausibleAtomicCountError):
            service.ingest()

    assert list(work.iterdir()) == []


def test_relative_work_dir_falls_back_to_memory_with_warning(
    db: Db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INGEST_WORK_DIR", "relative/work")

    with caplog.at_level(logging.WARNING, logger="app.services.ingestion.workdir"):
        summary = ingest_main_repo(db)

    assert summary.expected_count == EXPECTED_UNIQUE
    assert any("INGEST_WORK_DIR" in record.getMessage() for record in caplog.records)
    assert not (tmp_path / "relative").exists()


def test_unusable_work_dir_falls_back_to_memory_with_warning(
    db: Db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "a_file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("INGEST_WORK_DIR", str(blocker / "child"))

    with caplog.at_level(logging.WARNING, logger="app.services.ingestion.workdir"):
        summary = ingest_main_repo(db)

    assert summary.expected_count == EXPECTED_UNIQUE
    assert any("INGEST_WORK_DIR" in record.getMessage() for record in caplog.records)


def test_absent_work_dir_never_touches_the_disk(
    db: Db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the ingestion must not touch the disk without INGEST_WORK_DIR")

    monkeypatch.setenv("INGEST_WORK_DIR", "")
    monkeypatch.setattr(tempfile, "mkdtemp", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)

    summary = ingest_main_repo(db)

    assert summary.expected_count == EXPECTED_UNIQUE


# ---------------------------------------------------------------------------
# (i) progress(done, total) and heartbeat
# ---------------------------------------------------------------------------


def test_progress_reports_rows_then_expected_total_then_vectors(
    db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.atomic_red_team_ingest_service as module

    monkeypatch.setattr(module, "_ROW_BATCH_SIZE", 2)
    calls: list[tuple[int, int]] = []
    with db.factory() as session:
        make_service(
            db, session, StaticFetcher(documents_of(repo_members())), FakeEmbedder()
        ).ingest(progress=lambda done, total: calls.append((done, total)))
        session.commit()

    assert [c for c in calls if c[1] == 0] == [(2, 0), (4, 0), (5, 0)]
    with_total = [c for c in calls if c[1] > 0]
    assert with_total[0] == (EXPECTED_UNIQUE, EXPECTED_UNIQUE)  # relational part complete
    assert with_total[-1] == (EXPECTED_UNIQUE, EXPECTED_UNIQUE)  # vectors complete
    assert all(total == EXPECTED_UNIQUE for _, total in with_total)
    done_values = [done for done, _ in with_total]
    assert done_values == sorted(done_values)


def test_heartbeat_ticks_during_a_slow_download(db: Db) -> None:
    # Deterministic: no fixed sleeps. The "slow" download blocks until the ticker has
    # written a new heartbeat (bounded wait). The clock advances one second per call
    # so every tick is strictly newer. After the first observed tick the ticker is
    # muted (under a lock), because SQLite allows a single writer and a tick racing
    # with the persistence writes would make the test depend on timing.
    ticks = itertools.count()
    base = dt.datetime(2030, 1, 1)

    def fake_clock() -> dt.datetime:
        return base + dt.timedelta(seconds=next(ticks))

    gate = threading.Lock()
    ticker_enabled = [True]

    class GatedOrchestrator(IngestionOrchestrator):
        def _write_state(self, source: Any, *, only_owner: bool = False, **values: Any) -> bool:
            if set(values) == {"heartbeat_at"}:  # ticker write
                with gate:
                    if not ticker_enabled[0]:
                        return True
                    return super()._write_state(source, only_owner=only_owner, **values)
            return super()._write_state(source, only_owner=only_owner, **values)

    observed: list[Any] = []

    class SlowFetcher(StaticFetcher):
        def fetch_documents(self, *, ref: str) -> list[AtomicDocument]:
            observed.append(db.state_row().heartbeat_at)
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                if db.state_row().heartbeat_at != observed[0]:
                    break
                time.sleep(0.005)
            with gate:
                ticker_enabled[0] = False
            observed.append(db.state_row().heartbeat_at)
            return super().fetch_documents(ref=ref)

    fetcher = SlowFetcher(documents_of(many_members(1)))
    orchestrator = GatedOrchestrator(
        db.factory,
        {IngestSource.ATOMIC: AtomicSourceRunner(embedder=FakeEmbedder(), fetcher=fetcher)},
        heartbeat_interval=0.01,
        clock=fake_clock,
    )

    orchestrator.run_source(IngestSource.ATOMIC)

    assert observed[0] is not None
    assert observed[1] > observed[0]  # refreshed by the ticker while only downloading
    assert status_of(orchestrator).status == IngestStatus.READY
    assert not any(
        t.name.startswith("ingestion-heartbeat") and t.is_alive() for t in threading.enumerate()
    )


def test_orchestrator_persists_progress_counts_from_the_run(db: Db) -> None:
    orchestrator = make_orchestrator(
        db, StaticFetcher(documents_of(many_members(40))), FakeEmbedder()
    )

    orchestrator.run_source(IngestSource.ATOMIC)

    state = status_of(orchestrator)
    assert state.status == IngestStatus.READY
    assert state.expected_count == 40
    assert state.loaded_count == state.embedded_count == 40


# ---------------------------------------------------------------------------
# (i) ``input_arguments``: persistence, round trip, re-ingest (Req. 11.1, 11.7, 11.11)
# ---------------------------------------------------------------------------


def _ingest_arguments_document(db: Db, embedder: FakeEmbedder | None = None) -> Any:
    from _atomic_docs import arguments_document

    with db.factory() as session:
        service = make_service(
            db, session, StaticFetcher([arguments_document()]), embedder or FakeEmbedder()
        )
        summary = service.ingest()
        session.commit()
    return summary


def _rows_by_command(db: Db) -> dict[str, TechniqueCommand]:
    return {r.command: r for r in db.atomic_rows()}


def test_input_arguments_round_trip_through_ingestion(db: Db) -> None:
    from _atomic_docs import ARGUMENTS_COMMANDS

    summary = _ingest_arguments_document(db)
    assert summary.errors == []
    rows = _rows_by_command(db)
    assert set(rows) == set(ARGUMENTS_COMMANDS.values())

    stored = rows[ARGUMENTS_COMMANDS["with_defaults"]].input_arguments
    assert stored == {
        "zip_password": {"default": "infected", "description": "password", "type": "string"},
        "output_file": {
            "default": "$PathToAtomicsFolder/out.zip",
            "description": "out",
            "type": "path",
        },
        "input_file": {"default": "C:\\Temp\\in.txt", "description": "in", "type": "path"},
    }
    # The declared-without-default argument keeps its entry with ``default`` None.
    unknown = rows[ARGUMENTS_COMMANDS["unknown_placeholder"]].input_arguments
    assert unknown is not None and unknown["vbscript"]["default"] is None
    # A test without arguments stores ``{}`` (processed), never NULL (Req. 11.7).
    assert rows[ARGUMENTS_COMMANDS["no_arguments_stored"]].input_arguments == {}


def test_command_hash_is_computed_over_the_original_command(db: Db) -> None:
    from _atomic_docs import ARGUMENTS_COMMANDS

    _ingest_arguments_document(db)
    for command, row in _rows_by_command(db).items():
        assert command in ARGUMENTS_COMMANDS.values()
        assert row.command_hash == AtomicRedTeamIngestService._command_hash(command)
        assert "#{" in row.command or "PathToAtomicsFolder" in row.command


def test_stored_arguments_resolve_in_a_single_pass(db: Db) -> None:
    from _atomic_docs import ARGUMENTS_COMMANDS

    from app.services.atomic_arguments import resolve_command

    _ingest_arguments_document(db)
    rows = _rows_by_command(db)
    token = "$PathToAtomicsFolder"

    full = rows[ARGUMENTS_COMMANDS["with_defaults"]]
    result = resolve_command(full.command, full.input_arguments)
    # Repeated placeholder replaced everywhere; the path token in a default is kept.
    assert result.command == (f"7z a -pinfected {token}/out.zip C:\\Temp\\in.txt && echo infected")
    assert result.unresolved_arguments == ["PathToAtomicsFolder"]
    assert result.arguments_used["output_file"] == f"{token}/out.zip"

    path_row = rows[ARGUMENTS_COMMANDS["path_to_atomics"]]
    resolved = resolve_command(path_row.command, path_row.input_arguments)
    assert resolved.command == f"cp {token}/T1560.001/src/payload.txt /tmp/o"
    assert resolved.unresolved_arguments == ["PathToAtomicsFolder"]

    unknown = rows[ARGUMENTS_COMMANDS["unknown_placeholder"]]
    resolved = resolve_command(unknown.command, unknown.input_arguments)
    assert resolved.command == "run #{vbscript} with /tmp/o"
    assert resolved.unresolved_arguments == ["vbscript"]

    orphan = rows[ARGUMENTS_COMMANDS["no_arguments_stored"]]
    resolved = resolve_command(orphan.command, orphan.input_arguments)
    assert resolved.command == orphan.command
    assert resolved.unresolved_arguments == ["orphan"]


def test_malformed_input_arguments_do_not_fail_the_technique(db: Db) -> None:
    tests = [
        {
            "name": "bad map",
            "supported_platforms": ["linux"],
            "input_arguments": ["not", "a", "mapping"],
            "executor": {"name": "sh", "command": "echo #{x}"},
        },
        {
            "name": "bad entry",
            "supported_platforms": ["linux"],
            "input_arguments": {"x": "scalar", "y": {"default": 5}},
            "executor": {"name": "sh", "command": "echo #{y}"},
        },
    ]
    document = AtomicDocument("T1700", atomic_yaml("T1700", tests))
    with db.factory() as session:
        summary = make_service(db, session, StaticFetcher([document]), FakeEmbedder()).ingest()
        session.commit()

    assert summary.errors == [] and summary.skipped == []
    rows = _rows_by_command(db)
    assert rows["echo #{x}"].input_arguments == {}
    assert rows["echo #{y}"].input_arguments == {
        "y": {"default": "5", "description": None, "type": None}
    }


def test_reingest_fills_old_rows_without_duplicating_rows_or_embeddings(db: Db) -> None:
    from _atomic_docs import ARGUMENTS_COMMANDS

    _ingest_arguments_document(db)
    rows_before = db.count(TechniqueCommand)
    embeddings_before = db.count(ExperienceMemoryRow)
    ids_before = {r.command: r.id for r in db.atomic_rows()}

    # Simulate rows ingested before the column existed.
    with db.factory() as session:
        for row in session.query(TechniqueCommand):
            row.input_arguments = None
        session.commit()

    summary = _ingest_arguments_document(db)

    assert summary.commands_inserted == 0
    assert summary.affinities_inserted == 0
    assert db.count(TechniqueCommand) == rows_before
    assert db.count(ExperienceMemoryRow) == embeddings_before
    refilled = _rows_by_command(db)
    assert {c: r.id for c, r in refilled.items()} == ids_before
    assert (
        refilled[ARGUMENTS_COMMANDS["with_defaults"]].input_arguments["zip_password"]["default"]
        == "infected"
    )
    assert refilled[ARGUMENTS_COMMANDS["no_arguments_stored"]].input_arguments == {}
