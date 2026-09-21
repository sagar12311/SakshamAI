import unittest
from unittest.mock import AsyncMock, patch

from mac.browser_controller import BrowserController


class BrowserControllerTests(unittest.IsolatedAsyncioTestCase):
    def test_new_chrome_tab_becomes_active(self) -> None:
        controller = BrowserController()

        script = controller._chrome_navigate("https://www.youtube.com/results", True)

        self.assertIn("make new tab", script)
        self.assertIn("set active tab index to (count of tabs)", script)

    @patch("mac.browser_controller.run_applescript", new_callable=AsyncMock)
    async def test_chrome_javascript_uses_valid_applescript_target(self, run_applescript: AsyncMock) -> None:
        run_applescript.return_value = "2"
        controller = BrowserController()

        result = await controller.execute_javascript(
            'document.querySelectorAll("a#video-title").length'
        )

        script = run_applescript.await_args.args[0]
        self.assertTrue(result["success"])
        self.assertIn("execute active tab of front window javascript", script)
        self.assertNotIn("front window's active tab", script)
        self.assertIn('querySelectorAll(\\"a#video-title\\")', script)

    @patch("mac.browser_controller.run_applescript", new_callable=AsyncMock)
    async def test_tracked_tab_lookup_never_falls_back_to_active_tab(self, run_applescript: AsyncMock) -> None:
        run_applescript.return_value = "https://www.amazon.in/s?k=headphones"
        controller = BrowserController()

        result = await controller.get_chrome_tab_url("42")

        script = run_applescript.await_args.args[0]
        self.assertTrue(result["success"])
        self.assertIn("candidateTab", script)
        self.assertIn('is "42"', script)
        self.assertNotIn("active tab of front window", script)

    @patch("mac.browser_controller.run_applescript", new_callable=AsyncMock)
    async def test_activate_tracked_tab_uses_only_the_pinned_tab(self, run_applescript: AsyncMock) -> None:
        run_applescript.return_value = "42"
        controller = BrowserController()

        result = await controller.activate_tracked_chrome_tab("42")

        script = run_applescript.await_args.args[0]
        self.assertTrue(result["success"])
        self.assertIn('is "42"', script)
        self.assertIn("set active tab index of targetWindow", script)

    @patch("mac.browser_controller.run_applescript", new_callable=AsyncMock)
    async def test_reads_active_tab_id_and_url_for_bounded_recovery(self, run_applescript: AsyncMock) -> None:
        run_applescript.return_value = "42\nhttps://www.amazon.in/s?k=lamp"
        controller = BrowserController()

        result = await controller.get_active_chrome_tab()

        self.assertTrue(result["success"])
        self.assertEqual("42", result["tab_id"])
        self.assertEqual("https://www.amazon.in/s?k=lamp", result["url"])

    async def test_tracked_tab_rejects_non_numeric_id(self) -> None:
        controller = BrowserController()
        result = await controller.execute_javascript_in_chrome_tab("1; delete", "'safe'")
        self.assertFalse(result["success"])
        self.assertIn("tab id", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
