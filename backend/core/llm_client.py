"""
Saksham AI - LLM Client
Unified interface for OpenAI GPT-5.2 Thinking model
"""

import json
from typing import Any, AsyncGenerator, Optional

import io
import asyncio
import tempfile
import os
import httpx
from openai import AsyncOpenAI

from faster_whisper import WhisperModel
from loguru import logger

from config import get_settings


settings = get_settings()


class LLMClient:
    """
    Unified LLM client for Saksham AI.
    Supports OpenAI and Ollama (Local) providers.
    """
    
    def __init__(self):
        self._openai_client: Optional[AsyncOpenAI] = None

        self._whisper_model: Optional[WhisperModel] = None
        self._whisper_model: Optional[WhisperModel] = None

    async def complete(
        self,
        messages: list[dict[str, str]],
        system_prompt: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        **kwargs
    ) -> dict[str, Any]:
        """
        Comparison wrapper for 'chat' to maintain compatibility with BaseAgent.
        """
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)
        
        # Extract known kwargs for chat, pass others if needed or ignore
        tool_choice = kwargs.get("tool_choice")
        temperature = kwargs.get("temperature")
        model = kwargs.get("model") or kwargs.get("provider") # provider is sometimes passed as model switch
        
        # If provider was "openai" passed in kwargs, we might need to handle it?
        # The new chat method handles provider selection internally based on key presence.
        # But if 'provider' kwarg is 'openai', we might want to force it?
        # For now, let's trust the chat method's logic or pass 'model' if it's a model name.
        
        return await self.chat(
            messages=full_messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            model=model if model not in ["openai", "ollama"] else None,
            max_tokens=kwargs.get("max_tokens"),
        )

    async def chat(
        self,
        messages: list[dict[str, str]],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Chat completion with the LLM (OpenAI / LM Studio Only).
        """
        # Ensure we have an OpenAI client
        if not self._openai_client:
            self._openai_client = AsyncOpenAI(
                api_key=settings.llm_api_key.get_secret_value(),
                base_url=settings.llm_base_url if settings.llm_base_url else None,
                timeout=httpx.Timeout(120.0, connect=10.0),
            )
        
        try:
            response = await self._openai_client.chat.completions.create(
                model=model or settings.llm_model,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                temperature=(
                    temperature if temperature is not None else settings.llm_temperature
                ),
                max_tokens=max_tokens if max_tokens is not None else settings.llm_max_tokens,
            )
            return response.model_dump()
            
        except Exception as e:
            logger.error(f"LLM completion error (openai): {e}")
            raise e
    
    async def stream(
        self,
        messages: list[dict[str, str]],
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Stream a completion from the LLM (OpenAI / LM Studio Only).
        Yields chunks of text as they arrive.
        """
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)
        
        # Ensure we have an OpenAI client
        if not self._openai_client:
           self._openai_client = AsyncOpenAI(
                api_key=settings.llm_api_key.get_secret_value(),
                base_url=settings.llm_base_url if settings.llm_base_url else None,
                timeout=httpx.Timeout(120.0, connect=10.0),
            )

        try:
            stream = await self._openai_client.chat.completions.create(
                model=settings.llm_model,
                messages=full_messages,
                temperature=temperature or settings.llm_temperature,
                max_tokens=settings.llm_max_tokens,
                stream=True,
            )
            
            async for chunk in stream:
                if chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
                    
        except Exception as e:
            logger.error(f"LLM stream error (openai): {e}")
            raise
    
    async def transcribe(self, audio_data: bytes, language: Optional[str] = "en") -> str:
        """
        Transcribe audio to text using Whisper (API or Local).
        """
        # Determine strategy
        # If set to OpenAI/LM Studio, check if we can actually send audio
        # LM Studio often doesn't support audio endpoints yet, so we should default to local Whisper if using LM Studio
        # UNLESS the user explicitly wants to try sending it relative to base_url
        
        # Current logic: If base_url implies LM Studio (localhost), prefer Local Faster-Whisper to avoid 404/415
        # If base_url is None (Cloud OpenAI), use Cloud
        
        use_cloud_whisper = False
        api_key = settings.llm_api_key.get_secret_value()
        
        if api_key and not api_key.startswith("lm-studio"):
             # Real Key -> Cloud OpenAI likely
             use_cloud_whisper = True
        
        # Overrides? 
        # For now, let's Stick to: 
        # - Real OpenAI Key -> Cloud Whisper
        # - LM Studio / No Key -> Local Faster Whisper
        
        try:
            if use_cloud_whisper:
                client = self._openai_client
                if not client:
                    client = AsyncOpenAI(api_key=api_key)
                    self._openai_client = client
                
                logger.info(f"🎤 Transcribing audio bytes ({len(audio_data)} bytes) via OpenAI API...")
                
                # OpenAI requires filename
                request = {
                    "model": "whisper-1",
                    "file": ("audio.wav", audio_data, "audio/wav"),
                }
                if language:
                    request["language"] = language
                transcription = await client.audio.transcriptions.create(**request)
                logger.info(f"📝 Transcription result: '{transcription.text}'")
                return transcription.text
            else:
                # Use local Faster Whisper
                # Run in thread pool to avoid blocking
                
                def _run_local_whisper():
                    if self._whisper_model is None:
                        logger.info(f"Loading local Whisper model: {settings.whisper_model}") # Ensure settings.whisper_model exists
                        self._whisper_model = WhisperModel(settings.whisper_model or "base", device="cpu", compute_type="int8")
                    
                    # Write to temp file for robustness
                    try:
                        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                            tmp.write(audio_data)
                            tmp_path = tmp.name
                        
                        logger.debug(f"Transcribing temp file: {tmp_path}")
                        segments, _ = self._whisper_model.transcribe(
                            tmp_path,
                            language=language or None,
                            beam_size=5,
                        )
                        text = " ".join([segment.text for segment in segments]).strip()
                        logger.info(f"👂 Local Whisper heard: '{text}'")
                        return text
                    finally:
                        # Clean up temp file
                        if 'tmp_path' in locals() and os.path.exists(tmp_path):
                            os.unlink(tmp_path)

                return await asyncio.to_thread(_run_local_whisper)
            
        except Exception as e:
            logger.error(f"Transcription error: {e}")
            raise
    
    async def speak(self, text: str, voice: str = "alloy") -> bytes:
        """
        Convert text to speech using OpenAI TTS.
        Always uses OpenAI for TTS (quality), even if LLM is local.
        Returns audio bytes.
        """
        try:
            # Ensure we have an OpenAI client for TTS
            client = self._openai_client
            if client is None:
                api_key = settings.llm_api_key.get_secret_value()
                if api_key:
                    client = AsyncOpenAI(api_key=api_key)
                    self._openai_client = client
            
            if client is None:
                logger.warning("OpenAI API key missing - cannot generate TTS")
                return b""

            response = await client.audio.speech.create(
                model="tts-1",
                voice=voice,
                input=text,
            )
            return response.content
            
        except Exception as e:
            logger.error(f"TTS error: {e}")
            raise
    
    def get_agent_tools(self, agent_type: str) -> list[dict]:
        """
        Get the tool definitions for a specific agent type.
        These enable the LLM to call agent functions.
        """
        
        tools_by_agent = {
            "planner": [
                {
                    "type": "function",
                    "function": {
                        "name": "create_plan",
                        "description": "Create a multi-step plan to accomplish a goal",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "goal": {
                                    "type": "string",
                                    "description": "The goal to achieve",
                                },
                                "steps": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "action": {"type": "string"},
                                            "target": {"type": "string"},
                                            "parameters": {"type": "object"},
                                        },
                                        "required": ["action"],
                                    },
                                    "description": "The steps to execute",
                                },
                            },
                            "required": ["goal", "steps"],
                        },
                    },
                },
            ],
            "executor": [
                {
                    "type": "function",
                    "function": {
                        "name": "execute_mac_command",
                        "description": "Execute a command on MacOS",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": [
                                        "open_app",
                                        "close_app",
                                        "run_script",
                                        "file_operation",
                                        "browser_action",
                                        "terminal_command",
                                        "play_music",
                                        "music_control",
                                    ],
                                },
                                "target": {"type": "string"},
                                "parameters": {"type": "object"},
                            },
                            "required": ["action"],
                        },
                    },
                },
            ],
        }
        
        return tools_by_agent.get(agent_type, [])


# Global LLM client instance
_llm_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """Get or create the global LLM client instance"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
