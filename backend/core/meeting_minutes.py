"""Source-grounded live pointers and Minutes of Meeting generation."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from core.llm_client import get_llm_client


ALLOWED_POINTER_CATEGORIES = {
    "key_point",
    "decision",
    "action",
    "question",
    "risk",
    "next_step",
    "private_note",
}

# The configured local model has an 8k-token context window.  Transcript
# segment UUIDs are surprisingly expensive tokens, so we use short, per-call
# source labels in prompts and never send a whole long meeting in one request.
FINAL_CHUNK_MAX_CHARS = 12_000
FINAL_CHUNK_MAX_TOKENS = 1_300
FINAL_REDUCTION_MAX_TOKENS = 1_800
MOM_LLM_CALL_TIMEOUT_SECONDS = 75.0


def format_timestamp(milliseconds: int) -> str:
    total_seconds = max(0, int(milliseconds) // 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _response_content(response: dict[str, Any]) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    return str(content or "")


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("LLM response must be a JSON object")
    return value


def normalize_deadline_phrase(phrase: str, meeting_started_at: str) -> Optional[str]:
    """Normalize common explicit English/Hinglish date phrases without guessing."""
    value = " ".join(str(phrase).strip().split())
    if not value:
        return None
    meeting_date = datetime.fromisoformat(meeting_started_at).date()
    folded = value.casefold().strip(" .,!?")
    iso_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", folded)
    if iso_match:
        try:
            return datetime.strptime(folded, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None

    direct_offsets = {
        "today": 0,
        "aaj": 0,
        "tomorrow": 1,
        "kal": 1,
        "day after tomorrow": 2,
        "parso": 2,
        "parson": 2,
        "next week": 7,
        "agle hafte": 7,
    }
    for phrase_value, days in sorted(
        direct_offsets.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if re.search(rf"\b{re.escape(phrase_value)}\b", folded):
            return (meeting_date + timedelta(days=days)).isoformat()

    relative = re.search(
        r"\b(?:in\s+)?(\d{1,3})\s*(day|days|din|week|weeks|hafta|hafte)\b",
        folded,
    )
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        days = amount * 7 if unit in {"week", "weeks", "hafta", "hafte"} else amount
        return (meeting_date + timedelta(days=days)).isoformat()

    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    weekday_match = re.search(
        r"\b(?:next|coming)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        folded,
    )
    if weekday_match:
        target = weekdays[weekday_match.group(1)]
        delta = (target - meeting_date.weekday()) % 7
        return (meeting_date + timedelta(days=delta or 7)).isoformat()

    numeric = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", folded)
    if numeric:
        day, month = int(numeric.group(1)), int(numeric.group(2))
        year_value = numeric.group(3)
        year = int(year_value) if year_value else meeting_date.year
        if year < 100:
            year += 2000
        try:
            candidate = datetime(year, month, day).date()
        except ValueError:
            return None
        if not year_value and candidate < meeting_date:
            try:
                candidate = candidate.replace(year=year + 1)
            except ValueError:
                return None
        return candidate.isoformat()
    return None


def escape_markdown_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return re.sub(r"([\\`*_{}\[\]<>#+!|])", r"\\\1", text)


def _segment_lines(segments: list[dict[str, Any]], max_chars: int = 18000) -> str:
    lines: list[str] = []
    size = 0
    for segment in segments:
        line = (
            f'{segment["id"]} | {format_timestamp(segment["start_ms"])}-'
            f'{format_timestamp(segment["end_ms"])} | '
            f'{segment.get("speaker_label", "Speaker 1")}: {segment["text"]}'
        )
        if lines and size + len(line) > max_chars:
            break
        lines.append(line)
        size += len(line)
    return "\n".join(lines)


def _source_aliases(segments: list[dict[str, Any]]) -> dict[str, str]:
    """Assign compact labels for a single model prompt, preserving traceability."""
    return {str(segment["id"]): f"s{index}" for index, segment in enumerate(segments, start=1)}


def _prompt_segment_lines(
    segments: list[dict[str, Any]],
    aliases: dict[str, str],
    *,
    max_chars: int,
) -> str:
    """Render source text compactly without exposing UUID-sized source labels to the LLM."""
    lines: list[str] = []
    size = 0
    for segment in segments:
        source_id = str(segment["id"])
        line = (
            f'{aliases[source_id]} | {format_timestamp(segment["start_ms"])}-'
            f'{format_timestamp(segment["end_ms"])} | '
            f'{segment.get("speaker_label", "Speaker 1")}: {segment["text"]}'
        )
        if lines and size + len(line) > max_chars:
            break
        # A malformed ASR segment must not make a prompt unbounded.
        if not lines and len(line) > max_chars:
            line = line[: max_chars - 14].rstrip() + " [truncated]"
        lines.append(line)
        size += len(line)
    return "\n".join(lines)


def _segment_batches(
    segments: list[dict[str, Any]],
    *,
    max_chars: int,
) -> list[list[dict[str, Any]]]:
    """Split source segments on segment boundaries before sending them to the model."""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 0
    for segment in segments:
        # A short alias is representative enough for budgeting; the exact
        # source label is created once the batch is finalized.
        estimated = len(str(segment.get("text", ""))) + len(
            str(segment.get("speaker_label", "Speaker 1"))
        ) + 32
        if current and current_size + estimated > max_chars:
            batches.append(current)
            current = []
            current_size = 0
        current.append(segment)
        current_size += estimated
    if current:
        batches.append(current)
    return batches


def _remap_source_aliases(value: Any, aliases: dict[str, str]) -> Any:
    """Translate model-only source aliases back to persisted segment IDs."""
    reverse = {alias: source_id for source_id, alias in aliases.items()}
    if isinstance(value, list):
        return [_remap_source_aliases(item, aliases) for item in value]
    if not isinstance(value, dict):
        return value
    remapped: dict[str, Any] = {}
    for key, item in value.items():
        if key == "source_segment_ids" and isinstance(item, list):
            remapped[key] = [reverse.get(str(source), str(source)) for source in item]
        else:
            remapped[key] = _remap_source_aliases(item, aliases)
    return remapped


class MeetingMinutesGenerator:
    def __init__(self, llm: Optional[Any] = None) -> None:
        self.llm = llm or get_llm_client()

    async def _complete_json(self, prompt: str, *, system_prompt: str, max_tokens: int) -> dict[str, Any]:
        """Bound a single local-model call so MOM processing always fails closed."""
        try:
            return await asyncio.wait_for(
                self.llm.complete(
                    messages=[{"role": "user", "content": prompt}],
                    system_prompt=system_prompt,
                    temperature=0.0,
                    max_tokens=max_tokens,
                ),
                timeout=MOM_LLM_CALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as error:
            raise RuntimeError(
                f"MOM model response timed out after {MOM_LLM_CALL_TIMEOUT_SECONDS:.0f} seconds"
            ) from error

    async def generate_pointers(
        self,
        segments: list[dict[str, Any]],
        *,
        provisional: bool = True,
    ) -> list[dict[str, Any]]:
        public_segments = [segment for segment in segments if not segment.get("is_private")]
        if not public_segments:
            return []
        source_by_id = {str(segment["id"]): segment for segment in public_segments}
        aliases = _source_aliases(public_segments[-40:])
        prompt = f"""
