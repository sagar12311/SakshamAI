"""Consent-aware encrypted speaker profiles and conservative voice matching."""

from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
from typing import Any, Optional

from config import get_settings
from core.meeting_intelligence_client import MeetingIntelligenceClient
from core.meeting_store import MeetingStore


class VoiceProfileCryptoUnavailable(RuntimeError):
    pass


class VoiceProfileCipher:
    """Uses AES-GCM with a key stored in Keychain or a mode-0600 fallback file."""

    SERVICE_NAME = "ai.saksham.meeting-voiceprints"
    ACCOUNT_NAME = "profile-encryption-key"

    def __init__(self, fallback_path: Optional[Path] = None) -> None:
        settings = get_settings()
        self.fallback_path = fallback_path or settings.saksham_home / "voiceprints.key"
        self._key: Optional[bytes] = None

    def _load_key(self) -> bytes:
        if self._key is not None:
            return self._key

        encoded: Optional[str] = None
        try:
            import keyring

            encoded = keyring.get_password(self.SERVICE_NAME, self.ACCOUNT_NAME)
            if not encoded:
                encoded = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
                keyring.set_password(self.SERVICE_NAME, self.ACCOUNT_NAME, encoded)
        except Exception:
            self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
            if self.fallback_path.exists():
                encoded = self.fallback_path.read_text(encoding="ascii").strip()
            else:
                encoded = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
                self.fallback_path.write_text(encoded, encoding="ascii")
                os.chmod(self.fallback_path, 0o600)

        self._key = base64.urlsafe_b64decode(encoded.encode("ascii"))
        return self._key

    def encrypt(self, values: list[float]) -> tuple[bytes, bytes]:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as error:
            raise VoiceProfileCryptoUnavailable(
                "Install the 'cryptography' dependency to store voice profiles securely"
            ) from error
        nonce = os.urandom(12)
        plaintext = json.dumps(values, separators=(",", ":")).encode("utf-8")
        ciphertext = AESGCM(self._load_key()).encrypt(nonce, plaintext, None)
        return ciphertext, nonce

    def decrypt(self, ciphertext: bytes, nonce: bytes) -> list[float]:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as error:
            raise VoiceProfileCryptoUnavailable(
                "Install the 'cryptography' dependency to use voice profiles"
            ) from error
        plaintext = AESGCM(self._load_key()).decrypt(nonce, ciphertext, None)
        return [float(value) for value in json.loads(plaintext)]


def _normalize(values: list[float]) -> list[float]:
    magnitude = math.sqrt(sum(value * value for value in values))
    if magnitude <= 0:
        raise ValueError("Speaker embedding is empty")
    return [value / magnitude for value in values]


def _centroid(embeddings: list[list[float]]) -> list[float]:
    if not embeddings:
        raise ValueError("At least one speaker embedding is required")
    size = len(embeddings[0])
    if not size or any(len(item) != size for item in embeddings):
        raise ValueError("Speaker embeddings have inconsistent dimensions")
    return _normalize(
        [sum(item[index] for item in embeddings) / len(embeddings) for index in range(size)]
    )


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    left_n = _normalize(left)
    right_n = _normalize(right)
    return sum(a * b for a, b in zip(left_n, right_n))


class VoiceProfileService:
    def __init__(
        self,
        store: MeetingStore,
        intelligence: MeetingIntelligenceClient,
        cipher: Optional[VoiceProfileCipher] = None,
    ) -> None:
        self.store = store
        self.intelligence = intelligence
        self.cipher = cipher or VoiceProfileCipher()
        settings = get_settings()
        self.threshold = settings.voiceprint_match_threshold
        self.margin = settings.voiceprint_match_margin

    async def enroll(
        self,
        display_name: str,
        profile_type: str,
        audio_samples: list[bytes],
        *,
        consent_confirmed: bool,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if profile_type not in {"owner", "participant"}:
            raise ValueError("Voice profile type must be owner or participant")
        if not consent_confirmed:
            raise ValueError("Explicit voice-profile consent is required")
        if not display_name.strip():
            raise ValueError("Voice profile name is required")
        if not audio_samples:
            raise ValueError("Voice enrollment audio is required")

        embeddings: list[list[float]] = []
        total_duration = 0.0
        model_name = "unknown"
        for audio in audio_samples:
            result = await self.intelligence.embed(audio)
            embedding = [float(value) for value in result.get("embedding", [])]
            if embedding:
                embeddings.append(embedding)
            total_duration += float(result.get("duration_seconds", 0.0))
            model_name = str(result.get("model", model_name))
        minimum = 20.0 if profile_type == "owner" else 10.0
        if total_duration and total_duration < minimum:
            raise ValueError(f"Voice enrollment needs at least {minimum:.0f} seconds of clean speech")

        embedding = _centroid(embeddings)
        ciphertext, nonce = self.cipher.encrypt(embedding)
        return await self.store.create_voice_profile(
            {
                "display_name": display_name.strip(),
                "profile_type": profile_type,
                "embedding_ciphertext": ciphertext,
                "embedding_nonce": nonce,
                "model_name": model_name,
                "consent_confirmed": True,
                "metadata": {"sample_count": len(audio_samples), **(metadata or {})},
            }
        )

    async def match(self, audio: bytes, profile_type: Optional[str] = None) -> dict[str, Any]:
        probe_result = await self.intelligence.embed(audio)
        probe = [float(value) for value in probe_result.get("embedding", [])]
        if not probe:
            return {"matched": False, "reason": "no_embedding"}

        profiles = await self.store.list_voice_profiles(include_embedding=True)
        if profile_type:
            profiles = [profile for profile in profiles if profile["profile_type"] == profile_type]
        scores: list[tuple[float, dict[str, Any]]] = []
        for profile in profiles:
            known = self.cipher.decrypt(
                bytes(profile["embedding_ciphertext"]),
                bytes(profile["embedding_nonce"]),
            )
            scores.append((cosine_similarity(probe, known), profile))
        scores.sort(key=lambda item: item[0], reverse=True)
        if not scores:
            return {"matched": False, "reason": "no_enrolled_profile"}

        best_score, best = scores[0]
        second_score = scores[1][0] if len(scores) > 1 else -1.0
        matched = best_score >= self.threshold and (best_score - second_score) >= self.margin
        return {
            "matched": matched,
            "profile_id": best["id"] if matched else None,
            "display_name": best["display_name"] if matched else None,
            "score": round(best_score, 4),
            "margin": round(best_score - second_score, 4),
            "reason": "matched" if matched else "below_conservative_threshold",
        }

    async def verify_owner(self, audio: bytes) -> dict[str, Any]:
        return await self.match(audio, profile_type="owner")
