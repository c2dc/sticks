"""Property test for Atomic re-ingestion (task 44.12).

**Property 20: Atomic re-ingestion is idempotent and fills arguments**

For any set of rows previously ingested without arguments and any number n >= 1
of repeated re-ingest runs, the number of ``technique_commands`` rows and of
experience-memory embeddings equals the number after the first ingestion, every
row has its ``input_arguments`` filled, and the ``command_hash`` of each row is
unchanged.

Runs through the real :class:`AtomicRedTeamIngestService` against a throwaway
SQLite file (never the user's database), with a static in-memory fetcher and a
deterministic fake embedder. No network.

**Validates: Requirements 11.7**
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import ExperienceMemory as ExperienceMemoryRow
from app.models.domain import TechniqueCommand
from app.services.ai.memory import ExperienceMemory
from app.services.atomic_red_team_ingest_service import (
    AtomicDocument,
    AtomicRedTeamIngestService,
)


class _StaticFetcher:
    def __init__(self, documents: list[AtomicDocument]) -> None:
        self._documents = documents

    def fetch_documents(self, *, ref: str) -> list[AtomicDocument]:
        return list(self._documents)


class _FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        value = float(len(text) % 7)
        return [value, value, value]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


_NAME = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,10}", fullmatch=True)
_TEXT = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=1,
    max_size=12,
)

# One atomic test: an optional set of arguments (possibly empty or absent).
_ARGUMENTS = st.dictionaries(
    _NAME,
    st.fixed_dictionaries(
        {"description": _TEXT, "type": st.just("string")}, optional={"default": _TEXT}
    ),
    max_size=3,
)
_TESTS = st.lists(_ARGUMENTS, min_size=1, max_size=4)


def _document(per_test_arguments: list[dict[str, Any]]) -> AtomicDocument:
    tests = []
    for index, arguments in enumerate(per_test_arguments):
        test: dict[str, Any] = {
            "name": f"test {index}",
            "supported_platforms": ["linux"],
            # A distinct command per test so each one yields its own row.
            "executor": {"name": "sh", "command": f"echo hello-{index}"},
        }
        if arguments:
            test["input_arguments"] = arguments
        tests.append(test)
    payload = {"attack_technique": "T1033", "atomic_tests": tests}
    return AtomicDocument("T1033", yaml.safe_dump(payload, sort_keys=False).encode("utf-8"))


@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(per_test_arguments=_TESTS, runs=st.integers(min_value=1, max_value=3))
def test_reingestion_is_idempotent_and_fills_arguments(
    per_test_arguments: list[dict[str, Any]], runs: int
) -> None:
    """Property 20: no new rows/embeddings, arguments filled, hashes stable (Req. 11.7)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="atomic_reingest_"))
    engine = create_db_engine(
        Settings(database_url=f"sqlite:///{(tmp_dir / 'atomic.db').as_posix()}")
    )
    try:
        import_models()
        Base.metadata.create_all(bind=engine)
        factory: sessionmaker[Session] = sessionmaker(
            bind=engine, autoflush=False, expire_on_commit=False, future=True
        )
        document = _document(per_test_arguments)
        embedder = _FakeEmbedder()

        def ingest() -> None:
            with factory() as session:
                AtomicRedTeamIngestService(
                    session,
                    memory=ExperienceMemory(session, embedder=embedder),
                    fetcher=_StaticFetcher([document]),
                ).ingest(ref="master")
                session.commit()

        def count(model: Any) -> int:
            with factory() as session:
                return int(session.execute(select(func.count()).select_from(model)).scalar_one())

        def snapshot() -> dict[str, tuple[int, str]]:
            with factory() as session:
                return {
                    r.command: (r.id, r.command_hash)
                    for r in session.execute(
                        select(TechniqueCommand).where(TechniqueCommand.source == "atomic_red_team")
                    ).scalars()
                }

        ingest()
        rows_after_first = count(TechniqueCommand)
        embeddings_after_first = count(ExperienceMemoryRow)
        identity_after_first = snapshot()
        assert rows_after_first == len(per_test_arguments)

        # Simulate rows ingested before the column existed.
        with factory() as session:
            for row in session.query(TechniqueCommand):
                row.input_arguments = None
            session.commit()

        for _ in range(runs):
            ingest()

        assert count(TechniqueCommand) == rows_after_first
        assert count(ExperienceMemoryRow) == embeddings_after_first
        assert snapshot() == identity_after_first
        with factory() as session:
            rows = {
                r.command: r.input_arguments
                for r in session.execute(
                    select(TechniqueCommand).where(TechniqueCommand.source == "atomic_red_team")
                ).scalars()
            }
        for index, arguments in enumerate(per_test_arguments):
            filled = rows[f"echo hello-{index}"]
            assert filled is not None
            assert set(filled) == set(arguments)
            for name, body in arguments.items():
                assert filled[name]["default"] == body.get("default")
    finally:
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)