Return JSON only with this shape:
{{"pointers":[{{"category":"key_point|decision|action|question|risk|next_step","content":"concise factual statement","source_segment_ids":["s1"]}}]}}

Rules:
- Use only facts in the supplied transcript.
- Every pointer needs one or more exact supplied source labels such as s1.
- Do not invent owners, dates, priorities, decisions, or commitments.
- Ignore greetings and low-information chatter.
- Return at most 8 pointers.

Transcript:
{_prompt_segment_lines(public_segments[-40:], aliases, max_chars=12000)}
""".strip()
        try:
            response = await self._complete_json(
                prompt,
                system_prompt="You extract auditable meeting facts. Output strict JSON only.",
                max_tokens=900,
            )
            payload = _remap_source_aliases(
                parse_json_object(_response_content(response)), aliases
            )
            candidates = payload.get("pointers", [])
        except Exception:
            candidates = []

        validated: list[dict[str, Any]] = []
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for candidate in candidates if isinstance(candidates, list) else []:
            if not isinstance(candidate, dict):
                continue
            source_ids = [
                str(source_id)
                for source_id in candidate.get("source_segment_ids", [])
                if str(source_id) in source_by_id
            ]
            content = str(candidate.get("content", "")).strip()
            category = str(candidate.get("category", "key_point"))
            if not content or not source_ids or category not in ALLOWED_POINTER_CATEGORIES:
                continue
            dedupe_key = (content.casefold(), tuple(sorted(source_ids)))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            sources = [source_by_id[source_id] for source_id in source_ids]
            validated.append(
                {
                    "category": category,
                    "content": content,
                    "source_segment_ids": source_ids,
                    "start_ms": min(int(source["start_ms"]) for source in sources),
                    "end_ms": max(int(source["end_ms"]) for source in sources),
                    "is_provisional": provisional,
                    "metadata": {},
                }
            )

        if validated:
            return validated

        latest = public_segments[-1]
        return [
            {
                "category": "key_point",
                "content": str(latest["text"]).strip()[:280],
                "source_segment_ids": [str(latest["id"])],
                "start_ms": int(latest["start_ms"]),
                "end_ms": int(latest["end_ms"]),
                "is_provisional": provisional,
                "metadata": {"fallback": "verbatim_transcript"},
            }
        ]

    async def generate_final_structure(
        self,
        meeting: dict[str, Any],
        project: dict[str, Any],
        segments: list[dict[str, Any]],
        pointers: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], Optional[str]]:
        public_segments = [segment for segment in segments if not segment.get("is_private")]
        if not public_segments:
            return (
                self._fallback_structure([]),
                "Automated MOM summary unavailable: no public transcript was captured.",
            )
        source_ids = {str(segment["id"]) for segment in public_segments}
        # Do not let long calls overflow the model's context window.  Every
        # pass has its own compact source labels (s1, s2, ...) and is then
        # reduced hierarchically, so the final result still covers the whole
        # meeting rather than only its beginning.
        drafts: list[dict[str, Any]] = []
        failures: list[str] = []
        for chunk in _segment_batches(public_segments, max_chars=FINAL_CHUNK_MAX_CHARS):
            aliases = _source_aliases(chunk)
            chunk_source_ids = set(aliases)
            pointer_lines: list[str] = []
            for pointer in pointers:
                cited = [
                    aliases[str(source)]
                    for source in pointer.get("source_segment_ids", [])
                    if str(source) in chunk_source_ids
                ]
                if cited:
                    pointer_lines.append(
                        f'- {pointer.get("category", "key_point")}: '
                        f'{pointer.get("content", "")} [{",".join(cited)}]'
                    )
            prompt = self._chunk_prompt(
                meeting,
                project,
                chunk,
                aliases,
                pointer_lines[-12:],
            )
            try:
                response = await self._complete_json(
                    prompt,
                    system_prompt="You prepare auditable client meeting minutes. Output strict JSON only.",
                    max_tokens=FINAL_CHUNK_MAX_TOKENS,
                )
                raw = _remap_source_aliases(
                    parse_json_object(_response_content(response)),
                    aliases,
                )
                drafts.append(
                    self._validate_structure(raw, source_ids, meeting["started_at"])
                )
            except Exception as error:
                # Keep a conservative, source-linked extract for this chunk.
                # It is intentionally narrow: a false decision or action is
                # worse than an empty field in an auditable MOM.
                failures.append(str(error))
                drafts.append(self._fallback_chunk_structure(chunk))

        if not drafts or len(failures) == len(drafts):
            detail = failures[0] if failures else "No source chunks were available"
            return (
                self._fallback_structure(public_segments),
                f"Automated MOM summary unavailable: {detail}",
            )

        structured = await self._hierarchical_reduce(
            drafts,
            source_ids,
            meeting["started_at"],
        )
        return structured, None

    @staticmethod
    def _chunk_prompt(
        meeting: dict[str, Any],
        project: dict[str, Any],
        chunk: list[dict[str, Any]],
        aliases: dict[str, str],
        pointer_lines: list[str],
    ) -> str:
        hint_text = "\n".join(pointer_lines) or "- None"
        return f"""
