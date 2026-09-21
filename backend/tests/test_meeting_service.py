import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.meeting_minutes import (
    MeetingMinutesGenerator,
    normalize_deadline_phrase,
    parse_json_object,
)
from core.meeting_service import (
    MeetingCaptureBusy,
    MeetingService,
    align_speakers,
    redact_private_ranges,
    sanitize_project_slug,
    subtract_time_ranges,
    trim_transcript_overlap,
)
from core.meeting_store import MeetingStore
from core.voice_profiles import VoiceProfileCipher, VoiceProfileService, cosine_similarity


class FakeLLM:
    def __init__(self, response):
        self.response = response

    async def complete(self, **_kwargs):
        return {
            "choices": [
                {"message": {"content": json.dumps(self.response)}}
            ]
        }


class BoundedMinutesLLM:
    """Rejects oversized prompts and emits source aliases like the local model."""

    def __init__(self):
        self.prompts = []

    async def complete(self, *, messages, **_kwargs):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        if len(prompt) >= 15_000:
            raise AssertionError(f"MOM prompt is too large: {len(prompt)} characters")
        return {
            "choices": [{"message": {"content": json.dumps({
                "attendees": ["Sagar", "Client"],
                "summary": {
                    "content": "The scope, delivery risk, and follow-up were discussed.",
                    "source_segment_ids": ["s1"],
                },
                "discussion_points": [{
                    "content": "The requested scope was reviewed.",
                    "source_segment_ids": ["s1"],
                }],
                "decisions": [{
                    "content": "The pilot approach was approved.",
                    "source_segment_ids": ["s1"],
                }],
                "actions": [{
                    "action": "Send the pilot proposal",
                    "owner": "Sagar",
                    "deadline": "Not specified",
                    "deadline_original": "",
                    "priority": "Not specified",
                    "inferred_fields": [],
                    "inference_reason": "",
                    "status": "open",
                    "source_segment_ids": ["s1"],
                }],
                "risks": [{
                    "content": "The dependency may delay delivery.",
                    "source_segment_ids": ["s1"],
                }],
                "open_questions": [{
                    "content": "Who will confirm the dependency date?",
                    "source_segment_ids": ["s1"],
                }],
                "next_steps": [{
                    "content": "Review the pilot proposal next week.",
                    "source_segment_ids": ["s1"],
                }],
            })}}]
        }


class FakeIntelligence:
    def __init__(self, embeddings=None, segments=None):
        self.embeddings = list(embeddings or [])
        self.segments = list(segments or [])

    async def embed(self, _audio):
        embedding = self.embeddings.pop(0)
        return {
            "embedding": embedding,
            "duration_seconds": 20,
            "model": "test-embedding",
        }

    async def transcribe(self, _audio, **_kwargs):
        return {"segments": self.segments}


class FakeOwnerVerifier:
    def __init__(self, matched):
        self.matched = matched

    async def verify_owner(self, _audio):
        if not self.matched:
            return {"matched": False, "reason": "below_conservative_threshold"}
        return {
            "matched": True,
            "profile_id": "owner-profile",
            "display_name": "You",
            "score": 0.91,
        }


class FakeFinalIntelligence:
    async def transcribe(self, _audio, **_kwargs):
        return {
            "provider": "test-asr",
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 900,
                    "text": "Public opening",
                    "words": [
                        {"start_ms": 0, "end_ms": 400, "word": "Public"},
                        {"start_ms": 450, "end_ms": 900, "word": " opening"},
                    ],
                },
                {
                    "start_ms": 1000,
                    "end_ms": 1900,
                    "text": "Saksham note launch confidential",
                    "words": [
                        {"start_ms": 1000, "end_ms": 1250, "word": "Saksham"},
                        {"start_ms": 1300, "end_ms": 1450, "word": " note"},
                        {"start_ms": 1500, "end_ms": 1700, "word": " launch"},
                        {"start_ms": 1720, "end_ms": 1900, "word": " confidential"},
                    ],
                },
                {
                    "start_ms": 2000,
                    "end_ms": 2900,
                    "text": "Public follow up",
                    "words": [
                        {"start_ms": 2000, "end_ms": 2300, "word": "Public"},
                        {"start_ms": 2350, "end_ms": 2600, "word": " follow"},
                        {"start_ms": 2650, "end_ms": 2900, "word": " up"},
                    ],
                },
            ],
        }

    async def diarize(self, _audio):
        return {
            "provider": "test-diarization",
            "turns": [
                {"start_ms": 0, "end_ms": 950, "speaker": "CLIENT"},
                {"start_ms": 950, "end_ms": 1950, "speaker": "OWNER"},
                {"start_ms": 1950, "end_ms": 3000, "speaker": "CLIENT"},
            ],
        }


