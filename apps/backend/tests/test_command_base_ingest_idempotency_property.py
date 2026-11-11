"""Property test for the Command_Base idempotent ingest (task 11.2).

# Feature: stix-db-single-source-ai-pipeline, Property 11: A ingestão do Command_Base é idempotente por (técnica, comando, fonte)

Validates: Requirements 7.2, 7.6, 13.3, 13.6

Esta é uma property de *feature* (o spec ``stix-db-single-source-ai-pipeline``
é feature, não bugfix): a invariante deve **valer** contra a implementação
atual da ingestão do Command_Base.

Property (design — Property 11, funde as antigas Properties 10, 11 e 13.6)
--------------------------------------------------------------------------
A ingestão do Command_Base é idempotente por ``(technique_id, command_hash,
source)``. Parametrizado por ``source ∈ {atomic_red_team, threat_intel}`` num
único teste:

* ingerir **duas vezes** o mesmo conjunto produz o **mesmo** estado de
  ``technique_commands`` que ingerir **uma vez** — nenhuma linha é duplicada
  (Req. 7.6, 13.6);
* cada par ``(técnica, comando)`` vira **exatamente uma** linha em
  ``technique_commands``, carregando o ``source`` correto (ex-Property 10,
  Req. 7.2, 13.3).

Parametrização honesta da fonte
--------------------------------
A property é sobre a chave de idempotência ``(technique_id, command_hash,
source)``. Para ``atomic_red_team`` dirigimos o **serviço real**
:class:`app.services.atomic_red_team_ingest_service.AtomicRedTeamIngestService`
com um *fetcher* dublê apontando para uma árvore ``atomics/`` temporária que o
próprio teste escreve (sem rede). Para ``threat_intel`` o serviço de ingestão
(``ThreatIntelIngestService``) ainda não existe (é a task 15.1), então a mesma
invariante de persistência é exercitada diretamente contra ``technique_commands``
pelo **mesmo contrato check-before-insert** com ``source="threat_intel"`` — a
chave de idempotência verificada é idêntica, apenas a fonte muda.

Caso concreto (ex-Property 12): uma falha de *fetch* do Atomic Red Team
(``AtomicFetchError``) deixa ``technique_commands`` inalterada e reporta a falha
— asserido ao final do arquivo (Req. 7.7).

Isolamento / dublês
-------------------
Cada exemplo usa uma ``Session`` SQLite efêmera (arquivo temporário +
``Base.metadata.create_all``, mesmo padrão dos testes irmãos). O caminho de
afinidades da memória associativa chama ``memory._resolve_embedder().embed()``;
como o SQLite não possui os operadores do pgvector e não queremos um backend de
embeddings real, injetamos um ``_FakeEmbedder`` determinístico no
``ExperienceMemory``. Nenhuma rede é tocada.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import TechniqueCommand
from app.services.ai.memory import ExperienceMemory
from app.services.atomic_red_team_ingest_service import (
    AtomicFetchError,
    AtomicRedTeamIngestService,
    AtomicRepoFetcher,
)

_SOURCES = ["atomic_red_team", "threat_intel"]


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema per example).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterwards. Mirrors the temp-file SQLite +
    ``Base.metadata.create_all`` pattern of the sibling property tests.
    """
    tmp_dir = tempfile.mkdtemp(prefix="command_base_ingest_idempotency_prop_")
    db_path = Path(tmp_dir) / "command_base.db"
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


def _dispose(session: Session, engine: Any, db_path: Path) -> None:
    """Close the session, dispose the engine and remove the temp db file."""
    session.close()
    engine.dispose()
    try:
        db_path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """Deterministic ``Embedder`` double — a stable projection, never network.

    The affinity path of :class:`AtomicRedTeamIngestService` calls
    ``memory._resolve_embedder().embed(content)``; SQLite has no pgvector and we
    want no real embedding backend, so this fake returns a fixed-length vector
    derived deterministically from the text.
    """

    def embed(self, text: str) -> list[float]:
        value = float(len(text) % 7)
        return [value, value, value]


