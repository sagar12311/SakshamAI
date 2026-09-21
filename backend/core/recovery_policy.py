"""Allowlisted fallback plans for recoverable executor failures."""

from __future__ import annotations

from typing import Any

from config import get_settings


class RecoveryPolicy:
    """Produce bounded, semantically equivalent alternatives for failed steps."""

    max_alternatives_per_step = 2

    def alternatives(self, step: dict[str, Any], result: Any) -> list[dict[str, Any]]:
        action = str(step.get("action", ""))
        if action != "play_music" or not get_settings().youtube_music_fallback_enabled:
            return []

        parameters = step.get("parameters", {}) or {}
        query = str(parameters.get("genre") or step.get("target") or "").strip()
        if not query:
            return []

        primary_error = "Primary music provider did not complete playback."
        if isinstance(result, dict) and result.get("error"):
            primary_error = str(result["error"])

        return [{
            "action": "play_youtube_music",
            "target": query,
            "description": f"Use YouTube to play music matching {query}",
            "parameters": {
                "query": query,
                "fallback_from": "apple_music",
                "fallback_reason": primary_error,
            },
        }]


_recovery_policy = RecoveryPolicy()


def get_recovery_policy() -> RecoveryPolicy:
    return _recovery_policy
