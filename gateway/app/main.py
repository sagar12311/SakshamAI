"""Public, authenticated edge for Saksham's private home-server services."""

from __future__ import annotations

import asyncio
from io import BytesIO
import math
import wave
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

import asyncpg
import httpx
import jwt
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    supabase_url: HttpUrl
    supabase_jwks_url: HttpUrl | None = None
    database_url: str
    hosted_llm_base_url: HttpUrl
    hosted_llm_api_key: str
    hosted_llm_model: str = "openai/gpt-oss-20b"
    meeting_worker_url: HttpUrl
    meeting_worker_api_key: str
    cors_origins: list[str] = ["https://saksham.ai"]
    allowed_byok_hosts: list[str] = ["api.openai.com"]
    daily_token_limit: int = 10_000
    daily_global_token_limit: int = 50_000
    max_output_tokens: int = 1_024
    max_audio_bytes: int = 25 * 1024 * 1024
    daily_meeting_minutes: int = 30
    daily_global_meeting_minutes: int = 120
    max_hosted_meeting_upload_seconds: int = Field(default=900, ge=1, le=3_600)
    max_concurrent_hosted_jobs: int = Field(default=1, ge=1, le=4)

    @property
    def jwks_url(self) -> str:
        return str(self.supabase_jwks_url or f"{str(self.supabase_url).rstrip('/')}/auth/v1/.well-known/jwks.json")


@lru_cache
def settings() -> Settings:
    return Settings()


@lru_cache
def jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(jwks_url, cache_keys=True)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=12_000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=30)
    mode: Literal["hosted", "byok"] = "hosted"
    model: str | None = Field(default=None, max_length=160)
    provider_url: HttpUrl | None = None
    max_tokens: int = Field(default=512, ge=1, le=1_024)


