"""Stage 1 — STIX base selection: thin orchestration service (task 2.2).

This module implements the **thin** Stage 1 service of the six-stage pipeline
("STIX base selection"), fully reusing the existing
:class:`~app.services.stix.stix_service.StixService`. It does **not**
reimplement listing, download, bundle reading or dashboard: it only
*orchestrates* those operations in the order defined by Stage 1 and derives the
stage's completion verdict (Req. 5.1, 5.2, 5.3, 5.4, 5.7, 5.8, 5.9, 5.10, 2.2).

Alignment with the design ("Backend — Stage 1: STIX base selection")
--------------------------------------------------------------------
The Stage 1 orchestration (thin, new service, reusing ``StixService``) is:

1. **Choose version / list** — :meth:`Stage1Service.list_bases` delegates to
   ``StixService.list_bases``; ``downloaded``/``selectable`` are checked against
   disk on each call and **no version comes downloaded by default** (Req. 5.4).
2. **Download when absent** — :meth:`Stage1Service.download_base` downloads the
   bundle **only** when it is not on disk (reuses the on-disk copy when present,
   Req. 5.3), delegating to ``StixService.download_base`` — the system's **only
   network egress**, which validates ``version`` against ``KNOWN_VERSIONS``
   before touching disk/network, writes atomically and raises
   ``StixDownloadError`` without leaving a partial file on failure (Req. 5.2,
   5.9).
3. **Dashboard** — :meth:`Stage1Service.dashboard` delegates to
   ``StixService.dashboard`` (per-type counts) and the bundle's campaigns
   (Req. 5.7).
4. **Complete** — :meth:`Stage1Service.complete` assembles the stage Output
   (bundle on disk + dashboard + campaigns, Req. 5.8) and returns a
   :class:`Stage1ResultStix` with ``completed = True`` **only when** the bundle
   is on disk and the dashboard was assembled (Req. 5.10). A download/read
   failure keeps the stage **not completed** with the reported cause (Req. 5.9),
   without raising.

The curated-version flag (``CURATED_VERSION`` / ``version_is_curated``, task 2.1)
is projected into each version of the Output so the UI knows which base carries
the 8 curated campaigns (Req. 5.5, 5.6) — with no request-time lookup.

This service only **reads** disk; the only possible network egress is delegated
to ``StixService.download_base`` and only happens on explicit request when the
base is not yet on disk. No AI is invoked on any path.

_Requirements: 5.1, 5.2, 5.3, 5.4, 5.7, 5.8, 5.9, 5.10, 2.2_
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from app.services.stix.stix_service import (
    BaseStixInfo,
    CampaignStix,
    DashboardStix,
    StixDownloadError,
    StixFileError,
    StixService,
    version_is_curated,
)


# ---------------------------------------------------------------------------
# Typed output DTOs
# ---------------------------------------------------------------------------


class BaseStixListada(BaseStixInfo):
    """A STIX base in the Stage 1 listing with the curated-version flag.

    Extends :class:`~app.services.stix.stix_service.BaseStixInfo` (``version``,
    ``name``, ``downloaded``, ``selectable``, ``file``) with the ``curated``
    field, projected from
    :func:`~app.services.stix.stix_service.version_is_curated` (single source in
    ``CURATED_VERSION``, Req. 5.5, 5.6). This way the Stage 1 screen knows, with no
    per-request lookup, which version carries the 8 curated campaigns — version
    18 is the only one with ``curated == True``.
    """

    curated: bool = False


class Stage1ResultStix(BaseModel):
    """The typed result of the Stage 1 orchestration — "STIX base selection".

    Carries the stage's completion verdict and its Output (Req. 5.8, 5.10):

    * ``version`` — the chosen STIX package version (the Stage 1 Input, Req. 5.1).
    * ``completed`` — ``True`` **only when** the version's bundle is on disk
      **and** the dashboard was assembled successfully (Req. 5.10); ``False`` on
      any failure (download/read), keeping the stage not completed (Req. 5.9).
    * ``bundle_on_disk`` — whether the file ``enterprise-attack-{version}.json``
      is present on disk (the first half of the stage Output, Req. 5.8).
    * ``dashboard`` — the bundle dashboard (per-type counts), the second half of
      the stage Output (Req. 5.7, 5.8); ``None`` when the stage did not complete.
    * ``campaigns`` — the campaigns contained in the bundle (Stage 2 Input,
      Req. 2.2); empty when the stage did not complete.
    * ``curated`` — whether the chosen version is the curated version (Req. 5.5,
      5.6).
    * ``error_message`` — the cause reported to the Researcher when the stage did
      not complete (``None`` on success, Req. 5.9).

    The Output (bundle on disk + dashboard + campaigns) of a completed stage is
    the Stage 2 Input (Req. 2.2).
    """

    version: str
    completed: bool = False
    bundle_on_disk: bool = False
    dashboard: Optional[DashboardStix] = None
    campaigns: list[CampaignStix] = Field(default_factory=list)
    curated: bool = False
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class Stage1Service:
    """Orchestrates Stage 1 "STIX base selection" reusing ``StixService``.

    A **thin** service: it does not reimplement listing/download/read/dashboard —
    it delegates everything to the injected
    :class:`~app.services.stix.stix_service.StixService` and only orchestrates
    the Stage 1 sequence and derives the completion verdict (Req. 5.1–5.10, 2.2).

    A custom ``StixService`` can be injected (e.g. tests, with an isolated
    ``data_dir`` and a fake ``http_client``), so the only network egress
    (``download_base``) stays mockable and no real file is touched in tests.
    """

    def __init__(self, stix_service: Optional[StixService] = None) -> None:
        self._stix: StixService = (
            stix_service if stix_service is not None else StixService()
        )

    @property
    def stix_service(self) -> StixService:
        """The :class:`StixService` reused for all disk/network operations."""
        return self._stix

    # ---- list --------------------------------------------------------------

    def list_bases(self) -> list[BaseStixListada]:
        """List known versions with ``downloaded``/``selectable`` and ``curated``.

        Delegates to ``StixService.list_bases`` (newest→oldest ordering,
        live-checked disk presence, no version downloaded by default — Req. 5.4)
        and projects the curated-version flag into each entry via
        :func:`~app.services.stix.stix_service.version_is_curated` (Req. 5.5,
        5.6). No network egress.
        """
        return [
            BaseStixListada(
                version=base.version,
                name=base.name,
                downloaded=base.downloaded,
                selectable=base.selectable,
                file=base.file,
                curated=version_is_curated(base.version),
            )
            for base in self._stix.list_bases()
        ]

    # ---- download ----------------------------------------------------------

    def _bundle_on_disk(self, version: str) -> bool:
        """Whether the ``version`` bundle is already present on disk (no network)."""
        return self._stix.base_path(version).exists()

    def download_base(self, version: str) -> BaseStixInfo:
        """Ensure the ``version`` bundle is on disk, downloading it **only if absent**.

        Reuses the on-disk package without re-downloading when already present
        (Req. 5.3); otherwise downloads via ``StixService.download_base`` — the
        **only network egress** (validates ``version`` against
        ``KNOWN_VERSIONS`` before touching disk/network, atomic write,
        Req. 5.2). On a download failure it propagates ``StixDownloadError`` (no
        partial file stays on disk, Req. 5.9); the caller — :meth:`complete` —
        catches it and keeps the stage not completed with the reported cause.

        Returns:
            A :class:`BaseStixInfo` with ``downloaded=True`` and ``file`` pointing
            at the on-disk bundle (freshly downloaded or already present).

        Raises:
            StixDownloadError: when ``version`` is unknown, the download fails,
                the content is not a valid STIX bundle, or the write fails.
        """
        if self._bundle_on_disk(version):
            path = self._stix.base_path(version)
            return BaseStixInfo(
                version=version,
                name=f"Enterprise ATT&CK v{version}",
                downloaded=True,
                selectable=True,
                file=str(path),
            )
        return self._stix.download_base(version)

    # ---- dashboard ---------------------------------------------------------

    def dashboard(self, version: str) -> DashboardStix:
        """The bundle dashboard (per-type counts) of ``version`` (Req. 5.7).

        Delegates to ``StixService.dashboard``, which reads the bundle from disk
        and derives the per-type counts. The typed :class:`StixFileError` is
        **not** caught here — it propagates to the caller (:meth:`complete`) to
        handle as a not-completed stage with the reported cause (Req. 5.9).

        Raises:
            StixFileError: when the bundle is absent/malformed/invalid.
        """
        return self._stix.dashboard(version)

    def campaigns(self, version: str) -> list[CampaignStix]:
        """The campaigns contained in the ``version`` bundle (Stage 2 Input).

        Delegates to ``StixService.list_campaigns``; the list is the Stage 2
        Input (Req. 2.2). The :class:`StixFileError` propagates (not caught here)
        for the completion verdict to handle it.
        """
        return self._stix.list_campaigns(version)

    # ---- complete ----------------------------------------------------------

    def complete(self, version: str) -> Stage1ResultStix:
        """Orchestrate Stage 1 and return the completion verdict + Output.

        Flow (Req. 5.2, 5.3, 5.7, 5.8, 5.9, 5.10, 2.2):

        1. **Ensure the bundle on disk** — download via :meth:`download_base`
           only if absent (single network egress, reuses disk when present). A
           :class:`StixDownloadError` keeps the stage **not completed** with the
           reported cause and no partial file on disk (Req. 5.9).
        2. **Assemble the dashboard** — via :meth:`dashboard`; a
           :class:`StixFileError` (bundle absent/malformed/invalid) also keeps the
           stage **not completed** with the reported cause (Req. 5.9).
        3. **Assemble the campaigns** — the Stage 2 Input (Req. 2.2).
        4. **Complete** — ``completed = True`` **only when** the bundle is on disk
           **and** the dashboard was assembled (Req. 5.10). The Output (bundle +
           dashboard + campaigns) is the Stage 2 Input.

        Never raises: every failure is caught and reflected in ``completed=False``
        with ``error_message`` filled, so the stage screen always receives a
        reportable result (Req. 5.9).
        """
        curated = version_is_curated(version)

        # 1) Ensure the bundle on disk (only network egress, only if absent).
        try:
            self.download_base(version)
        except StixDownloadError as exc:
            # Download failed: stage not completed, reported cause, no partial
            # file on disk (Req. 5.9).
            return Stage1ResultStix(
                version=version,
                completed=False,
                bundle_on_disk=self._bundle_on_disk(version),
                dashboard=None,
                campaigns=[],
                curated=curated,
                error_message=exc.detail,
            )

        # 2) Assemble the dashboard (reads the bundle from disk).
        try:
            dashboard = self.dashboard(version)
            campaigns = self.campaigns(version)
        except StixFileError as exc:
            # Bundle absent/malformed/invalid after step 1: stage not completed,
            # reported cause (Req. 5.9).
            return Stage1ResultStix(
                version=version,
                completed=False,
                bundle_on_disk=self._bundle_on_disk(version),
                dashboard=None,
                campaigns=[],
                curated=curated,
                error_message=exc.detail,
            )

        # 3+4) Complete only when bundle on disk AND dashboard assembled (Req. 5.10).
        bundle_on_disk = self._bundle_on_disk(version)
        return Stage1ResultStix(
            version=version,
            completed=bundle_on_disk,
            bundle_on_disk=bundle_on_disk,
            dashboard=dashboard,
            campaigns=campaigns,
            curated=curated,
            error_message=None,
        )
