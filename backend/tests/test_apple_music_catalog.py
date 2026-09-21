import unittest

import httpx

from core.apple_music_catalog import AppleMusicCatalogClient


class AppleMusicCatalogClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_ranks_a_playlist_that_matches_the_requested_mood(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["Authorization"], "Bearer test-token")
            self.assertEqual(request.url.params["term"], "ambient")
            return httpx.Response(
                200,
                json={
                    "results": {
                        "songs": {
                            "data": [
                                {
                                    "id": "song-1",
                                    "attributes": {
                                        "name": "Loud Morning",
                                        "artistName": "Example Artist",
                                        "genreNames": ["Rock"],
                                        "url": "https://music.apple.com/in/song/loud-morning/1",
                                    },
                                }
                            ]
                        },
                        "playlists": {
                            "data": [
                                {
                                    "id": "playlist-1",
                                    "attributes": {
                                        "name": "Ambient Essentials",
                                        "genreNames": ["Ambient"],
                                        "url": "https://music.apple.com/in/playlist/ambient-essentials/pl.1",
                                    },
                                }
                            ]
                        },
                    }
                },
            )

        client = AppleMusicCatalogClient(
            developer_token="test-token",
            storefront="in",
            transport=httpx.MockTransport(handler),
        )

        result = await client.search_best_match("ambient")

        self.assertTrue(result["success"])
        self.assertEqual(result["match"]["name"], "Ambient Essentials")
        self.assertEqual(result["match"]["kind"], "playlist")

    async def test_reports_missing_configuration_without_network_access(self) -> None:
        client = AppleMusicCatalogClient(developer_token="")

        result = await client.search_best_match("jazz")

        self.assertFalse(result["success"])
        self.assertFalse(result["configured"])

    async def test_preserves_catalog_authentication_errors(self) -> None:
        client = AppleMusicCatalogClient(
            developer_token="test-token",
            transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"errors": []})),
        )

        result = await client.search_best_match("jazz")

        self.assertFalse(result["success"])
        self.assertIn("401", result["error"])


if __name__ == "__main__":
    unittest.main()
