"""Review-gated, local procedural learning candidates.

This is deliberately *not* a workflow runner.  It only turns a verified,
successful routine execution into a local candidate that a person can later
approve or reject for review.  Neither the planner nor the executor imports
or executes records from this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING

from core.action_policy import ApprovalLevel, get_action_policy

if TYPE_CHECKING:
    from core.state_store import StateStore


_REDACTED = "[REDACTED]"
_MAX_PLAN_BYTES = 64_000
_SENSITIVE_KEY_PARTS = {
    "access",
    "apikey",
    "authorization",
    "card",
    "cookie",
    "credential",
    "cvv",
    "passcode",
    "passphrase",
    "password",
    "private",
    "secret",
    "session",
    "token",
}
_SENSITIVE_COMPOUND_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "card_number",
    "cookie",
    "credential",
    "credentials",
    "private_key",
    "refresh_token",
    "session_token",
}
_SENSITIVE_COMPACT_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "cardnumber",
    "privatekey",
    "refreshtoken",
    "sessiontoken",
}


class LearningCandidateError(ValueError):
    """Raised when a candidate cannot be safely represented locally."""


@dataclass(frozen=True)
class ObservationResult:
    """The outcome of trying to observe one completed task execution."""

    candidate: Optional[dict[str, Any]]
    created: bool
    policy_level: str
    skipped_reason: Optional[str] = None


def _field_parts(key: str) -> set[str]:
    """Split snake, kebab, and camel case field names for redaction checks."""
    split_camel = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return {
        part.lower()
        for part in re.split(r"[^a-zA-Z0-9]+", split_camel)
        if part
    }


def is_sensitive_field(key: str) -> bool:
    """Return whether a structured field name is likely to contain a secret."""
    normalized = str(key or "").strip().lower().replace("-", "_")
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    if normalized in _SENSITIVE_COMPOUND_KEYS or compact in _SENSITIVE_COMPACT_KEYS:
        return True
    parts = _field_parts(normalized)
    if "api" in parts and "key" in parts:
        return True
    if "private" in parts and "key" in parts:
        return True
    if "card" in parts and ("number" in parts or "cvv" in parts):
        return True
    return bool(parts & _SENSITIVE_KEY_PARTS)


def redact_learning_text(value: str) -> str:
    """Redact common inline credential forms before local persistence."""
    text = str(value)
    text = re.sub(
        r"(?i)(\b(?:bearer|basic)\s+)[^\s,;]+",
        r"\1" + _REDACTED,
        text,
    )
    text = re.sub(
        r"(?i)(\b(?:api[_ -]?key|access[_ -]?token|authorization|password|"
        r"passcode|secret|session[_ -]?token|token)\s*[:=]\s*)[^\s,;]+",
        r"\1" + _REDACTED,
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:api[_-]?key|access[_-]?token|password|secret|token)=)[^&\s]+",
        r"\1" + _REDACTED,
        text,
    )
    return text


def redact_learning_value(value: Any, *, key: str = "") -> Any:
    """Create a JSON-safe deep copy with credential-bearing values removed."""
    if key and is_sensitive_field(key):
        return _REDACTED
    if isinstance(value, dict):
        return {
            str(child_key): redact_learning_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_learning_value(item, key=key) for item in value]
    if isinstance(value, str):
        return redact_learning_text(value)
    return value


def prepare_candidate_plan(plan: Any) -> tuple[list[dict[str, Any]], str]:
    """Validate, redact, and canonically digest a structured plan.

    The digest is calculated from the redacted representation, so it can be
    shown or compared without creating a secret-derived fingerprint.
    """
    if not isinstance(plan, list) or not plan:
        raise LearningCandidateError("A learning candidate needs a non-empty structured plan")
    if not all(isinstance(step, dict) for step in plan):
        raise LearningCandidateError("A learning candidate plan must contain structured steps")

    safe_plan = redact_learning_value(plan)
    try:
        canonical = json.dumps(
            safe_plan,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=str,
        )
    except (TypeError, ValueError) as error:
        raise LearningCandidateError("Learning candidate plan must be JSON serializable") from error

    if len(canonical.encode("utf-8")) > _MAX_PLAN_BYTES:
        raise LearningCandidateError("Learning candidate plan is too large")

    decoded = json.loads(canonical)
    if not isinstance(decoded, list) or not all(isinstance(step, dict) for step in decoded):
        raise LearningCandidateError("Learning candidate plan was not safely normalized")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return decoded, digest


def _has_verified_success(execution: dict[str, Any]) -> bool:
    """Require executor-produced evidence rather than a caller's success flag."""
    if str(execution.get("status") or "").strip().lower() != "completed":
        return False
    result = execution.get("result")
    if not isinstance(result, dict):
        return False
    outcomes = result.get("results")
    return bool(outcomes) and isinstance(outcomes, list) and all(
        isinstance(item, dict) and item.get("success") is True for item in outcomes
    )


async def observe_verified_success(
    store: "StateStore",
    *,
    task: dict[str, Any],
    execution: dict[str, Any],
) -> ObservationResult:
    """Save one review-only candidate for a verified routine success.

    Only policy level ``none`` is eligible today.  Confirmation, admin, and
    blocked plans are intentionally excluded even after they complete; this
    prevents personal or elevated workflows from becoming reusable patterns
    through background observation.
    """
    task_id = str(task.get("id") or "").strip()
    execution_id = str(execution.get("id") or "").strip()
    if not task_id or not execution_id or not _has_verified_success(execution):
        return ObservationResult(
            candidate=None,
            created=False,
            policy_level="unknown",
            skipped_reason="execution_not_verified",
        )

    plan = task.get("steps")
    try:
        safe_plan, plan_digest = prepare_candidate_plan(plan)
    except LearningCandidateError as error:
        return ObservationResult(
            candidate=None,
            created=False,
            policy_level="unknown",
            skipped_reason=str(error),
        )

    assessment = get_action_policy().assess_plan(safe_plan)
    policy_level = assessment.level.value
    if assessment.level is not ApprovalLevel.NONE:
        return ObservationResult(
            candidate=None,
            created=False,
            policy_level=policy_level,
            skipped_reason="only_routine_plans_are_observed",
        )

    try:
        candidate, created = await store.create_learning_candidate(
            task_id=task_id,
            execution_id=execution_id,
            title=str(task.get("title") or "Routine Saksham task"),
            plan=safe_plan,
            plan_digest=plan_digest,
            policy_level=policy_level,
            source="verified_task_completion",
        )
    except Exception:
        # Candidate creation should never change the execution result.  The
        # caller logs failures while retaining the completed task history.
        raise

    if candidate and created:
        await store.record_task_event(
            task_id,
            "learning.candidate_observed",
            "Successful routine plan saved as a review-only learning candidate",
            execution_id=execution_id,
            level="info",
            data={
                "candidate_id": candidate["id"],
                "plan_digest": candidate["plan_digest"],
                "policy_level": candidate["policy_level"],
            },
        )
    return ObservationResult(candidate=candidate, created=created, policy_level=policy_level)
