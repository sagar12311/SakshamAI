"""
Saksham AI - Voice Processor (Refactored)
Handles audio input/output only. All intelligence is delegated to the PlannerAgent.
Includes Wake Word detection and Session Management.
"""

import asyncio
import time
import re
from typing import Optional, Callable

import numpy as np
import noisereduce as nr
from loguru import logger
from openai import AsyncOpenAI

from config import get_settings
from core.llm_client import get_llm_client
from core.cognition_bus import get_cognition_bus, CognitionMessage, MessageType
from core.admin_auth import get_admin_auth_service
from core.dialogue_manager import CommittedTurn, get_dialogue_manager
from core.user_profile import get_user_profile
from mac.productivity import ProductivityController

settings = get_settings()

TONE_PROFILES = {
    "neutral": {"rate": "+0%", "pitch": "+0Hz", "length_scale": 1.0},
    "warm": {"rate": "-4%", "pitch": "-2Hz", "length_scale": 1.05},
    "reassuring": {"rate": "-8%", "pitch": "-3Hz", "length_scale": 1.1},
    "calm": {"rate": "-7%", "pitch": "-4Hz", "length_scale": 1.08},
    "positive": {"rate": "+4%", "pitch": "+3Hz", "length_scale": 0.96},
    "upbeat": {"rate": "+7%", "pitch": "+5Hz", "length_scale": 0.92},
    "excited": {"rate": "+9%", "pitch": "+6Hz", "length_scale": 0.9},
    "concerned": {"rate": "-6%", "pitch": "-2Hz", "length_scale": 1.07},
    "frustrated": {"rate": "-6%", "pitch": "-3Hz", "length_scale": 1.07},
    "sad": {"rate": "-10%", "pitch": "-5Hz", "length_scale": 1.12},
    "urgent": {"rate": "+10%", "pitch": "+2Hz", "length_scale": 0.9},
}

# Wake word patterns - starts a conversation session
WAKE_PATTERNS = [
    r"\bsaksham\b",
    r"\bhey saksham\b", 
    r"\bok saksham\b",
    r"\bsaksham ai\b",
    r"\bhi saksham\b",
    r"hisaksham",  # Common concatenation
    r"sak sham",   # Common split
    r"saksam",     # Phonetic (Common)
    r"sak sam",    # Phonetic split
    
    # Generic greetings for easier activation
    r"\bhello\b",
    r"\bhi\b",
    r"\bhey\b",
]

BARGE_IN_STOP_PHRASES = (
    "stop",
    "wait",
    "hold on",
    "shut up",
    "cancel",
    "saksham stop",
)


def is_barge_in_stop(text: str) -> bool:
    """Return true only for a short, direct request to stop current speech."""
    normalized = re.sub(r"[.!?,]+$", "", text.strip().lower())
    if not normalized or len(normalized.split()) > 6:
        return False
    return any(
        normalized == phrase or normalized.startswith(phrase + " ")
        for phrase in BARGE_IN_STOP_PHRASES
    )

