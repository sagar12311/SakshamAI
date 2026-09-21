"""
Saksham AI - Memory Agent
Manages all memory types with vector search capabilities.
The memory of Saksham - remembers everything important.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from .base_agent import BaseAgent
from core.cognition_bus import CognitionMessage, MessageType


class MemoryAgent(BaseAgent):
    """
    The Memory Agent is responsible for:
    - Storing long-term user preferences
    - Managing project contexts
    - Tracking ongoing tasks
    - Remembering past decisions
    - Semantic search across all memories
    """
    
    def __init__(self):
        super().__init__("memory")
        self._vector_store = None
        self._system_prompt = """You are Saksham's Memory Agent - the memory of a Jarvis-class AI system.

Your role is to:
1. Store and retrieve user preferences
2. Maintain project contexts
3. Track task history
4. Enable semantic search across memories
5. Provide relevant context to other agents

You remember:
- User identity and preferences
- Project details (Nefex, FrostBI, IceCubes, Saksham, etc.)
- Past decisions and their outcomes
- Conversation history
- Workflow patterns

Always provide relevant context without overwhelming."""

    async def initialize(self) -> None:
        """Initialize memory systems"""
        await super().initialize()
        
        # Enable VectorStore
        try:
            from memory.vector_store import VectorStore
            self._vector_store = VectorStore()
            await self._vector_store.initialize()
            self.log("Vector store initialized")
        except Exception as e:
            self.log(f"Vector store failed to start: {e}", level="error")
            self.log("Using lightweight JSONL memory (Fallback)", level="info")
            self._vector_store = None
        
        # Subscribe to memory events
        self.bus.subscribe(MessageType.MEMORY_STORE, self._handle_message)
        self.bus.subscribe(MessageType.MEMORY_RECALL, self._handle_message)

    async def think(self, message: CognitionMessage) -> dict:
        """
        Determine what memory operation to perform.
        """
        payload = message.payload
        
        if message.type == MessageType.MEMORY_STORE:
            return {
                "operation": "store",
                "memory_type": payload.get("type", "general"),
                "content": payload.get("content"),
                "metadata": payload.get("metadata", {}),
            }
        
        if message.type == MessageType.MEMORY_RECALL:
            return {
                "operation": "recall",
                "query": payload.get("query"),
                "memory_type": payload.get("type"),
                "limit": payload.get("limit", 5),
            }
        
        return {"operation": "unknown"}

    async def act(self, thought: dict) -> Any:
        """
        Execute the memory operation.
        """
        operation = thought.get("operation")
        
        if operation == "store":
            return await self._store_memory(thought)
        elif operation == "recall":
            return await self._recall_memory(thought)
        else:
            return {"error": f"Unknown operation: {operation}"}

    async def _store_memory(self, thought: dict) -> dict:
        """
        Store a memory in the vector database.
        """
        memory_type = thought.get("memory_type", "general")
        content = thought.get("content")
        metadata = thought.get("metadata", {})
        
        if not content:
            return {"error": "No content to store"}
        
        # Add timestamp
        metadata["timestamp"] = datetime.now().isoformat()
        metadata["type"] = memory_type
        
        if self._vector_store:
            memory_id = await self._vector_store.store(
                content=content,
                metadata=metadata,
                collection=memory_type,
            )
            self.log(f"Stored memory: {memory_id[:8]}...")
            return {"stored": True, "id": memory_id}
        else:
            # Fallback to file-based storage
            return await self._store_to_file(content, metadata, memory_type)

    async def _recall_memory(self, thought: dict) -> dict:
        """
        Recall memories relevant to a query.
        """
        query = thought.get("query")
        memory_type = thought.get("memory_type")
        limit = thought.get("limit", 5)
        
        if not query:
            return {"memories": []}
        
        if self._vector_store:
            results = await self._vector_store.search(
                query=query,
                collection=memory_type,
                limit=limit,
            )
            self.log(f"Recalled {len(results)} memories")
            return {"memories": results}
        else:
            # Fallback: Lightweight JSONL Search
            return await self._search_file_memory(query, memory_type, limit)

    async def _search_file_memory(self, query: str, memory_type: str, limit: int) -> dict:
        """Simple keyword search in JSONL files"""
        from config import get_settings
        settings = get_settings()
        
        memory_file = settings.saksham_home / "memory" / f"{memory_type}.jsonl"
        if not memory_file.exists():
            return {"memories": []}
            
        results = []
        try:
            with open(memory_file, "r") as f:
                # Read all lines (assuming reasonable size for local usage)
                lines = f.readlines()
                for line in reversed(lines): # Search newest first
                    if len(results) >= limit:
                        break
                    try:
                        entry = json.loads(line)
                        content = entry.get("content", "")
                        # Simple case-insensitive keyword match
                        if query.lower() in content.lower():
                             results.append(entry)
                    except:
                        continue
        except Exception as e:
            self.log(f"File search error: {e}", level="error")
            
        return {"memories": results}

    async def _store_to_file(self, content: str, metadata: dict, memory_type: str) -> dict:
        """
        Fallback file-based storage.
        """
        from config import get_settings
        settings = get_settings()
        
        memory_file = settings.saksham_home / "memory" / f"{memory_type}.jsonl"
        memory_file.parent.mkdir(parents=True, exist_ok=True)
        
        entry = {
            "content": content,
            "metadata": metadata,
        }
        
        with open(memory_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
        
        return {"stored": True, "file": str(memory_file)}

    # Convenience methods for specific memory types
    
    async def remember_user(self, key: str, value: Any) -> dict:
        """Store a user preference"""
        return await self._store_memory({
            "memory_type": "user",
            "content": json.dumps({key: value}),
            "metadata": {"key": key},
        })

    async def remember_project(self, project_name: str, context: dict) -> dict:
        """Store project context"""
        return await self._store_memory({
            "memory_type": "project",
            "content": json.dumps(context),
            "metadata": {"project": project_name},
        })

    async def remember_task(self, task: dict) -> dict:
        """Store task information"""
        return await self._store_memory({
            "memory_type": "task",
            "content": json.dumps(task),
            "metadata": {"status": task.get("status", "pending")},
        })

    async def get_user_context(self) -> dict:
        """Get overall user context for other agents"""
        result = await self._recall_memory({
            "query": "user preferences identity",
            "memory_type": "user",
            "limit": 10,
        })
        return result.get("memories", [])

    async def get_project_context(self, project_name: str) -> dict:
        """Get context for a specific project"""
        result = await self._recall_memory({
            "query": project_name,
            "memory_type": "project",
            "limit": 5,
        })
        return result.get("memories", [])

    async def save_skill(self, name: str, steps: list[dict]) -> dict:
        """Store a learned skill (training mode)"""
        return await self._store_memory({
            "memory_type": "skill",
            "content": json.dumps(steps),
            "metadata": {"skill_name": name, "step_count": len(steps)},
        })

    async def get_skill(self, name: str) -> list[dict]:
        """Retrieve a learned skill"""
        # Exact match logic using metadata filter would be better, but standard search works for now
        result = await self._recall_memory({
            "query": name,
            "memory_type": "skill",
            "limit": 1,
        })
        memories = result.get("memories", [])
        if memories:
            try:
                # Return the steps from the best match
                return json.loads(memories[0]["content"])
            except:
                pass
        return []
