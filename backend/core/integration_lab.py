"""Chat-only integration research proposals with explicit human review."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from core.state_store import StateStore, get_state_store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IntegrationLab:
    """Persist proposals without granting the assistant self-modifying privileges."""

    _STORAGE_KEY = "integration_lab.proposals.v1"
    _MAX_PROPOSALS = 50
    _REVIEW_TERMS = (
        "integration",
        "integrate",
        "connector",
        "new capability",
        "add support",
        "research and propose",
        "research an",
        "research a ",
    )

    def __init__(self, store: Optional[StateStore] = None) -> None:
        self._store = store or get_state_store()

    async def propose_from_chat(self, request: str) -> Optional[dict[str, Any]]:
        """Create a proposal only for clearly explicit chat-based integration requests."""
        normalized = re.sub(r"\s+", " ", str(request or "")).strip()
        lowered = normalized.lower()
        if not normalized or not any(term in lowered for term in self._REVIEW_TERMS):
            return None
        return await self.create_proposal(normalized)

    async def create_proposal(self, request: str) -> dict[str, Any]:
        normalized = re.sub(r"\s+", " ", str(request or "")).strip()[:800]
        if not normalized:
            raise ValueError("An integration request is required.")

        proposal = self._apple_music_proposal(normalized) if self._is_apple_music_request(normalized) else self._generic_proposal(normalized)
        proposals = await self._load()
        proposals.insert(0, proposal)
        await self._save(proposals[: self._MAX_PROPOSALS])
        return proposal

    async def list_proposals(self) -> list[dict[str, Any]]:
        return await self._load()

    async def add_feedback(
        self,
        proposal_id: str,
        *,
        approved: bool,
        feedback: str = "",
    ) -> Optional[dict[str, Any]]:
        proposals = await self._load()
        for proposal in proposals:
            if proposal.get("id") != proposal_id:
                continue
            proposal["feedback"].append(
                {
                    "approved": approved,
                    "comment": str(feedback or "").strip()[:1000],
                    "created_at": _now(),
                }
            )
            proposal["status"] = "approved_for_implementation" if approved else "needs_revision"
            proposal["updated_at"] = _now()
            await self._save(proposals)
            return proposal
        return None

    async def validate(self, proposal_id: str) -> Optional[dict[str, Any]]:
        """Run a non-destructive configuration check requested from the chat UI."""
        proposals = await self._load()
        for proposal in proposals:
            if proposal.get("id") != proposal_id:
                continue

            if proposal.get("capability") == "apple_music_catalog":
                from core.apple_music_catalog import AppleMusicCatalogClient

                validation = {
                    "checked_at": _now(),
                    "result": AppleMusicCatalogClient().configuration_status(),
                    "kind": "configuration",
                }
            else:
                validation = {
                    "checked_at": _now(),
                    "kind": "review",
                    "result": {
                        "configured": False,
                        "requirement": "No automatic implementation runs. Review the proposal before a sandboxed prototype is created.",
                    },
                }

            proposal["validation"] = validation
            proposal["updated_at"] = _now()
            await self._save(proposals)
            return proposal
        return None

    async def _load(self) -> list[dict[str, Any]]:
        raw = await self._store.get_runtime_value(self._STORAGE_KEY)
        if not raw:
            return []
        try:
            proposals = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        return proposals if isinstance(proposals, list) else []

    async def _save(self, proposals: list[dict[str, Any]]) -> None:
        await self._store.set_runtime_value(self._STORAGE_KEY, json.dumps(proposals, ensure_ascii=True))

    @staticmethod
    def _is_apple_music_request(request: str) -> bool:
        return "apple music" in request.lower() or "musickit" in request.lower()

    @staticmethod
    def _base_proposal(request: str, *, title: str, capability: str) -> dict[str, Any]:
        timestamp = _now()
        return {
            "id": str(uuid4()),
            "title": title,
            "capability": capability,
            "request": request,
            "status": "awaiting_review",
            "created_at": timestamp,
            "updated_at": timestamp,
            "feedback": [],
            "validation": None,
            "safety": {
                "chat_only": True,
                "auto_apply": False,
                "voice_execution": False,
                "requires_user_approval": True,
            },
        }

    @classmethod
    def _apple_music_proposal(cls, request: str) -> dict[str, Any]:
        proposal = cls._base_proposal(
            request,
            title="Apple Music catalog matching and playback",
            capability="apple_music_catalog",
        )
        proposal["research"] = {
            "summary": (
                "MusicKit can search the full Apple Music catalog. Catalog playback needs an Apple "
                "Developer MusicKit token and a user-authorized Apple Music session; AppleScript can "
                "only verify local Music.app library playback."
            ),
            "sources": [
                {"title": "Apple MusicKit overview", "url": "https://developer.apple.com/musickit/"},
                {"title": "Apple Music API developer tokens", "url": "https://developer.apple.com/documentation/applemusicapi/generating-developer-tokens"},
                {"title": "MusicKit user authentication", "url": "https://developer.apple.com/documentation/applemusicapi/user-authentication-for-musickit"},
            ],
        }
        proposal["plan"] = [
            {
                "phase": "Credentials",
                "detail": "Enable MusicKit for Saksham's Apple Developer App ID and configure APPLE_MUSIC_DEVELOPER_TOKEN.",
            },
            {
                "phase": "Catalog matching",
                "detail": "Search songs and playlists, rank results by the user's intent, and expose the chosen item with alternatives.",
            },
            {
                "phase": "Playback authorization",
                "detail": "Add MusicKit user authorization in the Electron client before attempting catalog playback.",
            },
            {
                "phase": "Verification",
                "detail": "Test search, authorization expiry, playback state, and fallback to the local library without claiming an unverified play action.",
            },
        ]
        return proposal

    @classmethod
    def _generic_proposal(cls, request: str) -> dict[str, Any]:
        proposal = cls._base_proposal(
            request,
            title="Integration research proposal",
            capability="custom_integration",
        )
        proposal["research"] = {
            "summary": (
                "Saksham will scope the capability, use official documentation, and propose a small "
                "sandboxed prototype. It will not install packages, edit production code, or execute "
                "external actions without an approved proposal."
            ),
            "sources": [],
        }
        proposal["plan"] = [
            {"phase": "Scope", "detail": "Define the user-visible task, required permissions, and success signal."},
            {"phase": "Research", "detail": "Prefer official provider documentation and identify supported authentication."},
            {"phase": "Prototype", "detail": "Create an isolated change with focused automated tests."},
            {"phase": "Review", "detail": "Show evidence and ask for chat feedback before applying any approved change."},
        ]
        return proposal


_integration_lab: Optional[IntegrationLab] = None


def get_integration_lab() -> IntegrationLab:
    global _integration_lab
    if _integration_lab is None:
        _integration_lab = IntegrationLab()
    return _integration_lab
