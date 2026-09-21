"""
Saksham AI Agents Module
"""

from .base_agent import BaseAgent, AgentState, AgentContext
from .planner_agent import PlannerAgent
from .executor_agent import ExecutorAgent
from .observer_agent import ObserverAgent
from .memory_agent import MemoryAgent
from .strategist_agent import StrategistAgent
from .guardian_agent import GuardianAgent

__all__ = [
    "BaseAgent",
    "AgentState", 
    "AgentContext",
    "PlannerAgent",
    "ExecutorAgent",
    "ObserverAgent",
    "MemoryAgent",
    "StrategistAgent",
    "GuardianAgent",
]


async def initialize_all_agents():
    """Initialize all agents and return them"""
    agents = [
        PlannerAgent(),
        ExecutorAgent(),
        ObserverAgent(),
        MemoryAgent(),
        StrategistAgent(),
        GuardianAgent(),
    ]
    
    for agent in agents:
        await agent.initialize()
    
    return agents
