import unittest
from unittest.mock import AsyncMock, patch

from core.apple_music_catalog import AppleMusicCatalogClient
from mac.music_controller import MusicController


class MusicControllerTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizes_moods_and_removes_music_words(self) -> None:
        self.assertEqual("ambient", MusicController.normalize_genre("relaxing songs"))
        self.assertEqual("ambient", MusicController.normalize_genre("soothing music"))
        self.assertEqual("lo-fi hip-hop", MusicController.normalize_genre("Lo-Fi Hip-Hop music"))

    @patch("mac.music_controller.run_applescript", new_callable=AsyncMock)
    async def test_play_genre_reports_verified_track(self, run_applescript: AsyncMock) -> None:
        run_applescript.return_value = "PLAYING|Blue in Green|Miles Davis"

        result = await MusicController().play_genre("jazz")

        self.assertTrue(result["success"])
        self.assertEqual("Blue in Green", result["track"])
        self.assertEqual("Miles Davis", result["artist"])
        self.assertIn('search library playlist 1 for "jazz"', run_applescript.await_args.args[0])
        self.assertEqual(10.0, run_applescript.await_args.kwargs["timeout"])

    @patch("mac.music_controller.run_applescript", new_callable=AsyncMock)
    async def test_play_genre_rejects_unverified_output(self, run_applescript: AsyncMock) -> None:
        run_applescript.return_value = ""

        result = await MusicController().play_genre("jazz")

        self.assertFalse(result["success"])
        self.assertIn("did not confirm", result["error"])

    @patch("mac.music_controller.run_applescript", new_callable=AsyncMock)
    async def test_catalog_result_is_not_reported_as_verified_playback(self, run_applescript: AsyncMock) -> None:
        class Catalog:
            async def search_best_match(self, query: str) -> dict:
                return {
                    "success": True,
                    "match": {
                        "name": "Ambient Essentials",
                        "artist": None,
                        "url": "https://music.apple.com/in/playlist/ambient-essentials/pl.1",
                    },
                }

        run_applescript.return_value = "OPENED"
        controller = MusicController(catalog_client=Catalog())

        result = await controller.play_genre("relaxing music")

        self.assertTrue(result["success"])
        self.assertEqual(result["playback_state"], "opened_for_user")
        self.assertEqual(result["track"], "Ambient Essentials")


if __name__ == "__main__":
    unittest.main()
