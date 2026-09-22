from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
import wave
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import ec
import jwt

os.environ.update({
    "SUPABASE_URL": "https://project.supabase.co",
    "DATABASE_URL": "postgresql://gateway:password@postgres:5432/gateway",
    "HOSTED_LLM_BASE_URL": "http://host.docker.internal:1234/v1",
    "HOSTED_LLM_API_KEY": "local-test-key",
    "MEETING_WORKER_URL": "http://host.docker.internal:8110",
    "MEETING_WORKER_API_KEY": "worker-test-key",
})

from app import main


def wav_payload(*, channels: int = 1, sample_width: int = 2, sample_rate: int = 16_000, frames: int = 16_000) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00" * frames * channels * sample_width)
    return output.getvalue()


def upload(name: str, payload: bytes) -> UploadFile:
    return UploadFile(filename=name, file=BytesIO(payload))


def test_hosted_audio_must_be_mono_pcm16_wav() -> None:
    payload = wav_payload()
    assert main.validate_audio_upload(upload("meeting.wav", payload), payload, "hosted") == 1

    stereo = wav_payload(channels=2)
    with pytest.raises(HTTPException, match="mono PCM16"):
        main.validate_audio_upload(upload("meeting.wav", stereo), stereo, "hosted")

    with pytest.raises(HTTPException, match="requires a mono PCM16 WAV"):
        main.validate_audio_upload(upload("meeting.webm", b"not a wav"), b"not a wav", "hosted")


def test_hosted_audio_length_is_limited() -> None:
    payload = wav_payload(sample_rate=1, frames=901)
    with pytest.raises(HTTPException, match="too long"):
        main.validate_audio_upload(upload("meeting.wav", payload), payload, "hosted")


def test_byok_transcription_accepts_supported_audio_without_hosted_wav_rules() -> None:
    assert main.validate_audio_upload(upload("meeting.webm", b"provider handles this"), b"provider handles this", "byok") == 0


def test_browser_preflight_allows_the_required_private_headers() -> None:
    client = TestClient(main.app)
    response = client.options("/v1/meetings/transcribe", headers={
        "Origin": "https://saksham.ai",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,x-saksham-consent-confirmed,x-saksham-provider-key",
    })
    assert response.status_code == 200
    assert "x-saksham-consent-confirmed" in response.headers["access-control-allow-headers"].lower()


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, *_args, **_kwargs) -> FakeResponse:
        return self.response


def test_byok_chat_does_not_touch_hosted_quota(monkeypatch) -> None:
    class Quota:
        async def reserve_tokens(self, *_):
            raise AssertionError("BYOK must not reserve hosted quota")

        async def settle_tokens(self, *_):
            raise AssertionError("BYOK must not settle hosted quota")

    monkeypatch.setattr(main.app.state, "quota", Quota(), raising=False)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **_: FakeClient(FakeResponse(200, {"choices": [], "usage": {}})))
    request = main.ChatRequest(messages=[main.ChatMessage(role="user", content="hello")], mode="byok", provider_url="https://api.openai.com/v1")

    result = asyncio.run(main.chat(request, main.User(id="user-1"), "session-only-key"))
    assert result["choices"] == []


def test_byok_meeting_upload_uses_the_authenticated_gateway_contract(monkeypatch) -> None:
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **_: FakeClient(FakeResponse(200, {"text": "Hello team.", "language": "en"})))
    main.app.dependency_overrides[main.get_user] = lambda: main.User(id="user-1")
    client = TestClient(main.app)
    try:
        response = client.post("/v1/meetings/transcribe", headers={
            "X-Saksham-Consent-Confirmed": "true",
            "X-Saksham-Provider-Key": "session-only-key",
        }, data={
            "mode": "byok",
            "provider_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini-transcribe",
        }, files={"audio": ("meeting.webm", b"provider handles this", "audio/webm")})
    finally:
        client.close()
        main.app.dependency_overrides.pop(main.get_user, None)

    assert response.status_code == 200
    assert response.json() == {
        "mode": "byok",
        "text": "Hello team.",
        "segments": [],
        "language": "en",
        "model": "gpt-4o-mini-transcribe",
    }


