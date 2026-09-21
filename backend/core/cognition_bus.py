"""
Saksham AI - Cognition Bus
Central nervous system for inter-agent communication.
Event-driven pub/sub architecture for agent orchestration.
"""

import asyncio
import queue
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from loguru import logger


class MessageType(Enum):
    """Types of messages that flow through the cognition bus"""
    
    # User interactions
    USER_INPUT = "user_input"
    USER_VOICE = "user_voice"
    USER_TURN_COMMITTED = "user_turn_committed"
    
    # Agent communications
    AGENT_REQUEST = "agent_request"
    AGENT_RESPONSE = "agent_response"
    AGENT_AUDIO_CHUNK = "agent_audio_chunk"
    
    # Planning
    PLAN_CREATED = "plan_created"
    PLAN_STEP = "plan_step"
    PLAN_COMPLETE = "plan_complete"
    
    # Execution
    TASK_START = "task_start"
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETE = "task_complete"
    TASK_FAILED = "task_failed"
    TASK_APPROVAL_REQUESTED = "task_approval_requested"
    TASK_APPROVAL_DECISION = "task_approval_decision"
    TASK_CANCELLATION_REQUESTED = "task_cancellation_requested"
    ADMIN_ACTIVATION_REQUESTED = "admin_activation_requested"
    ADMIN_CHALLENGE_ISSUED = "admin_challenge_issued"
    ADMIN_ACTIVATION_FAILED = "admin_activation_failed"
    ADMIN_ACTIVATION_VERIFIED = "admin_activation_verified"
    ADMIN_ACTIVATION_RESULT = "admin_activation_result"
    
    # Observations
    OBSERVATION = "observation"
    ERROR_DETECTED = "error_detected"
    
    # Memory
    MEMORY_STORE = "memory_store"
    MEMORY_RECALL = "memory_recall"
    
    # NOVA Memory (Layer-specific)
    MEMORY_STORE_EPISODIC = "memory_store_episodic"
    MEMORY_STORE_SEMANTIC = "memory_store_semantic"
    MEMORY_STORE_PROCEDURAL = "memory_store_procedural"
    
    # System
    MODE_CHANGE = "mode_change"
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_GRANTED = "permission_granted"
    PERMISSION_DENIED = "permission_denied"
    
    # Mac Control
    MAC_COMMAND = "mac_command"
    MAC_RESULT = "mac_result"


@dataclass
class CognitionMessage:
    """Message structure for the cognition bus"""
    
    type: MessageType
    payload: dict[str, Any]
    source: str  # Which agent/component sent this
    target: Optional[str] = None  # Specific target, or None for broadcast
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)
    priority: int = 5  # 1 (highest) to 10 (lowest)
    requires_response: bool = False
    
    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "payload": self.payload,
            "source": self.source,
            "target": self.target,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp.isoformat(),
            "priority": self.priority,
        }


