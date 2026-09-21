"""
Saksham AI - Memory API
Persistent memory management endpoints.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import get_settings
from core.state_store import get_state_store
from core.user_profile import get_user_profile


router = APIRouter()


class MemoryEntry(BaseModel):
    content: str
    type: str = "general"
    metadata: Optional[dict] = None


class PronunciationPreference(BaseModel):
    written: str
    spoken: str


def _bytes_to_mb(size_bytes: int) -> str:
    return f"{size_bytes / (1024 * 1024):.2f} MB"


def _database_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


@router.get("/")
async def list_memories(type: Optional[str] = None, limit: int = 20):
    """List memories, optionally filtered by type."""
    memories = await get_state_store().list_memories(memory_type=type, limit=limit)
    return {"memories": memories}


@router.get("/search")
async def search_memories(query: str, type: Optional[str] = None, limit: int = 5):
    """Search persisted memories."""
    results = await get_state_store().search_memories(query=query, memory_type=type, limit=limit)
    return {"query": query, "results": results}


@router.post("/")
async def store_memory(entry: MemoryEntry):
    """Store a new memory."""
    metadata = dict(entry.metadata or {})
    metadata["timestamp"] = datetime.now(timezone.utc).isoformat()
    metadata["type"] = entry.type

    stored = await get_state_store().create_memory(
        content=entry.content,
        memory_type=entry.type,
        metadata=metadata,
    )
    return {"stored": True, "id": stored["id"]}


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str):
    """Delete a memory."""
    deleted = await get_state_store().delete_memory(memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")
    return {"deleted": True, "id": memory_id}


@router.get("/stats")
async def memory_stats():
    """Get memory statistics."""
    settings = get_settings()
    stats = await get_state_store().get_memory_stats()
    stats["storage_size"] = _bytes_to_mb(_database_size(settings.database_path))
    return stats


@router.get("/profile")
async def get_profile():
    """Return local user preferences that directly affect Saksham's behavior."""
    return await get_user_profile().get_context()


@router.put("/profile/pronunciations")
async def save_pronunciation(preference: PronunciationPreference):
    """Persist a written-to-spoken pronunciation preference for all future TTS."""
    update = await get_user_profile().set_pronunciation(
        preference.written,
        preference.spoken,
    )
    if not update:
        raise HTTPException(status_code=400, detail="Both written and spoken values are required")
    return {
        "saved": True,
        "written": update.written,
        "spoken": update.spoken,
    }
