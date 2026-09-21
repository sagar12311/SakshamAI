"""Lifecycle management for Saksham's bundled local Chatterbox service."""

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from loguru import logger

from config import get_settings
from core.chatterbox_client import ChatterboxTTSClient


HealthCheck = Callable[[], Awaitable[bool]]
PortCheck = Callable[[], Awaitable[bool]]
ProcessFactory = Callable[[Path, Path, Any], Awaitable[Any]]


class ChatterboxServiceManager:
    """Start one bundled Chatterbox process and stop it with the backend."""

    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str,
        api_key: str = "",
        autostart: bool = True,
        startup_timeout_seconds: float = 120.0,
        service_dir: Optional[Path] = None,
        log_path: Optional[Path] = None,
        health_check: Optional[HealthCheck] = None,
        port_check: Optional[PortCheck] = None,
        process_factory: Optional[ProcessFactory] = None,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.autostart = autostart
        self.startup_timeout_seconds = startup_timeout_seconds
        self.service_dir = service_dir or (
            Path(__file__).resolve().parents[2] / "services" / "chatterbox_tts"
        )
        self.log_path = log_path or (
            Path.home() / ".saksham" / "logs" / "chatterbox.log"
        )
        self.poll_interval_seconds = poll_interval_seconds
        self._health_check = health_check or self._check_health
        self._port_check = port_check or self._check_port
        self._process_factory = process_factory or self._spawn_process
        self._process: Any = None
        self._log_handle: Any = None
        self._state = "not_started"
        self._owned = False

    @staticmethod
    def is_loopback_url(url: str) -> bool:
        """Only local loopback services are safe for the backend to own."""
        hostname = (urlparse(url).hostname or "").lower()
        return hostname in {"localhost", "127.0.0.1", "::1"}

    @property
    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "url": self.base_url,
            "autostart": self.autostart,
            "state": self._state,
            "owned_by_backend": self._owned,
        }

    async def _check_health(self) -> bool:
        client = ChatterboxTTSClient(
            self.base_url,
            api_key=self.api_key,
            timeout_seconds=3.0,
        )
        try:
            health = await client.health()
        except Exception:
            return False
        return health.get("status") == "healthy"

    async def _check_port(self) -> bool:
        parsed = urlparse(self.base_url)
        if not parsed.hostname:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(parsed.hostname, port),
                timeout=0.4,
            )
            del reader
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    async def _spawn_process(self, script: Path, cwd: Path, log_handle: Any) -> Any:
        return await asyncio.create_subprocess_exec(
            str(script),
            cwd=str(cwd),
            stdout=log_handle,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )

    async def _wait_until_healthy(self) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.startup_timeout_seconds
        while True:
            if await self._health_check():
                return True
            if self._process is not None and self._process.returncode is not None:
                return False
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(self.poll_interval_seconds, remaining))

    async def start(self) -> bool:
        """Ensure local Chatterbox is healthy without spawning duplicates."""
        if not self.enabled:
            self._state = "disabled"
            return False
        if not self.is_loopback_url(self.base_url):
            self._state = "remote"
            logger.info("Chatterbox uses a remote endpoint; backend autostart is skipped")
            return False
        if await self._health_check():
            self._state = "external"
            logger.info("Chatterbox is already healthy; reusing the running service")
            return True
        if not self.autostart:
            self._state = "unavailable"
            logger.warning("Local Chatterbox is unavailable and autostart is disabled")
            return False

        # A listener can be an independently started Chatterbox that is still loading.
        # Waiting here avoids racing it with a second process on the same port.
        if await self._port_check():
            self._state = "waiting_for_external"
            logger.info("Chatterbox port is occupied; waiting for that service to become healthy")
            if await self._wait_until_healthy():
                self._state = "external"
                return True
            self._state = "unavailable"
            logger.error("The process on the Chatterbox port did not become healthy")
            return False

        script = self.service_dir / "run_macos.sh"
        if not script.is_file():
            self._state = "unavailable"
            logger.error(f"Bundled Chatterbox runner was not found: {script}")
            return False

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("ab")
        try:
            self._process = await self._process_factory(
                script,
                self.service_dir,
                self._log_handle,
            )
            self._owned = True
            self._state = "starting"
            logger.info(f"Starting bundled Chatterbox; logs: {self.log_path}")
            if await self._wait_until_healthy():
                self._state = "healthy"
                logger.info("Bundled Chatterbox is healthy and warmed up")
                return True
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception as error:
            logger.error(f"Could not start bundled Chatterbox: {error}")

        await self.stop()
        self._state = "unavailable"
        logger.error(f"Bundled Chatterbox failed to start; inspect {self.log_path}")
        return False

    async def stop(self) -> None:
        """Stop only a process launched by this backend instance."""
        process = self._process
        if self._owned and process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._log_handle is not None:
            self._log_handle.close()
        self._process = None
        self._log_handle = None
        self._owned = False
        if self._state not in {"disabled", "remote", "external", "unavailable"}:
            self._state = "stopped"


_manager: Optional[ChatterboxServiceManager] = None


def get_chatterbox_service_manager() -> ChatterboxServiceManager:
    global _manager
    if _manager is None:
        settings = get_settings()
        api_key = ""
        if settings.chatterbox_tts_api_key:
            api_key = settings.chatterbox_tts_api_key.get_secret_value()
        _manager = ChatterboxServiceManager(
            enabled=(settings.tts_enabled and settings.tts_provider == "chatterbox"),
            base_url=settings.chatterbox_tts_url,
            api_key=api_key,
            autostart=settings.chatterbox_tts_autostart,
            startup_timeout_seconds=settings.chatterbox_tts_startup_timeout_seconds,
        )
    return _manager
