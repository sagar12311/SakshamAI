import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
from core.cognition_bus import CognitionMessage, MessageType
from core.state_store import StateStore


class CommandCenterPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore()
        self.store._db_path = Path(self.temp_dir.name) / "saksham.db"
        await self.store.initialize()
        self.store_patch = patch.object(main, "get_state_store", return_value=self.store)
        self.broadcast_patch = patch.object(main, "_broadcast_command_center_task", new=AsyncMock())
        self.store_patch.start()
        self.broadcast_patch.start()

    async def asyncTearDown(self) -> None:
        self.broadcast_patch.stop()
        self.store_patch.stop()
        self.temp_dir.cleanup()

    async def test_plan_approval_progress_and_completion_form_one_durable_timeline(self) -> None:
        task_id = "command-center-task"
        plan = [
            {"action": "open_app", "target": "Safari", "description": "Open Safari"},
            {"action": "web_search", "target": "Saksham AI", "description": "Research Saksham"},
        ]
        await main.handle_plan_created(CognitionMessage(
            type=MessageType.PLAN_CREATED,
            source="planner",
            payload={
                "task_id": task_id,
                "conversation_id": "conversation-1",
                "intent": "Research Saksham",
                "steps": plan,
            },
        ))
        await main.handle_task_approval_requested(CognitionMessage(
            type=MessageType.TASK_APPROVAL_REQUESTED,
            source="planner",
            payload={
                "task_id": task_id,
                "conversation_id": "conversation-1",
                "plan": plan,
                "approval": {
                    "approval_level": "confirm",
                    "reason": "Writing a personal note requires confirmation",
                    "max_risk": 5,
                },
            },
        ))
        pending = await self.store.get_task_snapshot(task_id)
        assert pending is not None
        self.assertEqual("awaiting_approval", pending["status"])
        self.assertEqual("confirm", pending["approval"])

        approval_id = pending["approval_request"]["id"]
        await self.store.resolve_task_approval(task_id, approval_id, decision="approved")
        execution = await self.store.create_execution(task_id, status="running", execution_id="execution-1")
        assert execution is not None

        await main.handle_task_progress(CognitionMessage(
            type=MessageType.TASK_PROGRESS,
            source="executor",
            payload={
                "task_id": task_id,
                "execution_id": "execution-1",
                "conversation_id": "conversation-1",
                "step": 2,
                "total_steps": 2,
            },
        ))
        await main.handle_task_terminal(CognitionMessage(
            type=MessageType.TASK_COMPLETE,
            source="executor",
            payload={
                "task_id": task_id,
                "execution_id": "execution-1",
                "conversation_id": "conversation-1",
                "success": True,
                "results": [
                    {"step": 1, "success": True, "result": {"opened": "Safari"}},
                    {"step": 2, "success": True, "result": {"results": ["Saksham"]}},
                ],
            },
        ))

        completed = await self.store.get_task_snapshot(task_id)
        assert completed is not None
        self.assertEqual("completed", completed["status"])
        self.assertEqual("completed", completed["execution"]["status"])
        self.assertEqual(["completed", "completed"], [step["status"] for step in completed["steps"]])
        self.assertTrue(any(event["type"] == "approval.requested" for event in completed["events"]))
        self.assertTrue(any(event["type"] == "execution.status" for event in completed["events"]))
