"""
Saksham AI — NOVA Memory System
4-layer cognitive memory architecture:
  1. Working Memory   — Ephemeral conversation/task context (in-memory deque)
  2. Episodic Memory  — Timestamped event records (ChromaDB)
  3. Semantic Memory   — Facts, preferences, knowledge (ChromaDB)
  4. Procedural Memory — Learned task procedures (ChromaDB)

All persistent layers use ChromaDB with local SentenceTransformer embeddings.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from loguru import logger


# ── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class WorkingEntry:
    """A single entry in working memory (conversation turn or task state)."""
    role: str                 # "user" | "assistant" | "system" | "task"
    content: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    def to_display(self) -> str:
        prefix = {"user": "User", "assistant": "Saksham", "system": "System", "task": "Task"}.get(self.role, self.role)
        return f"- {prefix}: {self.content}"


@dataclass
class EpisodicEntry:
    """A recorded event — something that happened."""
    event: str                # e.g. "opened_safari", "web_search_weather"
    summary: str              # Human-readable summary
    outcome: str              # "success" | "failure" | "partial"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    details: dict = field(default_factory=dict)


@dataclass
class SemanticEntry:
    """A stored fact or preference."""
    fact: str                 # e.g. "User's name is Sagar"
    category: str             # "user_preference" | "identity" | "project_knowledge" | "general_fact"
    confidence: float = 0.9   # 0.0–1.0
    source: str = "conversation"  # Where this fact came from
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ProceduralEntry:
    """A learned procedure — how to perform a task."""
    name: str                 # e.g. "check_weather"
    trigger_pattern: str      # e.g. "weather in *"
    steps: list[dict] = field(default_factory=list)   # Plan steps
    success_count: int = 0
    last_used: str = field(default_factory=lambda: datetime.now().isoformat())


# ── NOVA Memory Manager ─────────────────────────────────────────────────────

class NovaMemory:
    """
    The NOVA Memory system — 4-layer cognitive memory for Saksham AI.

    Usage:
        nova = NovaMemory(max_working=50, ttl_minutes=30)
        await nova.initialize()

        # Working memory (auto-managed)
        nova.push_working("user", "Hello Saksham")
        context = nova.get_working_context()

        # Episodic memory
        await nova.store_episode(EpisodicEntry(...))
        episodes = await nova.recall_episodes("weather search", limit=3)

        # Semantic memory
        await nova.store_fact(SemanticEntry(fact="User prefers dark mode", category="user_preference"))
        facts = await nova.recall_facts("user preferences", limit=5)

        # Procedural memory
        await nova.store_procedure(ProceduralEntry(name="check_weather", steps=[...]))
        procedures = await nova.recall_procedures("weather", limit=2)

        # Unified recall (searches all layers)
        full_context = await nova.recall(query="weather in pune")
    """

    def __init__(self, max_working: int = 50, ttl_minutes: int = 30):
        # ── Working Memory (Layer 1) ─────────────────────────────────────
        self._working: deque[WorkingEntry] = deque(maxlen=max_working)
        self._ttl_seconds = ttl_minutes * 60
        self._active_task: Optional[dict] = None

        # ── Persistent Layers (ChromaDB) ─────────────────────────────────
        self._vector_store = None
        self._initialized = False

    # ── Initialization ───────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Initialize ChromaDB-backed persistent layers."""
        try:
            from memory.vector_store import VectorStore
            self._vector_store = VectorStore()
            await self._vector_store.initialize()

            # Pre-create collections to avoid cold-start on first query
            if self._vector_store._client:
                for coll_name in ("nova_episodic", "nova_semantic", "nova_procedural"):
                    self._vector_store._get_collection(coll_name)

            self._initialized = True
            logger.info("🧠 NOVA Memory initialized (4 layers active)")

        except Exception as e:
            logger.error(f"NOVA Memory: ChromaDB init failed: {e}")
            logger.info("🧠 NOVA Memory: Working Memory active, persistent layers use JSONL fallback")
            self._initialized = True  # Working memory still works

    @property
    def is_ready(self) -> bool:
        return self._initialized

    # ══════════════════════════════════════════════════════════════════════
    # LAYER 1: WORKING MEMORY (Ephemeral)
    # ══════════════════════════════════════════════════════════════════════

    def push_working(self, role: str, content: str, **metadata) -> None:
        """Add a conversation turn or task update to working memory."""
        if not content or not content.strip():
            return
        self._working.append(WorkingEntry(
            role=role,
            content=content.strip(),
            metadata=metadata,
        ))

    def set_active_task(self, task_info: Optional[dict]) -> None:
        """Set or clear the currently active task context."""
        self._active_task = task_info

    def get_working_context(self) -> str:
        """
        Get the current working memory as a formatted string.
        Auto-prunes expired entries.
        """
        self._prune_expired()

        if not self._working and not self._active_task:
            return ""

        parts = []

        # Active task (if any)
        if self._active_task:
            parts.append(f"[Active Task] {json.dumps(self._active_task)}")

        # Recent conversation turns
        for entry in self._working:
            parts.append(entry.to_display())

        return "\n".join(parts)

    def get_working_messages(self) -> list[dict]:
        """Get working memory as structured list (for API)."""
        self._prune_expired()
        return [
            {
                "role": e.role,
                "content": e.content,
                "timestamp": datetime.fromtimestamp(e.timestamp).isoformat(),
                "metadata": e.metadata,
            }
            for e in self._working
        ]

    def clear_working(self) -> None:
        """Clear all working memory."""
        self._working.clear()
        self._active_task = None

    def _prune_expired(self) -> None:
        """Remove entries older than TTL."""
        now = time.time()
        cutoff = now - self._ttl_seconds
        # deque doesn't support random removal efficiently, so rebuild
        fresh = deque(
            (e for e in self._working if e.timestamp > cutoff),
            maxlen=self._working.maxlen,
        )
        if len(fresh) < len(self._working):
            pruned = len(self._working) - len(fresh)
            logger.debug(f"🧹 Working memory pruned {pruned} expired entries")
        self._working = fresh

    # ══════════════════════════════════════════════════════════════════════
    # LAYER 2: EPISODIC MEMORY (Persistent events)
    # ══════════════════════════════════════════════════════════════════════

    async def store_episode(self, episode: EpisodicEntry) -> Optional[str]:
        """Store an event in episodic memory."""
        content = f"[{episode.outcome.upper()}] {episode.summary}"
        metadata = {
            "event": episode.event,
            "outcome": episode.outcome,
            "timestamp": episode.timestamp,
            "layer": "episodic",
            **episode.details,
        }
        return await self._store("nova_episodic", content, metadata)

    async def recall_episodes(self, query: str, limit: int = 3) -> list[dict]:
        """Search episodic memory for similar past events."""
        return await self._search("nova_episodic", query, limit)

    # ══════════════════════════════════════════════════════════════════════
    # LAYER 3: SEMANTIC MEMORY (Facts & preferences)
    # ══════════════════════════════════════════════════════════════════════

    async def store_fact(self, fact: SemanticEntry) -> Optional[str]:
        """Store a fact or preference in semantic memory."""
        content = fact.fact
        metadata = {
            "category": fact.category,
            "confidence": fact.confidence,
            "source": fact.source,
            "timestamp": fact.timestamp,
            "layer": "semantic",
        }
        return await self._store("nova_semantic", content, metadata)

    async def recall_facts(self, query: str, limit: int = 3) -> list[dict]:
        """Search semantic memory for relevant facts."""
        return await self._search("nova_semantic", query, limit)

    @staticmethod
    def _project_collection(project_id: str) -> str:
        scope = re.sub(r"[^a-zA-Z0-9_-]", "_", str(project_id)).strip("_")
        if not scope:
            raise ValueError("Project memory scope is required")
        return f"nova_project_{scope[:96]}"

    async def store_project_fact(
        self,
        project_id: str,
        fact: SemanticEntry,
    ) -> Optional[str]:
        """Store knowledge in an isolated project collection to prevent cross-client recall."""
        metadata = {
            "project_id": project_id,
            "category": fact.category,
            "confidence": fact.confidence,
            "source": fact.source,
            "timestamp": fact.timestamp,
            "layer": "semantic",
        }
        return await self._store(self._project_collection(project_id), fact.fact, metadata)

    async def recall_project_facts(
        self,
        project_id: str,
        query: str,
        limit: int = 3,
    ) -> list[dict]:
        """Recall facts only from the selected project's isolated collection."""
        return await self._search(self._project_collection(project_id), query, limit)

    # ══════════════════════════════════════════════════════════════════════
    # LAYER 4: PROCEDURAL MEMORY (Learned workflows)
    # ══════════════════════════════════════════════════════════════════════

    async def store_procedure(self, procedure: ProceduralEntry) -> Optional[str]:
        """Store a learned procedure."""
        content = f"Procedure '{procedure.name}': {procedure.trigger_pattern}"
        metadata = {
            "name": procedure.name,
            "trigger_pattern": procedure.trigger_pattern,
            "steps": json.dumps(procedure.steps),
            "success_count": procedure.success_count,
            "last_used": procedure.last_used,
            "layer": "procedural",
        }
        return await self._store("nova_procedural", content, metadata)

    async def recall_procedures(self, query: str, limit: int = 2) -> list[dict]:
        """Search procedural memory for matching procedures."""
        return await self._search("nova_procedural", query, limit)

    async def increment_procedure_success(self, procedure_id: str) -> None:
        """Increment success count for a procedure after successful execution."""
        if not self._vector_store or not self._vector_store._client:
            return
        try:
            coll = self._vector_store._get_collection("nova_procedural")
            result = coll.get(ids=[procedure_id], include=["metadatas", "documents"])
            if result and result["metadatas"]:
                meta = result["metadatas"][0]
                meta["success_count"] = meta.get("success_count", 0) + 1
                meta["last_used"] = datetime.now().isoformat()
                coll.update(ids=[procedure_id], metadatas=[meta])
        except Exception as e:
            logger.error(f"NOVA: Failed to increment procedure success: {e}")

    # ══════════════════════════════════════════════════════════════════════
    # UNIFIED RECALL (Cross-layer search)
    # ══════════════════════════════════════════════════════════════════════

    async def recall(self, query: str) -> dict:
        """
        Search across ALL 4 memory layers and return structured context.
        Returns a dict with keys: working, episodic, semantic, procedural.
        """
        # Layer 1: Working memory (always free, synchronous)
        working_context = self.get_working_context()

        # Layers 2-4: Persistent search (async, parallel-safe)
        episodic = await self.recall_episodes(query, limit=3)
        semantic = await self.recall_facts(query, limit=3)
        procedural = await self.recall_procedures(query, limit=2)

        return {
            "working": working_context,
            "episodic": episodic,
            "semantic": semantic,
            "procedural": procedural,
        }

    def format_context(self, recall_result: dict) -> str:
        """
        Format a recall result into a human-readable context string
        suitable for injection into an LLM prompt.
        """
        sections = []

        # Working Memory
        working = recall_result.get("working", "")
        if working:
            sections.append(f"[WORKING MEMORY] Recent conversation:\n{working}")

        # Episodic Memory
        episodes = recall_result.get("episodic", [])
        if episodes:
            lines = []
            for ep in episodes:
                ts = ep.get("metadata", {}).get("timestamp", "unknown")
                content = ep.get("content", "")
                lines.append(f"  - ({ts}) {content}")
            sections.append("[EPISODIC MEMORY] Similar past events:\n" + "\n".join(lines))

        # Semantic Memory
        facts = recall_result.get("semantic", [])
        if facts:
            lines = [f"  - {f.get('content', '')}" for f in facts]
            sections.append("[SEMANTIC MEMORY] Known facts:\n" + "\n".join(lines))

        # Procedural Memory
        procedures = recall_result.get("procedural", [])
        if procedures:
            lines = []
            for proc in procedures:
                meta = proc.get("metadata", {})
                name = meta.get("name", "unknown")
                count = meta.get("success_count", 0)
                lines.append(f"  - Procedure '{name}' ({count} successful runs)")
            sections.append("[PROCEDURAL MEMORY] Known procedures:\n" + "\n".join(lines))

        if not sections:
            return ""

        return "=== CONTEXT FROM NOVA MEMORY ===\n" + "\n\n".join(sections)

    # ══════════════════════════════════════════════════════════════════════
    # STATISTICS
    # ══════════════════════════════════════════════════════════════════════

    async def get_stats(self) -> dict:
        """Get per-layer memory statistics."""
        stats = {
            "working": {
                "entries": len(self._working),
                "max_entries": self._working.maxlen,
                "active_task": self._active_task is not None,
                "ttl_minutes": self._ttl_seconds // 60,
            },
            "episodic": {"entries": 0},
            "semantic": {"entries": 0},
            "procedural": {"entries": 0},
        }

        if self._vector_store and self._vector_store._client:
            try:
                vs_stats = await self._vector_store.get_stats()
                for layer in ("nova_episodic", "nova_semantic", "nova_procedural"):
                    key = layer.replace("nova_", "")
                    stats[key]["entries"] = vs_stats.get(layer, 0)
            except Exception as e:
                logger.error(f"NOVA stats error: {e}")

        total = sum(
            stats[k].get("entries", 0)
            for k in ("working", "episodic", "semantic", "procedural")
        )
        stats["total_memories"] = total

        return stats

    # ══════════════════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ══════════════════════════════════════════════════════════════════════

    async def _store(self, collection: str, content: str, metadata: dict) -> Optional[str]:
        """Store content in a ChromaDB collection (with JSONL fallback)."""
        doc_id = str(uuid4())

        if self._vector_store:
            try:
                stored_id = await self._vector_store.store(
                    content=content,
                    metadata=metadata,
                    collection=collection,
                    doc_id=doc_id,
                )
                logger.debug(f"🧠 NOVA stored [{collection}]: {content[:60]}...")
                return stored_id
            except Exception as e:
                logger.error(f"NOVA store error [{collection}]: {e}")

        # Fallback: JSONL file
        return await self._store_jsonl(collection, content, metadata, doc_id)

    async def _search(self, collection: str, query: str, limit: int) -> list[dict]:
        """Search a ChromaDB collection (with JSONL fallback)."""
        if self._vector_store:
            try:
                results = await self._vector_store.search(
                    query=query,
                    collection=collection,
                    limit=limit,
                )
                return results
            except Exception as e:
                logger.error(f"NOVA search error [{collection}]: {e}")

        # Fallback: JSONL keyword search
        return await self._search_jsonl(collection, query, limit)

    async def _store_jsonl(self, collection: str, content: str, metadata: dict, doc_id: str) -> str:
        """Fallback: Store to JSONL file."""
        from config import get_settings
        settings = get_settings()

        filepath = settings.saksham_home / "nova_memory" / f"{collection}.jsonl"
        filepath.parent.mkdir(parents=True, exist_ok=True)

        entry = {"id": doc_id, "content": content, "metadata": metadata}
        with open(filepath, "a") as f:
            f.write(json.dumps(entry) + "\n")

        return doc_id

    async def _search_jsonl(self, collection: str, query: str, limit: int) -> list[dict]:
        """Fallback: Keyword search in JSONL file."""
        from config import get_settings
        settings = get_settings()

        filepath = settings.saksham_home / "nova_memory" / f"{collection}.jsonl"
        if not filepath.exists():
            return []

        results = []
        try:
            with open(filepath, "r") as f:
                lines = f.readlines()
                for line in reversed(lines):
                    if len(results) >= limit:
                        break
                    try:
                        entry = json.loads(line)
                        if query.lower() in entry.get("content", "").lower():
                            results.append(entry)
                    except (json.JSONDecodeError, KeyError):
                        continue
        except Exception as e:
            logger.error(f"NOVA JSONL search error: {e}")

        return results


# ── Global Singleton ─────────────────────────────────────────────────────────

_nova_memory: Optional[NovaMemory] = None


def get_nova_memory() -> NovaMemory:
    """Get the global NovaMemory singleton."""
    global _nova_memory
    if _nova_memory is None:
        from config import get_settings
        settings = get_settings()
        _nova_memory = NovaMemory(
            max_working=settings.working_memory_max_items,
            ttl_minutes=settings.working_memory_ttl_minutes,
        )
    return _nova_memory
