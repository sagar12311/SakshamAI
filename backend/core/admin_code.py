"""Local, Keychain-backed verifier for Saksham admin activation codes.

The admin code is never stored in SQLite, configuration, chat history, or a
prompt. This service stores only a salted PBKDF2 verifier in the macOS
Keychain. It deliberately does not grant execution privileges by itself: a
future activation flow must also provide live voice-verification evidence.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import re
import time
from typing import Any, Dict, Optional, Protocol

import keyring


class KeyringBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> Optional[str]: ...
    def set_password(self, service_name: str, username: str, password: str) -> None: ...


class AdminCodeError(ValueError):
    pass


class AdminCodeRateLimited(AdminCodeError):
    pass


class AdminCodeVerifier:
    SERVICE_NAME = "ai.saksham.admin-activation"
    ACCOUNT_NAME = "admin-code-v1"
    MIN_LENGTH = 8
    MAX_LENGTH = 12
    MAX_ATTEMPTS = 3
    LOCK_SECONDS = 60
    PBKDF2_ITERATIONS = 600_000

    def __init__(self, backend: Optional[KeyringBackend] = None) -> None:
        self._backend = backend or keyring
        self._failed_attempts = 0
        self._locked_until = 0.0

    def is_configured(self) -> bool:
        try:
            return self._backend.get_password(self.SERVICE_NAME, self.ACCOUNT_NAME) is not None
        except Exception:
            # Status/readiness must fail closed rather than dumping a Keychain
            # traceback in a headless shell. Enrollment still surfaces a write
            # failure directly to the user.
            return False

    def configure(self, code: str, *, current_code: Optional[str] = None) -> None:
        self._validate_code(code)
        if self.is_configured() and not (current_code and self.verify(current_code)):
            raise AdminCodeError("The current admin code is required to replace it")

        salt = secrets.token_bytes(16)
        record = {
            "version": 1,
            "kdf": "pbkdf2_sha256",
            "iterations": self.PBKDF2_ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
            "digest": base64.b64encode(self._derive(code, salt)).decode("ascii"),
        }
        self._backend.set_password(self.SERVICE_NAME, self.ACCOUNT_NAME, json.dumps(record))
        self._failed_attempts = 0
        self._locked_until = 0.0

    def verify(self, code: str) -> bool:
        if time.monotonic() < self._locked_until:
            raise AdminCodeRateLimited("Too many failed admin-code attempts. Try again shortly.")

        record = self._load_record()
        if not record or not isinstance(code, str):
            return self._record_failure()
        try:
            if record.get("kdf") != "pbkdf2_sha256":
                return self._record_failure()
            salt = base64.b64decode(record["salt"], validate=True)
            expected = base64.b64decode(record["digest"], validate=True)
        except (KeyError, ValueError, TypeError):
            return self._record_failure()

        verified = hmac.compare_digest(self._derive(code, salt), expected)
        if verified:
            self._failed_attempts = 0
            self._locked_until = 0.0
            return True
        return self._record_failure()

    @property
    def retry_after_seconds(self) -> int:
        return max(0, int(self._locked_until - time.monotonic()))

    def _load_record(self) -> Optional[Dict[str, Any]]:
        try:
            raw = self._backend.get_password(self.SERVICE_NAME, self.ACCOUNT_NAME)
        except Exception:
            return None
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) and parsed.get("version") == 1 else None

    def _record_failure(self) -> bool:
        self._failed_attempts += 1
        if self._failed_attempts >= self.MAX_ATTEMPTS:
            self._locked_until = time.monotonic() + self.LOCK_SECONDS
            self._failed_attempts = 0
        return False

    @classmethod
    def _derive(cls, code: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256",
            code.encode("utf-8"),
            salt,
            cls.PBKDF2_ITERATIONS,
            dklen=32,
        )

    @classmethod
    def _validate_code(cls, code: str) -> None:
        if (
            not isinstance(code, str)
            or not (cls.MIN_LENGTH <= len(code) <= cls.MAX_LENGTH)
            or re.fullmatch(r"[0-9]+", code) is None
        ):
            raise AdminCodeError(
                f"Admin PIN must be {cls.MIN_LENGTH}-{cls.MAX_LENGTH} digits long"
            )


_admin_code_verifier = AdminCodeVerifier()


def get_admin_code_verifier() -> AdminCodeVerifier:
    return _admin_code_verifier
