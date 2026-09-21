import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.admin_auth import (
    AdminAuthService,
    AdminVoiceProfileStore,
    configure_admin_voice_verifier,
)


class FakeCodeVerifier:
    def __init__(self, accepted="12345678"):
        self.accepted = accepted

    def verify(self, value):
        return value == self.accepted

    def is_configured(self):
        return True


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value


class FakeProfileStore:
    def __init__(self):
        self.embedding = [1.0, 0.0]
        self.saved = []

    def load(self):
        return list(self.embedding)

    def configured(self):
        return True

    def save(self, embedding, *, model="unknown", retain_previous=False):
        self.saved.append((embedding, model, retain_previous))
        self.embedding = list(embedding)


class FakeVoiceVerifier:
    def __init__(self, *, speaker=0.98, liveness=0.99, include_embedding=True):
        self.phrase = ""
        self.speaker = speaker
        self.liveness = liveness
        self.include_embedding = include_embedding

    async def verify(self, audio, **kwargs):
        result = {
            "transcript": self.phrase + " one two three four five six seven eight",
            "speaker_score": self.speaker,
            "liveness_score": self.liveness,
            "required_speaker_score": 0.82,
            "required_liveness_score": 0.90,
            "model_version": "test-worker",
        }
        if self.include_embedding:
            result["speaker_embedding"] = [0.99, 0.01]
        return result

    async def health(self):
        return {"status": "healthy", "capabilities": {"admin_voice_auth": True}}


class UnavailableVoiceVerifier:
    async def health(self):
        raise RuntimeError("network unavailable")


class AdminAuthAdaptationTests(unittest.IsolatedAsyncioTestCase):
    async def _attempt(self, voice, code=None):
        profile = FakeProfileStore()
        service = AdminAuthService(
            code_verifier=code or FakeCodeVerifier(),
            voice_verifier=voice,
            profile_store=profile,
        )
        challenge = await service.start_challenge("task", "approval", "digest", conversation_id="voice")
        voice.phrase = challenge["phrase"]
        result = await service.consume_audio(
            challenge["challenge_id"], b"wav",
            task_id="task", approval_id="approval", conversation_id="voice",
        )
        return result, profile

    async def test_strong_fully_verified_ceremony_adapts_profile(self):
        result, profile = await self._attempt(FakeVoiceVerifier())
        self.assertTrue(result["verified"])
        self.assertTrue(result["voice_profile_adapted"])
        self.assertEqual(1, len(profile.saved))
        self.assertTrue(profile.saved[0][2])

    async def test_weak_but_acceptable_proof_does_not_adapt(self):
        result, profile = await self._attempt(FakeVoiceVerifier(speaker=0.85, liveness=0.92))
        self.assertTrue(result["verified"])
        self.assertFalse(result["voice_profile_adapted"])
        self.assertEqual([], profile.saved)

    async def test_failed_pin_never_adapts(self):
        result, profile = await self._attempt(
            FakeVoiceVerifier(),
            code=FakeCodeVerifier(accepted="87654321"),
        )
        self.assertFalse(result["verified"])
        self.assertEqual([], profile.saved)

    async def test_missing_probe_embedding_never_adapts(self):
        result, profile = await self._attempt(FakeVoiceVerifier(include_embedding=False))
        self.assertTrue(result["verified"])
        self.assertFalse(result["voice_profile_adapted"])
        self.assertEqual([], profile.saved)

    def test_profile_store_retains_and_rolls_back_previous_version(self):
        store = AdminVoiceProfileStore(backend=FakeKeyring())
        store.save([1.0, 0.0], model="initial")
        store.save([0.9, 0.1], model="adapted", retain_previous=True)
        self.assertEqual([0.9, 0.1], store.load())
        self.assertTrue(store.rollback())
        self.assertEqual([1.0, 0.0], store.load())

    async def test_status_reports_a_sanitized_worker_failure_reason(self):
        service = AdminAuthService(
            code_verifier=FakeCodeVerifier(),
            voice_verifier=UnavailableVoiceVerifier(),
            profile_store=FakeProfileStore(),
        )
        status = await service.status()
        self.assertFalse(status["worker_ready"])
        self.assertEqual("worker_runtimeerror", status["worker_reason"])

    def test_cli_configuration_creates_a_private_rtx_client(self):
        service = AdminAuthService(code_verifier=FakeCodeVerifier(), profile_store=FakeProfileStore())
        settings = SimpleNamespace(
            admin_voice_enabled=True,
            admin_voice_worker_url="http://100.64.0.10:8110",
            meeting_intelligence_remote_url=None,
            meeting_intelligence_api_key=SimpleNamespace(get_secret_value=lambda: "x" * 32),
        )
        with patch("config.get_settings", return_value=settings):
            self.assertTrue(configure_admin_voice_verifier(service))
        self.assertIsNotNone(service.voice_verifier)


if __name__ == "__main__":
    unittest.main()
