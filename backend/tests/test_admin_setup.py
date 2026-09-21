import copy
import unittest

from admin_setup import _load_meetings_owner_embedding, _prompt_and_store_pin, enroll


class FakeMeetingStore:
    def __init__(self, profiles):
        self.profiles = profiles
        self.calls = []

    async def list_voice_profiles(self, include_revoked=False, include_embedding=False):
        self.calls.append((include_revoked, include_embedding))
        return self.profiles


class FakeCipher:
    def decrypt(self, ciphertext, nonce):
        if ciphertext != b"owner-cipher" or nonce != b"owner-nonce":
            raise AssertionError("unexpected encrypted profile")
        return [3.0, 4.0]


class FakeAdminStore:
    def __init__(self):
        self.saved = []

    def save(self, embedding, *, model="unknown", retain_previous=False):
        self.saved.append((embedding, model, retain_previous))


class FakePinVerifier:
    def __init__(self, configured=False):
        self.configured = configured
        self.saved = []

    def is_configured(self):
        return self.configured

    def configure(self, pin, *, current_code=None):
        self.saved.append((pin, current_code))


OWNER = {
    "id": "owner-1",
    "display_name": "You",
    "profile_type": "owner",
    "embedding_ciphertext": b"owner-cipher",
    "embedding_nonce": b"owner-nonce",
    "model_name": "pyannote-owner",
    "consent_confirmed": True,
}


class AdminSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_imports_only_active_owner_and_leaves_meetings_record_unchanged(self):
        profiles = [
            {**OWNER, "id": "participant-you", "profile_type": "participant"},
            copy.deepcopy(OWNER),
        ]
        before = copy.deepcopy(profiles)
        store = FakeMeetingStore(profiles)

        embedding, model = await _load_meetings_owner_embedding(
            meeting_store=store,
            meeting_cipher=FakeCipher(),
        )

        self.assertEqual([0.6, 0.8], embedding)
        self.assertEqual("pyannote-owner", model)
        self.assertEqual([(False, True)], store.calls)
        self.assertEqual(before, profiles)

    async def test_rejects_missing_or_ambiguous_owner(self):
        with self.assertRaisesRegex(RuntimeError, "No active"):
            await _load_meetings_owner_embedding(
                meeting_store=FakeMeetingStore([{**OWNER, "profile_type": "participant"}]),
                meeting_cipher=FakeCipher(),
            )
        with self.assertRaisesRegex(RuntimeError, "Multiple active"):
            await _load_meetings_owner_embedding(
                meeting_store=FakeMeetingStore([OWNER, {**OWNER, "id": "owner-2"}]),
                meeting_cipher=FakeCipher(),
            )

    async def test_rejects_owner_without_recorded_consent(self):
        with self.assertRaisesRegex(RuntimeError, "consent"):
            await _load_meetings_owner_embedding(
                meeting_store=FakeMeetingStore([{**OWNER, "consent_confirmed": False}]),
                meeting_cipher=FakeCipher(),
            )

    async def test_enroll_copies_owner_into_isolated_admin_store(self):
        meeting_profiles = [copy.deepcopy(OWNER)]
        admin_store = FakeAdminStore()
        pin_verifier = FakePinVerifier()
        answers = iter(["12345678", "12345678"])

        await enroll(
            [],
            from_meetings_owner=True,
            meeting_store=FakeMeetingStore(meeting_profiles),
            meeting_cipher=FakeCipher(),
            admin_store=admin_store,
            pin_verifier=pin_verifier,
            pin_prompt=lambda _: next(answers),
        )

        self.assertEqual([("12345678", None)], pin_verifier.saved)
        self.assertEqual([([0.6, 0.8], "pyannote-owner", False)], admin_store.saved)
        self.assertEqual(OWNER, meeting_profiles[0])

    def test_pin_replacement_requires_current_pin(self):
        verifier = FakePinVerifier(configured=True)
        answers = iter(["87654321", "12345678", "12345678"])
        _prompt_and_store_pin(verifier=verifier, prompt=lambda _: next(answers))
        self.assertEqual([("12345678", "87654321")], verifier.saved)


if __name__ == "__main__":
    unittest.main()
