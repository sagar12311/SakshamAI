"""Public, authenticated edge for Saksham's private home-server services."""

from __future__ import annotations

import asyncio
import json
import math
from datetime import date
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

import asyncpg
import httpx
import jwt
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
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
    max_output_tokens: int = 1_024
    max_audio_bytes: int = 25 * 1024 * 1024
    daily_meeting_minutes: int = 30

    @property
    def jwks_url(self) -> str:
        return str(self.supabase_jwks_url or f"{str(self.supabase_url).rstrip('/')}/auth/v1/.well-known/jwks.json")


@lru_cache
def settings() -> Settings:
    return Settings()


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=12_000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=30)
    mode: Literal["hosted", "byok"] = "hosted"
    model: str | None = Field(default=None, max_length=160)
    provider_url: HttpUrl | None = None
    max_tokens: int = Field(default=512, ge=1, le=1_024)


class User(BaseModel):
    id: str
    email: str | None = None


class QuotaStore:
    def __init__(self, pool: asyncpg.Pool, limit: int, meeting_minutes: int):
        self.pool, self.limit, self.meeting_minutes = pool, limit, meeting_minutes

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
        row = await self.pool.fetchrow("""
          INSERT INTO saksham_daily_quota (user_id, usage_day, reserved_tokens)
          VALUES ($1, CURRENT_DATE, $2)
          ON CONFLICT (user_id, usage_day) DO UPDATE SET reserved_tokens = saksham_daily_quota.reserved_tokens + $2
          WHERE saksham_daily_quota.used_tokens + saksham_daily_quota.reserved_tokens + $2 <= $3
          RETURNING user_id
        """, user_id, amount, self.limit)
        if not row:
            raise HTTPException(429, "Daily hosted token limit reached")

    async def settle_tokens(self, user_id: str, reserved: int, actual: int) -> None:
        await self.pool.execute("""
          UPDATE saksham_daily_quota SET reserved_tokens = GREATEST(0, reserved_tokens - $2),
          used_tokens = used_tokens + $3 WHERE user_id = $1 AND usage_day = CURRENT_DATE
        """, user_id, reserved, actual)

    async def reserve_meeting(self, user_id: str, seconds: int) -> None:
        row = await self.pool.fetchrow("""
          INSERT INTO saksham_daily_quota (user_id, usage_day, meeting_seconds)
          VALUES ($1, CURRENT_DATE, $2)
          ON CONFLICT (user_id, usage_day) DO UPDATE SET meeting_seconds = saksham_daily_quota.meeting_seconds + $2
          WHERE saksham_daily_quota.meeting_seconds + $2 <= $3
          RETURNING user_id
        """, user_id, seconds, self.meeting_minutes * 60)
        if not row:
            raise HTTPException(429, "Daily hosted meeting limit reached")


async def get_user(authorization: str = Header(default="")) -> User:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authentication required")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        client = jwt.PyJWKClient(settings().jwks_url, cache_keys=True)
        key = await asyncio.to_thread(client.get_signing_key_from_jwt, token)
        claims = jwt.decode(token, key.key, algorithms=[key.algorithm_name], audience="authenticated",
                            issuer=f"{str(settings().supabase_url).rstrip('/')}/auth/v1")
        return User(id=str(claims["sub"]), email=claims.get("email"))
    except Exception as error:
        raise HTTPException(401, "Invalid or expired session") from error


def estimate_tokens(messages: list[ChatMessage]) -> int:
    return max(1, math.ceil(sum(len(message.content) for message in messages) / 4))


def select_provider(request: ChatRequest, byok_key: str | None) -> tuple[str, str, str]:
    configured = settings()
    if request.mode == "hosted":
        return str(configured.hosted_llm_base_url).rstrip("/"), configured.hosted_llm_api_key, request.model or configured.hosted_llm_model
    if not byok_key or not request.provider_url:
        raise HTTPException(400, "BYOK requires a provider URL and API key")
    parsed = urlparse(str(request.provider_url))
    if parsed.scheme != "https" or parsed.hostname not in configured.allowed_byok_hosts:
        raise HTTPException(400, "That BYOK provider is not allowed")
    return str(request.provider_url).rstrip("/"), byok_key, request.model or configured.hosted_llm_model


async def read_limited(upload: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, "Audio file is too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def lifespan(app: FastAPI):
    configured = settings()
    pool = await asyncpg.create_pool(configured.database_url, min_size=1, max_size=5)
    app.state.quota = QuotaStore(pool, configured.daily_token_limit, configured.daily_meeting_minutes)
    await app.state.quota.initialize()
    yield
    await pool.close()


app = FastAPI(title="Saksham Public Gateway", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings().cors_origins, allow_methods=["GET", "POST"],
                   allow_headers=["Authorization", "Content-Type", "X-Saksham-Provider-Key"])


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat(request: ChatRequest, user: User = Depends(get_user), byok_key: str | None = Header(default=None, alias="X-Saksham-Provider-Key")):
    configured = settings()
    maximum = min(request.max_tokens, configured.max_output_tokens)
    reserved = estimate_tokens(request.messages) + maximum
    await app.state.quota.reserve_tokens(user.id, reserved)
    try:
        base_url, api_key, model = select_provider(request, byok_key)
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
            response = await client.post(f"{base_url}/chat/completions", headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": [message.model_dump() for message in request.messages], "max_tokens": maximum})
        if response.status_code >= 400:
            raise HTTPException(502, "Inference provider rejected the request")
        payload = response.json()
        usage = payload.get("usage") or {}
        actual = int(usage.get("total_tokens") or reserved)
        return {"id": payload.get("id"), "choices": payload.get("choices", []), "usage": usage, "model": payload.get("model", model)}
    finally:
        await app.state.quota.settle_tokens(user.id, reserved, actual if 'actual' in locals() else reserved)


@app.post("/v1/meetings/transcribe")
async def transcribe(audio: UploadFile = File(...), consent_confirmed: bool = Header(default=False), user: User = Depends(get_user)):
    if not consent_confirmed:
        raise HTTPException(400, "Recording consent is required")
    payload = await read_limited(audio, settings().max_audio_bytes)
    # A conservative upload-rate estimate prevents unbounded GPU use before worker processing.
    seconds = max(1, math.ceil(len(payload) / 32_000))
    await app.state.quota.reserve_meeting(user.id, seconds)
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
        response = await client.post(f"{str(settings().meeting_worker_url).rstrip('/')}/v1/transcribe",
            headers={"Authorization": f"Bearer {settings().meeting_worker_api_key}"},
            files={"audio": (audio.filename or "meeting.wav", payload, audio.content_type or "audio/wav")},
            data={"final": "true"})
    if response.status_code >= 400:
        raise HTTPException(502, "Meeting worker is unavailable")
    return response.json()
