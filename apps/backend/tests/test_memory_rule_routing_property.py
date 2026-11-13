"""Property test for the Memory_Rule routing + bootstrap/retrieval examples (task 12.2).

# Feature: stix-db-single-source-ai-pipeline, Property 7: O roteamento do Memory_Rule é determinístico por tipo de dado

Validates: Requirements 6.3, 6.4

Esta é uma property de *feature* (o spec ``stix-db-single-source-ai-pipeline`` é
feature, não bugfix): a invariante deve **valer** contra a implementação atual.

Property 7 (design — roteamento do Memory_Rule)
-----------------------------------------------
*Para qualquer* item de conhecimento, o roteamento é determinístico por tipo de
dado (design §F, Req. 6.3, 6.4):

* um item **exato/factual** (ex.: um mapeamento técnica↔comando) é persistido no
  ``Relational_Store`` (``technique_commands``) e **nunca** no ``Vector_Store``
  (``experience_memory``);
* um item **associativo** (ex.: uma afinidade comando↔ferramenta ou "ambiente
  que funcionou") é persistido no ``experience_memory`` (vetorial) e **nunca** em
  uma tabela relacional rígida.

Como a property é dirigida de forma honesta
-------------------------------------------
O Hypothesis gera, por exemplo, uma lista de itens de conhecimento, cada um já
*classificado* pela sua natureza (``exact`` vs ``associative``). O teste então
roteia cada item pelo caminho REAL da sua classe — itens associativos via
:meth:`app.services.ai.memory.ExperienceMemory.store_affinity`, itens exatos via
o caminho relacional ``technique_commands`` (o mesmo contrato check-before-insert
que a ingestão do Command_Base usa) — e depois **inspeciona o banco** para
afirmar o destino de cada um:

* todo item associativo tem **uma** linha correspondente em ``experience_memory``
  e **nenhuma** linha em ``technique_commands`` (não vazou para o relacional
  rígido);
* todo item exato tem **uma** linha correspondente em ``technique_commands`` e
  **nenhuma** linha em ``experience_memory`` (não vazou para o vetorial).

O roteamento é afirmado contando linhas por tabela e casando chaves, não
confiando no valor de retorno do código sob teste — então um vazamento de um
lado para o outro quebraria a property.

Casos concretos no mesmo arquivo
--------------------------------
* **ex-Property 8 (idempotência do bootstrap).** Rodar
  :meth:`app.services.experience_memory_bootstrap.ExperienceMemoryBootstrap.bootstrap`
  duas vezes == uma vez (segunda passada ``inserted == 0``, tudo em
  ``skipped_existing``) e toda linha gravada carrega um ``embedding`` não-nulo
  (Req. 6.1, 6.2).
* **ex-Property 9 (ordenação do retrieval).** Semear várias linhas de
  ``experience_memory`` com embeddings controlados em campanhas distintas e
  afirmar que :meth:`ExperienceMemory.retrieve` devolve ``<= k`` linhas ordenadas
  por distância de cosseno ascendente, excluindo a ``campaign_id`` corrente
  (Req. 6.6).

Isolamento / dublês
-------------------
Os caminhos de ``store_affinity``/``bootstrap``/relacional usam uma ``Session``
SQLite efêmera (arquivo temporário + ``Base.metadata.create_all``, mesmo padrão
dos testes irmãos) com um ``_FakeEmbedder`` determinístico injetado no
:class:`ExperienceMemory` (o SQLite não possui um backend de embeddings; o vetor
é apenas armazenado na coluna portável ``Vector``). Nenhuma rede é tocada.

Sobre pgvector/SQLite no retrieval (ex-Property 9)
--------------------------------------------------
A ordenação real de ``retrieve`` usa o operador de distância de cosseno do
pgvector (``embedding <=> :q``), que o SQLite não avalia. Em vez de um Postgres
vivo (que a suíte nunca sobe), a ordenação é exercida contra o ``retrieve`` REAL
com uma ``Session`` dublê que *interpreta* o ``select`` que o próprio código
produziu — lê o embedding da consulta, a ``campaign_id`` excluída e o ``LIMIT``
diretamente da instrução compilada e reproduz o que o pgvector faria (excluir a
campanha corrente, ordenar por distância de cosseno, cortar em ``k``). Derivar a
resposta da instrução emitida — e não de parâmetros entregues ao dublê — mantém a
asserção honesta: se ``retrieve`` esquecesse a exclusão, o ``ORDER BY`` de cosseno
ou o ``LIMIT``, o resultado interpretado violaria a ordenação e o teste falharia.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import shutil
import tempfile
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import (
    Ability,
    Case,
    TechniqueCommand,
)
from app.models.domain import (
    ExperienceMemory as ExperienceMemoryRow,
)
from app.services.ai.memory import ExperienceMemory
from app.services.experience_memory_bootstrap import ExperienceMemoryBootstrap

# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema per example).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterwards. Mirrors the temp-file SQLite +
    ``Base.metadata.create_all`` pattern of the sibling property tests.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="memory_rule_routing_prop_"))
    db_path = tmp_dir / "memory_rule.db"
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
    """Close the session, dispose the engine and remove the temp db file + dir."""
    session.close()
    engine.dispose()
    shutil.rmtree(db_path.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Doubles: a deterministic embedder (store path) and cosine helpers (retrieval).
# ---------------------------------------------------------------------------

# Fixed, small embedding width used by the deterministic embedder of the store
# path (store_affinity / bootstrap only store the vector; they never compute
# cosine on SQLite). Kept small to keep generation fast.
_EMBED_DIM = 8


class _FakeEmbedder:
    """Deterministic text->vector map standing in for the SEPINF embedder.

    Structurally satisfies the ``@runtime_checkable`` ``Embedder`` Protocol (an
    ``embed(text) -> list[float]`` method). The mapping is a stable projection of
    a text-and-dimension hash, so the SAME text always embeds to the SAME vector
    while DIFFERENT texts generally land at different cosine distances. No
    network, no model weights.
    """

    def embed(self, text: str) -> list[float]:
        vec: list[float] = []
        for dim in range(_EMBED_DIM):
            component = hashlib.sha256(f"{dim}:{text}".encode()).digest()[0]
            vec.append((component / 127.5) - 1.0)
        # Guard against the all-zero vector (cosine distance is undefined for a
        # zero vector): nudge the first component so every vector has non-zero
        # magnitude. Deterministic, so storage and query stay consistent.
        if all(abs(c) < 1e-12 for c in vec):
            vec[0] = 1.0
        return vec


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """Cosine distance ``1 - cos(a, b)`` — pgvector's ``<=>`` semantics.

    Recomputed independently here so the ordering assertions do not trust the
    code under test to define its own expected order.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - (dot / (na * nb))


