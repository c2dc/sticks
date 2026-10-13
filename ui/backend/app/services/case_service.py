"""CaseService — read and parse the 8 curated cases (task 3.1).

This service reads the *real* curated files that live under the sticks data
directory and turns them into typed, in-memory results:

* ``data/api/{slug}_dag-ability.json`` — a JSON **list** of ability objects
  (mapped to :class:`AbilityData`, aligned with the ``Ability`` ORM model).
* ``data/api/{slug}_dag-adversary.json`` — a JSON **object** describing the
  adversary and its ``atomic_ordering`` (mapped to :class:`AdversaryData`,
  aligned with the ``Adversary`` ORM model).
* ``data/dag/{slug}_dag.json`` — the Stage-1/2 structural graph input (mapped to
  :class:`DagData`, exposing ``campaign_name``, ``structural_nodes``,
  ``parent``/``child`` node relations and the ``ai_prompt_template``).

Design alignment
----------------
* The 8 curated cases are registered with the slugs and display names from the
  design / requirements (Req. 5.1). Each case carries its resolved file paths
  and an explicit, extensible ``origem_traducao`` (Req. 11.1) — always
  ``HUMAN_CURATION`` for the existing curated translations.
* Field names on the typed results mirror the ORM models in
  :mod:`app.models.domain` (``ability_id``, ``name``, ``tactic``,
  ``technique_name``, ``technique_id``, ``description``, ``executors`` for
  abilities; ``id``, ``name``, ``description``, ``atomic_ordering`` for the
  adversary), so persisting them later is a direct 1:1 mapping.
* Path resolution reuses the repo-root strategy from
  :mod:`app.core.sticks_config` (``_repo_root``): paths are resolved relative to
  the located repository root, never hard-coded. This keeps the integration
  portable on Windows (all paths built with :mod:`pathlib`).

Per-case error handling (task 3.2)
----------------------------------
Reading a case whose ability/adversary/DAG file is *missing* or contains
*invalid JSON* must not abort the other cases. The read seam
(:meth:`CaseService._read_json`) raises a typed :class:`CaseFileError` that
distinguishes a missing file (:attr:`CaseLoadErrorType.MISSING_FILE`) from
malformed JSON (:attr:`CaseLoadErrorType.MALFORMED_JSON`) and from a structural
parse failure (:attr:`CaseLoadErrorType.INVALID_STRUCTURE`). Callers get a
per-case, descriptive error via:

* :meth:`CaseService.try_load_case` — never raises for file/parse problems;
  returns a :class:`CaseLoadResult` carrying *either* the parsed
  :class:`CaseData` *or* a :class:`CaseLoadError` that identifies the affected
  case, the failing file and the error kind (Req. 5.6).
* :meth:`CaseService.load_all_cases_safe` — returns a
  :class:`CaseCatalog` with the successfully-loaded cases **and** a
  ``{slug: CaseLoadError}`` map, so one bad case never breaks the rest.

The happy-path API (:meth:`CaseService.load_case` /
:meth:`CaseService.load_all_cases`) is preserved unchanged for task 3.1 callers:
it still raises on the first problem.

"No adversary" / "no abilities" (Req. 3.7, 3.8) are represented *cleanly* rather
than as errors: an absent/empty adversary file yields ``adversary is None`` and
an empty ability list yields ``abilities == []`` — the service never crashes on
these, so Stage-2 display can show the "no adversary"/"no abilities" states.

This module only ever **reads** from ``sticks/`` — it never modifies anything
there.

_Requisitos: 5.1, 5.6, 3.2, 3.3, 3.5, 3.7, 3.8, 11.1_
"""

from __future__ import annotations

import enum
import json
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

from app.core.sticks_config import _repo_root
from app.models.enums import TranslationSource


# ---------------------------------------------------------------------------
# Curated case registry (Req. 5.1)
# ---------------------------------------------------------------------------