class DeterministicMinutes:
    async def generate_pointers(self, segments, *, provisional=True):
        public = [item for item in segments if not item.get("is_private")]
        if not public:
            return []
        source = public[0]
        return [
            {
                "category": "key_point",
                "content": source["text"],
                "source_segment_ids": [source["id"]],
                "start_ms": source["start_ms"],
                "end_ms": source["end_ms"],
                "is_provisional": provisional,
                "metadata": {},
            }
        ]

    async def generate_final_structure(self, _meeting, _project, segments, _pointers):
        public = [item for item in segments if not item.get("is_private")]
        source = public[0]
        return (
            {
                "attendees": ["Speaker 1"],
                "summary": "Public discussion captured.",
                "summary_source_segment_ids": [source["id"]],
                "discussion_points": [
                    {"content": source["text"], "source_segment_ids": [source["id"]]}
                ],
                "decisions": [],
                "actions": [],
                "risks": [],
                "open_questions": [],
                "next_steps": [],
            },
            None,
        )

    render_markdown = staticmethod(MeetingMinutesGenerator.render_markdown)


class FakeCipher:
    def encrypt(self, values):
        return json.dumps(values).encode(), b"nonce"

    def decrypt(self, ciphertext, _nonce):
        return json.loads(ciphertext)


class MeetingPureFunctionTests(unittest.TestCase):
    def test_project_slug_blocks_path_traversal(self) -> None:
        self.assertEqual("client-secrets", sanitize_project_slug("../../Client Secrets"))
        self.assertNotIn("/", sanitize_project_slug("A/B Consulting"))

    def test_transcription_overlap_is_removed(self) -> None:
        text, removed = trim_transcript_overlap(
            "We will send the proposal tomorrow",
            "the proposal tomorrow and schedule review",
        )
        self.assertEqual("and schedule review", text)
        self.assertEqual(3, removed)

    def test_speaker_alignment_uses_maximum_overlap(self) -> None:
        aligned = align_speakers(
            [{"start_ms": 900, "end_ms": 2400, "text": "Hello"}],
            [
                {"start_ms": 0, "end_ms": 1000, "speaker": "A"},
                {"start_ms": 1000, "end_ms": 3000, "speaker": "B"},
            ],
        )
        self.assertEqual("B", aligned[0]["speaker_cluster"])
        self.assertEqual("Speaker 2", aligned[0]["speaker_label"])

    def test_speaker_alignment_splits_words_at_speaker_changes(self) -> None:
        aligned = align_speakers(
            [
                {
                    "id": "mixed",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "text": "Hello yes agreed",
                    "words": [
                        {"start_ms": 0, "end_ms": 500, "word": "Hello"},
                        {"start_ms": 600, "end_ms": 1000, "word": " yes"},
                        {"start_ms": 1200, "end_ms": 1800, "word": " agreed"},
                    ],
                }
            ],
            [
                {"start_ms": 0, "end_ms": 1050, "speaker": "A"},
                {"start_ms": 1050, "end_ms": 2000, "speaker": "B"},
            ],
        )

        self.assertEqual(["A", "B"], [item["speaker_cluster"] for item in aligned])
        self.assertEqual(["Hello yes", "agreed"], [item["text"] for item in aligned])
        self.assertNotEqual(aligned[0].get("id"), aligned[1].get("id"))

    def test_voice_enrollment_ranges_exclude_overlapped_speech(self) -> None:
        self.assertEqual(
            [(0, 1000), (1750, 3000)],
            subtract_time_ranges([(0, 3000)], [(1000, 1750)]),
        )

    def test_json_parser_accepts_fenced_output(self) -> None:
        self.assertEqual({"ok": True}, parse_json_object("```json\n{\"ok\":true}\n```"))

    def test_cosine_similarity(self) -> None:
        self.assertAlmostEqual(1.0, cosine_similarity([1, 0], [2, 0]))
        self.assertAlmostEqual(0.0, cosine_similarity([1, 0], [0, 1]))

    def test_private_word_span_is_redacted_without_losing_surrounding_speech(self) -> None:
        redacted = redact_private_ranges(
            [
                {
                    "id": "segment",
                    "start_ms": 0,
                    "end_ms": 3000,
                    "text": "Hello Saksham continue",
                    "words": [
                        {"start_ms": 0, "end_ms": 700, "word": "Hello"},
                        {"start_ms": 900, "end_ms": 1600, "word": " Saksham"},
                        {"start_ms": 2000, "end_ms": 2800, "word": " continue"},
                    ],
                }
            ],
            [(800, 1900)],
            {"owner"},
        )
        self.assertEqual(["Hello", "continue"], [item["text"] for item in redacted])
        self.assertTrue(all(not item["is_private"] for item in redacted))

    def test_known_participant_speech_survives_overlapping_private_range(self) -> None:
        source = {
            "start_ms": 0,
            "end_ms": 1000,
            "text": "Client keeps speaking",
            "profile_id": "participant",
        }
        self.assertEqual(
            "Client keeps speaking",
            redact_private_ranges([source], [(0, 1000)], {"owner"})[0]["text"],
        )

    def test_hinglish_and_english_deadlines_are_normalized(self) -> None:
        started = "2026-08-11T10:00:00+05:30"
        self.assertEqual("2026-08-12", normalize_deadline_phrase("kal", started))
        self.assertEqual("2026-08-13", normalize_deadline_phrase("day after tomorrow", started))
        self.assertEqual("2026-08-25", normalize_deadline_phrase("in 2 weeks", started))


