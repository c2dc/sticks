"""StixService — localizar, listar, baixar e analisar bundles STIX 2.x (task 1.1).

This is the **skeleton** for the STIX service that powers the new flow:
*Landing → Listagem de Bases STIX → Dashboard de Inteligência → Seleção de
Campanha/Ator*. It defines the typed, pt-BR response models used across the new
``/api/stix/*`` endpoints, the backend-maintained constants and the method
signatures. The full behaviour is implemented in later tasks:

* ``listar_bases`` — task 1.2 (ordenação + flag baixada/selecionável).
* ``ler_bundle`` / ``dashboard`` — tasks 2.1 / 2.2 (parsing + contagens).
* ``listar_campanhas`` / ``listar_atores`` — tasks 3.1 / 3.2.
* ``baixar_base`` — task 4.1 (única saída de rede, escrita atômica).

Design alignment
----------------
* Field names on the response models mirror the pt-BR JSON contracts in the
  design document exactly (``versao``, ``nome``, ``baixada``, ``selecionavel``,
  ``arquivo``, ``contagens``, ``stix_id``, ``campanhas`` …), so the handlers in
  :mod:`app.api.stix` and the TS clients map 1:1.
* Path resolution reuses the repo-root strategy from
  :mod:`app.core.sticks_config` (``_repo_root``), the same one
  :class:`~app.services.case_service.CaseService` uses: the STIX data directory
  defaults to ``<repo>/sticks/data/stix``. A custom ``data_dir`` and an
  ``http_client`` may be injected (e.g. in tests) for isolation and to keep the
  download path (the only network egress) mockable.
* ``STIX_URL`` / ``STIX_DIR`` come from the existing ``sticks/config/config.py``
  loaded via :func:`~app.core.sticks_config.load_sticks_config` — never
  duplicated here.
* The known-versions index is kept as a backend-maintained constant
  (:data:`VERSOES_CONHECIDAS`) so ``GET /api/stix/bases`` is read-only and never
  performs network egress; only ``baixar_base`` reaches the network.

Target on-disk filename pattern: ``sticks/data/stix/enterprise-attack-{versao}.json``.

This module only ever **reads** from ``sticks/`` (the single write is the
download of a base, into ``sticks/data/stix``). It performs **no** network calls
in this skeleton task.

_Requisitos: 2.1, 2.5, 3.3, 4.5_
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
from app.services.case_service import CURATED_CASES


# ---------------------------------------------------------------------------
# Semantic version ordering
# ---------------------------------------------------------------------------


def _chave_versao_semantica(versao: str) -> tuple[int, ...]:
    """Numeric sort key for a ``"MAJOR.MINOR"`` (optionally ``.PATCH``) version.

    Splits on ``.`` and compares each component as an integer so the ordering is
    truly semantic rather than lexicographic: ``"18.0"`` sorts above ``"9.0"``
    and ``"17.1"`` above ``"17.0"``. Non-numeric components degrade gracefully to
    ``0`` so a malformed entry can never raise and break the listing.
    """
    partes: list[int] = []
    for parte in str(versao).split("."):
        try:
            partes.append(int(parte))
        except ValueError:
            partes.append(0)
    return tuple(partes)


# ---------------------------------------------------------------------------
# Backend-maintained constants
# ---------------------------------------------------------------------------

# Known MITRE ATT&CK Enterprise versions, newest first. Kept as a versioned
# backend constant so ``listar_bases`` (and ``GET /api/stix/bases``) never make
# a network call: the index of known versions lives here and the on-disk
# presence is checked live. ``listar_bases`` re-sorts this list defensively by
# semantic version, so the stored order is a convenience, not a contract.
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

# pt-BR description per counted STIX object type (Req. 3.3). The dashboard fills
# each ``ContagemTipo.descricao`` from this map; every description is non-empty.
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

# Mapping of the 8 curated case slugs (CaseService.CURATED_CASES) to the known
# STIX campaign identifier(s) / canonical name used to resolve the "curada"
# flag (Req. 4.5). ``external_references`` codes (e.g. ``C0010``) take priority,
# with the canonical name as a tolerant fallback (resolution in task 3.1).
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

# Sanity check: the mapping must cover exactly the 8 curated case slugs.
assert set(SLUG_PARA_STIX) == set(CURATED_CASES), (
    "SLUG_PARA_STIX must map exactly the curated case slugs"
)


# ---------------------------------------------------------------------------
# Typed bundle-read error (task 2.1, Req. 3.4, 3.5)
# ---------------------------------------------------------------------------


class StixLoadErrorType(str, enum.Enum):
    """Kind of failure while reading/parsing a STIX bundle (Req. 3.5).

    Mirrors :class:`app.services.case_service.CaseLoadErrorType` (same pt-BR
    values) so the STIX flow distinguishes the three failure modes with the same
    vocabulary the curated-case flow already uses:

    * :attr:`MISSING_FILE` — the base was not downloaded (file absent/unreadable);
    * :attr:`MALFORMED_JSON` — the file exists but is not valid JSON;
    * :attr:`INVALID_STRUCTURE` — valid JSON that is not a STIX bundle (not an
      object, or missing the expected ``"objects"`` list).
    """

    MISSING_FILE = "arquivo_ausente"
    MALFORMED_JSON = "json_malformado"
    INVALID_STRUCTURE = "estrutura_invalida"


class StixFileError(Exception):
    """Raised by :meth:`StixService.ler_bundle` for a bundle read/parse problem.

    Carries which file failed (:attr:`path`), the failure kind
    (:attr:`error_type`) and a human-readable pt-BR :attr:`detail`. Follows the
    exact shape of :class:`app.services.case_service.CaseFileError`
    (``error_type``, ``path``, ``detail``) so the ``/api/stix/*`` handlers can
    map it to the ``{"detail": {"motivo": "bundle_invalido", ...}}`` 422 contract
    (Req. 3.5). ``ler_bundle`` never returns a partial object on failure.
    """

    def __init__(self, error_type: StixLoadErrorType, path: Path, detail: str) -> None:
        self.error_type = error_type
        self.path = Path(path)
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Typed download error (task 4.1, Req. 2.5, 2.7)
# ---------------------------------------------------------------------------


class StixDownloadError(Exception):
    """Raised by :meth:`StixService.baixar_base` when a base download fails.

    Covers every failure mode of the single network egress — an unknown
    ``versao`` (rejected *before* touching disk/network to prevent path/URL
    injection), a network/HTTP error, downloaded content that is not a valid
    STIX bundle, and a write/atomic-rename error. Carries a human-readable
    pt-BR :attr:`detail` so the ``/api/stix/*`` handler maps it to the
    ``{"detail": {"motivo": "falha_download", "mensagem": ...}}`` 502/422
    contract (Req. 2.7). On any failure no partial/temp file is left behind and
    the base stays ``baixada == false``.
    """

    def __init__(self, detail: str, versao: Optional[str] = None) -> None:
        self.versao = versao
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Typed response models (pt-BR field names identical to the design contracts)
# ---------------------------------------------------------------------------


class BaseStixInfo(BaseModel):
    """One MITRE ATT&CK Enterprise base in the listing (Req. 2.1–2.3).

    ``baixada`` reflects the live presence of
    ``sticks/data/stix/enterprise-attack-{versao}.json`` on disk and
    ``selecionavel == baixada``. ``arquivo`` is filled only when downloaded.
    """

    versao: str
    nome: str
    baixada: bool = False
    selecionavel: bool = False
    arquivo: Optional[str] = None


class ContagemTipo(BaseModel):
    """A per-type object count for the intelligence dashboard (Req. 3.2, 3.3).

    ``tipo`` is the pt-BR label, ``stix_type`` the raw STIX ``type`` and
    ``descricao`` a non-empty pt-BR description of what the type means.
    """

    tipo: str
    stix_type: str
    quantidade: int
    descricao: str


class DashboardStix(BaseModel):
    """The intelligence dashboard of a selected base (Req. 3.1, 3.2)."""

    versao: str
    nome: str
    contagens: list[ContagemTipo] = Field(default_factory=list)


class CampanhaStix(BaseModel):
    """A STIX ``campaign`` object with the curated-case flag (Req. 4.2, 4.5).

    ``curada`` is ``True`` (with ``slug_curado`` filled) iff the campaign maps
    to one of the 8 curated cases; otherwise ``curada == False`` and
    ``slug_curado is None``.
    """

    stix_id: str
    nome: str
    descricao: Optional[str] = None
    curada: bool = False
    slug_curado: Optional[str] = None


class CampanhaStixDetalhada(BaseModel):
    """Rich fields of a STIX ``campaign`` object for the Stage 1 Entrada.

    Internal detailed view returned by :meth:`StixService.obter_campanha_por_slug`
    with the fields the Stage 1 Entrada renders (design: Entrada do Estágio 1 =
    recorte da campanha no pacote STIX). Mirrors the STIX ``campaign`` shape:
    ``stix_id`` from ``id``, ``nome`` from ``name``, ``external_id`` from the
    ``external_references`` ``external_id`` (e.g. ``C0045``), plus the optional
    ``descricao``, ``aliases`` and ``first_seen`` / ``last_seen`` dates. Only
    ``stix_id`` and ``nome`` are guaranteed; the remaining fields are omitted
    (``None`` / empty) when absent in the bundle object.
    """

    stix_id: str
    nome: str
    external_id: Optional[str] = None
    descricao: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None


class AtorStix(BaseModel):
    """A STIX ``intrusion-set`` object with its attributed campaigns (Req. 4.3)."""

    stix_id: str
    nome: str
    campanhas: list[CampanhaStix] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StixService:
    """Reads, lists, downloads and analyses STIX 2.x enterprise-attack bundles.

    Path resolution is consistent with :class:`CaseService`: the STIX data
    directory is located relative to the repository root
    (``<repo>/sticks/data/stix``), never hard-coded. A custom ``data_dir`` and
    an injectable ``http_client`` keep the service testable and the single
    network egress (``baixar_base``) mockable.

    This is a skeleton (task 1.1): the methods below define the public surface
    and raise :class:`NotImplementedError` until the later tasks implement them.
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
        # read, never duplicated. Resolution is defensive so the skeleton stays
        # importable even if a value is absent.
        config = load_sticks_config()
        self._stix_url: Optional[str] = getattr(config, "STIX_URL", None)
        self._stix_dir_config = getattr(config, "STIX_DIR", None)

    # ---- path resolution ---------------------------------------------------

    @property
    def data_dir(self) -> Path:
        """The resolved STIX data directory (``<repo>/sticks/data/stix``)."""
        return self._data_dir

    def base_path(self, versao: str) -> Path:
        """Path to ``sticks/data/stix/enterprise-attack-{versao}.json``."""
        return self._data_dir / f"enterprise-attack-{versao}.json"

    # ---- public API (stubs — implemented in later tasks) -------------------

    def listar_bases(self) -> list[BaseStixInfo]:
        """Known versions ordered newest→oldest, each flagged by disk presence.

        Enumerates :data:`VERSOES_CONHECIDAS` sorted monotonically descending by
        semantic version; marks ``baixada`` from the live file presence in
        ``sticks/data/stix`` and ``selecionavel == baixada``. No network egress.

        The sort is defensive — it does not rely on the stored order of
        :data:`VERSOES_CONHECIDAS` and compares each component **numerically**
        (so ``"18.0" > "9.0"`` and ``"17.1" > "17.0"``, never a lexicographic
        comparison). ``baixada`` is checked live against disk on every call, so
        the listing always reflects the current state of
        ``sticks/data/stix``; ``arquivo`` is the resolved path only when the
        base is on disk, otherwise ``None`` (Req. 2.1, 2.2, 2.3).
        """
        versoes_ordenadas = sorted(
            VERSOES_CONHECIDAS, key=_chave_versao_semantica, reverse=True
        )

        bases: list[BaseStixInfo] = []
        for versao in versoes_ordenadas:
            caminho = self.base_path(versao)
            baixada = caminho.exists()
            bases.append(
                BaseStixInfo(
                    versao=versao,
                    nome=f"Enterprise ATT&CK v{versao}",
                    baixada=baixada,
                    selecionavel=baixada,
                    arquivo=str(caminho) if baixada else None,
                )
            )
        return bases

    def baixar_base(self, versao: str) -> BaseStixInfo:
        """Download the ``enterprise-attack-{versao}`` bundle and persist it.

        The ONLY network egress, triggered only by explicit request. Validates
        ``versao`` against :data:`VERSOES_CONHECIDAS`, fetches via the injectable
        ``http_client`` and writes atomically into ``sticks/data/stix``; on
        failure leaves no partial file and signals a descriptive error.

        Behaviour (Req. 2.5, 2.6, 2.7):

        1. **Validate first (security).** ``versao`` is checked against
           :data:`VERSOES_CONHECIDAS` *before* any disk or network access. An
           unknown version raises :class:`StixDownloadError` immediately, so no
           caller-controlled value is ever interpolated into a filesystem path
           or a URL — closing the path/URL-injection vector. Because the version
           is on the allowlist, the derived URL and target path are safe.
        2. **Fetch.** The versioned bundle URL is derived from
           :attr:`_stix_url` (its directory, with the filename swapped for
           ``enterprise-attack-{versao}.json``), falling back to the canonical
           ``raw.githubusercontent.com/mitre-attack/attack-stix-data/master/
           enterprise-attack/`` base when the config URL is absent. The GET uses
           the injectable :attr:`_http_client` when provided (so tests mock the
           single network egress) or :func:`requests.get` otherwise, always with
           a finite timeout and an HTTP status check.
        3. **Validate content.** The downloaded body must be JSON with an
           ``"objects"`` list (the same structural contract as
           :meth:`ler_bundle`). Anything else is treated as a download failure
           and nothing is written.
        4. **Atomic write.** The content is written to a temporary file in the
           *same* directory as the target, flushed + ``fsync``'d, then
           :func:`os.replace` renames it over
           ``sticks/data/stix/enterprise-attack-{versao}.json`` — an atomic
           swap, so a reader never observes a half-written bundle. On *any*
           failure the temp file is removed and no partial file is left; the
           base stays ``baixada == false``.

        Args:
            versao: The ATT&CK Enterprise version (must be in
                :data:`VERSOES_CONHECIDAS`).

        Returns:
            A :class:`BaseStixInfo` with ``baixada=True`` and ``arquivo`` set to
            the resolved on-disk path on success.

        Raises:
            StixDownloadError: when ``versao`` is unknown, the fetch fails, the
                content is not a valid STIX bundle, or the write/rename fails.
                No partial file remains in either case.
        """
        # 1) SECURITY — validate before any disk/network access. An unknown
        #    version never reaches a path or a URL (no path/URL injection).
        if versao not in VERSOES_CONHECIDAS:
            raise StixDownloadError(
                f"Versão desconhecida '{versao}': não está na lista de versões "
                "conhecidas; download recusado sem acessar disco ou rede.",
                versao=versao,
            )

        url = self._url_versionada(versao)
        destino = self.base_path(versao)

        # 2) Fetch via the injectable client (tests) or requests (production).
        try:
            conteudo = self._baixar_conteudo(url)
        except StixDownloadError:
            raise
        except Exception as exc:  # noqa: BLE001 — any client/transport error.
            raise StixDownloadError(
                f"Não foi possível obter a base v{versao} do repositório "
                f"MITRE ATT&CK: {exc}.",
                versao=versao,
            ) from exc

        # 3) Validate content BEFORE committing: must be a STIX bundle (JSON
        #    object carrying an "objects" list). Invalid → download failure.
        try:
            bundle = json.loads(conteudo)
        except (json.JSONDecodeError, ValueError) as exc:
            raise StixDownloadError(
                f"Conteúdo baixado da base v{versao} não é JSON válido: {exc}.",
                versao=versao,
            ) from exc
        if not isinstance(bundle, dict) or not isinstance(bundle.get("objects"), list):
            raise StixDownloadError(
                f"Conteúdo baixado da base v{versao} não é um bundle STIX válido "
                '(esperava um objeto JSON com a lista "objects").',
                versao=versao,
            )

        # 4) Atomic write: temp file in the SAME directory, fsync, then
        #    os.replace. On any failure remove the temp file — never a partial.
        self._data_dir.mkdir(parents=True, exist_ok=True)
        temp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".tmp",
                prefix=f"enterprise-attack-{versao}.",
                dir=self._data_dir,
                delete=False,
            ) as tmp:
                temp_path = tmp.name
                tmp.write(conteudo)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(temp_path, destino)
            temp_path = None
        except OSError as exc:
            raise StixDownloadError(
                f"Falha ao gravar a base v{versao} em {destino}: {exc}.",
                versao=versao,
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
            versao=versao,
            nome=f"Enterprise ATT&CK v{versao}",
            baixada=True,
            selecionavel=True,
            arquivo=str(destino),
        )

    # Canonical raw base used when STIX_URL is absent from the config.
    _URL_BASE_CANONICA = (
        "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/"
        "master/enterprise-attack/"
    )

    # HTTP timeout (seconds) for the single network egress — finite so a hung
    # server can never block the request indefinitely.
    _DOWNLOAD_TIMEOUT_S = 60

    def _url_versionada(self, versao: str) -> str:
        """Build the versioned bundle URL for an already-validated ``versao``.

        Derives the directory from :attr:`_stix_url` (``.../enterprise-attack/``)
        and appends ``enterprise-attack-{versao}.json``, falling back to the
        canonical ``raw.githubusercontent.com`` base when the configured URL is
        absent. ``versao`` is assumed to be on the allowlist (validated by the
        caller), so the result is injection-safe.
        """
        base = self._stix_url
        if isinstance(base, str) and base.strip():
            # Keep everything up to and including the last "/" (the directory).
            diretorio = base.rsplit("/", 1)[0] + "/"
        else:
            diretorio = self._URL_BASE_CANONICA
        return f"{diretorio}enterprise-attack-{versao}.json"

    def _baixar_conteudo(self, url: str) -> str:
        """GET ``url`` and return its body as text, raising on a non-OK status.

        Uses the injected :attr:`_http_client` when present (tests mock the only
        network egress), otherwise :func:`requests.get`. A finite timeout is
        always applied and a non-2xx response is turned into an error.
        """
        cliente = self._http_client
        if cliente is not None:
            resposta = cliente.get(url, timeout=self._DOWNLOAD_TIMEOUT_S)
        else:
            import requests  # Local import: only the network path needs it.

            resposta = requests.get(url, timeout=self._DOWNLOAD_TIMEOUT_S)

        # Prefer raise_for_status when available; otherwise check status_code.
        raise_for_status = getattr(resposta, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
        else:
            status = getattr(resposta, "status_code", None)
            if isinstance(status, int) and not (200 <= status < 300):
                raise StixDownloadError(
                    f"Resposta HTTP inesperada ({status}) ao baixar {url}."
                )

        texto = getattr(resposta, "text", None)
        if texto is None:
            conteudo = getattr(resposta, "content", None)
            if isinstance(conteudo, (bytes, bytearray)):
                texto = conteudo.decode("utf-8")
        if not isinstance(texto, str):
            raise StixDownloadError(
                f"Resposta sem corpo de texto utilizável ao baixar {url}."
            )
        return texto

    def ler_bundle(self, versao: str) -> dict:
        """Read and parse the ``enterprise-attack-{versao}`` bundle (Req. 3.4, 3.5).

        Reads ``sticks/data/stix/enterprise-attack-{versao}.json`` (resolved via
        :meth:`base_path`) and returns the parsed STIX bundle ``dict`` on
        success. The returned bundle is always structurally valid: it is a JSON
        object containing an ``"objects"`` list, which the dashboard/campaign
        derivations (tasks 2.2, 3.1, 3.2) iterate.

        Distinguishes the three failure modes with a typed :class:`StixFileError`
        and **never** returns a partial object on failure:

        * :attr:`StixLoadErrorType.MISSING_FILE` — the base is not downloaded
          (file absent / unreadable);
        * :attr:`StixLoadErrorType.MALFORMED_JSON` — the file exists but is not
          valid JSON (``json.JSONDecodeError``);
        * :attr:`StixLoadErrorType.INVALID_STRUCTURE` — valid JSON that is not a
          STIX bundle (not an object, or without the expected ``"objects"``
          list).

        Reads with an explicit UTF-8 encoding for portability (Windows dev env).

        Args:
            versao: The ATT&CK Enterprise version (e.g. ``"19.1"``).

        Returns:
            The parsed STIX bundle ``dict`` (guaranteed to carry an ``"objects"``
            list).

        Raises:
            StixFileError: when the file is missing/unreadable, contains
                malformed JSON, or is not a valid STIX bundle structure.
        """
        caminho = self.base_path(versao)
        try:
            with caminho.open("r", encoding="utf-8") as fh:
                bruto = json.load(fh)
        except FileNotFoundError as exc:
            raise StixFileError(
                StixLoadErrorType.MISSING_FILE,
                caminho,
                f"Base v{versao} não baixada: arquivo ausente em {caminho}.",
            ) from exc
        except IsADirectoryError as exc:
            raise StixFileError(
                StixLoadErrorType.MISSING_FILE,
                caminho,
                f"Caminho da base v{versao} esperado como arquivo é um diretório: {caminho}.",
            ) from exc
        except json.JSONDecodeError as exc:
            raise StixFileError(
                StixLoadErrorType.MALFORMED_JSON,
                caminho,
                f"Bundle da base v{versao} está malformado (JSON inválido) em {caminho}: {exc}.",
            ) from exc
        except OSError as exc:
            # Any other read error (permissions, etc.) — treat as not downloaded
            # so the caller never gets a partial object.
            raise StixFileError(
                StixLoadErrorType.MISSING_FILE,
                caminho,
                f"Bundle da base v{versao} ilegível em {caminho}: {exc}.",
            ) from exc

        if not isinstance(bruto, dict):
            raise StixFileError(
                StixLoadErrorType.INVALID_STRUCTURE,
                caminho,
                (
                    f"Bundle da base v{versao} tem estrutura inválida em {caminho}: "
                    f"esperava um objeto JSON, obteve {type(bruto).__name__}."
                ),
            )
        if not isinstance(bruto.get("objects"), list):
            raise StixFileError(
                StixLoadErrorType.INVALID_STRUCTURE,
                caminho,
                (
                    f"Bundle da base v{versao} tem estrutura inválida em {caminho}: "
                    'não contém a lista "objects" esperada de um bundle STIX.'
                ),
            )
        return bruto

    def dashboard(self, versao: str) -> DashboardStix:
        """Per-type object counts (+ pt-BR descriptions) for the selected base.

        Reads the bundle via :meth:`ler_bundle` (which already validates and
        raises the typed :class:`StixFileError` for a missing/malformed/invalid
        base) and derives one :class:`ContagemTipo` per counted STIX ``type``.
        The error is deliberately **not** caught here: it propagates so the
        endpoint maps it to the ``bundle_invalido`` 422 contract (Req. 3.5) and
        no partial set of counts is ever returned.

        The result always carries the six required types in a stable order — the
        insertion order of :data:`ROTULO_TIPO` (``campaign``, ``attack-pattern``,
        ``x-mitre-tactic``, ``intrusion-set``, ``malware``, ``relationship``) —
        even when a type has zero objects, so the dashboard is deterministic
        across bases (Req. 3.2). For each type: ``quantidade`` is exactly the
        number of ``bundle["objects"]`` whose ``"type"`` equals ``stix_type``;
        ``tipo`` is the pt-BR label from :data:`ROTULO_TIPO`; ``descricao`` is the
        non-empty pt-BR description from :data:`DESCRICOES_TIPO` (Req. 3.3); and
        ``stix_type`` is the raw STIX type.

        Satisfies Correctness Properties 3 (count equals the number of objects of
        that type) and 4 (every ``descricao`` is non-empty).

        Args:
            versao: The ATT&CK Enterprise version (e.g. ``"19.1"``).

        Returns:
            A :class:`DashboardStix` with ``versao``, ``nome`` and the per-type
            ``contagens``.

        Raises:
            StixFileError: propagated from :meth:`ler_bundle` when the base is
                missing/unreadable, malformed, or not a valid STIX bundle.
        """
        bundle = self.ler_bundle(versao)

        # Count every object's raw "type" in a single pass. Objects that are not
        # dicts or lack a string "type" simply do not contribute to any count.
        contagem_por_tipo: dict[str, int] = {}
        for objeto in bundle["objects"]:
            if not isinstance(objeto, dict):
                continue
            stix_type = objeto.get("type")
            if not isinstance(stix_type, str):
                continue
            contagem_por_tipo[stix_type] = contagem_por_tipo.get(stix_type, 0) + 1

        # Emit the required types in ROTULO_TIPO insertion order so the dashboard
        # is stable and always complete (count 0 included when absent).
        contagens: list[ContagemTipo] = []
        for stix_type, rotulo in ROTULO_TIPO.items():
            contagens.append(
                ContagemTipo(
                    tipo=rotulo,
                    stix_type=stix_type,
                    quantidade=contagem_por_tipo.get(stix_type, 0),
                    descricao=DESCRICOES_TIPO[stix_type],
                )
            )

        return DashboardStix(
            versao=versao,
            nome=f"Enterprise ATT&CK v{versao}",
            contagens=contagens,
        )

    def listar_campanhas(self, versao: str) -> list[CampanhaStix]:
        """STIX ``campaign`` objects of a base with the curated-case flag resolved.

        Reads the bundle via :meth:`ler_bundle` (whose typed
        :class:`StixFileError` is deliberately **not** caught here — it
        propagates so the endpoint maps it to the ``bundle_invalido`` 422
        contract, Req. 3.5) and selects every object whose ``type`` is
        ``"campaign"``, in the **exact order** they appear in
        ``bundle["objects"]`` — never dropping nor duplicating any campaign
        object (Correctness Property 5).

        For each campaign a :class:`CampanhaStix` is built with ``stix_id`` from
        ``id``, ``nome`` from ``name`` (falling back to ``id`` when absent) and
        the optional ``descricao``. The ``curada`` / ``slug_curado`` pair is
        resolved against :data:`SLUG_PARA_STIX` tolerantly (Req. 4.5,
        Correctness Property 7):

        1. **By external reference** — if any of the campaign's
           ``external_references`` carries an ``external_id`` (e.g. ``"C0010"``)
           that case-insensitively equals one of a slug's ``"referencias"``
           codes, that slug matches.
        2. **By canonical name** — otherwise, the normalized campaign ``name``
           (``casefold`` + ``strip``) is compared against each slug's
           ``"nome_canonico"`` (also normalized).
        3. **Silent fallback** — if nothing matches, ``curada = False`` and
           ``slug_curado = None``. No campaign is ever invented.

        Args:
            versao: The ATT&CK Enterprise version (e.g. ``"19.1"``).

        Returns:
            The list of :class:`CampanhaStix`, preserving the bundle order.

        Raises:
            StixFileError: propagated from :meth:`ler_bundle` when the base is
                missing/unreadable, malformed, or not a valid STIX bundle.
        """
        bundle = self.ler_bundle(versao)

        campanhas: list[CampanhaStix] = []
        for objeto in bundle["objects"]:
            if not isinstance(objeto, dict) or objeto.get("type") != "campaign":
                continue

            campanha = self._mapear_campanha(objeto)
            if campanha is not None:
                campanhas.append(campanha)

        return campanhas

    def obter_campanha_por_slug(
        self, versao: str, slug: str
    ) -> Optional[CampanhaStixDetalhada]:
        """Resolve a curated slug to the rich STIX ``campaign`` of a base (task 1.1).

        Reads the base bundle via :meth:`ler_bundle`, scans every object whose
        ``type == "campaign"`` and returns the rich detail of the **first**
        campaign whose resolved curated slug equals ``slug``. The slug resolution
        reuses the single source of truth :meth:`_resolver_slug_curado` /
        :data:`SLUG_PARA_STIX` — the same tolerant rule (``external_id`` first,
        canonical name as fallback) used by :meth:`listar_campanhas` /
        :meth:`_mapear_campanha`, so this view never diverges from the campaigns
        screen (Correctness Property 2). The rule is **not** duplicated here.

        The returned :class:`CampanhaStixDetalhada` carries the fields the Stage 1
        Entrada renders, extracted from the matched ``campaign`` object:
        ``stix_id`` (``id``), ``nome`` (``name`` with a fallback to ``id``),
        ``external_id`` (the first usable ``external_id`` in
        ``external_references``), ``descricao``, ``aliases``, ``first_seen`` and
        ``last_seen``. Fields absent in the object are left ``None`` / empty so
        the Entrada can omit them.

        Returns ``None`` when no campaign in the base resolves to ``slug``.

        Args:
            versao: The ATT&CK Enterprise version (e.g. ``"18.0"``).
            slug: The curated case slug (e.g. ``"shadowray"``).

        Returns:
            The :class:`CampanhaStixDetalhada` of the first matching campaign, or
            ``None`` when none matches.

        Raises:
            StixFileError: propagated from :meth:`ler_bundle` when the base is
                missing/unreadable, malformed, or not a valid STIX bundle. The
                caller (StageService) treats this as the informative fallback —
                the Entrada never breaks the Stage.
        """
        bundle = self.ler_bundle(versao)

        for objeto in bundle["objects"]:
            if not isinstance(objeto, dict) or objeto.get("type") != "campaign":
                continue

            # Reuse the single resolution rule — never duplicated (Property 2).
            if self._resolver_slug_curado(objeto) != slug:
                continue

            stix_id = objeto.get("id")
            if not isinstance(stix_id, str):
                # A campaign without a usable id cannot be referenced; keep
                # scanning in case a later object resolves to the same slug.
                continue

            nome_bruto = objeto.get("name")
            nome = nome_bruto if isinstance(nome_bruto, str) and nome_bruto else stix_id

            descricao_bruta = objeto.get("description")
            descricao = descricao_bruta if isinstance(descricao_bruta, str) else None

            external_id = self._extrair_external_id(objeto)

            aliases_brutos = objeto.get("aliases")
            aliases = (
                [a for a in aliases_brutos if isinstance(a, str) and a]
                if isinstance(aliases_brutos, list)
                else []
            )

            first_seen_bruto = objeto.get("first_seen")
            first_seen = first_seen_bruto if isinstance(first_seen_bruto, str) else None

            last_seen_bruto = objeto.get("last_seen")
            last_seen = last_seen_bruto if isinstance(last_seen_bruto, str) else None

            return CampanhaStixDetalhada(
                stix_id=stix_id,
                nome=nome,
                external_id=external_id,
                descricao=descricao,
                aliases=aliases,
                first_seen=first_seen,
                last_seen=last_seen,
            )

        return None

    @staticmethod
    def _extrair_external_id(objeto: dict[str, Any]) -> Optional[str]:
        """Return the first usable ``external_id`` of a STIX object, or ``None``.

        Scans ``external_references`` and returns the first non-empty
        ``external_id`` string (e.g. ``"C0045"``), preserving its original form.
        """
        referencias = objeto.get("external_references")
        if not isinstance(referencias, list):
            return None
        for referencia in referencias:
            if not isinstance(referencia, dict):
                continue
            external_id = referencia.get("external_id")
            if isinstance(external_id, str) and external_id.strip():
                return external_id.strip()
        return None

    @classmethod
    def _mapear_campanha(cls, objeto: dict[str, Any]) -> Optional[CampanhaStix]:
        """Map a STIX ``campaign`` object to a :class:`CampanhaStix`, or ``None``.

        Single source of truth for building a campaign entry — used by **both**
        :meth:`listar_campanhas` and :meth:`listar_atores` so the ``curada`` /
        ``slug_curado`` resolution is identical in the campaign view and in the
        nested actor view (Correctness Property 7).

        Builds ``stix_id`` from ``id``, ``nome`` from ``name`` (falling back to
        ``id`` when absent), the optional ``descricao``, and resolves the
        ``curada`` / ``slug_curado`` pair via :meth:`_resolver_slug_curado`.
        Returns ``None`` for a campaign object without a usable string ``id`` (it
        cannot be referenced downstream), so callers simply skip it rather than
        emit a half-formed entry.
        """
        stix_id = objeto.get("id")
        if not isinstance(stix_id, str):
            return None

        nome_bruto = objeto.get("name")
        nome = nome_bruto if isinstance(nome_bruto, str) and nome_bruto else stix_id

        descricao_bruta = objeto.get("description")
        descricao = descricao_bruta if isinstance(descricao_bruta, str) else None

        slug_curado = cls._resolver_slug_curado(objeto)

        return CampanhaStix(
            stix_id=stix_id,
            nome=nome,
            descricao=descricao,
            curada=slug_curado is not None,
            slug_curado=slug_curado,
        )

    @staticmethod
    def _resolver_slug_curado(objeto: dict[str, Any]) -> Optional[str]:
        """Resolve the curated-case slug for a STIX ``campaign`` object, or ``None``.

        Tolerant matching against :data:`SLUG_PARA_STIX` (Req. 4.5): first by
        ``external_references`` ``external_id`` codes (case-insensitive), then by
        normalized canonical name. Returns the matching slug, or ``None`` when no
        curated case matches (silent fallback — a campaign is never invented).
        """
        # 1) Match by external_references external_id (e.g. "C0010").
        codigos: set[str] = set()
        referencias = objeto.get("external_references")
        if isinstance(referencias, list):
            for referencia in referencias:
                if not isinstance(referencia, dict):
                    continue
                external_id = referencia.get("external_id")
                if isinstance(external_id, str) and external_id.strip():
                    codigos.add(external_id.strip().casefold())

        if codigos:
            for slug, mapeamento in SLUG_PARA_STIX.items():
                for codigo in mapeamento.get("referencias", []):
                    if isinstance(codigo, str) and codigo.strip().casefold() in codigos:
                        return slug

        # 2) Match by normalized canonical name.
        nome_bruto = objeto.get("name")
        if isinstance(nome_bruto, str):
            nome_normalizado = nome_bruto.strip().casefold()
            if nome_normalizado:
                for slug, mapeamento in SLUG_PARA_STIX.items():
                    canonico = mapeamento.get("nome_canonico")
                    if (
                        isinstance(canonico, str)
                        and canonico.strip().casefold() == nome_normalizado
                    ):
                        return slug

        # 3) Silent fallback — nothing matched.
        return None

    # Synthetic group holding campaigns with no ``attributed-to`` relationship.
    ATOR_SEM_ASSOCIACAO_ID = "sem-ator"
    ATOR_SEM_ASSOCIACAO_NOME = "Sem ator associado"

    def listar_atores(self, versao: str) -> list[AtorStix]:
        """STIX ``intrusion-set`` objects grouped with their attributed campaigns.

        Reads the bundle via :meth:`ler_bundle` (whose typed
        :class:`StixFileError` is deliberately **not** caught here — it
        propagates so the endpoint maps it to the ``bundle_invalido`` 422
        contract, Req. 3.5).

        Builds the actor → campaigns grouping from the bundle (Req. 4.3,
        Correctness Property 6):

        * Every object with ``type == "intrusion-set"`` becomes an
          :class:`AtorStix` (``stix_id`` from ``id``, ``nome`` from ``name`` with
          a fallback to ``id``), kept in **bundle order** with an empty campaign
          list to start.
        * Every ``campaign`` object is mapped once via the shared
          :meth:`_mapear_campanha` helper (so the ``curada`` / ``slug_curado``
          flag is consistent with :meth:`listar_campanhas` — Correctness
          Property 7) into a lookup keyed by ``stix_id``.
        * In ATT&CK, attribution is a ``relationship`` with
          ``relationship_type == "attributed-to"`` linking a campaign
          (``source_ref``) to an intrusion-set (``target_ref``). Each such
          relationship attaches the campaign identified by ``source_ref`` under
          the actor identified by ``target_ref``. A campaign is attached under
          every actor it is attributed to, and only those — never dropped nor
          duplicated under the same actor.
        * Campaigns with no attribution relationship are collected under a single
          synthetic *"sem ator associado"* group (``stix_id`` =
          :data:`ATOR_SEM_ASSOCIACAO_ID`), appended last — and only when there is
          at least one unattributed campaign.

        Args:
            versao: The ATT&CK Enterprise version (e.g. ``"19.1"``).

        Returns:
            The list of :class:`AtorStix` (intrusion-sets in bundle order,
            optionally followed by the synthetic unattributed group).

        Raises:
            StixFileError: propagated from :meth:`ler_bundle` when the base is
                missing/unreadable, malformed, or not a valid STIX bundle.
        """
        bundle = self.ler_bundle(versao)
        objetos = bundle["objects"]

        # Actors in bundle order, each starting with an empty campaign list; plus
        # a quick lookup from intrusion-set id to its AtorStix.
        atores: list[AtorStix] = []
        atores_por_id: dict[str, AtorStix] = {}
        for objeto in objetos:
            if not isinstance(objeto, dict) or objeto.get("type") != "intrusion-set":
                continue
            stix_id = objeto.get("id")
            if not isinstance(stix_id, str):
                continue
            nome_bruto = objeto.get("name")
            nome = nome_bruto if isinstance(nome_bruto, str) and nome_bruto else stix_id
            ator = AtorStix(stix_id=stix_id, nome=nome, campanhas=[])
            atores.append(ator)
            # First declaration wins if an id somehow repeats.
            atores_por_id.setdefault(stix_id, ator)

        # Campaign lookup (keyed by stix_id) built with the SAME mapping used by
        # listar_campanhas, so curada/slug_curado stay consistent (Property 7).
        campanhas_por_id: dict[str, CampanhaStix] = {}
        for objeto in objetos:
            if not isinstance(objeto, dict) or objeto.get("type") != "campaign":
                continue
            campanha = self._mapear_campanha(objeto)
            if campanha is not None:
                campanhas_por_id.setdefault(campanha.stix_id, campanha)

        # Walk the attributed-to relationships: campaign (source_ref) → actor
        # (target_ref). Track which campaigns got attributed (to at least one
        # actor) and avoid duplicating a campaign under the same actor.
        campanhas_atribuidas: set[str] = set()
        anexadas_por_ator: dict[str, set[str]] = {}
        for objeto in objetos:
            if not isinstance(objeto, dict):
                continue
            if objeto.get("type") != "relationship":
                continue
            if objeto.get("relationship_type") != "attributed-to":
                continue

            source_ref = objeto.get("source_ref")
            target_ref = objeto.get("target_ref")
            if not isinstance(source_ref, str) or not isinstance(target_ref, str):
                continue

            campanha = campanhas_por_id.get(source_ref)
            ator = atores_por_id.get(target_ref)
            if campanha is None or ator is None:
                # Relationship referencing an unknown campaign or intrusion-set:
                # it cannot define a valid grouping, so ignore it. The campaign
                # (if it exists) stays unattributed and falls into the synthetic
                # group below.
                continue

            ja_anexadas = anexadas_por_ator.setdefault(target_ref, set())
            if source_ref not in ja_anexadas:
                ator.campanhas.append(campanha)
                ja_anexadas.add(source_ref)
            campanhas_atribuidas.add(source_ref)

        # Any campaign never attributed to a known actor goes under the synthetic
        # "sem ator associado" group, in bundle order. Only add the group when it
        # would hold at least one campaign.
        nao_atribuidas = [
            campanhas_por_id[cid]
            for cid in campanhas_por_id
            if cid not in campanhas_atribuidas
        ]
        if nao_atribuidas:
            atores.append(
                AtorStix(
                    stix_id=self.ATOR_SEM_ASSOCIACAO_ID,
                    nome=self.ATOR_SEM_ASSOCIACAO_NOME,
                    campanhas=nao_atribuidas,
                )
            )

        return atores
