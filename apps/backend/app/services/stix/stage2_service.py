"""Stage 2 — Campaign selection: thin orchestration service (task 3.1).

This module implements the **thin** Stage 2 service of the six-stage pipeline
("Campaign selection"), fully reusing existing services: the
:class:`~app.services.stix.stix_service.StixService` (listing the bundle's
campaigns, marking ``curated``/``curated_slug`` via ``SLUG_PARA_STIX``) and the
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
   ``curated``/``curated_slug`` via ``SLUG_PARA_STIX`` (Req. 6.2). For version 18
   (curated) the 8 curated campaigns are selectable (``selectable``); for
   non-curated campaigns, selection is unavailable at this first step, since the
   attribute extraction depends on curated artifacts on disk (Req. 6.5). The
   Input is the bundle selected in Stage 1 (Req. 6.1).
2. **Select / extract attributes** — :meth:`Stage2Service.select` extracts the
   attributes of the selected campaign (Req. 6.3): the **identity** and the
   **aliases** come from ``StageService._build_campaign_stix(slug, version)``
   (defensive, no network on curated paths — the bundle read is local), and the
   **techniques** come from the curated structural extraction of the DAG via
   ``StageService.run_stage1(slug, version)`` (reuse of the historical ``Stage1*``
   DTO which, after renumbering, serves Stage 2). The Output is the extracted
   attributes (``CampaignStixInput`` + techniques), which feed Stage 3 (Req. 6.4,
   2.3).
3. **Complete** — :meth:`Stage2Service.select` completes the stage
   (``completed = True``) **only when** the extraction succeeded (Req. 6.6). An
   extraction failure (nonexistent slug, absent/unreadable DAG) keeps the stage
   **not completed** with the reported cause (Req. 6.7), without raising.

This service only **reads** disk on the curated paths; no network is touched when
listing/selecting a curated campaign and no AI is invoked on any path (the
curated artifacts are read locally).

_Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 2.3_
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.models.enums import StageState
from app.services.stage.stage_service import (
    CampaignStixInput,
    StageService,
    TechniqueElement,
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
    ``selectable`` field: it is only ``True`` when the campaign is curated
    (``curated`` with ``curated_slug`` filled), since the attribute extraction
    depends on curated artifacts on disk. For version 18 (curated) the 8 curated
    campaigns are selectable (Req. 6.2, 6.5).
    """

    selectable: bool = False


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
    for identity/aliases and ``run_stage1`` for the techniques), only
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
        """List the campaigns of the ``version`` bundle, marking the selectable one.

        Delegates to ``StixService.list_campaigns`` (which already resolves
        ``curated``/``curated_slug`` via ``SLUG_PARA_STIX``, preserving the bundle
        order) and projects ``selectable`` into each entry: it is only ``True``
        for curated campaigns (with ``curated_slug``), since the attribute
        extraction depends on curated artifacts on disk. For version 18 (curated)
        the 8 curated campaigns are selectable (Req. 6.2, 6.5).

        The typed :class:`~app.services.stix.stix_service.StixFileError` is
        **not** caught here — it propagates for the endpoint to map it to the
        invalid-bundle contract (Stage 1 precedent).

        Raises:
            StixFileError: when the bundle is absent/malformed/invalid.
        """
        return [
            ListedCampaign(
                stix_id=campaign.stix_id,
                name=campaign.name,
                description=campaign.description,
                curated=campaign.curated,
                curated_slug=campaign.curated_slug,
                selectable=campaign.curated and campaign.curated_slug is not None,
            )
            for campaign in self._stix.list_campaigns(version)
        ]

    # ---- select ------------------------------------------------------------

    def select(self, slug: str, version: Optional[str]) -> CampaignSelectionResult:
        """Select the ``slug`` campaign and extract its attributes (Req. 6.3–6.7).

        Flow:

        1. **Identity/aliases** — assembles the :class:`CampaignStixInput` block
           via ``StageService._build_campaign_stix(slug, version)`` (defensive,
           no network on curated paths; never raises). For a curated campaign of
           the curated version it loads the rich fields of the ``campaign`` object
           (identity, external_id, description, aliases, dates).
        2. **Techniques** — extracts the associated techniques from the curated
           DAG via ``StageService.run_stage1(slug, version)``. ``run_stage1``
           reuses the historical ``Stage1*`` DTO (which, after renumbering, serves
           Stage 2) and does **not** raise for a campaign with an absent/unreadable
           DAG — it reports the failure in the result (``state = error``,
           ``failed_step``).
        3. **Verdict** — completes the stage (``completed = True``) **only when**
           the extraction succeeded (``state`` completed). An extraction failure
           keeps the stage not completed with the reported cause (nonexistent
           slug, absent/unreadable DAG) — Req. 6.6, 6.7.

        Never raises for an invalid campaign: the failure is reflected in
        ``completed=False`` with ``error_message``/``failed_step`` filled. A
        genuinely unknown slug (programming error) still propagates the
        ``KeyError`` from ``CaseService`` through ``run_stage1``, consistent with
        the Stage 1 precedent.

        Args:
            slug: The curated slug of the selected campaign (e.g. ``"shadowray"``).
            version: The STIX bundle version selected in Stage 1 (e.g. ``"18.0"``).
                Optional; when omitted, the identity falls back to
                ``versao_ausente`` without error, but the technique extraction
                (from the curated DAG) stays possible.

        Returns:
            A :class:`CampaignSelectionResult` with the identity, the techniques
            and the completion verdict.
        """
        # 1) Identity/aliases — defensive, never raises (Property 1).
        identity = self._stage._build_campaign_stix(slug, version)  # noqa: SLF001

        # 2) Techniques — structural extraction of the curated DAG (reuse Stage1*).
        #    Does not raise for a campaign with an absent/unreadable DAG: reports
        #    in the result.
        result = self._stage.run_stage1(slug, version=version)

        completed = result.state == StageState.COMPLETED
        techniques = (
            list(result.output.techniques)
            if completed and result.output is not None
            else []
        )

        return CampaignSelectionResult(
            campaign_id=slug,
            version=version,
            completed=completed,
            identity=identity,
            techniques=techniques,
            error_message=None if completed else result.error_message,
            failed_step=None if completed else result.failed_step,
        )

    # ---- read (read-only view) ---------------------------------------------

    def read(self, slug: str, version: Optional[str]) -> CampaignSelectionResult:
        """Read-only view of Stage 2 — NEVER runs nor persists.

        Reflects the stage's persisted state without running the extraction: it
        assembles the identity (always derivable, no write) and, when the stage
        has already completed, derives the techniques from the curated DAG purely
        via ``StageService.read_stage1`` (the same extractors as ``run_stage1``,
        without persisting). While the stage has not completed, ``techniques`` is
        empty and ``completed=False`` — opening the screen never fabricates a
        completed state.

        Mirrors the Stage 1 precedent (``read_stage1`` vs ``run_stage1``): the
        ``GET .../stage/2`` uses this view; the effective selection uses
        :meth:`select`.
        """
        identity = self._stage._build_campaign_stix(slug, version)  # noqa: SLF001
        result = self._stage.read_stage1(slug, version=version)

        completed = result.state == StageState.COMPLETED
        techniques = (
            list(result.output.techniques)
            if completed and result.output is not None
            else []
        )

        return CampaignSelectionResult(
            campaign_id=slug,
            version=version,
            completed=completed,
            identity=identity,
            techniques=techniques,
            error_message=None if completed else result.error_message,
            failed_step=None if completed else result.failed_step,
        )