def test_failed_hosted_chat_refunds_its_reservation_and_releases_capacity(monkeypatch) -> None:
    monkeypatch.setattr(main.settings(), "hosted_enabled", True)
    class Quota:
        def __init__(self):
            self.calls: list[tuple[str, int]] = []

        async def reserve_tokens(self, _user: str, amount: int):
            self.calls.append(("reserve", amount))

        async def settle_tokens(self, _user: str, _reserved: int, actual: int):
            self.calls.append(("settle", actual))

    quota = Quota()
    slots = asyncio.BoundedSemaphore(1)
    monkeypatch.setattr(main.app.state, "quota", quota, raising=False)
    monkeypatch.setattr(main.app.state, "hosted_gpu_slots", slots, raising=False)
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **_: FakeClient(FakeResponse(500, {})))
    request = main.ChatRequest(messages=[main.ChatMessage(role="user", content="hello")])

    with pytest.raises(HTTPException, match="Inference provider rejected"):
        asyncio.run(main.chat(request, main.User(id="user-1"), None))
    assert quota.calls[0][0] == "reserve"
    assert quota.calls[-1] == ("settle", 0)
    assert not slots.locked()


def test_hosted_disabled_cannot_select_a_gpu(monkeypatch):
    monkeypatch.setattr(main.settings(), "hosted_enabled", False)
    request = main.ChatRequest(messages=[main.ChatMessage(role="user", content="hello")])
    with pytest.raises(HTTPException, match="not enabled"):
        asyncio.run(main.chat(request, main.User(id="user-1"), None))


def test_hosted_model_is_operator_selected():
    request = main.ChatRequest(messages=[main.ChatMessage(role="user", content="hello")], model="unapproved-large-model")
    assert main.select_provider(request, None)[2] == main.settings().hosted_llm_model


def test_token_reservation_accounts_for_multibyte_text():
    content = "नमस्ते 🌍"
    messages = [main.ChatMessage(role="user", content=content)]
    assert main.estimate_tokens(messages) >= len(content.encode("utf-8"))


@pytest.mark.parametrize("url", [
    "https://user:password@api.openai.com/v1",
    "https://api.openai.com:8443/v1",
    "https://api.openai.com/v1?target=private",
    "https://api.openai.com/v1#fragment",
    "http://api.openai.com/v1",
    "https://127.0.0.1/v1",
])
def test_byok_rejects_unsafe_provider_urls(url):
    request = main.ChatRequest(messages=[main.ChatMessage(role="user", content="hello")], mode="byok", provider_url=url)
    with pytest.raises(HTTPException, match="not allowed"):
        main.select_provider(request, "session-only-key")


def test_first_request_cannot_bypass_quota():
    quota = main.QuotaStore(None, 100, 200, 1, 2)
    with pytest.raises(HTTPException, match="exceeds"):
        asyncio.run(quota.reserve_tokens("new-user", 101))
    with pytest.raises(HTTPException, match="exceeds"):
        asyncio.run(quota.reserve_meeting("new-user", 61))


def test_owner_can_bypass_daily_quota_without_bypassing_other_guards():
    quota = main.QuotaStore(None, 100, 200, 1, 2, ["owner-user"])
    asyncio.run(quota.reserve_tokens("owner-user", 10_000))
    asyncio.run(quota.settle_tokens("owner-user", 10_000, 10_000))
    asyncio.run(quota.reserve_meeting("owner-user", 10_000))
    asyncio.run(quota.refund_meeting("owner-user", 10_000))


@pytest.mark.parametrize("invited,anonymous,role,expected", [
    (True, False, "authenticated", 200),
    (False, False, "authenticated", 403),
    (True, True, "authenticated", 401),
    (True, False, "service_role", 401),
])
def test_signed_sessions_require_registered_invited_user(monkeypatch, invited, anonymous, role, expected):
    private_key = ec.generate_private_key(ec.SECP256R1())
    lookup = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private_key.public_key()))
    monkeypatch.setattr(main, "jwks_client", lambda _: lookup)
    monkeypatch.setattr(main.settings(), "beta_invite_only", True)
    monkeypatch.setattr(main.settings(), "beta_user_ids", ["test-user"] if invited else [])
    claims = {
        "sub": "test-user", "role": role, "is_anonymous": anonymous,
        "aud": "authenticated", "iss": "https://project.supabase.co/auth/v1",
        "iat": int(time.time()), "exp": int(time.time()) + 60,
    }
    token = jwt.encode(claims, private_key, algorithm="ES256")
    if expected == 200:
        assert asyncio.run(main.get_user(f"Bearer {token}")).id == "test-user"
    else:
        with pytest.raises(HTTPException) as result:
            asyncio.run(main.get_user(f"Bearer {token}"))
        assert result.value.status_code == expected