class _FakeFetcher:
    """``AtomicRepoFetcher`` double returning a pre-written ``atomics/`` tree.

    Satisfies the ``fetch_atomics_dir(ref, dest) -> Path`` contract by returning
    the path to a local fixture tree the test wrote, so the real ingest service
    parses it with no network access.
    """

    def __init__(self, atomics_dir: Path) -> None:
        self._atomics_dir = atomics_dir

    def fetch_atomics_dir(self, *, ref: str, dest: Path) -> Path:
        return self._atomics_dir


class _RaisingFetcher:
    """``AtomicRepoFetcher`` double whose fetch always raises ``AtomicFetchError``.

    Models the Req. 7.7 fetch-failure case: the ingest must report the failure
    before any write, leaving ``technique_commands`` unchanged.
    """

    def fetch_atomics_dir(self, *, ref: str, dest: Path) -> Path:
        raise AtomicFetchError("simulated fetch failure", ref=ref)


# ---------------------------------------------------------------------------
# Atomic fixture-tree writer
# ---------------------------------------------------------------------------


def _write_atomics_tree(root: Path, atomics: dict[str, list[dict[str, Any]]]) -> Path:
    """Write an ``atomics/T####/T####.yaml`` fixture tree and return its path.

    ``atomics`` maps a technique id to a list of atomic-test dicts; each test
    dict carries ``command``, ``executor`` and ``platforms``. The layout matches
    exactly what :class:`AtomicRedTeamIngestService` expects to parse.
    """
    import yaml

    atomics_dir = root / "atomics"
    atomics_dir.mkdir(parents=True, exist_ok=True)
    for technique_id, tests in atomics.items():
        technique_dir = atomics_dir / technique_id
        technique_dir.mkdir(parents=True, exist_ok=True)
        document = {
            "attack_technique": technique_id,
            "atomic_tests": [
                {
                    "name": f"{technique_id} test {index}",
                    "supported_platforms": test["platforms"],
                    "executor": {
                        "name": test["executor"],
                        "command": test["command"],
                    },
                }
                for index, test in enumerate(tests)
            ],
        }
        (technique_dir / f"{technique_id}.yaml").write_text(
            yaml.safe_dump(document), encoding="utf-8"
        )
    return atomics_dir


# ---------------------------------------------------------------------------
# Normalization helpers mirroring the service's idempotency key derivation.
# ---------------------------------------------------------------------------


def _normalize_command(command: str) -> str:
    """Normalize a command exactly as the ingest service does (idempotency key)."""
    return command.replace("\r\n", "\n").replace("\r", "\n").strip()


def _command_hash(command: str) -> str:
    """Deterministic SHA-256 of the normalized command (idempotency key part)."""
    return hashlib.sha256(
        _normalize_command(command).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Direct check-before-insert ingest for the threat_intel source.
#
# ThreatIntelIngestService is not implemented yet (task 15.1); this drives the
# SAME ``(technique_id, command_hash, source)`` idempotency contract directly
# against ``technique_commands`` with source="threat_intel", so the parametrized
# property verifies the invariant at the shared persistence level.
# ---------------------------------------------------------------------------


def _ingest_threat_intel(
    session: Session, rows: list[dict[str, Any]]
) -> None:
    """Persist ``technique_commands`` rows for the threat_intel source, idempotently.

    Replicates the service's check-before-insert on the
    ``(technique_id, command_hash, source)`` key so re-ingesting the same rows
    never duplicates — exactly the contract a real ThreatIntelIngestService must
    honour (Req. 13.3, 13.6).
    """
    source = "threat_intel"
    for row in rows:
        command = _normalize_command(row["command"])
        command_hash = _command_hash(command)
        existing = session.execute(
            select(TechniqueCommand.id).where(
                TechniqueCommand.technique_id == row["technique_id"],
                TechniqueCommand.command_hash == command_hash,
                TechniqueCommand.source == source,
            )
        ).first()
        if existing is not None:
            continue
        session.add(
            TechniqueCommand(
                technique_id=row["technique_id"],
                command=command,
                executor=row["executor"],
                platform=row["platform"],
                source=source,
                command_hash=command_hash,
            )
        )
        session.flush()
    session.commit()


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_technique_ids = st.builds(
    lambda n: f"T{n:04d}",
    st.integers(min_value=1000, max_value=9999),
)

_executors = st.sampled_from(["sh", "powershell", "cmd"])
_platforms = st.sampled_from(["linux", "windows", "macos"])
_commands = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=40,
).filter(lambda s: s.strip() != "")


