"""
Saksham AI - Base Agent
Abstract base class for all cognitive agents.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from loguru import logger

from core.cognition_bus import CognitionBus, CognitionMessage, MessageType, get_cognition_bus
from core.llm_client import LLMClient, get_llm_client


class AgentState(Enum):
    """Agent operational states"""
    IDLE = "idle"
    PROCESSING = "processing"
    WAITING = "waiting"
    ERROR = "error"


@dataclass
class AgentContext:
    """Context passed between agents"""
    user_input: str
    intent: Optional[str] = None
    plan: Optional[list[dict]] = None
    current_step: int = 0
    memory_context: Optional[dict] = None
    mode: str = "assist"
    conversation_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)


class BaseAgent(ABC):
    """
    Abstract base class for Saksham AI agents.
    All agents must implement think() and act() methods.
    """
    
    def __init__(self, name: str):
        self.name = name
        self.state = AgentState.IDLE
        self._bus: Optional[CognitionBus] = None
        self._llm: Optional[LLMClient] = None
        self._system_prompt: str = ""
        
    @property
    def bus(self) -> CognitionBus:
        if self._bus is None:
            self._bus = get_cognition_bus()
        return self._bus
    
    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_llm_client()
        return self._llm
    
    async def initialize(self) -> None:
        """Initialize the agent and register with cognition bus"""
        self.bus.register_agent(self.name, self._handle_message)
        logger.info(f"🤖 {self.name} initialized")
    
    async def shutdown(self) -> None:
        """Cleanup and unregister from cognition bus"""
        self.bus.unregister_agent(self.name)
        logger.info(f"🤖 {self.name} shutdown")
    
    async def _handle_message(self, message: CognitionMessage) -> None:
        """
        Handle incoming messages from the cognition bus.
        Routes to appropriate handler based on message type.
        """
        self.state = AgentState.PROCESSING
        
        try:
            # Think about the message
            thought = await self.think(message)
            
            # Act on the thought
            result = await self.act(thought)
            
            # If message requires response, send it
            if message.requires_response:
                await self.bus.respond(message.correlation_id, result)
            
            self.state = AgentState.IDLE
            
        except Exception as e:
            self.state = AgentState.ERROR
            logger.error(f"Agent {self.name} error: {e}")
            
            if message.requires_response:
                await self.bus.respond(
                    message.correlation_id,
                    {"error": str(e), "agent": self.name}
                )
    
    @abstractmethod
    async def think(self, message: CognitionMessage) -> dict:
        """
        Process the message and decide what to do.
        Returns a thought/decision dict.
        """
        pass
    
    @abstractmethod
    async def act(self, thought: dict) -> Any:
        """
        Execute the action based on the thought.
        Returns the result of the action.
        """
        pass
    
    async def send_to_agent(
        self,
        target: str,
        message_type: MessageType,
        payload: dict,
        requires_response: bool = False,
    ) -> Optional[Any]:
        """Send a message to another agent"""
        message = CognitionMessage(
            type=message_type,
            payload=payload,
            source=self.name,
            target=target,
            requires_response=requires_response,
        )
        return await self.bus.publish(message)
    
    async def broadcast(
        self,
        message_type: MessageType,
        payload: dict,
    ) -> None:
        """Broadcast a message to all agents"""
        message = CognitionMessage(
            type=message_type,
            payload=payload,
            source=self.name,
        )
        await self.bus.publish(message)
    
    async def ask_llm(
        self,
        prompt: str,
        context: Optional[dict] = None,
        tools: Optional[list[dict]] = None,
        system_prompt: Optional[str] = None,
        **kwargs,
    ) -> dict:
        """
        Ask the LLM for a response.
        Uses the agent's system prompt by default, but can be overridden.
        Accepts additional args like 'provider' for validation.
        """
        messages = []
        
        if context:
            messages.append({
                "role": "user",
                "content": f"Context: {context}"
            })
        
        messages.append({"role": "user", "content": prompt})
        
        return await self.llm.complete(
            messages=messages,
            system_prompt=system_prompt or self._system_prompt,
            tools=tools,
            **kwargs,
        )
    
    def log(self, message: str, level: str = "info") -> None:
        """Log a message with agent context"""
        log_fn = getattr(logger, level, logger.info)
        log_fn(f"[{self.name}] {message}")
