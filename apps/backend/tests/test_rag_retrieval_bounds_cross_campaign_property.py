"""Property test for RAG retrieval bounds and cross-campaign exclusion (task 5.3).

# Feature: ai-curation-agents, Property 5: RAG retrieval is cross-campaign and
# bounded — for ANY populated experience memory and ANY retrieval with parameter
# ``k`` in 1..100, :meth:`app.services.ai.memory.ExperienceMemory.retrieve`
# returns AT MOST ``k`` snippets, ordered by DESCENDING cosine similarity (i.e.
# ascending cosine distance) to the query, and contains NO snippet whose
# ``campaign_id`` equals the running campaign's ``campaign_id``.

Validates: Requirements 5.3, 5.5

Why a fake embedder + a cosine-modelling vector stub
----------------------------------------------------
The property under test (Req. 5.3, 5.5) is a property of the *retrieval query*
``ExperienceMemory.retrieve`` builds — the parameterized cosine top-k with the
current-campaign exclusion — not of PostgreSQL/pgvector itself. The production
``retrieve`` embeds the query through the injected ``Embedder`` Protocol and runs

    select(ExperienceMemoryRow)
      .where(ExperienceMemoryRow.campaign_id != :campaign_id)
      .order_by(ExperienceMemoryRow.embedding.cosine_distance(:query_embedding))
      .limit(:k)

against ``session.execute(...).scalars().all()``. The ephemeral SQLite the unit
suite uses cannot evaluate pgvector's ``<=>`` cosine operator, so a live Postgres
would be the only "real" backend — which the project's suite never spins up.

Instead this test exercises the REAL ``ExperienceMemory.retrieve`` (its bounds
check, its lazy embedder resolution, and the exact ``select`` it assembles) and
substitutes two fakes that stand in for the embedding backend and the pgvector
store:

* a FAKE ``Embedder`` that maps text to a deterministic vector (so both the
  stored rows and the query embed reproducibly, with no network); and
* a FAKE ``Session`` whose ``execute`` *interprets the production statement*:
  it reads the bound ``campaign_id``, query embedding and ``limit`` straight off
  the compiled ``select`` the real ``retrieve`` built, then applies exactly what
  pgvector would — drop rows of the running campaign, order the remainder by true
  cosine distance to the query embedding, and cut to ``k``.

Because the stub derives its answer from the statement the code under test
produced (rather than being handed the parameters directly), the assertions below
are really checking the query ``retrieve`` emits: if ``retrieve`` forgot the
``WHERE campaign_id != :campaign_id`` exclusion, omitted the cosine ``ORDER BY``,
or dropped the ``LIMIT``, the interpreted result would violate the property and
the test would fail. The fakes model the store faithfully; they do not encode the
expected answer.

How the property is driven
--------------------------
Hypothesis generates, per example:

* a pool of experience rows across MULTIPLE campaign ids, each with arbitrary
  textual ``content`` (the "random memory contents"); the running campaign id is
  drawn from that same id space so cross-campaign exclusion is actually exercised
  (the pool frequently contains rows OF the running campaign that must be
  filtered out);
* an arbitrary query string; and
* a random ``k`` in ``[1, 100]``.

The property asserts the three guarantees of Property 5 against the result of the
real ``retrieve``:

* **Bounded (Req. 5.3).** ``len(result) <= k``.
* **Cosine-ordered (Req. 5.3).** The returned snippets are in non-decreasing
  cosine-distance order to the query embedding (equivalently non-increasing cosine
  similarity), recomputed independently here from the fake embedder.
* **Cross-campaign (Req. 5.5).** No returned snippet carries the running
  campaign's ``campaign_id``; and the result is exactly the top-``k`` of the
  OTHER-campaign rows by cosine distance (nothing eligible was wrongly dropped).

Everything runs in-process: no network, no Docker, no PostgreSQL.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.ai.memory import ExperienceMemory

# ---------------------------------------------------------------------------
# Fakes: a deterministic embedder and a cosine-modelling vector-store session.
# ---------------------------------------------------------------------------

# Fixed, small embedding width: large enough that distinct texts separate in the
# vector space, small enough to keep generation fast. The property is width-
# agnostic, so any fixed dimension exercises it.
_EMBED_DIM = 8


class _FakeEmbedder:
    """Deterministic text->vector map standing in for the SEPINF embedder.

    Structurally satisfies the ``@runtime_checkable`` ``Embedder`` Protocol (an
    ``embed(text) -> list[float]`` method). The mapping is a stable hash spread
    across ``_EMBED_DIM`` dimensions, so the SAME text always embeds to the SAME
    vector — both when a row is stored and when it is used as the query — while
    DIFFERENT texts generally land at different cosine distances. No network, no
    model weights (Req. 5.6-adjacent: learning is never via weight changes).
    """

    def embed(self, text: str) -> list[float]:
        vec: list[float] = []
        for dim in range(_EMBED_DIM):
            # A cheap, deterministic, text-and-dimension-dependent component.
            h = hash((text, dim))
            # Map the hash into a bounded, non-trivial float component.
            vec.append(((h % 1000) / 500.0) - 1.0)
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
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        # Matches the fake embedder's zero-vector guard: should not occur.
        return 1.0
    return 1.0 - (dot / (na * nb))


class _Row:
    """A minimal stand-in for an ``experience_memory`` row.

    Carries exactly the attributes ``ExperienceMemory._row_to_hit`` projects
    (``content``, ``meta``, ``campaign_id``, ``stage``, ``outcome``) plus the
    ``embedding`` the fake store orders by.
    """

    def __init__(
        self, *, campaign_id: str, content: str, embedding: list[float]
    ) -> None:
        self.campaign_id = campaign_id
        self.content = content
        self.embedding = embedding
        self.meta = {"content_len": len(content)}
        self.stage = "operationalization"
        self.outcome = "success"


class _ScalarResult:
    """Tiny result wrapper mirroring ``Result.scalars().all()`` used by retrieve."""

    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def scalars(self) -> "_ScalarResult":
        return self

    def all(self) -> list[_Row]:
        return list(self._rows)


class _FakeVectorSession:
    """FAKE session that interprets the production cosine top-k ``select``.

    ``ExperienceMemory.retrieve`` hands this session the real SQLAlchemy
    ``Select`` it assembled. Rather than hitting a database, the fake compiles the
    statement and reads the three bound parameters off it — the excluded
    ``campaign_id``, the query embedding, and the ``LIMIT`` — then reproduces what
    pgvector would return: the OTHER-campaign rows, ordered by ascending cosine
    distance to the query embedding, truncated to the limit.

    Deriving the answer from the emitted statement (not from parameters passed in
    by the test) is what makes this a faithful check of the query ``retrieve``
    builds: a missing exclusion, order-by or limit in the production code would
    change the interpreted result and fail the property.
    """

    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def execute(self, stmt: Any) -> _ScalarResult:
        params = stmt.compile().params

        # Pull the bound values out by shape rather than by fragile bind-name:
        # the query embedding is the one list[float] parameter, the excluded
        # campaign id is the str parameter, and the limit is the int parameter.
        excluded_campaign: Optional[str] = None
        query_embedding: Optional[list[float]] = None
        limit: Optional[int] = None
        for value in params.values():
            if isinstance(value, list):
                query_embedding = value
            elif isinstance(value, bool):
                # Guard: bool is an int subclass; none expected, but never treat
                # a boolean as the integer limit.
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

        # Model pgvector: exclude the running campaign, order by cosine distance
        # to the query embedding, then cut to the limit. ``sorted`` is stable, so
        # rows at an equal distance keep their insertion order (a deterministic,
        # defensible tie-break the assertions mirror).
        eligible = [r for r in self._rows if r.campaign_id != excluded_campaign]
        eligible.sort(key=lambda r: _cosine_distance(r.embedding, query_embedding))
        return _ScalarResult(eligible[:limit])


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A small, fixed campaign-id alphabet so the generated row pool and the running
# campaign id draw from the SAME space — this makes cross-campaign exclusion
# bite (the pool routinely holds rows OF the running campaign to be filtered).
_CAMPAIGN_IDS = ["c0", "c1", "c2", "c3", "c4"]

_campaign_ids = st.sampled_from(_CAMPAIGN_IDS)

# Arbitrary textual memory contents. Distinct contents embed to distinct vectors
# via the fake embedder, producing a non-trivial cosine ordering.
_contents = st.text(min_size=0, max_size=40)


def _memory_rows() -> st.SearchStrategy[list[tuple[str, str]]]:
    """A pool of (campaign_id, content) pairs across multiple campaigns.

    Up to 60 rows keeps examples varied while fast; an empty pool is allowed so
    the "fewer than k eligible rows" case (result strictly shorter than k) is
    also exercised.
    """
    return st.lists(
        st.tuples(_campaign_ids, _contents),
        min_size=0,
        max_size=60,
    )


# ---------------------------------------------------------------------------
# Property 5: retrieval is bounded by k, cosine-ordered, and excludes the
# running campaign (Requirements 5.3, 5.5).
#
# ``deadline=None``: compiling the SQLAlchemy select and recomputing cosine
# distances per example is fast but variable on a loaded machine; the property's
# correctness does not depend on timing, so the per-example deadline is lifted to
# avoid flaky failures.
# ---------------------------------------------------------------------------
# Feature: ai-curation-agents, Property 5: RAG retrieval is cross-campaign and
# bounded.
@settings(max_examples=40, deadline=None)
@given(
    rows=_memory_rows(),
    running_campaign=_campaign_ids,
    query=_contents,
    k=st.integers(min_value=1, max_value=100),
)
def test_property5_retrieval_is_bounded_cosine_ordered_and_cross_campaign(
    rows: list[tuple[str, str]],
    running_campaign: str,
    query: str,
    k: int,
) -> None:
    """Retrieve returns <= k cosine-ordered snippets, none of the running campaign.

    # Feature: ai-curation-agents, Property 5: RAG retrieval is cross-campaign and
    # bounded.
    Validates: Requirements 5.3, 5.5
    """
    embedder = _FakeEmbedder()

    # Build the row pool with each row's embedding derived from its content by the
    # SAME fake embedder the query will use, so cosine ordering is meaningful.
    store_rows = [
        _Row(campaign_id=cid, content=content, embedding=embedder.embed(content))
        for cid, content in rows
    ]
    session = _FakeVectorSession(store_rows)

    memory = ExperienceMemory(session, embedder=embedder)

    # Exercise the REAL retrieve (bounds check + lazy embed + the cosine top-k
    # select). ``k`` is in range, so no RetrievalParameterError is raised here;
    # the out-of-range rejection is covered by the sibling unit test (task 5.4).
    hits = memory.retrieve(campaign_id=running_campaign, query=query, k=k)

    # --- Bounded (Req. 5.3): at most k snippets. ---
    assert len(hits) <= k

    # --- Cross-campaign (Req. 5.5): none of the running campaign. ---
    assert all(hit["campaign_id"] != running_campaign for hit in hits)

    # --- Cosine-ordered (Req. 5.3): non-decreasing cosine distance to the query
    # (equivalently non-increasing cosine similarity), recomputed independently. ---
    query_vec = embedder.embed(query)
    distances = [
        _cosine_distance(embedder.embed(hit["content"]), query_vec) for hit in hits
    ]
    assert all(
        earlier <= later + 1e-9
        for earlier, later in zip(distances, distances[1:])
    ), "returned snippets are not in ascending cosine-distance (top-similarity) order"

    # --- Completeness of the top-k (Req. 5.3, 5.5): the result is exactly the
    # top-k of the eligible (other-campaign) rows by cosine distance, so nothing
    # eligible was wrongly dropped and nothing excluded wrongly slipped in. ---
    eligible = [r for r in store_rows if r.campaign_id != running_campaign]
    eligible_sorted = sorted(
        eligible, key=lambda r: _cosine_distance(r.embedding, query_vec)
    )
    expected_len = min(k, len(eligible))
    assert len(hits) == expected_len
    # Compare by the stored cosine distance of each position: the returned
    # snippet at position i matches the i-th eligible row's distance (stable
    # tie-break preserved), which pins both bound and order simultaneously.
    for hit, expected_row in zip(hits, eligible_sorted[:expected_len]):
        hit_dist = _cosine_distance(embedder.embed(hit["content"]), query_vec)
        exp_dist = _cosine_distance(expected_row.embedding, query_vec)
        assert math.isclose(hit_dist, exp_dist, rel_tol=0.0, abs_tol=1e-9)
