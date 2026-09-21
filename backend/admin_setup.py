"""Headless setup for Saksham's dangerous-task admin factor.

Run from ``backend/`` over a trusted local SSH session. Secrets are entered
without echo and voice samples are sent only to the configured private worker.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import math
from pathlib import Path

from config import get_settings
from core.admin_auth import (
    AdminVoiceProfileStore,
    PrivateRTXVoiceVerifier,
    configure_admin_voice_verifier,
    get_admin_auth_service,
)
from core.admin_code import AdminCodeVerifier, get_admin_code_verifier
from core.meeting_store import MeetingStore
from core.voice_profiles import VoiceProfileCipher


def _centroid(vectors: list[list[float]]) -> list[float]:
    width = len(vectors[0])
    values = [sum(vector[i] for vector in vectors) / len(vectors) for i in range(width)]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def _prompt_and_store_pin(*, verifier=None, prompt=getpass.getpass) -> None:
    verifier = verifier or get_admin_code_verifier()
    current_pin = None
    if verifier.is_configured():
        current_pin = prompt("Current admin PIN: ")
    pin = prompt("New 8-12 digit admin PIN: ")
    if pin != prompt("Repeat admin PIN: "):
        raise RuntimeError("PINs did not match")
    AdminCodeVerifier._validate_code(pin)
    verifier.configure(pin, current_code=current_pin)


async def _load_meetings_owner_embedding(
    *,
    meeting_store=None,
    meeting_cipher=None,
) -> tuple[list[float], str]:
    store = meeting_store or MeetingStore()
    cipher = meeting_cipher or VoiceProfileCipher()
    profiles = await store.list_voice_profiles(
        include_revoked=False,
        include_embedding=True,
    )
    owners = [profile for profile in profiles if profile.get("profile_type") == "owner"]
    if not owners:
        raise RuntimeError("No active Meetings owner voice profile was found")
    if len(owners) > 1:
        raise RuntimeError("Multiple active Meetings owner profiles were found; revoke the obsolete profile first")
    owner = owners[0]
    if not owner.get("consent_confirmed"):
        raise RuntimeError("The Meetings owner profile does not have recorded consent")
    embedding = cipher.decrypt(
        bytes(owner["embedding_ciphertext"]),
        bytes(owner["embedding_nonce"]),
    )
    return _centroid([[float(value) for value in embedding]]), str(owner.get("model_name") or "unknown")


async def enroll(
    samples: list[Path],
    *,
    from_meetings_owner: bool = False,
    voice_verifier=None,
    meeting_store=None,
    meeting_cipher=None,
    admin_store=None,
    pin_verifier=None,
    pin_prompt=getpass.getpass,
) -> None:
    settings = get_settings()
    if from_meetings_owner:
        if samples:
            raise RuntimeError("Choose either --from-meetings-owner or --sample, not both")
        embedding, model = await _load_meetings_owner_embedding(
            meeting_store=meeting_store,
            meeting_cipher=meeting_cipher,
        )
    else:
        if not samples:
            raise RuntimeError("Provide voice samples or use --from-meetings-owner")
        if voice_verifier is None:
            url = settings.admin_voice_worker_url or settings.meeting_intelligence_remote_url
            key = settings.meeting_intelligence_api_key.get_secret_value() if settings.meeting_intelligence_api_key else ""
            if not url or not key:
                raise RuntimeError("Set ADMIN_VOICE_WORKER_URL and MEETING_INTELLIGENCE_API_KEY first")
            voice_verifier = PrivateRTXVoiceVerifier(url, key)
        vectors: list[list[float]] = []
        model = "private-rtx"
        for sample in samples:
            if not sample.is_file():
                raise RuntimeError(f"Voice sample does not exist: {sample}")
            result = await voice_verifier.embed(sample.read_bytes())
            vector = result.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise RuntimeError(f"Worker returned no embedding for {sample.name}")
            vectors.append([float(value) for value in vector])
            model = str(result.get("model") or model)
        if len({len(vector) for vector in vectors}) != 1:
            raise RuntimeError("Voice samples produced different embedding sizes")
        embedding = _centroid(vectors)

    # Validate/store the PIN before creating the isolated admin copy. A bad
    # or mismatched PIN therefore cannot leave a partially enrolled profile.
    _prompt_and_store_pin(verifier=pin_verifier, prompt=pin_prompt)
    (admin_store or AdminVoiceProfileStore()).save(embedding, model=model)
    print("Admin voice profile and PIN enrolled. No raw voice or PIN was stored.")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Configure hands-free Saksham admin activation")
    sub = parser.add_subparsers(dest="command", required=True)
    enroll_parser = sub.add_parser("enroll", help="Enroll from Meetings owner profile or WAV samples")
    enrollment_source = enroll_parser.add_mutually_exclusive_group(required=True)
    enrollment_source.add_argument("--sample", action="append", help="Local WAV path (repeat 3-5 times)")
    enrollment_source.add_argument("--from-meetings-owner", action="store_true", help="Import the active Meetings owner profile")
    sub.add_parser("set-pin", help="Set or replace only the hidden numeric PIN")
    sub.add_parser("status", help="Show readiness without exposing secrets")
    sub.add_parser("revoke", help="Revoke the enrolled voice profile")
    args = parser.parse_args()
    if args.command == "enroll":
        await enroll(
            [Path(path).expanduser() for path in (args.sample or [])],
            from_meetings_owner=bool(args.from_meetings_owner),
        )
    elif args.command == "set-pin":
        _prompt_and_store_pin()
        print("Admin PIN updated in macOS Keychain.")
    elif args.command == "revoke":
        AdminVoiceProfileStore().revoke()
        print("Admin voice profile revoked; dangerous actions are blocked.")
    else:
        configure_admin_voice_verifier()
        print(await get_admin_auth_service().status())


if __name__ == "__main__":
    asyncio.run(main())
