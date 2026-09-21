"""HTTP client for the separately deployed Chatterbox Turbo TTS service."""

from typing import Optional

import httpx


class ChatterboxTTSClient:
    """Generate WAV audio through Saksham's authenticated CUDA TTS service."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        voice: str = "saksham",
        timeout_seconds: float = 45.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.voice = voice
        self.timeout = httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds))
        self.transport = transport

    async def synthesize(self, text: str, *, tone: str = "neutral") -> bytes:
        if not self.base_url:
            raise RuntimeError("Chatterbox TTS URL is not configured")

        headers = {"Accept": "audio/wav"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self.transport,
        ) as client:
            response = await client.post(
                f"{self.base_url}/v1/audio/speech",
                headers=headers,
                json={
                    "input": text,
                    "voice": self.voice,
                    "tone": tone,
                    "response_format": "wav",
                },
            )

        if response.status_code >= 400:
            detail = response.text[:300].strip()
            raise RuntimeError(
                f"Chatterbox TTS returned HTTP {response.status_code}: {detail}"
            )
        if not response.content.startswith(b"RIFF"):
            raise RuntimeError("Chatterbox TTS returned invalid WAV audio")
        return response.content

    async def health(self) -> dict:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self.transport,
        ) as client:
            response = await client.get(f"{self.base_url}/health")
        response.raise_for_status()
        return response.json()