class MeetingMinutesTests(unittest.IsolatedAsyncioTestCase):
    async def test_pointer_sources_must_exist(self) -> None:
        llm = FakeLLM(
            {
                "pointers": [
                    {
                        "category": "decision",
                        "content": "Use the pilot plan.",
                        "source_segment_ids": ["real"],
                    },
                    {
                        "category": "decision",
                        "content": "Invented decision.",
                        "source_segment_ids": ["missing"],
                    },
                ]
            }
        )
        generator = MeetingMinutesGenerator(llm)
        pointers = await generator.generate_pointers(
            [
                {
                    "id": "real",
                    "start_ms": 1000,
                    "end_ms": 2000,
                    "speaker_label": "Speaker 1",
                    "text": "Let's use the pilot plan.",
                    "is_private": False,
                }
            ]
        )
        self.assertEqual(1, len(pointers))
        self.assertEqual(["real"], pointers[0]["source_segment_ids"])

    async def test_actions_keep_sources_and_normalize_spoken_deadline(self) -> None:
        generator = MeetingMinutesGenerator(
            FakeLLM(
                {
                    "attendees": ["Speaker 1"],
                    "summary": {
                        "content": "A delivery date was discussed.",
                        "source_segment_ids": ["source"],
                    },
                    "discussion_points": [],
                    "decisions": [],
                    "actions": [
                        {
                            "action": "Send the proposal",
                            "owner": "Speaker 1",
                            "deadline": "2099-01-01",
                            "deadline_original": "tomorrow",
                            "priority": "urgent",
                            "inferred_fields": ["priority", "unsupported"],
                            "inference_reason": "The client emphasized it.",
                            "source_segment_ids": ["source"],
                        }
                    ],
                    "risks": [],
                    "open_questions": [],
                    "next_steps": [],
                }
            )
        )
        structured, warning = await generator.generate_final_structure(
            {"started_at": "2026-08-11T10:00:00+05:30", "title": "Review"},
            {"name": "Client"},
            [
                {
                    "id": "source",
                    "start_ms": 1000,
                    "end_ms": 2000,
                    "speaker_label": "Speaker 1",
                    "text": "Please send it tomorrow.",
                    "is_private": False,
                }
            ],
            [],
        )

        self.assertIsNone(warning)
        action = structured["actions"][0]
        self.assertEqual("2026-08-12", action["deadline"])
        self.assertEqual("tomorrow", action["deadline_original"])
        self.assertEqual("Not specified", action["priority"])
        self.assertEqual(["priority"], action["inferred_fields"])
        self.assertEqual(["source"], structured["summary_source_segment_ids"])

    async def test_empty_public_transcript_never_calls_the_llm(self) -> None:
        llm = FakeLLM({"summary": "invented"})
        generator = MeetingMinutesGenerator(llm)
        structured, warning = await generator.generate_final_structure(
            {"started_at": "2026-08-11T10:00:00+05:30", "title": "Empty"},
            {"name": "Client"},
            [],
            [],
        )
        self.assertIn("no public transcript", warning)
        self.assertEqual([], structured["discussion_points"])
        self.assertEqual([], structured["summary_source_segment_ids"])

    async def test_long_meeting_is_batched_and_keeps_all_mom_sections(self) -> None:
        llm = BoundedMinutesLLM()
        generator = MeetingMinutesGenerator(llm)
        segments = [
            {
                "id": f"very-long-source-id-{index:04d}-aaaaaaaa-bbbbbbbb-cccccccc",
                "start_ms": index * 1_000,
                "end_ms": (index + 1) * 1_000,
                "speaker_label": "Sagar" if index % 2 else "Client",
                "text": (
                    "We agreed to use the pilot. Sagar will send the proposal. "
                    "The dependency remains a delivery risk. Who owns the date? "
                    "Next step is to review it next week. "
                ),
                "is_private": False,
            }
            for index in range(90)
        ]

        structured, warning = await generator.generate_final_structure(
            {"started_at": "2026-08-11T10:00:00+05:30", "title": "Long review"},
            {"name": "Client"},
            segments,
            [],
        )

        self.assertIsNone(warning)
        self.assertGreater(len(llm.prompts), 2)
        self.assertTrue(all(len(prompt) < 15_000 for prompt in llm.prompts))
        self.assertTrue(any("Transcript chunk:" in prompt for prompt in llm.prompts))
        self.assertTrue(all(
            "very-long-source-id" not in prompt for prompt in llm.prompts
        ))
        for key in ("decisions", "actions", "risks", "open_questions", "next_steps"):
            self.assertTrue(structured[key], key)
            self.assertIn(
                structured[key][0]["source_segment_ids"][0],
                {segment["id"] for segment in segments},
            )

    def test_markdown_escapes_untrusted_fields_and_preserves_source_time(self) -> None:
        markdown = MeetingMinutesGenerator.render_markdown(
            {
                "id": "meeting",
                "title": "Review #1",
                "started_at": "2026-08-11T10:00:00+05:30",
            },
            {"name": "Client | North"},
            {
                "attendees": ["Person\n## Injected"],
                "summary": "Safe *summary*",
                "discussion_points": [
                    {"content": "Scope | timing", "source_segment_ids": ["source"]}
                ],
                "decisions": [],
                "owner_notes": [],
                "risks": [],
                "open_questions": [],
                "next_steps": [],
                "actions": [
                    {
                        "action": "Ship | phase",
                        "owner": "Person",
                        "deadline": "2026-08-12",
                        "deadline_original": "tomorrow",
                        "priority": "high",
                        "status": "open",
                        "source_segment_ids": ["source"],
                    }
                ],
            },
            [
                {
                    "id": "source",
                    "start_ms": 42000,
                    "end_ms": 43000,
                    "speaker_label": "Speaker *1*",
                    "text": "Discuss | scope",
                    "is_private": False,
                }
            ],
        )
        self.assertIn("[00:00:42]", markdown)
        self.assertIn("Client \\| North", markdown)
        self.assertIn("Person \\#\\# Injected", markdown)
        self.assertIn("Ship \\| phase", markdown)
        self.assertIn('Spoken deadline: "tomorrow".', markdown)


class VoiceProfileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MeetingStore(Path(self.temp_dir.name) / "saksham.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_owner_enrollment_and_conservative_match(self) -> None:
        intelligence = FakeIntelligence([[1.0, 0.0], [1.0, 0.05]])
        profiles = VoiceProfileService(self.store, intelligence, cipher=FakeCipher())
        profiles.threshold = 0.7
        profiles.margin = 0.05

        enrolled = await profiles.enroll(
            "You",
            "owner",
            [b"enrollment"],
            consent_confirmed=True,
        )
        matched = await profiles.verify_owner(b"probe")

        self.assertEqual("owner", enrolled["profile_type"])
        self.assertTrue(matched["matched"])
        self.assertEqual(enrolled["id"], matched["profile_id"])

    async def test_enrollment_requires_consent(self) -> None:
        profiles = VoiceProfileService(
            self.store,
            FakeIntelligence([[1.0, 0.0]]),
            cipher=FakeCipher(),
        )
        with self.assertRaises(ValueError):
            await profiles.enroll(
                "Client",
                "participant",
                [b"sample"],
                consent_confirmed=False,
            )

    async def test_real_encryption_and_revocation_hide_active_profile(self) -> None:
        cipher = VoiceProfileCipher(Path(self.temp_dir.name) / "voiceprints.key")
        cipher._key = b"k" * 32
        profiles = VoiceProfileService(
            self.store,
            FakeIntelligence([[0.8, 0.2]]),
            cipher=cipher,
        )
        enrolled = await profiles.enroll(
            "Client",
            "participant",
            [b"sample"],
            consent_confirmed=True,
        )
        stored = (await self.store.list_voice_profiles(include_embedding=True))[0]

        self.assertNotIn(b"0.8", bytes(stored["embedding_ciphertext"]))
        self.assertAlmostEqual(0.9701, cipher.decrypt(
            bytes(stored["embedding_ciphertext"]),
            bytes(stored["embedding_nonce"]),
        )[0], places=3)
        self.assertTrue(await self.store.revoke_voice_profile(enrolled["id"]))
        self.assertEqual([], await self.store.list_voice_profiles())
        revoked = await self.store.list_voice_profiles(include_revoked=True)
        self.assertIsNotNone(revoked[0]["revoked_at"])


class MeetingServiceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.projects_root = self.root / "projects"
        self.projects_root.mkdir()
        self.store = MeetingStore(self.root / "saksham.db")
        await self.store.initialize()
        self.project = await self.store.create_project(
            "Example Client",
            "example-client",
            self.projects_root / "example-client",
        )
        Path(self.project["folder_path"]).mkdir(parents=True)
        self.intelligence = FakeIntelligence()
        self.service = MeetingService(store=self.store, intelligence=self.intelligence)
        self.service._projects_root = self.projects_root
        self.service.settings = SimpleNamespace(
            saksham_home=self.root / "private",
            meeting_sample_rate=16000,
            meeting_live_transcription_seconds=1,
            meeting_pointer_interval_seconds=45,
            meeting_audio_retention_days=30,
        )

    async def asyncTearDown(self) -> None:
        await self.service.shutdown()
        self.temp_dir.cleanup()

    async def _create_meeting(self, title="Discovery call"):
        return await self.service.create_meeting(
            self.project["id"],
            title,
            consent_confirmed=True,
        )

    async def _enroll_stub_owner(self) -> None:
        await self.store.create_voice_profile(
            {
                "id": "owner-profile",
                "display_name": "You",
                "profile_type": "owner",
                "embedding_ciphertext": b"encrypted",
                "embedding_nonce": b"nonce",
                "model_name": "test",
                "consent_confirmed": True,
            }
        )

    async def test_only_one_meeting_can_claim_capture(self) -> None:
        meeting = await self._create_meeting()

        with self.assertRaises(MeetingCaptureBusy):
            await self._create_meeting("Competing call")

        self.assertTrue(await self.service.has_active_capture())
        await self.store.update_meeting(meeting["id"], capture_state="completed")
        self.assertFalse(await self.service.has_active_capture())

    async def test_public_meeting_responses_hide_private_audio_path(self) -> None:
        meeting = await self._create_meeting()

        listed = await self.service.list_meetings()
        detail = await self.service.get_detail(meeting["id"])

        self.assertNotIn("audio_path", listed[0])
        self.assertNotIn("audio_path", detail["meeting"])
        self.assertIn("audio_path", await self.store.get_meeting(meeting["id"]))

    async def test_live_loop_does_not_write_pointers_after_spoken_stop(self) -> None:
        meeting = await self._create_meeting()
        await self.service.audio.append_frame(meeting["id"], 0, b"\x00\x00" * 16000)
        self.intelligence.segments = [
            {"start_ms": 0, "end_ms": 1000, "text": "Saksham stop recording"}
        ]

        async def commit_and_stop(meeting_id, _segments):
            await self.store.update_meeting(meeting_id, capture_state="processing")

        self.service._commit_live_segments = commit_and_stop
        self.service._maybe_generate_live_pointers = AsyncMock()
        await self.service._run_live_transcription(meeting["id"])

        current = await self.store.get_meeting(meeting["id"])
        self.assertEqual(0, current["transcribed_samples"])
        self.service._maybe_generate_live_pointers.assert_not_awaited()

    async def test_uncertain_voice_cannot_stop_recording(self) -> None:
        meeting = await self._create_meeting()
        await self._enroll_stub_owner()
        await self.service.audio.append_frame(meeting["id"], 0, b"\x00\x00" * 16000)
        self.service.profiles = FakeOwnerVerifier(matched=False)
        self.service.stop_meeting = AsyncMock()

        await self.service._commit_live_segments(
            meeting["id"],
            [{"start_ms": 0, "end_ms": 800, "text": "Saksham stop recording"}],
        )

        self.service.stop_meeting.assert_not_awaited()
        public = await self.store.list_segments(meeting["id"])
        self.assertEqual("Saksham stop recording", public[0]["text"])

    async def test_verified_wake_opens_exactly_one_private_note_turn(self) -> None:
        meeting = await self._create_meeting()
        await self._enroll_stub_owner()
        await self.service.audio.append_frame(meeting["id"], 0, b"\x00\x00" * 16000)
        self.service.profiles = FakeOwnerVerifier(matched=True)

        await self.service._commit_live_segments(
            meeting["id"],
            [
                {"start_ms": 0, "end_ms": 300, "text": "Saksham"},
                {"start_ms": 350, "end_ms": 950, "text": "note this launch is confidential"},
            ],
        )

        current = await self.store.get_meeting(meeting["id"])
        self.assertEqual("silent_monitoring", current["assistant_state"])
        self.assertEqual([], await self.store.list_segments(meeting["id"]))
        private = await self.store.list_segments(meeting["id"], include_private=True)
        self.assertEqual(2, len(private))
        self.assertTrue(all(item["is_private"] for item in private))
        notes = await self.store.list_pointers(meeting["id"], include_private=True)
        self.assertEqual("private_note", notes[0]["category"])
        self.assertEqual([private[1]["id"]], notes[0]["source_segment_ids"])

    async def test_generic_greeting_is_public_and_does_not_wake_assistant(self) -> None:
        meeting = await self._create_meeting()
        await self.service._commit_live_segments(
            meeting["id"],
            [{"start_ms": 0, "end_ms": 400, "text": "Hi everyone"}],
        )

        current = await self.store.get_meeting(meeting["id"])
        public = await self.store.list_segments(meeting["id"])
        self.assertEqual("silent_monitoring", current["assistant_state"])
        self.assertEqual("Hi everyone", public[0]["text"])

    async def test_button_stop_finalizes_audio_and_is_idempotent(self) -> None:
        meeting = await self._create_meeting()
        await self.service.audio.append_frame(meeting["id"], 0, b"\x00\x00" * 100)
        self.service._ensure_final_task = lambda _meeting_id: None

        stopped = await self.service.stop_meeting(meeting["id"])
        stopped_again = await self.service.stop_meeting(meeting["id"])

        self.assertEqual("processing", stopped["capture_state"])
        self.assertEqual("processing", stopped_again["capture_state"])
        self.assertTrue(Path(stopped["audio_path"]).is_file())

    async def test_speaker_rename_updates_transcript_mapping(self) -> None:
        meeting = await self._create_meeting()
        await self.store.add_segment(
            meeting["id"],
            {
                "start_ms": 0,
                "end_ms": 500,
                "text": "Welcome",
                "speaker_cluster": "SPEAKER_00",
                "speaker_label": "Speaker 1",
            },
        )

        mapping = await self.service.rename_speaker(
            meeting["id"],
            "SPEAKER_00",
            "Anita",
        )
        segments = await self.store.list_segments(meeting["id"])

        self.assertEqual("Anita", mapping["display_name"])
        self.assertEqual("Anita", segments[0]["speaker_label"])

    async def test_typed_speaker_mapping_is_deferred_during_recording(self) -> None:
        meeting = await self._create_meeting()

        result = await self.service.submit_private_command(
            meeting["id"],
            "User 1 is Joshua and user 2 is Maazin",
            anchor_ms=2500,
        )

        pending = await self.store.list_pending_speaker_mappings(meeting["id"])
        messages = await self.store.list_private_messages(meeting["id"])
        self.assertEqual("speaker_mapping", result["type"])
        self.assertEqual(["Joshua", "Maazin"], [item["display_name"] for item in pending])
        self.assertEqual(["user", "assistant"], [item["role"] for item in messages])

    async def test_typed_private_action_never_appears_in_public_pointers(self) -> None:
        meeting = await self._create_meeting()

        await self.service.submit_private_command(
            meeting["id"],
            "action: send the proposal tomorrow",
            anchor_ms=42000,
        )

        self.assertEqual([], await self.store.list_pointers(meeting["id"]))
        internal = await self.store.list_pointers(meeting["id"], include_private=True)
        self.assertEqual("private_action", internal[0]["category"])
        self.assertEqual(42000, internal[0]["start_ms"])

    async def test_private_confirmation_and_action_progress_are_persisted_events(self) -> None:
        meeting = await self._create_meeting()
        events = []

        async def collect(_meeting_id, payload):
            events.append(payload)

        self.service.set_event_sink(collect)
        await self.service.record_private_agent_response(
            meeting["id"],
            {
                "text": "Please confirm opening the client file.",
                "awaiting_confirmation": True,
                "execution_id": "execution-1",
            },
        )
        await self.service.record_private_action_progress(
            meeting["id"],
            {
                "step": 1,
                "total_steps": 2,
                "description": "Opening the client file",
                "execution_id": "execution-1",
            },
        )

        messages = await self.store.list_private_messages(meeting["id"])
        self.assertEqual(["awaiting_confirmation", "running"], [item["status"] for item in messages])
        self.assertIn("pending_confirmation", {item["type"] for item in events})
        self.assertIn("action_progress", {item["type"] for item in events})

    async def test_mom_paths_are_contained_atomic_and_collision_safe(self) -> None:
        first = await self._create_meeting()
        first_path = await self.service._write_mom(first, self.project, "# First\n")
        second = dict(first)
        second["id"] = "second-meeting"
        second["mom_path"] = None
        second_path = await self.service._write_mom(second, self.project, "# Second\n")

        self.assertEqual("# First\n", first_path.read_text())
        self.assertEqual("# Second\n", second_path.read_text())
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(second_path.stem.endswith("-2"))
        self.assertEqual([], list(first_path.parent.glob("*.tmp")))

        escaped_project = dict(self.project, folder_path=str(self.root / "outside"))
        with self.assertRaises(ValueError):
            await self.service._write_mom(second, escaped_project, "unsafe")

    async def test_draft_after_final_does_not_overwrite_finalized_markdown(self) -> None:
        meeting = await self._create_meeting()
        await self.store.update_meeting(meeting["id"], capture_state="completed")

        final_path = await self.service._write_mom(meeting, self.project, "# Final\n")
        await self.store.update_meeting(meeting["id"], mom_path=str(final_path))
        await self.store.add_mom_revision(meeting["id"], "# Final\n", is_final=True)
        await self.service.save_mom(meeting["id"], "# New draft\n", final=False)

        self.assertEqual("# Final\n", final_path.read_text())
        self.assertEqual("# New draft\n", (await self.store.get_latest_mom(meeting["id"]))["content"])
        self.assertEqual("# Final\n", (await self.store.get_latest_final_mom(meeting["id"]))["content"])

    async def test_regenerate_mom_reuses_final_transcript_without_reprocessing_audio(self) -> None:
        meeting = await self._create_meeting()
        await self.store.add_segment(
            meeting["id"],
            {
                "start_ms": 0,
                "end_ms": 1000,
                "text": "We agreed to send the proposal tomorrow.",
                "speaker_cluster": "SPEAKER_00",
                "speaker_label": "Sagar",
                "is_provisional": False,
            },
        )
        await self.store.update_meeting(meeting["id"], capture_state="completed")
        self.service.minutes = DeterministicMinutes()

        regenerated = await self.service.regenerate_mom(meeting["id"])

        self.assertIn("Public discussion captured.", regenerated["content"])
        self.assertEqual(
            "We agreed to send the proposal tomorrow.",
            (await self.store.list_segments(meeting["id"]))[0]["text"],
        )
        jobs = await self.store.list_processing_jobs(meeting["id"])
        self.assertEqual("completed", jobs[-1]["state"])
        self.assertEqual("mom_regeneration", jobs[-1]["kind"])

    async def test_retention_deletes_audio_but_preserves_mom(self) -> None:
        meeting = await self._create_meeting()
        await self.service.audio.append_frame(meeting["id"], 0, b"\x00\x00" * 10)
        wav_path = await self.service.audio.finalize(meeting["id"])
        raw_path = wav_path.with_suffix(".pcm")
        mom_path = await self.service._write_mom(meeting, self.project, "# Retained MOM\n")
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        await self.store.update_meeting(
            meeting["id"],
            capture_state="completed",
            stopped_at=old,
            mom_path=str(mom_path),
        )

        self.assertEqual(1, await self.service.cleanup_expired_audio())
        self.assertFalse(wav_path.exists())
        self.assertFalse(raw_path.exists())
        self.assertTrue(mom_path.exists())
        current = await self.store.get_meeting(meeting["id"])
        self.assertIsNone(current["audio_path"])

    async def test_final_pipeline_keeps_private_note_out_of_public_outputs(self) -> None:
        meeting = await self._create_meeting()
        events = []

        async def collect(_meeting_id, payload):
            events.append(payload)

        self.service.set_event_sink(collect)
        self.service._reprocessing_meetings.add(meeting["id"])
        await self.service.audio.append_frame(meeting["id"], 0, b"\x00\x00" * 48000)
        await self.service.audio.finalize(meeting["id"])
        private = await self.store.add_segment(
            meeting["id"],
            {
                "id": "private-source",
                "start_ms": 1000,
                "end_ms": 1900,
                "text": "note launch confidential",
                "speaker_cluster": "OWNER",
                "speaker_label": "You",
                "profile_id": "owner-profile",
                "is_private": True,
            },
        )
        await self.store.add_pointer(
            meeting["id"],
            {
                "category": "private_note",
                "content": "launch confidential",
                "start_ms": 1000,
                "end_ms": 1900,
                "source_segment_ids": [private["id"]],
                "is_provisional": False,
            },
        )
        await self.store.update_meeting(
            meeting["id"],
            capture_state="processing",
            stopped_at=datetime.now(timezone.utc).isoformat(),
        )
        self.service.intelligence = FakeFinalIntelligence()
        self.service.minutes = DeterministicMinutes()

        await self.service._process_final(meeting["id"])

        detail = await self.service.get_detail(meeting["id"])
        self.assertEqual("completed", detail["meeting"]["capture_state"])
        self.assertEqual(1, len(detail["private_notes"]))
        self.assertNotIn("confidential", " ".join(item["text"] for item in detail["segments"]))
        self.assertNotIn("confidential", " ".join(item["content"] for item in detail["pointers"]))
        self.assertNotIn("confidential", detail["mom"]["content"])
        internal = await self.store.list_segments(meeting["id"], include_private=True)
        self.assertIn("private-source", {item["id"] for item in internal})
        self.assertTrue(all(job["state"] == "completed" for job in detail["processing_jobs"]))
        completed = [item for item in events if item["type"] == "reprocessing_completed"]
        self.assertTrue(completed[0]["success"])


if __name__ == "__main__":
    unittest.main()
