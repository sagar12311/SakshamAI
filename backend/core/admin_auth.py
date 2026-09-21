"""Hands-free admin activation for dangerous Saksham tasks.

The service deliberately keeps challenges and capabilities process-local.  A
restart invalidates every pending activation, while the persistent factors
(PIN verifier and encrypted voice profile) remain in their dedicated secure
stores.  Raw audio and PINs are never written to disk or logs.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import httpx

from core.admin_code import AdminCodeRateLimited, AdminCodeVerifier, get_admin_code_verifier


_CHALLENGE_TTL_SECONDS = 30
_CAPABILITY_TTL_SECONDS = 60
_MAX_FAILURES = 3
_COOLDOWN_SECONDS = 60
_ADMIN_CAPABILITY_VERSION = 1
_ADMIN_CAPABILITY_PURPOSE = "dangerous_plan"
_ADMIN_CAPABILITY_KEY = secrets.token_bytes(32)
_CONSUMED_NONCES: dict[str, int] = {}
_NONCE_LOCK = threading.Lock()

_CHALLENGE_WORDS = (
    "amber", "anchor", "apricot", "atlas", "beacon", "birch", "cobalt", "comet",
    "copper", "cricket", "dawn", "delta", "ember", "fable", "falcon", "forest",
    "galaxy", "harbor", "hazel", "indigo", "jasmine", "lagoon", "maple", "meadow",
    "meteor", "mint", "nebula", "olive", "orbit", "pebble", "quartz", "raven",
    "river", "saffron", "sierra", "silver", "spruce", "sunset", "thunder", "violet",
)
_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_PIN_FILLER_WORDS = {"my", "pin", "is", "number", "the", "code", "admin", "please"}


class AdminVoiceVerifier(Protocol):
    async def verify(
        self,
        audio: bytes,
        *,
        challenge_id: str,
        reference_embedding: Optional[list[float]] = None,
    ) -> dict[str, Any]: ...

    async def embed(self, audio: bytes) -> dict[str, Any]: ...

    async def health(self) -> dict[str, Any]: ...


class PrivateRTXVoiceVerifier:
    """Authenticated client for the private worker's admin verification API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout_seconds: float = 12.0,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Admin voice worker URL must be an HTTP(S) URL")
        hostname = parsed.hostname or ""
        try:
            address = ipaddress.ip_address(hostname)
            # Tailscale commonly uses CGNAT space (100.64.0.0/10), which
            # Python reports as neither ``is_private`` nor ``is_global``.
            if not (address.is_private or address.is_loopback or address.is_link_local or not address.is_global):
                raise ValueError("Admin voice worker must use a private-network address")
        except ValueError:
            # Hostnames are accepted for private DNS/Tailscale deployments;
            # public URLs are rejected by deployment configuration and health.
            if "." in hostname and not hostname.endswith((".local", ".internal")):
                raise ValueError("Admin voice worker hostname must be private DNS")
        if not api_key or len(api_key) < 24:
            raise ValueError("Admin voice worker API key is not securely configured")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def health(self) -> dict[str, Any]:
        timeout = httpx.Timeout(5.0, connect=3.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            response = await client.get(f"{self.base_url}/health", headers=self.headers)
            response.raise_for_status()
            return response.json()

    async def verify(
        self,
        audio: bytes,
        *,
        challenge_id: str,
        reference_embedding: Optional[list[float]] = None,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(self.timeout_seconds, connect=3.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                f"{self.base_url}/v1/admin/verify",
                headers=self.headers,
                files={"audio": ("admin-activation.wav", audio, "audio/wav")},
                data={
                    "challenge_id": challenge_id,
                    "reference_embedding": json.dumps(reference_embedding or []),
                },
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}

    async def embed(self, audio: bytes) -> dict[str, Any]:
        timeout = httpx.Timeout(self.timeout_seconds, connect=3.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                f"{self.base_url}/v1/embed",
                headers=self.headers,
                files={"audio": ("admin-enrollment.wav", audio, "audio/wav")},
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}


class AdminVoiceProfileStore:
    """Encrypt the admin-only reference embedding in a dedicated Keychain slot."""

    SERVICE_NAME = "ai.saksham.admin-voiceprints"
    ACCOUNT_NAME = "admin-profile-v1"

    def __init__(self, backend: Any = None) -> None:
        self._backend = backend
        self._key: Optional[bytes] = None

    def _key_bytes(self) -> bytes:
        if self._key is not None:
            return self._key
        import base64
        import keyring

        backend = self._backend or keyring
        encoded = backend.get_password(self.SERVICE_NAME, "encryption-key")
        if not encoded:
            encoded = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
            backend.set_password(self.SERVICE_NAME, "encryption-key", encoded)
        self._key = base64.urlsafe_b64decode(encoded.encode("ascii"))
        return self._key

    def configured(self) -> bool:
        import keyring
        backend = self._backend or keyring
        try:
            return backend.get_password(self.SERVICE_NAME, self.ACCOUNT_NAME) is not None
        except Exception:
            # Keychain access can be unavailable to a detached/headless shell.
            # Never treat that uncertainty as configured.
            return False

    def _decrypt_payload(self, raw: str) -> Optional[dict[str, Any]]:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import base64
        try:
            record = json.loads(raw)
            plaintext = AESGCM(self._key_bytes()).decrypt(
                base64.b64decode(record["nonce"], validate=True),
                base64.b64decode(record["ciphertext"], validate=True),
                None,
            )
            values = json.loads(plaintext)
            return values if isinstance(values, dict) else None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _encrypt_payload(self, payload: dict[str, Any]) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import base64
        import keyring

        backend = self._backend or keyring
        nonce = secrets.token_bytes(12)
        plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ciphertext = AESGCM(self._key_bytes()).encrypt(nonce, plaintext, None)
        backend.set_password(self.SERVICE_NAME, self.ACCOUNT_NAME, json.dumps({
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }))

    def save(
        self,
        embedding: list[float],
        *,
        model: str = "unknown",
        retain_previous: bool = False,
    ) -> None:
        if not embedding:
            raise ValueError("Admin voice embedding is empty")
        current = self.load_payload() if retain_previous else None
        history: list[dict[str, Any]] = []
        version = 1
        if current:
            version = int(current.get("version") or 1) + 1
            prior_embedding = current.get("embedding")
            if isinstance(prior_embedding, list) and prior_embedding:
                history.append({"embedding": prior_embedding, "model": current.get("model", "unknown")})
            for item in current.get("history") or []:
                if isinstance(item, dict) and isinstance(item.get("embedding"), list):
                    history.append(item)
        self._encrypt_payload({
            "embedding": [float(value) for value in embedding],
            "model": str(model or "unknown"),
            "version": version,
            "history": history[:3],
        })

    def load_payload(self) -> Optional[dict[str, Any]]:
        import keyring
        backend = self._backend or keyring
        raw = backend.get_password(self.SERVICE_NAME, self.ACCOUNT_NAME)
        return self._decrypt_payload(raw) if raw else None

    def load(self) -> Optional[list[float]]:
        values = self.load_payload()
        embedding = values.get("embedding") if values else None
        return [float(value) for value in embedding] if isinstance(embedding, list) else None

    def rollback(self) -> bool:
        current = self.load_payload()
        history = list(current.get("history") or []) if current else []
        if not history:
            return False
        previous = history.pop(0)
        self._encrypt_payload({
            "embedding": previous["embedding"],
            "model": previous.get("model", "unknown"),
            "version": max(1, int(current.get("version") or 2) - 1),
            "history": history,
        })
        return True

    def revoke(self) -> None:
        import keyring
        backend = self._backend or keyring
        delete_password = getattr(backend, "delete_password", None)
        if delete_password:
            try:
                delete_password(self.SERVICE_NAME, self.ACCOUNT_NAME)
            except Exception:
                pass


@dataclass
class _Challenge:
    challenge_id: str
    task_id: str
    approval_id: str
    conversation_id: str
    plan_digest: str
    phrase_hash: str
    phrase: str
    issued_at: int
    expires_at: int
    status: str = "issued"


def _normalize_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").casefold())


def _extract_pin(transcript: str, phrase: str) -> Optional[str]:
    tokens = _normalize_tokens(transcript)
    phrase_tokens = _normalize_tokens(phrase)
    if len(tokens) < len(phrase_tokens) or tokens[: len(phrase_tokens)] != phrase_tokens:
        return None
    trailing = tokens[len(phrase_tokens) :]
    digits: list[str] = []
    for token in trailing:
        if token in _DIGIT_WORDS:
            digits.append(_DIGIT_WORDS[token])
            continue
        if token.isdigit() and len(token) == 1:
            digits.append(token)
            continue
        if token in _PIN_FILLER_WORDS:
            continue
        return None
    if not 8 <= len(digits) <= 12:
        return None
    return "".join(digits)


def _plan_digest(plan: Any) -> Optional[str]:
    try:
        encoded = json.dumps(
            plan,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _normalize_embedding(values: list[float]) -> list[float]:
    magnitude = sum(value * value for value in values) ** 0.5
    if magnitude <= 0:
        raise ValueError("Speaker embedding is empty")
    return [value / magnitude for value in values]


def _embedding_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return -1.0
    left_n = _normalize_embedding(left)
    right_n = _normalize_embedding(right)
    return sum(a * b for a, b in zip(left_n, right_n))


def issue_admin_capability(
    *,
    task_id: str,
    approval_id: str,
    conversation_id: str,
    plan_digest: str,
) -> dict[str, Any]:
    issued_at = int(time.time())
    fields = {
        "version": _ADMIN_CAPABILITY_VERSION,
        "purpose": _ADMIN_CAPABILITY_PURPOSE,
        "admin_verified": True,
        "task_id": str(task_id),
        "approval_id": str(approval_id),
        "conversation_id": str(conversation_id or "default"),
        "plan_digest": str(plan_digest),
        "issued_at": issued_at,
        "expires_at": issued_at + _CAPABILITY_TTL_SECONDS,
        "nonce": secrets.token_urlsafe(24),
    }
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return {**fields, "signature": hmac.new(_ADMIN_CAPABILITY_KEY, encoded, hashlib.sha256).hexdigest()}


def consume_admin_capability(
    capability: Any,
    *,
    plan: Any,
    task_id: Optional[str],
    approval_id: Optional[str],
    conversation_id: Optional[str],
    consume: bool = True,
) -> tuple[bool, str]:
    if not isinstance(capability, dict):
        return False, "Dangerous plans require live admin activation"
    digest = _plan_digest(plan)
    if not digest:
        return False, "Dangerous plan is not safely serializable"
    required = {
        "version", "purpose", "admin_verified", "task_id", "approval_id",
        "conversation_id", "plan_digest", "issued_at", "expires_at", "nonce", "signature",
    }
    if not required.issubset(capability):
        return False, "Admin capability is incomplete"
    try:
        fields = {key: capability[key] for key in required if key != "signature"}
        fields["version"] = int(fields["version"])
        fields["issued_at"] = int(fields["issued_at"])
        fields["expires_at"] = int(fields["expires_at"])
        fields["admin_verified"] = bool(fields["admin_verified"])
        supplied_signature = str(capability["signature"])
        encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    except (TypeError, ValueError, KeyError):
        return False, "Admin capability is malformed"
    if fields["version"] != _ADMIN_CAPABILITY_VERSION or fields["purpose"] != _ADMIN_CAPABILITY_PURPOSE:
        return False, "Admin capability has an unsupported purpose"
    if not fields["admin_verified"]:
        return False, "Admin capability is not verified"
    if str(fields["task_id"]) != str(task_id or "") or str(fields["approval_id"]) != str(approval_id or ""):
        return False, "Admin capability belongs to a different task approval"
    if str(fields["conversation_id"]) != str(conversation_id or "default"):
        return False, "Admin capability belongs to a different conversation"
    if str(fields["plan_digest"]) != digest:
        return False, "Plan changed after admin activation"
    now = int(time.time())
    if fields["issued_at"] > now + 30 or fields["expires_at"] <= now:
        return False, "Admin capability has expired"
    if fields["expires_at"] - fields["issued_at"] > _CAPABILITY_TTL_SECONDS:
        return False, "Admin capability has an invalid lifetime"
    expected = hmac.new(_ADMIN_CAPABILITY_KEY, encoded, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected):
        return False, "Admin capability is not trusted"
    nonce = str(fields["nonce"])
    with _NONCE_LOCK:
        for used, expiry in list(_CONSUMED_NONCES.items()):
            if expiry <= now:
                _CONSUMED_NONCES.pop(used, None)
        if nonce in _CONSUMED_NONCES:
            return False, "Admin capability was already used"
        if consume:
            _CONSUMED_NONCES[nonce] = fields["expires_at"]
    return True, "Verified live admin activation"


class AdminAuthService:
    """Coordinates dynamic challenge, local PIN verification, and voice proof."""

    def __init__(
        self,
        *,
        code_verifier: Optional[AdminCodeVerifier] = None,
        voice_verifier: Optional[AdminVoiceVerifier] = None,
        profile_store: Optional[AdminVoiceProfileStore] = None,
        clock: Any = time.time,
    ) -> None:
        self.code_verifier = code_verifier or get_admin_code_verifier()
        self.voice_verifier = voice_verifier
        self.profile_store = profile_store or AdminVoiceProfileStore()
        self._clock = clock
        self._challenges: dict[str, _Challenge] = {}
        self._failed_attempts = 0
        self._locked_until = 0.0

    @property
    def cooldown_seconds(self) -> int:
        return max(0, int(self._locked_until - float(self._clock())))

    def _record_failure(self) -> None:
        self._failed_attempts += 1
        if self._failed_attempts >= _MAX_FAILURES:
            self._locked_until = float(self._clock()) + _COOLDOWN_SECONDS
            self._failed_attempts = 0

    def _adapt_verified_profile(self, proof: dict[str, Any], current: list[float]) -> bool:
        """Apply a small update only for unusually strong, fully verified proofs."""
        probe_raw = proof.get("speaker_embedding")
        if not isinstance(probe_raw, list) or len(probe_raw) != len(current):
            return False
        try:
            probe = [float(value) for value in probe_raw]
            speaker_score = float(proof.get("speaker_score") or 0.0)
            liveness_score = float(proof.get("liveness_score") or 0.0)
            if speaker_score < 0.90 or liveness_score < 0.95:
                return False
            if _embedding_similarity(current, probe) < 0.90:
                return False
            weight = 0.05
            adapted = _normalize_embedding([
                ((1.0 - weight) * old) + (weight * new)
                for old, new in zip(_normalize_embedding(current), _normalize_embedding(probe))
            ])
            self.profile_store.save(
                adapted,
                model=str(proof.get("model_version") or "private-rtx-adapted"),
                retain_previous=True,
            )
            return True
        except (TypeError, ValueError):
            return False

    async def start_challenge(
        self,
        task_id: str,
        approval_id: str,
        plan_digest: str,
        *,
        conversation_id: str = "default",
    ) -> dict[str, Any]:
        if not task_id or not approval_id or not plan_digest:
            raise ValueError("Admin activation requires a task, approval, and plan digest")
        if float(self._clock()) < self._locked_until:
            raise RuntimeError("Admin activation is cooling down")
        challenge_id = secrets.token_urlsafe(18)
        phrase = " ".join(secrets.choice(_CHALLENGE_WORDS) for _ in range(5))
        now = int(self._clock())
        record = _Challenge(
            challenge_id=challenge_id,
            task_id=str(task_id),
            approval_id=str(approval_id),
            conversation_id=str(conversation_id or "default"),
            plan_digest=str(plan_digest),
            phrase_hash=hashlib.sha256(phrase.encode("utf-8")).hexdigest(),
            phrase=phrase,
            issued_at=now,
            expires_at=now + _CHALLENGE_TTL_SECONDS,
        )
        self._challenges[challenge_id] = record
        return {
            "challenge_id": challenge_id,
            "prompt": "Admin activation. Repeat the phrase, then speak your PIN.",
            "phrase": phrase,
            "expires_at": record.expires_at,
            "status": "challenge_issued",
        }

    async def consume_audio(
        self,
        challenge_id: str,
        audio: bytes,
        *,
        task_id: str,
        approval_id: str,
        conversation_id: str = "default",
    ) -> dict[str, Any]:
        record = self._challenges.get(str(challenge_id))
        now = int(self._clock())
        if not record or record.status != "issued":
            return {"verified": False, "reason": "Unknown or already used admin challenge"}
        if now >= record.expires_at:
            record.status = "expired"
            return {"verified": False, "reason": "Admin challenge expired"}
        if (
            record.task_id != str(task_id)
            or record.approval_id != str(approval_id)
            or record.conversation_id != str(conversation_id or "default")
        ):
            record.status = "rejected"
            return {"verified": False, "reason": "Admin challenge belongs to a different task"}
        if not isinstance(audio, bytes) or not audio:
            self._record_failure()
            return {"verified": False, "reason": "Admin activation audio was empty", "cooldown_seconds": self.cooldown_seconds}
        if self.voice_verifier is None:
            return {"verified": False, "reason": "Admin voice verification is unavailable"}
        reference_embedding = self.profile_store.load()
        if not reference_embedding:
            return {"verified": False, "reason": "Admin voice profile is not enrolled"}
        try:
            proof = await self.voice_verifier.verify(
                audio,
                challenge_id=record.challenge_id,
                reference_embedding=reference_embedding,
            )
        except Exception:
            return {"verified": False, "reason": "Admin voice verification is unavailable"}
        transcript = str(proof.get("transcript") or "")
        pin = _extract_pin(transcript, record.phrase)
        speaker_score = float(proof.get("speaker_score") or 0.0)
        liveness_score = float(proof.get("liveness_score") or 0.0)
        speaker_threshold = float(proof.get("required_speaker_score") or 0.82)
        liveness_threshold = float(proof.get("required_liveness_score") or 0.90)
        if not pin or speaker_score < speaker_threshold or liveness_score < liveness_threshold:
            self._record_failure()
            record.status = "rejected" if self.cooldown_seconds else "issued"
            return {
                "verified": False,
                "reason": "Admin voice, liveness, challenge, or PIN verification failed",
                "cooldown_seconds": self.cooldown_seconds,
            }
        try:
            code_ok = self.code_verifier.verify(pin)
        except AdminCodeRateLimited:
            record.status = "rejected"
            return {"verified": False, "reason": "Admin PIN verification is cooling down", "cooldown_seconds": self.cooldown_seconds}
        if not code_ok:
            self._record_failure()
            record.status = "rejected" if self.cooldown_seconds else "issued"
            return {"verified": False, "reason": "Admin voice, liveness, challenge, or PIN verification failed", "cooldown_seconds": self.cooldown_seconds}
        record.status = "verified"
        self._failed_attempts = 0
        profile_adapted = self._adapt_verified_profile(proof, reference_embedding)
        capability = issue_admin_capability(
            task_id=record.task_id,
            approval_id=record.approval_id,
            conversation_id=record.conversation_id,
            plan_digest=record.plan_digest,
        )
        return {"verified": True, "capability": capability, "expires_at": capability["expires_at"],
                "task_id": record.task_id, "approval_id": record.approval_id,
                "conversation_id": record.conversation_id,
                "voice_profile_adapted": profile_adapted}

    def active_challenge(self, conversation_id: str = "default") -> Optional[dict[str, str]]:
        now = int(self._clock())
        for record in list(self._challenges.values()):
            if record.status == "issued" and record.conversation_id == str(conversation_id or "default"):
                if now < record.expires_at:
                    return {"challenge_id": record.challenge_id, "task_id": record.task_id,
                            "approval_id": record.approval_id, "conversation_id": record.conversation_id}
                record.status = "expired"
        return None

    async def consume_active_audio(self, audio: bytes, *, conversation_id: str = "default") -> Optional[dict[str, Any]]:
        active = self.active_challenge(conversation_id)
        if not active:
            return None
        return await self.consume_audio(
            active["challenge_id"], audio,
            task_id=active["task_id"], approval_id=active["approval_id"],
            conversation_id=active["conversation_id"],
        )

    async def cancel(self, challenge_id: str) -> dict[str, Any]:
        record = self._challenges.get(str(challenge_id))
        if not record:
            return {"cancelled": False, "reason": "Unknown admin challenge"}
        record.status = "cancelled"
        return {"cancelled": True}

    async def status(self) -> dict[str, Any]:
        worker = {"ready": False, "reason": "not_configured"}
        if self.voice_verifier is not None:
            try:
                health = await self.voice_verifier.health()
                capabilities = health.get("capabilities") or {}
                worker = {
                    "ready": bool(
                        health.get("status") == "healthy"
                        and capabilities.get("admin_voice_auth")
                    ),
                    "capabilities": capabilities,
                    "reason": (
                        None
                        if health.get("status") == "healthy" and capabilities.get("admin_voice_auth")
                        else "admin_voice_capability_unavailable"
                    ),
                }
            except Exception as error:
                # Keep the CLI useful for deployment troubleshooting without
                # leaking endpoint credentials or response bodies.
                worker = {"ready": False, "reason": f"worker_{type(error).__name__.lower()}"}
        configured = bool(self.code_verifier.is_configured())
        voice_profile_configured = bool(self.profile_store.configured())
        return {
            "configured": configured,
            "voice_profile_configured": voice_profile_configured,
            "worker_ready": worker.get("ready", False),
            "activation_available": configured and voice_profile_configured and worker.get("ready", False) and self.cooldown_seconds == 0,
            "cooldown_seconds": self.cooldown_seconds,
            "worker_reason": worker.get("reason"),
        }


_admin_auth_service = AdminAuthService()


def get_admin_auth_service() -> AdminAuthService:
    return _admin_auth_service


def configure_admin_voice_verifier(
    service: Optional[AdminAuthService] = None,
) -> bool:
    """Attach the private RTX verifier from local settings when configured.

    The FastAPI lifespan and the standalone ``admin_setup status`` command run
    in different processes.  Keeping this setup in one function prevents the
    CLI from incorrectly reporting a healthy remote worker as unavailable.
    """
    from config import get_settings

    target = service or get_admin_auth_service()
    settings = get_settings()
    if not settings.admin_voice_enabled:
        target.voice_verifier = None
        return False
    worker_url = settings.admin_voice_worker_url or settings.meeting_intelligence_remote_url
    worker_key = settings.meeting_intelligence_api_key.get_secret_value() if settings.meeting_intelligence_api_key else ""
    if not worker_url or not worker_key:
        target.voice_verifier = None
        return False
    try:
        target.voice_verifier = PrivateRTXVoiceVerifier(worker_url, worker_key)
        return True
    except ValueError:
        target.voice_verifier = None
        return False
