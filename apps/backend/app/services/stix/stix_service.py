"""StixService — locate, list, download and analyse STIX 2.x bundles (task 1.1).

This is the service that powers the flow: *Landing → STIX base listing →
Intelligence dashboard → Campaign/Adversary selection*. It defines the typed
response models used across the ``/api/stix/*`` endpoints, the backend-maintained
constants and the method signatures.

* ``list_bases`` — ordering + downloaded/selectable flag.
* ``read_bundle`` / ``dashboard`` — parsing + counts.
* ``list_campaigns`` / ``list_adversaries`` — campaign and adversary views.
* ``download_base`` — the only network egress, atomic write.

Design alignment
----------------
* Field names on the response models mirror the JSON contracts in the design
  document exactly (``version``, ``name``, ``downloaded``, ``selectable``,
  ``file``, ``counts``, ``stix_id``, ``campaigns`` …), so the handlers in
  :mod:`app.api.stix` and the TS clients map 1:1.
* Path resolution reuses the repo-root strategy from
  :mod:`app.core.sticks_config` (``_repo_root``), the same one
  :class:`~app.services.campaign_service.CaseService` uses: the STIX data
  directory defaults to ``<repo>/sticks/data/stix``. A custom ``data_dir`` and an
  ``http_client`` may be injected (e.g. in tests) for isolation and to keep the
  download path (the only network egress) mockable.
* ``STIX_URL`` / ``STIX_DIR`` come from the existing ``sticks/config/config.py``
  loaded via :func:`~app.core.sticks_config.load_sticks_config` — never
  duplicated here.
* The known-versions index is kept as a backend-maintained constant
  (:data:`VERSOES_CONHECIDAS`) so ``GET /api/stix/bases`` is read-only and never
  performs network egress; only ``download_base`` reaches the network.

Target on-disk filename pattern: ``sticks/data/stix/enterprise-attack-{version}.json``.

This module only ever **reads** from ``sticks/`` (the single write is the
download of a base, into ``sticks/data/stix``).

_Requirements: 2.1, 2.5, 3.3, 4.5_
"""

from __future__ import annotations

import enum
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.core.sticks_config import _repo_root, load_sticks_config
from app.services.campaign_service import CURATED_CASES


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

