"""Property test for the Command_Base exact→similarity resolution (task 10.2).

# Feature: stix-db-single-source-ai-pipeline, Property 13: O Command_Base resolve por mapeamento exato e cai para similaridade na ausência

Validates: Requirements 7.4, 7.5, 12.3

Esta é uma property de *feature* (o spec ``stix-db-single-source-ai-pipeline``
é feature, não bugfix): a invariante deve **valer** contra a implementação
atual de :class:`app.services.command_base.CommandBase`.

Property (design — Property 13)
-------------------------------
Para toda técnica, ``CommandBase.commands_for_technique`` resolve pela metade
exata (relacional) quando há mapeamento em ``technique_commands`` e cai para a
metade de similaridade (vetorial, via ``ExperienceMemory``) apenas na ausência
de mapeamento exato:

* técnica **com** linhas em ``technique_commands`` → retorna **somente** essas
  sugestões exatas (``exact=True``), nunca sugestões de similaridade (Req. 7.4,
  12.1);
* técnica **sem** linhas → retorna **somente** sugestões de similaridade
  (``exact=False``) vindas do ``ExperienceMemory`` (Req. 7.5, 12.3).

Isolamento / dublês
-------------------
O caminho exato usa uma ``Session`` SQLite efêmera (arquivo temporário +
``Base.metadata.create_all``, mesmo padrão dos testes irmãos), com linhas
``TechniqueCommand`` semeadas. O caminho de similaridade injeta um
``ExperienceMemory`` dublado (``memory=`` em ``CommandBase``) cujo ``retrieve``
devolve hits controlados — o SQLite não possui os operadores de cosseno do
pgvector, então a metade vetorial é sempre exercitada pelo dublê, nunca pelo
banco. Nenhum embedder real é necessário.

Caso concreto (ex-Property 14): o attack flow agent e o op agent obtêm o **mesmo**
conjunto de comandos para uma técnica porque ambos passam pela mesma
``CommandBase.commands_for_technique`` — asserido ao final do arquivo.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import TechniqueCommand
from app.services.ai.memory import ExperienceMemory
from app.services.command_base import CommandBase, CommandSuggestion


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema per example).
#
# Mirrors the temp-file SQLite + ``Base.metadata.create_all`` pattern from the
# sibling property tests (e.g. ``test_aggregate_progress_property.py``). SQLite
# lacks pgvector cosine ops, so the similarity half is always exercised through
# an injected memory double, never through the DB.
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterward.
    """
    tmp_dir = tempfile.mkdtemp(prefix="command_base_resolution_prop_")
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


def _dispose(session: Session, engine: Any) -> None:
    """Close the session and dispose the engine for a single example."""
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """Deterministic embedder double (never actually used by the fake memory).

    The similarity half is served by :class:`_FakeMemory`, which overrides
    ``retrieve`` and so never embeds. This fake is kept only to satisfy the
    ``Embedder`` protocol shape where an embedder would otherwise be required.
    """

    def embed(self, text: str) -> list[float]:
        # A trivial, stable projection of the text length into a fixed vector;
        # deterministic and never network-bound.
        value = float(len(text) % 7)
        return [value, value, value]


class _FakeMemory(ExperienceMemory):
    """Experience_Memory double returning controlled similarity hits.

    Subclasses :class:`ExperienceMemory` so it is a drop-in for the ``memory=``
    injection point of :class:`CommandBase`. ``retrieve`` ignores the SQLite
    session entirely and returns up to ``k`` canned hits keyed by the technique
    id, emulating the pgvector cosine fallback without pgvector.
    """

    def __init__(self, hits_by_technique: dict[str, list[dict[str, Any]]]) -> None:
        # No session/embedder needed: retrieve is fully overridden.
        super().__init__(session=None, embedder=_FakeEmbedder())  # type: ignore[arg-type]
        self._hits_by_technique = hits_by_technique

    def retrieve(
        self, *, campaign_id: str, query: str, k: int = 5
    ) -> list[dict[str, Any]]:
        return list(self._hits_by_technique.get(query, []))[:k]


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_technique_ids = st.builds(
    lambda n: f"T{n:04d}",
    st.integers(min_value=1000, max_value=9999),
)

