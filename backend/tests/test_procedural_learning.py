import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import main
from api import learning as learning_api
from core.cognition_bus import CognitionMessage, MessageType
from core.procedural_learning import observe_verified_success
from core.state_store import StateStore, TaskStateError


class ProceduralLearningStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore()
        self.store._db_path = Path(self.temp_dir.name) / "saksham.db"
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _completed_task(
        self,
        *,
        task_id: str,
        execution_id: str,
        steps: list[dict],
    ) -> tuple[dict, dict]:
        await self.store.create_task(
            "Routine task token=should-not-persist",
            status="planning",
            steps=steps,
            task_id=task_id,
        )
        execution = await self.store.create_execution(
            task_id,
            status="running",
            execution_id=execution_id,
        )
        assert execution is not None
        completed = await self.store.update_execution(
            task_id,
            execution_id,
            {
                "status": "completed",
                "progress": 100,
                "result": {
                    "results": [
                        {"step": index, "success": True, "result": {"verified": True}}
                        for index, _ in enumerate(steps, start=1)
                    ]
                },
            },
        )
        assert completed is not None
        task = await self.store.get_task(task_id)
        assert task is not None
        return task, completed

    async def test_verified_routine_success_becomes_one_redacted_observed_candidate(self) -> None:
        task, execution = await self._completed_task(
            task_id="routine-task",
            execution_id="routine-execution",
            steps=[
                {
                    "action": "web_search",
                    "target": "https://example.test/?api_key=never-store-this",
                    "parameters": {"api_key": "never-store-this", "query": "Saksham"},
                },
                {"action": "open_app", "target": "Safari"},
            ],
        )

        observed = await observe_verified_success(self.store, task=task, execution=execution)
        self.assertTrue(observed.created)
        self.assertEqual("none", observed.policy_level)
        assert observed.candidate is not None
        candidate = observed.candidate
        self.assertEqual("observed", candidate["status"])
        self.assertEqual("routine-task", candidate["task_id"])
        self.assertEqual("routine-execution", candidate["execution_id"])
        self.assertEqual(64, len(candidate["plan_digest"]))
        self.assertEqual("none", candidate["policy_level"])
        self.assertTrue(candidate["observed_at"])
        self.assertFalse(candidate["execution_enabled"])
        self.assertEqual("[REDACTED]", candidate["plan"][0]["parameters"]["api_key"])
        self.assertIn("[REDACTED]", candidate["plan"][0]["target"])
        self.assertIn("[REDACTED]", candidate["title"])

        duplicate = await observe_verified_success(self.store, task=task, execution=execution)
        self.assertFalse(duplicate.created)
        assert duplicate.candidate is not None
        self.assertEqual(candidate["id"], duplicate.candidate["id"])
        self.assertEqual(1, len(await self.store.list_learning_candidates()))

        events = await self.store.list_task_events("routine-task")
        self.assertEqual(1, len([event for event in events if event["type"] == "learning.candidate_observed"]))

    async def test_confirmation_and_admin_plans_are_not_auto_observed(self) -> None:
        confirmed_task, confirmed_execution = await self._completed_task(
            task_id="confirm-task",
            execution_id="confirm-execution",
            steps=[
                {
                    "action": "file_operation",
                    "target": "/tmp/saksham-fixture/note.txt",
                    "parameters": {"operation": "write"},
                }
            ],
        )
        confirm_result = await observe_verified_success(
            self.store,
            task=confirmed_task,
            execution=confirmed_execution,
        )
        self.assertIsNone(confirm_result.candidate)
        self.assertEqual("confirm", confirm_result.policy_level)
        self.assertEqual("only_routine_plans_are_observed", confirm_result.skipped_reason)

        admin_task, admin_execution = await self._completed_task(
            task_id="admin-task",
            execution_id="admin-execution",
            steps=[{"action": "terminal_command", "target": "echo should-not-learn"}],
        )
        admin_result = await observe_verified_success(
            self.store,
            task=admin_task,
            execution=admin_execution,
        )
        self.assertIsNone(admin_result.candidate)
        self.assertEqual("admin", admin_result.policy_level)
        self.assertEqual([], await self.store.list_learning_candidates())

    async def test_store_rechecks_policy_and_verified_evidence(self) -> None:
        task, execution = await self._completed_task(
            task_id="guarded-task",
            execution_id="guarded-execution",
            steps=[{"action": "terminal_command", "target": "echo no"}],
        )
        from core.procedural_learning import prepare_candidate_plan

        plan, digest = prepare_candidate_plan(task["steps"])
        with self.assertRaisesRegex(TaskStateError, "learning policy level does not match"):
            await self.store.create_learning_candidate(
                task_id=task["id"],
                execution_id=execution["id"],
                title=task["title"],
                plan=plan,
                plan_digest=digest,
                policy_level="none",
                source="test",
            )


class ProceduralLearningApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore()
        self.store._db_path = Path(self.temp_dir.name) / "saksham.db"
        await self.store.initialize()
        self.store_patch = patch.object(learning_api, "get_state_store", return_value=self.store)
        self.store_patch.start()

    async def asyncTearDown(self) -> None:
        self.store_patch.stop()
        self.temp_dir.cleanup()

    async def _candidate(self, suffix: str) -> dict:
        task_id = f"task-{suffix}"
        execution_id = f"execution-{suffix}"
        await self.store.create_task(
            f"Search task {suffix}",
            status="planning",
            steps=[{"action": "web_search", "target": "Saksham"}],
            task_id=task_id,
        )
        await self.store.create_execution(task_id, status="running", execution_id=execution_id)
        execution = await self.store.update_execution(
            task_id,
            execution_id,
            {
                "status": "completed",
                "result": {"results": [{"step": 1, "success": True, "result": {"found": True}}]},
            },
        )
        task = await self.store.get_task(task_id)
        assert task is not None and execution is not None
        observed = await observe_verified_success(self.store, task=task, execution=execution)
        assert observed.candidate is not None
        return observed.candidate

    async def test_list_review_and_delete_have_no_execution_path(self) -> None:
        candidate = await self._candidate("approved")
        listed = await learning_api.list_candidates()
        self.assertTrue(listed["local_only"])
        self.assertFalse(listed["execution_enabled"])
        self.assertEqual([candidate["id"]], [item["id"] for item in listed["candidates"]])

        approved = await learning_api.approve_candidate(
            candidate["id"],
            learning_api.CandidateReview(note="password=do-not-store"),
        )
        self.assertEqual("approved", approved["candidate"]["status"])
        self.assertEqual("password=[REDACTED]", approved["candidate"]["review_note"])
        self.assertFalse(approved["execution_enabled"])

        with self.assertRaises(HTTPException) as already_reviewed:
            await learning_api.reject_candidate(candidate["id"], learning_api.CandidateReview())
        self.assertEqual(409, already_reviewed.exception.status_code)

        deleted = await learning_api.delete_candidate(candidate["id"])
        self.assertTrue(deleted["deleted"])

        rejected_candidate = await self._candidate("rejected")
        rejected = await learning_api.reject_candidate(
            rejected_candidate["id"],
            learning_api.CandidateReview(note="Not useful for a reusable pattern"),
        )
        self.assertEqual("rejected", rejected["candidate"]["status"])
        self.assertFalse(rejected["execution_enabled"])
        self.assertEqual(
            [rejected_candidate["id"]],
            [item["id"] for item in (await learning_api.list_candidates(status="rejected"))["candidates"]],
        )
        await learning_api.delete_candidate(rejected_candidate["id"])
        self.assertEqual([], (await learning_api.list_candidates())["candidates"])


class ProceduralLearningTerminalHookTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_terminal_pipeline_observes_only_verified_routine_completion(self) -> None:
        await self.store.create_task(
            "Research Saksham",
            status="planning",
            steps=[{"action": "web_search", "target": "Saksham"}],
            task_id="pipeline-task",
        )
        await self.store.create_execution(
            "pipeline-task",
            status="running",
            execution_id="pipeline-execution",
        )

        await main.handle_task_terminal(CognitionMessage(
            type=MessageType.TASK_COMPLETE,
            source="executor",
            payload={
                "task_id": "pipeline-task",
                "execution_id": "pipeline-execution",
                "conversation_id": "conversation-1",
                "success": True,
                "results": [{"step": 1, "success": True, "result": {"found": ["Saksham"]}}],
            },
        ))

        candidates = await self.store.list_learning_candidates()
        self.assertEqual(1, len(candidates))
        self.assertEqual("observed", candidates[0]["status"])
        self.assertEqual("pipeline-task", candidates[0]["task_id"])
        snapshot = await self.store.get_task_snapshot("pipeline-task")
        assert snapshot is not None
        self.assertTrue(any(event["type"] == "learning.candidate_observed" for event in snapshot["events"]))


if __name__ == "__main__":
    unittest.main()