# Known MITRE ATT&CK Enterprise versions, newest first. Kept as a versioned
# backend constant so ``list_bases`` (and ``GET /api/stix/bases``) never make a
# network call: the index of known versions lives here and the on-disk presence
# is checked live. ``list_bases`` re-sorts this list defensively by semantic
# version, so the stored order is a convenience, not a contract.
VERSOES_CONHECIDAS: list[str] = [
    "19.1",
    "19.0",
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
# Curated-version flag (Req. 5.5, 5.6, 11.5)
# ---------------------------------------------------------------------------

# Single source of truth for the curated STIX version. Version 18 is the only
# base that carries the 8 curated campaigns and the pointer to the curated
# artifacts (``data/dag``, ``data/api``, ``docker-compose.yml``). The
# ``stix_versions`` table is *seeded* from this constant (the DB row is a
# persistable projection of the flag, not an independent source) — keeping the
# flag in code means ``GET /api/stix/bases`` never needs a request-time lookup to
# know which base is curated (Req. 5.5, 11.5).
VERSAO_CURADA: str = "18.0"  # the only version with the 8 curated campaigns (Req. 5.5, 11.5)


def version_is_curated(version: str) -> bool:
    """True iff ``version`` is the curated version (carries the 8 curated campaigns).

    The curated version (:data:`VERSAO_CURADA`, ``"18.0"``) is the only one that
    carries the 8 curated campaigns and the pointer to the curated artifacts; for
    it Stages 3/4/5 resolve their artifacts from the curated directories on disk,
    with no network and no AI (Req. 5.5, 5.6, 11.5). Any other version returns
    ``False``.
    """
    return version == VERSAO_CURADA


# pt-BR description per counted STIX object type (Req. 3.3). The dashboard fills
# each ``TypeCount.description`` from this map; every description is non-empty.
DESCRICOES_TIPO: dict[str, str] = {
    "campaign": "Campanhas de adversário observadas no mundo real.",
    "attack-pattern": "Técnicas e subtécnicas de ataque do ATT&CK.",
    "x-mitre-tactic": "Objetivos táticos do adversário (fases).",
    "intrusion-set": "Grupos/atores de ameaça associados a campanhas.",
    "malware": "Software malicioso catalogado.",
    "relationship": "Ligações entre objetos (ex.: atribuição campanha→ator).",
}

# Logical label (pt-BR) exposed in the dashboard per STIX object type (Req. 3.2).
ROTULO_TIPO: dict[str, str] = {
    "campaign": "campanhas",
    "attack-pattern": "tecnicas",
    "x-mitre-tactic": "taticas",
    "intrusion-set": "intrusion_sets",
    "malware": "malware",
    "relationship": "relacionamentos",
}

# Mapping of the 8 curated campaign slugs (CaseService.CURATED_CASES) to the
# known STIX campaign identifier(s) / canonical name used to resolve the
# "curated" flag (Req. 4.5). ``external_references`` codes (e.g. ``C0010``) take
# priority, with the canonical name as a tolerant fallback.
SLUG_PARA_STIX: dict[str, dict[str, Any]] = {
    "apt41_dust": {"referencias": ["C0040"], "nome_canonico": "APT41 DUST"},
    "c0010": {"referencias": ["C0010"], "nome_canonico": "C0010"},
    "c0026": {"referencias": ["C0026"], "nome_canonico": "C0026"},
    "costaricto": {"referencias": ["C0004"], "nome_canonico": "CostaRicto"},
    "operation_midnighteclipse": {
        "referencias": ["C0036"],
        "nome_canonico": "Operation MidnightEclipse",
    },
    "outer_space": {"referencias": ["C0030"], "nome_canonico": "Operation Outer Space"},
    "salesforce_data_exfiltration": {
        "referencias": [],
        "nome_canonico": "Salesforce Data Exfiltration",
    },
    "shadowray": {"referencias": ["C0045"], "nome_canonico": "ShadowRay"},
}

# Sanity check: the mapping must cover exactly the 8 curated campaign slugs.
assert set(SLUG_PARA_STIX) == set(CURATED_CASES), (
    "SLUG_PARA_STIX must map exactly the curated campaign slugs"
)


# ---------------------------------------------------------------------------
# Typed bundle-read error (Req. 3.4, 3.5)
# ---------------------------------------------------------------------------


class StixLoadErrorType(str, enum.Enum):
    """Kind of failure while reading/parsing a STIX bundle (Req. 3.5).

    Mirrors :class:`app.services.campaign_service.CaseLoadErrorType` (same pt-BR
    values) so the STIX flow distinguishes the three failure modes with the same
    vocabulary the curated-campaign flow already uses:

    * :attr:`MISSING_FILE` — the base was not downloaded (file absent/unreadable);
    * :attr:`MALFORMED_JSON` — the file exists but is not valid JSON;
    * :attr:`INVALID_STRUCTURE` — valid JSON that is not a STIX bundle (not an
      object, or missing the expected ``"objects"`` list).
    """

    MISSING_FILE = "arquivo_ausente"
    MALFORMED_JSON = "json_malformado"
    INVALID_STRUCTURE = "estrutura_invalida"


class StixFileError(Exception):
    """Raised by :meth:`StixService.read_bundle` for a bundle read/parse problem.

    Carries which file failed (:attr:`path`), the failure kind
    (:attr:`error_type`) and a human-readable :attr:`detail`. Follows the exact
    shape of :class:`app.services.campaign_service.CaseFileError` (``error_type``,
    ``path``, ``detail``) so the ``/api/stix/*`` handlers can map it to the
    ``{"detail": {"motivo": "bundle_invalido", ...}}`` 422 contract (Req. 3.5).
    ``read_bundle`` never returns a partial object on failure.
    """

    def __init__(self, error_type: StixLoadErrorType, path: Path, detail: str) -> None:
        self.error_type = error_type
        self.path = Path(path)
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Typed download error (Req. 2.5, 2.7)
# ---------------------------------------------------------------------------


class StixDownloadError(Exception):
    """Raised by :meth:`StixService.download_base` when a base download fails.

    Covers every failure mode of the single network egress — an unknown
    ``version`` (rejected *before* touching disk/network to prevent path/URL
    injection), a network/HTTP error, downloaded content that is not a valid STIX
    bundle, and a write/atomic-rename error. Carries a human-readable
    :attr:`detail` so the ``/api/stix/*`` handler maps it to the
    ``{"detail": {"motivo": "falha_download", "mensagem": ...}}`` 502/422
    contract (Req. 2.7). On any failure no partial/temp file is left behind and
    the base stays ``downloaded == false``.
    """

    def __init__(self, detail: str, version: Optional[str] = None) -> None:
        self.version = version
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Typed response models (field names identical to the design contracts)
# ---------------------------------------------------------------------------


class BaseStixInfo(BaseModel):
    """One MITRE ATT&CK Enterprise base in the listing (Req. 2.1–2.3).

    ``downloaded`` reflects the live presence of
    ``sticks/data/stix/enterprise-attack-{version}.json`` on disk and
    ``selectable == downloaded``. ``file`` is filled only when downloaded.
    """

    version: str
    name: str
    downloaded: bool = False
    selectable: bool = False
    file: Optional[str] = None


class TypeCount(BaseModel):
    """A per-type object count for the intelligence dashboard (Req. 3.2, 3.3).

    ``label`` is the logical label, ``stix_type`` the raw STIX ``type`` and
    ``description`` a non-empty description of what the type means.
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
    description: Optional[str] = None
    curated: bool = False
    curated_slug: Optional[str] = None


class DetailedCampaignStix(BaseModel):
    """Rich fields of a STIX ``campaign`` object for the Stage 1 Input.

    Internal detailed view returned by :meth:`StixService.get_campaign_by_slug`
    with the fields the Stage 1 Input renders (design: Stage 1 Input = slice of
    the campaign in the STIX package). Mirrors the STIX ``campaign`` shape:
    ``stix_id`` from ``id``, ``name`` from ``name``, ``external_id`` from the
    ``external_references`` ``external_id`` (e.g. ``C0045``), plus the optional
    ``description``, ``aliases`` and ``first_seen`` / ``last_seen`` dates. Only
    ``stix_id`` and ``name`` are guaranteed; the remaining fields are omitted
    (``None`` / empty) when absent in the bundle object.
    """

    stix_id: str
    name: str
    external_id: Optional[str] = None
    description: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None


class AdversaryStix(BaseModel):
    """A STIX ``intrusion-set`` object with its attributed campaigns (Req. 4.3)."""

    stix_id: str
    name: str
    campaigns: list[CampaignStix] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StixService:
    """Reads, lists, downloads and analyses STIX 2.x enterprise-attack bundles.

    Path resolution is consistent with :class:`CaseService`: the STIX data
    directory is located relative to the repository root
    (``<repo>/sticks/data/stix``), never hard-coded. A custom ``data_dir`` and an
    injectable ``http_client`` keep the service testable and the single network
    egress (``download_base``) mockable.
    """

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        http_client: Any = None,
    ) -> None:
        # Default to <repo>/sticks/data/stix, resolved via the same repo-root
        # strategy used by sticks_config / CaseService. All paths are pathlib
        # for portability.
        self._data_dir: Path = (
            Path(data_dir) if data_dir is not None else _repo_root() / "sticks" / "data" / "stix"
        )
        self._http_client = http_client

        # STIX source URL and configured dir from the existing sticks config —
        # read, never duplicated. Resolution is defensive so the service stays
        # importable even if a value is absent.
        config = load_sticks_config()
        self._stix_url: Optional[str] = getattr(config, "STIX_URL", None)
        self._stix_dir_config = getattr(config, "STIX_DIR", None)

    # ---- path resolution ---------------------------------------------------

    @property
    def data_dir(self) -> Path:
        """The resolved STIX data directory (``<repo>/sticks/data/stix``)."""
        return self._data_dir

    def base_path(self, version: str) -> Path:
        """Path to ``sticks/data/stix/enterprise-attack-{version}.json``."""
        return self._data_dir / f"enterprise-attack-{version}.json"

    # ---- public API --------------------------------------------------------

    def list_bases(self) -> list[BaseStixInfo]:
        """Known versions ordered newest→oldest, each flagged by disk presence.

        Enumerates :data:`VERSOES_CONHECIDAS` sorted monotonically descending by
        semantic version; marks ``downloaded`` from the live file presence in
        ``sticks/data/stix`` and ``selectable == downloaded``. No network egress.

        The sort is defensive — it does not rely on the stored order of
        :data:`VERSOES_CONHECIDAS` and compares each component **numerically**
        (so ``"18.0" > "9.0"`` and ``"17.1" > "17.0"``, never a lexicographic
        comparison). ``downloaded`` is checked live against disk on every call,
        so the listing always reflects the current state of
        ``sticks/data/stix``; ``file`` is the resolved path only when the base is
        on disk, otherwise ``None`` (Req. 2.1, 2.2, 2.3).
        """
        sorted_versions = sorted(
            VERSOES_CONHECIDAS, key=_semantic_version_key, reverse=True
        )

        bases: list[BaseStixInfo] = []
        for version in sorted_versions:
            path = self.base_path(version)
            downloaded = path.exists()
            bases.append(
                BaseStixInfo(
                    version=version,
                    name=f"Enterprise ATT&CK v{version}",
                    downloaded=downloaded,
                    selectable=downloaded,
                    file=str(path) if downloaded else None,
                )
            )
        return bases

    def download_base(self, version: str) -> BaseStixInfo:
        """Download the ``enterprise-attack-{version}`` bundle and persist it.

        The ONLY network egress, triggered only by explicit request. Validates
        ``version`` against :data:`VERSOES_CONHECIDAS`, fetches via the injectable
        ``http_client`` and writes atomically into ``sticks/data/stix``; on
        failure leaves no partial file and signals a descriptive error.

        Behaviour (Req. 2.5, 2.6, 2.7):

        1. **Validate first (security).** ``version`` is checked against
           :data:`VERSOES_CONHECIDAS` *before* any disk or network access. An
           unknown version raises :class:`StixDownloadError` immediately, so no
           caller-controlled value is ever interpolated into a filesystem path or
           a URL — closing the path/URL-injection vector.
        2. **Fetch.** The versioned bundle URL is derived from
           :attr:`_stix_url` (its directory, with the filename swapped for
           ``enterprise-attack-{version}.json``), falling back to the canonical
           ``raw.githubusercontent.com/mitre-attack/attack-stix-data/master/
           enterprise-attack/`` base when the config URL is absent. The GET uses
           the injectable :attr:`_http_client` when provided (so tests mock the
           single network egress) or :func:`requests.get` otherwise, always with
           a finite timeout and an HTTP status check.
        3. **Validate content.** The downloaded body must be JSON with an
           ``"objects"`` list (the same structural contract as
           :meth:`read_bundle`). Anything else is treated as a download failure
           and nothing is written.
        4. **Atomic write.** The content is written to a temporary file in the
           *same* directory as the target, flushed + ``fsync``'d, then
           :func:`os.replace` renames it over
           ``sticks/data/stix/enterprise-attack-{version}.json`` — an atomic
           swap, so a reader never observes a half-written bundle. On *any*
           failure the temp file is removed and no partial file is left; the base
           stays ``downloaded == false``.

        Args:
            version: The ATT&CK Enterprise version (must be in
                :data:`VERSOES_CONHECIDAS`).

        Returns:
            A :class:`BaseStixInfo` with ``downloaded=True`` and ``file`` set to
            the resolved on-disk path on success.

        Raises:
            StixDownloadError: when ``version`` is unknown, the fetch fails, the
                content is not a valid STIX bundle, or the write/rename fails. No
                partial file remains in either case.
        """
        # 1) SECURITY — validate before any disk/network access. An unknown
        #    version never reaches a path or a URL (no path/URL injection).
        if version not in VERSOES_CONHECIDAS:
            raise StixDownloadError(
                f"Versão desconhecida '{version}': não está na lista de versões "
                "conhecidas; download recusado sem acessar disco ou rede.",
                version=version,
            )

        url = self._versioned_url(version)
        target = self.base_path(version)

        # 2) Fetch via the injectable client (tests) or requests (production).
        try:
            content = self._download_content(url)
        except StixDownloadError:
            raise
        except Exception as exc:  # noqa: BLE001 — any client/transport error.
            raise StixDownloadError(
                f"Não foi possível obter a base v{version} do repositório "
                f"MITRE ATT&CK: {exc}.",
                version=version,
            ) from exc

        # 3) Validate content BEFORE committing: must be a STIX bundle (JSON
        #    object carrying an "objects" list). Invalid → download failure.
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

        # 4) Atomic write: temp file in the SAME directory, fsync, then
        #    os.replace. On any failure remove the temp file — never a partial.
        temp_path: Optional[str] = None
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".tmp",
                prefix=f"enterprise-attack-{version}.",
                dir=self._data_dir,
                delete=False,
            ) as tmp:
                temp_path = tmp.name
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(temp_path, target)
            temp_path = None
        except OSError as exc:
            raise StixDownloadError(
                f"Falha ao gravar a base v{version} em {target}: {exc}.",
                version=version,
            ) from exc
        finally:
            # Any leftover temp file (write/rename failed mid-way) is removed so
            # no partial file remains on disk.
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        return BaseStixInfo(
            version=version,
            name=f"Enterprise ATT&CK v{version}",
            downloaded=True,
            selectable=True,
            file=str(target),
        )

    # Canonical raw base used when STIX_URL is absent from the config.
    _URL_BASE_CANONICA = (
        "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/"
        "master/enterprise-attack/"
    )

    # HTTP timeout (seconds) for the single network egress — finite so a hung
    # server can never block the request indefinitely.
    _DOWNLOAD_TIMEOUT_S = 60

    def _versioned_url(self, version: str) -> str:
        """Build the versioned bundle URL for an already-validated ``version``.

        Derives the directory from :attr:`_stix_url` (``.../enterprise-attack/``)
        and appends ``enterprise-attack-{version}.json``, falling back to the
        canonical ``raw.githubusercontent.com`` base when the configured URL is
        absent. ``version`` is assumed to be on the allowlist (validated by the
        caller), so the result is injection-safe.
        """
        base = self._stix_url
        if isinstance(base, str) and base.strip():
            # Keep everything up to and including the last "/" (the directory).
            directory = base.rsplit("/", 1)[0] + "/"
        else:
            directory = self._URL_BASE_CANONICA
        return f"{directory}enterprise-attack-{version}.json"

    def _download_content(self, url: str) -> str:
        """GET ``url`` and return its body as text, raising on a non-OK status.

        Uses the injected :attr:`_http_client` when present (tests mock the only
        network egress), otherwise :func:`requests.get`. A finite timeout is
        always applied and a non-2xx response is turned into an error.
        """
        client = self._http_client
        if client is not None:
            response = client.get(url, timeout=self._DOWNLOAD_TIMEOUT_S)
        else:
            import requests  # Local import: only the network path needs it.

            response = requests.get(url, timeout=self._DOWNLOAD_TIMEOUT_S)

        # Prefer raise_for_status when available; otherwise check status_code.
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
        else:
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and not (200 <= status < 300):
                raise StixDownloadError(
                    f"Resposta HTTP inesperada ({status}) ao baixar {url}."
                )

        text = getattr(response, "text", None)
        if text is None:
            body = getattr(response, "content", None)
            if isinstance(body, (bytes, bytearray)):
                text = body.decode("utf-8")
        if not isinstance(text, str):
            raise StixDownloadError(
                f"Resposta sem corpo de texto utilizável ao baixar {url}."
            )
        return text

    def read_bundle(self, version: str) -> dict:
        """Read and parse the ``enterprise-attack-{version}`` bundle (Req. 3.4, 3.5).

        Reads ``sticks/data/stix/enterprise-attack-{version}.json`` (resolved via
        :meth:`base_path`) and returns the parsed STIX bundle ``dict`` on success.
        The returned bundle is always structurally valid: it is a JSON object
        containing an ``"objects"`` list, which the dashboard/campaign derivations
        iterate.

        Distinguishes the three failure modes with a typed :class:`StixFileError`
        and **never** returns a partial object on failure:

        * :attr:`StixLoadErrorType.MISSING_FILE` — the base is not downloaded
          (file absent / unreadable);
        * :attr:`StixLoadErrorType.MALFORMED_JSON` — the file exists but is not
          valid JSON (``json.JSONDecodeError``);
        * :attr:`StixLoadErrorType.INVALID_STRUCTURE` — valid JSON that is not a
          STIX bundle (not an object, or without the expected ``"objects"`` list).

        Reads with an explicit UTF-8 encoding for portability (Windows dev env).

        Args:
            version: The ATT&CK Enterprise version (e.g. ``"19.1"``).

        Returns:
            The parsed STIX bundle ``dict`` (guaranteed to carry an ``"objects"``
            list).

        Raises:
            StixFileError: when the file is missing/unreadable, contains malformed
                JSON, or is not a valid STIX bundle structure.
        """
        path = self.base_path(version)
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError as exc:
            raise StixFileError(
                StixLoadErrorType.MISSING_FILE,
                path,
                f"Base v{version} não baixada: arquivo ausente em {path}.",
            ) from exc
        except IsADirectoryError as exc:
            raise StixFileError(
                StixLoadErrorType.MISSING_FILE,
                path,
                f"Caminho da base v{version} esperado como arquivo é um diretório: {path}.",
            ) from exc
        except json.JSONDecodeError as exc:
            raise StixFileError(
                StixLoadErrorType.MALFORMED_JSON,
                path,
                f"Bundle da base v{version} está malformado (JSON inválido) em {path}: {exc}.",
            ) from exc
        except OSError as exc:
            # Any other read error (permissions, etc.) — treat as not downloaded
            # so the caller never gets a partial object.
            raise StixFileError(
                StixLoadErrorType.MISSING_FILE,
                path,
                f"Bundle da base v{version} ilegível em {path}: {exc}.",
            ) from exc

        if not isinstance(raw, dict):
            raise StixFileError(
                StixLoadErrorType.INVALID_STRUCTURE,
                path,
                (
                    f"Bundle da base v{version} tem estrutura inválida em {path}: "
                    f"esperava um objeto JSON, obteve {type(raw).__name__}."
                ),
            )
        if not isinstance(raw.get("objects"), list):
            raise StixFileError(
                StixLoadErrorType.INVALID_STRUCTURE,
                path,
                (
                    f"Bundle da base v{version} tem estrutura inválida em {path}: "
                    'não contém a lista "objects" esperada de um bundle STIX.'
                ),
            )
        return raw

    def dashboard(self, version: str) -> DashboardStix:
        """Per-type object counts (+ descriptions) for the selected base.

        Reads the bundle via :meth:`read_bundle` (which already validates and
        raises the typed :class:`StixFileError` for a missing/malformed/invalid
        base) and derives one :class:`TypeCount` per counted STIX ``type``. The
        error is deliberately **not** caught here: it propagates so the endpoint
        maps it to the ``bundle_invalido`` 422 contract (Req. 3.5) and no partial
        set of counts is ever returned.

        The result always carries the six required types in a stable order — the
        insertion order of :data:`ROTULO_TIPO` (``campaign``, ``attack-pattern``,
        ``x-mitre-tactic``, ``intrusion-set``, ``malware``, ``relationship``) —
        even when a type has zero objects, so the dashboard is deterministic
        across bases (Req. 3.2). For each type: ``count`` is exactly the number of
        ``bundle["objects"]`` whose ``"type"`` equals ``stix_type``; ``label`` is
        the label from :data:`ROTULO_TIPO`; ``description`` is the non-empty
        description from :data:`DESCRICOES_TIPO` (Req. 3.3); and ``stix_type`` is
        the raw STIX type.

        Satisfies Correctness Properties 3 (count equals the number of objects of
        that type) and 4 (every ``description`` is non-empty).

        Args:
            version: The ATT&CK Enterprise version (e.g. ``"19.1"``).

        Returns:
            A :class:`DashboardStix` with ``version``, ``name`` and the per-type
            ``counts``.

        Raises:
            StixFileError: propagated from :meth:`read_bundle` when the base is
                missing/unreadable, malformed, or not a valid STIX bundle.
        """
        bundle = self.read_bundle(version)

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
                    description=DESCRICOES_TIPO[stix_type],
                )
            )

        return DashboardStix(
            version=version,
            name=f"Enterprise ATT&CK v{version}",
            counts=counts,
        )

    def list_campaigns(self, version: str) -> list[CampaignStix]:
        """STIX ``campaign`` objects of a base with the curated-campaign flag resolved.

        Reads the bundle via :meth:`read_bundle` (whose typed
        :class:`StixFileError` is deliberately **not** caught here — it propagates
        so the endpoint maps it to the ``bundle_invalido`` 422 contract, Req. 3.5)
        and selects every object whose ``type`` is ``"campaign"``, in the **exact
        order** they appear in ``bundle["objects"]`` — never dropping nor
        duplicating any campaign object (Correctness Property 5).

        For each campaign a :class:`CampaignStix` is built with ``stix_id`` from
        ``id``, ``name`` from ``name`` (falling back to ``id`` when absent) and the
        optional ``description``. The ``curated`` / ``curated_slug`` pair is
        resolved against :data:`SLUG_PARA_STIX` tolerantly (Req. 4.5, Correctness
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
            version: The ATT&CK Enterprise version (e.g. ``"19.1"``).

        Returns:
            The list of :class:`CampaignStix`, preserving the bundle order.

        Raises:
            StixFileError: propagated from :meth:`read_bundle` when the base is
                missing/unreadable, malformed, or not a valid STIX bundle.
        """
        bundle = self.read_bundle(version)

        campaigns: list[CampaignStix] = []
        for obj in bundle["objects"]:
            if not isinstance(obj, dict) or obj.get("type") != "campaign":
                continue

            campaign = self._map_campaign(obj)
            if campaign is not None:
                campaigns.append(campaign)

        return campaigns

    def get_campaign_by_slug(
        self, version: str, slug: str
    ) -> Optional[DetailedCampaignStix]:
        """Resolve a curated slug to the rich STIX ``campaign`` of a base (task 1.1).

        Reads the base bundle via :meth:`read_bundle`, scans every object whose
        ``type == "campaign"`` and returns the rich detail of the **first**
        campaign whose resolved curated slug equals ``slug``. The slug resolution
        reuses the single source of truth :meth:`_resolve_curated_slug` /
        :data:`SLUG_PARA_STIX` — the same tolerant rule (``external_id`` first,
        canonical name as fallback) used by :meth:`list_campaigns` /
        :meth:`_map_campaign`, so this view never diverges from the campaigns
        screen (Correctness Property 2). The rule is **not** duplicated here.

        The returned :class:`DetailedCampaignStix` carries the fields the Stage 1
        Input renders, extracted from the matched ``campaign`` object: ``stix_id``
        (``id``), ``name`` (``name`` with a fallback to ``id``), ``external_id``
        (the first usable ``external_id`` in ``external_references``),
        ``description``, ``aliases``, ``first_seen`` and ``last_seen``. Fields
        absent in the object are left ``None`` / empty so the Input can omit them.

        Returns ``None`` when no campaign in the base resolves to ``slug``.

        Args:
            version: The ATT&CK Enterprise version (e.g. ``"18.0"``).
            slug: The curated campaign slug (e.g. ``"shadowray"``).

        Returns:
            The :class:`DetailedCampaignStix` of the first matching campaign, or
            ``None`` when none matches.

        Raises:
            StixFileError: propagated from :meth:`read_bundle` when the base is
                missing/unreadable, malformed, or not a valid STIX bundle. The
                caller (StageService) treats this as the informative fallback —
                the Input never breaks the Stage.
        """
        bundle = self.read_bundle(version)

        for obj in bundle["objects"]:
            if not isinstance(obj, dict) or obj.get("type") != "campaign":
                continue

            # Reuse the single resolution rule — never duplicated (Property 2).
            if self._resolve_curated_slug(obj) != slug:
                continue

            stix_id = obj.get("id")
            if not isinstance(stix_id, str):
                # A campaign without a usable id cannot be referenced; keep
                # scanning in case a later object resolves to the same slug.
                continue

            raw_name = obj.get("name")
            name = raw_name if isinstance(raw_name, str) and raw_name else stix_id

            raw_description = obj.get("description")
            description = raw_description if isinstance(raw_description, str) else None

            external_id = self._extract_external_id(obj)

            raw_aliases = obj.get("aliases")
            aliases = (
                [a for a in raw_aliases if isinstance(a, str) and a]
                if isinstance(raw_aliases, list)
                else []
            )

            raw_first_seen = obj.get("first_seen")
            first_seen = raw_first_seen if isinstance(raw_first_seen, str) else None

            raw_last_seen = obj.get("last_seen")
            last_seen = raw_last_seen if isinstance(raw_last_seen, str) else None

            return DetailedCampaignStix(
                stix_id=stix_id,
                name=name,
                external_id=external_id,
                description=description,
                aliases=aliases,
                first_seen=first_seen,
                last_seen=last_seen,
            )

        return None

    @staticmethod
    def _extract_external_id(obj: dict[str, Any]) -> Optional[str]:
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
    def _map_campaign(cls, obj: dict[str, Any]) -> Optional[CampaignStix]:
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
    def _resolve_curated_slug(obj: dict[str, Any]) -> Optional[str]:
        """Resolve the curated-campaign slug for a STIX ``campaign`` object, or ``None``.

        Tolerant matching against :data:`SLUG_PARA_STIX` (Req. 4.5): first by
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
            for slug, mapping in SLUG_PARA_STIX.items():
                for code in mapping.get("referencias", []):
                    if isinstance(code, str) and code.strip().casefold() in codes:
                        return slug

        # 2) Match by normalized canonical name.
        raw_name = obj.get("name")
        if isinstance(raw_name, str):
            normalized_name = raw_name.strip().casefold()
            if normalized_name:
                for slug, mapping in SLUG_PARA_STIX.items():
                    canonical = mapping.get("nome_canonico")
                    if (
                        isinstance(canonical, str)
                        and canonical.strip().casefold() == normalized_name
                    ):
                        return slug

        # 3) Silent fallback — nothing matched.
        return None

    # Synthetic group holding campaigns with no ``attributed-to`` relationship.
    ATOR_SEM_ASSOCIACAO_ID = "sem-ator"
    ATOR_SEM_ASSOCIACAO_NOME = "Sem ator associado"

    def list_adversaries(self, version: str) -> list[AdversaryStix]:
        """STIX ``intrusion-set`` objects grouped with their attributed campaigns.

        Reads the bundle via :meth:`read_bundle` (whose typed
        :class:`StixFileError` is deliberately **not** caught here — it propagates
        so the endpoint maps it to the ``bundle_invalido`` 422 contract, Req. 3.5).

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
          :data:`ATOR_SEM_ASSOCIACAO_ID`), appended last — and only when there is
          at least one unattributed campaign.

        Args:
            version: The ATT&CK Enterprise version (e.g. ``"19.1"``).

        Returns:
            The list of :class:`AdversaryStix` (intrusion-sets in bundle order,
            optionally followed by the synthetic unattributed group).

        Raises:
            StixFileError: propagated from :meth:`read_bundle` when the base is
                missing/unreadable, malformed, or not a valid STIX bundle.
        """
        bundle = self.read_bundle(version)
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
            campaigns_by_id[cid]
            for cid in campaigns_by_id
            if cid not in attributed_campaigns
        ]
        if unattributed:
            adversaries.append(
                AdversaryStix(
                    stix_id=self.ATOR_SEM_ASSOCIACAO_ID,
                    name=self.ATOR_SEM_ASSOCIACAO_NOME,
                    campaigns=unattributed,
                )
            )

        return adversaries