SUPPORTED_AUDIO_SUFFIXES = {".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".wav", ".webm"}


class User(BaseModel):
    id: str
    email: str | None = None


class QuotaStore:
    aggregate_user_id = "__saksham_public_aggregate__"

    def __init__(self, pool: asyncpg.Pool, limit: int, global_limit: int, meeting_minutes: int, global_meeting_minutes: int):
        self.pool = pool
        self.limit = limit
        self.global_limit = global_limit
        self.meeting_minutes = meeting_minutes
        self.global_meeting_minutes = global_meeting_minutes

    async def initialize(self) -> None:
        await self.pool.execute("""
            CREATE TABLE IF NOT EXISTS saksham_daily_quota (
              user_id TEXT NOT NULL, usage_day DATE NOT NULL,
              used_tokens INTEGER NOT NULL DEFAULT 0, reserved_tokens INTEGER NOT NULL DEFAULT 0,
              meeting_seconds INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(user_id, usage_day)
            )
        """)

    async def reserve_tokens(self, user_id: str, amount: int) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow("""
          INSERT INTO saksham_daily_quota (user_id, usage_day, reserved_tokens)
          VALUES ($1, CURRENT_DATE, $2)
          ON CONFLICT (user_id, usage_day) DO UPDATE SET reserved_tokens = saksham_daily_quota.reserved_tokens + $2
          WHERE saksham_daily_quota.used_tokens + saksham_daily_quota.reserved_tokens + $2 <= $3
          RETURNING user_id
            """, user_id, amount, self.limit)
            if not row:
                raise HTTPException(429, "Daily hosted token limit reached")
            aggregate = await connection.fetchrow("""
          INSERT INTO saksham_daily_quota (user_id, usage_day, reserved_tokens)
          VALUES ($1, CURRENT_DATE, $2)
          ON CONFLICT (user_id, usage_day) DO UPDATE SET reserved_tokens = saksham_daily_quota.reserved_tokens + $2
          WHERE saksham_daily_quota.used_tokens + saksham_daily_quota.reserved_tokens + $2 <= $3
          RETURNING user_id
            """, self.aggregate_user_id, amount, self.global_limit)
            if not aggregate:
                raise HTTPException(429, "Hosted beta capacity has been reached for today")

    async def settle_tokens(self, user_id: str, reserved: int, actual: int) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            for quota_user_id in (user_id, self.aggregate_user_id):
                await connection.execute("""
          UPDATE saksham_daily_quota SET reserved_tokens = GREATEST(0, reserved_tokens - $2),
          used_tokens = used_tokens + $3 WHERE user_id = $1 AND usage_day = CURRENT_DATE
                """, quota_user_id, reserved, actual)

    async def reserve_meeting(self, user_id: str, seconds: int) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow("""
          INSERT INTO saksham_daily_quota (user_id, usage_day, meeting_seconds)
          VALUES ($1, CURRENT_DATE, $2)
          ON CONFLICT (user_id, usage_day) DO UPDATE SET meeting_seconds = saksham_daily_quota.meeting_seconds + $2
          WHERE saksham_daily_quota.meeting_seconds + $2 <= $3
          RETURNING user_id
            """, user_id, seconds, self.meeting_minutes * 60)
            if not row:
                raise HTTPException(429, "Daily hosted meeting limit reached")
            aggregate = await connection.fetchrow("""
          INSERT INTO saksham_daily_quota (user_id, usage_day, meeting_seconds)
          VALUES ($1, CURRENT_DATE, $2)
          ON CONFLICT (user_id, usage_day) DO UPDATE SET meeting_seconds = saksham_daily_quota.meeting_seconds + $2
          WHERE saksham_daily_quota.meeting_seconds + $2 <= $3
          RETURNING user_id
            """, self.aggregate_user_id, seconds, self.global_meeting_minutes * 60)
            if not aggregate:
                raise HTTPException(429, "Hosted beta meeting capacity has been reached for today")

    async def refund_meeting(self, user_id: str, seconds: int) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            for quota_user_id in (user_id, self.aggregate_user_id):
                await connection.execute("""
          UPDATE saksham_daily_quota SET meeting_seconds = GREATEST(0, meeting_seconds - $2)
          WHERE user_id = $1 AND usage_day = CURRENT_DATE
                """, quota_user_id, seconds)


async def get_user(authorization: str = Header(default="")) -> User:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authentication required")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        key = await asyncio.to_thread(jwks_client(settings().jwks_url).get_signing_key_from_jwt, token)
        claims = jwt.decode(token, key.key, algorithms=[key.algorithm_name], audience="authenticated",
                            issuer=f"{str(settings().supabase_url).rstrip('/')}/auth/v1")
        return User(id=str(claims["sub"]), email=claims.get("email"))
    except Exception as error:
        raise HTTPException(401, "Invalid or expired session") from error


def estimate_tokens(messages: list[ChatMessage]) -> int:
    return max(1, math.ceil(sum(len(message.content) for message in messages) / 4))


def select_byok_provider(provider_url: HttpUrl | None, byok_key: str | None, model: str | None, default_model: str) -> tuple[str, str, str]:
    if not byok_key or not provider_url:
        raise HTTPException(400, "BYOK requires a provider URL and API key")
    parsed = urlparse(str(provider_url))
    if parsed.scheme != "https" or parsed.hostname not in settings().allowed_byok_hosts:
        raise HTTPException(400, "That BYOK provider is not allowed")
    return str(provider_url).rstrip("/"), byok_key, model or default_model


def select_provider(request: ChatRequest, byok_key: str | None) -> tuple[str, str, str]:
    configured = settings()
    if request.mode == "hosted":
        return str(configured.hosted_llm_base_url).rstrip("/"), configured.hosted_llm_api_key, request.model or configured.hosted_llm_model
    return select_byok_provider(request.provider_url, byok_key, request.model, configured.hosted_llm_model)


async def read_limited(upload: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, "Audio file is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def validate_audio_upload(audio: UploadFile, payload: bytes, mode: str) -> int:
    suffix = (audio.filename or "").lower().rsplit(".", 1)
    extension = f".{suffix[-1]}" if len(suffix) == 2 else ""
    if extension not in SUPPORTED_AUDIO_SUFFIXES:
        raise HTTPException(415, "Unsupported audio format")
    if mode != "hosted":
        return 0
    if extension != ".wav":
        raise HTTPException(415, "Hosted meeting transcription currently requires a mono PCM16 WAV file")
    try:
        with wave.open(BytesIO(payload), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise HTTPException(415, "Hosted meeting transcription requires mono PCM16 WAV audio")
            sample_rate = wav.getframerate()
            if sample_rate <= 0:
                raise HTTPException(415, "Hosted meeting transcription requires a valid WAV sample rate")
            seconds = max(1, math.ceil(wav.getnframes() / sample_rate))
    except (EOFError, wave.Error) as error:
        raise HTTPException(415, "Hosted meeting transcription requires a valid WAV file") from error
    if seconds > settings().max_hosted_meeting_upload_seconds:
        raise HTTPException(413, "Hosted meeting recording is too long")
    return seconds


async def lifespan(app: FastAPI):
    configured = settings()
    pool = await asyncpg.create_pool(configured.database_url, min_size=1, max_size=5)
    app.state.quota = QuotaStore(
        pool,
        configured.daily_token_limit,
        configured.daily_global_token_limit,
        configured.daily_meeting_minutes,
        configured.daily_global_meeting_minutes,
    )
    app.state.hosted_gpu_slots = asyncio.BoundedSemaphore(configured.max_concurrent_hosted_jobs)
    await app.state.quota.initialize()
    yield
    await pool.close()


app = FastAPI(title="Saksham Public Gateway", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings().cors_origins, allow_methods=["GET", "POST"],
                   allow_headers=["Authorization", "Content-Type", "X-Saksham-Consent-Confirmed", "X-Saksham-Provider-Key"])


@app.middleware("http")
async def reject_oversized_meeting_request(request, call_next):
    if request.method == "POST" and request.url.path == "/v1/meetings/transcribe":
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                is_too_large = int(content_length) > settings().max_audio_bytes + 65_536
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header"})
            if is_too_large:
                return JSONResponse(status_code=413, content={"detail": "Audio file is too large"})
    return await call_next(request)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat(request: ChatRequest, user: User = Depends(get_user), byok_key: str | None = Header(default=None, alias="X-Saksham-Provider-Key")):
    configured = settings()
    maximum = min(request.max_tokens, configured.max_output_tokens)
    is_hosted = request.mode == "hosted"
    slots: asyncio.BoundedSemaphore | None = None
    reserved = 0
    quota_reserved = False
    actual = 0
    if is_hosted:
        slots = app.state.hosted_gpu_slots
        if slots.locked():
            raise HTTPException(503, "Hosted capacity is busy. Please try again shortly.")
        await slots.acquire()
    try:
        if is_hosted:
            reserved = estimate_tokens(request.messages) + maximum
            await app.state.quota.reserve_tokens(user.id, reserved)
            quota_reserved = True
        base_url, api_key, model = select_provider(request, byok_key)
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
            response = await client.post(f"{base_url}/chat/completions", headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": [message.model_dump() for message in request.messages], "max_tokens": maximum})
        if response.status_code >= 400:
            raise HTTPException(502, "Inference provider rejected the request")
        try:
            payload = response.json()
        except ValueError as error:
            raise HTTPException(502, "Inference provider returned an invalid response") from error
        if not isinstance(payload, dict):
            raise HTTPException(502, "Inference provider returned an invalid response")
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        actual = max(0, int(usage.get("total_tokens") or reserved))
        return {"id": payload.get("id"), "choices": payload.get("choices", []), "usage": usage, "model": payload.get("model", model)}
    except httpx.HTTPError as error:
        raise HTTPException(503, "Inference provider is unavailable") from error
    finally:
        if quota_reserved:
            await app.state.quota.settle_tokens(user.id, reserved, actual)
        if slots:
            slots.release()


@app.post("/v1/meetings/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    mode: Literal["hosted", "byok"] = Form(default="hosted"),
    provider_url: HttpUrl | None = Form(default=None),
    model: str | None = Form(default=None, max_length=160),
    consent_confirmed: bool = Header(default=False, alias="X-Saksham-Consent-Confirmed"),
    user: User = Depends(get_user),
    byok_key: str | None = Header(default=None, alias="X-Saksham-Provider-Key"),
):
    if not consent_confirmed:
        raise HTTPException(400, "Recording consent is required")
    payload = await read_limited(audio, settings().max_audio_bytes)
    hosted_seconds = validate_audio_upload(audio, payload, mode)
    if mode == "byok":
        base_url, api_key, selected_model = select_byok_provider(
            provider_url, byok_key, model, "gpt-4o-mini-transcribe"
        )
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
                response = await client.post(
                    f"{base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (audio.filename or "meeting.webm", payload, audio.content_type or "application/octet-stream")},
                    data={"model": selected_model},
                )
        except httpx.HTTPError as error:
            raise HTTPException(503, "BYOK transcription provider is unavailable") from error
        if response.status_code >= 400:
            raise HTTPException(502, "BYOK transcription provider rejected the request")
        try:
            provider_payload = response.json()
        except ValueError as error:
            raise HTTPException(502, "BYOK transcription provider returned an invalid response") from error
        if not isinstance(provider_payload, dict):
            raise HTTPException(502, "BYOK transcription provider returned an invalid response")
        return {
            "mode": "byok",
            "text": provider_payload.get("text", ""),
            "segments": provider_payload.get("segments", []),
            "language": provider_payload.get("language"),
            "model": selected_model,
        }

    slots: asyncio.BoundedSemaphore = app.state.hosted_gpu_slots
    if slots.locked():
        raise HTTPException(503, "Hosted capacity is busy. Please try again shortly.")
    await slots.acquire()
    quota_reserved = False
    try:
        await app.state.quota.reserve_meeting(user.id, hosted_seconds)
        quota_reserved = True
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
            response = await client.post(f"{str(settings().meeting_worker_url).rstrip('/')}/v1/transcribe",
                headers={"Authorization": f"Bearer {settings().meeting_worker_api_key}"},
                files={"audio": (audio.filename or "meeting.wav", payload, audio.content_type or "audio/wav")},
                data={"final": "true"})
        if response.status_code == 409:
            raise HTTPException(503, "Hosted capacity is busy. Please try again shortly.")
        if response.status_code >= 400:
            raise HTTPException(502, "Meeting worker is unavailable")
        try:
            worker_payload = response.json()
        except ValueError as error:
            raise HTTPException(502, "Meeting worker returned an invalid response") from error
        if not isinstance(worker_payload, dict):
            raise HTTPException(502, "Meeting worker returned an invalid response")
        return {
            "mode": "hosted",
            "segments": worker_payload.get("segments", []),
            "language": worker_payload.get("language"),
            "language_probability": worker_payload.get("language_probability"),
            "model": worker_payload.get("model"),
        }
    except httpx.HTTPError as error:
        if quota_reserved:
            await app.state.quota.refund_meeting(user.id, hosted_seconds)
        raise HTTPException(503, "Hosted meeting worker is unavailable") from error
    except Exception:
        if quota_reserved:
            await app.state.quota.refund_meeting(user.id, hosted_seconds)
        raise
    finally:
        slots.release()