Create a compact, source-grounded partial Minutes of Meeting as strict JSON:
{{
  "attendees":["name or speaker label"],
  "summary":{{"content":"short factual summary","source_segment_ids":["s1"]}},
  "discussion_points":[{{"content":"...","source_segment_ids":["s1"]}}],
  "decisions":[{{"content":"...","source_segment_ids":["s1"]}}],
  "actions":[{{"action":"...","owner":"... or Not specified","deadline":"ISO date or Not specified","deadline_original":"spoken phrase or empty","priority":"high|medium|low|Not specified","inferred_fields":["deadline"],"inference_reason":"brief reason or empty","status":"open","source_segment_ids":["s1"]}}],
  "risks":[{{"content":"...","source_segment_ids":["s1"]}}],
  "open_questions":[{{"content":"...","source_segment_ids":["s1"]}}],
  "next_steps":[{{"content":"...","source_segment_ids":["s1"]}}]
}}

Meeting date: {meeting["started_at"]}
Project: {project["name"]}
Title: {meeting["title"]}

Rules:
- Source labels such as s1 are the only valid source IDs. Cite every item.
- A decision requires an explicit agreement, approval, selection, or decision.
- An action requires an explicit commitment or request; do not treat a suggestion as one.
- A risk/blocker, open question, and next step each require explicit evidence. Omit unsupported items.
- Never guess an owner, date, priority, decision, or commitment. Use "Not specified" when needed.
- Keep the summary under 240 characters; at most 3 discussion points, 3 decisions, 4 actions, and 3 items in each other section.
- The candidate pointers are hints only. Verify them against the transcript and discard unsupported hints.