# slug -> display name for the 8 curated cases. The slug matches the real file
# stems in data/api/ and data/dag/ (e.g. "shadowray" ->
# "shadowray_dag-ability.json"). Order follows the requirement listing.
CURATED_CASES: dict[str, str] = {
    "apt41_dust": "APT41-DUST",
    "c0010": "ATT&CK Campaign C0010",
    "c0026": "ATT&CK Campaign C0026",
    "costaricto": "CostaRicto",
    "operation_midnighteclipse": "Operation MidnightEclipse",
    "outer_space": "Operation Outer Space",
    "salesforce_data_exfiltration": "Salesforce Data Exfiltration",
    "shadowray": "ShadowRay",
}


# ---------------------------------------------------------------------------
# Typed results (field names aligned with the ORM models in domain.py)
# ---------------------------------------------------------------------------


class Executor(BaseModel):
    """A single executor of an Ability: a concrete command on a platform.

    Mirrors the objects inside ``Ability.executors`` (JSON list of
    ``{name, platform, command}``).
    """

    name: Optional[str] = None
    platform: Optional[str] = None
    command: Optional[str] = None


class AbilityData(BaseModel):
    """One curated Ability, aligned with the ``Ability`` ORM model.

    Field names match ``app.models.domain.Ability`` so this maps 1:1 when
    persisted later. ``executors`` is kept as a list of typed :class:`Executor`
    here for clarity; it corresponds to the ORM ``executors`` JSON column.
    """

    ability_id: str
    name: Optional[str] = None
    tactic: Optional[str] = None
    technique_name: Optional[str] = None
    technique_id: Optional[str] = None
    description: Optional[str] = None
    executors: list[Executor] = Field(default_factory=list)


class AdversaryData(BaseModel):
    """The curated Adversary, aligned with the ``Adversary`` ORM model.

    ``atomic_ordering`` is the ordered list of ability ids that composes the
    adversary/campaign (Req. 3.3, 3.5).
    """

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    atomic_ordering: list[str] = Field(default_factory=list)


class DagNode(BaseModel):
    """A structural node of the Stage-1/2 DAG input.

    Only the fields needed as Stage-1/2 input are typed explicitly; any extra
    keys present in the real files are ignored. Beyond the technique identity and
    the parent/child relationships, a few extra structural fields are preserved
    because the Stage-1 structural modeling (task 8.1) derives indicators,
    infrastructure and malware signals from them:

    * ``provides`` — the capabilities a node contributes (e.g. ``"infrastructure"``,
      ``"credentials"``, ``"c2_channel"``); used to flag infrastructure/malware.
    * ``attacker_commands`` — the concrete attacker commands, mined for network
      indicators (destination IPs/hosts).
    * ``campaign_context`` — the per-node campaign narrative, surfaced as metadata.
    """

    node_id: Optional[str] = None
    node_index: Optional[int] = None
    node_type: Optional[str] = None
    technique_id: Optional[str] = None
    technique_name: Optional[str] = None
    tactic: Optional[str] = None
    description: Optional[str] = None
    parent_nodes: list[str] = Field(default_factory=list)
    child_nodes: list[str] = Field(default_factory=list)
    ai_prompt_template: Optional[str] = None
    # Extra structural signals preserved for Stage-1 derivation (task 8.1).
    provides: list[str] = Field(default_factory=list)
    attacker_commands: list[str] = Field(default_factory=list)
    campaign_context: Optional[str] = None


class DagData(BaseModel):
    """The Stage-1/2 graph input parsed from ``data/dag/{slug}_dag.json``.

    Exposes the campaign name, the structural nodes (with their parent/child
    relations and per-node ``ai_prompt_template``) plus the raw metadata block.
    ``campaign_file``, ``generated_at`` and ``capability_flow`` are preserved as
    campaign metadata surfaced by the Stage-1 structural modeling (task 8.1).
    """

    campaign_name: Optional[str] = None
    campaign_file: Optional[str] = None
    generated_at: Optional[str] = None
    structural_nodes: list[DagNode] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    capability_flow: dict[str, Any] = Field(default_factory=dict)