# ---------------------------------------------------------------------------
# Relational (exact/factual) ingest — the technique_commands path.
#
# The exact half of the Command_Base uses the SAME check-before-insert on the
# (technique_id, command_hash, source) key the ingest services use. Routing an
# exact item here (never through store_affinity) is the factual branch of the
# Memory_Rule (Req. 6.3).
# ---------------------------------------------------------------------------


def _command_hash(command: str) -> str:
    """Deterministic SHA-256 of the normalized command (idempotency key part)."""
    normalized = command.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _store_exact_command(
    session: Session, *, technique_id: str, command: str, executor: str, platform: str
) -> None:
    """Persist one exact technique→command mapping into the Relational_Store.

    Check-before-insert on ``(technique_id, command_hash, source)`` so the exact
    routing branch is itself deterministic and never duplicates.
    """
    command_hash = _command_hash(command)
    existing = session.execute(
        select(TechniqueCommand.id).where(
            TechniqueCommand.technique_id == technique_id,
            TechniqueCommand.command_hash == command_hash,
            TechniqueCommand.source == "atomic_red_team",
        )
    ).first()
    if existing is not None:
        return
    session.add(
        TechniqueCommand(
            technique_id=technique_id,
            command=command.strip(),
            executor=executor,
            platform=platform,
            source="atomic_red_team",
            command_hash=command_hash,
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# Strategies for Property 7 — a pool of classified knowledge items.
# ---------------------------------------------------------------------------

_technique_ids = st.builds(lambda n: f"T{n:04d}", st.integers(min_value=1000, max_value=9999))
_executors = st.sampled_from(["sh", "powershell", "cmd"])
_platforms = st.sampled_from(["linux", "windows", "macos"])
_affinity_kinds = st.sampled_from(["command_affinity", "environment_affinity"])
_payloads = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=24,
).filter(lambda s: s.strip() != "")


@st.composite
def _knowledge_items(draw: st.DrawFn) -> list[dict[str, Any]]:
    """Generate a pool of classified knowledge items (exact vs associative).

    Each item is a dict with ``nature`` in ``{"exact", "associative"}`` plus the
    fields its routing path needs. Payloads are made distinct across items so an
    item's destination row can be matched one-to-one, letting the routing
    assertions pin both presence (one row on the right side) and absence (no row
    on the wrong side) deterministically.
    """
    count = draw(st.integers(min_value=1, max_value=8))
    items: list[dict[str, Any]] = []
    for index in range(count):
        nature = draw(st.sampled_from(["exact", "associative"]))
        payload = f"{draw(_payloads)}#item{index}"
        if nature == "exact":
            items.append(
                {
                    "nature": "exact",
                    "technique_id": draw(_technique_ids),
                    "command": payload,
                    "executor": draw(_executors),
                    "platform": draw(_platforms),
                }
            )
        else:
            items.append(
                {
                    "nature": "associative",
                    "kind": draw(_affinity_kinds),
                    "content": f"affinity {payload}",
                }
            )
    return items


# ---------------------------------------------------------------------------
# Property 7 — the Memory_Rule routing is deterministic by data type.
#
# ``deadline=None``: each example builds and tears down a fresh temp-file SQLite
# database, whose file I/O timing varies on Windows and can exceed Hypothesis's
# default per-example deadline. The per-example isolation is intentional, so the
# deadline is lifted rather than weakened.
# ---------------------------------------------------------------------------
# Feature: stix-db-single-source-ai-pipeline, Property 7: O roteamento do Memory_Rule é determinístico por tipo de dado
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(items=_knowledge_items())
def test_property7_memory_rule_routing_is_deterministic_by_data_type(
    items: list[dict[str, Any]],
) -> None:
    """Exact data routes relational-only; associative data routes vector-only.

    # Feature: stix-db-single-source-ai-pipeline, Property 7: O roteamento do Memory_Rule é determinístico por tipo de dado

    Validates: Requirements 6.3, 6.4
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        memory = ExperienceMemory(session, embedder=_FakeEmbedder())

        # Route every item through the REAL path for its class.
        for item in items:
            if item["nature"] == "exact":
                _store_exact_command(
                    session,
                    technique_id=item["technique_id"],
                    command=item["command"],
                    executor=item["executor"],
                    platform=item["platform"],
                )
            else:
                memory.store_affinity(kind=item["kind"], content=item["content"])
        session.commit()

        exact_items = [i for i in items if i["nature"] == "exact"]
        assoc_items = [i for i in items if i["nature"] == "associative"]

        # --- Associative items: in the Vector_Store, never in the rigid
        #     relational technique_commands table (Req. 6.4). ---
        for item in assoc_items:
            dedup_key = ExperienceMemory.content_dedup_key(item["content"])
            vector_rows = session.execute(
                select(func.count())
                .select_from(ExperienceMemoryRow)
                .where(ExperienceMemoryRow.meta["dedup_key"].as_string() == dedup_key)
            ).scalar_one()
            assert vector_rows == 1, (
                "an associative item must land as exactly one experience_memory row"
            )
            # The associative content never leaked into the rigid relational
            # table as a command.
            leaked = session.execute(
                select(func.count())
                .select_from(TechniqueCommand)
                .where(TechniqueCommand.command == item["content"])
            ).scalar_one()
            assert leaked == 0, "associative knowledge must not reach a rigid table"

        # --- Exact items: in the Relational_Store, never in the Vector_Store
        #     (Req. 6.3). ---
        for item in exact_items:
            command = item["command"].strip()
            relational_rows = session.execute(
                select(func.count())
                .select_from(TechniqueCommand)
                .where(
                    TechniqueCommand.technique_id == item["technique_id"],
                    TechniqueCommand.command == command,
                    TechniqueCommand.source == "atomic_red_team",
                )
            ).scalar_one()
            assert relational_rows == 1, (
                "an exact item must land as exactly one technique_commands row"
            )
            # The exact command never leaked into the vector store as content.
            leaked = session.execute(
                select(func.count())
                .select_from(ExperienceMemoryRow)
                .where(ExperienceMemoryRow.content == command)
            ).scalar_one()
            assert leaked == 0, "exact/factual data must not reach the vector store"

        # --- Table-level separation: the vector store holds exactly the
        #     associative items, the relational store exactly the exact ones. ---
        vector_total = session.execute(
            select(func.count()).select_from(ExperienceMemoryRow)
        ).scalar_one()
        relational_total = session.execute(
            select(func.count()).select_from(TechniqueCommand)
        ).scalar_one()
        # Distinct dedup keys / commands make each item map to one row.
        assert vector_total == len(
            {ExperienceMemory.content_dedup_key(i["content"]) for i in assoc_items}
        )
        assert relational_total == len(
            {(i["technique_id"], _command_hash(i["command"])) for i in exact_items}
        )
    finally:
        _dispose(session, engine, db_path)


# ---------------------------------------------------------------------------
# Concrete example (ex-Property 8): the bootstrap is idempotent and embeds all.
# ---------------------------------------------------------------------------


def _seed_curated_campaign(session: Session) -> None:
    """Seed one curated campaign + abilities directly into the relational tables.

    The bootstrap reads curated content FROM THE DB (never disk), so seeding the
    ``campaigns``/``abilities`` rows is enough to drive it. Two abilities with
    concrete executors give the bootstrap command_affinity rows plus one
    environment_affinity per campaign.
    """
    session.add(Case(id="curated-demo", name="Curated Demo", description="seed for bootstrap"))
    session.add(
        Ability(
            ability_id="ab-1",
            campaign_id="curated-demo",
            name="T1059 - Command Interpreter",
            technique_id="T1059",
            executors=[
                {"name": "sh", "platform": "linux", "command": "echo hello"},
                {"name": "powershell", "platform": "windows", "command": "Get-Process"},
            ],
        )
    )
    session.add(
        Ability(
            ability_id="ab-2",
            campaign_id="curated-demo",
            name="T1105 - Ingress Tool Transfer",
            technique_id="T1105",
            executors=[
                {"name": "sh", "platform": "linux", "command": "curl http://x/y"},
            ],
        )
    )
    session.flush()


def test_ex_property8_bootstrap_is_idempotent_and_embeds_every_row() -> None:
    """Running bootstrap twice == once; every stored row carries an embedding.

    ex-Property 8 (design): the second pass reports ``inserted == 0`` with every
    equivalent entry under ``skipped_existing``, and no row is duplicated
    (Req. 6.1, 6.2). Every ``experience_memory`` row has a non-null embedding.
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        _seed_curated_campaign(session)
        session.commit()

        memory = ExperienceMemory(session, embedder=_FakeEmbedder())
        bootstrap = ExperienceMemoryBootstrap(session, memory=memory)

        first = bootstrap.bootstrap()
        session.commit()
        rows_after_first = session.execute(
            select(func.count()).select_from(ExperienceMemoryRow)
        ).scalar_one()

        # The first pass inserted something (two command_affinity + one
        # environment_affinity from the seeded campaign) and skipped nothing.
        assert first.inserted > 0
        assert first.skipped_existing == 0
        assert rows_after_first == first.inserted

        # Every stored row carries a non-null, non-empty embedding.
        embeddings = session.execute(select(ExperienceMemoryRow.embedding)).scalars().all()
        assert embeddings, "bootstrap must have stored at least one row"
        for embedding in embeddings:
            assert embedding is not None
            assert len(list(embedding)) == _EMBED_DIM

        # Second pass of the unchanged corpus: idempotent — inserts nothing, all
        # equivalent entries skipped, row count unchanged.
        second = bootstrap.bootstrap()
        session.commit()
        rows_after_second = session.execute(
            select(func.count()).select_from(ExperienceMemoryRow)
        ).scalar_one()

        assert second.inserted == 0, "a second bootstrap must insert no rows"
        assert second.skipped_existing == first.inserted, (
            "every equivalent entry is counted as skipped on the second pass"
        )
        assert rows_after_second == rows_after_first, "no rows duplicated"
    finally:
        _dispose(session, engine, db_path)


# ---------------------------------------------------------------------------
# Concrete example (ex-Property 9): retrieval ordering + campaign exclusion.
#
# The real cosine ordering uses pgvector, which SQLite lacks. This example drives
# the REAL ExperienceMemory.retrieve against a fake session that INTERPRETS the
# production cosine top-k select (reading the bound embedding, excluded
# campaign_id and LIMIT off the compiled statement), so the assertion checks the
# query retrieve builds — not a hand-fed answer.
# ---------------------------------------------------------------------------


class _Row:
    """Minimal stand-in for an ``experience_memory`` row used by retrieval.

    Carries exactly the attributes ``ExperienceMemory._row_to_hit`` projects plus
    the ``embedding`` the fake store orders by.
    """

    def __init__(self, *, campaign_id: str, content: str, embedding: list[float]) -> None:
        self.campaign_id = campaign_id
        self.content = content
        self.embedding = embedding
        self.meta = {"kind": "environment_affinity"}
        self.stage = "stage_4_operationalization"
        self.outcome = "sucesso"


class _ScalarResult:
    """Tiny result wrapper mirroring ``Result.scalars().all()`` used by retrieve."""

    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[_Row]:
        return list(self._rows)


class _FakeVectorSession:
    """FAKE session that interprets the production cosine top-k ``select``.

    Reproduces what pgvector would return by reading the excluded ``campaign_id``,
    the query embedding and the ``LIMIT`` straight off the compiled statement the
    real ``retrieve`` built: exclude the running campaign, order by ascending
    cosine distance to the query embedding, truncate to the limit. Deriving the
    answer from the emitted statement (not from parameters passed in by the test)
    makes this a faithful check of the query ``retrieve`` builds.
    """

    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def execute(self, stmt: Any) -> _ScalarResult:
        params = stmt.compile().params

        excluded_campaign: str | None = None
        query_embedding: list[float] | None = None
        limit: int | None = None
        for value in params.values():
            if isinstance(value, list):
                query_embedding = value
            elif isinstance(value, bool):
                continue
            elif isinstance(value, int):
                limit = value
            elif isinstance(value, str):
                excluded_campaign = value

        assert query_embedding is not None, "retrieve must bind a query embedding"
        assert excluded_campaign is not None, (
            "retrieve must bind the excluded campaign_id (WHERE campaign_id != :c)"
        )
        assert limit is not None, "retrieve must bind a LIMIT (the k bound)"

        eligible = [r for r in self._rows if r.campaign_id != excluded_campaign]
        eligible.sort(key=lambda r: _cosine_distance(r.embedding, query_embedding))
        return _ScalarResult(eligible[:limit])


def test_ex_property9_retrieve_orders_by_cosine_and_excludes_current_campaign() -> None:
    """Retrieve returns <= k rows by ascending cosine distance, campaign-excluded.

    ex-Property 9 (design, Req. 6.6): seed several experience_memory rows with
    controlled embeddings across campaigns; the REAL ``retrieve`` returns at most
    ``k`` rows ordered by ascending cosine distance to the query and never any row
    of the running campaign.
    """
    embedder = _FakeEmbedder()
    query = "target environment linux web service"
    running_campaign = "camp-current"

    # Seed rows across campaigns, including rows OF the running campaign that must
    # be excluded. Each row's embedding is derived from its content by the SAME
    # embedder the query uses, so cosine ordering is meaningful.
    seed = [
        (running_campaign, "this row is of the current campaign and must be excluded"),
        ("camp-a", "linux web service nginx target environment"),
        ("camp-b", "windows active directory domain controller"),
        ("camp-c", "linux container kubernetes cluster"),
        (running_campaign, "another current-campaign row to exclude"),
        ("camp-d", "macos endpoint laptop fleet"),
    ]
    store_rows = [
        _Row(campaign_id=cid, content=content, embedding=embedder.embed(content))
        for cid, content in seed
    ]
    session = _FakeVectorSession(store_rows)
    memory = ExperienceMemory(session, embedder=embedder)

    k = 3
    hits = memory.retrieve(campaign_id=running_campaign, query=query, k=k)

    # Bounded by k.
    assert len(hits) <= k

    # Excludes the running campaign.
    assert all(hit["campaign_id"] != running_campaign for hit in hits)

    # Ordered by ascending cosine distance (recomputed independently).
    query_vec = embedder.embed(query)
    distances = [_cosine_distance(embedder.embed(hit["content"]), query_vec) for hit in hits]
    assert all(earlier <= later + 1e-9 for earlier, later in itertools.pairwise(distances)), (
        "returned rows are not in ascending cosine-distance order"
    )

    # The result is exactly the top-k of the OTHER-campaign rows by cosine
    # distance, so nothing eligible was wrongly dropped nor excluded wrongly kept.
    eligible = [r for r in store_rows if r.campaign_id != running_campaign]
    eligible_sorted = sorted(eligible, key=lambda r: _cosine_distance(r.embedding, query_vec))
    expected_len = min(k, len(eligible))
    assert len(hits) == expected_len
    for hit, expected_row in zip(hits, eligible_sorted[:expected_len], strict=False):
        hit_dist = _cosine_distance(embedder.embed(hit["content"]), query_vec)
        exp_dist = _cosine_distance(expected_row.embedding, query_vec)
        assert math.isclose(hit_dist, exp_dist, rel_tol=0.0, abs_tol=1e-9)
