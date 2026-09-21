import json
import unittest

import httpx

from mac.youtube_controller import YouTubeController


class FakeBrowser:
    def __init__(self, candidates=None) -> None:
        self.urls = []
        self.candidates = candidates or [{
            "video_id": "abc123xyz89",
            "title": "Ambient Focus Mix",
            "channel": "Focus Music",
            "index": 0,
        }]

    async def navigate(self, url: str, browser: str, new_tab: bool = True) -> dict:
        self.urls.append(url)
        return {"success": True, "url": url, "browser": browser}

    async def execute_javascript(self, script: str, browser: str) -> dict:
        if "const links" in script:
            return {"success": True, "result": json.dumps(self.candidates)}
        return {"success": True, "result": "PLAYING"}

    async def get_current_url(self, browser: str) -> dict:
        return {"success": True, "url": "https://www.youtube.com/watch?v=abc123xyz89"}


class YouTubeControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_search_selects_and_verifies_a_video_without_api_key(self) -> None:
        browser = FakeBrowser()
        controller = YouTubeController(browser=browser, api_key="")

        result = await controller.play_music("ambient focus")

        self.assertTrue(result["success"])
        self.assertEqual(result["provider"], "youtube")
        self.assertEqual(result["playback_state"], "playing")
        self.assertEqual(result["track"], "Ambient Focus Mix")
        self.assertIn("search_query=ambient+focus+music", browser.urls[0])
        self.assertIn("watch?v=abc123xyz89", browser.urls[1])

    async def test_exact_song_prefers_official_result_over_first_remix(self) -> None:
        browser = FakeBrowser(candidates=[
            {
                "video_id": "wrong123456",
                "title": "Starboy x Another Song (Slowed + Reverb Mashup)",
                "channel": "Random Mixes",
                "index": 0,
            },
            {
                "video_id": "right123456",
                "title": "The Weeknd - Starboy ft. Daft Punk (Official Video)",
                "channel": "The Weeknd",
                "index": 1,
            },
        ])
        controller = YouTubeController(browser=browser, api_key="")

        result = await controller.play_music("play the song Starboy")

        self.assertTrue(result["success"])
        self.assertEqual(
            result["track"],
            "The Weeknd - Starboy ft. Daft Punk (Official Video)",
        )
        self.assertIn("watch?v=right123456", browser.urls[1])

    async def test_exact_song_refuses_unrelated_results(self) -> None:
        browser = FakeBrowser(candidates=[{
            "video_id": "wrong123456",
            "title": "The Weeknd - Blinding Lights (Official Video)",
            "channel": "The Weeknd",
            "index": 0,
        }])
        controller = YouTubeController(browser=browser, api_key="")

        result = await controller.play_music("Starboy")

        self.assertFalse(result["success"])
        self.assertIn("none matched 'Starboy'", result["error"])
        self.assertEqual(len(browser.urls), 1)

    def test_broad_genre_prefers_playlist_over_song_with_genre_in_title(self) -> None:
        selected = YouTubeController._select_best_candidate("rock", [
            {
                "video_id": "single12345",
                "title": "Charli xcx - Rock Music (Official Video)",
                "channel": "Charli xcx",
                "index": 0,
            },
            {
                "video_id": "mix12345678",
                "title": "Best Rock Songs - Classic Rock Hits Playlist",
                "channel": "Rock Radio",
                "index": 1,
            },
        ])

        self.assertIsNotNone(selected)
        self.assertEqual("mix12345678", selected["video_id"])

    async def test_official_api_result_opens_a_direct_watch_url(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params["type"], "video")
            self.assertEqual(request.url.params["key"], "test-key")
            return httpx.Response(200, json={
                "items": [{
                    "id": {"videoId": "abc123xyz89"},
                    "snippet": {"title": "Calm &amp; Focus"},
                }],
            })

        browser = FakeBrowser()
        controller = YouTubeController(
            browser=browser,
            api_key="test-key",
            transport=httpx.MockTransport(handler),
        )

        result = await controller.play_music("calm")

        self.assertTrue(result["success"])
        self.assertEqual(result["track"], "Calm & Focus")
        self.assertIn("watch?v=abc123xyz89", browser.urls[0])


if __name__ == "__main__":
    unittest.main()
