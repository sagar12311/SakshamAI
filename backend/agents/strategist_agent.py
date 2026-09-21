"""
Saksham AI - Strategist Agent
Suggests optimizations and thinks long-term.
The advisor of Saksham - challenges bad decisions.
"""

from typing import Any

from loguru import logger

from .base_agent import BaseAgent
from core.cognition_bus import CognitionMessage, MessageType


class StrategistAgent(BaseAgent):
    """
    The Strategist Agent is responsible for:
    - Analyzing execution patterns
    - Suggesting optimizations
    - Challenging inefficient decisions
    - Long-term goal tracking
    - Workflow improvement
    """
    
    def __init__(self):
        super().__init__("strategist")
        self._system_prompt = """You are Saksham's Strategist Agent - the advisor of a Jarvis-class AI system.

Your role is to:
1. Analyze patterns in user behavior
2. Suggest workflow optimizations
3. Challenge inefficient approaches  
4. Think strategically about long-term goals
5. Proactively offer improvements

You are sharp and slightly sarcastic. You don't just agree - you push back when things could be better.
But you're never annoying about it. You're a trusted advisor, not a nag.

Examples:
- "You've opened Chrome 47 times today. Maybe we set up that tab group I suggested?"
- "That's the third time you've manually compiled. Want an auto-build watcher?"
- "I noticed you always check Slack after standup. Should I remind you automatically?"

Be observant. Be helpful. Be honest."""

    async def initialize(self) -> None:
        """Initialize strategist"""
        await super().initialize()
        
        # Subscribe to observations for pattern analysis
        self.bus.subscribe(MessageType.OBSERVATION, self._analyze_observation)
        self.bus.subscribe(MessageType.TASK_COMPLETE, self._analyze_completion)
        
        self.log("Strategist active - analyzing patterns")

    async def _analyze_observation(self, message: CognitionMessage) -> None:
        """Analyze observations for patterns"""
        observation = message.payload
        observation_type = observation.get("type")
        
        if observation_type == "task_failure":
            await self._handle_failure_pattern(observation)
        elif observation_type == "repetitive_action":
            await self._suggest_automation(observation)

    async def _analyze_completion(self, message: CognitionMessage) -> None:
        """Analyze completed tasks for optimization opportunities"""
        # Store for pattern analysis
        results = message.payload.get("results", [])
        
        # Check for slow steps that could be optimized
        # This would be enhanced with actual timing data

    async def _handle_failure_pattern(self, observation: dict) -> None:
        """Analyze failure and suggest improvements"""
        # Ask LLM to analyze the failure
        details = observation.get("details", {})
        
        response = await self.ask_llm(
            f"Analyze this task failure and suggest how to prevent it:\n{details}"
        )
        
        suggestion = response.get("content", "")
        
        if suggestion:
            await self.broadcast(
                MessageType.OBSERVATION,
                {
                    "type": "suggestion",
                    "source": "failure_analysis",
                    "content": suggestion,
                }
            )

    async def _suggest_automation(self, observation: dict) -> None:
        """Suggest automation for repetitive actions"""
        action = observation.get("action")
        count = observation.get("count", 0)
        
        if count >= 3:
            await self.broadcast(
                MessageType.OBSERVATION,
                {
                    "type": "suggestion",
                    "source": "repetition_detection",
                    "content": f"You've done '{action}' {count} times. Want me to automate this?",
                }
            )

    async def think(self, message: CognitionMessage) -> dict:
        """Strategic analysis"""
        return {"action": "analyze", "payload": message.payload}

    async def act(self, thought: dict) -> Any:
        """Provide strategic insight"""
        return {"analyzed": True}

    async def get_suggestions(self, context: str) -> list[str]:
        """Get strategic suggestions based on context"""
        response = await self.ask_llm(
            f"Based on this context, what strategic suggestions do you have?\n{context}"
        )
        
        content = response.get("content", "")
        # Parse suggestions from response
        return [content] if content else []
