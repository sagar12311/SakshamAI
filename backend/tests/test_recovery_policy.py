import unittest
from unittest.mock import AsyncMock

from agents.executor_agent import ExecutorAgent
from core.recovery_policy import RecoveryPolicy


class RecoveryPolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_music_failure_produces_a_semantically_equivalent_youtube_fallback(self) -> None:
        alternatives = RecoveryPolicy().alternatives(
            {
                "action": "play_music",
                "target": "ambient",
                "parameters": {"genre": "ambient"},
            },
            {"success": False, "error": "No library match"},
        )

        self.assertEqual(len(alternatives), 1)
        self.assertEqual(alternatives[0]["action"], "play_youtube_music")
        self.assertEqual(alternatives[0]["parameters"]["query"], "ambient")

    async def test_executor_uses_fallback_after_unverified_catalog_open(self) -> None:
        executor = ExecutorAgent()
        executor._execute_step = AsyncMock(side_effect=[
            {
                "success": True,
                "action": "play_music",
                "playback_state": "opened_for_user",
                "track": "Ambient Essentials",
            },
            {
                "success": True,
                "action": "play_music",
                "provider": "youtube",
                "playback_state": "playing",
                "track": "Ambient Focus Mix",
            },
        ])

        result, success = await executor._execute_step_with_recovery({
            "action": "play_music",
            "target": "ambient",
            "parameters": {"genre": "ambient"},
        })

        self.assertTrue(success)
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["provider"], "youtube")
        self.assertEqual(len(result["attempts"]), 2)


if __name__ == "__main__":
    unittest.main()
