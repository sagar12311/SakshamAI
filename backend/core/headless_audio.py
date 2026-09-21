"""Optional microphone/speaker bridge for screenless macOS deployments.

The browser and headless runtime both feed the same VoiceProcessor, so admin
activation policy cannot be bypassed by choosing a different client.
"""

from __future__ import annotations

import asyncio
import io
import wave
from typing import Optional

from loguru import logger


class HeadlessAudioRuntime:
    def __init__(self, voice_processor, *, conversation_id: str = "headless", sample_rate: int = 16000):
        self.voice_processor = voice_processor
        self.conversation_id = conversation_id
        self.sample_rate = sample_rate
        self._stream = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self.available = False

    async def start(self) -> bool:
        try:
            import sounddevice as sd
        except ImportError:
            logger.warning("Headless audio disabled: install sounddevice for microphone support")
            return False
        loop = asyncio.get_running_loop()

        def callback(indata, frames, time_info, status):
            if status:
                logger.debug(f"Headless audio status: {status}")
            data = bytes(indata)
            loop.call_soon_threadsafe(self._queue.put_nowait, data)

        try:
            self._stream = sd.RawInputStream(
                samplerate=self.sample_rate, channels=1, dtype="int16",
                blocksize=int(self.sample_rate * 0.25), callback=callback,
            )
            self._stream.start()
            self._task = asyncio.create_task(self._consume())
            self.available = True
            logger.info("Headless microphone runtime started")
            return True
        except Exception as error:
            logger.warning(f"Headless audio unavailable: {error}")
            return False

    async def _consume(self) -> None:
        # The frontend VAD normally supplies utterance boundaries. For a
        # screenless device use a conservative fixed utterance window; the
        # worker still verifies the complete blob as one ceremony response.
        frames: list[bytes] = []
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            frames.append(chunk)
            if len(frames) >= 28:  # seven seconds at 250ms blocks
                payload = io.BytesIO()
                with wave.open(payload, "wb") as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(self.sample_rate)
                    wav.writeframes(b"".join(frames))
                await self.voice_processor.process_audio_chunk(
                    payload.getvalue(), conversation_id=self.conversation_id
                )
                frames.clear()

    async def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._task:
            await self._queue.put(None)
            self._task.cancel()
            self._task = None
        self.available = False
