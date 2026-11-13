"""StixService — fetch (in memory) and analyse STIX 2.x bundles.

The service powers the flow *Landing → STIX base listing → Intelligence
dashboard → Campaign/Adversary selection*. It holds **no state on disk**: the
bundle is downloaded from the MITRE ``attack-stix-data`` repository straight
into memory (:meth:`StixService.fetch_bundle`), the derivations
(:meth:`dashboard`, :meth:`list_campaigns`, :meth:`list_adversaries`) are pure
functions over the in-memory bundle, and the result is persisted in the database
by :class:`~app.services.stix.stix_persistence_service.StixPersistenceService`
(driven by the ``stix_packages`` ingestion source).

* ``fetch_bundle`` — the only network egress; validates the version before
  building the URL and the body before returning it.
* ``dashboard`` / ``list_campaigns`` / ``list_adversaries`` — derivations over a
  bundle already in memory.

The known-versions index is kept as a backend-maintained constant
(:data:`KNOWN_VERSIONS`, a mirror of the upstream index) so listing the bases
never needs a network call.

_Requirements: 2.1, 2.5, 3.3, 4.5, 7.1_
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from app.seeds.manifest import CURATED_STIX_VERSION

# A version is digits separated by dots (e.g. ``18.0``); anything else is rejected
# before a URL is built.
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


# ---------------------------------------------------------------------------
# Semantic version ordering
# ---------------------------------------------------------------------------


def _semantic_version_key(version: str) -> tuple[int, ...]:
    """Numeric sort key for a ``"MAJOR.MINOR"`` (optionally ``.PATCH``) version.

    Splits on ``.`` and compares each component as an integer so the ordering is
    truly semantic rather than lexicographic: ``"18.0"`` sorts above ``"9.0"``
    and ``"17.1"`` above ``"17.0"``. Non-numeric components degrade gracefully to
    ``0`` so a malformed entry can never raise and break the listing.
    """
    parts: list[int] = []
    for part in str(version).split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


# ---------------------------------------------------------------------------
# Backend-maintained constants
# ---------------------------------------------------------------------------

# Known MITRE ATT&CK Enterprise versions, newest first: an OFFLINE FALLBACK
# mirror of the upstream index. The live list comes from the upstream index
# (``StixVersionCatalog``); this constant is used only when that lookup fails.
# Availability (``downloaded``) comes from the
# database, never from disk. Consumers sort defensively by semantic version, so
# the stored order is a convenience, not a contract.
KNOWN_VERSIONS: list[str] = [
    "20.1",
    "20.0",
    "19.2",
    "19.1",
    "19.0",
    "18.1",
    "18.0",
    "17.1",
    "17.0",
    "16.1",
    "16.0",
    "15.1",
    "15.0",
    "14.1",
    "14.0",
    "13.1",
    "13.0",
    "12.1",
    "12.0",
    "11.3",
    "11.2",
    "11.1",
    "11.0",
    "10.1",
    "10.0",
    "9.0",
    "8.2",
    "8.1",
    "8.0",
]

# ---------------------------------------------------------------------------
# Curated STIX version
# ---------------------------------------------------------------------------

# The ATT&CK version the 8 curated cases were curated against. The single source
# of truth is the curated seed (``campaign.stix_version`` in
# ``app/seeds/data/<slug>.json``), mirrored by the seed manifest; nothing else
# hard-codes it. The ``stix_packages`` ingestion always keeps this version
# installed (``REQUIRED_STIX_VERSIONS``) next to the newest one.
CURATED_VERSION: str = CURATED_STIX_VERSION


# English description per counted STIX object type (Req. 3.3). The dashboard fills
# each ``TypeCount.description`` from this map; every description is non-empty.
TYPE_DESCRIPTIONS: dict[str, str] = {
    "campaign": "campaign",
    "attack-pattern": "attack-pattern",
    "x-mitre-tactic": "x-mitre-tactic",
    "intrusion-set": "intrusion-set",
    "malware": "malware",
    "relationship": "relationship",
}

# Logical label (English) exposed in the dashboard per STIX object type (Req. 3.2).
ROTULO_TIPO: dict[str, str] = {
    "campaign": "campaigns",
    "attack-pattern": "techniques",
    "x-mitre-tactic": "tactics",
    "intrusion-set": "actors",
    "malware": "malware",
    "relationship": "relationships",
}

# Mapping of the 8 curated campaign slugs (``campaigns.curated`` rows) to the
# known STIX campaign identifier(s) / canonical name used to resolve the
# "curated" flag (Req. 4.5). ``external_references`` codes (e.g. ``C0010``) take
# priority, with the canonical name as a tolerant fallback.
SLUG_TO_STIX: dict[str, dict[str, Any]] = {
    "apt41_dust": {"referencias": ["C0040"], "nome_canonico": "APT41 DUST"},
    "c0010": {"referencias": ["C0010"], "nome_canonico": "C0010"},
    "c0026": {"referencias": ["C0026"], "nome_canonico": "C0026"},
    "costaricto": {"referencias": ["C0004"], "nome_canonico": "CostaRicto"},
    "operation_midnighteclipse": {
        "referencias": ["C0048"],
        "nome_canonico": "Operation MidnightEclipse",
    },
    "outer_space": {"referencias": ["C0042"], "nome_canonico": "Outer Space"},
    "salesforce_data_exfiltration": {
        "referencias": ["C0059"],
        "nome_canonico": "Salesforce Data Exfiltration",
    },
    "shadowray": {"referencias": ["C0045"], "nome_canonico": "ShadowRay"},
}


# ---------------------------------------------------------------------------
# Typed download error (Req. 2.5, 2.7)
# ---------------------------------------------------------------------------


class StixDownloadError(Exception):
    """Raised by :meth:`StixService.fetch_bundle` when a base download fails.

    Covers every failure mode of the single network egress — a malformed
    ``version`` (rejected *before* building the URL to prevent URL injection), a
    network/HTTP error, or downloaded content that is not a valid STIX bundle.
    Carries a human-readable :attr:`detail`. On any failure nothing is persisted
    and the base stays ``downloaded == false``.
    """

    def __init__(self, detail: str, version: str | None = None) -> None:
        self.version = version
        self.detail = detail
        super().__init__(detail)


_ENTERPRISE_URL_RE = re.compile(r"/enterprise-attack/enterprise-attack-(\d+(?:\.\d+)+)\.json$")


def latest_enterprise_version(index: Any) -> str:
    """Highest semantic ``enterprise-attack`` version listed in the upstream ``index.json``.

    Format (verified against the published file): ``{"collections": [{"name":
    "Enterprise ATT&CK", "versions": [{"version": "20.1", "url": ".../enterprise-attack/
    enterprise-attack-20.1.json", "modified": "..."}, ...]}, {ICS...}, {Mobile...}]}``.
    Only versions whose URL points at ``enterprise-attack`` are considered (ICS and
    Mobile are ignored); malformed entries are skipped.

    Raises:
        StixDownloadError: the index has no enterprise-attack version.
    """
    found = enterprise_versions(index)
    if not found:
        raise StixDownloadError("O índice do MITRE ATT&CK não lista versões enterprise-attack.")
    return found[0]


def enterprise_versions(index: Any) -> list[str]:
    """Every ``enterprise-attack`` version of the upstream ``index.json``, newest first.

    Same parsing rules as :func:`latest_enterprise_version` (ICS/Mobile ignored,
    malformed entries skipped); returns an empty list when nothing valid is found.
    """
    found: set[str] = set()
    collections = index.get("collections") if isinstance(index, dict) else None
    for collection in collections if isinstance(collections, list) else []:
        versions = collection.get("versions") if isinstance(collection, dict) else None
        for entry in versions if isinstance(versions, list) else []:
            if not isinstance(entry, dict):
                continue
            version, url = entry.get("version"), entry.get("url")
            if not isinstance(version, str) or not isinstance(url, str):
                continue
            match = _ENTERPRISE_URL_RE.search(url)
            if match and match.group(1) == version:
                found.add(version)
    return sorted(found, key=_semantic_version_key, reverse=True)


class StixVersionCatalog:
    """Cached list of the ATT&CK Enterprise versions published upstream.

    ``list_versions`` never raises: it reads the upstream ``index.json`` through an
    injectable ``fetcher`` (``fetcher(timeout) -> parsed index``), caches a success
    for ``ttl_s`` and a failure (served as :data:`KNOWN_VERSIONS`) for the shorter
    ``negative_ttl_s`` so an offline machine is not re-queried on every request.
    Thread-safe; the lock is not held during the network call.
    """

    def __init__(
        self,
        fetcher: Callable[[float], Any] | None = None,
        ttl_s: float = 3600.0,
        negative_ttl_s: float = 60.0,
        timeout_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetcher = fetcher or self._default_fetcher
        self._ttl_s = ttl_s
        self._negative_ttl_s = negative_ttl_s
        self._timeout_s = timeout_s
        self._clock = clock
        self._lock = threading.Lock()
        self._versions: list[str] | None = None
        self._expires_at = 0.0
        self._remote = False
        self._refreshing = False
        self._inflight: threading.Event | None = None

    @staticmethod
    def _default_fetcher(timeout: float) -> Any:
        import requests  # Local import: only the network path needs it.

        response = requests.get(StixService._INDEX_URL, timeout=timeout)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _fallback() -> list[str]:
        return sorted(KNOWN_VERSIONS, key=_semantic_version_key, reverse=True)

    def list_versions(self) -> list[str]:
        """Versions newest→oldest: from the index, or ``KNOWN_VERSIONS`` on any failure."""
        # Single-flight: one thread (the leader) queries upstream; concurrent callers
        # wait for its result (bounded by the catalog timeout) and read the cache.
        with self._lock:
            if self._versions is not None and self._clock() < self._expires_at:
                return list(self._versions)
            inflight = self._inflight
            leader = inflight is None
            if leader:
                inflight = self._inflight = threading.Event()
        assert inflight is not None  # noqa: S101 -- type narrowing; inflight is always set under the lock
        if not leader:
            inflight.wait(timeout=self._timeout_s + 1.0)
            with self._lock:
                if self._versions is not None:
                    return list(self._versions)
            return self._fallback()
        try:
            try:
                versions = enterprise_versions(self._fetcher(self._timeout_s))
                remote = bool(versions)
            except Exception:  # noqa: BLE001 -- offline, HTTP, JSON or fetcher error.
                versions, remote = [], False
            result = versions if remote else self._fallback()
            with self._lock:
                self._versions = result
                self._remote = remote
                self._expires_at = self._clock() + (self._ttl_s if remote else self._negative_ttl_s)
            return list(result)
        finally:
            with self._lock:
                self._inflight = None
            inflight.set()

    def is_known(self, version: str) -> bool:
        """Whether ``version`` is in the current list (remote, or fallback offline)."""
        return version in self.list_versions()

    def warm_up(self) -> None:
        """Populate the cache (for a background thread); never raises."""
        self.list_versions()

    def cached_latest(self, *, block: bool = False) -> str | None:
        """Newest upstream version known from the index, or ``None``; never raises.

        ``None`` means "no remote answer": offline, never fetched or only the
        fallback list is cached. Without ``block`` this never touches the network on
        the calling thread: it serves the cache (even a stale remote answer) and,
        when the cache is cold or expired, refreshes it in a daemon thread. With
        ``block`` a cold/expired cache is refreshed inline (bounded by the catalog
        timeout), for callers that already run in the background.
        """
        with self._lock:
            fresh = self._versions is not None and self._clock() < self._expires_at
            if fresh or not block:
                versions, remote = self._versions, self._remote
        if not fresh and block:
            self.list_versions()
            with self._lock:
                versions, remote = self._versions, self._remote
        elif not fresh:
            self._refresh_in_background()
        return versions[0] if remote and versions else None

    def _refresh_in_background(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True

        def _work() -> None:
            try:
                self.list_versions()
            finally:
                with self._lock:
                    self._refreshing = False

        try:
            threading.Thread(target=_work, name="stix-version-refresh", daemon=True).start()
        except Exception:  # noqa: BLE001 -- never break the caller
            with self._lock:
                self._refreshing = False

    def clear(self) -> None:
        """Drop the cache (tests)."""
        with self._lock:
            self._versions = None
            self._expires_at = 0.0


_default_catalog = StixVersionCatalog()


def get_version_catalog() -> StixVersionCatalog:
    """The process-wide catalog (override in tests via ``set_version_catalog``)."""
    return _default_catalog


def set_version_catalog(catalog: StixVersionCatalog) -> StixVersionCatalog:
    """Replace the process-wide catalog; returns the previous one."""
    global _default_catalog
    previous, _default_catalog = _default_catalog, catalog
    return previous


def list_remote_versions(fetcher: Callable[[float], Any] | None = None) -> list[str]:
    """Enterprise versions newest→oldest (cached; falls back to ``KNOWN_VERSIONS``).

    With a ``fetcher`` a throw-away catalog is used (no shared cache).
    """
    catalog = StixVersionCatalog(fetcher) if fetcher else get_version_catalog()
    return catalog.list_versions()


def version_is_known(version: str) -> bool:
    """Whether ``version`` is published upstream (or in the offline fallback)."""
    return get_version_catalog().is_known(version)


# ---------------------------------------------------------------------------
# Typed response models (field names identical to the design contracts)
# ---------------------------------------------------------------------------


class BaseStixInfo(BaseModel):
    """One MITRE ATT&CK Enterprise base in the listing (Req. 2.1-2.3).

    ``downloaded`` reflects whether the version is persisted in the database and
    ``selectable == downloaded``.
    """

    version: str
    name: str
    downloaded: bool = False
    selectable: bool = False


class TypeCount(BaseModel):
    """A per-type object count for the intelligence dashboard (Req. 3.2, 3.3).

    ``label`` is the English logical label, ``stix_type`` the raw STIX ``type``
    and ``description`` a non-empty English description of what the type means.
    """

    label: str
    stix_type: str
    count: int
    description: str


class DashboardStix(BaseModel):
    """The intelligence dashboard of a selected base (Req. 3.1, 3.2)."""

    version: str
    name: str
    counts: list[TypeCount] = Field(default_factory=list)


class CampaignStix(BaseModel):
    """A STIX ``campaign`` object with the curated-campaign flag (Req. 4.2, 4.5).

    ``curated`` is ``True`` (with ``curated_slug`` filled) iff the campaign maps
    to one of the 8 curated campaigns; otherwise ``curated == False`` and
    ``curated_slug is None``.
    """

    stix_id: str
    name: str
    description: str | None = None
    curated: bool = False
    curated_slug: str | None = None


class AdversaryStix(BaseModel):
    """A STIX ``intrusion-set`` object with its attributed campaigns (Req. 4.3)."""

    stix_id: str
    name: str
    campaigns: list[CampaignStix] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StixService:
    """Fetches (in memory) and analyses STIX 2.x enterprise-attack bundles.

    An injectable ``http_client`` (an object with ``get(url, timeout=...)``)
    keeps the single network egress (:meth:`fetch_bundle`) mockable. Nothing is
    ever written to disk.
    """

    def __init__(self, http_client: Any = None) -> None:
        self._http_client = http_client

    # ---- public API --------------------------------------------------------

    def fetch_bundle(self, version: str) -> dict:
        """Download the ``enterprise-attack-{version}`` bundle into memory.

        The ONLY network egress. ``version`` is validated *before* any URL is
        built (digits separated by dots), which closes the URL-injection vector.
        The body must be JSON carrying an ``"objects"`` list; anything else is a
        download failure. Nothing is written to disk.

        Raises:
            StixDownloadError: unknown/malformed version, network or HTTP error,
                or content that is not a valid STIX bundle.
        """
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            raise StixDownloadError(
                f"Versão inválida '{version}': download recusado sem acessar a rede.",
                version=version if isinstance(version, str) else None,
            )

        url = self._versioned_url(version)
        try:
            content = self._download_content(url)
        except StixDownloadError:
            raise
        except Exception as exc:
            raise StixDownloadError(
                f"Não foi possível obter a base v{version} do repositório MITRE ATT&CK: {exc}.",
                version=version,
            ) from exc

        try:
            bundle = json.loads(content)
        except (json.JSONDecodeError, ValueError) as exc:
            raise StixDownloadError(
                f"Conteúdo baixado da base v{version} não é JSON válido: {exc}.",
                version=version,
            ) from exc
        if not isinstance(bundle, dict) or not isinstance(bundle.get("objects"), list):
            raise StixDownloadError(
                f"Conteúdo baixado da base v{version} não é um bundle STIX válido "
                '(esperava um objeto JSON com a lista "objects").',
                version=version,
            )
        return bundle

    # Canonical raw base of the official MITRE ATT&CK STIX data.
    _URL_BASE_CANONICA = (
        "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/enterprise-attack/"
    )

    # HTTP timeout (seconds) for the single network egress — finite so a hung
    # server can never block the request indefinitely.
    _DOWNLOAD_TIMEOUT_S = 60

    def _versioned_url(self, version: str) -> str:
        """Build the versioned bundle URL for an already-validated ``version``.

        Appends ``enterprise-attack-{version}.json`` to the canonical MITRE
        ``raw.githubusercontent.com`` base. ``version`` is assumed to be on the allowlist (validated by the
        caller), so the result is injection-safe.
        """
        return f"{self._URL_BASE_CANONICA}enterprise-attack-{version}.json"

    # Official index of every published ATT&CK release (all domains), served by
    # the same repository as the bundles.
    _INDEX_URL = "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/index.json"

    # The index is tiny, so a short timeout keeps an offline start fast.
    _INDEX_TIMEOUT_S = 15

    def fetch_latest_version(self) -> str:
        """Newest published ``enterprise-attack`` version according to the upstream index.

        One small GET of ``index.json`` (``collections[].versions[]`` with
        ``version``/``url``/``modified``); see :func:`latest_enterprise_version`.

        Raises:
            StixDownloadError: network/HTTP error, unreadable JSON or an index
                without any enterprise-attack version.
        """
        try:
            content = self._download_content(self._INDEX_URL, timeout=self._INDEX_TIMEOUT_S)
            index = json.loads(content)
        except StixDownloadError:
            raise
        except Exception as exc:
            raise StixDownloadError(
                f"Não foi possível consultar o índice de versões do MITRE ATT&CK: {exc}."
            ) from exc
        return latest_enterprise_version(index)

    def _download_content(self, url: str, timeout: float | None = None) -> str:
        """GET ``url`` and return its body as text, raising on a non-OK status.

        Uses the injected :attr:`_http_client` when present (tests mock the only
        network egress), otherwise :func:`requests.get`. A finite timeout is
        always applied and a non-2xx response is turned into an error.
        """
        client = self._http_client
        if client is not None:
            response = client.get(url, timeout=timeout or self._DOWNLOAD_TIMEOUT_S)
        else:
            import requests  # Local import: only the network path needs it.

            response = requests.get(url, timeout=timeout or self._DOWNLOAD_TIMEOUT_S)

        # Prefer raise_for_status when available; otherwise check status_code.
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
        else:
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and not (200 <= status < 300):
                raise StixDownloadError(f"Resposta HTTP inesperada ({status}) ao baixar {url}.")

        text = getattr(response, "text", None)
        if text is None:
            body = getattr(response, "content", None)
            if isinstance(body, (bytes, bytearray)):
                text = body.decode("utf-8")
        if not isinstance(text, str):
            raise StixDownloadError(f"Resposta sem corpo de texto utilizável ao baixar {url}.")
        return text

    def dashboard(self, version: str, bundle: dict) -> DashboardStix:
        """Per-type object counts (+ descriptions) for the selected base.

        Derives, from the in-memory ``bundle`` (already validated by
        :meth:`fetch_bundle`), one :class:`TypeCount` per counted STIX ``type``.

        The result always carries the six required types in a stable order — the
        insertion order of :data:`ROTULO_TIPO` (``campaign``, ``attack-pattern``,
        ``x-mitre-tactic``, ``intrusion-set``, ``malware``, ``relationship``) —
        even when a type has zero objects, so the dashboard is deterministic
        across bases (Req. 3.2). For each type: ``count`` is exactly the number of
        ``bundle["objects"]`` whose ``"type"`` equals ``stix_type``; ``label`` is
        the label from :data:`ROTULO_TIPO`; ``description`` is the non-empty
        description from :data:`TYPE_DESCRIPTIONS` (Req. 3.3); and ``stix_type`` is
        the raw STIX type.

        Satisfies Correctness Properties 3 (count equals the number of objects of
        that type) and 4 (every ``description`` is non-empty).

        Args:
            version: The ATT&CK Enterprise version (e.g. ``"19.1"``).

        Returns:
            A :class:`DashboardStix` with ``version``, ``name`` and the per-type
            ``counts``.

        """
        # Count every object's raw "type" in a single pass. Objects that are not
        # dicts or lack a string "type" simply do not contribute to any count.
        count_by_type: dict[str, int] = {}
        for obj in bundle["objects"]:
            if not isinstance(obj, dict):
                continue
            stix_type = obj.get("type")
            if not isinstance(stix_type, str):
                continue
            count_by_type[stix_type] = count_by_type.get(stix_type, 0) + 1

        # Emit the required types in ROTULO_TIPO insertion order so the dashboard
        # is stable and always complete (count 0 included when absent).
        counts: list[TypeCount] = []
        for stix_type, label in ROTULO_TIPO.items():
            counts.append(
                TypeCount(
                    label=label,
                    stix_type=stix_type,
                    count=count_by_type.get(stix_type, 0),
                    description=TYPE_DESCRIPTIONS[stix_type],
                )
            )

        return DashboardStix(
            version=version,
            name=f"Enterprise ATT&CK v{version}",
            counts=counts,
        )

    def list_campaigns(self, bundle: dict) -> list[CampaignStix]:
        """STIX ``campaign`` objects of a base with the curated-campaign flag resolved.

        Works on the in-memory ``bundle`` and selects every object whose ``type`` is ``"campaign"``, in the **exact
        order** they appear in ``bundle["objects"]`` — never dropping nor
        duplicating any campaign object (Correctness Property 5).

        For each campaign a :class:`CampaignStix` is built with ``stix_id`` from
        ``id``, ``name`` from ``name`` (falling back to ``id`` when absent) and the
        optional ``description``. The ``curated`` / ``curated_slug`` pair is
        resolved against :data:`SLUG_TO_STIX` tolerantly (Req. 4.5, Correctness
        Property 7):

        1. **By external reference** — if any of the campaign's
           ``external_references`` carries an ``external_id`` (e.g. ``"C0010"``)
           that case-insensitively equals one of a slug's ``"referencias"`` codes,
           that slug matches.
        2. **By canonical name** — otherwise, the normalized campaign ``name``
           (``casefold`` + ``strip``) is compared against each slug's
           ``"nome_canonico"`` (also normalized).
        3. **Silent fallback** — if nothing matches, ``curated = False`` and
           ``curated_slug = None``. No campaign is ever invented.

        Args:
            bundle: The in-memory STIX bundle (a dict with an ``"objects"`` list).

        Returns:
            The list of :class:`CampaignStix`, preserving the bundle order.

        """
        campaigns: list[CampaignStix] = []
        for obj in bundle["objects"]:
            if not isinstance(obj, dict) or obj.get("type") != "campaign":
                continue

            campaign = self._map_campaign(obj)
            if campaign is not None:
                campaigns.append(campaign)

        return campaigns

    @staticmethod
    def _extract_external_id(obj: dict[str, Any]) -> str | None:
        """Return the first usable ``external_id`` of a STIX object, or ``None``.

        Scans ``external_references`` and returns the first non-empty
        ``external_id`` string (e.g. ``"C0045"``), preserving its original form.
        """
        references = obj.get("external_references")
        if not isinstance(references, list):
            return None
        for reference in references:
            if not isinstance(reference, dict):
                continue
            external_id = reference.get("external_id")
            if isinstance(external_id, str) and external_id.strip():
                return external_id.strip()
        return None

    @classmethod
    def _map_campaign(cls, obj: dict[str, Any]) -> CampaignStix | None:
        """Map a STIX ``campaign`` object to a :class:`CampaignStix`, or ``None``.

        Single source of truth for building a campaign entry — used by **both**
        :meth:`list_campaigns` and :meth:`list_adversaries` so the ``curated`` /
        ``curated_slug`` resolution is identical in the campaign view and in the
        nested adversary view (Correctness Property 7).

        Builds ``stix_id`` from ``id``, ``name`` from ``name`` (falling back to
        ``id`` when absent), the optional ``description``, and resolves the
        ``curated`` / ``curated_slug`` pair via :meth:`_resolve_curated_slug`.
        Returns ``None`` for a campaign object without a usable string ``id`` (it
        cannot be referenced downstream), so callers simply skip it rather than
        emit a half-formed entry.
        """
        stix_id = obj.get("id")
        if not isinstance(stix_id, str):
            return None

        raw_name = obj.get("name")
        name = raw_name if isinstance(raw_name, str) and raw_name else stix_id

        raw_description = obj.get("description")
        description = raw_description if isinstance(raw_description, str) else None

        curated_slug = cls._resolve_curated_slug(obj)

        return CampaignStix(
            stix_id=stix_id,
            name=name,
            description=description,
            curated=curated_slug is not None,
            curated_slug=curated_slug,
        )

    @staticmethod
    def _resolve_curated_slug(obj: dict[str, Any]) -> str | None:
        """Resolve the curated-campaign slug for a STIX ``campaign`` object, or ``None``.

        Tolerant matching against :data:`SLUG_TO_STIX` (Req. 4.5): first by
        ``external_references`` ``external_id`` codes (case-insensitive), then by
        normalized canonical name. Returns the matching slug, or ``None`` when no
        curated campaign matches (silent fallback — a campaign is never invented).
        """
        # 1) Match by external_references external_id (e.g. "C0010").
        codes: set[str] = set()
        references = obj.get("external_references")
        if isinstance(references, list):
            for reference in references:
                if not isinstance(reference, dict):
                    continue
                external_id = reference.get("external_id")
                if isinstance(external_id, str) and external_id.strip():
                    codes.add(external_id.strip().casefold())

        if codes:
            for slug, mapping in SLUG_TO_STIX.items():
                for code in mapping.get("referencias", []):
                    if isinstance(code, str) and code.strip().casefold() in codes:
                        return slug

        # 2) Match by normalized canonical name.
        raw_name = obj.get("name")
        if isinstance(raw_name, str):
            normalized_name = raw_name.strip().casefold()
            if normalized_name:
                for slug, mapping in SLUG_TO_STIX.items():
                    canonical = mapping.get("nome_canonico")
                    if (
                        isinstance(canonical, str)
                        and canonical.strip().casefold() == normalized_name
                    ):
                        return slug

        # 3) Silent fallback — nothing matched.
        return None

    # Synthetic group holding campaigns with no ``attributed-to`` relationship.
    ADVERSARY_UNASSOCIATED_ID = "sem-ator"
    ADVERSARY_UNASSOCIATED_NAME = "Sem ator associado"

    def list_adversaries(self, bundle: dict) -> list[AdversaryStix]:
        """STIX ``intrusion-set`` objects grouped with their attributed campaigns.

        Works on the in-memory ``bundle``.

        Builds the adversary → campaigns grouping from the bundle (Req. 4.3,
        Correctness Property 6):

        * Every object with ``type == "intrusion-set"`` becomes an
          :class:`AdversaryStix` (``stix_id`` from ``id``, ``name`` from ``name``
          with a fallback to ``id``), kept in **bundle order** with an empty
          campaign list to start.
        * Every ``campaign`` object is mapped once via the shared
          :meth:`_map_campaign` helper (so the ``curated`` / ``curated_slug`` flag
          is consistent with :meth:`list_campaigns` — Correctness Property 7) into
          a lookup keyed by ``stix_id``.
        * In ATT&CK, attribution is a ``relationship`` with
          ``relationship_type == "attributed-to"`` linking a campaign
          (``source_ref``) to an intrusion-set (``target_ref``). Each such
          relationship attaches the campaign identified by ``source_ref`` under
          the adversary identified by ``target_ref``. A campaign is attached under
          every adversary it is attributed to, and only those — never dropped nor
          duplicated under the same adversary.
        * Campaigns with no attribution relationship are collected under a single
          synthetic *"sem ator associado"* group (``stix_id`` =
          :data:`ADVERSARY_UNASSOCIATED_ID`), appended last — and only when there is
          at least one unattributed campaign.

        Args:
            bundle: The in-memory STIX bundle (a dict with an ``"objects"`` list).

        Returns:
            The list of :class:`AdversaryStix` (intrusion-sets in bundle order,
            optionally followed by the synthetic unattributed group).

        """
        objects = bundle["objects"]

        # Adversaries in bundle order, each starting with an empty campaign list;
        # plus a quick lookup from intrusion-set id to its AdversaryStix.
        adversaries: list[AdversaryStix] = []
        adversaries_by_id: dict[str, AdversaryStix] = {}
        for obj in objects:
            if not isinstance(obj, dict) or obj.get("type") != "intrusion-set":
                continue
            stix_id = obj.get("id")
            if not isinstance(stix_id, str):
                continue
            raw_name = obj.get("name")
            name = raw_name if isinstance(raw_name, str) and raw_name else stix_id
            adversary = AdversaryStix(stix_id=stix_id, name=name, campaigns=[])
            adversaries.append(adversary)
            # First declaration wins if an id somehow repeats.
            adversaries_by_id.setdefault(stix_id, adversary)

        # Campaign lookup (keyed by stix_id) built with the SAME mapping used by
        # list_campaigns, so curated/curated_slug stay consistent (Property 7).
        campaigns_by_id: dict[str, CampaignStix] = {}
        for obj in objects:
            if not isinstance(obj, dict) or obj.get("type") != "campaign":
                continue
            campaign = self._map_campaign(obj)
            if campaign is not None:
                campaigns_by_id.setdefault(campaign.stix_id, campaign)

        # Walk the attributed-to relationships: campaign (source_ref) → adversary
        # (target_ref). Track which campaigns got attributed (to at least one
        # adversary) and avoid duplicating a campaign under the same adversary.
        attributed_campaigns: set[str] = set()
        attached_by_adversary: dict[str, set[str]] = {}
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            if obj.get("type") != "relationship":
                continue
            if obj.get("relationship_type") != "attributed-to":
                continue

            source_ref = obj.get("source_ref")
            target_ref = obj.get("target_ref")
            if not isinstance(source_ref, str) or not isinstance(target_ref, str):
                continue

            campaign = campaigns_by_id.get(source_ref)
            adversary = adversaries_by_id.get(target_ref)
            if campaign is None or adversary is None:
                # Relationship referencing an unknown campaign or intrusion-set:
                # it cannot define a valid grouping, so ignore it. The campaign
                # (if it exists) stays unattributed and falls into the synthetic
                # group below.
                continue

            already_attached = attached_by_adversary.setdefault(target_ref, set())
            if source_ref not in already_attached:
                adversary.campaigns.append(campaign)
                already_attached.add(source_ref)
            attributed_campaigns.add(source_ref)

        # Any campaign never attributed to a known adversary goes under the
        # synthetic "sem ator associado" group, in bundle order. Only add the
        # group when it would hold at least one campaign.
        unattributed = [
            campaigns_by_id[cid] for cid in campaigns_by_id if cid not in attributed_campaigns
        ]
        if unattributed:
            adversaries.append(
                AdversaryStix(
                    stix_id=self.ADVERSARY_UNASSOCIATED_ID,
                    name=self.ADVERSARY_UNASSOCIATED_NAME,
                    campaigns=unattributed,
                )
            )

        return adversaries
