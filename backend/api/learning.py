"""Local, review-gated procedural learning API.

Candidates are observations only.  This router intentionally has no endpoint
to run, schedule, or inject an approved candidate into the planner.
"""

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.state_store import TaskStateError, get_state_store


router = APIRouter()


class CandidateReview(BaseModel):
    """A local human review decision, not an execution authorization."""

    note: Optional[str] = Field(default=None, max_length=4_000)


def _state_error(error: TaskStateError) -> HTTPException:
    detail = str(error)
    if "already" in detail:
        return HTTPException(status_code=409, detail=detail)
    return HTTPException(status_code=400, detail=detail)


@router.get("/candidates")
async def list_candidates(
    status: Optional[Literal["observed", "approved", "rejected"]] = None,
    limit: int = 100,
):
    """List local candidates awaiting or having completed human review."""
    try:
        candidates = await get_state_store().list_learning_candidates(status=status, limit=limit)
    except TaskStateError as error:
        raise _state_error(error) from error
    return {
        "candidates": candidates,
        "local_only": True,
        "execution_enabled": False,
    }


@router.get("/candidates/{candidate_id}")
async def get_candidate(candidate_id: str):
    """Read one redacted, review-only candidate and its provenance."""
    candidate = await get_state_store().get_learning_candidate(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail=f"Learning candidate not found: {candidate_id}")
    return {"candidate": candidate, "execution_enabled": False}


@router.post("/candidates/{candidate_id}/approve")
async def approve_candidate(candidate_id: str, review: CandidateReview):
    """Mark a candidate approved for human-reviewed future product work only."""
    try:
        candidate = await get_state_store().review_learning_candidate(
            candidate_id,
            decision="approved",
            note=review.note,
        )
    except TaskStateError as error:
        raise _state_error(error) from error
    if not candidate:
        raise HTTPException(status_code=404, detail=f"Learning candidate not found: {candidate_id}")
    return {"updated": True, "candidate": candidate, "execution_enabled": False}


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate(candidate_id: str, review: CandidateReview):
    """Reject a candidate without touching its original task or execution."""
    try:
        candidate = await get_state_store().review_learning_candidate(
            candidate_id,
            decision="rejected",
            note=review.note,
        )
    except TaskStateError as error:
        raise _state_error(error) from error
    if not candidate:
        raise HTTPException(status_code=404, detail=f"Learning candidate not found: {candidate_id}")
    return {"updated": True, "candidate": candidate, "execution_enabled": False}


@router.delete("/candidates/{candidate_id}")
async def delete_candidate(candidate_id: str):
    """Permanently remove one local candidate, not the originating task."""
    deleted = await get_state_store().delete_learning_candidate(candidate_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Learning candidate not found: {candidate_id}")
    return {"deleted": True, "candidate_id": candidate_id}
