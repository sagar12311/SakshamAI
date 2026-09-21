import unittest
from unittest.mock import AsyncMock

from core.action_policy import ApprovalLevel, ActionPolicy
from agents.executor_agent import ExecutorAgent
from agents.guardian_agent import GuardianAgent


class ActionPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.policy = ActionPolicy()

    def test_read_only_research_does_not_need_affirmation(self) -> None:
        assessment = self.policy.assess({"action": "web_search", "target": "latest Mac security updates"})
        self.assertEqual(ApprovalLevel.NONE, assessment.level)

    def test_file_write_needs_regular_confirmation(self) -> None:
        assessment = self.policy.assess({
            "action": "file_operation", "target": "/tmp/saksham-fixture/Notes/today.md",
            "parameters": {"operation": "write"},
        })
        self.assertEqual(ApprovalLevel.CONFIRM, assessment.level)

    def test_local_configuration_write_needs_regular_confirmation(self) -> None:
        assessment = self.policy.assess({
            "action": "add_app_alias",
            "parameters": {"alias": "work", "app_name": "Google Chrome"},
        })
        self.assertEqual(ApprovalLevel.CONFIRM, assessment.level)

    def test_deletion_and_terminal_require_live_admin_activation(self) -> None:
        deletion = self.policy.assess({
            "action": "file_operation", "target": "/tmp/saksham-fixture/Notes/today.md",
            "parameters": {"operation": "delete"},
        })
        terminal = self.policy.assess({"action": "terminal_command", "target": "rm -rf ~/Downloads/old"})
        self.assertEqual(ApprovalLevel.ADMIN, deletion.level)
        self.assertEqual(ApprovalLevel.ADMIN, terminal.level)

    def test_system_locations_are_blocked(self) -> None:
        assessment = self.policy.assess({
            "action": "file_operation", "target": "/System/Library/a", "parameters": {"operation": "write"},
        })
        self.assertEqual(ApprovalLevel.BLOCKED, assessment.level)

    async def test_direct_executor_command_cannot_bypass_policy(self) -> None:
        executor = ExecutorAgent()
        executor._execute_step = AsyncMock()

        result = await executor._execute_single_action({
            "command": "terminal_command",
            "target": "rm -rf ~/Downloads/old",
        })

        self.assertFalse(result["success"])
        self.assertEqual("admin", result["requires_approval"])
        executor._execute_step.assert_not_awaited()

    async def test_autonomous_mode_cannot_skip_a_personal_confirmation(self) -> None:
        guardian = GuardianAgent()
        guardian._current_mode = "autonomous"
        guardian._require_confirmation = False
        guardian._audit_log = AsyncMock()

        result = await guardian.act({
            "risks": [{"step": 1, "risk_level": 5, "reason": "Writing files requires confirmation"}],
            "max_risk": 5,
            "approval_level": "confirm",
            "requires_confirmation": True,
            "requires_admin_activation": False,
            "should_block": False,
            "reason": "Writing files requires your confirmation",
        })

        self.assertFalse(result["approved"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual("confirm", result["approval_level"])
