"""Verified YouTube playback for resilient music fallback."""

from __future__ import annotations

import asyncio
import html
import json
import re
import urllib.parse
from typing import Any, Optional

import httpx

from config import get_settings
from .browser_controller import BrowserController


class YouTubeController:
    """Find a relevant video and verify that browser playback actually started."""

    _SEARCH_ENDPOINT = "https://www.googleapis.com/youtube/v3/search"

    def __init__(
        self,
        browser: Optional[BrowserController] = None,
        *,
        api_key: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        settings = get_settings()
        configured_key = settings.youtube_api_key
        self._api_key = (
            api_key
            if api_key is not None
            else (configured_key.get_secret_value() if configured_key else "")
        )
        self._browser_name = settings.youtube_browser
        self._browser = browser or BrowserController(default_browser=self._browser_name)
        self._timeout_seconds = settings.youtube_search_timeout_seconds
        self._transport = transport

    async def play_music(self, query: str) -> dict[str, Any]:
        normalized = self._normalize_query(query)
        if not normalized:
            return {
                "success": False,
                "action": "play_music",
                "provider": "youtube",
                "error": "A music query is required for the YouTube fallback.",
            }

        match = await self._search_api(normalized) if self._api_key else None
        if match:
            watch_url = f"https://www.youtube.com/watch?v={match['video_id']}&autoplay=1"
            navigation = await self._browser.navigate(
                watch_url,
                browser=self._browser_name,
                new_tab=True,
            )
            if not navigation.get("success"):
                return self._failure(normalized, str(navigation.get("error", "YouTube could not open.")))
            title = match.get("title")
        else:
            search_url = (
                "https://www.youtube.com/results?search_query="
                + urllib.parse.quote_plus(f"{normalized} music")
            )
            navigation = await self._browser.navigate(
                search_url,
                browser=self._browser_name,
                new_tab=True,
            )
            if not navigation.get("success"):
                return self._failure(normalized, str(navigation.get("error", "YouTube could not open.")))
            selected = await self._select_browser_video(normalized)
            if not selected.get("success"):
                return self._failure(normalized, str(selected.get("error", "No YouTube video was selected.")))
            title = selected.get("title")

        playback = await self._verify_playback()
        if not playback.get("success"):
            return self._failure(normalized, str(playback.get("error", "YouTube playback was not confirmed.")))

        return {
            "success": True,
            "action": "play_music",
            "provider": "youtube",
            "query": normalized,
            "track": title or normalized,
            "url": playback.get("url"),
            "playback_state": "playing",
        }

    async def _search_api(self, query: str) -> Optional[dict[str, str]]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    self._SEARCH_ENDPOINT,
                    params={
                        "part": "snippet",
                        "q": f"{query} music",
                        "type": "video",
                        "maxResults": "5",
                        "safeSearch": "moderate",
                        "key": self._api_key,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None

        candidates = []
        for index, item in enumerate(payload.get("items", []) if isinstance(payload, dict) else []):
            item_id = item.get("id", {}) if isinstance(item, dict) else {}
            video_id = str(item_id.get("videoId", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
                continue
            snippet = item.get("snippet", {})
            candidates.append({
                "video_id": video_id,
                "title": html.unescape(str(snippet.get("title", ""))).strip(),
                "channel": html.unescape(str(snippet.get("channelTitle", ""))).strip(),
                "index": index,
            })
        return self._select_best_candidate(query, candidates)

    async def _select_browser_video(self, query: str) -> dict[str, Any]:
        candidates = await self._collect_browser_candidates()
        match = self._select_best_candidate(query, candidates)
        if not match:
            return {
                "success": False,
                "error": (
                    f"YouTube returned results, but none matched '{query}' closely enough "
                    "to play safely."
                ),
            }

        video_id = str(match.get("video_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
            return {"success": False, "error": "The selected YouTube result had an invalid video ID."}

        watch_url = f"https://www.youtube.com/watch?v={video_id}&autoplay=1"
        navigation = await self._browser.navigate(
            watch_url,
            browser=self._browser_name,
            new_tab=False,
        )
        if not navigation.get("success"):
            return {
                "success": False,
                "error": str(navigation.get("error", "The matching YouTube video could not open.")),
            }
        return {"success": True, "title": match.get("title"), "url": watch_url}

    async def _collect_browser_candidates(self) -> list[dict[str, Any]]:
        script = """(() => {
            const links = Array.from(document.querySelectorAll("a#video-title, a#video-title-link"));
            const seen = new Set();
            const candidates = [];
            for (const [index, link] of links.entries()) {
                const href = link.getAttribute("href") || "";
                if (!href.startsWith("/watch?") || link.closest("ytd-ad-slot-renderer")) continue;
                const videoId = new URL(href, location.origin).searchParams.get("v") || "";
                if (!videoId || seen.has(videoId)) continue;
                const renderer = link.closest(
                    "ytd-video-renderer, ytd-rich-item-renderer, ytd-grid-video-renderer"
                );
                const channelNode = renderer && renderer.querySelector(
                    "ytd-channel-name a, #channel-name a, #text.ytd-channel-name"
                );
                const clean = (value) => (value || "").trim().replace(/[\\n\\r|]+/g, " ");
                seen.add(videoId);
                candidates.push({
                    video_id: videoId,
                    title: clean(link.getAttribute("title") || link.textContent),
                    channel: clean(channelNode && channelNode.textContent),
                    index: index,
                });
            }
            return JSON.stringify(candidates.slice(0, 30));
        })()"""

        for _ in range(6):
            result = await self._browser.execute_javascript(script, self._browser_name)
            output = str(result.get("result", "")) if result.get("success") else ""
            try:
                candidates = json.loads(output)
            except (TypeError, ValueError):
                candidates = []
            if isinstance(candidates, list) and candidates:
                return [candidate for candidate in candidates if isinstance(candidate, dict)]
            await asyncio.sleep(0.6)

        return []

    @classmethod
    def _select_best_candidate(
        cls,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        scored = []
        for candidate in candidates:
            score = cls._candidate_score(query, candidate)
            if score is None:
                continue
            position = int(candidate.get("index", 999) or 0)
            scored.append((score, -position, candidate))
        if not scored:
            return None
        return max(scored, key=lambda item: (item[0], item[1]))[2]

    @classmethod
    def _candidate_score(cls, query: str, candidate: dict[str, Any]) -> Optional[float]:
        query_text = cls._match_text(query)
        title_text = cls._match_text(str(candidate.get("title", "")))
        channel_text = cls._match_text(str(candidate.get("channel", "")))
        query_tokens = cls._matching_tokens(query_text)
        title_tokens = set(cls._matching_tokens(title_text))
        if not query_tokens or not title_tokens:
            return None

        covered = sum(token in title_tokens for token in query_tokens)
        coverage = covered / len(query_tokens)
        minimum_coverage = 1.0 if len(query_tokens) <= 2 else 0.72
        if coverage < minimum_coverage:
            return None

        broad_request = cls._is_broad_music_request(query_tokens)
        score = coverage * 100
        if query_text in title_text:
            score += 38
        if title_text == query_text:
            score += 22

        official_markers = ("official video", "official audio", "official music video")
        has_official_marker = any(marker in title_text for marker in official_markers)
        if broad_request:
            if has_official_marker:
                score -= 32
            collection_markers = (
                "playlist", "mix", "best", "hits", "collection", "compilation",
                "radio", "nonstop", "non stop", "top songs",
            )
            if any(marker in title_text for marker in collection_markers):
                score += 42
        else:
            if has_official_marker:
                score += 28
            if channel_text.endswith(" vevo") or channel_text.endswith(" topic"):
                score += 16

        channel_tokens = set(cls._matching_tokens(channel_text))
        score += min(18, 6 * len(set(query_tokens) & channel_tokens))

        if not broad_request:
            unwanted_variants = {
                "cover": 75,
                "karaoke": 85,
                "reaction": 90,
                "remix": 60,
                "mashup": 75,
                "sped up": 65,
                "slowed": 60,
                "reverb": 45,
                "nightcore": 75,
                "instrumental": 60,
                "full album": 55,
                "playlist": 55,
                "1 hour": 55,
            }
            for phrase, penalty in unwanted_variants.items():
                if phrase in title_text and phrase not in query_text:
                    score -= penalty

        extra_tokens = max(0, len(title_tokens) - len(set(query_tokens)))
        score -= min(10, extra_tokens * 0.7)
        score -= min(6, max(0, int(candidate.get("index", 0) or 0)) * 0.2)
        return score

    @staticmethod
    def _match_text(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
        return re.sub(r"\s+", " ", normalized).strip()

    @staticmethod
    def _matching_tokens(value: str) -> list[str]:
        ignored = {
            "a", "an", "by", "from", "me", "music", "on", "please", "play",
            "song", "songs", "the", "to", "track", "youtube",
        }
        return [token for token in value.split() if token not in ignored]

    @staticmethod
    def _is_broad_music_request(tokens: list[str]) -> bool:
        broad_terms = {
            "ambient", "best", "bollywood", "calm", "chill", "classical", "devotional",
            "focus", "genre", "good", "happy", "hindi", "jazz", "latest", "lofi",
            "marathi", "meditation", "mind", "new", "party", "peaceful", "playlist",
            "pop", "punjabi", "relaxation", "relaxing", "rock", "romantic", "sad",
            "sleep", "soft", "some", "soothing", "study", "top", "trending", "workout",
        }
        return bool(tokens) and all(token in broad_terms or token.isdigit() for token in tokens)

    async def _verify_playback(self) -> dict[str, Any]:
        script = """(() => {
            const video = document.querySelector("video");
            if (!video) return "NO_VIDEO";
            if (video.paused) {
                const playButton = document.querySelector("button.ytp-large-play-button");
                if (playButton) playButton.click();
                video.play().catch(() => {});
            }
            return video.paused ? "STARTING" : "PLAYING";
        })()"""

        current_url = ""
        for _ in range(6):
            url_result = await self._browser.get_current_url(self._browser_name)
            current_url = str(url_result.get("url", "")) if url_result.get("success") else ""
            if "youtube.com/watch" in current_url:
                state = await self._browser.execute_javascript(script, self._browser_name)
                if state.get("success") and str(state.get("result", "")) == "PLAYING":
                    return {"success": True, "url": current_url}
            await asyncio.sleep(0.6)

        return {
            "success": False,
            "error": "YouTube opened a result but did not confirm that its video started playing.",
        }

    @staticmethod
    def _normalize_query(query: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9 &'/-]+", " ", str(query or ""))
        normalized = re.sub(r"\s+", " ", normalized).strip(" -/")
        normalized = re.sub(
            r"^(?:please\s+)?(?:play|put\s+on|listen\s+to)\s+(?:me\s+)?",
            "",
            normalized,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(
            r"^(?:the\s+)?(?:song|track)\s*(?:called|named)?\s+",
            "",
            normalized,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(r"\s+(?:on|from)\s+youtube$", "", normalized, flags=re.IGNORECASE)
        normalized = normalized.strip(" '\"")
        return normalized[:100]

    @staticmethod
    def _failure(query: str, error: str) -> dict[str, Any]:
        return {
            "success": False,
            "action": "play_music",
            "provider": "youtube",
            "query": query,
            "error": error,
        }
