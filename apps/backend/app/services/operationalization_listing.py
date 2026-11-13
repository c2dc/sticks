"""Operationalization listing: campaign techniques -> exact Atomic commands.

Backs ``GET /api/stix/bases/{version}/campaigns/{campaign_key}/operationalization``.
The technique list comes from ``stix_campaign_techniques`` and the commands from
an exact relational lookup in ``technique_commands`` (never the vector memory),
plus the ingestion state of the ``atomic`` source (Req. 6.2, 6.3, 6.4, 6.6).
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import IngestSourceState
from app.models.enums import IngestSource, IngestStatus
from app.services.atomic_arguments import resolve_command
from app.services.command_base import CommandBase, CommandSuggestion
from app.services.stix.stix_package_read_service import StixPackageReadService


class OperationalizationCommand(BaseModel):
    """One candidate command of a technique."""

    id: int | None = None
    command: str
    executor: str
    platform: str
    source: str
    resolved_command: str = ""
    arguments_used: dict[str, str] = Field(default_factory=dict)
    unresolved_arguments: list[str] = Field(default_factory=list)


def _to_listing_command(c: CommandSuggestion) -> OperationalizationCommand:
    """Build the API command: ``command`` is the canonical template and
    ``resolved_command`` is only an example with the Atomic default values."""
    resolved = resolve_command(c.command, c.input_arguments)
    return OperationalizationCommand(
        id=c.id,
        command=c.command,
        executor=c.executor,
        platform=c.platform,
        source=c.source,
        resolved_command=resolved.command,
        arguments_used=resolved.arguments_used,
        unresolved_arguments=resolved.unresolved_arguments,
    )


class OperationalizationTechnique(BaseModel):
    """A campaign technique with its (possibly empty) command list."""

    technique_id: str
    technique_name: str | None = None
    commands: list[OperationalizationCommand] = Field(default_factory=list)


class AtomicStatus(BaseModel):
    """Ingestion state of the Atomic Red Team source."""

    status: IngestStatus
    loaded_count: int | None = None
    expected_count: int | None = None


class OperationalizationListing(BaseModel):
    """Response of the operationalization listing route."""

    campaign_key: str
    atomic_status: AtomicStatus
    techniques: list[OperationalizationTechnique] = Field(default_factory=list)


class OperationalizationListingService:
    """Builds the operationalization listing from the database only."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_for_campaign(self, version: str, campaign_key: str) -> OperationalizationListing:
        """Return techniques, exact commands and the atomic ingestion status.

        Raises:
            StixPackageNotPersistedError: unknown/unpersisted ``version``.
            CampaignNotFoundError: no campaign matches ``campaign_key``.
        """
        overview = StixPackageReadService(self._session).campaign_overview(version, campaign_key)
        techniques = sorted(overview.techniques, key=lambda t: t.technique_id)
        commands_by_technique = CommandBase(self._session).exact_commands_for(
            [t.technique_id for t in techniques]
        )
        return OperationalizationListing(
            campaign_key=campaign_key,
            atomic_status=self._atomic_status(),
            techniques=[
                OperationalizationTechnique(
                    technique_id=t.technique_id,
                    technique_name=t.technique_name,
                    commands=[
                        _to_listing_command(c)
                        for c in commands_by_technique.get(t.technique_id, [])
                    ],
                )
                for t in techniques
            ],
        )

    def _atomic_status(self) -> AtomicStatus:
        """Read the ``atomic`` row of ``ingest_source_state`` (pending if absent)."""
        row = self._session.execute(
            select(IngestSourceState).where(IngestSourceState.source == IngestSource.ATOMIC)
        ).scalar_one_or_none()
        if row is None:
            return AtomicStatus(status=IngestStatus.PENDING)
        return AtomicStatus(
            status=row.status,
            loaded_count=row.loaded_count,
            expected_count=row.expected_count,
        )
