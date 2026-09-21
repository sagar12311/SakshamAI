import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.executor_agent import ExecutorAgent, issue_trusted_confirmation_context
from agents.planner_agent import PlannerAgent
from core.cognition_bus import CognitionMessage, MessageType


class TaskStartAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.executor = ExecutorAgent()
        self.executor._execute_step = AsyncMock(return_value={"success": True})
        self.executor.broadcast = AsyncMock()

    async def test_direct_confirm_task_start_cannot_bypass_user_confirmation(self) -> None:
        plan = [{
            "action": "file_operation",
            "target": "/tmp/saksham-fixture/Notes/today.md",
            "parameters": {"operation": "write", "content": "hello"},
        }]

        thought = await self.executor.think(CognitionMessage(
            type=MessageType.TASK_START,
            payload={"plan": plan, "task_id": "task-direct", "conversation_id": "test"},
            # A source label alone is not an approval.  Executor also needs
            # the signed, plan-bound context that only Planner mints after a
            # real affirmation.
            source="planner",
        ))
        self.assertEqual("refuse_plan", thought["action"])

        result = await self.executor.act(thought)

        self.assertFalse(result["success"])
        self.assertTrue(result["refused"])
        self.executor._execute_step.assert_not_awaited()

    async def test_context_for_one_plan_cannot_authorize_a_forged_confirm_plan(self) -> None:
        confirmed_plan = [{
            "action": "file_operation",
            "target": "/tmp/saksham-fixture/Notes/today.md",
            "parameters": {"operation": "write", "content": "approved"},
        }]
        context = issue_trusted_confirmation_context(
            confirmed_plan,
            task_id="task-forged",
            conversation_id="test",
        )
        forged_plan = [{
            "action": "file_operation",
            "target": "/tmp/saksham-fixture/Notes/private.md",
            "parameters": {"operation": "write", "content": "not approved"},
        }]

        thought = await self.executor.think(CognitionMessage(
            type=MessageType.TASK_START,
            payload={
                "plan": forged_plan,
                "task_id": "task-forged",
                "conversation_id": "test",
                "confirmation_context": context,
            },
            source="planner",
        ))
        self.assertEqual("refuse_plan", thought["action"])

        await self.executor.act(thought)
        self.executor._execute_step.assert_not_awaited()

    async def test_admin_task_start_fails_closed_even_with_a_valid_normal_context(self) -> None:
        plan = [{
            "action": "terminal_command",
            "target": "rm -rf ~/Downloads/old",
            "parameters": {},
        }]
        context = issue_trusted_confirmation_context(
            plan,
            task_id="task-admin",
            conversation_id="test",
        )

        thought = await self.executor.think(CognitionMessage(
            type=MessageType.TASK_START,
            payload={
                "plan": plan,
                "task_id": "task-admin",
                "conversation_id": "test",
                "confirmation_context": context,
            },
            source="planner",
        ))
        self.assertEqual("refuse_plan", thought["action"])

        result = await self.executor.act(thought)
        self.assertIn("live admin activation", result["error"])
        self.executor._execute_step.assert_not_awaited()

    def test_amazon_order_dispatch_cannot_share_an_admin_plan_with_other_steps(self) -> None:
        plan = [
            {"action": "amazon_search", "target": "headphones", "parameters": {"max_price_inr": 500}},
            {"action": "amazon_place_order", "target": "B012345678", "parameters": {"order_snapshot": {}}},
        ]
        authorization = self.executor._authorize_task_start(
            plan,
            confirmation_context=None,
            source="planner",
            task_id="task-commerce-mixed",
            conversation_id="test",
            admin_capability=None,
            consume_confirmation=False,
        )
        self.assertFalse(authorization["allowed"])
        self.assertIn("one-step", authorization["reason"])

    async def test_planner_user_confirmation_produces_an_executable_bound_context(self) -> None:
        plan = [{
            "action": "file_operation",
            "target": "/tmp/saksham-fixture/Notes/today.md",
            "parameters": {"operation": "write", "content": "confirmed"},
        }]
        planner = PlannerAgent()
        planner._bus = SimpleNamespace(publish=AsyncMock())
        planner._awaiting_confirmation = True
        planner._pending_plan = plan
        planner._pending_intent = "Write the daily note"
        planner._pending_conversation_id = "approval-session"
        planner._pending_nlu = {}
        planner._pending_approval = {"approval_level": "confirm", "requires_confirmation": True}
        planner._pending_task_id = "task-confirmed"

        confirmed_thought = await planner.think(CognitionMessage(
            type=MessageType.USER_INPUT,
            payload={"text": "yes", "conversation_id": "approval-session"},
            source="user",
        ))
        self.assertEqual("execute_pending_plan", confirmed_thought["action"])
        self.assertIsInstance(confirmed_thought["_confirmation_context"], dict)

        planner.send_to_agent = AsyncMock(return_value={"execution_id": "planner-execution"})
        await planner.act(confirmed_thought)
        payload = planner.send_to_agent.await_args.kwargs["payload"]
        self.assertIn("confirmation_context", payload)

        execution_thought = await self.executor.think(CognitionMessage(
            type=MessageType.TASK_START,
            payload=payload,
            source="planner",
        ))
        self.assertEqual("execute_plan", execution_thought["action"])

        store = SimpleNamespace(
            create_execution=AsyncMock(return_value={"id": "planner-execution"}),
            get_execution=AsyncMock(return_value=None),
        )
        with patch("core.state_store.get_state_store", return_value=store):
            result = await self.executor.act(execution_thought)

        self.assertTrue(result["success"])
        self.executor._execute_step.assert_awaited_once()

    async def test_command_center_approval_uses_the_same_bound_context(self) -> None:
        plan = [{
            "action": "file_operation",
            "target": "/tmp/saksham-fixture/Notes/command-center.md",
            "parameters": {"operation": "create"},
        }]
        planner = PlannerAgent()
        planner._bus = SimpleNamespace(publish=AsyncMock())
        planner._awaiting_confirmation = True
        planner._pending_plan = plan
        planner._pending_intent = "Create a note"
        planner._pending_conversation_id = "command-center-session"
        planner._pending_nlu = {}
        planner._pending_approval = {"approval_level": "confirm", "requires_confirmation": True}
        planner._pending_task_id = "task-command-center"

        approved_thought = await planner.think(CognitionMessage(
            type=MessageType.TASK_APPROVAL_DECISION,
            payload={
                "task_id": "task-command-center",
                "approval_id": "approval-command-center",
                "decision": "approved",
            },
            source="tasks_api",
        ))
        self.assertEqual("execute_pending_plan", approved_thought["action"])
        self.assertIsInstance(approved_thought["_confirmation_context"], dict)

        planner.send_to_agent = AsyncMock(return_value={"execution_id": "command-center-execution"})
        await planner.act(approved_thought)
        payload = planner.send_to_agent.await_args.kwargs["payload"]
        self.assertIn("confirmation_context", payload)


if __name__ == "__main__":
    unittest.main()
