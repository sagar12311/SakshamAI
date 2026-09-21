"""HTTP boundary for local or private-server meeting intelligence workers."""

from __future__ import annotations

from typing import Any, Optional

import httpx
from loguru import logger

from config import get_settings
from core.llm_client import get_llm_client


class MeetingIntelligenceUnavailable(RuntimeError):
    pass


class MeetingIntelligenceClient:
    """Tries the private RTX worker, then the bundled local worker, then ASR fallback."""

    def __init__(
        self,
        remote_url: Optional[str] = None,
        local_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        settings = get_settings()
        configured_key = settings.meeting_intelligence_api_key
        self.remote_url = (remote_url or settings.meeting_intelligence_remote_url or "").rstrip("/")
        self.local_url = (local_url or settings.meeting_intelligence_local_url or "").rstrip("/")
        self.api_key = api_key if api_key is not None else (
            configured_key.get_secret_value() if configured_key else ""
        )
        self.timeout_seconds = timeout_seconds or settings.meeting_intelligence_timeout_seconds
        self.transport = transport

    @property
    def urls(self) -> list[str]:
        urls: list[str] = []
        for url in (self.remote_url, self.local_url):
            if url and url not in urls:
                urls.append(url)
        return urls

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def health(self) -> dict[str, Any]:
        statuses: list[dict[str, Any]] = []
        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
            for url in self.urls:
                try:
                    response = await client.get(f"{url}/health", headers=self.headers)
                    payload = response.json() if response.content else {}
                    statuses.append({"url": url, "ok": response.is_success, **payload})
                except Exception as error:
                    statuses.append({"url": url, "ok": False, "error": str(error)})
        return {"workers": statuses, "available": any(item.get("ok") for item in statuses)}

    async def _post_audio(
        self,
        endpoint: str,
        audio: bytes,
        data: Optional[dict[str, Any]] = None,
        local_data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        errors: list[str] = []
        timeout = httpx.Timeout(self.timeout_seconds, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
            for url in self.urls:
                try:
                    request_data = (
                        local_data
                        if local_data is not None and url == self.local_url and url != self.remote_url
                        else data
                    )
                    response = await client.post(
                        f"{url}{endpoint}",
                        headers=self.headers,
                        files={"audio": ("meeting.wav", audio, "audio/wav")},
                        data={key: str(value) for key, value in (request_data or {}).items()},
                    )
                    if response.status_code in {409, 429, 503}:
                        errors.append(f"{url}: worker busy or unavailable ({response.status_code})")
                        continue
                    response.raise_for_status()
                    payload = response.json()
                    payload.setdefault("provider", url)
                    return payload
                except Exception as error:
                    errors.append(f"{url}: {error}")
                    logger.debug(f"Meeting worker {endpoint} failed at {url}: {error}")
        raise MeetingIntelligenceUnavailable("; ".join(errors) or "No meeting worker configured")

    async def transcribe(
        self,
        audio: bytes,
        *,
        offset_ms: int = 0,
        final: bool = False,
    ) -> dict[str, Any]:
        settings = get_settings()
        remote_model = (
            settings.meeting_final_whisper_model if final else settings.meeting_live_whisper_model
        )
        local_model = (
            settings.meeting_local_final_whisper_model
            if final
            else settings.meeting_live_whisper_model
        )
        request_data = {
            "offset_ms": offset_ms,
            "model": remote_model,
            "language": "auto",
            "final": str(final).lower(),
        }
        local_data = {**request_data, "model": local_model}
        try:
            return await self._post_audio(
                "/v1/transcribe",
                audio,
                request_data,
                local_data,
            )
        except MeetingIntelligenceUnavailable as worker_error:
            try:
                text = await get_llm_client().transcribe(audio, language=None)
            except Exception as fallback_error:
                raise MeetingIntelligenceUnavailable(
                    f"{worker_error}; local transcription failed: {fallback_error}"
                ) from fallback_error
            if not text.strip():
                return {"segments": [], "provider": "backend-faster-whisper"}
            duration_ms = _wav_duration_ms(audio)
            return {
                "segments": [
                    {
                        "start_ms": offset_ms,
                        "end_ms": offset_ms + duration_ms,
                        "text": text.strip(),
                        "confidence": None,
                    }
                ],
                "provider": "backend-faster-whisper",
                "warning": str(worker_error),
            }

    async def diarize(
        self,
        audio: bytes,
        *,
        num_speakers: Optional[int] = None,
        min_speakers: Optional[int] = None,
        max_speakers: Optional[int] = None,
    ) -> dict[str, Any]:
        data = {
            key: value
            for key, value in {
                "num_speakers": num_speakers,
                "min_speakers": min_speakers,
                "max_speakers": max_speakers,
            }.items()
            if value is not None
        }
        return await self._post_audio("/v1/diarize", audio, data)

    async def embed(self, audio: bytes) -> dict[str, Any]:
        return await self._post_audio("/v1/embed", audio)


def _wav_duration_ms(audio: bytes) -> int:
    import io
    import wave

    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            return round((wav.getnframes() / max(1, wav.getframerate())) * 1000)
    except Exception:
        return 0
