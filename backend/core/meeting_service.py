"""Orchestrates meeting capture, transcription, controls, diarization, and MOM output."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import unicodedata
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from loguru import logger

from config import get_settings
from core.llm_client import get_llm_client
from core.meeting_audio import AudioSequenceGap, MeetingAudioRecorder
from core.meeting_intelligence_client import (
    MeetingIntelligenceClient,
    MeetingIntelligenceUnavailable,
)
from core.meeting_minutes import MeetingMinutesGenerator
from core.meeting_store import MeetingStore, get_meeting_store, utc_now
from core.state_store import get_state_store
from core.voice_profiles import VoiceProfileService
from memory.nova_memory import SemanticEntry, get_nova_memory


EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]

WAKE_PATTERN = re.compile(r"\bsaksham\b", re.IGNORECASE)
STOP_RECORDING_PATTERN = re.compile(
    r"^\s*(?:hey\s+)?(?:saksham[\s,]+)?(?:please\s+)?stop\s+(?:the\s+)?recording(?:\s+now)?[.!?\s]*$",
    re.IGNORECASE,
)
NOTE_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:note|remember|write\s+down)(?:\s+that)?\s*[:,-]?\s*(.+)$",
    re.IGNORECASE,
)
SPEAKER_MAPPING_PATTERN = re.compile(
    r"\b(?:speaker|user|person|participant)\s*(\d+)\s*"
    r"(?:is|=|:|named|name\s+is)\s*"
    r"([A-Za-z][A-Za-z .'-]{0,80}?)"
    r"(?=\s*(?:[,;]|\band\b)\s*(?:speaker|user|person|participant)\s*\d+|\s*$)",
    re.IGNORECASE,
)
PRIVATE_ITEM_PATTERN = re.compile(
    r"^\s*(?:(private)\s+)?(note|action|todo|follow[ -]?up|pointer)"
    r"(?:\s+that)?\s*[:,-]?\s*(.+)$",
    re.IGNORECASE,
)
REPROCESS_PATTERN = re.compile(
    r"\b(?:re[- ]?(?:diarize|process)|separate\s+(?:the\s+)?speakers|"
    r"there\s+(?:are|were)\s+(\d+)\s+speakers?)\b",
    re.IGNORECASE,
)


class MeetingCaptureBusy(RuntimeError):
    pass


def sanitize_project_slug(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    return slug[:80] or "project"


def trim_transcript_overlap(previous_text: str, new_text: str) -> tuple[str, int]:
    """Remove the longest word overlap and return text plus removed word count."""
    previous = previous_text.split()
    incoming = new_text.split()
    previous_folded = [re.sub(r"\W+", "", word).casefold() for word in previous]
    incoming_folded = [re.sub(r"\W+", "", word).casefold() for word in incoming]
    maximum = min(30, len(previous_folded), len(incoming_folded))
    overlap = 0
    for size in range(maximum, 0, -1):
        if previous_folded[-size:] == incoming_folded[:size]:
            overlap = size
            break
    return " ".join(incoming[overlap:]).strip(), overlap


def align_speakers(
    segments: list[dict[str, Any]],
    turns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Assign timestamped words to turns and split segments at speaker changes."""
    if not turns:
        return [dict(segment) for segment in segments]

    ordered_turns = sorted(turns, key=lambda item: int(item.get("start_ms", 0)))
    ordered_speakers: list[str] = []
    for turn in ordered_turns:
        speaker = str(turn.get("speaker", "SPEAKER_00"))
        if speaker not in ordered_speakers:
            ordered_speakers.append(speaker)
    display = {speaker: f"Speaker {index + 1}" for index, speaker in enumerate(ordered_speakers)}

    def speaker_for_range(start: int, end: int) -> str:
        best_speaker = str(ordered_turns[0].get("speaker", "SPEAKER_00"))
        best_overlap = -1
        midpoint = (start + end) / 2
        best_distance = float("inf")
        for turn in ordered_turns:
            turn_start = int(turn.get("start_ms", 0))
            turn_end = int(turn.get("end_ms", turn_start))
            overlap = max(0, min(end, turn_end) - max(start, turn_start))
            distance = 0 if turn_start <= midpoint <= turn_end else min(
                abs(midpoint - turn_start),
                abs(midpoint - turn_end),
            )
            if overlap > best_overlap or (overlap == best_overlap and distance < best_distance):
                best_overlap = overlap
                best_distance = distance
                best_speaker = str(turn.get("speaker", best_speaker))
        return best_speaker

    def words_to_text(words: list[dict[str, Any]]) -> str:
        raw = [str(word.get("word", "")) for word in words]
        if any(value[:1].isspace() for value in raw):
            return "".join(raw).strip()
        return " ".join(value.strip() for value in raw).strip()

    aligned: list[dict[str, Any]] = []
    for segment in segments:
        start = int(segment["start_ms"])
        end = int(segment["end_ms"])
        words = [word for word in segment.get("words", []) if isinstance(word, dict)]
        if not words:
            speaker = speaker_for_range(start, end)
            aligned.append(
                dict(
                    segment,
                    speaker_cluster=speaker,
                    speaker_label=display.get(speaker, "Speaker 1"),
                )
            )
            continue

        groups: list[tuple[str, list[dict[str, Any]]]] = []
        for word in words:
            word_start = int(word.get("start_ms", start))
            word_end = max(word_start + 1, int(word.get("end_ms", word_start + 1)))
            speaker = speaker_for_range(word_start, word_end)
            if groups and groups[-1][0] == speaker:
                groups[-1][1].append(word)
            else:
                groups.append((speaker, [word]))

        for index, (speaker, group) in enumerate(groups):
            text = words_to_text(group)
            if not text:
                continue
            result = dict(segment)
            if index:
                result.pop("id", None)
            result.update(
                {
                    "start_ms": int(group[0].get("start_ms", start)),
                    "end_ms": int(group[-1].get("end_ms", end)),
                    "text": text,
                    "words": group,
                    "speaker_cluster": speaker,
                    "speaker_label": display.get(speaker, "Speaker 1"),
                }
            )
            aligned.append(result)
    return aligned


def redact_private_ranges(
    segments: list[dict[str, Any]],
    private_ranges: list[tuple[int, int]],
    owner_profile_ids: set[str],
) -> list[dict[str, Any]]:
    """Remove private command audio while retaining safely separable public words."""
    if not private_ranges:
        return [dict(segment, is_private=False, is_provisional=False) for segment in segments]

    def overlaps_private(start_ms: int, end_ms: int) -> bool:
        return any(
            max(0, min(end_ms, private_end) - max(start_ms, private_start)) > 0
            for private_start, private_end in private_ranges
        )

    public: list[dict[str, Any]] = []
    for segment in segments:
        start_ms = int(segment["start_ms"])
        end_ms = int(segment["end_ms"])
        if not overlaps_private(start_ms, end_ms):
            public.append(dict(segment, is_private=False, is_provisional=False))
            continue

        profile_id = str(segment.get("profile_id") or "")
        if profile_id and profile_id not in owner_profile_ids:
            public.append(dict(segment, is_private=False, is_provisional=False))
            continue

        words = [word for word in segment.get("words", []) if isinstance(word, dict)]
        groups: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for word in words:
            word_start = int(word.get("start_ms", start_ms))
            word_end = int(word.get("end_ms", word_start))
            if word_end <= word_start or overlaps_private(word_start, word_end):
                if current:
                    groups.append(current)
                    current = []
                continue
            current.append(word)
        if current:
            groups.append(current)

        for index, group in enumerate(groups):
            raw_words = [str(word.get("word", "")) for word in group]
            if any(word[:1].isspace() for word in raw_words):
                text = "".join(raw_words).strip()
            else:
                text = " ".join(word.strip() for word in raw_words).strip()
            if not text:
                continue
            cleaned = dict(segment)
            if index or len(groups) > 1:
                cleaned.pop("id", None)
            cleaned.update(
                {
                    "start_ms": int(group[0].get("start_ms", start_ms)),
                    "end_ms": int(group[-1].get("end_ms", end_ms)),
                    "text": text,
                    "words": group,
                    "is_private": False,
                    "is_provisional": False,
                }
            )
            public.append(cleaned)
    return public


