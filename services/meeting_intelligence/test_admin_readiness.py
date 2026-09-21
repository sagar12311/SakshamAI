"""Readiness tests for the RTX-only admin verification path."""

import unittest
from unittest.mock import patch

import app


class AdminReadinessTests(unittest.TestCase):
    def test_readiness_requires_transcript_speaker_and_liveness_models(self):
        models = app.Models()
        with patch.object(app, "ADMIN_VOICE_AUTH_ENABLED", True), patch.object(
            app, "module_available", return_value=True
        ), patch.object(models, "ensure_capacity") as capacity, patch.object(
            models, "get_asr"
        ) as asr, patch.object(models, "get_embedding") as embedding, patch.object(
            models, "get_admin_antispoof"
        ) as antispoof:
            self.assertTrue(models.admin_voice_ready())

        capacity.assert_called_once()
        asr.assert_called_once_with(app.DEFAULT_ASR_MODEL)
        embedding.assert_called_once()
        antispoof.assert_called_once()
        self.assertEqual("", models.admin_voice_error)

    def test_any_model_readiness_failure_fails_closed(self):
        models = app.Models()
        with patch.object(app, "ADMIN_VOICE_AUTH_ENABLED", True), patch.object(
            app, "module_available", return_value=True
        ), patch.object(models, "ensure_capacity"), patch.object(
            models, "get_asr", side_effect=RuntimeError("unavailable")
        ):
            self.assertFalse(models.admin_voice_ready())
        self.assertEqual("RuntimeError", models.admin_voice_error)


if __name__ == "__main__":
    unittest.main()
