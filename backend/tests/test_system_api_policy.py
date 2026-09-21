import unittest

from fastapi import HTTPException

from api import system


class LegacySystemApiPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def assert_direct_control_blocked(self, awaitable) -> None:
        with self.assertRaises(HTTPException) as raised:
            await awaitable
        self.assertEqual(403, raised.exception.status_code)
        self.assertIn("Command Center", str(raised.exception.detail))

    async def test_terminal_cannot_bypass_executor_policy(self) -> None:
        await self.assert_direct_control_blocked(
            system.run_terminal(system.TerminalCommand(command="rm -rf ~/Downloads")),
        )

    async def test_app_and_browser_controls_cannot_bypass_approval_flow(self) -> None:
        await self.assert_direct_control_blocked(
            system.control_app(system.AppAction(app_name="Finder", action="close")),
        )
        await self.assert_direct_control_blocked(
            system.control_browser(system.BrowserAction(action="click", selector="#purchase")),
        )

    async def test_notification_is_also_audited_task_only(self) -> None:
        await self.assert_direct_control_blocked(
            system.send_notification("Saksham", "This must not bypass task audit"),
        )