class CaseData(BaseModel):
    """Full parsed result for one curated case.

    Bundles the case metadata (slug, display name, resolved file paths, and the
    explicit/extensible translation source), the curated Abilities, the
    Adversary with its ``atomic_ordering``, and the Stage-1/2 DAG input.
    """

    # Case metadata (aligned with the ``Case`` ORM model fields).
    id: str  # slug, e.g. "shadowray"
    nome: str  # display name, e.g. "ShadowRay"
    arquivo_ability: str
    arquivo_adversary: str
    arquivo_dag: str
    origem_traducao: TranslationSource = TranslationSource.HUMAN_CURATION

    # Parsed contents.
    abilities: list[AbilityData] = Field(default_factory=list)
    adversary: Optional[AdversaryData] = None
    dag: Optional[DagData] = None

    @property
    def atomic_ordering(self) -> list[str]:
        """Convenience accessor for the adversary's ordering (Req. 3.3, 3.5)."""
        return self.adversary.atomic_ordering if self.adversary else []

    @property
    def has_adversary(self) -> bool:
        """Whether this case has an Adversary grouping (Req. 3.8).

        ``False`` means "no adversary": Stage 2 should indicate the absence of an
        Ability grouping rather than treat it as an error.
        """
        return self.adversary is not None

    @property
    def has_abilities(self) -> bool:
        """Whether this case has any curated Abilities (Req. 3.7).

        ``False`` means "no abilities": Stage 2 should indicate the absence of an
        Output rather than treat it as an error.
        """
        return len(self.abilities) > 0


# ---------------------------------------------------------------------------
# Per-case error handling (task 3.2, Req. 5.6)
# ---------------------------------------------------------------------------


class CaseLoadErrorType(str, enum.Enum):
    """Kind of failure while loading a single curated case (Req. 5.6).

    The distinct values let the UI (and tests) tell a *missing file* apart from
    *malformed JSON* apart from a *structurally invalid* (but syntactically
    valid) JSON payload.
    """

    MISSING_FILE = "arquivo_ausente"
    MALFORMED_JSON = "json_malformado"
    INVALID_STRUCTURE = "estrutura_invalida"


class CaseFileError(Exception):
    """Raised by the read/parse seam for a single case file problem.

    Carries which file failed and the failure kind so :meth:`try_load_case` can
    build a descriptive, per-case :class:`CaseLoadError`. This is an internal
    control-flow exception; the public safe API converts it into a
    :class:`CaseLoadError` value instead of propagating it.
    """

    def __init__(self, error_type: CaseLoadErrorType, path: Path, detail: str) -> None:
        self.error_type = error_type
        self.path = Path(path)
        self.detail = detail
        super().__init__(detail)


class CaseLoadError(BaseModel):
    """A descriptive, per-case load failure (Req. 5.6).

    Identifies the affected case (``slug``/``nome``), the failing file and the
    error kind, plus a human-readable ``detail``. Returned as a value (never
    raised) by the safe API so one bad case does not abort the rest.
    """

    slug: str
    nome: str
    error_type: CaseLoadErrorType
    arquivo: Optional[str] = None
    detail: str

    @property
    def is_missing_file(self) -> bool:
        """``True`` when the failure is a missing file (vs malformed JSON)."""
        return self.error_type is CaseLoadErrorType.MISSING_FILE

    @property
    def is_malformed_json(self) -> bool:
        """``True`` when the failure is invalid JSON (vs a missing file)."""
        return self.error_type is CaseLoadErrorType.MALFORMED_JSON


class CaseLoadResult(BaseModel):
    """Outcome of loading a single case: either a parsed case or an error.

    Exactly one of :attr:`case` / :attr:`error` is set. :attr:`ok` is a
    convenience flag for callers.
    """

    slug: str
    case: Optional[CaseData] = None
    error: Optional[CaseLoadError] = None

    @property
    def ok(self) -> bool:
        """``True`` when the case loaded successfully."""
        return self.case is not None and self.error is None


