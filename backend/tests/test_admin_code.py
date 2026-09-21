import unittest
from typing import Dict, Optional, Tuple

from core.admin_code import AdminCodeError, AdminCodeRateLimited, AdminCodeVerifier


class FakeKeyring:
    def __init__(self) -> None:
        self.values: Dict[Tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> Optional[str]:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password


class UnavailableKeyring:
    def get_password(self, service_name: str, username: str) -> Optional[str]:
        raise RuntimeError("Keychain unavailable")


class AdminCodeVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keyring = FakeKeyring()
        self.verifier = AdminCodeVerifier(backend=self.keyring)

    def test_code_is_stored_as_a_verifier_not_plaintext(self) -> None:
        self.verifier.configure("48291736")
        stored = next(iter(self.keyring.values.values()))

        self.assertNotIn("48291736", stored)
        self.assertTrue(self.verifier.is_configured())
        self.assertTrue(self.verifier.verify("48291736"))
        self.assertFalse(self.verifier.verify("00000000"))

    def test_replacing_a_code_requires_the_current_code(self) -> None:
        self.verifier.configure("48291736")
        with self.assertRaises(AdminCodeError):
            self.verifier.configure("13572468")

        self.verifier.configure("13572468", current_code="48291736")
        self.assertTrue(self.verifier.verify("13572468"))

    def test_failed_attempts_are_rate_limited(self) -> None:
        self.verifier.configure("48291736")
        for _ in range(self.verifier.MAX_ATTEMPTS):
            self.assertFalse(self.verifier.verify("00000000"))

        with self.assertRaises(AdminCodeRateLimited):
            self.verifier.verify("48291736")

    def test_keychain_read_error_is_unconfigured_and_fails_closed(self) -> None:
        verifier = AdminCodeVerifier(backend=UnavailableKeyring())
        self.assertFalse(verifier.is_configured())
        self.assertFalse(verifier.verify("48291736"))
