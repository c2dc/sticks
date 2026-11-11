"""Stage 2 — Campaign selection: thin orchestration service (task 3.1).

This module implements the **thin** Stage 2 service of the six-stage pipeline
("Campaign selection"), fully reusing existing services: the
:class:`~app.services.stix.stix_service.StixService` (listing the bundle's
campaigns, marking ``curated``/``curated_slug`` via ``SLUG_TO_STIX``) and the
:class:`~app.services.stage.stage_service.StageService` (the historical DTO
:class:`~app.services.stage.stage_service.CampaignStixInput` assembled by
``StageService._build_campaign_stix`` for identity/aliases, and the curated
structural extraction of the DAG for the associated techniques). It does **not**
reimplement listing, bundle reading or extraction — it only *orchestrates* those
operations in the order defined by Stage 2 and derives the completion verdict
(Req. 6.1–6.7, 2.3).

Alignment with the design ("Backend — Stage 2: Campaign selection")
-------------------------------------------------------------------
The Stage 2 orchestration (thin, new service) is:

1. **List campaigns** — :meth:`Stage2Service.list_campaigns` delegates to
   ``StixService.list_campaigns``, which already marks each campaign with
   ``curated``/``curated_slug`` via ``SLUG_TO_STIX`` (Req. 6.2). For version 18
   (curated) the 8 curated campaigns are selectable (``selectable``); for
   non-curated campaigns, selection is unavailable at this first step, since the
   attribute extraction depends on curated artifacts on disk (Req. 6.5). The
   Input is the bundle selected in Stage 1 (Req. 6.1).
2. **Select / extract attributes** — :meth:`Stage2Service.select` extracts the
   attributes of the selected campaign (Req. 6.3) from the **database** for
   ANY campaign (curated or not): identity/aliases and the ATT&CK techniques
   are read via :meth:`StixPackageReadService.campaign_overview`. There is no
   disk read and no curated-artifact dependency — the curated/non-curated
   distinction is no longer a selection gate (it is just an attribute in the
   persisted STIX). The Output (``CampaignStixInput`` + techniques) feeds
   Stage 3 (Req. 6.4, 2.3).
3. **Complete** — :meth:`Stage2Service.select` completes the stage
   (``completed = True``) when the campaign is persisted and its overview was
   assembled from the database. A non-persisted version or a missing campaign
   keeps the stage **not completed** with the reported cause (Req. 6.7),
   without raising.

This service reads **only from the database** (no disk, no network) and no AI
is invoked on any path.

_Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 2.3_
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.models.enums import StageState
from app.services.stage.stage_service import (
    STAGE_CAMPAIGN_SELECTION,
    CampaignStixInput,
    StageService,
    TechniqueElement,
)
from app.services.stix.stix_package_read_service import (
    CampaignNotFoundError,
    StixPackageNotPersistedError,
    StixPackageReadService,
)
from app.services.stix.stix_service import (
    CampaignStix,
    StixService,
)


# ---------------------------------------------------------------------------
# Typed output DTOs
# ---------------------------------------------------------------------------


class ListedCampaign(CampaignStix):
    """A bundle campaign in the Stage 2 listing, with the selection flag.

    Extends :class:`~app.services.stix.stix_service.CampaignStix` (``stix_id``,
    ``name``, ``description``, ``curated``, ``curated_slug``) with the
    ``selectable`` field. The curated-campaign gate is removed (Req. 4.1, 4.2,
    4.5): EVERY listed campaign is selectable — curated or not — so a non-curated
    campaign's STIX/ATT&CK context can flow through the AI stages as the agent
    ``agent_input`` without requiring human-curated artifacts on disk.

    It also surfaces the per-campaign Container_Emulability marking (Req. 6.4,
    6.5): ``container_emulable`` is ``True`` when every technique of the campaign
    is container-emulable or dispensable (so the campaign list can show the
    container icon), ``False`` when at least one technique blocks it, and
    ``None`` when the marking has not been derived yet (the campaign is still
    listed either way — Req. 6.5). ``blocking_techniques`` carries the technique
    ids that block container emulation, the source for the accessible reason the
    UI shows on a non-emulable campaign (``None``/empty when emulable).
    """

    selectable: bool = True
    container_emulable: Optional[bool] = None
    blocking_techniques: list[str] = Field(default_factory=list)


class CampaignSelectionResult(BaseModel):
    """The typed result of selecting a campaign in Stage 2 (Req. 6.3–6.7).

    Carries the stage's completion verdict and its Output:

    * ``campaign_id`` — the curated slug of the selected campaign.
    * ``version`` — the STIX bundle version (the Stage 1/2 Input, Req. 6.1).
    * ``completed`` — ``True`` **only when** the attribute extraction succeeded
      (identity + techniques, Req. 6.6); ``False`` on any extraction failure,
      keeping the stage not completed (Req. 6.7).
    * ``identity`` — the campaign's identity/aliases block
      (:class:`CampaignStixInput`), always present (assembled defensively, never
      raises — Req. 6.3).
    * ``techniques`` — the associated techniques extracted from the curated DAG
      (Stage 2 Output, Req. 6.4); empty when the extraction failed.
    * ``error_message`` — the cause reported to the Researcher when the stage did
      not complete (``None`` on success, Req. 6.7).
    * ``failed_step`` — the structural-extraction step where the failure occurred
      (``None`` on success).

    The Output (identity + techniques) of a completed Stage 2 is the Stage 3
    Input (Req. 6.4, 2.3).
    """

    campaign_id: str
    version: Optional[str] = None
    completed: bool = False
    identity: Optional[CampaignStixInput] = None
    techniques: list[TechniqueElement] = Field(default_factory=list)
    error_message: Optional[str] = None
    failed_step: Optional[str] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class Stage2Service:
    """Orchestrates Stage 2 "Campaign selection" reusing existing services.

    A **thin** service: it does not reimplement campaign listing nor attribute
    extraction — it delegates listing to the
    :class:`~app.services.stix.stix_service.StixService` and extraction to the
    :class:`~app.services.stage.stage_service.StageService` (``_build_campaign_stix``
    for identity/aliases and ``run_stage2_selection`` for the techniques), only
    orchestrating the Stage 2 sequence and deriving the completion verdict
    (Req. 6.1–6.7, 2.3).

    The :class:`StixService` reused for listing is, by default, the same one the
    :class:`StageService` already carries (``stage_service._stix_service``), so
    listing and extraction see the same ``data_dir`` — essential in tests with an
    isolated temporary directory. An explicit ``stix_service`` can be injected
    when needed.
    """

    def __init__(
        self,
        stage_service: StageService,
        stix_service: Optional[StixService] = None,
    ) -> None:
        self._stage: StageService = stage_service
        # Reuse the StageService's StixService by default to share the same
        # data_dir (listing and extraction over the same on-disk bundle).
        self._stix: StixService = (
            stix_service
            if stix_service is not None
            else stage_service._stix_service  # noqa: SLF001 — intentional reuse
        )

    @property
    def stage_service(self) -> StageService:
        """The :class:`StageService` reused for the attribute extraction."""
        return self._stage

    @property
    def stix_service(self) -> StixService:
        """The :class:`StixService` reused to list the bundle's campaigns."""
        return self._stix

    # ---- list --------------------------------------------------------------

    def list_campaigns(self, version: str) -> list[ListedCampaign]:
        """List the campaigns of the ``version`` package, marking each selectable.

        Reads the campaigns **from the database** (the single source of truth)
        via :meth:`StixPackageReadService.list_campaigns`, which resolves
        ``curated``/``curated_slug`` from the persisted ``stix_campaigns`` rows.
        The on-disk bundle is deleted after a successful persistence
        (``stix-base-persisted-state-and-file-cleanup``), so this must never
        reopen the bundle from disk — doing so raised a spurious
        ``bundle_invalido`` (422) once the version was persisted and its file
        cleaned up.

        Every listed campaign is ``selectable`` (the curated-campaign gate is
        removed — Req. 4.1, 4.2, 4.5): any campaign, curated or not, can run
        through the AI stages, since ``select`` extracts identity/aliases/
        techniques defensively from the database with no curated-artifact
        dependency.

        A version with no persisted package yields an empty list (there is
        nothing to list yet), mirroring the defensive, never-raising contract of
        the DB-backed ``select``.
        """
        try:
            summaries = self._read_service().list_campaigns(version)
        except StixPackageNotPersistedError:
            return []
        return [
            ListedCampaign(
                stix_id=summary.stix_id,
                name=summary.name,
                description=None,
                curated=summary.curated,
                curated_slug=summary.curated_slug,
                selectable=True,
            )
            for summary in summaries
        ]

    # ---- db-backed extraction (all campaigns, curated or not) ----------

    def _read_service(self) -> StixPackageReadService:
        """A :class:`StixPackageReadService` over the StageService session."""
        return StixPackageReadService(self._stage._session)  # noqa: SLF001

    def _selection_from_db(
        self, campaign_key: str, version: Optional[str]
    ) -> CampaignSelectionResult:
        """Assemble the Stage 2 selection result from the persisted STIX (DB).

        Reads the campaign overview of ANY campaign (curated or not) from the
        database via :meth:`StixPackageReadService.campaign_overview` and maps
        it onto the Stage 2 result: identity/aliases from the campaign row and
        the techniques from ``stix_campaign_techniques``. No disk, no network,
        no curated-artifact dependency. Never raises: a non-persisted version
        or a missing campaign is reflected as ``completed=False`` with a clear
        cause, mirroring the defensive contract of the former disk path.
        """
        if not version:
            # Without a base version there is nothing to read from the DB.
            return CampaignSelectionResult(
                campaign_id=campaign_key,
                version=version,
                completed=False,
                identity=CampaignStixInput(
                    found=False, unavailable_reason="versao_ausente"
                ),
                techniques=[],
                error_message=None,
                failed_step=None,
            )
        try:
            overview = self._read_service().campaign_overview(version, campaign_key)
        except StixPackageNotPersistedError as exc:
            return CampaignSelectionResult(
                campaign_id=campaign_key,
                version=version,
                completed=False,
                identity=CampaignStixInput(
                    found=False,
                    base_version=version,
                    unavailable_reason="bundle_indisponivel",
                ),
                techniques=[],
                error_message=str(exc),
                failed_step="leitura_do_pacote",
            )
        except CampaignNotFoundError as exc:
            return CampaignSelectionResult(
                campaign_id=campaign_key,
                version=version,
                completed=False,
                identity=CampaignStixInput(
                    found=False,
                    base_version=version,
                    unavailable_reason="campanha_nao_encontrada",
                ),
                techniques=[],
                error_message=str(exc),
                failed_step="selecao_da_campanha",
            )

        identity = CampaignStixInput(
            found=True,
            base_version=version,
            stix_id=overview.stix_id,
            name=overview.name,
            external_id=overview.external_id,
            description=overview.description,
            aliases=list(overview.aliases),
        )
        techniques = [
            TechniqueElement(
                technique_id=t.technique_id,
                technique_name=t.technique_name,
                tactic=t.tactic,
                description=t.description,
            )
            for t in overview.techniques
        ]
        return CampaignSelectionResult(
            campaign_id=campaign_key,
            version=version,
            completed=True,
            identity=identity,
            techniques=techniques,
            error_message=None,
            failed_step=None,
        )

    # ---- select ------------------------------------------------------------

    def select(
        self, campaign_key: str, version: Optional[str]
    ) -> CampaignSelectionResult:
        """Select ``campaign_key`` and extract its attributes from the DB (Req. 6.3-6.7).

        ``campaign_key`` is the stable key the listing exposes: the curated
        slug when the campaign is curated, otherwise the STIX id. Works for
        EVERY campaign of the persisted package (curated or not): identity,
        aliases and techniques are read from the database
        (:meth:`StixPackageReadService.campaign_overview`), never from disk and
        with no curated-artifact dependency. Never raises; a non-persisted
        version or a missing campaign is reflected as ``completed=False`` with
        the reported cause.
        """
        return self._selection_from_db(campaign_key, version)

    # ---- read (read-only view) ---------------------------------------------

    def read(
        self, campaign_key: str, version: Optional[str]
    ) -> CampaignSelectionResult:
        """Read-only view of Stage 2 — NEVER runs nor persists.

        Reflects the persisted selection without completing it. The identity
        and techniques are read from the database (same source as
        :meth:`select`, via :meth:`StixPackageReadService.campaign_overview`),
        but the ``completed`` verdict is taken from the Stage 2 ``StageRun``
        state of THIS session, so opening the screen never fabricates a
        selection: until a real selection completed, ``techniques`` is empty
        and ``completed=False``.
        """
        stage2_state = self._stage.get_stage_state(
            campaign_key, STAGE_CAMPAIGN_SELECTION
        ).state
        completed = stage2_state == StageState.COMPLETED

        # Identity is always derivable from the DB (informative, no write).
        db_result = self._selection_from_db(campaign_key, version)
        identity = db_result.identity

        # Only reflect techniques once Stage 2 actually completed in this
        # session; otherwise the panel stays empty (no auto-selection).
        techniques = db_result.techniques if completed else []

        return CampaignSelectionResult(
            campaign_id=campaign_key,
            version=version,
            completed=completed,
            identity=identity,
            techniques=techniques,
            # A read-only view never surfaces an extraction error: it only
            # reflects whether a selection was completed in this session.
            error_message=None,
            failed_step=None,
        )