class CognitionBus:
    """
    Central communication hub for all Saksham agents.
    Implements async pub/sub with priority queuing.
    """
    
    def __init__(self):
        self._subscribers: dict[MessageType, list[Callable]] = {}
        self._agent_handlers: dict[str, Callable] = {}
        self._message_queue: Optional[asyncio.PriorityQueue] = None
        self._running = False
        self._processor_task: Optional[asyncio.Task] = None
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._sequence_counter = 0  # For priority queue ordering
        
    @property
    def is_running(self) -> bool:
        return self._running
    
    @property
    def active_agents(self) -> list[str]:
        return list(self._agent_handlers.keys())
    
    async def start(self) -> None:
        """Start the cognition bus message processor"""
        if self._running:
            return
            
        # Initialize queue here to ensure it uses the current event loop
        self._message_queue = asyncio.PriorityQueue()
        self._running = True
        self._processor_task = asyncio.create_task(self._process_messages())
        logger.info("🧠 Cognition Bus started")
    
    async def stop(self) -> None:
        """Stop the cognition bus"""
        self._running = False
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        logger.info("🧠 Cognition Bus stopped")
    
    def subscribe(self, message_type: MessageType, handler: Callable) -> None:
        """Subscribe to a specific message type"""
        if message_type not in self._subscribers:
            self._subscribers[message_type] = []
        self._subscribers[message_type].append(handler)
        logger.debug(f"Subscribed handler to {message_type.value}")
    
    def register_agent(self, agent_name: str, handler: Callable) -> None:
        """Register an agent to receive direct messages"""
        self._agent_handlers[agent_name] = handler
        logger.info(f"🤖 Agent registered: {agent_name}")
    
    def unregister_agent(self, agent_name: str) -> None:
        """Unregister an agent"""
        if agent_name in self._agent_handlers:
            del self._agent_handlers[agent_name]
            logger.info(f"🤖 Agent unregistered: {agent_name}")
    
    async def publish(self, message: CognitionMessage) -> Optional[Any]:
        """
        Publish a message to the cognition bus.
        If requires_response is True, waits for and returns the response.
        """
        logger.info(f"📤 Publishing message: type={message.type.value}, target={message.target}, requires_response={message.requires_response}")
        # Register the response channel before exposing the message to workers.
        # A fast local agent can otherwise respond between queue.put() and future
        # creation, leaving the caller waiting until its timeout.
        future: Optional[asyncio.Future] = None
        if message.requires_response:
            future = asyncio.get_event_loop().create_future()
            self._pending_responses[message.correlation_id] = future

        # Add to priority queue (lower number = higher priority)
        # Use sequence counter to avoid comparing message objects
        if self._message_queue is None:
            # Should normally be initialized in start(), but create if missing to be safe
            self._message_queue = asyncio.PriorityQueue()
            
        self._sequence_counter += 1
        await self._message_queue.put((message.priority, self._sequence_counter, message))
        logger.debug(f"✅ Message added to queue, sequence={self._sequence_counter}")
        
        if future is not None:
            try:
                # Wait for response with timeout
                return await asyncio.wait_for(future, timeout=180.0)
            except asyncio.TimeoutError:
                logger.warning(f"Response timeout for {message.correlation_id}")
                return None
            finally:
                self._pending_responses.pop(message.correlation_id, None)
        
        return None
    
    async def respond(self, correlation_id: str, response: Any) -> None:
        """Respond to a message that requires a response"""
        if correlation_id in self._pending_responses:
            future = self._pending_responses[correlation_id]
            if not future.done():
                future.set_result(response)
    
    async def _process_messages(self) -> None:
        """Main message processing loop"""
        logger.info("🔄 Message processing loop started")
        iteration = 0
        while self._running:
            if self._message_queue is None:
                await asyncio.sleep(0.1)
                continue
                
            # iteration += 1
            # qsize = self._message_queue.qsize()
            # if qsize > 0 or iteration % 50 == 0:
            #     logger.debug(f"🔁 Loop iteration {iteration}, qsize={qsize}")
            try:
                # Get next message from queue using blocking async get
                # This properly yields control to other coroutines while waiting
                try:
                    # Use a short timeout to prevent blocking forever
                    # The timeout allows the loop to check self._running periodically
                    result = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
                    
                    _, _, message = result
                    # Only log details for non-heartbeat messages to reduce spam
                    logger.debug(f"📨 Processing message: type={message.type.value}, target={message.target}")
                except asyncio.TimeoutError:
                    # Timeout is normal - no message available, continue to next iteration
                    continue
                except asyncio.CancelledError:
                    logger.info("Message loop cancelled, shutting down...")
                    raise  # Re-raise to properly handle task cancellation
                except Exception as e:
                    logger.error(f"❌ Error getting/unpacking message: {type(e).__name__}: {e}", exc_info=True)
                    continue
                
                # Route message
                logger.info(f"🚦 Routing message to {message.target}...")
                await self._route_message(message)
                logger.info(f"✅ Message routed successfully")
                
            except asyncio.CancelledError:
                raise
            except BaseException as e:
                logger.error(f"❌❌ CRITICAL: BaseException in message loop: {type(e).__name__}: {e}", exc_info=True)
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
    
    async def _route_message(self, message: CognitionMessage) -> None:
        """Route message to appropriate handlers"""
        
        logger.debug(f"Routing message: type={message.type.value}, target={message.target}, correlation_id={message.correlation_id}")
        
        # If message has specific target, route directly
        if message.target and message.target in self._agent_handlers:
            handler = self._agent_handlers[message.target]
            logger.debug(f"Calling handler for agent: {message.target}")
            # Run handler as a task to prevent blocking the message loop
            # This is critical to prevent deadlocks when agents wait for responses
            asyncio.create_task(self._safe_call(handler, message))
            return
        
        # Otherwise, broadcast to all subscribers of this message type
        handlers = self._subscribers.get(message.type, [])
        
        if not handlers:
            logger.debug(f"No handlers for message type: {message.type.value}")
            return
        
        # Execute all handlers concurrently
        tasks = []
        for handler in handlers:
            tasks.append(asyncio.create_task(self._safe_call(handler, message)))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _safe_call(self, handler: Callable, message: CognitionMessage) -> None:
        """Safely call a handler with error handling"""
        try:
            result = await handler(message)
            
            # If the message requires a response, send the result back
            if message.requires_response and result is not None:
                await self.respond(message.correlation_id, result)
        except Exception as e:
            logger.error(f"Handler error: {e}")
    
    # Convenience methods for common operations
    
    async def process_message(self, text: str, conversation_id: str = "default") -> dict:
        """Process a text message from the user"""
        from core.nlu import get_nlu_engine

        nlu = get_nlu_engine().analyze(text)
        message = CognitionMessage(
            type=MessageType.USER_INPUT,
            payload={
                "text": text,
                "conversation_id": conversation_id,
                "source": "text",
                "nlu": nlu.to_dict(),
            },
            source="user",
            target="planner",
            requires_response=True,
        )
        
        response = await self.publish(message)
        return response or {"error": "No response received"}
    
    async def process_voice(self, audio_data: bytes) -> dict:
        """Process voice audio from the user"""
        message = CognitionMessage(
            type=MessageType.USER_VOICE,
            payload={"audio": audio_data},
            source="user",
            target="voice_processor",
            requires_response=True,
        )
        
        response = await self.publish(message)
        return response or {"error": "No response received"}
    
    async def process_voice_stream(self, audio_chunk: bytes) -> dict:
        """Process streaming voice audio"""
        # For now, accumulate and process
        # In production, this would use a streaming transcription service
        return await self.process_voice(audio_chunk)
    
    async def request_mac_action(
        self,
        action: str,
        parameters: dict,
        source_agent: str
    ) -> dict:
        """Request a Mac system action"""
        message = CognitionMessage(
            type=MessageType.MAC_COMMAND,
            payload={
                "action": action,
                "parameters": parameters,
            },
            source=source_agent,
            target="executor",
            requires_response=True,
            priority=3,  # High priority for system actions
        )
        
        response = await self.publish(message)
        return response or {"error": "Mac action failed"}


# Global cognition bus instance
_cognition_bus: Optional[CognitionBus] = None


def get_cognition_bus() -> CognitionBus:
    """Get or create the global cognition bus instance"""
    global _cognition_bus
    if _cognition_bus is None:
        _cognition_bus = CognitionBus()
    return _cognition_bus
