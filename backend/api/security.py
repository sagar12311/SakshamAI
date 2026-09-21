"""Local-only setup and verification for the admin-code factor."""

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.admin_code import AdminCodeError, AdminCodeRateLimited, get_admin_code_verifier
from core.admin_auth import get_admin_auth_service


router = APIRouter()


class AdminCodeSetup(BaseModel):
    code: str
    current_code: Optional[str] = None


class AdminCodeCheck(BaseModel):
    code: str


@router.get("/admin/status")
async def admin_status():
    status = await get_admin_auth_service().status()
    return {**status, "voice_verification_required": True, "activation_ready": bool(status.get("activation_available"))}


@router.put("/admin/code")
async def set_admin_code(payload: AdminCodeSetup):
    try:
        await asyncio.to_thread(
            get_admin_code_verifier().configure,
            payload.code,
            current_code=payload.current_code,
        )
    except AdminCodeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"configured": True}


@router.post("/admin/code/verify")
async def verify_admin_code(payload: AdminCodeCheck):
    verifier = get_admin_code_verifier()
    try:
        verified = await asyncio.to_thread(verifier.verify, payload.code)
    except AdminCodeRateLimited as error:
        raise HTTPException(
            status_code=429,
            detail=str(error),
            headers={"Retry-After": str(max(1, verifier.retry_after_seconds))},
        ) from error
    return {"verified": verified, "activation_granted": False}
