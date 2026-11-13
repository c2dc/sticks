"""Integration tests for the ``ImageResolver`` catalog-before-internet order + network containment.

Concrete cases (no Hypothesis) exercising the ``environment`` phase (design
Component 11) of :mod:`app.services.environment.image_resolver`. Each case proves
a distinct face of the Req. 5.4 / 14.2 / 14.4 / 14.5 contract:

* **Catalog-before-internet ordering (Req. 5.4).** With a seeded
  ``image_catalog`` row AND an injected Docker Hub lookup that would also
  resolve, ``resolve`` prefers the catalog (``source == "image_catalog"``) and
  never consults the online lookup. Docker Hub online is reached only when the
  catalog has no adequate candidate (``source == "docker_hub"``).

* **Semantic match first (Req. 5.4).** When a catalog semantic matcher is
  injected and names a candidate image confirmed by the relational catalog,
  ``resolve`` returns it with ``source == "image_catalog_semantic"``, ahead of
  the plain relational match. A semantic candidate that is NOT confirmed
  relationally is ignored (no hallucinated image), and the resolver falls
  through to the relational catalog.

* **Network containment (Req. 14.5 — real security guarantee).** The online
  Docker Hub access is a backend-provisioning-layer concern injected into the
  ``ImageResolver`` and never reachable from inside the contained lab. A
  ``_SpyLookup`` records every call: when the catalog covers the need the spy is
  never invoked, and an offline-by-construction resolver (no lookup) resolves
  purely from the catalog with ZERO online attempts.

* **ex-Property 22 — ``detect_target_os`` coherence (Req. 14.2).** Dominant
  Windows platform/commands → ``"windows"``; Linux/empty → ``"linux"``.

Test infra
----------
The relational path uses an ephemeral SQLite ``Session`` (temp file +
``Base.metadata.create_all``, the same pattern as the sibling tests) seeded with
``ImageCatalog`` rows via the ORM. No network is touched: the only possible
egress path is the injected lookup, which is always a test double. The semantic
matcher is likewise a small in-memory double returning fixed candidate lists.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import ImageCatalog
from app.services.environment.image_resolver import (
    SOURCE_DOCKER_HUB,
    SOURCE_IMAGE_CATALOG,
    SOURCE_IMAGE_CATALOG_SEMANTIC,
    TARGET_OS_LINUX,
    TARGET_OS_WINDOWS,
    ImageNeed,
    ImageResolver,
    ImageUnavailableError,
    build_catalog_semantic_matcher,
    detect_target_os,
)

# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterwards. Mirrors the temp-file SQLite +
    ``Base.metadata.create_all`` pattern of the sibling tests.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="image_resolver_"))
    db_path = tmp_dir / "image_resolver.db"
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


@pytest.fixture()
def db_session() -> Any:
    """Yield an ephemeral SQLite session, disposed after the test."""
    session, db_path, engine = _build_ephemeral_session()
    try:
        yield session
    finally:
        _dispose(session, engine, db_path)


# ---------------------------------------------------------------------------
# Seeding helper (image_catalog rows via the ORM).
# ---------------------------------------------------------------------------


def _seed_image(
    session: Session,
    *,
    image: str,
    target_os: str,
    services: list[str] | None = None,
) -> None:
    """Insert one ``image_catalog`` row (the primary catalog source, Req. 5.4)."""
    session.add(ImageCatalog(image=image, target_os=target_os, services=services))
    session.flush()


# ---------------------------------------------------------------------------
# Test doubles for the injectable Docker Hub lookup (the provisioning layer).
# ---------------------------------------------------------------------------


class _SpyLookup:
    """A Docker Hub lookup that records every call — the egress probe.

    The injected lookup is the ONLY path by which the resolver could reach the
    Docker Hub online registry. Recording each invocation lets a test assert,
    honestly, that a catalog-covered or offline-by-construction resolver makes
    ZERO online attempts (Req. 5.4 catalog-first / Req. 14.5 containment).
    """

    def __init__(self, result: str | None) -> None:
        self._result = result
        self.calls: list[ImageNeed] = []

    def __call__(self, need: ImageNeed) -> str | None:
        self.calls.append(need)
        return self._result


class _ConnectionErrorLookup:
    """A Docker Hub lookup that raises a connection error — i.e. offline.

    Models the production provisioning layer when Docker Hub is unreachable
    (timeout / connection refused). The resolver treats any raised error as
    "unavailable" when it is reached as the last-resort fallback (Req. 5.4).
    """

    def __init__(self) -> None:
        self.calls: list[ImageNeed] = []

    def __call__(self, need: ImageNeed) -> str | None:
        self.calls.append(need)
        raise ConnectionError("Docker Hub unreachable (simulated offline)")


class _StaticSemanticMatcher:
    """A catalog semantic matcher returning a fixed candidate list.

    Stands in for the Vector_Store-backed
    :func:`build_catalog_semantic_matcher` so the ordering contract can be tested
    without an embedder. Records each call so a test can assert it was (or was
    not) consulted.
    """

    def __init__(self, candidates: list[str]) -> None:
        self._candidates = candidates
        self.calls: list[ImageNeed] = []

    def __call__(self, need: ImageNeed) -> list[str]:
        self.calls.append(need)
        return list(self._candidates)


# ---------------------------------------------------------------------------
# 1. Catalog-before-internet ordering (Req. 5.4).
# ---------------------------------------------------------------------------


def test_resolve_prefers_catalog_over_docker_hub_online(db_session: Session) -> None:
    """Catalog FIRST: a seeded row wins even when the online lookup WOULD resolve (Req. 5.4)."""
    catalog_image = "catalog/linux:1.0"
    _seed_image(db_session, image=catalog_image, target_os=TARGET_OS_LINUX)

    # An online lookup that WOULD resolve — but must never be consulted because
    # the catalog already covers the need (catalog before internet).
    lookup = _SpyLookup(result="dockerhub/linux:latest")
    resolver = ImageResolver(db_session, docker_hub_lookup=lookup)

    need = ImageNeed(target_os=TARGET_OS_LINUX, services=("ssh",))
    resolution = resolver.resolve(need)

    assert resolution.image == catalog_image
    assert resolution.source == SOURCE_IMAGE_CATALOG
    assert resolution.target_os == TARGET_OS_LINUX
    # The online lookup was NOT reached — the catalog resolved first (Req. 5.4).
    assert lookup.calls == []


def test_resolve_falls_back_to_docker_hub_when_catalog_has_no_candidate(
    db_session: Session,
) -> None:
    """No catalog candidate → Docker Hub online is reached as the last resort (Req. 5.4)."""
    # Catalog has only a linux row; the need is for windows, so the catalog has
    # no adequate candidate and the online lookup becomes the fallback.
    _seed_image(db_session, image="catalog/linux:1.0", target_os=TARGET_OS_LINUX)

    online_image = "dockerhub/windows:ltsc2022"
    lookup = _SpyLookup(result=online_image)
    resolver = ImageResolver(db_session, docker_hub_lookup=lookup)

    need = ImageNeed(target_os=TARGET_OS_WINDOWS)
    resolution = resolver.resolve(need)

    # Fell through to Docker Hub only because the catalog could not cover it.
    assert len(lookup.calls) == 1
    assert resolution.image == online_image
    assert resolution.source == SOURCE_DOCKER_HUB
    assert resolution.target_os == TARGET_OS_WINDOWS


def test_resolve_raises_when_neither_catalog_nor_online_covers(
    db_session: Session,
) -> None:
    """Neither the catalog nor Docker Hub covers the need → ImageUnavailableError."""
    # Catalog has only a linux row; the need is for windows and the online
    # lookup is unavailable (raises), so nothing covers it.
    _seed_image(db_session, image="catalog/linux:1.0", target_os=TARGET_OS_LINUX)
    resolver = ImageResolver(db_session, docker_hub_lookup=_ConnectionErrorLookup())

    with pytest.raises(ImageUnavailableError):
        resolver.resolve(ImageNeed(target_os=TARGET_OS_WINDOWS))


# ---------------------------------------------------------------------------
# 2. Semantic match FIRST, confirmed relationally (Req. 5.4).
# ---------------------------------------------------------------------------


def test_semantic_match_wins_when_confirmed_relationally(
    db_session: Session,
) -> None:
    """A semantic candidate confirmed by the catalog resolves as image_catalog_semantic (Req. 5.4)."""
    # Two cataloged linux rows: the plain relational match (alpha order) would
    # pick "catalog/linux-a"; the semantic matcher names "catalog/linux-b", which
    # is confirmed relationally and must win.
    _seed_image(db_session, image="catalog/linux-a:1.0", target_os=TARGET_OS_LINUX)
    _seed_image(db_session, image="catalog/linux-b:1.0", target_os=TARGET_OS_LINUX)

    matcher = _StaticSemanticMatcher(candidates=["catalog/linux-b:1.0"])
    online = _SpyLookup(result="dockerhub/linux:latest")
    resolver = ImageResolver(db_session, catalog_matcher=matcher, docker_hub_lookup=online)

    resolution = resolver.resolve(ImageNeed(target_os=TARGET_OS_LINUX))

    assert resolution.image == "catalog/linux-b:1.0"
    assert resolution.source == SOURCE_IMAGE_CATALOG_SEMANTIC
    assert len(matcher.calls) == 1
    # Catalog resolved semantically — the online lookup was never reached.
    assert online.calls == []


def test_unconfirmed_semantic_candidate_is_ignored_and_falls_through(
    db_session: Session,
) -> None:
    """A semantic candidate not in the relational catalog is ignored (no hallucination, Req. 5.4)."""
    catalog_image = "catalog/linux:1.0"
    _seed_image(db_session, image=catalog_image, target_os=TARGET_OS_LINUX)

    # The matcher names an image that is NOT cataloged for this OS; it must be
    # rejected and the resolver falls through to the relational catalog row.
    matcher = _StaticSemanticMatcher(candidates=["ghost/linux:does-not-exist"])
    resolver = ImageResolver(db_session, catalog_matcher=matcher)

    resolution = resolver.resolve(ImageNeed(target_os=TARGET_OS_LINUX))

    assert resolution.image == catalog_image
    assert resolution.source == SOURCE_IMAGE_CATALOG


def test_build_catalog_semantic_matcher_yields_catalog_sourced_images() -> None:
    """The Vector_Store-backed matcher keeps only catalog hits matching the OS (Req. 5.4)."""

    class _FakeMemory:
        """A minimal ExperienceMemory double returning fixed retrieval hits."""

        def __init__(self, hits: list[dict[str, Any]]) -> None:
            self._hits = hits
            self.calls: list[dict[str, Any]] = []

        def retrieve(self, *, campaign_id: str, query: str, k: int) -> list[dict]:
            self.calls.append({"campaign_id": campaign_id, "query": query, "k": k})
            return self._hits

    hits = [
        # A non-catalog affinity — ignored (wrong source).
        {"metadata": {"source": "command_affinity", "image": "x/y:1"}},
        # A catalog hit for the wrong OS — ignored.
        {
            "metadata": {
                "source": "docker_image_catalog",
                "image": "catalog/win:1",
                "target_os": "windows",
            }
        },
        # A catalog hit for the right OS — kept.
        {
            "metadata": {
                "source": "docker_image_catalog",
                "image": "catalog/linux:1",
                "target_os": "linux",
            }
        },
    ]
    memory = _FakeMemory(hits)
    matcher = build_catalog_semantic_matcher(memory, campaign_id="run-1")

    result = matcher(ImageNeed(target_os=TARGET_OS_LINUX, services=("ssh",)))

    assert result == ["catalog/linux:1"]
    assert memory.calls[0]["campaign_id"] == "run-1"


# ---------------------------------------------------------------------------
# 3. Network containment (Req. 14.5 — real security guarantee).
# ---------------------------------------------------------------------------


def test_offline_by_construction_resolver_never_attempts_network(
    db_session: Session,
) -> None:
    """A resolver built WITHOUT a lookup makes ZERO online attempts (Req. 14.5).

    Containment reasoning: the lab networks are ``internal: true`` (no egress),
    so the online Docker Hub call is a backend-provisioning-layer concern that is
    injected into the resolver. When there is no provisioning-layer egress the
    resolver is built offline-by-construction (``docker_hub_lookup=None``) and
    resolves purely from the catalog.
    """
    catalog_image = "catalog/linux:1.0"
    _seed_image(db_session, image=catalog_image, target_os=TARGET_OS_LINUX)

    # Offline by construction: no provisioning-layer lookup injected.
    resolver = ImageResolver(db_session, docker_hub_lookup=None)

    resolution = resolver.resolve(ImageNeed(target_os=TARGET_OS_LINUX))

    # The resolver has no outbound seam at all — the lookup is None.
    assert resolver._docker_hub_lookup is None
    # It still resolves, purely from the catalog.
    assert resolution.image == catalog_image
    assert resolution.source == SOURCE_IMAGE_CATALOG


def test_injected_lookup_is_the_only_path_to_docker_hub(
    db_session: Session,
) -> None:
    """The injected lookup is the SOLE egress seam, reached only when the catalog cannot cover (Req. 5.4, 14.5).

    Containment reasoning: we build two resolvers.

    * With a spy lookup injected and NO catalog candidate (empty catalog) the spy
      IS consulted — proving the injected callable is the resolver's only
      outbound path, reached as the last resort.
    * With the same spy injected but a seeded catalog row, the spy records ZERO
      calls — proving the catalog-first order keeps egress closed when the
      catalog already covers the need.
    """
    need = ImageNeed(target_os=TARGET_OS_LINUX)

    # No catalog candidate: the injected spy is the single egress seam reached.
    injected_spy = _SpyLookup(result="dockerhub/linux:latest")
    empty_resolver = ImageResolver(db_session, docker_hub_lookup=injected_spy)
    empty_resolver.resolve(need)
    assert len(injected_spy.calls) == 1  # the injected seam was used as fallback

    # Catalog covers the need: the online seam must NOT be reached.
    _seed_image(db_session, image="catalog/linux:1.0", target_os=TARGET_OS_LINUX)
    covered_spy = _SpyLookup(result="dockerhub/linux:latest")
    covered_resolver = ImageResolver(db_session, docker_hub_lookup=covered_spy)
    resolution = covered_resolver.resolve(need)
    assert covered_spy.calls == []  # catalog-first keeps egress closed
    assert resolution.source == SOURCE_IMAGE_CATALOG


# ---------------------------------------------------------------------------
# 4. ex-Property 22 — detect_target_os coherence (Req. 14.2).
# ---------------------------------------------------------------------------


def test_detect_target_os_windows_from_dominant_platform() -> None:
    """A Windows platform token selects target_os == windows (Req. 14.2)."""
    assert detect_target_os(platforms=["windows"]) == TARGET_OS_WINDOWS
    assert detect_target_os(platforms=["linux", "win64"]) == TARGET_OS_WINDOWS


def test_detect_target_os_windows_from_dominant_commands() -> None:
    """A Windows command-shell token selects target_os == windows (Req. 14.2)."""
    assert detect_target_os(commands=["powershell -enc ZQBj"]) == TARGET_OS_WINDOWS
    assert detect_target_os(commands=["cmd /c whoami"]) == TARGET_OS_WINDOWS


def test_detect_target_os_linux_from_linux_or_empty() -> None:
    """Linux/empty signals default to target_os == linux (total, Req. 14.2)."""
    assert detect_target_os(platforms=["linux"]) == TARGET_OS_LINUX
    assert detect_target_os(commands=["ls -la /tmp"]) == TARGET_OS_LINUX
    # No signal at all is still total → linux.
    assert detect_target_os() == TARGET_OS_LINUX
    assert detect_target_os(platforms=[], commands=[]) == TARGET_OS_LINUX


# ---------------------------------------------------------------------------
# 5. Relational catalog service coverage (Req. 5.4, 14.4).
# ---------------------------------------------------------------------------


def test_catalog_prefers_row_covering_requested_services(
    db_session: Session,
) -> None:
    """With services named, the catalog match prefers a row covering them (Req. 5.4, 14.4)."""
    _seed_image(
        db_session,
        image="catalog/linux-bare:1.0",
        target_os=TARGET_OS_LINUX,
        services=[],
    )
    _seed_image(
        db_session,
        image="catalog/linux-web:1.0",
        target_os=TARGET_OS_LINUX,
        services=["nginx", "ssh"],
    )

    resolver = ImageResolver(db_session, docker_hub_lookup=None)
    resolution = resolver.resolve(ImageNeed(target_os=TARGET_OS_LINUX, services=("nginx",)))

    assert resolution.image == "catalog/linux-web:1.0"
    assert resolution.source == SOURCE_IMAGE_CATALOG