class VoiceProcessor:
    """
    Pure I/O Voice Processor with Session Management.
    1. Listens to Audio -> Transcribes
    2. Checks Wake Word / Active Session
    3. Publishes USER_INPUT to Bus if valid
    """
    
    def __init__(self):
        self._llm = None
        self._productivity = ProductivityController()
        
        self._is_listening = False
        self._last_response_time: float = 0
        self._is_speaking: bool = False
        self._playback_conversations: set[str] = set()
        self._speech_end_time: float = 0
        self._speech_cooldown_seconds: float = 0.8
        
        # Session State
        self._session_active: bool = False
        self._last_interaction_time: float = 0
        self._interaction_timeout_seconds: float = 30.0
        
        # Audio filters
        self._min_audio_bytes: int = 3000
        
        # Conversation Mode
        self.conversation_mode: bool = True
        
        # Audio buffer for VAD
        self._ambient_buffer = []
        self._dialogue = get_dialogue_manager()
        self._dialogue.set_commit_handler(self._commit_conversation_turn)
        self._audio_sequences: dict[str, int] = {}

        # Piper Voice instance
        self._piper_voice = None
        self._load_piper_model()

    def _load_piper_model(self, *, force: bool = False):
        """Load Piper TTS model if enabled"""
        if settings.tts_enabled and (settings.tts_provider == "piper" or force):
            import os
            if os.path.exists(settings.piper_model_path):
                try:
                    from piper import PiperVoice
                    # This loads the ONNX model (can take a moment)
                    self._piper_voice = PiperVoice.load(settings.piper_model_path)
                    logger.info(f"⚡ Loaded Piper voice model: {settings.piper_model_path}")
                except Exception as e:
                    logger.error(f"Failed to load Piper model: {e}")
            else:
                 logger.warning(f"Piper model not found at {settings.piper_model_path}")
        
    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm_client()
        return self._llm
    
    @property
    def is_listening(self) -> bool:
        return self._is_listening
        
    @property
    def session_active(self) -> bool:
        """Check if conversation session is active"""
        if not self._session_active:
            return False
        
        # Check timeout
        silence_duration = time.time() - self._last_interaction_time
        if silence_duration > self._interaction_timeout_seconds:
            logger.info(f"📴 Session ended: silence ({int(silence_duration)}s > {self._interaction_timeout_seconds}s)")
            self._session_active = False
            return False
            
        return True

    def start_session(self) -> None:
        self._session_active = True
        self._last_interaction_time = time.time()
        logger.info("🟢 Session Started")
        
    def extend_session(self) -> None:
        self._last_interaction_time = time.time()
        
    def end_session(self) -> None:
        self._session_active = False
        logger.info("📴 Session Ended")

    async def start_listening(self) -> None:
        self._is_listening = True
        logger.info("🎤 Ambient listening started")
    
    async def stop_listening(self) -> None:
        self._is_listening = False
        self._session_active = False
        self._playback_conversations.clear()
        logger.info("🎤 Ambient listening stopped")

    async def flush_conversation(self, conversation_id: str = "default") -> None:
        await self._dialogue.flush(conversation_id)

    async def record_assistant_turn(
        self,
        text: str,
        conversation_id: str = "default",
    ) -> None:
        await self._dialogue.record_assistant_turn(conversation_id, text)

    def set_playback_state(
        self,
        speaking: bool,
        conversation_id: str = "default",
    ) -> None:
        """Track actual client playback, not only TTS generation time."""
        if speaking:
            self._playback_conversations.add(conversation_id)
        else:
            self._playback_conversations.discard(conversation_id)
            self._speech_end_time = time.time()
        logger.debug(
            f"Client playback state changed: conversation={conversation_id}, "
            f"speaking={speaking}"
        )
        
    async def note_audio_received(self, conversation_id: str, sequence: int) -> None:
        """Tell the dialogue manager that another spoken fragment is queued."""
        await self._dialogue.note_audio_received(conversation_id, sequence)

    async def note_speech_activity(
        self,
        conversation_id: str,
        speaking: bool,
    ) -> None:
        """Pause turn settlement while the client is actively forming a fragment."""
        if speaking:
            await self._dialogue.note_speech_started(conversation_id)
        else:
            await self._dialogue.note_speech_ended(conversation_id)

    async def process_audio_chunk(
        self,
        audio_data: bytes,
        *,
        conversation_id: str = "default",
        sequence: Optional[int] = None,
    ) -> dict:
        if sequence is None:
            sequence = self._audio_sequences.get(conversation_id, 0) + 1
            self._audio_sequences[conversation_id] = sequence
            await self.note_audio_received(conversation_id, sequence)

        try:
            return await self._process_audio_chunk(
                audio_data,
                conversation_id=conversation_id,
                sequence=sequence,
            )
        finally:
            await self._dialogue.mark_audio_processed(conversation_id, sequence)

    async def _process_audio_chunk(
        self,
        audio_data: bytes,
        *,
        conversation_id: str,
        sequence: int,
    ) -> dict:
        """
        Process incoming audio chunk:
        1. Reduce Noise (RNNoise/Noisereduce)
        2. Transcribe (Whisper)
        3. Check Session / Wake Word
        """
        # --- Noise Reduction (Phase 2) ---
        try:
            import soundfile as sf
            import io
            
            # Read WAV safely using soundfile (handles header automatically)
            with io.BytesIO(audio_data) as wav_io:
                # Returns float64 array (-1.0 to 1.0)
                audio_array, sample_rate = sf.read(wav_io)

            if isinstance(audio_array, np.ndarray) and audio_array.ndim > 1:
                audio_array = np.mean(audio_array, axis=1)
            
            # Apply stationary noise reduction
            reduced_noise = nr.reduce_noise(
                y=audio_array,
                sr=sample_rate,
                stationary=True,
                prop_decrease=0.75,
                n_fft=1024
            )
            
            # Write back to WAV bytes (converts back to PCM 16-bit)
            with io.BytesIO() as out_io:
                sf.write(out_io, reduced_noise, sample_rate, format='WAV', subtype='PCM_16')
                audio_data = out_io.getvalue()
                
        except Exception as e:
            logger.error(f"Noise reduction error: {e}")
            # Fallback to original audio is automatic as audio_data isn't overwritten on error
        
        # --- End Noise Reduction ---

        # 0. Reject very small chunks (Safety check)ing but input is substantial, interrupt the speaker
        if self._is_speaking:
            # Simple heuristic: if audio is long enough, it might be an interruption
            # ideally we'd check volume/VAD here, but we receive raw chunks.
            # strict "ignore self" is safer to prevent echo, BUT user wants interruption.
            # We rely on Frontend VAD to only send "Speech".
            
            # If we receive speech while speaking, it implies user is shouting over us?
            # Or it's echo. 
            # Risk: Echo cancellation isn't perfect.
            # Compromise: We check if it's a stop command specifically? 
            # No, that requires transcription.
            
            # Let's transcribe even if speaking, to check for "Stop".
            pass
        
        # Cooldown check: Ignore audio captured shortly after Saksham finished speaking
        # This prevents echo/self-listening from being processed as user input
        import time as _time
        if self._speech_end_time > 0:
            elapsed = _time.time() - self._speech_end_time
            if elapsed < self._speech_cooldown_seconds:
                logger.debug(f"🔇 Ignoring audio during cooldown ({elapsed:.2f}s < {self._speech_cooldown_seconds}s)")
                return {"transcription": "", "ignored": True, "reason": "cooldown"}
            
        if len(audio_data) < self._min_audio_bytes:
            return {"transcription": "", "ignored": True, "reason": "too_short"}

        # During the explicit admin ceremony the next utterance is treated as
        # a single opaque proof blob. It is never sent through normal chat
        # transcription or persisted in conversation history.
        admin_result = await get_admin_auth_service().consume_active_audio(
            audio_data, conversation_id=conversation_id
        )
        if admin_result is not None:
            await get_cognition_bus().publish(CognitionMessage(
                type=(MessageType.ADMIN_ACTIVATION_VERIFIED if admin_result.get("verified") else MessageType.ADMIN_ACTIVATION_FAILED),
                payload={key: value for key, value in admin_result.items() if key not in {"capability"}},
                source="voice_processor",
                target="planner",
            ))
            await get_cognition_bus().publish(CognitionMessage(
                type=MessageType.ADMIN_ACTIVATION_RESULT,
                payload=admin_result,
                source="voice_processor",
                target="planner",
            ))
            return {"transcription": "", "admin_activation": admin_result, "session_active": True}
            
        try:
            # 1. Transcribe
            transcription = await self.llm.transcribe(audio_data)
            
            if not transcription or not transcription.strip():
                return {"transcription": "", "ignored": True, "reason": "silence"}
            
            text = transcription.strip()
            text_lower = text.strip().lower().replace(".", "").replace("!", "").replace("?", "")
            
            # Garbage Filter: Reject known false positive patterns
            garbage_phrases = [
                "you", "um", "uh", "hmm", "hm", "ah", "oh", "okay", "ok",
                "thank you", "thanks", "bye", "the", "a", "an", "it", "is",
                "i", "we", "he", "she", "they", "this", "that", "what",
                "yes", "no", "yeah", "nope", "yep", "right", "sure",
                "music", "playing", "so", "and", "but", "or", "if",
                "...", "♪", "subtitle", "subtitles",
            ]
            
            # Reject exact matches to garbage phrases (unless it contains wake word)
            has_wake_word = any(re.search(p, text_lower) for p in WAKE_PATTERNS)
            
            if text_lower in garbage_phrases and not has_wake_word:
                logger.debug(f"🗑️ Rejected garbage phrase: '{text}'")
                return {"transcription": text, "ignored": True, "reason": "garbage_phrase"}
            
            # Reject very short transcriptions (1-2 words) unless they contain wake word
            word_count = len(text.split())
            if word_count <= 2 and not has_wake_word and not self.session_active:
                logger.debug(f"🗑️ Rejected short transcription: '{text}' ({word_count} words)")
                return {"transcription": text, "ignored": True, "reason": "too_few_words"}
            
            conversation_speaking = (
                self._is_speaking
                or conversation_id in self._playback_conversations
            )
            logger.info(
                f"👂 Heard: '{text}' "
                f"(Session: {self.session_active}, Speaking: {conversation_speaking})"
            )
            
            # Barge-in Check: If we were speaking, did they say stop?
            if conversation_speaking:
                if is_barge_in_stop(text):
                    logger.info(f"🛑 Interruption detected: {text}")
                    await self._productivity.stop_speaking()
                    self._is_speaking = False
                    self._playback_conversations.discard(conversation_id)
                    self._speech_end_time = time.time()
                    return {
                        "transcription": text,
                        "ignored": True,
                        "reason": "interrupted",
                        "interrupted": True,
                        "playback_action": "stop",
                        "should_respond": False,
                        "session_active": True,
                    }
                else:
                     # It was likely echo or non-stop speech. Ignore to be safe.
                     return {"transcription": text, "ignored": True, "reason": "speaking_ignored"}

            
            should_process = False
            is_wake_word = False
            
            # 2. Check Wake Word / Session Logic
            
            # Check for Stop/Wait commands (Normal flow)
            stop_phrases = [
                "stop", "stop talking", "shut up", "cancel", "thanks", "thank you", 
                "bye", "goodbye", "that is all", "that's it", "wait", "hold on", "pause"
            ]
            
            # semantic check: exact match or starts with key phrase
            if any(text_lower == s or text_lower.startswith(s + " ") for s in stop_phrases):
                await self._productivity.stop_speaking()
                # Determine intent
                if text_lower in ["wait", "hold on", "pause"]:
                    logger.info("⏸️  Pausing session per user request")
                    # Just end session, user will wake up when ready
                    self.end_session()
                    return {"transcription": text, "ignored": True, "reason": "user_paused"}
                else:
                    # Closing
                    if self.session_active:
                        self.end_session()
                        return {"transcription": text, "ignored": True, "reason": "session_end"}
            
            # Check Wake Word
            
            # Check Wake Word
            for pattern in WAKE_PATTERNS:
                if re.search(pattern, text_lower):
                    is_wake_word = True
                    break
            
            if is_wake_word:
                self.start_session()
                should_process = True
                
            elif self.session_active:
                self.extend_session()
                should_process = True
            
            elif self.conversation_mode:
                self.start_session() # Keep active
                should_process = True
            
            if not should_process:
                return {
                    "transcription": text, 
                    "ignored": True, 
                    "reason": "no_wake_word",
                    "should_respond": False,
                    "session_active": False
                }
            
            # 3. Buffer related fragments before publishing a complete turn.
            turn_update = await self._dialogue.submit_fragment(
                conversation_id,
                text,
                sequence,
            )
            
            return {
                "transcription": text,
                "merged_transcription": turn_update.merged_text,
                "fragment_count": turn_update.fragment_count,
                "nlu": turn_update.nlu.to_dict(),
                "sent_to_brain": False,
                "pending_turn": True,
                "should_respond": False,
                "session_active": True,
            }
            
        except Exception as e:
            logger.error(f"Audio processing error: {e}")
            return {"error": str(e)}

    async def _commit_conversation_turn(self, turn: CommittedTurn) -> None:
        """Publish one coherent user turn after related fragments settle."""
        bus = get_cognition_bus()
        payload = turn.to_payload()
        payload["source"] = "voice"
        payload["conversation_context"] = await self._dialogue.recent_context(
            turn.conversation_id
        )

        await bus.publish(CognitionMessage(
            type=MessageType.USER_TURN_COMMITTED,
            payload=payload,
            source="dialogue_manager",
        ))
        await bus.publish(CognitionMessage(
            type=MessageType.USER_INPUT,
            payload=payload,
            source="voice_processor",
            target="planner",
            requires_response=False,
        ))

    async def set_conversation_mode(self, enabled: bool) -> None:
        """Enable or disable always-on conversation mode"""
        self.conversation_mode = enabled
        logger.info(f"🗣️ Conversation Mode set to: {enabled}")
        if enabled:
            self.start_session()
        else:
            self._session_active = False # Reset session when leaving mode


    async def text_to_speech(self, text: str, tone: str = "neutral") -> bytes:
        """Convert text to speech with the configured provider and safe fallbacks."""
        self._is_speaking = True
        normalized_tone = tone if tone in TONE_PROFILES else "neutral"
        profile = TONE_PROFILES[normalized_tone]
        try:
            if not settings.tts_enabled:
                return b""

            speech_text = await get_user_profile().prepare_speech(text)
            if speech_text != text:
                logger.info("Applied saved pronunciation preferences to TTS output")
            
            provider = settings.tts_provider

            if provider == "chatterbox":
                try:
                    from core.chatterbox_client import ChatterboxTTSClient

                    api_key = ""
                    if settings.chatterbox_tts_api_key:
                        api_key = settings.chatterbox_tts_api_key.get_secret_value()
                    client = ChatterboxTTSClient(
                        settings.chatterbox_tts_url,
                        api_key=api_key,
                        voice=settings.chatterbox_tts_voice,
                        timeout_seconds=settings.chatterbox_tts_timeout_seconds,
                    )
                    logger.info(
                        f"⚡ Generating audio with Chatterbox Turbo "
                        f"({settings.chatterbox_tts_voice}, tone={normalized_tone})..."
                    )
                    audio_data = await client.synthesize(speech_text, tone=normalized_tone)
                    logger.info(f"🔊 Chatterbox Turbo generated {len(audio_data)} bytes")
                    return audio_data
                except Exception as e:
                    provider = settings.chatterbox_tts_fallback
                    if provider == "none":
                        logger.error(
                            f"Chatterbox TTS failed: {e}; audio skipped to preserve "
                            "Saksham's configured voice"
                        )
                        return b""
                    logger.warning(f"Chatterbox TTS failed: {e}; falling back to {provider}")
            
            # Edge Neural is substantially more natural than the local Piper voice.
            if provider == "edge":
                try:
                    import edge_tts
                    import io
                    
                    logger.info(f"⚡ Generating audio with Edge TTS ({settings.edge_tts_voice})...")
                    communicate = edge_tts.Communicate(
                        speech_text,
                        settings.edge_tts_voice,
                        rate=profile["rate"],
                        pitch=profile["pitch"],
                    )
                    audio_buffer = io.BytesIO()
                    
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            audio_buffer.write(chunk["data"])
                    
                    audio_data = audio_buffer.getvalue()
                    if not audio_data:
                        raise RuntimeError("Edge TTS returned no audio")
                    logger.info(f"🔊 Edge TTS Generated {len(audio_data)} bytes")
                    return audio_data
                except Exception as e:
                    logger.warning(f"Edge TTS failed: {e}, falling back to local Piper")
                    provider = "piper"

            # Piper TTS is fully local and remains available when Edge is offline.
            if provider == "piper":
                try:
                    if not self._piper_voice:
                        self._load_piper_model(force=True)
                    if not self._piper_voice:
                        raise RuntimeError("Piper voice model is unavailable")

                    import io
                    import wave
                    
                    logger.info("⚡ Generating audio with Piper...")
                    audio_buffer = io.BytesIO()
                    with wave.open(audio_buffer, "wb") as wav_file:
                        if hasattr(self._piper_voice, "synthesize_wav"):
                            from piper.config import SynthesisConfig

                            self._piper_voice.synthesize_wav(
                                speech_text,
                                wav_file,
                                syn_config=SynthesisConfig(
                                    length_scale=profile["length_scale"],
                                ),
                            )
                        else:
                            wav_file.setnchannels(1)
                            wav_file.setsampwidth(2)
                            wav_file.setframerate(self._piper_voice.config.sample_rate)
                            for chunk in self._piper_voice.synthesize_stream_raw(speech_text):
                                wav_file.writeframes(chunk)
                    return audio_buffer.getvalue()
                except Exception as e:
                    logger.error(f"Piper TTS failed: {e}, falling back to OpenAI")
                    provider = "openai"
            
            # OpenAI TTS (best quality, ~1-3s latency)
            if provider == "openai":
                tts_api_key = None
                if settings.openai_tts_api_key:
                    tts_api_key = settings.openai_tts_api_key.get_secret_value()
                elif settings.llm_api_key:
                    api_key = settings.llm_api_key.get_secret_value()
                    if api_key.startswith("sk-"):
                        tts_api_key = api_key
                
                if tts_api_key:
                    try:
                        logger.info(f"🎤 Generating audio with OpenAI TTS ({settings.tts_voice})...")
                        client = AsyncOpenAI(api_key=tts_api_key)
                        response = await client.audio.speech.create(
                            model="tts-1",
                            voice=settings.tts_voice,
                            input=speech_text
                        )
                        audio_data = response.content
                        logger.info(f"🔊 OpenAI TTS Generated {len(audio_data)} bytes")
                        return audio_data
                    except Exception as e:
                        logger.error(f"OpenAI TTS failed: {e}")
            
            # Fallback to Mac System TTS
            logger.info("🤖 Using Mac System TTS (Fallback)...")
            await self._productivity.speak_system(speech_text)
            return b""
        finally:
            self._is_speaking = False
            self._speech_end_time = time.time()  # Start cooldown timer
            self._last_response_time = time.time()


# Global instance
_voice_processor: Optional[VoiceProcessor] = None

def get_voice_processor() -> VoiceProcessor:
    global _voice_processor
    if _voice_processor is None:
        _voice_processor = VoiceProcessor()
    return _voice_processor