_executors = st.sampled_from(["sh", "powershell", "cmd"])
_platforms = st.sampled_from(["linux", "windows", "macos"])
_sources = st.sampled_from(["atomic_red_team", "threat_intel"])
_commands = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=40,
).filter(lambda s: s.strip() != "")


@st.composite
def _exact_rows(draw: st.DrawFn, technique_id: str) -> list[dict[str, Any]]:
    """Generate 1..4 distinct exact command rows for a technique."""
    count = draw(st.integers(min_value=1, max_value=4))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i in range(count):
        command = draw(_commands)
        # Keep (command_hash, source) distinct so the UniqueConstraint holds.
        unique_command = f"{command}#{i}"
        if unique_command in seen:
            continue
        seen.add(unique_command)
        rows.append(
            {
                "command": unique_command,
                "executor": draw(_executors),
                "platform": draw(_platforms),
                "source": draw(_sources),
            }
        )
    return rows


@st.composite
def _similarity_hits(draw: st.DrawFn, technique_id: str) -> list[dict[str, Any]]:
    """Generate 1..4 canned retrieval hits carrying a command payload."""
    count = draw(st.integers(min_value=1, max_value=4))
    hits: list[dict[str, Any]] = []
    for _ in range(count):
        command = draw(_commands)
        hits.append(
            {
                "content": command,
                "metadata": {
                    "technique_id": technique_id,
                    "command": command,
                    "executor": draw(_executors),
                    "platform": draw(_platforms),
                    "source": draw(_sources),
                },
                "campaign_id": "__some_other_campaign__",
                "stage": None,
                "outcome": None,
            }
        )
    return hits


def _seed_exact(session: Session, technique_id: str, rows: list[dict[str, Any]]) -> None:
    """Persist the generated ``TechniqueCommand`` rows for a technique."""
    for index, row in enumerate(rows):
        session.add(
            TechniqueCommand(
                technique_id=technique_id,
                command=row["command"],
                executor=row["executor"],
                platform=row["platform"],
                source=row["source"],
                command_hash=f"hash-{technique_id}-{index}",
            )
        )
    session.commit()


# ---------------------------------------------------------------------------
# Property 13
# ---------------------------------------------------------------------------


# ``deadline=None``: each example builds and tears down a fresh temp-file SQLite
# database, whose file I/O timing varies on Windows and can occasionally exceed
# Hypothesis's default per-example deadline. The per-example DB is intentional
# (no state leak between examples), so we disable the deadline rather than
# weaken the isolation.
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(data=st.data())
def test_command_base_resolves_exact_then_similarity(data: st.DataObject) -> None:
    """Exact mapping present → only exact; absent → only similarity.

    # Feature: stix-db-single-source-ai-pipeline, Property 13: O Command_Base
    # resolve por mapeamento exato e cai para similaridade na ausência

    Validates: Requirements 7.4, 7.5, 12.3
    """
    technique_id = data.draw(_technique_ids)
    exact_rows = data.draw(_exact_rows(technique_id))
    similarity_hits = data.draw(_similarity_hits(technique_id))
    has_exact = data.draw(st.booleans())

    session, db_path, engine = _build_ephemeral_session()
    try:
        if has_exact:
            _seed_exact(session, technique_id, exact_rows)

        # The similarity half is always served by the double; the exact half is
        # served by the real SQLite session.
        memory = _FakeMemory({technique_id: similarity_hits})
        command_base = CommandBase(session, memory=memory)

        suggestions = command_base.commands_for_technique(technique_id, k=5)

        assert all(isinstance(s, CommandSuggestion) for s in suggestions)

        if has_exact:
            # Technique WITH exact rows → ONLY exact suggestions (Req. 7.4, 12.1).
            assert suggestions, "expected exact suggestions for a mapped technique"
            assert all(s.exact is True for s in suggestions)
            assert {s.command for s in suggestions} == {
                r["command"] for r in exact_rows
            }
            assert all(s.technique_id == technique_id for s in suggestions)
            # has_command_for is True whenever a backing command exists.
            assert command_base.has_command_for(technique_id) is True
        else:
            # Technique WITHOUT exact rows → ONLY similarity suggestions (7.5, 12.3).
            assert all(s.exact is False for s in suggestions)
            assert {s.command for s in suggestions} == {
                h["metadata"]["command"] for h in similarity_hits
            }
            # has_command_for mirrors whether the similarity half produced hits.
            assert command_base.has_command_for(technique_id) is bool(similarity_hits)
    finally:
        _dispose(session, engine)
        try:
            db_path.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Concrete example (ex-Property 14): attack flow agent and op agent see the SAME set.
