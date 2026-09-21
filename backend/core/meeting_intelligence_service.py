"""Lifecycle manager for Saksham's optional bundled meeting worker."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from loguru import logger

from config import get_settings


class MeetingIntelligenceServiceManager:
    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str,
        api_key: str = "",
        autostart: bool = False,
        startup_timeout_seconds: float = 180.0,
        service_dir: Optional[Path] = None,
    ) -> None:
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.autostart = autostart
        self.startup_timeout_seconds = startup_timeout_seconds
        self.service_dir = service_dir or (
            Path(__file__).resolve().parents[2] / "services" / "meeting_intelligence"
        )
        self.log_path = Path.home() / ".saksham" / "logs" / "meeting-intelligence.log"
        self._process: Any = None
        self._log_handle: Any = None
        self._owned = False
        self._state = "not_started"

    @staticmethod
    def is_loopback_url(url: str) -> bool:
        return (urlparse(url).hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}

    @property
    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "url": self.base_url,
            "autostart": self.autostart,
            "state": self._state,
            "owned_by_backend": self._owned,
        }

    async def _healthy(self) -> bool:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{self.base_url}/health", headers=headers)
            return response.is_success
        except Exception:
            return False

    async def start(self) -> bool:
        if not self.enabled:
            self._state = "disabled"
            return False
        if not self.is_loopback_url(self.base_url):
            self._state = "remote"
            return False
        if await self._healthy():
            self._state = "external"
            return True
        if not self.autostart:
            self._state = "unavailable"
            return False
        script = self.service_dir / "run_macos.sh"
        if not script.is_file():
            self._state = "unavailable"
            return False
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("ab")
        try:
            environment = os.environ.copy()
            if self.api_key:
                environment["MEETING_INTELLIGENCE_API_KEY"] = self.api_key
            self._process = await asyncio.create_subprocess_exec(
                str(script),
                cwd=str(self.service_dir),
                env=environment,
                stdout=self._log_handle,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            self._owned = True
            self._state = "starting"
            deadline = asyncio.get_running_loop().time() + self.startup_timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                if await self._healthy():
                    self._state = "healthy"
                    return True
                if self._process.returncode is not None:
                    break
                await asyncio.sleep(0.5)
        except Exception as error:
            logger.warning(f"Bundled meeting worker did not start: {error}")
        await self.stop()
        self._state = "unavailable"
        return False

    async def stop(self) -> None:
        if self._owned and self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._log_handle:
            self._log_handle.close()
        self._process = None
        self._log_handle = None
        self._owned = False
        if self._state not in {"disabled", "remote", "external", "unavailable"}:
            self._state = "stopped"


_manager: Optional[MeetingIntelligenceServiceManager] = None


def get_meeting_intelligence_service_manager() -> MeetingIntelligenceServiceManager:
    global _manager
    if _manager is None:
        settings = get_settings()
        key = (
            settings.meeting_intelligence_api_key.get_secret_value()
            if settings.meeting_intelligence_api_key
            else ""
        )
        _manager = MeetingIntelligenceServiceManager(
            enabled=settings.meeting_mode_enabled,
            base_url=settings.meeting_intelligence_local_url,
            api_key=key,
            autostart=settings.meeting_intelligence_autostart,
            startup_timeout_seconds=settings.meeting_intelligence_startup_timeout_seconds,
        )
    return _manager
