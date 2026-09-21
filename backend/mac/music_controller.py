"""Apple Music playback controls for Saksham's macOS executor."""

import re
from typing import Any, Optional

from core.apple_music_catalog import AppleMusicCatalogClient
from .applescript_bridge import run_applescript


class MusicController:
    """Play library tracks or surface the closest authenticated catalog result."""

    _MOOD_GENRES = {
        "relaxing": "ambient",
        "relax": "ambient",
        "calm": "ambient",
        "calming": "ambient",
        "soothing": "ambient",
        "peaceful": "ambient",
        "meditation": "ambient",
        "unwind": "ambient",
        "sleep": "ambient",
        "focus": "instrumental",
        "study": "instrumental",
        "work": "instrumental",
    }

    def __init__(self, catalog_client: Optional[AppleMusicCatalogClient] = None) -> None:
        self._catalog_client = catalog_client or AppleMusicCatalogClient()

    @classmethod
    def normalize_genre(cls, genre: str) -> str:
        """Return a short, safe genre query for AppleScript."""
        cleaned = re.sub(r"[^A-Za-z0-9 &'/-]+", " ", str(genre or ""))
        cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
        cleaned = re.sub(r"\b(?:music|songs?|tracks?)\b", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -/")
        cleaned = cls._MOOD_GENRES.get(cleaned, cleaned)
        return cleaned[:60]

    async def play_genre(self, genre: str) -> dict:
        """Use the catalog when configured, then fall back to verified library playback."""
        resolved_genre = self.normalize_genre(genre)
        if not resolved_genre:
            return {
                "success": False,
                "action": "play_music",
                "error": "Please tell me which genre you would like to hear.",
            }

        catalog_result = await self._catalog_client.search_best_match(resolved_genre)
        if catalog_result.get("success"):
            return await self._open_catalog_match(resolved_genre, catalog_result["match"])

        # normalize_genre allows only simple ASCII punctuation, so it is safe to
        # embed in this AppleScript string literal.
        script = f'''
        tell application "Music"
            activate
            set matchingTracks to (search library playlist 1 for "{resolved_genre}")
            if (count of matchingTracks) is 0 then
                return "NO_MATCH"
            end if
            set chosenTrack to missing value
            repeat with candidateTrack in matchingTracks
                if (genre of candidateTrack) contains "{resolved_genre}" then
                    set chosenTrack to candidateTrack
                    exit repeat
                end if
            end repeat
            if chosenTrack is missing value then
                set chosenTrack to item 1 of matchingTracks
            end if
            play chosenTrack
            return "PLAYING|" & (name of chosenTrack) & "|" & (artist of chosenTrack)
        end tell
        '''

        try:
            result = await run_applescript(script, timeout=10.0)
        except Exception as error:
            return {
                "success": False,
                "action": "play_music",
                "genre": resolved_genre,
                "error": f"Apple Music could not start playback: {error}",
            }

        if result == "NO_MATCH":
            return {
                "success": False,
                "action": "play_music",
                "genre": resolved_genre,
                "error": f"I couldn't find any {resolved_genre} tracks in your Apple Music library.",
            }

        if not result.startswith("PLAYING|"):
            return {
                "success": False,
                "action": "play_music",
                "genre": resolved_genre,
                "error": "Apple Music did not confirm that playback started.",
            }

        _, track, artist = (result.split("|", 2) + ["", "", ""])[0:3]
        return {
            "success": True,
            "action": "play_music",
            "genre": resolved_genre,
            "track": track or None,
            "artist": artist or None,
        }

    async def _open_catalog_match(self, genre: str, match: dict[str, Any]) -> dict:
        """Open a catalog result in Music without claiming playback that cannot be observed."""
        url = str(match.get("url", ""))
        escaped_url = url.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
        tell application "Music"
            activate
            open location "{escaped_url}"
        end tell
        return "OPENED"
        '''
        try:
            result = await run_applescript(script, timeout=10.0)
        except Exception as error:
            return {
                "success": False,
                "action": "play_music",
                "genre": genre,
                "error": f"Apple Music found a catalog match but could not open it: {error}",
            }

        if result != "OPENED":
            return {
                "success": False,
                "action": "play_music",
                "genre": genre,
                "error": "Apple Music did not confirm that it opened the catalog match.",
            }

        return {
            "success": True,
            "action": "play_music",
            "genre": genre,
            "track": match.get("name"),
            "artist": match.get("artist"),
            "catalog_url": url,
            "catalog_match": True,
            "playback_state": "opened_for_user",
        }

    async def pause(self) -> dict:
        """Pause the currently playing Apple Music track."""
        return await self._run_control("pause", 'tell application "Music" to pause')

    async def resume(self) -> dict:
        """Resume Apple Music playback."""
        return await self._run_control("resume", 'tell application "Music" to play')

    async def next_track(self) -> dict:
        """Advance Apple Music to the next track."""
        return await self._run_control("next", 'tell application "Music" to next track')

    async def _run_control(self, command: str, script: str) -> dict:
        try:
            await run_applescript(script)
            return {"success": True, "action": "music_control", "command": command}
        except Exception as error:
            return {
                "success": False,
                "action": "music_control",
                "command": command,
                "error": f"Apple Music could not {command}: {error}",
            }
