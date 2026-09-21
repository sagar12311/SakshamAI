"""Crash-safe PCM capture and WAV finalization for meetings."""

from __future__ import annotations

import asyncio
import os
import wave
from pathlib import Path
from typing import Any, Optional

from core.meeting_store import MeetingStore


class AudioSequenceGap(ValueError):
    def __init__(self, expected: int, received: int) -> None:
        super().__init__(f"Expected audio frame {expected}, received {received}")
        self.expected = expected
        self.received = received


class MeetingAudioRecorder:
    """Serializes audio appends and keeps the file consistent with SQLite state."""

    def __init__(self, store: MeetingStore) -> None:
        self.store = store
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, meeting_id: str) -> asyncio.Lock:
        return self._locks.setdefault(meeting_id, asyncio.Lock())

    @staticmethod
    def _raw_path(meeting: dict[str, Any]) -> Path:
        audio_path = Path(str(meeting["audio_path"]))
        return audio_path.with_suffix(".pcm")

    @staticmethod
    def _repair_raw_file(raw_path: Path, expected_bytes: int) -> None:
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        if not raw_path.exists():
            raw_path.touch()
        current_size = raw_path.stat().st_size
        if current_size < expected_bytes:
            raise RuntimeError(
                f"Meeting audio is shorter than committed state ({current_size} < {expected_bytes})"
            )
        if current_size > expected_bytes:
            with raw_path.open("r+b") as handle:
                handle.truncate(expected_bytes)

    async def append_frame(self, meeting_id: str, sequence: int, pcm: bytes) -> dict[str, int]:
        if not pcm or len(pcm) % 2:
            raise ValueError("Meeting audio must contain non-empty PCM16 samples")

        async with self._lock_for(meeting_id):
            meeting = await self.store.get_meeting(meeting_id)
            if not meeting:
                raise KeyError(meeting_id)
            if meeting["capture_state"] != "recording":
                raise RuntimeError("Meeting is not recording")

            last_sequence = int(meeting["last_sequence"])
            total_samples = int(meeting["total_samples"])
            if sequence <= last_sequence:
                return {"ack": last_sequence, "total_samples": total_samples}
            expected = last_sequence + 1
            if sequence != expected:
                raise AudioSequenceGap(expected, sequence)

            raw_path = self._raw_path(meeting)
            expected_bytes = total_samples * 2
            await asyncio.to_thread(self._repair_raw_file, raw_path, expected_bytes)
            sample_count = len(pcm) // 2

            def append() -> None:
                with raw_path.open("ab") as handle:
                    handle.write(pcm)
                    handle.flush()
                    os.fsync(handle.fileno())

            await asyncio.to_thread(append)
            committed = await self.store.record_frame(
                meeting_id,
                sequence,
                sample_count,
                last_sequence,
                total_samples,
            )
            if not committed:
                await asyncio.to_thread(self._repair_raw_file, raw_path, expected_bytes)
                latest = await self.store.get_meeting(meeting_id)
                if latest and sequence <= int(latest["last_sequence"]):
                    return {
                        "ack": int(latest["last_sequence"]),
                        "total_samples": int(latest["total_samples"]),
                    }
                raise RuntimeError("Audio frame state changed while committing")

            return {
                "ack": sequence,
                "total_samples": total_samples + sample_count,
            }

    async def read_wav_slice(
        self,
        meeting_id: str,
        start_sample: int,
        end_sample: int,
    ) -> bytes:
        meeting = await self.store.get_meeting(meeting_id)
        if not meeting:
            raise KeyError(meeting_id)
        start = max(0, int(start_sample))
        end = min(int(meeting["total_samples"]), max(start, int(end_sample)))
        raw_path = self._raw_path(meeting)
        sample_rate = int(meeting["sample_rate"])

        def build() -> bytes:
            import io

            with raw_path.open("rb") as source:
                source.seek(start * 2)
                pcm = source.read((end - start) * 2)
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(pcm)
            return output.getvalue()

        return await asyncio.to_thread(build)

    async def finalize(self, meeting_id: str) -> Path:
        async with self._lock_for(meeting_id):
            meeting = await self.store.get_meeting(meeting_id)
            if not meeting:
                raise KeyError(meeting_id)
            wav_path = Path(str(meeting["audio_path"]))
            raw_path = self._raw_path(meeting)
            expected_bytes = int(meeting["total_samples"]) * 2
            await asyncio.to_thread(self._repair_raw_file, raw_path, expected_bytes)

            def write_wav() -> None:
                temp_path = wav_path.with_suffix(".wav.tmp")
                with raw_path.open("rb") as source, wave.open(str(temp_path), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(int(meeting["sample_rate"]))
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        wav.writeframesraw(chunk)
                os.replace(temp_path, wav_path)

            await asyncio.to_thread(write_wav)
            return wav_path

    async def remove_audio(self, meeting: dict[str, Any]) -> None:
        audio_value: Optional[str] = meeting.get("audio_path")
        if not audio_value:
            return
        wav_path = Path(audio_value)
        raw_path = wav_path.with_suffix(".pcm")

        def remove() -> None:
            for path in (wav_path, raw_path, wav_path.with_suffix(".wav.tmp")):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

        await asyncio.to_thread(remove)