@st.composite
def _ingest_dataset(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a small multi-technique ingest dataset with distinct commands.

    Returns ``{"atomics": {technique_id: [test, ...]}, "expected_pairs":
    {(technique_id, command_hash)}}``. Commands within a technique are made
    distinct so each ``(technique, command)`` pair maps to exactly one row; this
    lets the property assert the one-row-per-pair invariant deterministically.
    """
    technique_count = draw(st.integers(min_value=1, max_value=3))
    technique_ids = draw(
        st.lists(_technique_ids, min_size=technique_count, max_size=technique_count, unique=True)
    )

    atomics: dict[str, list[dict[str, Any]]] = {}
    expected_pairs: set[tuple[str, str]] = set()
    for technique_id in technique_ids:
        test_count = draw(st.integers(min_value=1, max_value=3))
        tests: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        for index in range(test_count):
            raw_command = draw(_commands)
            # Keep normalized commands distinct within a technique so every pair
            # is one row; a trailing marker survives normalization (no whitespace).
            command = f"{raw_command}#cmd{index}"
            command_hash = _command_hash(command)
            if command_hash in seen_hashes:
                continue
            seen_hashes.add(command_hash)
            expected_pairs.add((technique_id, command_hash))
            tests.append(
                {
                    "command": command,
                    "executor": draw(_executors),
                    "platforms": [draw(_platforms)],
                }
            )
        if tests:
            atomics[technique_id] = tests

    return {"atomics": atomics, "expected_pairs": expected_pairs}


# ---------------------------------------------------------------------------
# Persistence inspection helpers
# ---------------------------------------------------------------------------


def _row_count(session: Session, source: str) -> int:
    """Count ``technique_commands`` rows for a given source."""
    return int(
        session.execute(
            select(func.count())
            .select_from(TechniqueCommand)
            .where(TechniqueCommand.source == source)
        ).scalar_one()
    )


def _pairs_with_source(
    session: Session, source: str
) -> list[tuple[str, str, str]]:
    """Return ``(technique_id, command_hash, source)`` triples for a source."""
    return [
        (r.technique_id, r.command_hash, r.source)
        for r in session.execute(
            select(
                TechniqueCommand.technique_id,
                TechniqueCommand.command_hash,
                TechniqueCommand.source,
            ).where(TechniqueCommand.source == source)
        ).all()
    ]


# ---------------------------------------------------------------------------
# Property 11 — idempotent ingest parametrized by source
# ---------------------------------------------------------------------------


# ``deadline=None``: each example builds and tears down a fresh temp-file SQLite
# database plus an on-disk atomics fixture tree, whose file I/O timing varies on
# Windows and can exceed Hypothesis's default per-example deadline. The
# per-example isolation is intentional, so we disable the deadline rather than
# weaken it.
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(source=st.sampled_from(_SOURCES), dataset=_ingest_dataset())
def test_command_base_ingest_is_idempotent_by_technique_command_source(
    source: str, dataset: dict[str, Any]
) -> None:
    """Ingesting twice equals once; each (technique, command) is one row.

    # Feature: stix-db-single-source-ai-pipeline, Property 11: A ingestão do
    # Command_Base é idempotente por (técnica, comando, fonte)

    Validates: Requirements 7.2, 7.6, 13.3, 13.6
    """
    atomics_spec: dict[str, list[dict[str, Any]]] = dataset["atomics"]
    expected_pairs: set[tuple[str, str]] = dataset["expected_pairs"]

    session, db_path, engine = _build_ephemeral_session()
    fixture_root = Path(tempfile.mkdtemp(prefix="atomics_fixture_"))
    try:
        if source == "atomic_red_team":
            atomics_dir = _write_atomics_tree(fixture_root, atomics_spec)
            memory = ExperienceMemory(session, embedder=_FakeEmbedder())
            service = AtomicRedTeamIngestService(
                session, memory=memory, fetcher=_FakeFetcher(atomics_dir)
            )

            def _run_ingest() -> None:
                service.ingest(ref="master")
                session.commit()
        else:
            # threat_intel: drive the SAME idempotency contract directly against
            # technique_commands (ThreatIntelIngestService is task 15.1).
            threat_rows = [
                {
                    "technique_id": technique_id,
                    "command": test["command"],
                    "executor": test["executor"],
                    "platform": test["platforms"][0],
                }
                for technique_id, tests in atomics_spec.items()
                for test in tests
            ]

            def _run_ingest() -> None:
                _ingest_threat_intel(session, threat_rows)

        # First ingest.
        _run_ingest()
        count_after_first = _row_count(session, source)
        pairs_after_first = _pairs_with_source(session, source)

        # Each (technique, command) pair becomes exactly one row with the source.
        assert count_after_first == len(expected_pairs)
        assert len(pairs_after_first) == len(set(pairs_after_first)), (
            "no duplicate (technique_id, command_hash, source) rows"
        )
        assert {(t, h) for (t, h, _) in pairs_after_first} == expected_pairs
        assert all(src == source for (_, _, src) in pairs_after_first)

        # Second ingest of the SAME dataset — idempotent: 2x == 1x, no dup.
        _run_ingest()
        count_after_second = _row_count(session, source)
        pairs_after_second = _pairs_with_source(session, source)

        assert count_after_second == count_after_first, (
            "re-ingesting the same dataset must not add rows (idempotency)"
        )
        assert sorted(pairs_after_second) == sorted(pairs_after_first)
    finally:
        _dispose(session, engine, db_path)
        import shutil

        shutil.rmtree(fixture_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Concrete example (ex-Property 12): a fetch failure leaves the table unchanged.
# ---------------------------------------------------------------------------


def test_atomic_fetch_failure_leaves_technique_commands_unchanged_and_reports() -> None:
    """A fetch failure preserves ``technique_commands`` and reports the failure.

    The ingest fetches BEFORE opening any write (Req. 7.7). When the fetcher
    raises :class:`AtomicFetchError`, the service must propagate the failure and
    leave the already-persisted ``technique_commands`` index untouched.
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        # Seed one pre-existing row so we can assert it survives the failed fetch.
        existing_command = "whoami"
        session.add(
            TechniqueCommand(
                technique_id="T1033",
                command=existing_command,
                executor="sh",
                platform="linux",
                source="atomic_red_team",
                command_hash=_command_hash(existing_command),
            )
        )
        session.commit()

        before = _pairs_with_source(session, "atomic_red_team")
        assert len(before) == 1

        memory = ExperienceMemory(session, embedder=_FakeEmbedder())
        service = AtomicRedTeamIngestService(
            session, memory=memory, fetcher=_RaisingFetcher()
        )

        # The fetch failure is reported (raised), not swallowed.
        with pytest.raises(AtomicFetchError):
            service.ingest(ref="master")

        # The persisted index is unchanged — no rows added, none removed.
        after = _pairs_with_source(session, "atomic_red_team")
        assert after == before
        assert _row_count(session, "atomic_red_team") == 1
    finally:
        _dispose(session, engine, db_path)
