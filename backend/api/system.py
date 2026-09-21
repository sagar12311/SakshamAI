"""Legacy system API surface.

System-changing routes remain present only to give older local clients a clear
migration response.  They must not bypass Saksham's planner, ActionPolicy,
affirmation records, or the Command Center audit trail.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


router = APIRouter()


def _direct_control_disabled() -> None:
    """Keep every side-effecting legacy endpoint fail-closed.

    A caller must submit a structured task through the normal planning flow;
    the Executor then re-checks policy immediately before touching the Mac.
    """
    raise HTTPException(
        status_code=403,
        detail=(
            "Direct system control is disabled. Submit the request through "
            "Saksham's Command Center so policy and approvals are enforced."
        ),
    )


class AppAction(BaseModel):
    app_name: str
    action: str = "open"
    file_path: Optional[str] = None
    url: Optional[str] = None


class TerminalCommand(BaseModel):
    command: str
    cwd: Optional[str] = None


class BrowserAction(BaseModel):
    action: str  # navigate, click, type, etc.
    url: Optional[str] = None
    selector: Optional[str] = None
    value: Optional[str] = None
    browser: str = "Google Chrome"


@router.post("/app")
async def control_app(action: AppAction):
    """Disabled: direct app control would bypass task authorization."""
    _direct_control_disabled()


@router.post("/terminal")
async def run_terminal(cmd: TerminalCommand):
    """Disabled: terminal commands require a Command Center task."""
    _direct_control_disabled()


@router.post("/browser")
async def control_browser(action: BrowserAction):
    """Disabled: direct browser control would bypass task authorization."""
    _direct_control_disabled()


@router.get("/frontmost")
async def get_frontmost_app():
    """Get the currently active application"""
    from mac import get_frontmost_app
    
    app = await get_frontmost_app()
    return {"app": app}


@router.post("/notify")
async def send_notification(title: str, message: str):
    """Disabled: side effects must remain visible in the Command Center."""
    _direct_control_disabled()
