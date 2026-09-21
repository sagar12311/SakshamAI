"""Authenticated Chatterbox Turbo speech service for Saksham AI."""

import asyncio
import io
import os
import secrets
import time
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, Field


Tone = Literal[
    "neutral", "warm", "reassuring", "calm", "positive", "upbeat",
    "excited", "concerned", "frustrated", "sad", "urgent",
]

TONE_TEMPERATURES = {
    "neutral": 0.76,
    "warm": 0.74,
    "reassuring": 0.72,
    "calm": 0.71,
    "positive": 0.78,
    "upbeat": 0.80,
    "excited": 0.82,
    "concerned": 0.73,
    "frustrated": 0.72,
    "sad": 0.70,
    "urgent": 0.79,
}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    api_key: str = os.getenv("TTS_API_KEY", "")
    device: str = os.getenv("TTS_DEVICE", "cuda")
    voice_id: str = os.getenv("TTS_VOICE_ID", "saksham")
    voice_prompt: str = os.getenv("TTS_VOICE_PROMPT", "")
    max_chars: int = int(os.getenv("TTS_MAX_CHARS", "600"))
    default_temperature: float = float(os.getenv("TTS_TEMPERATURE", "0.8"))
    top_p: float = float(os.getenv("TTS_TOP_P", "0.95"))
    top_k: int = int(os.getenv("TTS_TOP_K", "1000"))
    repetition_penalty: float = float(os.getenv("TTS_REPETITION_PENALTY", "1.2"))
    seed: int = int(os.getenv("TTS_SEED", "2026"))
    warmup_text: str = os.getenv("TTS_WARMUP_TEXT", "Ready.")
    require_cuda: bool = _env_bool("TTS_REQUIRE_CUDA", True)


settings = Settings()

SUPPORTED_DEVICES = {"cpu", "cuda", "mps"}


def seed_generation(torch_module, device: str, seed: int) -> None:
    """Keep one voice identity while still allowing tone-specific prosody."""
    torch_module.manual_seed(seed)
    if device == "cuda":
        torch_module.cuda.manual_seed_all(seed)


class SpeechRequest(BaseModel):
    input: str = Field(min_length=1, max_length=settings.max_chars)
    voice: str = Field(default=settings.voice_id, min_length=1, max_length=80)
    tone: Tone = "neutral"
    response_format: Literal["wav"] = "wav"
    temperature: Optional[float] = Field(default=None, ge=0.1, le=1.5)


