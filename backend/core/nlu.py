"""Fast, deterministic natural-language analysis for conversation routing.

The planner LLM remains responsible for nuanced reasoning. This module supplies
low-latency signals needed before an LLM call: turn completeness, continuation
markers, references, likely actions, entities, and fragment relationships.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional


class SpeechAct(str, Enum):
    QUESTION = "question"
    COMMAND = "command"
    CONFIRMATION = "confirmation"
    REJECTION = "rejection"
    CORRECTION = "correction"
    STATEMENT = "statement"
    GREETING = "greeting"
    GRATITUDE = "gratitude"
    FAREWELL = "farewell"


class Affect(str, Enum):
    NEUTRAL = "neutral"
    POSITIVE = "positive"
    EXCITED = "excited"
    CONCERNED = "concerned"
    FRUSTRATED = "frustrated"
    SAD = "sad"
    URGENT = "urgent"


@dataclass(frozen=True)
class IntentCandidate:
    action: str
    text: str
    confidence: float


@dataclass(frozen=True)
class NLUResult:
    original_text: str
    normalized_text: str
    speech_act: SpeechAct
    affect: Affect
    intents: tuple[IntentCandidate, ...]
    entities: dict[str, list[str]] = field(default_factory=dict)
    topics: tuple[str, ...] = ()
    continuation_markers: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    is_fragment: bool = False
    is_complete: bool = True
    urgency: float = 0.0
    confidence: float = 0.7

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["speech_act"] = self.speech_act.value
        result["affect"] = self.affect.value
        return result


@dataclass(frozen=True)
class RelationshipResult:
    related: bool
    score: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


CONTINUATION_PREFIXES = (
    "and",
    "and also",
    "also",
    "plus",
    "additionally",
    "in addition",
    "along with that",
    "as well",
    "then",
    "and then",
    "next",
    "after that",
    "before that",
    "once that is done",
    "while you are at it",
    "while you're at it",
    "one more thing",
    "another thing",
    "moreover",
    "furthermore",
    "besides",
    "but",
    "however",
    "instead",
    "actually",
    "or",
    "otherwise",
    "regarding that",
    "about that",
    "for that",
    "with that",
    "on that",
    "the same",
    "same for",
    "what about",
    "how about",
    # Common Romanized Hindi/Hinglish continuations.
    "aur",
    "aur bhi",
    "phir",
    "phir se",
    "uske baad",
    "iske baad",
    "iske saath",
    "uske saath",
    "yeh bhi",
    "wo bhi",
    "woh bhi",
    "lekin",
    "par",
    "matlab",
    "waise",
)

REFERENCE_PHRASES = (
    "it",
    "that",
    "this",
    "them",
    "they",
    "those",
    "these",
    "there",
    "the same",
    "same one",
    "same thing",
    "previous one",
    "former",
    "latter",
    "above",
    "mentioned",
    "again",
    "usko",
    "isko",
    "uska",
    "iska",
    "woh",
    "wo",
    "yeh",
)

INCOMPLETE_ENDINGS = {
    "and", "or", "but", "because", "so", "then", "also", "with", "to",
    "for", "about", "if", "when", "while", "which", "that", "like",
    "aur", "phir", "lekin", "par", "kyunki", "matlab",
}

QUESTION_STARTERS = {
    "what", "why", "when", "where", "who", "whom", "whose", "which",
    "how", "can", "could", "would", "will", "is", "are", "do", "does",
    "did", "tell", "explain",
}

CONFIRMATION_PHRASES = {
    "yes", "yeah", "yep", "sure", "okay", "ok", "go ahead", "do it",
    "confirm", "proceed", "correct", "right", "haan", "ha", "theek hai",
}

REJECTION_PHRASES = {
    "no", "nope", "cancel", "do not", "don't", "stop", "abort", "nah",
    "nahi", "mat karo",
}

GREETING_PHRASES = (
    "hi", "hello", "hey", "hiya", "good morning", "good afternoon",
    "good evening", "namaste", "namaskar",
)

GRATITUDE_PHRASES = (
    "thanks", "thank you", "thanks a lot", "thank you so much", "shukriya",
)

FAREWELL_PHRASES = (
    "bye", "goodbye", "good night", "see you", "talk later", "take care",
    "alvida",
)

CORRECTION_PREFIXES = (
    "no i mean", "i mean", "rather", "instead", "actually", "correction",
    "not that", "let me rephrase", "mera matlab", "nahi mera matlab",
)

ACTION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("compare", ("compare", "difference", "versus", "vs")),
    ("search", ("search", "find", "look up", "research")),
    ("open_app", ("open", "launch", "start")),
    ("close_app", ("close", "quit", "exit")),
    ("summarize", ("summarize", "summary", "condense")),
    ("explain", ("explain", "tell me", "describe", "what is", "who is")),
    ("create", ("create", "make", "add", "schedule", "remind", "set up")),
    ("send", ("send", "message", "email", "share")),
    ("call", ("call", "dial", "phone")),
    ("remember", ("remember", "save", "store", "note")),
    ("adjust", ("set", "change", "increase", "decrease", "raise", "lower", "turn")),
    ("media_control", ("play", "pause", "resume", "skip", "stop playing")),
    ("navigate", ("go to", "navigate", "visit")),
    ("answer", ("what", "why", "when", "where", "who", "how", "can", "could")),
)

ACTION_WORDS = {
    phrase
    for _, phrases in ACTION_PATTERNS
    for phrase in phrases
    if " " not in phrase
}

STOP_WORDS = {
    "a", "an", "the", "me", "my", "you", "your", "please", "saksham",
    "hey", "hi", "hello", "of", "in", "on", "at", "to", "for", "from",
    "with", "and", "or", "but", "also", "then", "is", "are", "was", "were",
    "be", "been", "being", "do", "does", "did", "can", "could", "would",
    "will", "should", "i", "we", "us", "our", "about", "now", "just",
    "aur", "phir", "uske", "iske", "saath", "yeh", "woh", "wo", "bhi",
} | ACTION_WORDS

AFFECT_PATTERNS: tuple[tuple[Affect, tuple[str, ...]], ...] = (
    (Affect.URGENT, ("urgent", "immediately", "right now", "asap", "jaldi", "abhi")),
    (Affect.FRUSTRATED, (
        "frustrated", "frustruated", "frusterated", "frustraded", "frustrating",
        "annoyed", "annoying", "not working", "keeps failing", "fed up",
        "gussa", "pareshan", "kaam nahi kar",
    )),
    (Affect.CONCERNED, (
        "worried", "anxious", "concerned", "scared", "afraid", "tension",
        "darr", "chinta",
    )),
    (Affect.SAD, ("sad", "upset", "hurt", "feeling low", "dukhi", "udaas")),
    (Affect.EXCITED, ("excited", "amazing", "awesome", "can't wait", "bahut excited")),
    (Affect.POSITIVE, ("happy", "great", "good news", "love it", "khush", "accha laga")),
)


def extract_music_playback_query(text: str) -> Optional[str]:
    """Recover a requested track or mood from natural and imperfect speech."""
    normalized = re.sub(r"\s+", " ", str(text or "").lower()).strip(" .!?")
    if not normalized:
        return None

    explicit = re.search(r"\b(?:play|put on|start)\s+(.+)$", normalized)
    if explicit:
        request = explicit.group(1)
    else:
        # These verbs ask for information rather than immediate playback.
        if re.search(r"\b(?:recommend|suggest|list|name|tell|explain)\b", normalized):
            return None
        request = re.sub(
            r"^(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|"
            r"i\s+(?:want|would\s+like)\s+|give\s+me\s+|get\s+me\s+)",
            "",
            normalized,
        )

    request = re.sub(r"\s+for\s+me$", "", request).strip()
    request = re.sub(r"^(?:me\s+)?(?:some\s+)?", "", request).strip()
    request = re.sub(r"^(?:the\s+)?(?:song|track)\s+(?:called\s+|named\s+)?", "", request)

    media_match = re.fullmatch(r"(.+?)\s+(?:music|songs?|tracks?|playlist)", request)
    if media_match:
        request = media_match.group(1).strip()
    elif not explicit:
        return None

    request = request.strip(" '\"-")
    if not request or request in {"music", "song", "songs", "track", "tracks", "playlist"}:
        return None
    return request


class NLUEngine:
    """Analyze utterances and decide whether adjacent fragments are related."""

    _token_pattern = re.compile(r"[a-z0-9]+(?:'[a-z]+)?", re.IGNORECASE)

    def analyze(self, text: str) -> NLUResult:
        original = " ".join(text.strip().split())
        normalized = self._normalize(original)
        tokens = self._tokens(normalized)
        continuation_markers = self._find_prefixes(normalized, CONTINUATION_PREFIXES)
        references = self._find_phrases(normalized, REFERENCE_PHRASES)
        speech_act = self._classify_speech_act(normalized, tokens)
        affect, urgency = self._detect_affect(normalized)
        intents = self._extract_intents(original, normalized)
        entities = self._extract_entities(original)
        topics = tuple(self._topic_terms(tokens))

        ends_incomplete = bool(tokens and tokens[-1] in INCOMPLETE_ENDINGS)
        short_without_action = len(tokens) <= 5 and not intents
        starts_as_continuation = bool(continuation_markers)
        is_fragment = ends_incomplete or short_without_action or starts_as_continuation
        is_complete = not ends_incomplete and not self._has_unclosed_delimiter(original)

        confidence = 0.9 if intents else 0.72
        if not original:
            confidence = 0.0
            is_complete = False
            is_fragment = True

        return NLUResult(
            original_text=original,
            normalized_text=normalized,
            speech_act=speech_act,
            affect=affect,
            intents=tuple(intents),
            entities=entities,
            topics=topics,
            continuation_markers=continuation_markers,
            references=references,
            is_fragment=is_fragment,
            is_complete=is_complete,
            urgency=urgency,
            confidence=confidence,
        )

    def relationship(
        self,
        previous: NLUResult,
        current: NLUResult,
        *,
        gap_seconds: Optional[float] = None,
    ) -> RelationshipResult:
        score = 0.0
        reasons: list[str] = []

        if current.continuation_markers:
            score += 0.55
            reasons.append("continuation_marker")

        if current.references:
            score += 0.3
            reasons.append("reference_to_prior_context")

        if not previous.is_complete:
            score += 0.45
            reasons.append("previous_fragment_incomplete")

        previous_topics = set(previous.topics)
        current_topics = set(current.topics)
        if previous_topics and current_topics:
            overlap = len(previous_topics & current_topics) / len(previous_topics | current_topics)
            if overlap:
                score += min(0.35, 0.15 + overlap * 0.35)
                reasons.append("shared_topic")

        previous_actions = {intent.action for intent in previous.intents}
        current_actions = {intent.action for intent in current.intents}
        if previous_actions & current_actions:
            score += 0.2
            reasons.append("shared_intent")

        if gap_seconds is not None:
            if gap_seconds <= 2.5:
                score += 0.12
                reasons.append("short_pause")
            elif gap_seconds > 8.0:
                score -= 0.25
                reasons.append("long_pause")

        if current.speech_act == SpeechAct.CORRECTION:
            score += 0.45
            reasons.append("correction")

        score = max(0.0, min(score, 1.0))
        return RelationshipResult(
            related=score >= 0.45,
            score=round(score, 3),
            reasons=tuple(reasons),
        )

    def merge(self, fragments: Iterable[str]) -> str:
        cleaned = [" ".join(fragment.strip().split()) for fragment in fragments if fragment.strip()]
        if not cleaned:
            return ""

        merged = cleaned[0]
        for fragment in cleaned[1:]:
            if fragment.casefold() in merged.casefold():
                continue

            is_continuation = bool(
                self._find_prefixes(self._normalize(fragment), CONTINUATION_PREFIXES)
            )
            joiner = ", " if is_continuation else ". "
            next_fragment = fragment.lstrip(" ,.")
            if is_continuation and next_fragment[:1].isupper():
                next_fragment = next_fragment[:1].lower() + next_fragment[1:]
            merged = merged.rstrip(" .!?") + joiner + next_fragment

        return merged.strip()

    def _normalize(self, text: str) -> str:
        normalized = text.casefold().replace("’", "'")
        return re.sub(r"\s+", " ", normalized).strip(" \t\r\n.,!?;")

    def _tokens(self, text: str) -> list[str]:
        return self._token_pattern.findall(text)

    def _find_prefixes(self, text: str, phrases: Iterable[str]) -> tuple[str, ...]:
        filler_trimmed = re.sub(r"^(?:um+|uh+|hmm+|okay|ok|so)\s*[,.-]?\s*", "", text)
        matches = [
            phrase
            for phrase in phrases
            if filler_trimmed == phrase or filler_trimmed.startswith(phrase + " ")
        ]
        return tuple(sorted(matches, key=len, reverse=True)[:2])

    def _find_phrases(self, text: str, phrases: Iterable[str]) -> tuple[str, ...]:
        matches = []
        for phrase in phrases:
            if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text):
                matches.append(phrase)
        return tuple(matches)

    def _classify_speech_act(self, text: str, tokens: list[str]) -> SpeechAct:
        if any(text.startswith(prefix) for prefix in CORRECTION_PREFIXES):
            return SpeechAct.CORRECTION
        if text in CONFIRMATION_PHRASES:
            return SpeechAct.CONFIRMATION
        if text in REJECTION_PHRASES:
            return SpeechAct.REJECTION
        if extract_music_playback_query(text):
            return SpeechAct.COMMAND
        if (
            tokens
            and (
                tokens[0] in QUESTION_STARTERS
                or re.search(
                    r"(?:^|[.!]\s+)(?:what|why|when|where|who|which|how|can|could|"
                    r"would|will|is|are|do|does|did|tell|explain)\b",
                    text,
                )
                or re.search(r"\b(?:do you remember me|who am i|what(?:'s| is) my name)\b", text)
            )
        ):
            return SpeechAct.QUESTION
        intents = self._extract_intents(text, text)
        if intents:
            return SpeechAct.COMMAND
        if self._starts_with_phrase(text, GREETING_PHRASES):
            return SpeechAct.GREETING
        if self._starts_with_phrase(text, GRATITUDE_PHRASES):
            return SpeechAct.GRATITUDE
        if self._starts_with_phrase(text, FAREWELL_PHRASES):
            return SpeechAct.FAREWELL
        return SpeechAct.STATEMENT

    def _extract_intents(self, original: str, normalized: str) -> list[IntentCandidate]:
        candidates: list[tuple[int, IntentCandidate]] = []
        music_query = extract_music_playback_query(normalized)
        if music_query:
            candidates.append((
                -1,
                IntentCandidate(action="media_control", text=original, confidence=0.92),
            ))
        for action, phrases in ACTION_PATTERNS:
            if music_query and action == "answer":
                continue
            if action == "remember" and re.search(
                r"\b(?:do you|did you|can you)\s+remember\s+(?:me|who|my name)\b",
                normalized,
            ):
                continue
            best_match: Optional[re.Match[str]] = None
            for phrase in phrases:
                match = re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", normalized)
                if match and (best_match is None or match.start() < best_match.start()):
                    best_match = match
            if best_match:
                candidates.append((
                    best_match.start(),
                    IntentCandidate(action=action, text=original, confidence=0.85),
                ))

        candidates.sort(key=lambda item: item[0])
        return [candidate for _, candidate in candidates]

    @staticmethod
    def _starts_with_phrase(text: str, phrases: Iterable[str]) -> bool:
        return any(text == phrase or text.startswith(phrase + " ") for phrase in phrases)

    def _extract_entities(self, text: str) -> dict[str, list[str]]:
        patterns = {
            "url": r"https?://[^\s]+",
            "email": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
            "duration": r"\b\d+(?:\.\d+)?\s*(?:seconds?|minutes?|hours?|days?|weeks?)\b",
            "time": r"\b(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)?\b",
            "number": r"(?<!\w)[+-]?\d+(?:[.,]\d+)?%?(?!\w)",
            "quoted_text": r"[\"']([^\"']+)[\"']",
        }
        entities: dict[str, list[str]] = {}
        for name, pattern in patterns.items():
            values = []
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                value = match.group(1) if name == "quoted_text" else match.group(0)
                if value not in values:
                    values.append(value)
            if values:
                entities[name] = values

        app_match = re.search(
            r"\b(?:open|close|launch|start|quit|exit)\s+(?:the\s+)?"
            r"([A-Za-z][A-Za-z0-9 ._-]{1,40}?)(?=\s+(?:app|application)\b|[,.!?]|$)",
            text,
            flags=re.IGNORECASE,
        )
        if app_match:
            entities["application"] = [app_match.group(1).strip()]
        return entities

    def _detect_affect(self, text: str) -> tuple[Affect, float]:
        for affect, phrases in AFFECT_PATTERNS:
            if any(
                re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text)
                for phrase in phrases
            ):
                urgency = 1.0 if affect == Affect.URGENT else 0.65 if "now" in text else 0.0
                return affect, urgency
        return Affect.NEUTRAL, 0.0

    def _topic_terms(self, tokens: Iterable[str]) -> list[str]:
        terms = []
        for token in tokens:
            if len(token) < 3 or token in STOP_WORDS or token in REFERENCE_PHRASES:
                continue
            if token not in terms:
                terms.append(token)
        return terms[:16]

    def _has_unclosed_delimiter(self, text: str) -> bool:
        return (
            text.count("(") > text.count(")")
            or text.count("[") > text.count("]")
            or text.count('"') % 2 == 1
        )


_nlu_engine: Optional[NLUEngine] = None


def get_nlu_engine() -> NLUEngine:
    global _nlu_engine
    if _nlu_engine is None:
        _nlu_engine = NLUEngine()
    return _nlu_engine