# ---------------------------------------------------------------------------


def test_flow_and_op_agents_obtain_same_command_set() -> None:
    """attack flow agent and op agent resolve the identical command set for a technique.

    Both agents query the same ``CommandBase.commands_for_technique`` entry
    point, so for a given technique the set of commands they obtain must be
    identical — there is one Command_Base, shared (Req. 12.2).
    """
    technique_id = "T1059"
    session, db_path, engine = _build_ephemeral_session()
    try:
        _seed_exact(
            session,
            technique_id,
            [
                {
                    "command": "whoami",
                    "executor": "sh",
                    "platform": "linux",
                    "source": "atomic_red_team",
                },
                {
                    "command": "id",
                    "executor": "sh",
                    "platform": "linux",
                    "source": "atomic_red_team",
                },
            ],
        )
        command_base = CommandBase(session, memory=_FakeMemory({}))

        # The attack flow agent and the op agent each consult the shared Command_Base.
        flow_agent_commands = command_base.commands_for_technique(technique_id, k=5)
        op_agent_commands = command_base.commands_for_technique(technique_id, k=5)

        assert flow_agent_commands == op_agent_commands
        assert {s.command for s in flow_agent_commands} == {"whoami", "id"}
        assert all(s.exact is True for s in flow_agent_commands)
    finally:
        _dispose(session, engine)
        try:
            db_path.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Batch exact lookup (agentic-only-flow-redesign, Property 13: Operationalization
# listing is an exact relational lookup; Req. 6.2, 6.3, 6.6). Deterministic.
# ---------------------------------------------------------------------------


class _ExplodingMemory(_FakeMemory):
    """Memory double that fails the test if the vector half is ever consulted."""

    def retrieve(
        self, *, campaign_id: str, query: str, k: int = 5
    ) -> list[dict[str, Any]]:
        raise AssertionError("exact_commands_for must not consult ExperienceMemory")


def test_exact_commands_for_batch_is_exact_ordered_and_memory_free() -> None:
    session, db_path, engine = _build_ephemeral_session()
    try:
        _seed_exact(
            session,
            "T1059",
            [
                {"command": "b-first", "executor": "sh", "platform": "linux", "source": "atomic_red_team"},
                {"command": "a-second", "executor": "sh", "platform": "linux", "source": "atomic_red_team"},
            ],
        )
        _seed_exact(
            session,
            "T1003",
            [{"command": "dump", "executor": "cmd", "platform": "windows", "source": "atomic_red_team"}],
        )
        _seed_exact(
            session,
            "T9999",
            [{"command": "outside", "executor": "sh", "platform": "linux", "source": "atomic_red_team"}],
        )
        command_base = CommandBase(session, memory=_ExplodingMemory({}))

        result = command_base.exact_commands_for(["T1059", "T1003", "T1105", "T1059"])

        # One key per distinct requested id; unmapped technique -> empty list.
        assert set(result) == {"T1059", "T1003", "T1105"}
        assert result["T1105"] == []
        # Insertion (id) order inside a technique; nothing from unrequested ids.
        assert [s.command for s in result["T1059"]] == ["b-first", "a-second"]
        assert [s.command for s in result["T1003"]] == ["dump"]
        assert all(s.exact is True and s.id is not None for v in result.values() for s in v)
        # Empty input is a no-op.
        assert command_base.exact_commands_for([]) == {}
    finally:
        _dispose(session, engine)
        try:
            db_path.unlink(missing_ok=True)
        except OSError:
            pass