Candidate pointers:
{hint_text}

Transcript chunk:
{_prompt_segment_lines(chunk, aliases, max_chars=FINAL_CHUNK_MAX_CHARS)}
""".strip()

    async def _hierarchical_reduce(
        self,
        drafts: list[dict[str, Any]],
        valid_source_ids: set[str],
        meeting_started_at: str,
    ) -> dict[str, Any]:
        current = drafts
        while len(current) > 1:
            next_level: list[dict[str, Any]] = []
            for offset in range(0, len(current), 3):
                group = current[offset : offset + 3]
                merged = self._merge_structures(group)
                aliases = _source_aliases(
                    [
                        {"id": source_id}
                        for source_id in sorted(self._structure_source_ids(merged))
                    ]
                )
                try:
                    response = await self._complete_json(
                        self._reduction_prompt(merged, aliases),
                        system_prompt=(
                            "You consolidate source-grounded meeting evidence. "
                            "Output strict JSON only."
                        ),
                        max_tokens=FINAL_REDUCTION_MAX_TOKENS,
                    )
                    raw = _remap_source_aliases(
                        parse_json_object(_response_content(response)), aliases
                    )
                    next_level.append(
                        self._validate_structure(raw, valid_source_ids, meeting_started_at)
                    )
                except Exception:
                    # A failed reduction must not throw away valid extracts.
                    next_level.append(merged)
            current = next_level
        return current[0]

    @staticmethod
    def _reduction_prompt(structured: dict[str, Any], aliases: dict[str, str]) -> str:
        def cited(item: dict[str, Any]) -> str:
            labels = [
                aliases[str(source)]
                for source in item.get("source_segment_ids", [])
                if str(source) in aliases
            ]
            return f' [{",".join(labels)}]' if labels else ""

        lines: list[str] = []
        summary_sources = {
            "source_segment_ids": structured.get("summary_source_segment_ids", [])
        }
        if structured.get("summary"):
            lines.append(f'SUMMARY: {structured["summary"]}{cited(summary_sources)}')
        for key, label in (
            ("discussion_points", "DISCUSSION"),
            ("decisions", "DECISION"),
            ("risks", "RISK"),
            ("open_questions", "QUESTION"),
            ("next_steps", "NEXT"),
        ):
            for item in structured.get(key, []):
                lines.append(f'{label}: {item.get("content", "")}{cited(item)}')
        for action in structured.get("actions", []):
            lines.append(
                "ACTION: "
                f'{action.get("action", "")} | owner: {action.get("owner", "Not specified")} '
                f'| deadline: {action.get("deadline_original") or action.get("deadline", "Not specified")} '
                f'| priority: {action.get("priority", "Not specified")}{cited(action)}'
            )
        evidence = "\n".join(lines) or "No evidence items."
        return f"""
