import unittest
from unittest.mock import AsyncMock

from agents.executor_agent import ExecutorAgent
from core.cognition_bus import CognitionMessage, MessageType


class ExecutorSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.executor = ExecutorAgent()

    def test_adds_current_year_to_time_sensitive_recommendation(self) -> None:
        query = self.executor._add_search_freshness(
            "best LLM to run on RTX 5060 Ti"
        )

        self.assertIn("current", query)
        self.assertRegex(query, r"\b20\d{2}\b")

    def test_preserves_explicit_search_year(self) -> None:
        query = self.executor._add_search_freshness(
            "best local LLM benchmarks 2025"
        )

        self.assertEqual("best local LLM benchmarks 2025", query)

    def test_web_source_attribution_strips_query_and_fragment(self) -> None:
        source = self.executor._safe_web_source_url(
            "https://example.test/research?access_token=secret#instructions"
        )

        self.assertEqual("https://example.test/research", source)
        self.assertEqual("", self.executor._safe_web_source_url("file:///etc/passwd"))

    def test_web_text_is_bounded_before_synthesis(self) -> None:
        bounded = self.executor._bounded_web_text("x" * 200, 32)

        self.assertLessEqual(len(bounded), 32)
        self.assertTrue(bounded.endswith("…"))

    async def test_preserves_memory_context_through_execution(self) -> None:
        result = await self.executor.think(CognitionMessage(
            type=MessageType.TASK_START,
            payload={
                "plan": [],
                "intent": "recommend a model",
                "conversation_id": "test-conversation",
                "memory_context": {"memories": [{"content": "Uses GPT-OSS 20B"}]},
            },
            source="test",
        ))

        self.assertEqual(
            "Uses GPT-OSS 20B",
            result["memory_context"]["memories"][0]["content"],
        )

    async def test_music_failure_marks_the_task_as_failed(self) -> None:
        class UnavailableMusic:
            async def play_genre(self, genre: str) -> dict:
                return {
                    "success": False,
                    "action": "play_music",
                    "error": f"No {genre} tracks found",
                }

        self.executor._music_controller = UnavailableMusic()
        self.executor.broadcast = AsyncMock()

        execution = await self.executor._execute_plan({
            "execution_id": "music-test",
            "intent": "Play jazz music",
            "plan": [{
                "action": "play_music",
                "target": "jazz",
                "parameters": {"genre": "jazz"},
            }],
        })

        self.assertFalse(execution["success"])
        self.assertFalse(execution["results"][0]["success"])


if __name__ == "__main__":
    unittest.main()
