"""Chat-facing API for reviewing new capability proposals."""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.integration_lab import get_integration_lab


router = APIRouter()


class ProposalRequest(BaseModel):
    request: str = Field(min_length=3, max_length=800)


class ProposalFeedback(BaseModel):
    approved: bool
    feedback: str = Field(default="", max_length=1000)


@router.get("/proposals")
async def list_proposals() -> dict[str, list[dict[str, Any]]]:
    return {"proposals": await get_integration_lab().list_proposals()}


@router.post("/proposals")
async def create_proposal(payload: ProposalRequest) -> dict[str, Any]:
    try:
        proposal = await get_integration_lab().create_proposal(payload.request)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"proposal": proposal}


@router.post("/proposals/{proposal_id}/feedback")
async def save_feedback(proposal_id: str, payload: ProposalFeedback) -> dict[str, Any]:
    proposal = await get_integration_lab().add_feedback(
        proposal_id,
        approved=payload.approved,
        feedback=payload.feedback,
    )
    if not proposal:
        raise HTTPException(status_code=404, detail="Integration proposal not found")
    return {"proposal": proposal}


@router.post("/proposals/{proposal_id}/validate")
async def validate_proposal(proposal_id: str) -> dict[str, Any]:
    proposal = await get_integration_lab().validate(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Integration proposal not found")
    return {"proposal": proposal}
