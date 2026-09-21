"""Durable user preferences that must affect Saksham's behavior directly."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from core.state_store import StateStore, get_state_store
from memory.nova_memory import SemanticEntry, get_nova_memory


@dataclass(frozen=True)
class PronunciationUpdate:
    """A confirmed written-to-spoken preference supplied by the user."""

    written: str
    spoken: str


class UserProfile:
    """Local, persistent user profile with a pronunciation lexicon for TTS."""

    _STORAGE_KEY = "user_profile.v1"
    _MAX_TERM_LENGTH = 60
    _NON_NAME_WORDS = {
        "fine", "good", "great", "okay", "ok", "sad", "happy", "tired",
        "frustrated", "worried", "working", "ready", "here", "back",
    }

    def __init__(
        self,
        store: Optional[StateStore] = None,
        semantic_memory: Optional[Any] = None,
    ) -> None:
        self._store = store or get_state_store()
        self._semantic_memory = semantic_memory
        self._profile: dict[str, Any] = {
            "preferred_name": "",
            "pronunciations": {},
        }
        self._loaded = False
        self._lock = asyncio.Lock()

    @staticmethod
    def is_pronunciation_instruction(text: str) -> bool:
        """Detect explicit name or pronunciation information worth learning."""
        normalized = str(text or "").lower()
        return bool(re.search(
            r"\bpronounce\b|\bpronounced\b|\bsay\s+.+?\s+as\b|\b(?:my\s+name\s+is|call\s+me)\b",
            normalized,
        )) or UserProfile._extract_introduction_name(text) is not None

    @staticmethod
    def _extract_introduction_name(text: str) -> Optional[str]:
        """Extract a name only from explicit introductions, not ordinary `I am` statements."""
        source = re.sub(r"\s+", " ", str(text or "")).strip()
        explicit = re.search(
            r"\b(?:my\s+name\s+is|call\s+me)\s+([A-Za-z][A-Za-z0-9'-]{0,59})",
            source,
            flags=re.IGNORECASE,
        )
        match = explicit
        if not match and re.search(r"\b(?:hi|hello|hey|namaste)\b", source, flags=re.IGNORECASE):
            match = re.search(
                r"\b(?:this\s+is|i\s+am|i'm|it\s+is|it's)\s+"
                r"([A-Za-z][A-Za-z0-9'-]{0,59})",
                source,
                flags=re.IGNORECASE,
            )
        if not match:
            return None

        name = UserProfile._clean_term(match.group(1))
        if not name or name.casefold() in UserProfile._NON_NAME_WORDS:
            return None
        return name

    @staticmethod
    def _clean_term(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9 &'/-]+", " ", str(value or ""))
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,!?:;\"'")
        return cleaned[:UserProfile._MAX_TERM_LENGTH]

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return

        async with self._lock:
            if self._loaded:
                return
            raw_profile = await self._store.get_runtime_value(self._STORAGE_KEY)
            if raw_profile:
                try:
                    candidate = json.loads(raw_profile)
                    if isinstance(candidate, dict):
                        pronunciations = candidate.get("pronunciations", {})
                        self._profile = {
                            "preferred_name": str(candidate.get("preferred_name", "")),
                            "pronunciations": pronunciations if isinstance(pronunciations, dict) else {},
                        }
                except json.JSONDecodeError:
                    pass
            self._loaded = True

    async def _persist(self) -> None:
        await self._store.set_runtime_value(
            self._STORAGE_KEY,
            json.dumps(self._profile, ensure_ascii=True),
        )

    async def get_context(self) -> dict[str, Any]:
        """Return the profile context that belongs in planning prompts."""
        await self._ensure_loaded()
        entries = list(self._profile["pronunciations"].values())
        return {
            "preferred_name": self._profile.get("preferred_name", ""),
            "pronunciations": entries,
        }

    async def set_pronunciation(self, written: str, spoken: str) -> Optional[PronunciationUpdate]:
        """Save a user-supplied pronunciation and record it as semantic memory."""
        written = self._clean_term(written)
        spoken = self._clean_term(spoken)
        if not written or not spoken:
            return None

        await self._ensure_loaded()
        key = written.casefold()
        update = PronunciationUpdate(written=written, spoken=spoken)
        existing = self._profile["pronunciations"].get(key)
        self._profile["pronunciations"][key] = {
            "written": written,
            "spoken": spoken,
        }
        if not self._profile.get("preferred_name"):
            self._profile["preferred_name"] = written
        await self._persist()

        if existing != self._profile["pronunciations"][key]:
            await self._store.create_memory(
                content=f"User pronunciation preference: say {written} as {spoken}.",
                memory_type="user_preference",
                metadata={
                    "kind": "pronunciation",
                    "written": written,
                    "spoken": spoken,
                    "source": "user_correction",
                },
            )
            if self._semantic_memory is not None:
                await self._semantic_memory.store_fact(SemanticEntry(
                    fact=f"User pronunciation preference: say {written} as {spoken}.",
                    category="user_preference",
                    confidence=1.0,
                    source="user_correction",
                ))
        return update

    async def set_preferred_name(self, name: str) -> Optional[str]:
        """Persist an explicitly supplied user name as durable profile memory."""
        name = self._clean_term(name)
        if not name or name.casefold() in self._NON_NAME_WORDS:
            return None

        await self._ensure_loaded()
        previous = str(self._profile.get("preferred_name", ""))
        self._profile["preferred_name"] = name
        await self._persist()
        if previous.casefold() == name.casefold():
            return name

        await self._store.create_memory(
            content=f"The user's preferred name is {name}.",
            memory_type="user_preference",
            metadata={"kind": "preferred_name", "name": name, "source": "user_introduction"},
        )
        if self._semantic_memory is not None:
            await self._semantic_memory.store_fact(SemanticEntry(
                fact=f"The user's preferred name is {name}.",
                category="user_preference",
                confidence=1.0,
                source="user_introduction",
            ))
        return name

    async def learn_from_user_text(self, text: str) -> Optional[PronunciationUpdate]:
        """Learn only explicit pronunciation corrections from a user message."""
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        if not normalized:
            return None

        await self._ensure_loaded()
        named_match = re.search(
            r"\b(?:pronounce|say)\s+(?P<written>[A-Za-z][A-Za-z0-9 &'/-]{0,59}?)\s+as\s+(?P<spoken>[A-Za-z][A-Za-z0-9 &'/-]{0,59})(?:[.!?]|$)",
            normalized,
            flags=re.IGNORECASE,
        )
        if named_match and named_match.group("written").casefold() not in {"my name", "it"}:
            return await self.set_pronunciation(
                named_match.group("written"),
                named_match.group("spoken"),
            )

        contextual_match = re.search(
            r"\b(?:pronounce|say)\s+(?:my\s+name|it)\s+as\s+(?P<spoken>[A-Za-z][A-Za-z0-9 &'/-]{0,59})(?:[.!?]|$)",
            normalized,
            flags=re.IGNORECASE,
        )
        preferred_name = str(self._profile.get("preferred_name", ""))
        if contextual_match and preferred_name:
            return await self.set_pronunciation(
                preferred_name,
                contextual_match.group("spoken"),
            )

        introduction_match = re.search(
            r"\b(?:my\s+name\s+is|call\s+me)\s+(?P<written>[A-Za-z][A-Za-z0-9'-]{0,59})(?:\s*(?:,|and)?\s*(?:is\s+)?pronounced\s+(?P<spoken>[A-Za-z][A-Za-z0-9 &'/-]{0,59}))?(?:[.!?]|$)",
            normalized,
            flags=re.IGNORECASE,
        )
        written = self._extract_introduction_name(normalized)
        if not written:
            return None
        await self.set_preferred_name(written)
        spoken = introduction_match.group("spoken") if introduction_match else None
        if spoken:
            return await self.set_pronunciation(written, spoken)
        return None

    async def prepare_speech(self, text: str) -> str:
        """Substitute only saved pronunciation terms in text sent to a TTS provider."""
        await self._ensure_loaded()
        result = str(text or "")
        entries = sorted(
            self._profile["pronunciations"].values(),
            key=lambda entry: len(str(entry.get("written", ""))),
            reverse=True,
        )
        for entry in entries:
            written = self._clean_term(str(entry.get("written", "")))
            spoken = self._clean_term(str(entry.get("spoken", "")))
            if not written or not spoken:
                continue
            result = re.sub(
                rf"(?<!\w){re.escape(written)}(?!\w)",
                spoken,
                result,
                flags=re.IGNORECASE,
            )
        return result


_user_profile: Optional[UserProfile] = None


def get_user_profile() -> UserProfile:
    """Return the singleton local user profile."""
    global _user_profile
    if _user_profile is None:
        _user_profile = UserProfile(semantic_memory=get_nova_memory())
    return _user_profile
