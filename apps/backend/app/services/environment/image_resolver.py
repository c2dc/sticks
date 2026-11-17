"""ImageResolver — ``environment`` image resolution, Image_Catalog → Docker Hub (Req. 5.4, 14).

Phase ``environment`` (design Component 11). This module is the env agent's
image-resolution seam: it detects the campaign's ``Target_OS`` from the concrete
operationalization commands/technique platforms and resolves a container image
for it, consulting the **Docker_Image_Catalog FIRST** (semantic match via the
Vector_Store, then the relational ``image_catalog``) and only recurring to
**Docker Hub online** through the application's provisioning layer when no
adequate candidate exists in the catalog.

Catalog-before-internet ordering (Req. 5.4). The Docker_Image_Catalog is
ingested and vectorized in the setup Ingestion (installation and first-run
ingestion, not the ``input`` phase) (``metadata.source =
"docker_image_catalog"`` under the ``environment_affinity`` kind), so the
``environment`` phase can resolve candidate images offline and depend less on the
internet at run time. The resolver therefore queries the catalog first — a
semantic match over the vectorized descriptions, then a relational match over
``image_catalog`` — and falls back to the live Docker Hub registry only when the
catalog yields no adequate candidate for the need (Req. 5.4). This inverts the
earlier Docker-Hub-first draft (Req. 14.3): the catalog is now the primary
source, the online registry the last resort.

Containment (Req. 14.5). The Docker Hub lookup is a **backend provisioning-layer
concern**, never performed from inside the contained lab. The lab networks are
``internal: true`` (no egress), so an online registry call from within the lab
would be a design error barred by the network topology. Accordingly the online
lookup is an **injectable** callable (:class:`DockerHubLookup`): production wires
it in the backend provisioning layer, and tests (or explicit offline operation)
double or disable it. When no lookup is injected the resolver is *offline by
construction* and resolves straight from the catalog.

Resolution order (Req. 5.4, 14.4, 14.5):

1. **Docker_Image_Catalog — semantic FIRST.** When a :class:`CatalogSemanticMatcher`
   is injected, query the vectorized catalog descriptions
   (``metadata.source="docker_image_catalog"``) for the need and, if it names a
   candidate image whose relational ``image_catalog`` row covers the need's
   ``target_os`` (and services, when present), resolve it with
   ``source="image_catalog_semantic"``.
2. **Docker_Image_Catalog — relational next.** Otherwise read the relational
   :class:`~app.models.domain.ImageCatalog` filtered by ``target_os``, preferring
   a row whose ``services`` cover the need, and resolve with
   ``source="image_catalog"``.
3. **Docker Hub online — fallback last.** Only when the catalog yields no
   adequate candidate, and a Docker Hub lookup is injected, call it through the
   provisioning layer; a returned image resolves with ``source="docker_hub"``. A
   raised error or empty result is treated as "unavailable".
4. **Nothing covers the need →** raise :class:`ImageUnavailableError` naming the
   requested ``target_os`` (and the services it needed), so the env agent can
   treat it as a phase insufficiency the evaluator may route an environment
   refinement for (design §"Geração de ambiente").

Conventions carried over from the other Session-injected services
(:class:`~app.services.provisioning_map_service.ProvisioningMapService`): the ORM
model is resolved lazily inside the method, so importing this module performs no
I/O and has no circular import with :mod:`app.models` (Req. 1.7). The service does
not open or close the ``Session``; it only reads from the catalog.

_Requirements: 5.4, 14.1, 14.2, 14.4, 14.5_
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# The two target operating systems the resolver understands — kept consistent
# with the Provisioning_Map derivation (``image_catalog``/``provisioning_map``
# ``target_os`` column): ``linux`` | ``windows``.
TARGET_OS_LINUX = "linux"
TARGET_OS_WINDOWS = "windows"

# Executor/platform tokens that map to the Windows target OS. Everything else
# (``linux``, ``darwin``, ``nix``, unknown, …) resolves to the default Linux
# target, so ``detect_target_os`` is **total**: every input yields a value in
# ``{linux, windows}``. Kept consistent with
# :data:`ProvisioningMapService._WINDOWS_PLATFORM_TOKENS` (windows tokens →
# windows; else linux).
_WINDOWS_PLATFORM_TOKENS = frozenset(
    {"windows", "win", "win32", "win64", "powershell", "psh", "cmd"}
)


@dataclass(frozen=True)
class ImageNeed:
    """What an image must satisfy for a campaign (input to :meth:`ImageResolver.resolve`).

    A small, typed carrier for the image-resolution request: the detected
    ``target_os`` (``linux`` | ``windows``) and, optionally, the services the
    image must provide (e.g. ``("ssh", "nginx")``) so a Docker Hub lookup can
    score candidates and the catalog fallback can prefer a row covering them. The
    ``target_os`` is the only field the offline catalog fallback filters on; the
    ``services`` refine the choice when present.

    Attributes:
        target_os: Target OS the image must run on: ``linux`` | ``windows``.
        services: Services/ports the image must provide; empty when unspecified.
    """

    target_os: str
    services: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImageResolution:
    """Resolved image + provenance (output of :meth:`ImageResolver.resolve`).

    Mirrors the design Component 11 dataclass: the resolved ``image`` reference,
    the ``source`` it came from (``image_catalog_semantic`` when a semantic match
    over the vectorized catalog resolved it, ``image_catalog`` on the relational
    catalog match, ``docker_hub`` when the online fallback resolved it) and the
    ``target_os`` it was resolved for — so the caller can record where the image
    came from in the Audit_Trace.
    """

    image: str
    source: str  # "image_catalog_semantic" | "image_catalog" | "docker_hub"
    target_os: str  # "linux" | "windows"


# Source tokens for :attr:`ImageResolution.source`.
SOURCE_DOCKER_HUB = "docker_hub"
SOURCE_IMAGE_CATALOG = "image_catalog"
SOURCE_IMAGE_CATALOG_SEMANTIC = "image_catalog_semantic"


class ImageUnavailableError(RuntimeError):
    """Raised when neither Docker Hub nor the catalog can cover an :class:`ImageNeed`.

    Names the requested ``target_os`` (and the services it needed) so the env
    agent can surface the phase insufficiency and the evaluator can route an
    environment refinement (design §"Geração de ambiente", Req. 14.4).
    """

    def __init__(self, need: ImageNeed) -> None:
        self.need = need
        services = ", ".join(need.services) if need.services else "<none>"
        super().__init__(
            "No image available for target_os "
            f"{need.target_os!r} (services: {services}) from the image_catalog "
            "(semantic or relational) or the Docker Hub online fallback."
        )


@runtime_checkable
class CatalogSemanticMatcher(Protocol):
    """Semantic match over the vectorized Docker_Image_Catalog (Req. 5.4).

    A callable the ``environment`` phase provides to retrieve candidate image
    references by **similarity** over the catalog descriptions vectorized at
    setup (``metadata.source="docker_image_catalog"`` under the
    ``environment_affinity`` kind, see
    :class:`~app.services.docker_catalog_ingest_service.DockerCatalogIngestService`).
    Given an :class:`ImageNeed`, return the candidate image references ranked by
    semantic similarity (most similar first); the resolver then confirms a
    candidate against the relational ``image_catalog`` before resolving it, so a
    hallucinated or stale embedding never yields an image that is not actually
    cataloged. Returning an empty sequence (or raising) means "no semantic
    candidate", and the resolver falls through to the relational catalog match.

    This matcher is **injectable** and typically backed by
    :meth:`~app.services.ai.memory.ExperienceMemory.retrieve`; it is optional, so
    a resolver built without it simply skips the semantic step and resolves
    relationally first.
    """

    def __call__(self, need: ImageNeed) -> Iterable[str]:  # pragma: no cover - protocol
        ...


@runtime_checkable
class DockerHubLookup(Protocol):
    """The injectable Docker Hub lookup — the application provisioning layer (Req. 14.5).

    A callable the backend provisioning layer provides to resolve an image from
    Docker Hub online. It is **never** invoked from inside the contained lab: the
    lab networks are ``internal: true`` (no egress), so the online call lives in
    the backend. Return an image reference (``str``) when a compatible image is
    found online; return ``None`` (or raise) when the lookup is unavailable
    (offline / no connection / no match) so the resolver falls back to the
    catalog.
    """

    def __call__(self, need: ImageNeed) -> str | None:  # pragma: no cover - protocol
        ...


def detect_target_os(
    *,
    platforms: Iterable[str] | None = None,
    commands: Iterable[str] | None = None,
) -> str:
    """Detect the ``Target_OS`` (``linux`` | ``windows``) from the operationalization (Req. 14.2).

    Totally classifies the campaign's target OS from the concrete
    operationalization commands and/or the technique/executor platforms: returns
    :data:`TARGET_OS_WINDOWS` when any Windows token appears among the platforms
    or the command text (consistent with
    :meth:`ProvisioningMapService._derive_target_os` — windows tokens → windows;
    else linux), otherwise defaults to :data:`TARGET_OS_LINUX`. The default makes
    the detection **total**: with no signal at all it still yields a value in
    ``{linux, windows}``.

    Both inputs are optional and defensively iterated so a malformed entry never
    aborts detection:

    * ``platforms`` — the executor/technique platform tokens (e.g. the
      ``abilities.executors[].platform`` values the operationalization carries).
      A single Windows platform token is a strong signal and selects Windows.
    * ``commands`` — the concrete command strings (the operationalization phase output, Req.
      14.1). Scanned for Windows command-shell tokens (``powershell``, ``cmd``,
      …) so a campaign whose commands are clearly Windows is detected even when
      the platform tokens are absent.

    Args:
        platforms: Executor/technique platform tokens; ``None``/empty when
            unavailable.
        commands: Concrete operationalization command strings; ``None``/empty
            when unavailable.

    Returns:
        ``"windows"`` when a Windows signal is present, else ``"linux"``.
    """
    for platform in _clean_tokens(platforms):
        if platform.lower() in _WINDOWS_PLATFORM_TOKENS:
            return TARGET_OS_WINDOWS

    for command in _clean_tokens(commands):
        lowered = command.lower()
        if any(token in lowered for token in _WINDOWS_PLATFORM_TOKENS):
            return TARGET_OS_WINDOWS

    return TARGET_OS_LINUX


class ImageResolver:
    """Resolve a container image for an :class:`ImageNeed`, catalog → Docker Hub (Req. 5.4, 14).

    Session-injected for the relational catalog read; the semantic matcher and
    the Docker Hub lookup are **injected** so the semantic query over the
    Vector_Store and the online call both stay in the backend and never cross
    into the contained lab (Req. 14.5). The catalog is queried FIRST (semantic
    then relational) and Docker Hub online is the LAST resort (Req. 5.4). See the
    module docstring for the full resolution-order contract.

    Args:
        session: The request-scoped :class:`~sqlalchemy.orm.Session` used to read
            the ``image_catalog``. Not opened/closed here — the resolver only
            reads.
        catalog_matcher: Optional injectable :class:`CatalogSemanticMatcher` — a
            semantic match over the vectorized Docker_Image_Catalog. ``None``
            skips the semantic step and resolves relationally first (Req. 5.4).
        docker_hub_lookup: Optional injectable :class:`DockerHubLookup` — the
            application provisioning layer's online Docker Hub lookup, used only
            as the LAST-resort fallback when the catalog yields no adequate
            candidate. ``None`` makes the resolver *offline by construction* (it
            resolves straight from the catalog), which is exactly how explicit
            offline operation and most tests run it.
    """

    def __init__(
        self,
        session: Session,
        *,
        catalog_matcher: Callable[[ImageNeed], Iterable[str]] | None = None,
        docker_hub_lookup: Callable[[ImageNeed], str | None] | None = None,
    ) -> None:
        self._session = session
        self._catalog_matcher = catalog_matcher
        self._docker_hub_lookup = docker_hub_lookup

    def resolve(self, need: ImageNeed) -> ImageResolution:
        """Resolve an image for ``need`` — Docker_Image_Catalog FIRST, Docker Hub last.

        1. **Catalog semantic match FIRST** (Req. 5.4). When a catalog matcher is
           injected, query the vectorized catalog for candidate images and, if a
           candidate is confirmed against the relational ``image_catalog`` for the
           need, resolve it with ``source="image_catalog_semantic"``.
        2. **Catalog relational match next** (Req. 5.4, 14.4). Read the relational
           ``image_catalog`` filtered by ``need.target_os``, preferring a row
           whose ``services`` cover the need; resolve with
           ``source="image_catalog"``.
        3. **Docker Hub online — fallback last** (Req. 5.4, 14.5). Only when the
           catalog has no adequate candidate and a lookup is injected, call it
           through the provisioning layer; a returned image resolves with
           ``source="docker_hub"``. A raised error or an empty result is treated
           as "unavailable".
        4. **Nothing covers the need →** raise :class:`ImageUnavailableError`
           naming the request.

        Args:
            need: The detected target OS + services the image must satisfy.

        Returns:
            The :class:`ImageResolution` with the resolved image and its source.

        Raises:
            ImageUnavailableError: when neither the catalog nor Docker Hub
                resolves an image for ``need``.
        """
        semantic = self._resolve_from_catalog_semantic(need)
        if semantic is not None:
            return ImageResolution(
                image=semantic,
                source=SOURCE_IMAGE_CATALOG_SEMANTIC,
                target_os=need.target_os,
            )

        catalog = self._resolve_from_catalog(need)
        if catalog is not None:
            return ImageResolution(
                image=catalog,
                source=SOURCE_IMAGE_CATALOG,
                target_os=need.target_os,
            )

        online = self._resolve_online(need)
        if online is not None:
            return ImageResolution(
                image=online,
                source=SOURCE_DOCKER_HUB,
                target_os=need.target_os,
            )

        raise ImageUnavailableError(need)

    # ---- catalog semantic match (Req. 5.4) ---------------------------------

    def _resolve_from_catalog_semantic(self, need: ImageNeed) -> str | None:
        """Return a semantically matched catalog image confirmed relationally, or ``None``.

        Asks the injected :class:`CatalogSemanticMatcher` for candidate image
        references ranked by similarity over the vectorized Docker_Image_Catalog
        (Req. 5.4) and returns the first candidate whose relational
        ``image_catalog`` row actually covers the need's ``target_os`` (and
        services, when present). Confirming each candidate against the relational
        catalog keeps the resolution honest: a stale or hallucinated embedding
        never yields an image that is not truly cataloged for the need.

        A ``None`` matcher (not injected), an empty candidate list, or a matcher
        that raises (Vector_Store unavailable) is uniformly treated as "no
        semantic candidate" so the resolver falls through to the relational
        catalog match (Req. 5.4).
        """
        if self._catalog_matcher is None:
            return None
        try:
            candidates = self._catalog_matcher(need)
        except Exception as exc:  # noqa: BLE001 -- semantic match is best-effort
            logger.info(
                "Catalog semantic match unavailable for target_os %s (%s: %s); "
                "falling back to the relational image_catalog.",
                need.target_os,
                type(exc).__name__,
                exc,
            )
            return None

        for candidate in _clean_tokens(candidates):
            if self._catalog_row_covers(candidate, need):
                return candidate
        return None

    def _catalog_row_covers(self, image: str, need: ImageNeed) -> bool:
        """True if an ``image_catalog`` row for ``image`` covers ``need``.

        Confirms a semantic candidate against the Relational_Store: the row must
        exist for ``image`` and ``need.target_os`` and, when the need names
        services, its ``services`` must cover them. This is the relational
        confirmation of the semantic match (Req. 5.4).
        """
        from app.models.domain import ImageCatalog

        row = (
            self._session.execute(
                select(ImageCatalog)
                .where(
                    ImageCatalog.image == image,
                    ImageCatalog.target_os == need.target_os,
                )
                .limit(1)
            )
            .scalars()
            .first()
        )
        if row is None:
            return False
        return self._services_cover(row.services, need)

    # ---- Docker Hub online fallback (Req. 5.4, 14.5) -----------------------

    def _resolve_online(self, need: ImageNeed) -> str | None:
        """Return an image from the injected Docker Hub lookup, or ``None`` if unavailable.

        This is the LAST-resort fallback, reached only after the
        Docker_Image_Catalog (semantic and relational) yields no adequate
        candidate (Req. 5.4). A ``None`` lookup (offline by construction), a
        lookup that returns nothing (no match / empty), or a lookup that raises
        (timeout, connection error) is uniformly treated as "unavailable" so the
        resolver raises :class:`ImageUnavailableError`. The online call happens
        here in the backend provisioning layer, never inside the contained lab
        (Req. 14.5).
        """
        if self._docker_hub_lookup is None:
            return None
        try:
            image = self._docker_hub_lookup(need)
        except Exception as exc:  # noqa: BLE001 -- any network error means "offline"
            logger.info(
                "Docker Hub lookup unavailable for target_os %s (%s: %s); "
                "falling back to the image_catalog.",
                need.target_os,
                type(exc).__name__,
                exc,
            )
            return None
        image = image.strip() if isinstance(image, str) else ""
        return image or None

    # ---- catalog relational match (Req. 5.4, 14.4) ------------------------

    def _resolve_from_catalog(self, need: ImageNeed) -> str | None:
        """Resolve an image from the relational ``image_catalog`` filtered by target OS.

        Reads the :class:`~app.models.domain.ImageCatalog` rows for
        ``need.target_os`` (ordered deterministically) and, when the need names
        services, prefers a row whose ``services`` cover them; otherwise returns
        the first cataloged image. Returns ``None`` when the catalog has no row
        for the target OS, so :meth:`resolve` falls through to the Docker Hub
        online fallback (Req. 5.4).
        """
        from app.models.domain import ImageCatalog

        rows = (
            self._session.execute(
                select(ImageCatalog)
                .where(ImageCatalog.target_os == need.target_os)
                .order_by(ImageCatalog.image.asc())
            )
            .scalars()
            .all()
        )
        if not rows:
            return None

        if need.services:
            for row in rows:
                if self._services_cover(row.services, need):
                    return row.image

        return rows[0].image

    @staticmethod
    def _services_cover(row_services: Any, need: ImageNeed) -> bool:
        """True if ``row_services`` cover the services named by ``need``.

        A need with no services is covered by any row. Otherwise every requested
        service (case-insensitive) must appear among the row's ``services``.
        Shared by the semantic confirmation and the relational match so both
        apply the same coverage rule (Req. 5.4, 14.4).
        """
        wanted = {s.lower() for s in need.services if isinstance(s, str) and s.strip()}
        if not wanted:
            return True
        covered = {token.lower() for token in _clean_tokens(row_services)}
        return wanted.issubset(covered)


# ---- semantic matcher factory (Req. 5.4) ----------------------------------

# The provenance stamped by the Docker catalog Ingestion on every vectorized
# image description; the semantic matcher only considers hits carrying it, so a
# generic experience/affinity embedding is never mistaken for a catalog image.
# Mirrors ``DockerCatalogIngestService._SOURCE``.
_CATALOG_SOURCE = "docker_image_catalog"

# How many similar catalog hits to retrieve per need. Small on purpose: the
# resolver only needs the top few candidates to confirm one relationally.
_DEFAULT_SEMANTIC_K = 5


def build_catalog_semantic_matcher(
    memory: Any,
    *,
    campaign_id: str,
    k: int = _DEFAULT_SEMANTIC_K,
) -> Callable[[ImageNeed], list[str]]:
    """Build a :class:`CatalogSemanticMatcher` backed by the Vector_Store (Req. 5.4).

    Wraps an :class:`~app.services.ai.memory.ExperienceMemory` so the
    ``environment`` phase can retrieve candidate image references by similarity
    over the Docker_Image_Catalog descriptions vectorized at setup
    (``metadata.source="docker_image_catalog"`` under the ``environment_affinity``
    kind). The returned callable embeds a short query built from the need
    (target OS + services), retrieves the top-``k`` most similar experience rows,
    keeps only the catalog-sourced hits whose ``target_os`` matches the need, and
    yields their ``metadata.image`` references in similarity order.

    The matcher is deliberately **read-only and best-effort**: it does not raise
    on an empty or unavailable memory (``ExperienceMemory.retrieve`` already
    degrades to an empty list when the Vector_Store is unreachable), so the
    resolver simply falls through to the relational catalog match. The resolver
    confirms every returned candidate against the relational ``image_catalog``
    before resolving it, so a stale embedding never yields an uncataloged image.

    Args:
        memory: The session-injected :class:`ExperienceMemory` (or any object
            exposing a compatible ``retrieve(campaign_id, query, k)`` method).
        campaign_id: The running campaign id passed to ``retrieve``; catalog
            affinities are anchored to a sentinel campaign, so they stay eligible
            for retrieval regardless of the running campaign.
        k: How many similar hits to retrieve (default small).

    Returns:
        A callable ``(need) -> list[str]`` of candidate image references ranked
        by similarity — exactly the :class:`CatalogSemanticMatcher` shape.
    """

    def _match(need: ImageNeed) -> list[str]:
        query = _semantic_query(need)
        hits = memory.retrieve(campaign_id=campaign_id, query=query, k=k)
        images: list[str] = []
        for hit in hits or []:
            image = _catalog_hit_image(hit, need.target_os)
            if image and image not in images:
                images.append(image)
        return images

    return _match


def _semantic_query(need: ImageNeed) -> str:
    """Render the similarity query text for an :class:`ImageNeed`.

    Mirrors the text the Ingestion embeds per image
    (:meth:`DockerCatalogIngestService._embedding_content`) — the target OS and
    the needed services — so the query lands near the relevant catalog
    descriptions in the embedding space (Req. 5.4).
    """
    services = ", ".join(need.services) if need.services else ""
    return f"docker image for {need.target_os} services: {services}".strip()


def _catalog_hit_image(hit: Any, target_os: str) -> str | None:
    """Extract a catalog image reference from a retrieval hit, or ``None``.

    Keeps only hits stamped with the Docker catalog provenance
    (``metadata.source == "docker_image_catalog"``) whose ``metadata.target_os``
    matches the need, and returns the hit's ``metadata.image``. Any other hit
    (a generic experience/affinity, a different OS, a malformed metadata) yields
    ``None`` so it is ignored (Req. 5.4).
    """
    if not isinstance(hit, dict):
        return None
    metadata = hit.get("metadata")
    if not isinstance(metadata, dict):
        return None
    if metadata.get("source") != _CATALOG_SOURCE:
        return None
    if metadata.get("target_os") != target_os:
        return None
    image = metadata.get("image")
    if isinstance(image, str) and image.strip():
        return image.strip()
    return None


# ---- helpers --------------------------------------------------------------


def _clean_tokens(values: Any) -> list[str]:
    """Return the non-empty, trimmed string tokens of an iterable, defensively.

    Accepts ``None`` (→ empty), a single string (→ one token), or any iterable of
    string-ish values; non-strings and blanks are dropped so a malformed entry
    never aborts detection/coverage checks.
    """
    if values is None:
        return []
    if isinstance(values, str):
        value = values.strip()
        return [value] if value else []
    try:
        iterator = iter(values)
    except TypeError:
        return []
    tokens: list[str] = []
    for item in iterator:
        if isinstance(item, str):
            trimmed = item.strip()
            if trimmed:
                tokens.append(trimmed)
    return tokens


__all__ = [
    "SOURCE_DOCKER_HUB",
    "SOURCE_IMAGE_CATALOG",
    "SOURCE_IMAGE_CATALOG_SEMANTIC",
    "TARGET_OS_LINUX",
    "TARGET_OS_WINDOWS",
    "CatalogSemanticMatcher",
    "DockerHubLookup",
    "ImageNeed",
    "ImageResolution",
    "ImageResolver",
    "ImageUnavailableError",
    "build_catalog_semantic_matcher",
    "detect_target_os",
]
