import sqlite3
import tempfile
import unittest
import wave
from pathlib import Path

from core.meeting_audio import AudioSequenceGap, MeetingAudioRecorder
from core.meeting_store import MeetingStore


class MeetingStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = MeetingStore(self.root / "saksham.db")
        await self.store.initialize()
        self.project = await self.store.create_project(
            "Example Client",
            "example-client",
            self.root / "projects" / "example-client",
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def create_meeting(self):
        meeting_id = "meeting-1"
        audio_path = self.root / "private" / meeting_id / "audio.wav"
        audio_path.parent.mkdir(parents=True)
        return await self.store.create_meeting(
            self.project["id"],
            "Discovery call",
            16000,
            audio_path,
            meeting_id=meeting_id,
        )

    async def test_migration_and_meeting_crud(self) -> None:
        meeting = await self.create_meeting()

        self.assertEqual("recording", meeting["capture_state"])
        self.assertEqual("silent_monitoring", meeting["assistant_state"])
        self.assertTrue(meeting["consent_confirmed"])
        self.assertEqual(1, len(await self.store.list_meetings()))

        updated = await self.store.update_meeting(
            meeting["id"],
            capture_state="processing",
            assistant_state="silent_monitoring",
        )
        self.assertEqual("processing", updated["capture_state"])

    async def test_second_migration_upgrades_early_transcript_schema(self) -> None:
        legacy_path = self.root / "legacy.db"
        with sqlite3.connect(legacy_path) as db:
            db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)")
            db.execute("INSERT INTO schema_migrations VALUES (1, '2026-01-01T00:00:00+00:00')")
            db.execute(
                """
                CREATE TABLE meeting_transcript_segments(
                    id TEXT PRIMARY KEY,
                    meeting_id TEXT NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    speaker_label TEXT NOT NULL DEFAULT 'Speaker 1'
                )
                """
            )
        legacy = MeetingStore(legacy_path)
        await legacy.initialize()

        with sqlite3.connect(legacy_path) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(meeting_transcript_segments)")}
            versions = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
        self.assertIn("speaker_cluster", columns)
        self.assertEqual({1, 2, 3, 4}, versions)

    async def test_private_console_and_consent_fields_round_trip(self) -> None:
        meeting = await self.store.create_meeting(
            self.project["id"],
            "Consented call",
            16000,
            self.root / "private" / "consented" / "audio.wav",
            expected_speaker_count=3,
            voice_memory_consent_confirmed=True,
            voice_memory_consent_scope="all_participants",
        )
        message = await self.store.add_private_message(
            meeting["id"],
            {
                "role": "user",
                "command_type": "speaker_mapping",
                "status": "received",
                "text": "Speaker 1 is Joshua",
                "anchor_ms": 1500,
            },
        )
        pending = await self.store.upsert_pending_speaker_mapping(
            meeting["id"],
            1,
            "Joshua",
        )

        self.assertEqual(3, meeting["expected_speaker_count"])
        self.assertTrue(meeting["voice_memory_consent_confirmed"])
        self.assertEqual(message["id"], (await self.store.list_private_messages(meeting["id"]))[0]["id"])
        self.assertEqual("Joshua", pending["display_name"])

    async def test_audio_frames_are_idempotent_and_gaps_are_rejected(self) -> None:
        meeting = await self.create_meeting()
        recorder = MeetingAudioRecorder(self.store)
        frame = b"\x01\x00" * 16000

        first = await recorder.append_frame(meeting["id"], 0, frame)
        duplicate = await recorder.append_frame(meeting["id"], 0, frame)
        self.assertEqual(0, first["ack"])
        self.assertEqual(first, duplicate)

        with self.assertRaises(AudioSequenceGap) as raised:
            await recorder.append_frame(meeting["id"], 2, frame)
        self.assertEqual(1, raised.exception.expected)

        await recorder.append_frame(meeting["id"], 1, frame)
        wav_path = await recorder.finalize(meeting["id"])
        with wave.open(str(wav_path), "rb") as wav:
            self.assertEqual(16000, wav.getframerate())
            self.assertEqual(32000, wav.getnframes())

    async def test_uncommitted_audio_tail_is_truncated(self) -> None:
        meeting = await self.create_meeting()
        recorder = MeetingAudioRecorder(self.store)
        raw_path = Path(meeting["audio_path"]).with_suffix(".pcm")
        raw_path.write_bytes(b"\x00\x00" * 100)

        result = await recorder.append_frame(meeting["id"], 0, b"\x02\x00" * 10)

        self.assertEqual(10, result["total_samples"])
        self.assertEqual(20, raw_path.stat().st_size)

    async def test_transcript_pointer_and_mom_round_trip(self) -> None:
        meeting = await self.create_meeting()
        segment = await self.store.add_segment(
            meeting["id"],
            {
                "start_ms": 1000,
                "end_ms": 3000,
                "text": "We will send the proposal tomorrow.",
                "words": [
                    {
                        "start_ms": 1000,
                        "end_ms": 1300,
                        "word": " We",
                        "probability": 0.95,
                    }
                ],
                "speaker_cluster": "SPEAKER_00",
                "speaker_label": "Speaker 1",
            },
        )
        pointer = await self.store.add_pointer(
            meeting["id"],
            {
                "category": "action",
                "content": "Send the proposal tomorrow.",
                "start_ms": 1000,
                "end_ms": 3000,
                "source_segment_ids": [segment["id"]],
            },
        )
        mom = await self.store.add_mom_revision(meeting["id"], "# MOM\n", {"actions": []})

        self.assertEqual([segment["id"]], pointer["source_segment_ids"])
        stored_segments = await self.store.list_segments(meeting["id"])
        self.assertEqual(1, len(stored_segments))
        self.assertEqual(" We", stored_segments[0]["words"][0]["word"])
        self.assertEqual(1, len(await self.store.list_pointers(meeting["id"])))
        self.assertEqual(mom["id"], (await self.store.get_latest_mom(meeting["id"]))["id"])

    async def test_processing_jobs_record_stage_failures(self) -> None:
        meeting = await self.create_meeting()
        job = await self.store.create_processing_job(
            meeting["id"],
            "speaker_diarization",
            state="running",
        )
        updated = await self.store.update_processing_job(
            job["id"],
            "failed",
            provider="local-worker",
            error="model unavailable",
        )

        self.assertEqual("failed", updated["state"])
        self.assertEqual("model unavailable", updated["error"])
        jobs = await self.store.list_processing_jobs(meeting["id"])
        self.assertEqual([job["id"]], [item["id"] for item in jobs])


if __name__ == "__main__":
    unittest.main()
