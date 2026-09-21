"""
Saksham AI - Chat Store
Async wrapper around the persistent runtime state store.
"""

from typing import Any, Optional

from core.state_store import get_state_store


class ChatStore:
    async def add_message(
        self,
        role: str,
        content: str,
        msg_type: str = "text",
        conversation_id: str = "default",
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await get_state_store().add_chat_message(
            role=role,
            content=content,
            conversation_id=conversation_id,
            msg_type=msg_type,
            **kwargs,
        )

    async def get_history(
        self,
        conversation_id: str = "default",
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        return await get_state_store().get_chat_history(conversation_id, limit=limit)

    async def clear(self, conversation_id: Optional[str] = None) -> None:
        await get_state_store().clear_chat_history(conversation_id)


_store = ChatStore()


def get_chat_store() -> ChatStore:
    return _store