Consolidate the following already source-grounded meeting evidence into strict JSON:
{{
  "attendees":["name or speaker label"],
  "summary":{{"content":"short factual summary","source_segment_ids":["s1"]}},
  "discussion_points":[{{"content":"...","source_segment_ids":["s1"]}}],
  "decisions":[{{"content":"...","source_segment_ids":["s1"]}}],
  "actions":[{{"action":"...","owner":"... or Not specified","deadline":"ISO date or Not specified","deadline_original":"spoken phrase or empty","priority":"high|medium|low|Not specified","inferred_fields":[],"inference_reason":"","status":"open","source_segment_ids":["s1"]}}],
  "risks":[{{"content":"...","source_segment_ids":["s1"]}}],
  "open_questions":[{{"content":"...","source_segment_ids":["s1"]}}],
  "next_steps":[{{"content":"...","source_segment_ids":["s1"]}}]
}}

Rules:
- Only select, combine, or shorten the supplied evidence. Do not introduce facts.
- Every output item must retain one or more listed source labels.
- Preserve explicit decisions, risks/blockers, unresolved questions, commitments, and next steps.
- Do not turn a suggestion into a decision or action; omit unsupported entries.
- Keep the summary under 420 characters and each list to at most 12 items.

Evidence:
{evidence}
""".strip()

    @staticmethod
    def _structure_source_ids(structured: dict[str, Any]) -> set[str]:
        source_ids = {str(item) for item in structured.get("summary_source_segment_ids", [])}
        for key in (
            "discussion_points", "decisions", "actions", "risks", "open_questions", "next_steps"
        ):
            for item in structured.get(key, []):
                source_ids.update(str(source) for source in item.get("source_segment_ids", []))
        return source_ids

    @staticmethod
    def _merge_structures(structures: list[dict[str, Any]]) -> dict[str, Any]:
        """Losslessly combine verified chunk extracts when no reducer is available."""
        result: dict[str, Any] = {
            "attendees": [],
            "summary": "",
            "summary_source_segment_ids": [],
            "discussion_points": [],
            "decisions": [],
            "actions": [],
            "risks": [],
            "open_questions": [],
            "next_steps": [],
        }
        summaries: list[str] = []
        seen: dict[str, set[str]] = {key: set() for key in result if key not in {
            "attendees", "summary", "summary_source_segment_ids"
        }}
        for structure in structures:
            for attendee in structure.get("attendees", []):
                if attendee not in result["attendees"]:
                    result["attendees"].append(attendee)
            if structure.get("summary"):
                summaries.append(str(structure["summary"]))
            for source in structure.get("summary_source_segment_ids", []):
                if source not in result["summary_source_segment_ids"]:
                    result["summary_source_segment_ids"].append(source)
            for key in seen:
                for item in structure.get(key, []):
                    identity = str(
                        item.get("action") if key == "actions" else item.get("content", "")
                    ).casefold()
                    if not identity or identity in seen[key]:
                        continue
                    seen[key].add(identity)
                    result[key].append(dict(item))
        result["summary"] = " ".join(summaries)[:1_200].strip()
        return result

    @staticmethod
    def _fallback_chunk_structure(chunk: list[dict[str, Any]]) -> dict[str, Any]:
        """Conservative no-model rescue that never manufactures meeting facts."""
        attendees = sorted({str(item.get("speaker_label", "Speaker 1")) for item in chunk})
        result: dict[str, Any] = {
            "attendees": attendees,
            "summary": "",
            "summary_source_segment_ids": [],
            "discussion_points": [],
            "decisions": [],
            "actions": [],
            "risks": [],
            "open_questions": [],
            "next_steps": [],
        }
        if chunk:
            first = chunk[0]
            result["summary"] = str(first.get("text", "")).strip()[:240]
            result["summary_source_segment_ids"] = [str(first["id"])]
        patterns = {
            "decisions": re.compile(
                r"\b(?:we\s+(?:decided|agreed)|(?:it\s+)?was\s+decided|approved|finali[sz]ed|confirmed)\b",
                re.IGNORECASE,
            ),
            "risks": re.compile(
                r"\b(?:risk|blocker|blocked|blocking|dependency|concern|issue|delay)\b",
                re.IGNORECASE,
            ),
            "open_questions": re.compile(
                r"(?:\?|^\s*(?:what|when|who|where|why|how|can|could|should)\b)",
                re.IGNORECASE,
            ),
            "next_steps": re.compile(
                r"\b(?:next step|follow[- ]?up|after that|then we|we(?:'| wi)ll next)\b",
                re.IGNORECASE,
            ),
        }
        for segment in chunk:
            text = str(segment.get("text", "")).strip()
            if not text:
                continue
            source = [str(segment["id"])]
            for key, pattern in patterns.items():
                if len(result[key]) < 8 and pattern.search(text):
                    result[key].append({"content": text[:360], "source_segment_ids": source})
            if (
                len(result["actions"]) < 10
                and re.search(r"\b(?:i|we|[A-Z][a-z]+)\s+(?:will|shall|need to)\b", text)
            ):
                result["actions"].append(
                    {
                        "action": text[:360],
                        "owner": str(segment.get("speaker_label") or "Not specified"),
                        "deadline": "Not specified",
                        "deadline_original": "",
                        "priority": "Not specified",
                        "inferred_fields": [],
                        "inference_reason": "",
                        "status": "open",
                        "source_segment_ids": source,
                    }
                )
        return result

    @staticmethod
    def _validate_structure(
        value: dict[str, Any],
        valid_source_ids: set[str],
        meeting_started_at: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "attendees": [str(item) for item in value.get("attendees", []) if str(item).strip()],
            "summary": "",
            "summary_source_segment_ids": [],
        }
        summary = value.get("summary", {})
        if isinstance(summary, dict):
            summary_sources = [
                str(source)
                for source in summary.get("source_segment_ids", [])
                if str(source) in valid_source_ids
            ]
            summary_content = str(summary.get("content", "")).strip()
            if summary_content and summary_sources:
                result["summary"] = summary_content
                result["summary_source_segment_ids"] = summary_sources
        for key in (
            "discussion_points",
            "decisions",
            "actions",
            "risks",
            "open_questions",
            "next_steps",
        ):
            result[key] = []
            candidates = value.get(key, [])
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                sources = [
                    str(source)
                    for source in candidate.get("source_segment_ids", [])
                    if str(source) in valid_source_ids
                ]
                if not sources:
                    continue
                cleaned = dict(candidate)
                cleaned["source_segment_ids"] = sources
                if key == "actions":
                    action = str(cleaned.get("action", "")).strip()
                    if not action:
                        continue
                    original = str(cleaned.get("deadline_original", "")).strip()
                    supplied_deadline = str(cleaned.get("deadline", "")).strip()
                    normalized = normalize_deadline_phrase(
                        original or supplied_deadline,
                        meeting_started_at,
                    )
                    cleaned["action"] = action
                    cleaned["owner"] = str(cleaned.get("owner") or "Not specified").strip()
                    cleaned["deadline"] = normalized or "Not specified"
                    cleaned["deadline_original"] = original
                    priority = str(cleaned.get("priority") or "Not specified").casefold()
                    cleaned["priority"] = (
                        priority if priority in {"high", "medium", "low"} else "Not specified"
                    )
                    inferred = [
                        str(field)
                        for field in cleaned.get("inferred_fields", [])
                        if str(field) in {"owner", "deadline", "priority"}
                    ]
                    cleaned["inferred_fields"] = inferred
                    cleaned["inference_reason"] = str(
                        cleaned.get("inference_reason", "")
                    ).strip()
                    if inferred and not cleaned["inference_reason"]:
                        cleaned["inference_reason"] = "Inferred from the cited transcript."
                    cleaned["status"] = str(cleaned.get("status") or "open").strip()
                else:
                    content = str(cleaned.get("content", "")).strip()
                    if not content:
                        continue
                    cleaned["content"] = content
                result[key].append(cleaned)
        return result

    @staticmethod
    def _fallback_structure(segments: list[dict[str, Any]]) -> dict[str, Any]:
        attendees = sorted({str(item.get("speaker_label", "Speaker 1")) for item in segments})
        points = [
            {"content": str(segment["text"]), "source_segment_ids": [str(segment["id"])]}
            for segment in segments
        ]
        return {
            "attendees": attendees,
            "summary": "Automated summary unavailable. The source-grounded transcript follows.",
            "summary_source_segment_ids": [],
            "discussion_points": points,
            "decisions": [],
            "actions": [],
            "risks": [],
            "open_questions": [],
            "next_steps": [],
        }

    @staticmethod
    def render_markdown(
        meeting: dict[str, Any],
        project: dict[str, Any],
        structured: dict[str, Any],
        segments: list[dict[str, Any]],
        *,
        final: bool = False,
    ) -> str:
        source_by_id = {str(segment["id"]): segment for segment in segments}

        def source_stamp(item: dict[str, Any]) -> str:
            sources = [
                source_by_id[source_id]
                for source_id in item.get("source_segment_ids", [])
                if source_id in source_by_id
            ]
            if not sources:
                return "[source unavailable]"
            start = min(int(source["start_ms"]) for source in sources)
            return f"[{format_timestamp(start)}]"

        started = datetime.fromisoformat(meeting["started_at"]).astimezone()
        lines = [
            f'# Minutes of Meeting: {escape_markdown_text(meeting["title"])}',
            "",
            f'- **Status:** {"Final" if final else "Draft"}',
            f'- **Project:** {escape_markdown_text(project["name"])}',
            f'- **Date:** {started.strftime("%Y-%m-%d %H:%M %Z")}',
            f'- **Meeting ID:** `{meeting["id"]}`',
            "",
            "## Attendees",
            "",
        ]
        attendees = structured.get("attendees", [])
        lines.extend(f"- {escape_markdown_text(attendee)}" for attendee in attendees)
        if not attendees:
            lines.append("- Not specified")

        summary_item = {
            "source_segment_ids": structured.get("summary_source_segment_ids", []),
        }
        summary_stamp = (
            source_stamp(summary_item) + " "
            if structured.get("summary_source_segment_ids")
            else ""
        )
        lines.extend(
            [
                "",
                "## Summary",
                "",
                summary_stamp + escape_markdown_text(structured.get("summary") or "Not available."),
            ]
        )

        sections = (
            ("Discussion Points", "discussion_points"),
            ("Decisions", "decisions"),
            ("Risks and Blockers", "risks"),
            ("Open Questions", "open_questions"),
            ("Next Steps", "next_steps"),
        )
        for heading, key in sections:
            lines.extend(["", f"## {heading}", ""])
            items = structured.get(key, [])
            if not items:
                lines.append("- None recorded.")
                continue
            for item in items:
                lines.append(
                    f'- {source_stamp(item)} {escape_markdown_text(item.get("content", ""))}'
                )

        lines.extend(
            [
                "",
                "## Action Register",
                "",
                "| Time | Action | Owner | Deadline | Priority | Status | Notes |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        actions = structured.get("actions", [])
        if not actions:
            lines.append("| - | No actions recorded | - | - | - | - | - |")
        for action in actions:
            inferred = ", ".join(str(item) for item in action.get("inferred_fields", []))
            reason = str(action.get("inference_reason", "")).strip()
            notes: list[str] = []
            original_deadline = str(action.get("deadline_original", "")).strip()
            if original_deadline:
                notes.append(f'Spoken deadline: "{original_deadline}".')
            if inferred:
                notes.append(f"Inferred: {inferred}. {reason}".strip())
            cells = [
                source_stamp(action),
                action.get("action", ""),
                action.get("owner", "Not specified"),
                action.get("deadline", "Not specified"),
                action.get("priority", "Not specified"),
                action.get("status", "open"),
                " ".join(notes),
            ]
            escaped = [escape_markdown_text(cell) for cell in cells]
            lines.append("| " + " | ".join(escaped) + " |")

        lines.extend(["", "## Transcript", ""])
        for segment in segments:
            if segment.get("is_private"):
                continue
            lines.append(
                f'**[{format_timestamp(segment["start_ms"])}] '
                f'{escape_markdown_text(segment.get("speaker_label", "Speaker 1"))}:** '
                f'{escape_markdown_text(segment["text"])}'
            )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