class CaseCatalog(BaseModel):
    """Result of loading *all* cases with per-case error isolation (Req. 5.6).

    Carries the successfully-loaded ``cases`` (keyed by slug) **and** the
    per-case ``errors`` (keyed by slug). A single malformed/missing case appears
    in ``errors`` while every other case remains available in ``cases``.
    """

    cases: dict[str, CaseData] = Field(default_factory=dict)
    errors: dict[str, CaseLoadError] = Field(default_factory=dict)

    @property
    def loaded_slugs(self) -> list[str]:
        """Slugs of the cases that loaded successfully."""
        return list(self.cases.keys())

    @property
    def failed_slugs(self) -> list[str]:
        """Slugs of the cases that failed to load."""
        return list(self.errors.keys())

    @property
    def all_ok(self) -> bool:
        """``True`` when every case loaded without error."""
        return not self.errors


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CaseService:
    """Reads and parses the 8 curated ability/adversary/DAG file sets.

    Path resolution is consistent with :mod:`app.core.sticks_config`: the sticks
    data directory is located relative to the repository root, never hard-coded.
    A custom ``data_dir`` may be injected (e.g. in tests / fixtures) to point at
    an alternative data root.
    """

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        # Default to <repo>/sticks/data, resolved via the same repo-root
        # strategy used by sticks_config. All paths are pathlib for portability.
        self._data_dir: Path = (
            Path(data_dir) if data_dir is not None else _repo_root() / "sticks" / "data"
        )

    # ---- path resolution (relative to the sticks data dir) -----------------

    @property
    def data_dir(self) -> Path:
        """The resolved sticks data directory (``<repo>/sticks/data`` default)."""
        return self._data_dir

    def ability_path(self, slug: str) -> Path:
        """Path to ``data/api/{slug}_dag-ability.json``."""
        return self._data_dir / "api" / f"{slug}_dag-ability.json"

    def adversary_path(self, slug: str) -> Path:
        """Path to ``data/api/{slug}_dag-adversary.json``."""
        return self._data_dir / "api" / f"{slug}_dag-adversary.json"

    def dag_path(self, slug: str) -> Path:
        """Path to ``data/dag/{slug}_dag.json``."""
        return self._data_dir / "dag" / f"{slug}_dag.json"

    # ---- registry ----------------------------------------------------------

    def list_cases(self) -> dict[str, str]:
        """Return the 8 curated cases as ``{slug: display_name}`` (Req. 5.1)."""
        return dict(CURATED_CASES)

    def case_slugs(self) -> list[str]:
        """Return the 8 curated case slugs in registration order (Req. 5.1)."""
        return list(CURATED_CASES.keys())

    # ---- parsing seams (per-case error handling lives here, task 3.2) ------

    @staticmethod
    def _read_json(path: Path) -> Any:
        """Read and parse a JSON file at ``path``.

        This is the single read seam for a case file. It raises a typed
        :class:`CaseFileError` that distinguishes a **missing file** from
        **malformed JSON**, so :meth:`try_load_case` can surface a descriptive
        per-case error (Req. 5.6). Reads with an explicit UTF-8 encoding for
        portability (Windows dev environment).

        Raises:
            CaseFileError: with :attr:`CaseLoadErrorType.MISSING_FILE` when the
                file does not exist / cannot be opened, or
                :attr:`CaseLoadErrorType.MALFORMED_JSON` when the contents are
                not valid JSON.
        """
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError as exc:
            raise CaseFileError(
                CaseLoadErrorType.MISSING_FILE,
                path,
                f"Arquivo ausente: {path}",
            ) from exc
        except IsADirectoryError as exc:
            raise CaseFileError(
                CaseLoadErrorType.MISSING_FILE,
                path,
                f"Caminho esperado como arquivo é um diretório: {path}",
            ) from exc
        except json.JSONDecodeError as exc:
            raise CaseFileError(
                CaseLoadErrorType.MALFORMED_JSON,
                path,
                f"JSON malformado em {path}: {exc}",
            ) from exc
        except OSError as exc:
            # Any other read error (permissions, etc.) — treat as unreadable /
            # missing so a single bad case never aborts the rest (Req. 5.6).
            raise CaseFileError(
                CaseLoadErrorType.MISSING_FILE,
                path,
                f"Arquivo ilegível em {path}: {exc}",
            ) from exc

    def _parse_abilities(self, raw: Any, path: Path) -> list[AbilityData]:
        """Parse the ability list JSON into typed :class:`AbilityData`.

        Represents "no abilities" cleanly (Req. 3.7): a JSON ``null`` or an empty
        list yields ``[]`` (never an error). A non-list payload or an item that
        fails validation is an :attr:`CaseLoadErrorType.INVALID_STRUCTURE`
        failure that identifies the file.
        """
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise CaseFileError(
                CaseLoadErrorType.INVALID_STRUCTURE,
                path,
                f"Esperava uma lista de abilities em {path}, obteve {type(raw).__name__}.",
            )
        try:
            return [AbilityData.model_validate(item) for item in raw]
        except ValidationError as exc:
            raise CaseFileError(
                CaseLoadErrorType.INVALID_STRUCTURE,
                path,
                f"Ability inválida em {path}: {exc}",
            ) from exc

    def _parse_adversary(self, raw: Any, path: Path) -> Optional[AdversaryData]:
        """Parse the adversary object JSON into a typed :class:`AdversaryData`.

        Represents "no adversary" cleanly (Req. 3.8): a JSON ``null`` or an empty
        object yields ``None`` (never an error), so Stage 2 can indicate the
        absence of an Adversary grouping. A non-object payload or one that fails
        validation is an :attr:`CaseLoadErrorType.INVALID_STRUCTURE` failure.
        """
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise CaseFileError(
                CaseLoadErrorType.INVALID_STRUCTURE,
                path,
                f"Esperava um objeto de adversary em {path}, obteve {type(raw).__name__}.",
            )
        if not raw:
            # Empty object => no adversary associated with this case.
            return None
        try:
            return AdversaryData.model_validate(raw)
        except ValidationError as exc:
            raise CaseFileError(
                CaseLoadErrorType.INVALID_STRUCTURE,
                path,
                f"Adversary inválido em {path}: {exc}",
            ) from exc

    def _parse_dag(self, raw: Any, path: Path) -> Optional[DagData]:
        """Parse the DAG graph JSON into a typed :class:`DagData`.

        A JSON ``null`` yields ``None``. A non-object payload or one that fails
        validation is an :attr:`CaseLoadErrorType.INVALID_STRUCTURE` failure.
        """
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise CaseFileError(
                CaseLoadErrorType.INVALID_STRUCTURE,
                path,
                f"Esperava um objeto de DAG em {path}, obteve {type(raw).__name__}.",
            )
        try:
            return DagData.model_validate(raw)
        except ValidationError as exc:
            raise CaseFileError(
                CaseLoadErrorType.INVALID_STRUCTURE,
                path,
                f"DAG inválido em {path}: {exc}",
            ) from exc

    # ---- public API --------------------------------------------------------

    def load_case(self, slug: str) -> CaseData:
        """Read and parse a single curated case by slug (happy-path API).

        Reads the ability list, the adversary object and the DAG graph, and
        assembles a :class:`CaseData` with the case metadata (display name,
        resolved paths, translation source), the Abilities, the Adversary and
        its ``atomic_ordering``, and the Stage-1/2 DAG input.

        "No adversary"/"no abilities" are represented cleanly (``adversary is
        None`` / ``abilities == []``, Req. 3.7, 3.8) rather than as errors: an
        absent adversary file, or an empty/``null`` adversary payload, yields
        ``adversary is None``; an empty/``null`` ability list yields ``[]``.

        Args:
            slug: One of the registered curated case slugs.

        Returns:
            The fully parsed :class:`CaseData`.

        Raises:
            KeyError: if ``slug`` is not a registered curated case.
            CaseFileError: if the ability or DAG file is missing, or any file
                contains malformed JSON / an invalid structure. This preserves
                the task-3.1 fail-fast behaviour; callers that need per-case
                isolation should use :meth:`try_load_case` /
                :meth:`load_all_cases_safe` instead (Req. 5.6).
        """
        if slug not in CURATED_CASES:
            raise KeyError(f"Unknown curated case slug: {slug!r}")

        ability_path = self.ability_path(slug)
        adversary_path = self.adversary_path(slug)
        dag_path = self.dag_path(slug)

        abilities = self._parse_abilities(self._read_json(ability_path), ability_path)
        adversary = self._read_adversary(adversary_path)
        dag = self._parse_dag(self._read_json(dag_path), dag_path)

        return CaseData(
            id=slug,
            nome=CURATED_CASES[slug],
            arquivo_ability=str(ability_path),
            arquivo_adversary=str(adversary_path),
            arquivo_dag=str(dag_path),
            origem_traducao=TranslationSource.HUMAN_CURATION,
            abilities=abilities,
            adversary=adversary,
            dag=dag,
        )

    def _read_adversary(self, adversary_path: Path) -> Optional[AdversaryData]:
        """Read + parse the adversary file, treating a missing file as "none".

        A missing adversary file means the case simply has no Adversary grouping
        (Req. 3.8), so it yields ``None`` instead of raising. Malformed JSON or
        an invalid structure is still surfaced as a :class:`CaseFileError`.
        """
        try:
            raw = self._read_json(adversary_path)
        except CaseFileError as exc:
            if exc.error_type is CaseLoadErrorType.MISSING_FILE:
                return None
            raise
        return self._parse_adversary(raw, adversary_path)

    def try_load_case(self, slug: str) -> CaseLoadResult:
        """Load a single case, capturing file/parse problems as a value.

        Unlike :meth:`load_case`, this never raises for a missing/malformed file
        (Req. 5.6): it returns a :class:`CaseLoadResult` whose :attr:`error`
        carries a descriptive, per-case :class:`CaseLoadError` (which case, which
        file, and whether it was a missing file, malformed JSON or an invalid
        structure). Unknown-slug (a programming error) still raises ``KeyError``.
        """
        if slug not in CURATED_CASES:
            raise KeyError(f"Unknown curated case slug: {slug!r}")
        try:
            case = self.load_case(slug)
        except CaseFileError as exc:
            return CaseLoadResult(
                slug=slug,
                error=CaseLoadError(
                    slug=slug,
                    nome=CURATED_CASES[slug],
                    error_type=exc.error_type,
                    arquivo=str(exc.path),
                    detail=exc.detail,
                ),
            )
        return CaseLoadResult(slug=slug, case=case)

    def load_all_cases(self) -> dict[str, CaseData]:
        """Read and parse all 8 curated cases, keyed by slug (happy-path API).

        Fail-fast: raises on the first missing/malformed file. Preserved for
        task-3.1 callers. For per-case error isolation use
        :meth:`load_all_cases_safe` (Req. 5.6).
        """
        return {slug: self.load_case(slug) for slug in CURATED_CASES}

    def load_all_cases_safe(self) -> CaseCatalog:
        """Read and parse all 8 cases, isolating per-case failures (Req. 5.6).

        Returns a :class:`CaseCatalog` carrying the successfully-loaded cases
        **and** a ``{slug: CaseLoadError}`` map. One missing/malformed case is
        recorded in ``errors`` and every other case remains available in
        ``cases`` — a single bad case never raises or aborts the rest.
        """
        catalog = CaseCatalog()
        for slug in CURATED_CASES:
            result = self.try_load_case(slug)
            if result.ok and result.case is not None:
                catalog.cases[slug] = result.case
            elif result.error is not None:
                catalog.errors[slug] = result.error
        return catalog
