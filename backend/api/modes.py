"""
Saksham AI - Modes API
Operating mode management.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.cognition_bus import get_cognition_bus, CognitionMessage, MessageType
from core.state_store import get_state_store
from config import get_settings


router = APIRouter()

settings = get_settings()


class ModeChange(BaseModel):
    mode: str  # assist, execute, autonomous, shadow


MODE_DESCRIPTIONS = {
    "assist": "Suggests actions, waits for approval before executing",
    "execute": "Executes after brief confirmation",
    "autonomous": "Executes within defined boundaries, reports after",
    "shadow": "Observes silently, learns patterns, suggests improvements",
}


@router.get("/current")
async def get_current_mode():
    """Get the current operating mode"""
    mode = await get_state_store().get_mode(settings.default_mode)
    return {
        "mode": mode,
        "description": MODE_DESCRIPTIONS.get(mode, ""),
    }


@router.get("/available")
async def list_modes():
    """List all available operating modes"""
    return {
        "modes": [
            {"name": mode, "description": desc}
            for mode, desc in MODE_DESCRIPTIONS.items()
        ]
    }


@router.post("/switch")
async def switch_mode(change: ModeChange):
    """Switch to a different operating mode"""
    if change.mode not in MODE_DESCRIPTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown mode: {change.mode}")

    await get_state_store().set_mode(change.mode)
    
    # Broadcast mode change
    bus = get_cognition_bus()
    
    message = CognitionMessage(
        type=MessageType.MODE_CHANGE,
        payload={"mode": change.mode},
        source="api",
    )
    
    await bus.publish(message)
    
    return {
        "switched": True,
        "mode": change.mode,
        "description": MODE_DESCRIPTIONS[change.mode],
    }
