"""Apple Music catalog search with explicit playback verification boundaries."""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from config import get_settings


class AppleMusicCatalogClient:
    """Find the closest Apple Music catalog item without treating it as a library track."""

    _BASE_URL = "https://api.music.apple.com/v1"

    def __init__(
        self,
        *,
        developer_token: Optional[str] = None,
        storefront: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        settings = get_settings()
        configured_token = settings.apple_music_developer_token
        self._developer_token = (
            developer_token
            if developer_token is not None
            else (configured_token.get_secret_value() if configured_token else "")
        )
        self._storefront = (storefront or settings.apple_music_storefront).strip() or "in"
        self._timeout_seconds = settings.apple_music_catalog_timeout_seconds
        self._transport = transport

    @property
    def is_configured(self) -> bool:
        return bool(self._developer_token)

    def configuration_status(self) -> dict[str, Any]:
        return {
            "configured": self.is_configured,
            "storefront": self._storefront,
            "requirement": (
                "Set APPLE_MUSIC_DEVELOPER_TOKEN after enabling MusicKit for Saksham's "
                "Apple Developer App ID."
            ),
        }

    async def search_best_match(self, query: str) -> dict[str, Any]:
        """Return a ranked catalog item for a requested mood, genre, artist, or song."""
        cleaned_query = re.sub(r"\s+", " ", str(query or "")).strip()[:120]
        if not cleaned_query:
            return {"success": False, "error": "A music query is required."}
        if not self.is_configured:
            return {
                "success": False,
                "configured": False,
                "error": "Apple Music catalog search is not configured.",
            }

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    f"{self._BASE_URL}/catalog/{self._storefront}/search",
                    headers={"Authorization": f"Bearer {self._developer_token}"},
                    params={
                        "term": cleaned_query,
                        "types": "songs,playlists",
                        "limit": "10",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as error:
            return {
                "success": False,
                "configured": True,
                "error": f"Apple Music catalog search failed ({error.response.status_code}).",
            }
        except httpx.HTTPError as error:
            return {
                "success": False,
                "configured": True,
                "error": f"Apple Music catalog search is unavailable: {error}.",
            }
        except ValueError:
            return {
                "success": False,
                "configured": True,
                "error": "Apple Music returned an unreadable catalog response.",
            }

        candidates = self._extract_candidates(payload, cleaned_query)
        if not candidates:
            return {
                "success": False,
                "configured": True,
                "error": f"Apple Music could not find a catalog match for {cleaned_query}.",
            }

        return {
            "success": True,
            "configured": True,
            "query": cleaned_query,
            "match": candidates[0],
            "candidates": candidates[:3],
        }

    @classmethod
    def _extract_candidates(cls, payload: dict[str, Any], query: str) -> list[dict[str, Any]]:
        results = payload.get("results", {}) if isinstance(payload, dict) else {}
        query_tokens = set(cls._tokens(query))
        candidates: list[dict[str, Any]] = []

        for result_type in ("playlists", "songs"):
            collection = results.get(result_type, {}) if isinstance(results, dict) else {}
            for item in collection.get("data", []) if isinstance(collection, dict) else []:
                if not isinstance(item, dict):
                    continue
                attributes = item.get("attributes", {})
                if not isinstance(attributes, dict):
                    continue
                name = str(attributes.get("name", "")).strip()
                url = str(attributes.get("url", "")).strip()
                if not name or not url:
                    continue
                artist = str(attributes.get("artistName", "")).strip() or None
                genres = [str(value) for value in attributes.get("genreNames", []) if value]
                searchable = " ".join([name, artist or "", *genres])
                item_tokens = set(cls._tokens(searchable))
                overlap = len(query_tokens & item_tokens)
                score = overlap * 10
                if query.lower() in name.lower():
                    score += 15
                if result_type == "playlists":
                    # A playlist is generally a better fit for a mood or genre request.
                    score += 2

                candidates.append(
                    {
                        "id": str(item.get("id", "")),
                        "kind": result_type[:-1],
                        "name": name,
                        "artist": artist,
                        "genres": genres,
                        "url": url,
                        "score": score,
                    }
                )

        return sorted(candidates, key=lambda item: (-item["score"], item["name"].lower()))

    @staticmethod
    def _tokens(value: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", value.lower())
