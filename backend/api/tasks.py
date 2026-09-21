"""
Saksham AI - Tasks API

The Command Center reads durable task snapshots from here.  Execution updates
are deliberately structured, and an admin-level approval cannot be granted by
this general-purpose API.
"""

import asyncio
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.state_store import TaskStateError, get_state_store
from core.cognition_bus import CognitionMessage, MessageType, get_cognition_bus


router = APIRouter()


class Task(BaseModel):
    """Backward-compatible task creation payload."""

    id: Optional[str] = None
    title: str = Field(min_length=1, max_length=500)
    description: Optional[str] = Field(default=None, max_length=10_000)
    status: str = "pending"
    steps: list[dict[str, Any]] = Field(default_factory=list)


class ExecutionCreate(BaseModel):
    id: Optional[str] = None
    status: Literal[
        "queued", "awaiting_approval", "running", "cancelling", "cancelled", "completed", "failed", "blocked"
    ] = "queued"
    progress: float = Field(default=0, ge=0, le=100)
    current_step_id: Optional[str] = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionUpdate(BaseModel):
    status: Optional[
        Literal[
            "queued", "awaiting_approval", "running", "cancelling", "cancelled", "completed", "failed", "blocked"
        ]
    ] = None
    progress: Optional[float] = Field(default=None, ge=0, le=100)
    current_step_id: Optional[str] = Field(default=None, max_length=256)
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = Field(default=None, max_length=4_000)


class TaskEventCreate(BaseModel):
    # Accept `type` as well as `event_type` so existing activity components do
    # not have to learn a second spelling.
    event_type: Optional[str] = Field(default=None, max_length=80)
    type: Optional[str] = Field(default=None, max_length=80)
    message: str = Field(min_length=1, max_length=4_000)
    execution_id: Optional[str] = None
    level: Literal["debug", "info", "success", "warning", "error"] = "info"
    data: dict[str, Any] = Field(default_factory=dict)


class TaskApprovalRequest(BaseModel):
    level: Literal["confirm", "admin"]
    # `reason` is the decision-card label; `summary` remains the explicit API
    # term for backend callers.  At least one is required below.
    summary: Optional[str] = Field(default=None, max_length=4_000)
    reason: Optional[str] = Field(default=None, max_length=4_000)
    risk: Optional[str] = Field(default=None, max_length=1_000)
    action: dict[str, Any] = Field(default_factory=dict)
    execution_id: Optional[str] = None
    expires_at: Optional[str] = Field(default=None, max_length=128)


class TaskApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    note: Optional[str] = Field(default=None, max_length=4_000)


class TaskCancellation(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=4_000)


def _state_error(error: TaskStateError) -> HTTPException:
    detail = str(error)
    if "requires verified live voice" in detail:
        return HTTPException(status_code=403, detail=detail)
    if "already" in detail or "Cannot transition" in detail:
        return HTTPException(status_code=409, detail=detail)
    return HTTPException(status_code=400, detail=detail)


async def _require_task_snapshot(task_id: str, *, event_limit: int = 50) -> dict[str, Any]:
    task = await get_state_store().get_task_snapshot(task_id, event_limit=event_limit)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return task


@router.get("/")
async def list_tasks(
    status: Optional[str] = None,
    limit: int = 100,
    include_details: bool = True,
):
    """List tasks; Command Center snapshots are returned by default."""
    store = get_state_store()
    if include_details:
        tasks = await store.list_task_snapshots(status=status, limit=limit)
    else:
        # Opt-out keeps the original compact response available to old clients.
        tasks = await store.list_tasks(status=status)
    return {"tasks": tasks}


@router.get("/active")
async def get_active_executions():
    """Get active Command Center task snapshots."""
    store = get_state_store()
    active = await store.get_active_tasks()
    snapshots = await asyncio.gather(
        *(store.get_task_snapshot(task["id"], event_limit=0) for task in active)
    )
    return {"active": [snapshot for snapshot in snapshots if snapshot]}


@router.get("/{task_id}/events")
async def get_task_events(task_id: str, limit: int = 100, after: Optional[int] = None):
    """Read an append-only task timeline, optionally after a known sequence."""
    await _require_task_snapshot(task_id, event_limit=0)
    events = await get_state_store().list_task_events(task_id, limit=limit, after=after)
    return {"task_id": task_id, "events": events}


@router.post("/{task_id}/events")
async def create_task_event(task_id: str, event: TaskEventCreate):
    """Append a redacted activity event for a task or execution."""
    event_type = event.event_type or event.type
    if not event_type:
        raise HTTPException(status_code=422, detail="event_type or type is required")
    try:
        created = await get_state_store().record_task_event(
            task_id,
            event_type,
            event.message,
            execution_id=event.execution_id,
            level=event.level,
            data=event.data,
        )
    except TaskStateError as error:
        raise _state_error(error) from error
    if not created:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return {"created": True, "event": created}


