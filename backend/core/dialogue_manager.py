"""Conversation turn buffering and related-fragment merging."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from loguru import logger

from core.nlu import NLUEngine, NLUResult, RelationshipResult, get_nlu_engine


@dataclass(frozen=True)
class CommittedTurn:
    conversation_id: str
    text: str
    nlu: NLUResult
    fragments: tuple[str, ...]
    created_at: float

    def to_payload(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "text": self.text,
            "nlu": self.nlu.to_dict(),
            "fragments": list(self.fragments),
            "fragment_count": len(self.fragments),
        }


@dataclass(frozen=True)
class TurnUpdate:
    pending: bool
    merged_text: str
    fragment_count: int
    nlu: NLUResult
    relationship: Optional[RelationshipResult] = None

    def to_dict(self) -> dict:
        return {
            "pending": self.pending,
            "merged_text": self.merged_text,
            "fragment_count": self.fragment_count,
            "nlu": self.nlu.to_dict(),
            "relationship": self.relationship.to_dict() if self.relationship else None,
        }


@dataclass
class _ConversationState:
    pending: list[NLUResult] = field(default_factory=list)
    pending_started_at: float = 0.0
    last_fragment_at: float = 0.0
    latest_received_sequence: int = 0
    latest_processed_sequence: int = 0
    speech_active: bool = False
    settle_task: Optional[asyncio.Task] = None
    recent_turns: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=10))


CommitHandler = Callable[[CommittedTurn], Awaitable[None]]


class DialogueManager:
    """Build complete user turns from individually transcribed speech chunks."""

    def __init__(
        self,
        *,
        nlu: Optional[NLUEngine] = None,
        settle_seconds: float = 0.85,
        incomplete_settle_seconds: float = 2.0,
        on_commit: Optional[CommitHandler] = None,
    ):
        self._nlu = nlu or get_nlu_engine()
        self._settle_seconds = settle_seconds
        self._incomplete_settle_seconds = incomplete_settle_seconds
        self._on_commit = on_commit
        self._states: dict[str, _ConversationState] = {}
        self._lock = asyncio.Lock()

    def set_commit_handler(self, handler: CommitHandler) -> None:
        self._on_commit = handler

    async def note_audio_received(self, conversation_id: str, sequence: int) -> None:
        """Record queued audio before transcription starts and pause turn commit."""
        async with self._lock:
            state = self._get_state(conversation_id)
            state.speech_active = False
            state.latest_received_sequence = max(state.latest_received_sequence, sequence)
            self._cancel_settle_task(state)

    async def note_speech_started(self, conversation_id: str) -> None:
        """Pause a pending commit as soon as client-side VAD hears a follow-up."""
        async with self._lock:
            state = self._get_state(conversation_id)
            state.speech_active = True
            self._cancel_settle_task(state)

    async def note_speech_ended(self, conversation_id: str) -> None:
        """Resume settling when a detected voice start produced no audio chunk."""
        async with self._lock:
            state = self._get_state(conversation_id)
            state.speech_active = False
            if (
                state.pending
                and state.latest_processed_sequence >= state.latest_received_sequence
            ):
                merged = self._nlu.merge(
                    fragment.original_text for fragment in state.pending
                )
                self._schedule_settle(
                    conversation_id,
                    state,
                    self._nlu.analyze(merged),
                )

    async def submit_fragment(
        self,
        conversation_id: str,
        text: str,
        sequence: int,
    ) -> TurnUpdate:
        analysis = self._nlu.analyze(text)
        now = time.monotonic()
        commit_before_current: Optional[CommittedTurn] = None
        relationship: Optional[RelationshipResult] = None

        async with self._lock:
            state = self._get_state(conversation_id)

            if state.pending:
                previous_analysis = self._nlu.analyze(
                    self._nlu.merge(fragment.original_text for fragment in state.pending)
                )
                relationship = self._nlu.relationship(
                    previous_analysis,
                    analysis,
                    gap_seconds=max(0.0, now - state.last_fragment_at),
                )
                if not relationship.related:
                    commit_before_current = self._take_pending_turn(conversation_id, state)

            if not state.pending:
                state.pending_started_at = now

            state.pending.append(analysis)
            state.last_fragment_at = now
            state.latest_processed_sequence = max(state.latest_processed_sequence, sequence)
            merged_text = self._nlu.merge(fragment.original_text for fragment in state.pending)
            merged_analysis = self._nlu.analyze(merged_text)

            if (
                not state.speech_active
                and state.latest_processed_sequence >= state.latest_received_sequence
            ):
                self._schedule_settle(conversation_id, state, merged_analysis)

            update = TurnUpdate(
                pending=True,
                merged_text=merged_text,
                fragment_count=len(state.pending),
                nlu=merged_analysis,
                relationship=relationship,
            )

        if commit_before_current:
            await self._deliver(commit_before_current)

        return update

    async def mark_audio_processed(self, conversation_id: str, sequence: int) -> None:
        """Allow a pending turn to settle when an ignored audio chunk finishes."""
        async with self._lock:
            state = self._get_state(conversation_id)
            state.latest_processed_sequence = max(state.latest_processed_sequence, sequence)
            if (
                state.pending
                and not state.speech_active
                and state.latest_processed_sequence >= state.latest_received_sequence
            ):
                merged = self._nlu.merge(fragment.original_text for fragment in state.pending)
                self._schedule_settle(conversation_id, state, self._nlu.analyze(merged))

    async def flush(self, conversation_id: str) -> Optional[CommittedTurn]:
        async with self._lock:
            state = self._states.get(conversation_id)
            if not state or not state.pending:
                return None
            turn = self._take_pending_turn(conversation_id, state)

        await self._deliver(turn)
        return turn

    async def record_assistant_turn(self, conversation_id: str, text: str) -> None:
        if not text.strip():
            return
        async with self._lock:
            self._get_state(conversation_id).recent_turns.append(("assistant", text.strip()))

    async def recent_context(self, conversation_id: str) -> list[dict[str, str]]:
        async with self._lock:
            state = self._states.get(conversation_id)
            if not state:
                return []
            return [{"role": role, "content": content} for role, content in state.recent_turns]

    async def shutdown(self) -> None:
        async with self._lock:
            tasks = [state.settle_task for state in self._states.values() if state.settle_task]
            for task in tasks:
                if task and not task.done():
                    task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _get_state(self, conversation_id: str) -> _ConversationState:
        if conversation_id not in self._states:
            self._states[conversation_id] = _ConversationState()
        return self._states[conversation_id]

    def _schedule_settle(
        self,
        conversation_id: str,
        state: _ConversationState,
        analysis: NLUResult,
    ) -> None:
        self._cancel_settle_task(state)
        delay = self._settle_seconds if analysis.is_complete else self._incomplete_settle_seconds
        state.settle_task = asyncio.create_task(self._flush_after_delay(conversation_id, delay))

    def _cancel_settle_task(self, state: _ConversationState) -> None:
        task = state.settle_task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        state.settle_task = None

    async def _flush_after_delay(self, conversation_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self.flush(conversation_id)
        except asyncio.CancelledError:
            return
        except Exception as error:
            logger.error(f"Dialogue turn commit failed: {error}")

    def _take_pending_turn(
        self,
        conversation_id: str,
        state: _ConversationState,
    ) -> CommittedTurn:
        self._cancel_settle_task(state)
        fragments = tuple(fragment.original_text for fragment in state.pending)
        text = self._nlu.merge(fragments)
        analysis = self._nlu.analyze(text)
        created_at = state.pending_started_at or time.monotonic()
        state.pending = []
        state.pending_started_at = 0.0
        state.last_fragment_at = 0.0
        state.recent_turns.append(("user", text))
        return CommittedTurn(
            conversation_id=conversation_id,
            text=text,
            nlu=analysis,
            fragments=fragments,
            created_at=created_at,
        )

    async def _deliver(self, turn: CommittedTurn) -> None:
        logger.info(
            f"🧩 Committed conversation turn with {len(turn.fragments)} fragment(s): "
            f"'{turn.text}'"
        )
        if self._on_commit:
            await self._on_commit(turn)


_dialogue_manager: Optional[DialogueManager] = None


def get_dialogue_manager() -> DialogueManager:
    global _dialogue_manager
    if _dialogue_manager is None:
        from config import get_settings

        settings = get_settings()
        _dialogue_manager = DialogueManager(
            settle_seconds=settings.voice_turn_settle_seconds,
            incomplete_settle_seconds=settings.voice_incomplete_turn_settle_seconds,
        )
    return _dialogue_manager