def subtract_time_ranges(
    ranges: list[tuple[int, int]],
    excluded: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Remove overlapped-speech spans from candidate voice-enrollment ranges."""
    result: list[tuple[int, int]] = []
    for start, end in ranges:
        pieces = [(start, end)]
        for excluded_start, excluded_end in excluded:
            next_pieces: list[tuple[int, int]] = []
            for piece_start, piece_end in pieces:
                if excluded_end <= piece_start or excluded_start >= piece_end:
                    next_pieces.append((piece_start, piece_end))
                    continue
                if excluded_start > piece_start:
                    next_pieces.append((piece_start, excluded_start))
                if excluded_end < piece_end:
                    next_pieces.append((excluded_end, piece_end))
            pieces = next_pieces
        result.extend((start, end) for start, end in pieces if end - start >= 250)
    return result


class MeetingService:
    def __init__(
        self,
        store: Optional[MeetingStore] = None,
        intelligence: Optional[MeetingIntelligenceClient] = None,
        minutes: Optional[MeetingMinutesGenerator] = None,
    ) -> None:
        self.settings = get_settings()
        self.store = store or get_meeting_store()
        self.audio = MeetingAudioRecorder(self.store)
        self.intelligence = intelligence or MeetingIntelligenceClient()
        self.minutes = minutes or MeetingMinutesGenerator()
        self.profiles = VoiceProfileService(self.store, self.intelligence)
        self._event_sink: Optional[EventSink] = None
        self._live_tasks: dict[str, asyncio.Task[None]] = {}
        self._final_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_locks: dict[str, asyncio.Lock] = {}
        self._private_timeouts: dict[str, asyncio.Task[None]] = {}
        self._reprocessing_meetings: set[str] = set()
        self._connection_counts: dict[str, int] = {}
        self._disconnect_tasks: dict[str, asyncio.Task[None]] = {}
        self._retention_task: Optional[asyncio.Task[None]] = None
        self._create_lock = asyncio.Lock()
        self._mom_lock = asyncio.Lock()
        self._projects_root = self.settings.projects_root.expanduser().resolve()

    def set_event_sink(self, sink: EventSink) -> None:
        self._event_sink = sink

    async def _emit(self, meeting_id: str, payload: dict[str, Any]) -> None:
        if not self._event_sink:
            return
        try:
            await self._event_sink(meeting_id, payload)
        except Exception as error:
            logger.debug(f"Meeting event delivery failed: {error}")

    async def initialize(self) -> None:
        await self.store.initialize()
        stored_root = await get_state_store().get_runtime_value("meeting_projects_root")
        if stored_root:
            self._projects_root = Path(stored_root).expanduser().resolve()
        self._projects_root.mkdir(parents=True, exist_ok=True)
        interrupted = await self.store.mark_active_interrupted()
        for meeting_id in interrupted:
            try:
                await self.audio.finalize(meeting_id)
            except Exception as error:
                logger.warning(f"Could not recover interrupted meeting {meeting_id}: {error}")
        await self.cleanup_expired_audio()
        if not self._retention_task or self._retention_task.done():
            self._retention_task = asyncio.create_task(self._retention_loop())

    async def shutdown(self) -> None:
        tasks = [
            *self._live_tasks.values(),
            *self._final_tasks.values(),
            *self._private_timeouts.values(),
            *self._disconnect_tasks.values(),
        ]
        if self._retention_task:
            tasks.append(self._retention_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._retention_task = None

    async def _retention_loop(self) -> None:
        while True:
            await asyncio.sleep(6 * 60 * 60)
            try:
                await self.cleanup_expired_audio()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(f"Meeting audio retention cleanup failed: {error}")

    def client_connected(self, meeting_id: str) -> None:
        self._connection_counts[meeting_id] = self._connection_counts.get(meeting_id, 0) + 1
        pending = self._disconnect_tasks.pop(meeting_id, None)
        if pending:
            pending.cancel()

    def client_disconnected(self, meeting_id: str) -> None:
        remaining = max(0, self._connection_counts.get(meeting_id, 1) - 1)
        self._connection_counts[meeting_id] = remaining
        if remaining:
            return
        previous = self._disconnect_tasks.pop(meeting_id, None)
        if previous:
            previous.cancel()

        async def mark_after_grace() -> None:
            await asyncio.sleep(60)
            meeting = await self.store.get_meeting(meeting_id)
            if self._connection_counts.get(meeting_id, 0) or not meeting:
                return
            if meeting["capture_state"] != "recording":
                return
            await self.store.update_meeting(
                meeting_id,
                capture_state="interrupted",
                stopped_at=utc_now(),
                processing_error="Capture connection was lost before an explicit stop.",
            )
            try:
                await self.audio.finalize(meeting_id)
            except Exception as error:
                logger.warning(f"Interrupted meeting finalization failed: {error}")
            await self._emit(
                meeting_id,
                {
                    "type": "meeting_state",
                    "state": "interrupted",
                    "message": "Recording was preserved after the capture connection was lost.",
                },
            )

        self._disconnect_tasks[meeting_id] = asyncio.create_task(mark_after_grace())

    @property
    def projects_root(self) -> Path:
        return self._projects_root

    async def set_projects_root(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ValueError("Projects root must be an absolute path or begin with ~")
        resolved = candidate.resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        if not resolved.is_dir():
            raise ValueError("Projects root is not a directory")
        self._projects_root = resolved
        await get_state_store().set_runtime_value("meeting_projects_root", str(resolved))
        return resolved

    async def create_project(self, name: str) -> dict[str, Any]:
        clean_name = " ".join(name.split()).strip()
        if not clean_name:
            raise ValueError("Project name is required")
        base_slug = sanitize_project_slug(clean_name)
        existing = await self.store.list_projects()
        used = {project["slug"] for project in existing}
        slug = base_slug
        suffix = 2
        while slug in used:
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        root = self._projects_root.resolve()
        folder = (root / slug).resolve()
        if not folder.is_relative_to(root):
            raise ValueError("Project folder escapes the configured projects root")
        (folder / "Meetings").mkdir(parents=True, exist_ok=True)
        return await self.store.create_project(clean_name, slug, folder)

    async def list_projects(self) -> list[dict[str, Any]]:
        return await self.store.list_projects()

    async def create_meeting(
        self,
        project_id: str,
        title: str,
        *,
        consent_confirmed: bool,
        expected_speaker_count: Optional[int] = None,
        voice_memory_consent_confirmed: bool = False,
        voice_memory_consent_scope: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not consent_confirmed:
            raise ValueError("Participant recording consent must be confirmed")
        if expected_speaker_count is not None and not 2 <= expected_speaker_count <= 10:
            raise ValueError("Expected speaker count must be between 2 and 10")
        if voice_memory_consent_confirmed and voice_memory_consent_scope != "all_participants":
            raise ValueError("Voice-memory consent scope must cover all participants")
        project = await self.store.get_project(project_id)
        if not project:
            raise KeyError(project_id)
        async with self._create_lock:
            active = await self.store.list_active_captures()
            if active:
                raise MeetingCaptureBusy(
                    f"'{active[0]['title']}' is already recording. Stop it before starting another meeting."
                )
            meeting_id = str(uuid4())
            private_dir = self.settings.saksham_home / "meetings" / meeting_id
            private_dir.mkdir(parents=True, exist_ok=False)
            audio_path = private_dir / "audio.wav"
            try:
                meeting = await self.store.create_meeting(
                    project_id,
                    " ".join(title.split()).strip() or "Client meeting",
                    self.settings.meeting_sample_rate,
                    audio_path,
                    metadata,
                    expected_speaker_count=expected_speaker_count,
                    voice_memory_consent_confirmed=voice_memory_consent_confirmed,
                    voice_memory_consent_scope=voice_memory_consent_scope,
                    meeting_id=meeting_id,
                )
            except Exception:
                private_dir.rmdir()
                raise
        await self._emit(
            meeting_id,
            {"type": "meeting_state", "meeting": self.public_meeting(meeting)},
        )
        if voice_memory_consent_confirmed:
            await self.store.add_voice_profile_event(
                profile_id=None,
                meeting_id=meeting_id,
                event_type="meeting_voice_memory_consent_confirmed",
                consent_scope=voice_memory_consent_scope,
                metadata={"source": "meeting_start_attestation"},
            )
        return meeting

    async def has_active_capture(self) -> bool:
        return bool(await self.store.list_active_captures())

    async def get_detail(self, meeting_id: str) -> dict[str, Any]:
        meeting = await self.store.get_meeting(meeting_id)
        if not meeting:
            raise KeyError(meeting_id)
        project = await self.store.get_project(meeting["project_id"])
        audio = self.audio_availability(meeting)
        return {
            "meeting": self.public_meeting(meeting),
            "project": project,
            "segments": await self.store.list_segments(meeting_id),
            "pointers": await self.store.list_pointers(meeting_id),
            "private_notes": [
                pointer
                for pointer in await self.store.list_pointers(meeting_id, include_private=True)
                if pointer.get("category") == "private_note"
            ],
            "mom": await self.store.get_latest_mom(meeting_id),
            "speakers": await self.store.list_speaker_mappings(meeting_id),
            "processing_jobs": await self.store.list_processing_jobs(meeting_id),
            "private_messages": await self.store.list_private_messages(meeting_id),
            "pending_speaker_mappings": await self.store.list_pending_speaker_mappings(meeting_id),
            "audio": audio,
        }

    @staticmethod
    def public_meeting(meeting: Optional[dict[str, Any]]) -> dict[str, Any]:
        """Remove private filesystem details before a meeting leaves the backend."""
        result = dict(meeting or {})
        result.pop("audio_path", None)
        return result

    def audio_availability(self, meeting: dict[str, Any]) -> dict[str, Any]:
        audio_path = Path(str(meeting.get("audio_path") or ""))
        available = bool(
            meeting.get("audio_path")
            and audio_path.is_file()
            and meeting.get("capture_state") in {"completed", "failed", "interrupted"}
        )
        expires_at: Optional[str] = None
        if meeting.get("stopped_at"):
            stopped = datetime.fromisoformat(str(meeting["stopped_at"]))
            expires_at = (
                stopped + timedelta(days=self.settings.meeting_audio_retention_days)
            ).isoformat()
        return {
            "available": available,
            "duration_ms": round(
                int(meeting.get("total_samples", 0)) * 1000
                / max(1, int(meeting.get("sample_rate", self.settings.meeting_sample_rate)))
            ),
            "expires_at": expires_at,
        }

    async def list_meetings(
        self,
        project_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        meetings = await self.store.list_meetings(project_id, limit)
        projects = {project["id"]: project for project in await self.store.list_projects()}
        public_meetings: list[dict[str, Any]] = []
        for meeting in meetings:
            public_meeting = self.public_meeting(meeting)
            public_meeting["project_name"] = projects.get(meeting["project_id"], {}).get(
                "name", "Unknown"
            )
            public_meetings.append(public_meeting)
        return public_meetings

    async def append_audio(self, meeting_id: str, sequence: int, pcm: bytes) -> dict[str, int]:
        result = await self.audio.append_frame(meeting_id, sequence, pcm)
        meeting = await self.store.get_meeting(meeting_id)
        if meeting:
            window_samples = int(meeting["sample_rate"]) * self.settings.meeting_live_transcription_seconds
            if int(meeting["total_samples"]) - int(meeting["transcribed_samples"]) >= window_samples:
                self._ensure_live_task(meeting_id)
        return result

    def _ensure_live_task(self, meeting_id: str) -> None:
        current = self._live_tasks.get(meeting_id)
        if current and not current.done():
            return
        task = asyncio.create_task(self._run_live_transcription(meeting_id))
        self._live_tasks[meeting_id] = task

    async def _run_live_transcription(self, meeting_id: str) -> None:
        try:
            while True:
                meeting = await self.store.get_meeting(meeting_id)
                if not meeting or meeting["capture_state"] != "recording":
                    return
                sample_rate = int(meeting["sample_rate"])
                window_samples = sample_rate * self.settings.meeting_live_transcription_seconds
                cursor = int(meeting["transcribed_samples"])
                total = int(meeting["total_samples"])
                if total - cursor < window_samples:
                    return
                end = cursor + window_samples
                overlap_start = max(0, cursor - sample_rate)
                wav = await self.audio.read_wav_slice(meeting_id, overlap_start, end)
                try:
                    result = await self.intelligence.transcribe(
                        wav,
                        offset_ms=round(overlap_start * 1000 / sample_rate),
                        final=False,
                    )
                    await self._commit_live_segments(meeting_id, result.get("segments", []))
                except Exception as error:
                    await self._emit(
                        meeting_id,
                        {"type": "recoverable_error", "stage": "live_transcription", "message": str(error)},
                    )
                current = await self.store.get_meeting(meeting_id)
                if not current or current["capture_state"] != "recording":
                    return
                await self.store.update_meeting(meeting_id, transcribed_samples=end)
                await self._maybe_generate_live_pointers(meeting_id, end)
        finally:
            self._live_tasks.pop(meeting_id, None)

    async def _commit_live_segments(
        self,
        meeting_id: str,
        raw_segments: list[dict[str, Any]],
    ) -> None:
        existing = await self.store.list_segments(meeting_id, include_private=True)
        previous_text = str(existing[-1]["text"]) if existing else ""
        for raw in raw_segments:
            text = str(raw.get("text", "")).strip()
            if not text:
                continue
            trimmed, removed_words = trim_transcript_overlap(previous_text, text)
            if not trimmed:
                continue
            start_ms = max(0, int(raw.get("start_ms", 0)))
            end_ms = max(start_ms, int(raw.get("end_ms", start_ms)))
            word_count = max(1, len(text.split()))
            if removed_words:
                start_ms += round((end_ms - start_ms) * removed_words / word_count)

            private = False
            owner_match: Optional[dict[str, Any]] = None
            command_audio: Optional[bytes] = None
            meeting = await self.store.get_meeting(meeting_id)
            is_control_candidate = bool(
                meeting
                and (
                    STOP_RECORDING_PATTERN.match(trimmed)
                    or WAKE_PATTERN.search(trimmed)
                    or meeting["assistant_state"] == "private_request"
                )
            )
            if is_control_candidate:
                owner_profiles = [
                    profile
                    for profile in await self.store.list_voice_profiles()
                    if profile["profile_type"] == "owner"
                ]
                if owner_profiles and end_ms > start_ms:
                    sample_rate = int(meeting["sample_rate"])
                    command_audio = await self.audio.read_wav_slice(
                        meeting_id,
                        round(start_ms * sample_rate / 1000),
                        round(end_ms * sample_rate / 1000),
                    )
                    try:
                        owner_match = await self.profiles.verify_owner(command_audio)
                    except Exception as error:
                        await self._emit(
                            meeting_id,
                            {"type": "recoverable_error", "stage": "owner_verification", "message": str(error)},
                        )

            owner_verified = bool(owner_match and owner_match.get("matched"))
            if owner_verified and (
                STOP_RECORDING_PATTERN.match(trimmed)
                or WAKE_PATTERN.search(trimmed)
                or (meeting and meeting["assistant_state"] == "private_request")
            ):
                private = True

            segment = await self.store.add_segment(
                meeting_id,
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": trimmed,
                    "words": list(raw.get("words") or [])[removed_words:],
                    "speaker_cluster": "LIVE",
                    "speaker_label": owner_match.get("display_name", "Speaker 1") if owner_verified else "Speaker 1",
                    "profile_id": owner_match.get("profile_id") if owner_verified else None,
                    "is_private": private,
                    "is_provisional": True,
                    "confidence": raw.get("confidence"),
                },
            )
            previous_text = trimmed

            if not private:
                await self._emit(meeting_id, {"type": "transcript_segment", "segment": segment})
                continue

            if STOP_RECORDING_PATTERN.match(trimmed):
                await self._emit(
                    meeting_id,
                    {"type": "private_response", "text": "Stopping the recording now."},
                )
                await self.stop_meeting(meeting_id)
                return

            request = WAKE_PATTERN.sub("", trimmed, count=1).strip(" ,:.-")
            if request:
                await self._handle_private_request(meeting_id, segment, request)
            elif meeting and meeting["assistant_state"] == "private_request":
                await self._handle_private_request(meeting_id, segment, trimmed)
            else:
                await self._enter_private_request(meeting_id)

    async def _enter_private_request(self, meeting_id: str) -> None:
        await self.store.update_meeting(meeting_id, assistant_state="private_request")
        await self._emit(
            meeting_id,
            {
                "type": "assistant_state",
                "state": "private_request",
                "text": "Recording continues in the background. Listening for one private request.",
            },
        )
        old_timeout = self._private_timeouts.pop(meeting_id, None)
        if old_timeout:
            old_timeout.cancel()

        async def expire() -> None:
            await asyncio.sleep(30)
            current = await self.store.get_meeting(meeting_id)
            if current and current["assistant_state"] == "private_request":
                await self.store.update_meeting(meeting_id, assistant_state="silent_monitoring")
                await self._emit(
                    meeting_id,
                    {"type": "assistant_state", "state": "silent_monitoring", "text": "Returned to silent monitoring."},
                )

        self._private_timeouts[meeting_id] = asyncio.create_task(expire())

    async def _handle_private_request(
        self,
        meeting_id: str,
        segment: dict[str, Any],
        request: str,
    ) -> None:
        timeout = self._private_timeouts.pop(meeting_id, None)
        if timeout:
            timeout.cancel()
        result = await self.submit_private_command(
            meeting_id,
            request,
            anchor_ms=int(segment["start_ms"]),
            selected_segment_ids=[str(segment["id"])],
            source="voice",
        )
        response_text = str(result.get("response", "Request received."))
        await self.store.update_meeting(meeting_id, assistant_state="silent_monitoring")
        await self._emit(
            meeting_id,
            {"type": "assistant_state", "state": "silent_monitoring", "text": "Returned to silent monitoring."},
        )

    async def _store_private_response(
        self,
        meeting_id: str,
        text: str,
        *,
        command_type: str,
        status: str = "completed",
        anchor_ms: int = 0,
        execution_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        existing = await self.store.list_private_messages(meeting_id)
        if existing:
            latest = existing[-1]
            if latest.get("role") == "assistant" and latest.get("text") == text:
                return latest
        message = await self.store.add_private_message(
            meeting_id,
            {
                "role": "assistant",
                "command_type": command_type,
                "status": status,
                "text": text,
                "anchor_ms": anchor_ms,
                "execution_id": execution_id,
                "metadata": metadata or {},
            },
        )
        await self._emit(meeting_id, {"type": "private_message", "message": message})
        await self._emit(meeting_id, {"type": "private_response", "text": text})
        return message

    async def record_private_agent_response(
        self,
        meeting_id: str,
        payload: dict[str, Any],
    ) -> None:
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        status = "awaiting_confirmation" if payload.get("awaiting_confirmation") else "completed"
        message = await self._store_private_response(
            meeting_id,
            text,
            command_type="external_action",
            status=status,
            execution_id=payload.get("execution_id"),
            metadata={"response_type": payload.get("type", "response")},
        )
        event_type = "pending_confirmation" if status == "awaiting_confirmation" else "action_progress"
        await self._emit(
            meeting_id,
            {
                "type": event_type,
                "status": status,
                "message": message,
                "execution_id": payload.get("execution_id"),
            },
        )

    async def record_private_action_progress(
        self,
        meeting_id: str,
        payload: dict[str, Any],
    ) -> None:
        step = int(payload.get("step") or 0)
        total_steps = int(payload.get("total_steps") or 0)
        description = str(payload.get("description") or "Working on the requested action.").strip()
        prefix = f"Step {step} of {total_steps}: " if step and total_steps else ""
        message = await self._store_private_response(
            meeting_id,
            f"{prefix}{description}",
            command_type="external_action",
            status="running",
            execution_id=payload.get("execution_id"),
            metadata={"step": step, "total_steps": total_steps},
        )
        await self._emit(
            meeting_id,
            {
                "type": "action_progress",
                "status": "running",
                "message": message,
                "execution_id": payload.get("execution_id"),
            },
        )

    async def submit_private_command(
        self,
        meeting_id: str,
        text: str,
        *,
        anchor_ms: int = 0,
        selected_segment_ids: Optional[list[str]] = None,
        source: str = "text",
    ) -> dict[str, Any]:
        meeting = await self.store.get_meeting(meeting_id)
        if not meeting:
            raise KeyError(meeting_id)
        command = " ".join(text.split()).strip()
        if not command:
            raise ValueError("Private command cannot be empty")
        valid_segment_ids = {
            str(item["id"])
            for item in await self.store.list_segments(
                meeting_id,
                include_private=source == "voice",
            )
        }
        selected_ids = [
            str(value)
            for value in (selected_segment_ids or [])
            if str(value) in valid_segment_ids
        ]
        user_message = await self.store.add_private_message(
            meeting_id,
            {
                "role": "user",
                "command_type": "pending",
                "status": "received",
                "text": command,
                "anchor_ms": anchor_ms,
                "metadata": {"source": source, "selected_segment_ids": selected_ids},
            },
        )
        await self._emit(meeting_id, {"type": "private_message", "message": user_message})

        async def resolve_user(
            command_type: str,
            status: str = "completed",
            execution_id: Optional[str] = None,
        ) -> None:
            updated = await self.store.update_private_message(
                user_message["id"],
                command_type=command_type,
                status=status,
                execution_id=execution_id,
            )
            if updated:
                await self._emit(meeting_id, {"type": "private_message", "message": updated})

        mappings = [
            (int(match.group(1)), match.group(2).strip(" .,'\""))
            for match in SPEAKER_MAPPING_PATTERN.finditer(command)
        ]
        if mappings:
            responses: list[str] = []
            for ordinal, display_name in mappings:
                if ordinal < 1 or ordinal > 20 or not display_name:
                    continue
                pending = await self.store.upsert_pending_speaker_mapping(
                    meeting_id,
                    ordinal,
                    display_name,
                    status="pending",
                    profile_status=(
                        "pending"
                        if meeting.get("voice_memory_consent_confirmed")
                        else "not_consented"
                    ),
                )
                if meeting["capture_state"] in {"completed", "failed", "interrupted"}:
                    segments = await self.store.list_segments(meeting_id, include_private=True)
                    clusters = self._ordered_public_clusters(segments)
                    if ordinal <= len(clusters):
                        cluster = clusters[ordinal - 1]
                        await self.rename_speaker(meeting_id, cluster, display_name)
                        if meeting.get("voice_memory_consent_confirmed"):
                            try:
                                await self.remember_speaker(
                                    meeting_id,
                                    cluster,
                                    display_name,
                                    True,
                                )
                                profile_status = "remembered"
                            except Exception as error:
                                profile_status = "pending"
                                await self.store.upsert_pending_speaker_mapping(
                                    meeting_id,
                                    ordinal,
                                    display_name,
                                    status="applied",
                                    cluster_label=cluster,
                                    profile_status=profile_status,
                                    error=str(error),
                                )
                                responses.append(
                                    f"Speaker {ordinal} is now {display_name}; voice memory is pending: {error}."
                                )
                                continue
                        else:
                            profile_status = "not_consented"
                        await self.store.upsert_pending_speaker_mapping(
                            meeting_id,
                            ordinal,
                            display_name,
                            status="applied",
                            cluster_label=cluster,
                            profile_status=profile_status,
                        )
                        responses.append(f"Speaker {ordinal} is now {display_name}.")
                        continue
                responses.append(
                    f"I will map Speaker {ordinal} to {display_name} after final speaker processing."
                )
            response_text = " ".join(responses) or "I could not resolve that speaker mapping."
            await resolve_user("speaker_mapping")
            await self._store_private_response(
                meeting_id,
                response_text,
                command_type="speaker_mapping",
                anchor_ms=anchor_ms,
                metadata={"mappings": mappings},
            )
            return {"type": "speaker_mapping", "response": response_text, "mappings": mappings}

        private_match = PRIVATE_ITEM_PATTERN.match(command) or NOTE_PATTERN.match(command)
        if private_match:
            if len(private_match.groups()) == 1:
                item_type = "note"
                content = private_match.group(1).strip()
            else:
                item_type = private_match.group(2).casefold().replace(" ", "_").replace("-", "_")
                content = private_match.group(3).strip()
            category = "private_action" if item_type in {"action", "todo", "follow_up"} else "private_note"
            pointer = await self.store.add_pointer(
                meeting_id,
                {
                    "category": category,
                    "content": content,
                    "source_segment_ids": selected_ids,
                    "start_ms": max(0, anchor_ms),
                    "end_ms": max(0, anchor_ms),
                    "is_provisional": False,
                    "metadata": {"owner_private": True, "source": source},
                },
            )
            response_text = f"Saved privately as {'an action' if category == 'private_action' else 'a note'}."
            await resolve_user(category)
            await self._emit(meeting_id, {"type": "private_pointer", "pointer": pointer})
            await self._store_private_response(
                meeting_id,
                response_text,
                command_type=category,
                anchor_ms=anchor_ms,
            )
            return {"type": category, "response": response_text, "pointer": pointer}

        reprocess_match = REPROCESS_PATTERN.search(command)
        if reprocess_match:
            count_match = re.search(r"\b([2-9]|10)\s+speakers?\b", command, re.IGNORECASE)
            automatic = bool(re.search(r"\bauto(?:matic)?\b", command, re.IGNORECASE))
            count = int(count_match.group(1)) if count_match else None
            await self.retry_processing(
                meeting_id,
                count,
                automatic_speaker_count=automatic,
            )
            response_text = (
                f"Reprocessing with exactly {count} speakers."
                if count
                else "Reprocessing with automatic speaker detection."
            )
            await resolve_user("reprocess", "running")
            await self._store_private_response(
                meeting_id,
                response_text,
                command_type="reprocess",
                status="running",
                anchor_ms=anchor_ms,
            )
            return {"type": "reprocess", "response": response_text}

        from core.nlu import get_nlu_engine

        nlu = get_nlu_engine().analyze(command)
        action_names = {intent.action for intent in nlu.intents}
        external_actions = {
            "search", "open_app", "close_app", "create", "send", "call",
            "adjust", "media_control", "navigate",
        }
        if action_names & external_actions:
            from core.cognition_bus import get_cognition_bus

            result = await get_cognition_bus().process_message(
                command,
                conversation_id=f"meeting:{meeting_id}",
            )
            response_text = str(result.get("text") or "Action submitted.")
            await resolve_user(
                "external_action",
                "awaiting_confirmation" if result.get("awaiting_confirmation") else "completed",
                result.get("execution_id"),
            )
            await self._store_private_response(
                meeting_id,
                response_text,
                command_type="external_action",
                status="awaiting_confirmation" if result.get("awaiting_confirmation") else "completed",
                anchor_ms=anchor_ms,
                execution_id=result.get("execution_id"),
            )
            return {"type": "external_action", "response": response_text, **result}

        public = await self.store.list_segments(meeting_id)
        context = "\n".join(
            f'{item.get("speaker_label", "Speaker")}: {item["text"]}' for item in public[-80:]
        )
        try:
            response = await get_llm_client().complete(
                messages=[{"role": "user", "content": command}],
                system_prompt=(
                    "You are Saksham answering privately about a meeting transcript. "
                    "Reply concisely in text only, never speak, and do not invent facts. "
                    "If the transcript does not contain the answer, say so.\n\n"
                    f"Meeting transcript:\n{context}"
                ),
                temperature=0.2,
            )
            response_text = str(response["choices"][0]["message"]["content"]).strip()
        except Exception as error:
            response_text = f"I could not answer that privately right now: {error}"
        await resolve_user("transcript_question")
        await self._store_private_response(
            meeting_id,
            response_text,
            command_type="transcript_question",
            anchor_ms=anchor_ms,
        )
        return {"type": "transcript_question", "response": response_text}

    async def _maybe_generate_live_pointers(self, meeting_id: str, cursor: int) -> None:
        meeting = await self.store.get_meeting(meeting_id)
        if not meeting:
            return
        interval = int(meeting["sample_rate"]) * self.settings.meeting_pointer_interval_seconds
        if cursor - int(meeting["pointer_samples"]) < interval:
            return
        segments = await self.store.list_segments(meeting_id)
        pointers = await self.minutes.generate_pointers(segments, provisional=True)
        existing = {
            (item["content"].casefold(), tuple(sorted(item["source_segment_ids"])))
            for item in await self.store.list_pointers(meeting_id)
        }
        for pointer in pointers:
            key = (pointer["content"].casefold(), tuple(sorted(pointer["source_segment_ids"])))
            if key in existing:
                continue
            stored = await self.store.add_pointer(meeting_id, pointer)
            await self._emit(meeting_id, {"type": "meeting_pointer", "pointer": stored})
        await self.store.update_meeting(meeting_id, pointer_samples=cursor)

    async def _start_processing_stage(
        self,
        meeting_id: str,
        kind: str,
        progress: int,
        message: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        job = await self.store.create_processing_job(
            meeting_id,
            kind,
            state="running",
            metadata=metadata,
        )
        await self._emit(
            meeting_id,
            {
                "type": "processing_progress",
                "stage": kind,
                "state": "running",
                "progress": progress,
                "message": message,
            },
        )
        return job

    async def _finish_processing_stage(
        self,
        meeting_id: str,
        job: dict[str, Any],
        *,
        state: str,
        progress: int,
        message: str,
        provider: Optional[str] = None,
        error: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        await self.store.update_processing_job(
            job["id"],
            state,
            provider=provider,
            error=error,
            metadata=metadata,
        )
        await self._emit(
            meeting_id,
            {
                "type": "processing_progress",
                "stage": job["kind"],
                "state": state,
                "progress": progress,
                "message": message,
                "error": error,
            },
        )

    async def stop_meeting(self, meeting_id: str) -> dict[str, Any]:
        lock = self._stop_locks.setdefault(meeting_id, asyncio.Lock())
        async with lock:
            meeting = await self.store.get_meeting(meeting_id)
            if not meeting:
                raise KeyError(meeting_id)
            if meeting["capture_state"] in {"processing", "completed", "failed", "interrupted"}:
                return meeting
            await self.store.update_meeting(
                meeting_id,
                capture_state="stopping",
                assistant_state="silent_monitoring",
            )
            await self._emit(meeting_id, {"type": "meeting_state", "state": "stopping"})
            live_task = self._live_tasks.get(meeting_id)
            if live_task and live_task is not asyncio.current_task():
                live_task.cancel()
                await asyncio.gather(live_task, return_exceptions=True)
            await self.audio.finalize(meeting_id)
            meeting = await self.store.update_meeting(
                meeting_id,
                capture_state="processing",
                stopped_at=utc_now(),
            )
            await self._emit(
                meeting_id,
                {"type": "meeting_state", "meeting": self.public_meeting(meeting)},
            )
            self._ensure_final_task(meeting_id)
            return meeting or {}

    def _ensure_final_task(self, meeting_id: str) -> None:
        current = self._final_tasks.get(meeting_id)
        if current and not current.done():
            return
        self._final_tasks[meeting_id] = asyncio.create_task(self._process_final(meeting_id))

    async def retry_processing(
        self,
        meeting_id: str,
        expected_speaker_count: Optional[int] = None,
        *,
        automatic_speaker_count: bool = False,
    ) -> dict[str, Any]:
        meeting = await self.store.get_meeting(meeting_id)
        if not meeting:
            raise KeyError(meeting_id)
        if meeting["capture_state"] in {"recording", "stopping"}:
            raise ValueError("Stop the meeting before retrying final processing")
        if meeting["capture_state"] == "processing":
            return meeting
        if not meeting.get("audio_path"):
            raise ValueError("Meeting audio is no longer available for reprocessing")
        if expected_speaker_count is not None and not 2 <= expected_speaker_count <= 10:
            raise ValueError("Expected speaker count must be between 2 and 10")
        selected_count = (
            None
            if automatic_speaker_count
            else expected_speaker_count
            if expected_speaker_count is not None
            else meeting.get("expected_speaker_count")
        )
        await self.store.update_meeting(
            meeting_id,
            capture_state="processing",
            processing_error=None,
            expected_speaker_count=selected_count,
        )
        await self._emit(
            meeting_id,
            {
                "type": "reprocessing_started",
                "expected_speaker_count": selected_count,
            },
        )
        self._reprocessing_meetings.add(meeting_id)
        self._ensure_final_task(meeting_id)
        return (await self.store.get_meeting(meeting_id)) or meeting

    async def _process_final(self, meeting_id: str) -> None:
        warnings: list[str] = []
        try:
            meeting = await self.store.get_meeting(meeting_id)
            if not meeting or not meeting.get("audio_path"):
                raise RuntimeError("Meeting audio is unavailable")
            wav_path = Path(meeting["audio_path"])
            audio_bytes = await asyncio.to_thread(wav_path.read_bytes)
            provisional = await self.store.list_segments(meeting_id, include_private=True)
            private_segments = [dict(item) for item in provisional if item.get("is_private")]
            private_ranges = [
                (int(item["start_ms"]), int(item["end_ms"]))
                for item in private_segments
            ]

            transcription_job = await self._start_processing_stage(
                meeting_id,
                "final_transcription",
                5,
                "Creating the final word-timestamped transcript.",
            )
            try:
                transcription = await self.intelligence.transcribe(audio_bytes, offset_ms=0, final=True)
                final_segments = [
                    {
                        "start_ms": int(item.get("start_ms", 0)),
                        "end_ms": int(item.get("end_ms", 0)),
                        "text": str(item.get("text", "")).strip(),
                        "words": list(item.get("words") or []),
                        "confidence": item.get("confidence"),
                        "is_private": False,
                    }
                    for item in transcription.get("segments", [])
                    if str(item.get("text", "")).strip()
                ]
                if transcription.get("warning"):
                    warnings.append(str(transcription["warning"]))
                if (
                    getattr(self.intelligence, "remote_url", "")
                    and transcription.get("provider") == getattr(self.intelligence, "local_url", None)
                ):
                    await self._emit(
                        meeting_id,
                        {
                            "type": "worker_fallback",
                            "stage": "final_transcription",
                            "provider": getattr(self.intelligence, "local_url", "local-worker"),
                            "message": "RTX transcription was unavailable; the Mac worker completed this stage.",
                        },
                    )
                await self._finish_processing_stage(
                    meeting_id,
                    transcription_job,
                    state="completed",
                    progress=40,
                    message="Final transcript is ready.",
                    provider=str(transcription.get("provider") or "meeting-worker"),
                )
            except Exception as error:
                warnings.append(f"Final transcription fallback used: {error}")
                final_segments = [dict(item) for item in provisional]
                await self._finish_processing_stage(
                    meeting_id,
                    transcription_job,
                    state="failed",
                    progress=40,
                    message="Final transcription was unavailable; preserving the live transcript.",
                    error=str(error),
                )

            if not final_segments:
                final_segments = [dict(item) for item in provisional]

            diarization_job = await self._start_processing_stage(
                meeting_id,
                "speaker_diarization",
                45,
                "Separating and aligning speakers.",
                metadata={"expected_speaker_count": meeting.get("expected_speaker_count")},
            )
            try:
                if meeting.get("expected_speaker_count"):
                    diarization = await self.intelligence.diarize(
                        audio_bytes,
                        num_speakers=int(meeting["expected_speaker_count"]),
                    )
                else:
                    diarization = await self.intelligence.diarize(audio_bytes)
                turns = list(diarization.get("turns", []))
                if turns:
                    final_segments = align_speakers(final_segments, turns)
                if (
                    getattr(self.intelligence, "remote_url", "")
                    and diarization.get("provider") == getattr(self.intelligence, "local_url", None)
                ):
                    await self._emit(
                        meeting_id,
                        {
                            "type": "worker_fallback",
                            "stage": "speaker_diarization",
                            "provider": getattr(self.intelligence, "local_url", "local-worker"),
                            "message": "RTX diarization was unavailable; the Mac worker completed this stage.",
                        },
                    )
                await self._finish_processing_stage(
                    meeting_id,
                    diarization_job,
                    state="completed",
                    progress=70,
                    message=(
                        f"Speaker alignment is ready with {diarization.get('speaker_count', 0)} speakers."
                    ),
                    provider=str(diarization.get("provider") or "meeting-worker"),
                    metadata={
                        "expected_speaker_count": meeting.get("expected_speaker_count"),
                        "speaker_count": diarization.get("speaker_count"),
                        "overlap_ranges": diarization.get("overlap_ranges", []),
                    },
                )
            except Exception as error:
                warnings.append(f"Speaker diarization unavailable: {error}")
                for item in final_segments:
                    item.setdefault("speaker_cluster", "SPEAKER_00")
                    item.setdefault("speaker_label", "Speaker 1")
                await self._finish_processing_stage(
                    meeting_id,
                    diarization_job,
                    state="failed",
                    progress=70,
                    message="Speaker diarization is unavailable; using unknown speaker labels.",
                    error=str(error),
                )

            await self._identify_known_speakers(wav_path, final_segments, warnings)
            profiles = await self.store.list_voice_profiles()
            owner_profile_ids = {
                str(profile["id"])
                for profile in profiles
                if profile.get("profile_type") == "owner"
            }
            final_segments = redact_private_ranges(
                final_segments,
                private_ranges,
                owner_profile_ids,
            )
            final_segments = await self._apply_pending_speaker_mappings(
                meeting_id,
                final_segments,
            )
            final_segments.extend(
                dict(item, is_private=True, is_provisional=False)
                for item in private_segments
            )
            final_segments.sort(key=lambda item: (int(item["start_ms"]), int(item["end_ms"])))
            await self.store.replace_final_segments(meeting_id, final_segments)
            final_segments = await self.store.list_segments(meeting_id, include_private=True)
            await self._enroll_applied_speakers(meeting_id, warnings)
            final_segments = await self.store.list_segments(meeting_id, include_private=True)
            await self.store.clear_generated_pointers(meeting_id)
            final_pointers = await self.minutes.generate_pointers(final_segments, provisional=False)
            for pointer in final_pointers:
                await self.store.add_pointer(meeting_id, pointer)
            pointers = await self.store.list_pointers(meeting_id)

            project = await self.store.get_project(meeting["project_id"])
            if not project:
                raise RuntimeError("Meeting project no longer exists")
            mom_job = await self._start_processing_stage(
                meeting_id,
                "mom_generation",
                75,
                "Building source-grounded meeting minutes.",
            )
            structured, generation_warning = await self.minutes.generate_final_structure(
                meeting,
                project,
                final_segments,
                pointers,
            )
            if generation_warning:
                warnings.append(generation_warning)
                await self._finish_processing_stage(
                    meeting_id,
                    mom_job,
                    state="failed",
                    progress=90,
                    message="Automated MOM synthesis was unavailable; a source transcript draft was created.",
                    error=generation_warning,
                )
            else:
                await self._finish_processing_stage(
                    meeting_id,
                    mom_job,
                    state="completed",
                    progress=90,
                    message="Source-grounded meeting minutes are ready.",
                    provider="configured-llm",
                )
            markdown = self.minutes.render_markdown(
                meeting,
                project,
                structured,
                final_segments,
                final=False,
            )
            finalization_job = await self._start_processing_stage(
                meeting_id,
                "mom_write",
                95,
                "Writing the editable MOM atomically.",
            )
            previous_final_mom = await self.store.get_latest_final_mom(meeting_id)
            mom_path = Path(meeting["mom_path"]) if meeting.get("mom_path") else None
            if not previous_final_mom:
                mom_path = await self._write_mom(meeting, project, markdown)
            mom = await self.store.add_mom_revision(
                meeting_id,
                markdown,
                structured,
                is_final=False,
            )
            meeting = await self.store.update_meeting(
                meeting_id,
                capture_state="completed",
                mom_path=str(mom_path) if mom_path else meeting.get("mom_path"),
                processing_error="\n".join(warnings) if warnings else None,
            )
            await self._finish_processing_stage(
                meeting_id,
                finalization_job,
                state="completed",
                progress=100,
                message="Meeting processing is complete.",
                provider="local-filesystem",
            )
            await self._emit(
                meeting_id,
                {
                    "type": "meeting_completed",
                    "meeting": self.public_meeting(meeting),
                    "mom": mom,
                    "segments": [item for item in final_segments if not item.get("is_private")],
                    "pointers": pointers,
                    "warnings": warnings,
                },
            )
            if meeting_id in self._reprocessing_meetings:
                await self._emit(
                    meeting_id,
                    {
                        "type": "reprocessing_completed",
                        "success": True,
                        "meeting": self.public_meeting(meeting),
                        "warnings": warnings,
                    },
                )
        except Exception as error:
            logger.exception(f"Meeting final processing failed for {meeting_id}: {error}")
            for job in await self.store.list_processing_jobs(meeting_id):
                if job["state"] == "running":
                    await self.store.update_processing_job(
                        job["id"],
                        "failed",
                        error=f"Pipeline stopped: {error}",
                    )
            meeting = await self.store.update_meeting(
                meeting_id,
                capture_state="failed",
                processing_error=str(error),
            )
            await self._emit(
                meeting_id,
                {
                    "type": "meeting_failed",
                    "meeting": self.public_meeting(meeting),
                    "message": str(error),
                },
            )
            if meeting_id in self._reprocessing_meetings:
                await self._emit(
                    meeting_id,
                    {
                        "type": "reprocessing_completed",
                        "success": False,
                        "meeting": self.public_meeting(meeting),
                        "error": str(error),
                    },
                )
        finally:
            self._reprocessing_meetings.discard(meeting_id)
            self._final_tasks.pop(meeting_id, None)

    @staticmethod
    def _ordered_public_clusters(segments: list[dict[str, Any]]) -> list[str]:
        clusters: list[str] = []
        for item in sorted(segments, key=lambda value: int(value.get("start_ms", 0))):
            if item.get("is_private"):
                continue
            cluster = str(item.get("speaker_cluster", "SPEAKER_00"))
            if cluster not in clusters:
                clusters.append(cluster)
        return clusters

    async def _apply_pending_speaker_mappings(
        self,
        meeting_id: str,
        segments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        clusters = self._ordered_public_clusters(segments)
        existing = {
            str(item["cluster_label"]): item
            for item in await self.store.list_speaker_mappings(meeting_id)
        }
        for item in segments:
            mapping = existing.get(str(item.get("speaker_cluster")))
            if mapping:
                item["speaker_label"] = mapping["display_name"]
                item["profile_id"] = mapping.get("profile_id") or item.get("profile_id")

        meeting = await self.store.get_meeting(meeting_id)
        consented = bool(meeting and meeting.get("voice_memory_consent_confirmed"))
        for pending in await self.store.list_pending_speaker_mappings(meeting_id):
            ordinal = int(pending["speaker_ordinal"])
            if ordinal < 1 or ordinal > len(clusters):
                await self.store.upsert_pending_speaker_mapping(
                    meeting_id,
                    ordinal,
                    pending["display_name"],
                    status="unresolved",
                    profile_status="pending" if consented else "not_consented",
                    error=f"Speaker {ordinal} was not found after diarization",
                )
                continue
            cluster = clusters[ordinal - 1]
            profile_id: Optional[str] = None
            for item in segments:
                if str(item.get("speaker_cluster")) == cluster:
                    item["speaker_label"] = pending["display_name"]
                    profile_id = profile_id or item.get("profile_id")
            await self.store.upsert_speaker_mapping(
                meeting_id,
                cluster,
                pending["display_name"],
                profile_id,
            )
            await self.store.upsert_pending_speaker_mapping(
                meeting_id,
                ordinal,
                pending["display_name"],
                status="applied",
                cluster_label=cluster,
                profile_status="pending" if consented else "not_consented",
            )
        return segments

    async def _enroll_applied_speakers(
        self,
        meeting_id: str,
        warnings: list[str],
    ) -> None:
        meeting = await self.store.get_meeting(meeting_id)
        if not meeting or not meeting.get("voice_memory_consent_confirmed"):
            return
        for pending in await self.store.list_pending_speaker_mappings(meeting_id):
            cluster = pending.get("cluster_label")
            if pending.get("status") != "applied" or not cluster:
                continue
            try:
                profile = await self.remember_speaker(
                    meeting_id,
                    str(cluster),
                    str(pending["display_name"]),
                    True,
                )
                await self.store.upsert_pending_speaker_mapping(
                    meeting_id,
                    int(pending["speaker_ordinal"]),
                    str(pending["display_name"]),
                    status="applied",
                    cluster_label=str(cluster),
                    profile_status="remembered",
                )
                await self._emit(
                    meeting_id,
                    {
                        "type": "speaker_mapping_updated",
                        "speaker_ordinal": pending["speaker_ordinal"],
                        "display_name": pending["display_name"],
                        "profile_id": profile["id"],
                        "profile_status": "remembered",
                    },
                )
            except Exception as error:
                message = str(error)
                warnings.append(f"Voice enrollment pending for {pending['display_name']}: {message}")
                await self.store.upsert_pending_speaker_mapping(
                    meeting_id,
                    int(pending["speaker_ordinal"]),
                    str(pending["display_name"]),
                    status="applied",
                    cluster_label=str(cluster),
                    profile_status="pending",
                    error=message,
                )

    async def _identify_known_speakers(
        self,
        wav_path: Path,
        segments: list[dict[str, Any]],
        warnings: list[str],
    ) -> None:
        profiles = await self.store.list_voice_profiles()
        if not profiles:
            return
        clusters = sorted({str(item.get("speaker_cluster", "SPEAKER_00")) for item in segments})
        for cluster in clusters:
            ranges = [
                (int(item["start_ms"]), int(item["end_ms"]))
                for item in segments
                if str(item.get("speaker_cluster", "SPEAKER_00")) == cluster
                and not item.get("is_private")
            ]
            clip = await asyncio.to_thread(self._extract_wav_ranges, wav_path, ranges, 30)
            if not clip:
                continue
            try:
                match = await self.profiles.match(clip)
            except Exception as error:
                warnings.append(f"Voice profile matching unavailable: {error}")
                return
            if not match.get("matched"):
                continue
            label = "You" if any(
                profile["id"] == match["profile_id"] and profile["profile_type"] == "owner"
                for profile in profiles
            ) else str(match["display_name"])
            for item in segments:
                if str(item.get("speaker_cluster", "SPEAKER_00")) == cluster:
                    item["speaker_label"] = label
                    item["profile_id"] = match["profile_id"]

    @staticmethod
    def _extract_wav_ranges(
        wav_path: Path,
        ranges: list[tuple[int, int]],
        maximum_seconds: int,
    ) -> bytes:
        if not ranges:
            return b""
        output = io.BytesIO()
        with wave.open(str(wav_path), "rb") as source:
            rate = source.getframerate()
            channels = source.getnchannels()
            width = source.getsampwidth()
            remaining = maximum_seconds * rate
            chunks: list[bytes] = []
            for start_ms, end_ms in ranges:
                if remaining <= 0:
                    break
                start_frame = max(0, round(start_ms * rate / 1000))
                frame_count = min(remaining, max(0, round((end_ms - start_ms) * rate / 1000)))
                if frame_count <= 0:
                    continue
                source.setpos(min(start_frame, source.getnframes()))
                chunks.append(source.readframes(frame_count))
                remaining -= frame_count
        with wave.open(output, "wb") as target:
            target.setnchannels(channels)
            target.setsampwidth(width)
            target.setframerate(rate)
            for chunk in chunks:
                target.writeframes(chunk)
        return output.getvalue()

    async def _write_mom(
        self,
        meeting: dict[str, Any],
        project: dict[str, Any],
        content: str,
    ) -> Path:
        async with self._mom_lock:
            configured_root = self._projects_root.resolve()
            project_dir = Path(project["folder_path"]).resolve()
            if not project_dir.is_relative_to(configured_root):
                raise ValueError("Project folder escapes the configured projects root")
            meetings_dir = (Path(project["folder_path"]) / "Meetings").resolve()
            if not meetings_dir.is_relative_to(project_dir):
                raise ValueError("MOM path escapes the project folder")
            meetings_dir.mkdir(parents=True, exist_ok=True)
            if meeting.get("mom_path"):
                destination = Path(meeting["mom_path"]).resolve()
                if not destination.is_relative_to(meetings_dir):
                    raise ValueError("MOM path escapes the project Meetings folder")
            else:
                started = datetime.fromisoformat(meeting["started_at"]).astimezone()
                stem = started.strftime("%Y-%m-%d_%H-%M")
                destination = meetings_dir / f"{stem}.md"
                suffix = 2
                while destination.exists():
                    destination = meetings_dir / f"{stem}-{suffix}.md"
                    suffix += 1

            def atomic_write() -> None:
                temp = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
                try:
                    with temp.open("w", encoding="utf-8") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp, destination)
                finally:
                    try:
                        temp.unlink()
                    except FileNotFoundError:
                        pass

            await asyncio.to_thread(atomic_write)
            return destination

    async def save_mom(self, meeting_id: str, content: str, *, final: bool = False) -> dict[str, Any]:
        meeting = await self.store.get_meeting(meeting_id)
        if not meeting:
            raise KeyError(meeting_id)
        if not content.strip():
            raise ValueError("MOM content cannot be empty")
        project = await self.store.get_project(meeting["project_id"])
        if not project:
            raise RuntimeError("Meeting project no longer exists")
        existing_final = await self.store.get_latest_final_mom(meeting_id)
        path = Path(meeting["mom_path"]) if meeting.get("mom_path") else None
        if final or not existing_final:
            path = await self._write_mom(meeting, project, content)
        latest = await self.store.get_latest_mom(meeting_id)
        revision = await self.store.add_mom_revision(
            meeting_id,
            content,
            latest.get("structured", {}) if latest else {},
            is_final=final,
        )
        if path:
            await self.store.update_meeting(meeting_id, mom_path=str(path))
        if final:
            await get_nova_memory().store_project_fact(
                project["id"],
                SemanticEntry(
                    fact=content,
                    category="project_knowledge",
                    confidence=1.0,
                    source=f"meeting:{meeting_id}:{path}",
                )
            )
        return revision

    async def regenerate_mom(self, meeting_id: str) -> dict[str, Any]:
        """Rebuild a draft MOM from retained final text without altering the recording."""
        meeting = await self.store.get_meeting(meeting_id)
        if not meeting:
            raise KeyError(meeting_id)
        if meeting["capture_state"] not in {"completed", "failed", "interrupted"}:
            raise ValueError("Finish meeting processing before regenerating its MOM")
        if await self.store.get_latest_final_mom(meeting_id):
            raise ValueError("The MOM is finalized and cannot be regenerated automatically")
        project = await self.store.get_project(meeting["project_id"])
        if not project:
            raise RuntimeError("Meeting project no longer exists")
        segments = await self.store.list_segments(meeting_id, include_private=True)
        pointers = await self.store.list_pointers(meeting_id)
        job = await self._start_processing_stage(
            meeting_id,
            "mom_regeneration",
            80,
            "Regenerating source-grounded meeting minutes.",
        )
        structured, warning = await self.minutes.generate_final_structure(
            meeting, project, segments, pointers
        )
        markdown = self.minutes.render_markdown(
            meeting, project, structured, segments, final=False
        )
        if warning:
            await self._finish_processing_stage(
                meeting_id,
                job,
                state="failed",
                progress=100,
                message="MOM regeneration used a transcript-only fallback.",
                error=warning,
            )
        else:
            await self._write_mom(meeting, project, markdown)
            await self.store.update_meeting(meeting_id, processing_error=None)
            await self._finish_processing_stage(
                meeting_id,
                job,
                state="completed",
                progress=100,
                message="Source-grounded meeting minutes were regenerated.",
                provider="configured-llm",
            )
        mom = await self.store.add_mom_revision(
            meeting_id, markdown, structured, is_final=False
        )
        await self._emit(
            meeting_id,
            {"type": "mom_regenerated", "mom": mom, "warning": warning},
        )
        return mom

    async def rename_speaker(
        self,
        meeting_id: str,
        cluster_label: str,
        display_name: str,
    ) -> dict[str, Any]:
        if not display_name.strip():
            raise ValueError("Speaker name is required")
        meeting = await self.store.get_meeting(meeting_id)
        if not meeting:
            raise KeyError(meeting_id)
        segments = await self.store.list_segments(meeting_id, include_private=True)
        clusters = self._ordered_public_clusters(segments)
        if cluster_label not in clusters:
            raise ValueError("Speaker cluster is not present in this meeting")
        ordinal = clusters.index(cluster_label) + 1
        existing_profile_id = next(
            (
                item.get("profile_id")
                for item in segments
                if item.get("speaker_cluster") == cluster_label and item.get("profile_id")
            ),
            None,
        )
        mapping = await self.store.upsert_speaker_mapping(
            meeting_id,
            cluster_label,
            display_name.strip(),
            existing_profile_id,
        )
        await self.store.upsert_pending_speaker_mapping(
            meeting_id,
            ordinal,
            display_name.strip(),
            status="applied",
            cluster_label=cluster_label,
            profile_status="remembered" if existing_profile_id else "pending",
        )
        await self.store.add_voice_profile_event(
            profile_id=existing_profile_id,
            meeting_id=meeting_id,
            event_type="speaker_renamed",
            consent_scope=meeting.get("voice_memory_consent_scope"),
            metadata={"cluster_label": cluster_label, "display_name": display_name.strip()},
        )
        return mapping

    async def remember_speaker(
        self,
        meeting_id: str,
        cluster_label: str,
        display_name: str,
        consent_confirmed: bool,
    ) -> dict[str, Any]:
        meeting = await self.store.get_meeting(meeting_id)
        if not meeting or not meeting.get("audio_path"):
            raise KeyError(meeting_id)
        if not consent_confirmed:
            raise ValueError("Explicit voice-profile consent is required")
        segments = await self.store.list_segments(meeting_id, include_private=True)
        ranges = [
            (int(item["start_ms"]), int(item["end_ms"]))
            for item in segments
            if item.get("speaker_cluster") == cluster_label and not item.get("is_private")
        ]
        jobs = await self.store.list_processing_jobs(meeting_id)
        diarization_job = next(
            (
                item
                for item in reversed(jobs)
                if item.get("kind") == "speaker_diarization" and item.get("state") == "completed"
            ),
            None,
        )
        overlap_ranges = [
            (int(item["start_ms"]), int(item["end_ms"]))
            for item in (diarization_job or {}).get("metadata", {}).get("overlap_ranges", [])
            if isinstance(item, dict) and "start_ms" in item and "end_ms" in item
        ]
        ranges = subtract_time_ranges(ranges, overlap_ranges)
        clean_duration_ms = sum(max(0, end - start) for start, end in ranges)
        if clean_duration_ms < 10_000:
            raise ValueError("Voice enrollment needs at least 10 seconds of clean speech")
        clip = await asyncio.to_thread(
            self._extract_wav_ranges,
            Path(meeting["audio_path"]),
            ranges,
            30,
        )
        matched = await self.profiles.match(clip, profile_type="participant")
        if matched.get("matched"):
            profile_id = str(matched["profile_id"])
            await self.store.rename_voice_profile(profile_id, display_name.strip())
            profile = next(
                item
                for item in await self.store.list_voice_profiles()
                if item["id"] == profile_id
            )
            event_type = "profile_matched_and_renamed"
        else:
            profile = await self.profiles.enroll(
                display_name,
                "participant",
                [clip],
                consent_confirmed=True,
                metadata={"source_meeting_id": meeting_id, "source_cluster": cluster_label},
            )
            event_type = "profile_enrolled"
        await self.store.upsert_speaker_mapping(
            meeting_id,
            cluster_label,
            display_name.strip(),
            profile["id"],
        )
        await self.store.add_voice_profile_event(
            profile_id=profile["id"],
            meeting_id=meeting_id,
            event_type=event_type,
            consent_scope=meeting.get("voice_memory_consent_scope") or "individual_confirmation",
            metadata={
                "source_cluster": cluster_label,
                "display_name": display_name.strip(),
                "clean_duration_ms": clean_duration_ms,
                "match_score": matched.get("score"),
            },
        )
        return profile

    async def cleanup_expired_audio(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.settings.meeting_audio_retention_days)
        meetings = await self.store.meetings_with_expired_audio(cutoff.isoformat())
        for meeting in meetings:
            await self.audio.remove_audio(meeting)
            await self.store.update_meeting(meeting["id"], audio_path=None)
        return len(meetings)

    async def delete_meeting(self, meeting_id: str, delete_note: bool = False) -> bool:
        meeting = await self.store.get_meeting(meeting_id)
        if not meeting:
            return False
        if meeting["capture_state"] in {"recording", "stopping", "processing"}:
            raise ValueError("Active meetings cannot be deleted")
        await self.audio.remove_audio(meeting)
        if delete_note and meeting.get("mom_path"):
            try:
                Path(meeting["mom_path"]).unlink()
            except FileNotFoundError:
                pass
        return await self.store.delete_meeting(meeting_id)


_meeting_service: Optional[MeetingService] = None


def get_meeting_service() -> MeetingService:
    global _meeting_service
    if _meeting_service is None:
        _meeting_service = MeetingService()
    return _meeting_service


__all__ = [
    "AudioSequenceGap",
    "MeetingCaptureBusy",
    "MeetingService",
    "align_speakers",
    "get_meeting_service",
    "redact_private_ranges",
    "subtract_time_ranges",
    "sanitize_project_slug",
    "trim_transcript_overlap",
]
