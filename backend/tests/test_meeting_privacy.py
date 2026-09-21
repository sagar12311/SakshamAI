import unittest
from unittest.mock import AsyncMock, patch

from core.cognition_bus import CognitionMessage, MessageType
from main import handle_audio_chunk


class MeetingAudioPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_meeting_planner_chunks_never_reach_tts(self) -> None:
        message = CognitionMessage(
            type=MessageType.AGENT_AUDIO_CHUNK,
            payload={
                "text": "This must remain in the private console.",
                "conversation_id": "meeting:test-meeting",
            },
            source="planner",
        )

        with patch("main.voice_processor.text_to_speech", new=AsyncMock()) as synthesize:
            await handle_audio_chunk(message)

        synthesize.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
