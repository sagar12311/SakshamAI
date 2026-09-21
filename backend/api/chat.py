"""
Saksham AI - Chat API
Conversational endpoints for the frontend.
"""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.cognition_bus import get_cognition_bus
from core.chat_store import get_chat_store
from core.integration_lab import get_integration_lab


router = APIRouter()


class ChatMessage(BaseModel):
    message: str
    context: Optional[str] = None
    conversation_id: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    conversation_id: str
    type: str
    speak: bool = True
    execution_id: Optional[str] = None
    proposal: Optional[dict[str, Any]] = None


@router.post("/message", response_model=ChatResponse)
async def send_message(chat: ChatMessage):
    """
    Send a text message to Saksham and get a response.
    """
    bus = get_cognition_bus()
    store = get_chat_store()
    
    try:
        # Store user message (Clean history)
        conversation_id = chat.conversation_id or "default"
        await store.add_message("user", chat.message, conversation_id=conversation_id)

        # Integration research is intentionally chat-only. It never enters the
        # voice planner or executes changes while the user is speaking.
        proposal = await get_integration_lab().propose_from_chat(chat.message)
        if proposal:
            response_text = (
                f"I created the {proposal['title']} proposal. Review the scope and approve or "
                "request changes in the Integration Lab card below. Nothing has been installed, "
                "changed, or executed."
            )
            await store.add_message(
                "assistant",
                response_text,
                conversation_id=conversation_id,
                msg_type="integration_proposal",
                speak=False,
                proposal_id=proposal["id"],
                proposal=proposal,
            )
            return ChatResponse(
                response=response_text,
                conversation_id=conversation_id,
                type="integration_proposal",
                speak=False,
                proposal=proposal,
            )
        
        # Prepare full context for the Agent (Message + File Content)
        agent_input = chat.message
        if chat.context:
            agent_input = f"{chat.message}\n\n--- FILE CONTEXT ---\n{chat.context}"
        
        result = await bus.process_message(agent_input, conversation_id=conversation_id)
        
        # Do NOT store agent response here.
        # It is handled by the global AGENT_RESPONSE listener in main.py to prevent duplicates.
        
        return ChatResponse(
            response=result.get("text", ""),
            conversation_id=result.get("conversation_id", conversation_id),
            type=result.get("type", "response"),
            speak=result.get("speak", True),
            execution_id=result.get("execution_id"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/voice")
async def process_voice(audio_data: bytes):
    """
    Process voice input and return response.
    """
    bus = get_cognition_bus()
    
    try:
        result = await bus.process_voice(audio_data)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history/{conversation_id}")
async def get_history(conversation_id: str):
    """
    Get conversation history.
    """
    store = get_chat_store()
    return {
        "conversation_id": conversation_id,
        "messages": await store.get_history(conversation_id),
    }
