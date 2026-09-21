"""
Saksham AI - Observer Agent
Watches execution results and detects errors/deviations.
The eyes of Saksham - monitors everything.
"""

from typing import Any

from loguru import logger

from .base_agent import BaseAgent
from core.cognition_bus import CognitionMessage, MessageType


class ObserverAgent(BaseAgent):
    """
    The Observer Agent is responsible for:
    - Monitoring execution progress
    - Detecting errors and deviations
    - Verifying task completion
    - Reporting anomalies to other agents
    - Learning from execution patterns
    """
    
    def __init__(self):
        super().__init__("observer")
        self._active_executions: dict = {}
        self._system_prompt = """You are Saksham's Observer Agent - the eyes of a Jarvis-class AI system.

Your role is to:
1. Monitor all execution progress
2. Detect errors and unexpected outcomes
3. Verify tasks completed successfully
4. Report issues to Planner for replanning
5. Learn patterns for future improvement

You observe silently until something goes wrong.
When it does, you alert immediately."""

    async def initialize(self) -> None:
        """Initialize observer and subscribe to events"""
        await super().initialize()
        
        # Subscribe to execution events
        self.bus.subscribe(MessageType.TASK_START, self._on_task_start)
        self.bus.subscribe(MessageType.TASK_PROGRESS, self._on_task_progress)
        self.bus.subscribe(MessageType.TASK_COMPLETE, self._on_task_complete)
        self.bus.subscribe(MessageType.TASK_FAILED, self._on_task_failed)
        self.bus.subscribe(MessageType.ERROR_DETECTED, self._on_error)
        
        self.log("Observer active - watching all executions")

    async def _on_task_start(self, message: CognitionMessage) -> None:
        """Track new execution"""
        execution_id = message.payload.get("execution_id")
        if execution_id:
            self._active_executions[execution_id] = {
                "started_at": message.timestamp,
                "steps": [],
                "status": "running",
            }

    async def _on_task_progress(self, message: CognitionMessage) -> None:
        """Track execution progress"""
        execution_id = message.payload.get("execution_id")
        if execution_id and execution_id in self._active_executions:
            self._active_executions[execution_id]["steps"].append({
                "step": message.payload.get("step"),
                "description": message.payload.get("description"),
                "timestamp": message.timestamp,
            })

    async def _on_task_complete(self, message: CognitionMessage) -> None:
        """Handle task completion"""
        execution_id = message.payload.get("execution_id")
        if execution_id and execution_id in self._active_executions:
            self._active_executions[execution_id]["status"] = "completed"
            self.log(f"Execution {execution_id[:8]}... completed successfully")

    async def _on_task_failed(self, message: CognitionMessage) -> None:
        """Handle task failure"""
        execution_id = message.payload.get("execution_id")
        if execution_id and execution_id in self._active_executions:
            self._active_executions[execution_id]["status"] = "failed"
            self.log(f"Execution {execution_id[:8]}... failed", level="warning")
            
            # Notify strategist for analysis
            await self.send_to_agent(
                target="strategist",
                message_type=MessageType.OBSERVATION,
                payload={
                    "type": "task_failure",
                    "execution_id": execution_id,
                    "details": message.payload,
                },
            )

    async def _on_error(self, message: CognitionMessage) -> None:
        """Handle detected errors"""
        self.log(f"Error detected: {message.payload}", level="error")
        
        # Could trigger replanning here
        await self.broadcast(
            MessageType.OBSERVATION,
            {
                "type": "error",
                "details": message.payload,
            }
        )

    async def think(self, message: CognitionMessage) -> dict:
        """Analyze observation"""
        return {"action": "observe", "payload": message.payload}

    async def act(self, thought: dict) -> Any:
        """Report observation"""
        return {"observed": True}