@router.get("/{task_id}/executions")
async def get_task_executions(task_id: str, limit: int = 50):
    """List execution attempts for a task, newest first."""
    await _require_task_snapshot(task_id, event_limit=0)
    executions = await get_state_store().list_task_executions(task_id, limit=limit)
    return {"task_id": task_id, "executions": executions}


@router.post("/{task_id}/executions")
async def create_execution(task_id: str, execution: ExecutionCreate):
    """Create a persisted execution attempt for a task."""
    try:
        created = await get_state_store().create_execution(
            task_id,
            status=execution.status,
            progress=execution.progress,
            current_step_id=execution.current_step_id,
            metadata=execution.metadata,
            execution_id=execution.id,
        )
    except TaskStateError as error:
        raise _state_error(error) from error
    if not created:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return {"created": True, "execution": created}


@router.patch("/{task_id}/executions/{execution_id}")
async def update_execution(task_id: str, execution_id: str, updates: ExecutionUpdate):
    """Report validated execution progress, a result, or a terminal outcome."""
    payload = updates.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(status_code=400, detail="At least one execution field is required")
    try:
        updated = await get_state_store().update_execution(task_id, execution_id, payload)
    except TaskStateError as error:
        raise _state_error(error) from error
    if not updated:
        raise HTTPException(status_code=404, detail=f"Execution not found: {execution_id}")
    return {"updated": True, "execution": updated}


@router.post("/{task_id}/approvals")
async def request_task_approval(task_id: str, request: TaskApprovalRequest):
    """Create a pending affirmation request for the Command Center."""
    summary = request.summary or request.reason
    if not summary:
        raise HTTPException(status_code=422, detail="summary or reason is required")
    action = dict(request.action)
    if request.risk:
        action["risk"] = request.risk
    try:
        approval = await get_state_store().request_task_approval(
            task_id,
            level=request.level,
            summary=summary,
            action=action,
            execution_id=request.execution_id,
            expires_at=request.expires_at,
        )
    except TaskStateError as error:
        raise _state_error(error) from error
    if not approval:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return {"requested": True, "approval": approval}


@router.patch("/{task_id}/approvals/{approval_id}")
async def resolve_task_approval(
    task_id: str,
    approval_id: str,
    decision: TaskApprovalDecision,
):
    """Resolve a normal affirmation. Admin elevation is intentionally blocked."""
    try:
        approval = await get_state_store().resolve_task_approval(
            task_id,
            approval_id,
            decision=decision.decision,
            note=decision.note,
        )
    except TaskStateError as error:
        raise _state_error(error) from error
    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval not found: {approval_id}")
    await get_cognition_bus().publish(CognitionMessage(
        type=MessageType.TASK_APPROVAL_DECISION,
        payload={
            "task_id": task_id,
            "approval_id": approval_id,
            "decision": decision.decision,
        },
        source="tasks_api",
        target="planner",
    ))
    return {"updated": True, "approval": approval}


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, cancellation: TaskCancellation):
    """Request cancellation and return the real persisted state afterwards."""
    try:
        outcome = await get_state_store().request_task_cancellation(
            task_id,
            reason=cancellation.reason,
        )
    except TaskStateError as error:
        raise _state_error(error) from error
    if not outcome:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    await get_cognition_bus().publish(CognitionMessage(
        type=MessageType.TASK_CANCELLATION_REQUESTED,
        payload={"task_id": task_id, "reason": cancellation.reason},
        source="tasks_api",
        target="planner",
    ))
    task = await _require_task_snapshot(task_id)
    return {"cancel": outcome, "task": task}


@router.get("/{task_id}")
async def get_task(task_id: str):
    """Get a task's full Command Center snapshot and recent event timeline."""
    return {"task": await _require_task_snapshot(task_id)}


@router.post("/")
async def create_task(task: Task):
    """Create a new task, preserving the original task payload contract."""
    created = await get_state_store().create_task(
        title=task.title,
        description=task.description,
        status=task.status,
        steps=task.steps,
        task_id=task.id,
    )
    snapshot = await get_state_store().get_task_snapshot(created["id"], event_limit=0)
    return {"created": True, "task": snapshot or created}


@router.patch("/{task_id}")
async def update_task(task_id: str, updates: dict[str, Any]):
    """Update legacy plan fields. Execution state has dedicated endpoints."""
    # The legacy endpoint intentionally remains permissive for existing local
    # clients, but it cannot mutate execution/approval records accidentally.
    allowed = {"title", "description", "status", "steps"}
    clean_updates = {key: value for key, value in updates.items() if key in allowed}
    if not clean_updates:
        raise HTTPException(status_code=400, detail="No supported task fields supplied")
    updated = await get_state_store().update_task(task_id, clean_updates)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    snapshot = await get_state_store().get_task_snapshot(task_id, event_limit=0)
    return {"updated": True, "task": snapshot or updated}


@router.delete("/{task_id}")
async def delete_task(task_id: str):
    """Delete a task and its locally persisted Command Center history."""
    deleted = await get_state_store().delete_task(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return {"deleted": True, "task_id": task_id}