class Runtime:
    def __init__(self) -> None:
        self.model = None
        self.torch = None
        self.device_name = "unavailable"
        self.sample_rate = 0
        self.loaded_at = 0.0
        self.warmed_up = False
        self.lock = asyncio.Semaphore(1)

    def load(self) -> None:
        import torch
        from chatterbox.tts_turbo import ChatterboxTurboTTS

        device = settings.device.strip().lower()
        validate_device_name(device)
        if settings.require_cuda and device != "cuda":
            raise RuntimeError("TTS_REQUIRE_CUDA=true requires TTS_DEVICE=cuda")
        if device == "cuda":
            if not torch.cuda.is_available():
                raise RuntimeError("TTS_DEVICE=cuda but CUDA is unavailable")
            # This catches incompatible sm_120 wheels that can detect the GPU but cannot run kernels.
            probe = torch.ones(1, device="cuda") + 1
            torch.cuda.synchronize()
            if probe.item() != 2:
                raise RuntimeError("CUDA startup probe returned an invalid result")
            self.device_name = torch.cuda.get_device_name(0)
        elif device == "mps":
            if not torch.backends.mps.is_available():
                raise RuntimeError("TTS_DEVICE=mps but Apple Metal acceleration is unavailable")
            # Fail at startup if this PyTorch build cannot execute on Apple Silicon.
            probe = torch.ones(1, device="mps") + 1
            torch.mps.synchronize()
            if probe.item() != 2:
                raise RuntimeError("MPS startup probe returned an invalid result")
            self.device_name = "Apple Metal Performance Shaders"
        else:
            self.device_name = "CPU"

        model = ChatterboxTurboTTS.from_pretrained(device=device)
        if settings.voice_prompt:
            voice_path = Path(settings.voice_prompt)
            if not voice_path.is_file():
                raise RuntimeError(f"Voice prompt does not exist: {voice_path}")
            model.prepare_conditionals(str(voice_path))
        elif model.conds is None:
            raise RuntimeError("No built-in voice is available; configure TTS_VOICE_PROMPT")

        self.model = model
        self.torch = torch
        self.sample_rate = int(model.sr)
        if settings.warmup_text.strip():
            self.synthesize(SpeechRequest(input=settings.warmup_text.strip()))
            self.warmed_up = True
        self.loaded_at = time.time()

    def synthesize(self, request: SpeechRequest) -> bytes:
        if self.model is None or self.torch is None:
            raise RuntimeError("Chatterbox model is not loaded")

        temperature = request.temperature
        if temperature is None:
            temperature = TONE_TEMPERATURES.get(
                request.tone,
                settings.default_temperature,
            )

        with self.torch.inference_mode():
            seed_generation(self.torch, settings.device.strip().lower(), settings.seed)
            audio = self.model.generate(
                request.input,
                temperature=temperature,
                top_p=settings.top_p,
                top_k=settings.top_k,
                repetition_penalty=settings.repetition_penalty,
            )

        waveform = audio.detach().float().cpu()
        if waveform.ndim == 1:
            waveform = waveform.unsqueeze(0)
        waveform = waveform.clamp(-1.0, 1.0)
        pcm = (waveform * 32767.0).round().to(self.torch.int16).numpy()

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(int(pcm.shape[0]))
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(pcm.T.tobytes())
        return buffer.getvalue()

    def accelerator_status(self) -> dict:
        result = {
            "device": settings.device.strip().lower(),
            "device_name": self.device_name,
        }
        if self.torch is not None and self.torch.cuda.is_available():
            free_bytes, total_bytes = self.torch.cuda.mem_get_info()
            result.update({
                "cuda_version": self.torch.version.cuda,
                "compute_capability": list(self.torch.cuda.get_device_capability(0)),
                "free_vram_mb": round(free_bytes / 1024 / 1024),
                "total_vram_mb": round(total_bytes / 1024 / 1024),
            })
        if self.torch is not None:
            result["mps_available"] = self.torch.backends.mps.is_available()
        return result


runtime = Runtime()


def validate_device_name(device: str) -> None:
    if device not in SUPPORTED_DEVICES:
        supported = ", ".join(sorted(SUPPORTED_DEVICES))
        raise RuntimeError(f"Unsupported TTS_DEVICE={device!r}; choose one of: {supported}")


def validate_api_key(api_key: str) -> None:
    """Reject missing, short, or copied example credentials at startup."""
    if (
        len(api_key) < 32
        or api_key.lower().startswith("replace-with")
        or api_key.lower().startswith("your-")
    ):
        raise RuntimeError(
            "TTS_API_KEY must be a generated secret containing at least 32 characters"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_api_key(settings.api_key)
    await asyncio.to_thread(runtime.load)
    yield


app = FastAPI(
    title="Saksham Chatterbox Turbo TTS",
    version="1.0.0",
    lifespan=lifespan,
)


def authorize(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> None:
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    supplied = bearer or (x_api_key or "")
    if not secrets.compare_digest(supplied, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid TTS API key",
        )


@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy" if runtime.model is not None else "loading",
        "model": "ResembleAI/chatterbox-turbo",
        "voice": settings.voice_id,
        "sample_rate": runtime.sample_rate,
        "loaded_at": runtime.loaded_at,
        "warmed_up": runtime.warmed_up,
        "seed": settings.seed,
        "accelerator": runtime.accelerator_status(),
    }


@app.post(
    "/v1/audio/speech",
    dependencies=[Depends(authorize)],
    response_class=Response,
)
async def create_speech(request: SpeechRequest) -> Response:
    if request.voice != settings.voice_id:
        raise HTTPException(status_code=404, detail=f"Unknown voice: {request.voice}")

    started = time.perf_counter()
    try:
        async with runtime.lock:
            audio = await asyncio.to_thread(runtime.synthesize, request)
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"Synthesis failed: {error}") from error

    elapsed_ms = round((time.perf_counter() - started) * 1000)
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "X-TTS-Generation-Ms": str(elapsed_ms),
            "X-TTS-Sample-Rate": str(runtime.sample_rate),
        },
    )
