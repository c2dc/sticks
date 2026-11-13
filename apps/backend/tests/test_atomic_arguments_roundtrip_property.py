"""Property test for the Atomic input arguments round trip (task 44.11).

**Property 19: Atomic input arguments round trip through ingestion**

For any valid Atomic document with ``input_arguments``, ingesting it and reading
the ``technique_commands`` row returns an ``input_arguments`` mapping whose names
and defaults equal the YAML values.

The ingestion runs through the real :class:`AtomicRedTeamIngestService` against a
throwaway SQLite file (never the user's database), with a static in-memory
fetcher and a deterministic fake embedder. No network.

**Validates: Requirements 11.1**
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
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


_NAME = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,15}", fullmatch=True)
_TEXT = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "S")),
    min_size=1,
    max_size=24,
)

# Each argument: an optional default (a missing default is a key left out).
_ARGUMENTS = st.dictionaries(
    _NAME,
    st.fixed_dictionaries(
        {"description": _TEXT, "type": st.sampled_from(["string", "path", "url", "integer"])},
        optional={"default": _TEXT},
    ),
    min_size=1,
    max_size=6,
)


def _ingest_and_read(arguments: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="atomic_args_roundtrip_"))
    engine = create_db_engine(
        Settings(database_url=f"sqlite:///{(tmp_dir / 'atomic.db').as_posix()}")
    )
    try:
        import_models()
        Base.metadata.create_all(bind=engine)
        factory: sessionmaker[Session] = sessionmaker(
            bind=engine, autoflush=False, expire_on_commit=False, future=True
        )
        document = AtomicDocument(
            "T1033",
            yaml.safe_dump(
                {
                    "attack_technique": "T1033",
                    "atomic_tests": [
                        {
                            "name": "with arguments",
                            "supported_platforms": ["linux"],
                            "input_arguments": arguments,
                            "executor": {"name": "sh", "command": "echo hello"},
                        }
                    ],
                },
                sort_keys=False,
            ).encode("utf-8"),
        )
        embedder = _FakeEmbedder()
        with factory() as session:
            service = AtomicRedTeamIngestService(
                session,
                memory=ExperienceMemory(session, embedder=embedder),
                fetcher=_StaticFetcher([document]),
            )
            service.ingest(ref="master")
            session.commit()
        with factory() as session:
            rows = list(
                session.execute(
                    select(TechniqueCommand).where(TechniqueCommand.source == "atomic_red_team")
                ).scalars()
            )
        assert len(rows) == 1
        return rows[0].input_arguments
    finally:
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(arguments=_ARGUMENTS)
def test_input_arguments_round_trip_through_ingestion(
    arguments: dict[str, dict[str, Any]],
) -> None:
    """Property 19: names and defaults stored equal the YAML values (Req. 11.1)."""
    stored = _ingest_and_read(arguments)

    assert stored is not None
    assert set(stored) == set(arguments)
    for name, body in arguments.items():
        assert stored[name]["default"] == body.get("default")
