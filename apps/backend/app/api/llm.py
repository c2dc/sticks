"""Language-model profile routes (``/api/llm/*``).

* ``GET /api/llm/profiles``: list the work profile and the local Ollama models.
* ``PUT /api/llm/selection``: choose the profile the agents use.
* ``POST /api/llm/preflight``: check that a profile can answer before a run.

Responses carry only codes and numbers. They never include a base URL or a key.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.ai.llm_profiles import (
    WORK_PROFILE_ID,
    LlmProfile,
    ProfileKind,
    find_profile,
    list_profiles,
    read_api_key,
)
from app.services.ai.llm_selection import load_selected_profile_id, save_selected_profile_id
from app.services.ai.preflight import PreflightResult, invalidate_preflight_cache, run_preflight

router = APIRouter(prefix="/api/llm", tags=["llm"])


class LlmProfileView(BaseModel):
    """One selectable model profile."""

    id: str = Field(..., description="Profile id: 'work' or 'ollama:<model name>'.")
    kind: str = Field(..., description="'openai_compatible' or 'ollama_local'.")
    model_id: str = Field(..., description="Model identifier sent to the server.")
    is_default: bool = Field(..., description="True for the work profile.")
    configured: bool = Field(
        ..., description="False when a required SEPINF_* variable is missing (work profile only)."
    )
    size_bytes: int | None = Field(None, description="Model size on disk (local profiles only).")
    memory_fit: str | None = Field(
        None,
        description=("fits_gpu, fits_ram_only, too_large or memory_unknown (local profiles only)."),
    )


class LlmProfilesResponse(BaseModel):
    """The selectable profiles and the current selection."""

    selected_id: str = Field(..., description="Id of the selected profile.")
    local_discovery: str = Field(..., description="'ok' or 'ollama_down'.")
    profiles: list[LlmProfileView] = Field(..., description="Work profile first, then local ones.")


class LlmSelectionRequest(BaseModel):
    """Body of ``PUT /api/llm/selection``."""

    profile_id: str = Field(..., description="Id of the profile to select.")


class LlmSelectionResponse(BaseModel):
    """Result of ``PUT /api/llm/selection``."""

    selected_id: str = Field(..., description="Id of the selected profile.")


class LlmPreflightRequest(BaseModel):
    """Body of ``POST /api/llm/preflight``."""

    profile_id: str | None = Field(
        None, description="Profile to check; null checks the selected profile."
    )
    force: bool = Field(False, description="Ignore the 30 s cache and check again.")


def _is_configured(profile: LlmProfile) -> bool:
    if profile.kind is not ProfileKind.OPENAI_COMPATIBLE:
        return True
    return bool(profile.base_url and profile.model_id and read_api_key(profile))


def selected_profile(db: Session) -> LlmProfile:
    """Return the selected profile, falling back to the work profile when invalid."""
    profile = find_profile(load_selected_profile_id(db))
    return profile if profile is not None else find_profile(WORK_PROFILE_ID)  # type: ignore[return-value]


def _unknown_profile() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail={"reason": "unknown_profile"}
    )


@router.get("/profiles", response_model=LlmProfilesResponse, summary="List model profiles")
def get_profiles(db: Session = Depends(get_db)) -> LlmProfilesResponse:
    """List profiles with size and memory fit. Does not call any model."""
    listing = list_profiles()
    views = [
        LlmProfileView(
            id=entry.profile.id,
            kind=entry.profile.kind.value,
            model_id=entry.profile.model_id,
            is_default=entry.profile.id == WORK_PROFILE_ID,
            configured=_is_configured(entry.profile),
            size_bytes=entry.size_bytes,
            memory_fit=entry.memory_fit.value if entry.memory_fit is not None else None,
        )
        for entry in listing.profiles
    ]
    return LlmProfilesResponse(
        selected_id=selected_profile(db).id,
        local_discovery=listing.local_discovery,
        profiles=views,
    )


@router.put(
    "/selection",
    response_model=LlmSelectionResponse,
    summary="Select the model profile",
    responses={status.HTTP_404_NOT_FOUND: {"description": "unknown_profile"}},
)
def put_selection(body: LlmSelectionRequest, db: Session = Depends(get_db)) -> LlmSelectionResponse:
    """Persist the selected profile; the previous selection stays when the id is unknown."""
    profile = find_profile(body.profile_id)
    if profile is None:
        raise _unknown_profile()
    save_selected_profile_id(db, profile.id)
    invalidate_preflight_cache(profile.id)
    return LlmSelectionResponse(selected_id=profile.id)


@router.post(
    "/preflight",
    response_model=PreflightResult,
    summary="Check that a model profile can answer",
    responses={status.HTTP_404_NOT_FOUND: {"description": "unknown_profile"}},
)
def post_preflight(body: LlmPreflightRequest, db: Session = Depends(get_db)) -> PreflightResult:
    """Run the preflight for a profile without changing the selection."""
    if body.profile_id is None:
        profile = selected_profile(db)
    else:
        found = find_profile(body.profile_id)
        if found is None:
            raise _unknown_profile()
        profile = found
    return run_preflight(profile, force=body.force)
