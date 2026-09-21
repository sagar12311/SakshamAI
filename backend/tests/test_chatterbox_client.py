import json
import unittest

import httpx

from core.chatterbox_client import ChatterboxTTSClient


class ChatterboxTTSClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_authenticated_openai_style_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual("Bearer secret-token", request.headers["Authorization"])
            payload = json.loads(request.content)
            self.assertEqual("A short answer.", payload["input"])
            self.assertEqual("saksham", payload["voice"])
            self.assertEqual("reassuring", payload["tone"])
            return httpx.Response(200, content=b"RIFFtest-wave-data")

        client = ChatterboxTTSClient(
            "http://tts.internal:8100/",
            api_key="secret-token",
            transport=httpx.MockTransport(handler),
        )

        audio = await client.synthesize("A short answer.", tone="reassuring")

        self.assertTrue(audio.startswith(b"RIFF"))

    async def test_rejects_non_wav_response(self) -> None:
        client = ChatterboxTTSClient(
            "http://tts.internal:8100",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"not audio")
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "invalid WAV"):
            await client.synthesize("Hello")

    async def test_reports_service_error_without_hiding_detail(self) -> None:
        client = ChatterboxTTSClient(
            "http://tts.internal:8100",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(503, json={"detail": "model unavailable"})
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "model unavailable"):
            await client.synthesize("Hello")


if __name__ == "__main__":
    unittest.main()
