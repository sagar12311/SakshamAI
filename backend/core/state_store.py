"""
Saksham AI - Persistent runtime state storage.
Backs lightweight product state with SQLite.
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from config import get_settings
from core.action_policy import ApprovalLevel, get_action_policy
from core.procedural_learning import LearningCandidateError, prepare_candidate_plan, redact_learning_text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Task records existed before the Command Center.  Keep their loose, legacy
# shape intact, but use a small, explicit state model for new execution data.
_EXECUTION_STATUSES = {
    "queued",
    "awaiting_approval",
    "running",
    "cancelling",
    "cancelled",
    "completed",
    "failed",
    "blocked",
}
_ACTIVE_EXECUTION_STATUSES = {"queued", "awaiting_approval", "running", "cancelling"}
_TERMINAL_EXECUTION_STATUSES = {"cancelled", "completed", "failed", "blocked"}
_APPROVAL_LEVELS = {"confirm", "admin"}
_APPROVAL_STATUSES = {"pending", "approved", "rejected", "cancelled", "superseded", "expired"}
_EVENT_LEVELS = {"debug", "info", "success", "warning", "error"}
_LEARNING_CANDIDATE_STATUSES = {"observed", "approved", "rejected"}
_LEARNING_POLICY_LEVELS = {"none", "confirm", "admin", "blocked"}
_SENSITIVE_PAYLOAD_KEYS = {
    "access_token",
    "admin_code",
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "cookie",
    "passcode",
    "password",
    "private_key",
    "secret",
    "session_token",
    "token",
}
_MAX_EVENT_MESSAGE_LENGTH = 4_000
_MAX_EVENT_PAYLOAD_BYTES = 32_000


class TaskStateError(ValueError):
    """Raised when a Command Center state transition is invalid or unsafe."""


def _safe_json_loads(value: Optional[str], default: Any) -> Any:
    """Return valid persisted JSON without making the activity feed fragile."""
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _redact_sensitive_payload(value: Any, *, key: str = "") -> Any:
    """Remove secret-bearing values before they become durable activity data."""
    normalized_key = key.lower().replace("-", "_")
    if normalized_key in _SENSITIVE_PAYLOAD_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(child_key): _redact_sensitive_payload(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive_payload(item, key=key) for item in value]
    return value


def _safe_payload(value: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Validate, redact, and size-bound structured activity payloads."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TaskStateError("Task event data must be an object")

    redacted = _redact_sensitive_payload(value)
    try:
        encoded = json.dumps(redacted, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as error:
        raise TaskStateError("Task event data must be JSON serializable") from error
    if len(encoded.encode("utf-8")) > _MAX_EVENT_PAYLOAD_BYTES:
        raise TaskStateError("Task event data is too large")
    decoded = _safe_json_loads(encoded, {})
    return decoded if isinstance(decoded, dict) else {}


def _validate_choice(value: str, allowed: set[str], field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise TaskStateError(f"Invalid {field_name}; expected one of: {choices}")
    return normalized


def _validate_text(value: str, field_name: str, *, max_length: int = _MAX_EVENT_MESSAGE_LENGTH) -> str:
    text = str(value or "").strip()
    if not text:
        raise TaskStateError(f"{field_name} is required")
    if len(text) > max_length:
        raise TaskStateError(f"{field_name} is too long")
    return text


class StateStore:
    """SQLite-backed storage for runtime mode, tasks, and chat history."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._db_path = self._settings.database_path
        self._initialized = False
        self._init_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_sync(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL,
                    steps_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    type TEXT NOT NULL DEFAULT 'text',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation_created
                ON chat_messages(conversation_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_tasks_status
                ON tasks(status);

                -- Command Center execution history is deliberately separate
                -- from legacy task rows so existing installations migrate
                -- without a destructive table rewrite.
                CREATE TABLE IF NOT EXISTS task_executions (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    current_step_id TEXT,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    cancellation_requested INTEGER NOT NULL DEFAULT 0,
                    cancellation_requested_at TEXT,
                    cancellation_reason TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_task_executions_task_updated
                ON task_executions(task_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_task_executions_active
                ON task_executions(task_id, status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS task_approvals (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    execution_id TEXT,
                    level TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    action_json TEXT NOT NULL DEFAULT '{}',
                    expires_at TEXT,
                    requested_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolution_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_id) REFERENCES task_executions(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_task_approvals_task_status
                ON task_approvals(task_id, status, requested_at DESC);

                CREATE TABLE IF NOT EXISTS task_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    execution_id TEXT,
                    event_type TEXT NOT NULL,
                    level TEXT NOT NULL DEFAULT 'info',
                    message TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_id) REFERENCES task_executions(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_task_events_task_sequence
                ON task_events(task_id, sequence ASC);

                -- Candidates are local, review-gated observations.  They do
                -- not reference task rows with a foreign key so the audit
                -- provenance survives a user deleting the original task.
                CREATE TABLE IF NOT EXISTS procedural_learning_candidates (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    policy_level TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    review_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(task_id, execution_id)
                );

                CREATE INDEX IF NOT EXISTS idx_procedural_learning_status_observed
                ON procedural_learning_candidates(status, observed_at DESC);

                CREATE TABLE IF NOT EXISTS memory_entries (
                    id TEXT PRIMARY KEY,
                    memory_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_entries_type_created
                ON memory_entries(memory_type, created_at);
                """
            )
            db.commit()

    async def initialize(self) -> None:
        """Create required tables on first use."""
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True
            await self.set_mode(self._settings.default_mode, only_if_missing=True)

    def _get_mode_sync(self) -> Optional[str]:
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM runtime_state WHERE key = ?",
                ("current_mode",),
            ).fetchone()
        return str(row["value"]) if row and row["value"] else None

    async def get_mode(self, default_mode: str) -> str:
        """Return the persisted runtime mode."""
        await self.initialize()
        mode = await asyncio.to_thread(self._get_mode_sync)
        if mode:
            return mode

        await self.set_mode(default_mode, only_if_missing=True)
        return default_mode

    def _set_mode_sync(self, mode: str, only_if_missing: bool) -> None:
        now = _utc_now()
        sql = (
            "INSERT OR IGNORE INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)"
            if only_if_missing
            else """
            INSERT INTO runtime_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """
        )
        with self._connect() as db:
            db.execute(sql, ("current_mode", mode, now))
            db.commit()

    async def set_mode(self, mode: str, *, only_if_missing: bool = False) -> None:
        """Persist the runtime mode."""
        await self.initialize()
        await asyncio.to_thread(self._set_mode_sync, mode, only_if_missing)

    def _get_runtime_value_sync(self, key: str) -> Optional[str]:
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM runtime_state WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row and row["value"] else None

    async def get_runtime_value(self, key: str) -> Optional[str]:
        """Read a small JSON or text value used by a local product feature."""
        await self.initialize()
        return await asyncio.to_thread(self._get_runtime_value_sync, key)

    def _set_runtime_value_sync(self, key: str, value: str) -> None:
        now = _utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO runtime_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
            db.commit()

    async def set_runtime_value(self, key: str, value: str) -> None:
        """Persist a small JSON or text value used by a local product feature."""
        await self.initialize()
        await asyncio.to_thread(self._set_runtime_value_sync, key, value)

    def _create_task_sync(
        self,
        title: str,
        description: Optional[str],
        status: str,
        steps: list[Any],
        task_id: Optional[str],
    ) -> dict[str, Any]:
        now = _utc_now()
        stored_id = task_id or str(uuid4())
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO tasks (id, title, description, status, steps_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (stored_id, title, description, status, json.dumps(steps), now, now),
            )
            db.commit()

        return {
            "id": stored_id,
            "title": title,
            "description": description,
            "status": status,
            "steps": steps,
            "created_at": now,
            "updated_at": now,
        }

    async def create_task(
        self,
        title: str,
        description: Optional[str] = None,
        status: str = "pending",
        steps: Optional[list[Any]] = None,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create and persist a task."""
        await self.initialize()
        return await asyncio.to_thread(
            self._create_task_sync,
            title,
            description,
            status,
            steps or [],
            task_id,
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "status": row["status"],
            "steps": _safe_json_loads(row["steps_json"], []),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _get_task_sync(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT id, title, description, status, steps_json, created_at, updated_at
                FROM tasks
                WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
        return self._task_from_row(row) if row else None

    async def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        """Fetch a task by id."""
        await self.initialize()
        return await asyncio.to_thread(self._get_task_sync, task_id)

    def _list_tasks_sync(self, status: Optional[str]) -> list[dict[str, Any]]:
        query = """
            SELECT id, title, description, status, steps_json, created_at, updated_at
            FROM tasks
        """
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY updated_at DESC, created_at DESC"

        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._task_from_row(row) for row in rows]

    async def list_tasks(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        """List persisted tasks, optionally filtered by status."""
        await self.initialize()
        return await asyncio.to_thread(self._list_tasks_sync, status)

    def _update_task_sync(self, task_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
        current = self._get_task_sync(task_id)
        if not current:
            return None

        title = updates.get("title", current["title"])
        description = updates.get("description", current.get("description"))
        status = updates.get("status", current["status"])
        steps = updates.get("steps", current.get("steps", []))
        now = _utc_now()

        with self._connect() as db:
            db.execute(
                """
                UPDATE tasks
                SET title = ?, description = ?, status = ?, steps_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (title, description, status, json.dumps(steps or []), now, task_id),
            )
            db.commit()

        return self._get_task_sync(task_id)

    async def update_task(self, task_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Patch a task and return the updated record."""
        await self.initialize()
        return await asyncio.to_thread(self._update_task_sync, task_id, updates)

    def _delete_task_sync(self, task_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            db.commit()
            return bool(cursor.rowcount)

    async def delete_task(self, task_id: str) -> bool:
        """Delete a task by id."""
        await self.initialize()
        return await asyncio.to_thread(self._delete_task_sync, task_id)

    def _get_active_tasks_sync(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, title, description, status, steps_json, created_at, updated_at
                FROM tasks
                WHERE status IN ('active', 'running', 'in_progress')
                ORDER BY updated_at DESC, created_at DESC
                """
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    async def get_active_tasks(self) -> list[dict[str, Any]]:
        """Return tasks that look active in the UI."""
        await self.initialize()
        return await asyncio.to_thread(self._get_active_tasks_sync)

    # ------------------------------------------------------------------
    # Command Center task execution state
    # ------------------------------------------------------------------
    # These records are append-friendly and intentionally independent of the
    # original tasks table.  That lets an executor report honest state while a
    # UI can poll a stable, durable task timeline after a backend restart.

    @staticmethod
    def _execution_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "status": row["status"],
            "progress": float(row["progress"]),
            "current_step_id": row["current_step_id"],
            "result": _safe_json_loads(row["result_json"], {}),
            "error": row["error"],
            "cancellation_requested": bool(row["cancellation_requested"]),
            "cancellation_requested_at": row["cancellation_requested_at"],
            "cancellation_reason": row["cancellation_reason"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _approval_from_row(row: sqlite3.Row) -> dict[str, Any]:
        action = _safe_json_loads(row["action_json"], {})
        if not isinstance(action, dict):
            action = {}
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "execution_id": row["execution_id"],
            "level": row["level"],
            "status": row["status"],
            "summary": row["summary"],
            # `reason` and `risk` are convenience fields for decision cards;
            # the complete, redacted action context remains available too.
            "reason": row["summary"],
            "risk": action.get("risk"),
            "action": action,
            "expires_at": row["expires_at"],
            "requested_at": row["requested_at"],
            "resolved_at": row["resolved_at"],
            "resolution_note": row["resolution_note"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
        event_type = row["event_type"]
        return {
            "sequence": int(row["sequence"]),
            "id": row["id"],
            "task_id": row["task_id"],
            "execution_id": row["execution_id"],
            # `type` is friendlier for the existing chat/activity UI.  Keep
            # the explicit alias for integrations that use event terminology.
            "type": event_type,
            "event_type": event_type,
            "level": row["level"],
            "message": row["message"],
            "data": _safe_json_loads(row["data_json"], {}),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _task_plan_summary(steps: Any) -> dict[str, int]:
        if not isinstance(steps, list):
            return {"total_steps": 0, "completed_steps": 0}

        completed = 0
        for step in steps:
            if not isinstance(step, dict):
                continue
            status = str(step.get("status", "")).strip().lower()
            if status in {"completed", "complete", "done", "success", "succeeded"}:
                completed += 1
        return {"total_steps": len(steps), "completed_steps": completed}

    @staticmethod
    def _risk_score(risk: Any) -> Optional[int]:
        """Expose a display-safe risk score without inventing a policy result."""
        if isinstance(risk, (int, float)) and not isinstance(risk, bool):
            return max(0, min(10, round(float(risk))))
        normalized = str(risk or "").strip().lower()
        if normalized in {"destructive", "high", "irreversible", "financial", "external"}:
            return 8
        if normalized in {"personal", "write", "moderate", "reversible"}:
            return 5
        if normalized in {"low", "routine", "read_only", "read-only"}:
            return 2
        return None

    @staticmethod
    def _task_from_db(db: sqlite3.Connection, task_id: str) -> Optional[dict[str, Any]]:
        row = db.execute(
            """
            SELECT id, title, description, status, steps_json, created_at, updated_at
            FROM tasks
            WHERE id = ?
            """,
            (task_id,),
        ).fetchone()
        return StateStore._task_from_row(row) if row else None

    @staticmethod
    def _execution_from_db(
        db: sqlite3.Connection,
        execution_id: str,
    ) -> Optional[dict[str, Any]]:
        row = db.execute(
            """
            SELECT id, task_id, status, progress, current_step_id, result_json, error,
                   cancellation_requested, cancellation_requested_at, cancellation_reason,
                   started_at, finished_at, created_at, updated_at
            FROM task_executions
            WHERE id = ?
            """,
            (execution_id,),
        ).fetchone()
        return StateStore._execution_from_row(row) if row else None

    @staticmethod
    def _latest_execution_from_db(
        db: sqlite3.Connection,
        task_id: str,
        *,
        active_only: bool = False,
    ) -> Optional[dict[str, Any]]:
        query = """
            SELECT id, task_id, status, progress, current_step_id, result_json, error,
                   cancellation_requested, cancellation_requested_at, cancellation_reason,
                   started_at, finished_at, created_at, updated_at
            FROM task_executions
            WHERE task_id = ?
        """
        if active_only:
            query += " AND status IN ('queued', 'awaiting_approval', 'running', 'cancelling')"
        query += """
            ORDER BY CASE WHEN status IN ('queued', 'awaiting_approval', 'running', 'cancelling')
                          THEN 0 ELSE 1 END,
                     updated_at DESC,
                     created_at DESC
            LIMIT 1
        """
        row = db.execute(query, (task_id,)).fetchone()
        return StateStore._execution_from_row(row) if row else None

    @staticmethod
    def _approval_from_db(
        db: sqlite3.Connection,
        approval_id: str,
    ) -> Optional[dict[str, Any]]:
        row = db.execute(
            """
            SELECT id, task_id, execution_id, level, status, summary, action_json,
                   expires_at, requested_at, resolved_at, resolution_note, created_at, updated_at
            FROM task_approvals
            WHERE id = ?
            """,
            (approval_id,),
        ).fetchone()
        return StateStore._approval_from_row(row) if row else None

    @staticmethod
    def _latest_approval_from_db(
        db: sqlite3.Connection,
        task_id: str,
    ) -> Optional[dict[str, Any]]:
        row = db.execute(
            """
            SELECT id, task_id, execution_id, level, status, summary, action_json,
                   expires_at, requested_at, resolved_at, resolution_note, created_at, updated_at
            FROM task_approvals
            WHERE task_id = ?
            ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
                     requested_at DESC,
                     created_at DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        return StateStore._approval_from_row(row) if row else None

    @staticmethod
    def _normalize_event_type(event_type: str) -> str:
        normalized = _validate_text(event_type, "event type", max_length=80).lower()
        if not all(character.isalnum() or character in {"_", "-", "."} for character in normalized):
            raise TaskStateError("event type can only contain letters, numbers, _, -, and .")
        return normalized

    def _insert_task_event(
        self,
        db: sqlite3.Connection,
        *,
        task_id: str,
        event_type: str,
        message: str,
        execution_id: Optional[str] = None,
        level: str = "info",
        data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        event_id = str(uuid4())
        now = _utc_now()
        normalized_type = self._normalize_event_type(event_type)
        normalized_level = _validate_choice(level, _EVENT_LEVELS, "event level")
        safe_message = _validate_text(message, "event message")
        payload = _safe_payload(data)
        db.execute(
            """
            INSERT INTO task_events
                (id, task_id, execution_id, event_type, level, message, data_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                task_id,
                execution_id,
                normalized_type,
                normalized_level,
                safe_message,
                json.dumps(payload, ensure_ascii=False),
                now,
            ),
        )
        row = db.execute(
            """
            SELECT sequence, id, task_id, execution_id, event_type, level, message, data_json, created_at
            FROM task_events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        return self._event_from_row(row)

    @staticmethod
    def _task_status_for_execution(status: str) -> str:
        return {
            "queued": "queued",
            "awaiting_approval": "awaiting_approval",
            "running": "running",
            "cancelling": "cancelling",
            "cancelled": "cancelled",
            "completed": "completed",
            "failed": "failed",
            "blocked": "blocked",
        }[status]

    def _list_task_events_from_db(
        self,
        db: sqlite3.Connection,
        task_id: str,
        *,
        limit: int,
        after: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 200))
        select = """
            SELECT sequence, id, task_id, execution_id, event_type, level, message, data_json, created_at
            FROM task_events
            WHERE task_id = ?
        """
        if after is not None:
            rows = db.execute(
                select + " AND sequence > ? ORDER BY sequence ASC LIMIT ?",
                (task_id, max(0, int(after)), bounded_limit),
            ).fetchall()
            return [self._event_from_row(row) for row in rows]

        # Read the newest page efficiently, then restore chronological order
        # for rendering and incremental polling.
        rows = db.execute(
            select + " ORDER BY sequence DESC LIMIT ?",
            (task_id, bounded_limit),
        ).fetchall()
        return [self._event_from_row(row) for row in reversed(rows)]

    def _task_snapshot_from_db(
        self,
        db: sqlite3.Connection,
        task_id: str,
        *,
        event_limit: int,
    ) -> Optional[dict[str, Any]]:
        task = self._task_from_db(db, task_id)
        if not task:
            return None

        execution = self._latest_execution_from_db(db, task_id)
        approval = self._latest_approval_from_db(db, task_id)
        plan_summary = self._task_plan_summary(task["steps"])
        progress_percent = execution["progress"] if execution else (
            round((plan_summary["completed_steps"] / plan_summary["total_steps"]) * 100, 2)
            if plan_summary["total_steps"]
            else 0.0
        )
        completed_steps = plan_summary["completed_steps"]
        total_steps = plan_summary["total_steps"]
        if execution and total_steps:
            if execution["status"] == "completed":
                completed_steps = total_steps
            else:
                completed_steps = max(
                    completed_steps,
                    min(total_steps, round((progress_percent / 100) * total_steps)),
                )
        pending_approval = approval if approval and approval["status"] == "pending" else None
        approval_level = pending_approval["level"] if pending_approval else "none"
        cancellation = {
            "requested": bool(execution and execution["cancellation_requested"]),
            "requested_at": execution.get("cancellation_requested_at") if execution else None,
            "reason": execution.get("cancellation_reason") if execution else None,
        }
        return {
            **task,
            "plan": {
                "steps": task["steps"],
                # This reflects the persisted plan step statuses exactly;
                # execution percent is kept separately below until the runner
                # writes the individual step outcome.
                **plan_summary,
            },
            "execution": execution,
            # Flat aliases allow the React Command Center to render this
            # snapshot directly.  Keep the full request for audit/detail UI.
            "approval": approval_level,
            "approval_level": approval_level,
            "approval_reason": pending_approval["reason"] if pending_approval else None,
            "risk": pending_approval["risk"] if pending_approval else None,
            "risk_score": self._risk_score(pending_approval["risk"]) if pending_approval else None,
            "approval_request": approval,
            "progress": {
                "completed": completed_steps,
                "total": total_steps,
                "percent": progress_percent,
                "label": execution.get("current_step_id") if execution else None,
            },
            "progress_percent": progress_percent,
            "cancellation": cancellation,
            # The flat flag keeps polling clients simple and does not obscure
            # whether an external executor has actually stopped yet.
            "cancellation_requested": cancellation["requested"],
            "events": (
                self._list_task_events_from_db(db, task_id, limit=event_limit)
                if event_limit > 0
                else []
            ),
        }

    def _get_task_snapshot_sync(
        self,
        task_id: str,
        event_limit: int,
    ) -> Optional[dict[str, Any]]:
        with self._connect() as db:
            return self._task_snapshot_from_db(db, task_id, event_limit=event_limit)

    async def get_task_snapshot(
        self,
        task_id: str,
        *,
        event_limit: int = 50,
    ) -> Optional[dict[str, Any]]:
        """Return a task with its current execution, approval, and timeline."""
        await self.initialize()
        return await asyncio.to_thread(self._get_task_snapshot_sync, task_id, event_limit)

    def _list_task_snapshots_sync(
        self,
        status: Optional[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 200))
        query = "SELECT id FROM tasks"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
        with self._connect() as db:
            rows = db.execute(query, (*params, bounded_limit)).fetchall()
            return [
                snapshot
                for row in rows
                if (snapshot := self._task_snapshot_from_db(db, row["id"], event_limit=0))
            ]

    async def list_task_snapshots(
        self,
        status: Optional[str] = None,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List compact Command Center task cards without loading timelines."""
        await self.initialize()
        return await asyncio.to_thread(self._list_task_snapshots_sync, status, limit)

    def _create_execution_sync(
        self,
        task_id: str,
        status: str,
        progress: float,
        current_step_id: Optional[str],
        metadata: Optional[dict[str, Any]],
        execution_id: Optional[str],
    ) -> Optional[dict[str, Any]]:
        normalized_status = _validate_choice(status, _EXECUTION_STATUSES, "execution status")
        if isinstance(progress, bool) or not isinstance(progress, (int, float)):
            raise TaskStateError("execution progress must be a number")
        normalized_progress = float(progress)
        if not 0 <= normalized_progress <= 100:
            raise TaskStateError("execution progress must be between 0 and 100")
        safe_metadata = _safe_payload(metadata)
        stored_id = execution_id or str(uuid4())
        now = _utc_now()
        started_at = now if normalized_status == "running" else None
        finished_at = now if normalized_status in _TERMINAL_EXECUTION_STATUSES else None

        with self._connect() as db:
            if not self._task_from_db(db, task_id):
                return None
            db.execute(
                """
                INSERT INTO task_executions
                    (id, task_id, status, progress, current_step_id, result_json, error,
                     cancellation_requested, cancellation_requested_at, cancellation_reason,
                     started_at, finished_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, '{}', NULL, 0, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    stored_id,
                    task_id,
                    normalized_status,
                    normalized_progress,
                    current_step_id,
                    started_at,
                    finished_at,
                    now,
                    now,
                ),
            )
            db.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (self._task_status_for_execution(normalized_status), now, task_id),
            )
            self._insert_task_event(
                db,
                task_id=task_id,
                execution_id=stored_id,
                event_type="execution.created",
                message=f"Execution {normalized_status}",
                data={"status": normalized_status, "progress": normalized_progress, **safe_metadata},
            )
            db.commit()
            return self._execution_from_db(db, stored_id)

    async def create_execution(
        self,
        task_id: str,
        *,
        status: str = "queued",
        progress: float = 0,
        current_step_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        execution_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Start a durable task execution and add its first timeline event."""
        await self.initialize()
        return await asyncio.to_thread(
            self._create_execution_sync,
            task_id,
            status,
            progress,
            current_step_id,
            metadata,
            execution_id,
        )

    def _get_execution_sync(self, execution_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as db:
            return self._execution_from_db(db, execution_id)

    async def get_execution(self, execution_id: str) -> Optional[dict[str, Any]]:
        """Fetch an execution by id."""
        await self.initialize()
        return await asyncio.to_thread(self._get_execution_sync, execution_id)

    def _list_task_executions_sync(self, task_id: str, limit: int) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, task_id, status, progress, current_step_id, result_json, error,
                       cancellation_requested, cancellation_requested_at, cancellation_reason,
                       started_at, finished_at, created_at, updated_at
                FROM task_executions
                WHERE task_id = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (task_id, bounded_limit),
            ).fetchall()
        return [self._execution_from_row(row) for row in rows]

    async def list_task_executions(self, task_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """List execution attempts for a task, newest first."""
        await self.initialize()
        return await asyncio.to_thread(self._list_task_executions_sync, task_id, limit)

    @staticmethod
    def _can_transition_execution(current: str, next_status: str) -> bool:
        if current == next_status:
            return True
        allowed = {
            "queued": {"awaiting_approval", "running", "cancelling", "cancelled", "completed", "failed", "blocked"},
            "awaiting_approval": {"queued", "cancelling", "cancelled", "failed", "blocked"},
            "running": {"cancelling", "cancelled", "completed", "failed", "blocked"},
            # A task can complete just before the cancellation signal reaches
            # the executor, so completed remains an honest terminal outcome.
            "cancelling": {"cancelled", "completed", "failed"},
        }
        return next_status in allowed.get(current, set())

    def _update_execution_sync(
        self,
        task_id: str,
        execution_id: str,
        updates: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        allowed_fields = {"status", "progress", "current_step_id", "result", "error"}
        unexpected = set(updates) - allowed_fields
        if unexpected:
            raise TaskStateError(f"Unsupported execution fields: {', '.join(sorted(unexpected))}")
        if not updates:
            raise TaskStateError("At least one execution field is required")

        with self._connect() as db:
            current = self._execution_from_db(db, execution_id)
            if not current or current["task_id"] != task_id:
                return None

            status = current["status"]
            if "status" in updates:
                status = _validate_choice(updates["status"], _EXECUTION_STATUSES, "execution status")
                if not self._can_transition_execution(current["status"], status):
                    raise TaskStateError(
                        f"Cannot transition execution from {current['status']} to {status}"
                    )

            progress = current["progress"]
            if "progress" in updates:
                supplied_progress = updates["progress"]
                if isinstance(supplied_progress, bool) or not isinstance(supplied_progress, (int, float)):
                    raise TaskStateError("execution progress must be a number")
                progress = float(supplied_progress)
                if not 0 <= progress <= 100:
                    raise TaskStateError("execution progress must be between 0 and 100")

            current_step_id = (
                updates["current_step_id"]
                if "current_step_id" in updates
                else current["current_step_id"]
            )
            if current_step_id is not None and len(str(current_step_id)) > 256:
                raise TaskStateError("current step id is too long")

            result = current["result"]
            if "result" in updates:
                result = _safe_payload(updates["result"])

            error = current["error"]
            if "error" in updates:
                supplied_error = updates["error"]
                error = None if supplied_error is None else _validate_text(
                    supplied_error, "execution error"
                )

            now = _utc_now()
            started_at = current["started_at"] or (now if status == "running" else None)
            finished_at = current["finished_at"]
            if status in _TERMINAL_EXECUTION_STATUSES and not finished_at:
                finished_at = now

            db.execute(
                """
                UPDATE task_executions
                SET status = ?, progress = ?, current_step_id = ?, result_json = ?, error = ?,
                    started_at = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    progress,
                    current_step_id,
                    json.dumps(result, ensure_ascii=False),
                    error,
                    started_at,
                    finished_at,
                    now,
                    execution_id,
                ),
            )
            db.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (self._task_status_for_execution(status), now, task_id),
            )

            if "status" in updates and status != current["status"]:
                event_type = "execution.status"
                message = f"Execution status changed to {status}"
                level = "error" if status == "failed" else "success" if status == "completed" else "info"
            elif "result" in updates:
                event_type = "execution.result"
                message = "Execution result updated"
                level = "success" if status == "completed" else "info"
            elif "error" in updates:
                event_type = "execution.error"
                message = "Execution error updated"
                level = "error"
            else:
                event_type = "execution.progress"
                message = "Execution progress updated"
                level = "info"
            self._insert_task_event(
                db,
                task_id=task_id,
                execution_id=execution_id,
                event_type=event_type,
                message=message,
                level=level,
                data={
                    "status": status,
                    "progress": progress,
                    "current_step_id": current_step_id,
                    "has_result": bool(result),
                    "has_error": bool(error),
                },
            )
            db.commit()
            return self._execution_from_db(db, execution_id)

    async def update_execution(
        self,
        task_id: str,
        execution_id: str,
        updates: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Persist one validated execution transition and its timeline event."""
        await self.initialize()
        return await asyncio.to_thread(self._update_execution_sync, task_id, execution_id, updates)

    def _record_task_event_sync(
        self,
        task_id: str,
        event_type: str,
        message: str,
        execution_id: Optional[str],
        level: str,
        data: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        with self._connect() as db:
            if not self._task_from_db(db, task_id):
                return None
            if execution_id:
                execution = self._execution_from_db(db, execution_id)
                if not execution or execution["task_id"] != task_id:
                    raise TaskStateError("Execution does not belong to this task")
            event = self._insert_task_event(
                db,
                task_id=task_id,
                execution_id=execution_id,
                event_type=event_type,
                message=message,
                level=level,
                data=data,
            )
            db.commit()
            return event

    async def record_task_event(
        self,
        task_id: str,
        event_type: str,
        message: str,
        *,
        execution_id: Optional[str] = None,
        level: str = "info",
        data: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Append one redacted, durable event to a task's activity timeline."""
        await self.initialize()
        return await asyncio.to_thread(
            self._record_task_event_sync,
            task_id,
            event_type,
            message,
            execution_id,
            level,
            data,
        )

    def _list_task_events_sync(
        self,
        task_id: str,
        limit: int,
        after: Optional[int],
    ) -> list[dict[str, Any]]:
        with self._connect() as db:
            return self._list_task_events_from_db(db, task_id, limit=limit, after=after)

    async def list_task_events(
        self,
        task_id: str,
        *,
        limit: int = 100,
        after: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """List task events chronologically; `after` supports incremental polling."""
        await self.initialize()
        return await asyncio.to_thread(self._list_task_events_sync, task_id, limit, after)

    def _request_task_approval_sync(
        self,
        task_id: str,
        level: str,
        summary: str,
        action: Optional[dict[str, Any]],
        execution_id: Optional[str],
        expires_at: Optional[str],
        approval_id: Optional[str],
    ) -> Optional[dict[str, Any]]:
        normalized_level = _validate_choice(level, _APPROVAL_LEVELS, "approval level")
        safe_summary = _validate_text(summary, "approval summary")
        safe_action = _safe_payload(action)
        if expires_at is not None and len(str(expires_at)) > 128:
            raise TaskStateError("approval expiry is too long")

        now = _utc_now()
        stored_id = approval_id or str(uuid4())
        with self._connect() as db:
            if not self._task_from_db(db, task_id):
                return None
            if execution_id:
                execution = self._execution_from_db(db, execution_id)
                if not execution or execution["task_id"] != task_id:
                    raise TaskStateError("Execution does not belong to this task")

            # A task should have one actionable decision at a time.  Keeping
            # older requests in the history preserves the audit trail.
            db.execute(
                """
                UPDATE task_approvals
                SET status = 'superseded', resolved_at = ?, updated_at = ?
                WHERE task_id = ? AND status = 'pending'
                """,
                (now, now, task_id),
            )
            db.execute(
                """
                INSERT INTO task_approvals
                    (id, task_id, execution_id, level, status, summary, action_json,
                     expires_at, requested_at, resolved_at, resolution_note, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    stored_id,
                    task_id,
                    execution_id,
                    normalized_level,
                    safe_summary,
                    json.dumps(safe_action, ensure_ascii=False),
                    expires_at,
                    now,
                    now,
                    now,
                ),
            )
            db.execute(
                "UPDATE tasks SET status = 'awaiting_approval', updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            if execution_id:
                db.execute(
                    """
                    UPDATE task_executions
                    SET status = 'awaiting_approval', updated_at = ?
                    WHERE id = ? AND status IN ('queued', 'running', 'awaiting_approval')
                    """,
                    (now, execution_id),
                )
            self._insert_task_event(
                db,
                task_id=task_id,
                execution_id=execution_id,
                event_type="approval.requested",
                message="Approval requested",
                level="warning" if normalized_level == "admin" else "info",
                data={"approval_id": stored_id, "level": normalized_level, "summary": safe_summary},
            )
            db.commit()
            return self._approval_from_db(db, stored_id)

    async def request_task_approval(
        self,
        task_id: str,
        *,
        level: str,
        summary: str,
        action: Optional[dict[str, Any]] = None,
        execution_id: Optional[str] = None,
        expires_at: Optional[str] = None,
        approval_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Create a pending, auditable approval request for a task."""
        await self.initialize()
        return await asyncio.to_thread(
            self._request_task_approval_sync,
            task_id,
            level,
            summary,
            action,
            execution_id,
            expires_at,
            approval_id,
        )

    def _resolve_task_approval_sync(
        self,
        task_id: str,
        approval_id: str,
        decision: str,
        note: Optional[str],
        *,
        admin_verified: bool,
    ) -> Optional[dict[str, Any]]:
        normalized_decision = _validate_choice(decision, {"approved", "rejected"}, "approval decision")
        safe_note = None if note is None else _validate_text(note, "approval note")
        now = _utc_now()
        with self._connect() as db:
            approval = self._approval_from_db(db, approval_id)
            if not approval or approval["task_id"] != task_id:
                return None
            if approval["status"] != "pending":
                raise TaskStateError(f"Approval is already {approval['status']}")
            if approval["level"] == "admin" and normalized_decision == "approved" and not admin_verified:
                raise TaskStateError(
                    "Admin approval requires verified live voice and admin-code activation"
                )

            db.execute(
                """
                UPDATE task_approvals
                SET status = ?, resolved_at = ?, resolution_note = ?, updated_at = ?
                WHERE id = ?
                """,
                (normalized_decision, now, safe_note, now, approval_id),
            )
            task_status = "queued" if normalized_decision == "approved" else "blocked"
            db.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (task_status, now, task_id),
            )
            if approval["execution_id"]:
                execution_status = "queued" if normalized_decision == "approved" else "blocked"
                db.execute(
                    """
                    UPDATE task_executions
                    SET status = ?, finished_at = CASE WHEN ? = 'blocked' THEN ? ELSE finished_at END,
                        updated_at = ?
                    WHERE id = ? AND status = 'awaiting_approval'
                    """,
                    (execution_status, execution_status, now, now, approval["execution_id"]),
                )
            self._insert_task_event(
                db,
                task_id=task_id,
                execution_id=approval["execution_id"],
                event_type=f"approval.{normalized_decision}",
                message=("Approval granted" if normalized_decision == "approved" else "Approval rejected"),
                level="success" if normalized_decision == "approved" else "warning",
                data={"approval_id": approval_id, "level": approval["level"], "note": safe_note},
            )
            db.commit()
            return self._approval_from_db(db, approval_id)

    async def resolve_task_approval(
        self,
        task_id: str,
        approval_id: str,
        *,
        decision: str,
        note: Optional[str] = None,
        admin_verified: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Resolve an approval; admin decisions require a verified caller."""
        await self.initialize()
        return await asyncio.to_thread(
            self._resolve_task_approval_sync,
            task_id,
            approval_id,
            decision,
            note,
            admin_verified=admin_verified,
        )

    def _request_task_cancellation_sync(
        self,
        task_id: str,
        reason: Optional[str],
    ) -> Optional[dict[str, Any]]:
        safe_reason = None if reason is None else _validate_text(reason, "cancellation reason")
        now = _utc_now()
        with self._connect() as db:
            task = self._task_from_db(db, task_id)
            if not task:
                return None
            if task["status"] in {"cancelled", "completed", "failed", "blocked"}:
                return {
                    "accepted": False,
                    "already_requested": False,
                    "state": task["status"],
                    "execution_id": None,
                }

            execution = self._latest_execution_from_db(db, task_id, active_only=True)
            if execution and execution["cancellation_requested"]:
                return {
                    "accepted": True,
                    "already_requested": True,
                    "state": "cancelling",
                    "execution_id": execution["id"],
                }

            # Pending approval and queued work have not started a side effect,
            # so cancelling them is final.  A running execution only receives
            # a cancellation *request*; it must later report its real outcome.
            if execution and execution["status"] == "running":
                state = "cancelling"
                execution_status = "cancelling"
                finished_at = execution["finished_at"]
            elif execution:
                state = "cancelled"
                execution_status = "cancelled"
                finished_at = now
            else:
                state = "cancelled"
                execution_status = None
                finished_at = None

            if execution:
                db.execute(
                    """
                    UPDATE task_executions
                    SET status = ?, cancellation_requested = 1, cancellation_requested_at = ?,
                        cancellation_reason = ?, finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (execution_status, now, safe_reason, finished_at, now, execution["id"]),
                )
            db.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (state, now, task_id),
            )
            db.execute(
                """
                UPDATE task_approvals
                SET status = 'cancelled', resolved_at = ?, updated_at = ?
                WHERE task_id = ? AND status = 'pending'
                """,
                (now, now, task_id),
            )
            self._insert_task_event(
                db,
                task_id=task_id,
                execution_id=execution["id"] if execution else None,
                event_type="execution.cancellation_requested",
                message=(
                    "Cancellation requested from the active executor"
                    if state == "cancelling"
                    else "Task cancelled before execution"
                ),
                level="warning",
                data={"reason": safe_reason, "state": state},
            )
            db.commit()
            return {
                "accepted": True,
                "already_requested": False,
                "state": state,
                "execution_id": execution["id"] if execution else None,
            }

    async def request_task_cancellation(
        self,
        task_id: str,
        *,
        reason: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Record a cancellation request without claiming an executor stopped."""
        await self.initialize()
        return await asyncio.to_thread(self._request_task_cancellation_sync, task_id, reason)

    # ------------------------------------------------------------------
    # Review-gated procedural learning candidates
    # ------------------------------------------------------------------
    # These records are intentionally not part of planner or executor state.
    # An "approved" candidate means it has passed human review for later
    # product work; it is never an execution permission or runnable workflow.

    @staticmethod
    def _learning_candidate_from_row(row: sqlite3.Row) -> dict[str, Any]:
        plan = _safe_json_loads(row["plan_json"], [])
        if not isinstance(plan, list):
            plan = []
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "execution_id": row["execution_id"],
            "title": row["title"],
            "plan": plan,
            "plan_digest": row["plan_digest"],
            "policy_level": row["policy_level"],
            "status": row["status"],
            "source": row["source"],
            "observed_at": row["observed_at"],
            "reviewed_at": row["reviewed_at"],
            "review_note": row["review_note"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            # This explicit flag makes the non-execution contract visible to
            # every API consumer rather than relying on documentation alone.
            "execution_enabled": False,
        }

    @staticmethod
    def _learning_candidate_from_db(
        db: sqlite3.Connection,
        candidate_id: str,
    ) -> Optional[dict[str, Any]]:
        row = db.execute(
            """
            SELECT id, task_id, execution_id, title, plan_json, plan_digest,
                   policy_level, status, source, observed_at, reviewed_at,
                   review_note, created_at, updated_at
            FROM procedural_learning_candidates
            WHERE id = ?
            """,
            (candidate_id,),
        ).fetchone()
        return StateStore._learning_candidate_from_row(row) if row else None

    @staticmethod
    def _learning_candidate_for_execution_from_db(
        db: sqlite3.Connection,
        task_id: str,
        execution_id: str,
    ) -> Optional[dict[str, Any]]:
        row = db.execute(
            """
            SELECT id, task_id, execution_id, title, plan_json, plan_digest,
                   policy_level, status, source, observed_at, reviewed_at,
                   review_note, created_at, updated_at
            FROM procedural_learning_candidates
            WHERE task_id = ? AND execution_id = ?
            """,
            (task_id, execution_id),
        ).fetchone()
        return StateStore._learning_candidate_from_row(row) if row else None

    @staticmethod
    def _verified_execution_evidence(execution: dict[str, Any]) -> bool:
        """Ensure automatic observation has executor-produced success evidence."""
        if execution.get("status") != "completed":
            return False
        result = execution.get("result")
        if not isinstance(result, dict):
            return False
        outcomes = result.get("results")
        return bool(outcomes) and isinstance(outcomes, list) and all(
            isinstance(item, dict) and item.get("success") is True for item in outcomes
        )

    def _create_learning_candidate_sync(
        self,
        task_id: str,
        execution_id: str,
        title: str,
        plan: list[dict[str, Any]],
        plan_digest: str,
        policy_level: str,
        source: str,
        candidate_id: Optional[str],
    ) -> tuple[Optional[dict[str, Any]], bool]:
        safe_task_id = _validate_text(task_id, "learning task id", max_length=256)
        safe_execution_id = _validate_text(execution_id, "learning execution id", max_length=256)
        safe_title = redact_learning_text(_validate_text(title, "learning title", max_length=500))
        safe_source = _validate_text(source, "learning source", max_length=128).lower()
        if not all(character.isalnum() or character in {"_", "-", "."} for character in safe_source):
            raise TaskStateError("learning source can only contain letters, numbers, _, -, and .")
        normalized_policy_level = _validate_choice(
            policy_level,
            _LEARNING_POLICY_LEVELS,
            "learning policy level",
        )
        if normalized_policy_level != ApprovalLevel.NONE.value:
            raise TaskStateError("Only routine plans can become automatic learning candidates")
        try:
            safe_plan, computed_digest = prepare_candidate_plan(plan)
        except LearningCandidateError as error:
            raise TaskStateError(str(error)) from error
        assessed_level = get_action_policy().assess_plan(safe_plan).level.value
        if assessed_level != normalized_policy_level:
            raise TaskStateError("learning policy level does not match the plan")
        supplied_digest = str(plan_digest or "").strip().lower()
        if len(supplied_digest) != 64 or any(
            character not in "0123456789abcdef" for character in supplied_digest
        ):
            raise TaskStateError("learning plan digest must be a SHA-256 hex digest")
        if supplied_digest != computed_digest:
            raise TaskStateError("learning plan digest does not match the redacted plan")

        now = _utc_now()
        stored_id = candidate_id or str(uuid4())
        with self._connect() as db:
            task = self._task_from_db(db, safe_task_id)
            execution = self._execution_from_db(db, safe_execution_id)
            if not task:
                return None, False
            if not execution or execution["task_id"] != safe_task_id:
                raise TaskStateError("Learning execution does not belong to this task")
            if task["status"] != "completed" or not self._verified_execution_evidence(execution):
                raise TaskStateError("Learning candidates require a verified completed execution")

            # Completion events can be delivered more than once after a
            # reconnect.  Keep candidate creation idempotent for audit truth.
            existing = self._learning_candidate_for_execution_from_db(
                db,
                safe_task_id,
                safe_execution_id,
            )
            if existing:
                return existing, False

            db.execute(
                """
                INSERT INTO procedural_learning_candidates
                    (id, task_id, execution_id, title, plan_json, plan_digest,
                     policy_level, status, source, observed_at, reviewed_at,
                     review_note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'observed', ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    stored_id,
                    safe_task_id,
                    safe_execution_id,
                    safe_title,
                    json.dumps(safe_plan, ensure_ascii=False),
                    supplied_digest,
                    normalized_policy_level,
                    safe_source,
                    now,
                    now,
                    now,
                ),
            )
            db.commit()
            return self._learning_candidate_from_db(db, stored_id), True

    async def create_learning_candidate(
        self,
        *,
        task_id: str,
        execution_id: str,
        title: str,
        plan: list[dict[str, Any]],
        plan_digest: str,
        policy_level: str,
        source: str,
        candidate_id: Optional[str] = None,
    ) -> tuple[Optional[dict[str, Any]], bool]:
        """Persist an observed candidate; this never schedules or executes it."""
        await self.initialize()
        return await asyncio.to_thread(
            self._create_learning_candidate_sync,
            task_id,
            execution_id,
            title,
            plan,
            plan_digest,
            policy_level,
            source,
            candidate_id,
        )

    def _get_learning_candidate_sync(self, candidate_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as db:
            return self._learning_candidate_from_db(db, candidate_id)

    async def get_learning_candidate(self, candidate_id: str) -> Optional[dict[str, Any]]:
        """Read one review-only learning candidate."""
        await self.initialize()
        return await asyncio.to_thread(self._get_learning_candidate_sync, candidate_id)

    def _list_learning_candidates_sync(
        self,
        status: Optional[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        normalized_status = None
        if status is not None:
            normalized_status = _validate_choice(status, _LEARNING_CANDIDATE_STATUSES, "learning status")
        bounded_limit = max(1, min(int(limit), 200))
        query = """
            SELECT id, task_id, execution_id, title, plan_json, plan_digest,
                   policy_level, status, source, observed_at, reviewed_at,
                   review_note, created_at, updated_at
            FROM procedural_learning_candidates
        """
        params: tuple[Any, ...] = ()
        if normalized_status:
            query += " WHERE status = ?"
            params = (normalized_status,)
        query += " ORDER BY observed_at DESC, created_at DESC LIMIT ?"
        with self._connect() as db:
            rows = db.execute(query, (*params, bounded_limit)).fetchall()
        return [self._learning_candidate_from_row(row) for row in rows]

    async def list_learning_candidates(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List candidates for local human review; none are runnable."""
        await self.initialize()
        return await asyncio.to_thread(self._list_learning_candidates_sync, status, limit)

    def _review_learning_candidate_sync(
        self,
        candidate_id: str,
        decision: str,
        note: Optional[str],
    ) -> Optional[dict[str, Any]]:
        normalized_decision = _validate_choice(
            decision,
            {"approved", "rejected"},
            "learning review decision",
        )
        safe_note = None
        if note is not None:
            safe_note = redact_learning_text(_validate_text(note, "learning review note"))
        now = _utc_now()
        with self._connect() as db:
            candidate = self._learning_candidate_from_db(db, candidate_id)
            if not candidate:
                return None
            if candidate["status"] != "observed":
                raise TaskStateError(f"Learning candidate is already {candidate['status']}")
            db.execute(
                """
                UPDATE procedural_learning_candidates
                SET status = ?, reviewed_at = ?, review_note = ?, updated_at = ?
                WHERE id = ?
                """,
                (normalized_decision, now, safe_note, now, candidate_id),
            )
            db.commit()
            return self._learning_candidate_from_db(db, candidate_id)

    async def review_learning_candidate(
        self,
        candidate_id: str,
        *,
        decision: str,
        note: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Approve or reject a candidate for review records only, never execution."""
        await self.initialize()
        return await asyncio.to_thread(
            self._review_learning_candidate_sync,
            candidate_id,
            decision,
            note,
        )

    def _delete_learning_candidate_sync(self, candidate_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM procedural_learning_candidates WHERE id = ?",
                (candidate_id,),
            )
            db.commit()
            return bool(cursor.rowcount)

    async def delete_learning_candidate(self, candidate_id: str) -> bool:
        """Delete one local learning candidate without affecting its original task."""
        await self.initialize()
        return await asyncio.to_thread(self._delete_learning_candidate_sync, candidate_id)

    def _create_memory_sync(
        self,
        content: str,
        memory_type: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        memory_id = str(uuid4())
        created_at = _utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO memory_entries (id, memory_type, content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (memory_id, memory_type, content, json.dumps(metadata), created_at),
            )
            db.commit()

        return {
            "id": memory_id,
            "type": memory_type,
            "content": content,
            "metadata": metadata,
            "created_at": created_at,
        }

    async def create_memory(
        self,
        content: str,
        memory_type: str = "general",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Persist a memory entry."""
        await self.initialize()
        return await asyncio.to_thread(
            self._create_memory_sync,
            content,
            memory_type,
            metadata or {},
        )

    @staticmethod
    def _memory_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "type": row["memory_type"],
            "content": row["content"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "created_at": row["created_at"],
        }

    def _list_memories_sync(self, memory_type: Optional[str], limit: int) -> list[dict[str, Any]]:
        query = """
            SELECT id, memory_type, content, metadata_json, created_at
            FROM memory_entries
        """
        params: tuple[Any, ...] = ()
        if memory_type:
            query += " WHERE memory_type = ?"
            params = (memory_type,)
        query += " ORDER BY created_at DESC LIMIT ?"
        params = (*params, limit)

        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._memory_from_row(row) for row in rows]

    async def list_memories(
        self,
        memory_type: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List persisted memories."""
        await self.initialize()
        return await asyncio.to_thread(self._list_memories_sync, memory_type, limit)

    def _search_memories_sync(
        self,
        query: str,
        memory_type: Optional[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        like_query = f"%{query.lower()}%"
        sql = """
            SELECT id, memory_type, content, metadata_json, created_at
            FROM memory_entries
            WHERE (LOWER(content) LIKE ? OR LOWER(metadata_json) LIKE ?)
        """
        params: list[Any] = [like_query, like_query]
        if memory_type:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as db:
            rows = db.execute(sql, tuple(params)).fetchall()
        return [self._memory_from_row(row) for row in rows]

    async def search_memories(
        self,
        query: str,
        memory_type: Optional[str] = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Search persisted memories with lexical matching."""
        await self.initialize()
        return await asyncio.to_thread(self._search_memories_sync, query, memory_type, limit)

    def _delete_memory_sync(self, memory_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM memory_entries WHERE id = ?", (memory_id,))
            db.commit()
            return bool(cursor.rowcount)

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a persisted memory entry."""
        await self.initialize()
        return await asyncio.to_thread(self._delete_memory_sync, memory_id)

    def _get_memory_stats_sync(self) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT memory_type, COUNT(*) AS count
                FROM memory_entries
                GROUP BY memory_type
                ORDER BY memory_type ASC
                """
            ).fetchall()

        by_type = {row["memory_type"]: row["count"] for row in rows}
        return {
            "total_memories": sum(by_type.values()),
            "by_type": by_type,
        }

    async def get_memory_stats(self) -> dict[str, Any]:
        """Return persisted memory counts."""
        await self.initialize()
        return await asyncio.to_thread(self._get_memory_stats_sync)

    def _add_chat_message_sync(
        self,
        role: str,
        content: str,
        conversation_id: str,
        msg_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        message_id = str(uuid4())
        created_at = _utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO chat_messages (id, conversation_id, role, content, type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, conversation_id, role, content, msg_type, json.dumps(payload), created_at),
            )
            db.commit()

        return {
            "id": message_id,
            "role": role,
            "content": content,
            "type": msg_type,
            "timestamp": created_at,
            **payload,
        }

    async def add_chat_message(
        self,
        role: str,
        content: str,
        *,
        conversation_id: str = "default",
        msg_type: str = "text",
        **payload: Any,
    ) -> dict[str, Any]:
        """Persist a chat message and return the stored payload."""
        await self.initialize()
        return await asyncio.to_thread(
            self._add_chat_message_sync,
            role,
            content,
            conversation_id,
            msg_type,
            payload,
        )

    def _get_chat_history_sync(self, conversation_id: str, limit: Optional[int]) -> list[dict[str, Any]]:
        query = """
            SELECT id, role, content, type, payload_json, created_at
            FROM chat_messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC
        """
        params: list[Any] = [conversation_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()

        messages = []
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            messages.append(
                {
                    "id": row["id"],
                    "role": row["role"],
                    "content": row["content"],
                    "type": row["type"],
                    "timestamp": row["created_at"],
                    **payload,
                }
            )
        return messages

    async def get_chat_history(
        self,
        conversation_id: str = "default",
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Return persisted chat history in ascending timestamp order."""
        await self.initialize()
        return await asyncio.to_thread(self._get_chat_history_sync, conversation_id, limit)

    def _clear_chat_history_sync(self, conversation_id: Optional[str]) -> None:
        with self._connect() as db:
            if conversation_id:
                db.execute(
                    "DELETE FROM chat_messages WHERE conversation_id = ?",
                    (conversation_id,),
                )
            else:
                db.execute("DELETE FROM chat_messages")
            db.commit()

    async def clear_chat_history(self, conversation_id: Optional[str] = None) -> None:
        """Clear persisted history for one conversation or all conversations."""
        await self.initialize()
        await asyncio.to_thread(self._clear_chat_history_sync, conversation_id)


_state_store: Optional[StateStore] = None


def get_state_store() -> StateStore:
    """Return the singleton runtime state store."""
    global _state_store
    if _state_store is None:
        _state_store = StateStore()
    return _state_store
