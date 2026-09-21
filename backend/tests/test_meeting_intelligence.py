import importlib.util
import tempfile
import unittest
import wave
from pathlib import Path

import httpx
from fastapi import HTTPException

from core.meeting_intelligence_client import MeetingIntelligenceClient
from api.meetings import parse_byte_range


class MeetingIntelligenceClientTests(unittest.IsolatedAsyncioTestCase):
    def test_audio_byte_ranges_support_seek_and_reject_invalid_requests(self) -> None:
        self.assertEqual((0, 999, 200), parse_byte_range(None, 1000))
        self.assertEqual((100, 199, 206), parse_byte_range("bytes=100-199", 1000))
        self.assertEqual((900, 999, 206), parse_byte_range("bytes=-100", 1000))
        with self.assertRaises(ValueError):
            parse_byte_range("bytes=1000-1200", 1000)

    async def test_diarization_sends_exact_speaker_count(self) -> None:
        bodies = []

        async def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(await request.aread())
            return httpx.Response(200, json={"turns": [], "speaker_count": 2})

        client = MeetingIntelligenceClient(
            remote_url="http://rtx-worker:8110",
            local_url="",
            transport=httpx.MockTransport(handler),
        )
        await client.diarize(b"RIFF-test", num_speakers=2)

        self.assertIn(b'num_speakers', bodies[0])
        self.assertIn(b'2', bodies[0])
    async def test_busy_rtx_falls_back_to_small_local_final_model(self) -> None:
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            body = await request.aread()
            requests.append((request, body))
            if request.url.host == "rtx-worker":
                return httpx.Response(503, json={"detail": "GPU busy"})
            return httpx.Response(
                200,
                json={"segments": [], "model": "small", "device": "cpu"},
            )

        client = MeetingIntelligenceClient(
            remote_url="http://rtx-worker:8110",
            local_url="http://mac-worker:8110",
            api_key="shared-secret",
            transport=httpx.MockTransport(handler),
        )
        result = await client.transcribe(b"RIFF-test", final=True)

        self.assertEqual("http://mac-worker:8110", result["provider"])
        self.assertEqual(["rtx-worker", "mac-worker"], [item[0].url.host for item in requests])
        self.assertIn(b"large-v3-turbo", requests[0][1])
        self.assertIn(b"small", requests[1][1])
        self.assertEqual("Bearer shared-secret", requests[1][0].headers["Authorization"])

    async def test_health_reports_each_configured_worker(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200 if request.url.host == "mac-worker" else 503,
                json={"status": "healthy" if request.url.host == "mac-worker" else "busy"},
            )

        client = MeetingIntelligenceClient(
            remote_url="http://rtx-worker:8110",
            local_url="http://mac-worker:8110",
            transport=httpx.MockTransport(handler),
        )
        health = await client.health()

        self.assertTrue(health["available"])
        self.assertEqual([False, True], [worker["ok"] for worker in health["workers"]])


class MeetingIntelligenceSidecarSecurityTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app_path = Path(__file__).resolve().parents[2] / "services" / "meeting_intelligence" / "app.py"
        spec = importlib.util.spec_from_file_location("saksham_meeting_sidecar", app_path)
        if not spec or not spec.loader:
            raise RuntimeError("Could not load meeting intelligence sidecar")
        cls.sidecar = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.sidecar)

    async def test_sidecar_fails_closed_without_a_secure_api_key(self) -> None:
        original = self.sidecar.API_KEY
        try:
            self.sidecar.API_KEY = ""
            with self.assertRaises(HTTPException) as missing:
                await self.sidecar.authorize(None)
            self.assertEqual(503, missing.exception.status_code)

            self.sidecar.API_KEY = "a" * 32
            with self.assertRaises(HTTPException) as wrong:
                await self.sidecar.authorize("Bearer wrong")
            self.assertEqual(401, wrong.exception.status_code)
            await self.sidecar.authorize("Bearer " + "a" * 32)
        finally:
            self.sidecar.API_KEY = original

    def test_pcm16_waveform_loading_does_not_require_torchcodec(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            with wave.open(audio.name, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\x00\x80\x00\x00\xff\x7f")

            decoded = self.sidecar.load_waveform(audio.name)

        self.assertEqual(16000, decoded["sample_rate"])
        self.assertEqual((1, 3), tuple(decoded["waveform"].shape))
        self.assertAlmostEqual(-1.0, decoded["waveform"][0, 0].item())
        self.assertAlmostEqual(0.0, decoded["waveform"][0, 1].item())
        self.assertAlmostEqual(32767 / 32768, decoded["waveform"][0, 2].item())


if __name__ == "__main__":
    unittest.main()
