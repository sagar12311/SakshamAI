import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from api import tasks as tasks_api
from core.state_store import StateStore, TaskStateError


class CommandCenterTaskStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore()
        self.store._db_path = Path(self.temp_dir.name) / "saksham.db"
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _task(self, task_id: str = "task-1") -> dict:
        return await self.store.create_task(
            "Prepare proposal",
            steps=[
                {"id": "research", "status": "completed"},
                {"id": "draft", "status": "pending"},
            ],
            task_id=task_id,
        )

    async def test_execution_snapshot_is_durable_redacted_and_chronological(self) -> None:
        await self._task()
        execution = await self.store.create_execution(
            "task-1",
            metadata={"api_key": "must-not-persist", "source": "planner"},
        )
        assert execution is not None
        await self.store.update_execution("task-1", execution["id"], {"status": "running"})
        await self.store.update_execution("task-1", execution["id"], {"progress": 55})
        completed = await self.store.update_execution(
            "task-1",
            execution["id"],
            {"status": "completed", "result": {"token": "must-not-persist", "document": "proposal.md"}},
        )
        assert completed is not None

        snapshot = await self.store.get_task_snapshot("task-1")
        assert snapshot is not None
        self.assertEqual("completed", snapshot["status"])
        self.assertEqual(55.0, snapshot["progress"]["percent"])
        self.assertEqual(2, snapshot["plan"]["total_steps"])
        self.assertEqual(1, snapshot["plan"]["completed_steps"])
        self.assertEqual("[REDACTED]", snapshot["execution"]["result"]["token"])
        self.assertEqual("[REDACTED]", snapshot["events"][0]["data"]["api_key"])
        self.assertEqual(
            sorted(event["sequence"] for event in snapshot["events"]),
            [event["sequence"] for event in snapshot["events"]],
        )

        later = await self.store.list_task_events("task-1", after=snapshot["events"][1]["sequence"])
        self.assertTrue(later)
        self.assertTrue(all(event["sequence"] > snapshot["events"][1]["sequence"] for event in later))

    async def test_normal_approval_can_resume_but_admin_requires_verified_activation(self) -> None:
        await self._task()
        execution = await self.store.create_execution("task-1")
        assert execution is not None
        approval = await self.store.request_task_approval(
            "task-1",
            level="confirm",
            summary="Create the project note",
            action={"risk": "reversible"},
            execution_id=execution["id"],
        )
        assert approval is not None
        pending = await self.store.get_task_snapshot("task-1")
        assert pending is not None
        self.assertEqual("awaiting_approval", pending["status"])
        self.assertEqual("confirm", pending["approval"])
        self.assertEqual("Create the project note", pending["approval_reason"])
        self.assertEqual("reversible", pending["risk"])

        resolved = await self.store.resolve_task_approval(
            "task-1", approval["id"], decision="approved"
        )
        assert resolved is not None
        self.assertEqual("approved", resolved["status"])
        resumed = await self.store.get_task_snapshot("task-1")
        assert resumed is not None
        self.assertEqual("queued", resumed["status"])

        admin = await self.store.request_task_approval(
            "task-1", level="admin", summary="Delete 12 files"
        )
        assert admin is not None
        with self.assertRaisesRegex(TaskStateError, "live voice and admin-code"):
            await self.store.resolve_task_approval("task-1", admin["id"], decision="approved")
        rejected = await self.store.resolve_task_approval(
            "task-1", admin["id"], decision="rejected"
        )
        assert rejected is not None
        self.assertEqual("rejected", rejected["status"])

    async def test_cancellation_is_a_request_for_running_work(self) -> None:
        await self._task()
        execution = await self.store.create_execution("task-1", status="running")
        assert execution is not None

        cancellation = await self.store.request_task_cancellation("task-1", reason="User stopped it")
        assert cancellation is not None
        self.assertTrue(cancellation["accepted"])
        self.assertEqual("cancelling", cancellation["state"])
        snapshot = await self.store.get_task_snapshot("task-1")
        assert snapshot is not None
        self.assertEqual("cancelling", snapshot["status"])
        self.assertTrue(snapshot["cancellation_requested"])
        self.assertTrue(snapshot["execution"]["cancellation_requested"])
        self.assertEqual("User stopped it", snapshot["cancellation"]["reason"])


class CommandCenterTaskApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore()
        self.store._db_path = Path(self.temp_dir.name) / "saksham.db"
        await self.store.initialize()
        self.store_patch = patch.object(tasks_api, "get_state_store", return_value=self.store)
        self.store_patch.start()

    async def asyncTearDown(self) -> None:
        self.store_patch.stop()
        self.temp_dir.cleanup()

    async def test_api_returns_enriched_snapshot_and_blocks_admin_shortcut(self) -> None:
        created = await tasks_api.create_task(tasks_api.Task(id="task-api", title="Send summary"))
        self.assertTrue(created["created"])
        listed = await tasks_api.list_tasks()
        self.assertEqual(1, len(listed["tasks"]))
        self.assertIn("progress", listed["tasks"][0])
        self.assertIn("events", listed["tasks"][0])

        execution_response = await tasks_api.create_execution(
            "task-api", tasks_api.ExecutionCreate(status="queued")
        )
        execution_id = execution_response["execution"]["id"]
        requested = await tasks_api.request_task_approval(
            "task-api",
            tasks_api.TaskApprovalRequest(
                level="admin", reason="Delete old files", risk="destructive", execution_id=execution_id
            ),
        )
        self.assertEqual("destructive", requested["approval"]["risk"])
        with self.assertRaises(HTTPException) as raised:
            await tasks_api.resolve_task_approval(
                "task-api",
                requested["approval"]["id"],
                tasks_api.TaskApprovalDecision(decision="approved"),
            )
        self.assertEqual(403, raised.exception.status_code)


if __name__ == "__main__":
    unittest.main()
