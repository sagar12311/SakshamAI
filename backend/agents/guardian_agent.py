"""
Saksham AI - Guardian Agent
Safety, permissions, and destructive action prevention.
The conscience of Saksham - keeps things safe.
"""

from typing import Any

from loguru import logger

from .base_agent import BaseAgent
from core.cognition_bus import CognitionMessage, MessageType
from core.state_store import get_state_store
from config import get_settings
from core.action_policy import ApprovalLevel, get_action_policy


class GuardianAgent(BaseAgent):
    """
    The Guardian Agent is responsible for:
    - Reviewing risky actions before execution
    - Managing permission requests
    - Preventing destructive operations
    - Maintaining audit logs
    - Enforcing safety boundaries
    """
    
    # Actions that always require confirmation
    DANGEROUS_ACTIONS = {
        "delete",
        "remove",
        "rm -rf",
        "drop",
        "truncate",
        "format",
        "destroy",
        "kill",
        "terminate",
        "shutdown",
        "restart",
    }
    
    # Paths that are protected
    PROTECTED_PATHS = {
        "/",
        "/System",
        "/Library",
        "/Applications",
        "/Users",
        "~",
    }
    
    def __init__(self):
        super().__init__("guardian")
        self._current_mode = "assist"
        self._action_policy = get_action_policy()
        self._system_prompt = """You are Saksham's Guardian Agent - the conscience of a Jarvis-class AI system.

Your role is to:
1. Review actions for safety before execution
2. Protect system files and important data
3. Request user confirmation for risky operations
4. Maintain audit logs of all actions
5. Enforce operating mode boundaries

You are the last line of defense. Be vigilant but not paranoid.
Allow safe operations to proceed. Block truly dangerous ones.
Ask for confirmation when unsure."""

    async def initialize(self) -> None:
        """Initialize guardian with settings"""
        await super().initialize()
        
        settings = get_settings()
        self._require_confirmation = settings.require_confirmation_for_destructive
        self._current_mode = await get_state_store().get_mode(settings.default_mode)
        
        # Subscribe to permission requests
        self.bus.subscribe(MessageType.PERMISSION_REQUEST, self._handle_message)
        self.bus.subscribe(MessageType.MODE_CHANGE, self._handle_mode_change)
        
        self.log("Guardian active - protecting your system")

    async def _handle_mode_change(self, message: CognitionMessage) -> None:
        """Handle mode changes"""
        new_mode = message.payload.get("mode")
        if new_mode:
            self._current_mode = new_mode
            self.log(f"Mode changed to: {new_mode}")

    async def think(self, message: CognitionMessage) -> dict:
        """
        Analyze the action for safety.
        """
        payload = message.payload
        plan = payload.get("plan", [])
        intent = payload.get("intent", "")
        
        # Analyze every structured step. The LLM does not get to decide its
        # own permission level.
        risks = []
        levels = []
        
        for i, step in enumerate(plan):
            assessment = self._action_policy.assess(step)
            levels.append(assessment.level)
            risk = {"level": assessment.risk, "reason": assessment.reason}
            if risk["level"] > 0:
                risks.append({
                    "step": i + 1,
                    "action": step.get("action"),
                    "risk_level": risk["level"],
                    "reason": risk["reason"],
                })
        
        # Determine the strongest requirement in the plan.
        max_risk = max((r["risk_level"] for r in risks), default=0)
        plan_assessment = self._action_policy.assess_plan(plan)
        
        return {
            "risks": risks,
            "max_risk": max_risk,
            "approval_level": plan_assessment.level.value,
            "requires_confirmation": plan_assessment.level is not ApprovalLevel.NONE,
            "requires_admin_activation": plan_assessment.level is ApprovalLevel.ADMIN,
            "should_block": plan_assessment.level is ApprovalLevel.BLOCKED,
            "reason": plan_assessment.reason,
            "correlation_id": message.correlation_id,
        }

    async def act(self, thought: dict) -> Any:
        """
        Respond with approval decision.
        """
        # Log the review
        await self._audit_log({
            "type": "permission_review",
            "risks": thought["risks"],
            "max_risk": thought["max_risk"],
            "mode": self._current_mode,
        })
        
        if thought["should_block"]:
            return {
                "approved": False,
                "reason": "Action blocked for safety - too risky",
                "risks": thought["risks"],
                "approval_level": thought["approval_level"],
            }

        if thought["requires_admin_activation"]:
            return {
                "approved": False,
                "requires_confirmation": True,
                "requires_admin_activation": True,
                "approval_level": thought["approval_level"],
                "reason": thought["reason"],
                "risks": thought["risks"],
            }
        
        # An operating mode can change how Saksham helps, but cannot turn a
        # policy-confirmed personal change into an implicit approval.  This is
        # deliberately independent of the legacy destructive-action setting:
        # ActionPolicy is the single authority for confirmation requirements.
        if thought["requires_confirmation"]:
            return {
                "approved": False,
                "requires_confirmation": True,
                "approval_level": thought["approval_level"],
                "reason": thought["reason"],
                "risks": thought["risks"],
            }
        
        return {
            "approved": True,
            "risks": thought["risks"],
            "approval_level": thought["approval_level"],
        }

    def _assess_risk(self, step: dict) -> dict:
        """
        Assess the risk level of an action.
        Returns risk level (0-10) and reason.
        """
        action = step.get("action", "").lower()
        target = step.get("target", "").lower()
        params = step.get("parameters", {})

        def is_url(value: str) -> bool:
            return value.startswith("http://") or value.startswith("https://")
        
        # Check for dangerous action keywords
        for dangerous in self.DANGEROUS_ACTIONS:
            if dangerous in action or dangerous in target:
                return {
                    "level": 8,
                    "reason": f"Potentially destructive action: {dangerous}",
                }
        
        # Check for protected filesystem paths only on filesystem-like actions
        if action in {"file_operation", "terminal_command"} and not is_url(target):
            for protected in self.PROTECTED_PATHS:
                if target == protected.lower() or target.startswith(protected.lower() + "/"):
                    return {
                        "level": 9,
                        "reason": f"Accessing protected path: {protected}",
                    }
        
        # Specific action assessments
        if action == "terminal_command":
            return self._assess_terminal_risk(target, params)
        
        if action == "file_operation":
            operation = params.get("operation", "")
            if operation in ("delete", "write"):
                return {"level": 5, "reason": f"File {operation} operation"}
        
        if action == "close_app":
            return {"level": 6, "reason": "Closing application requires confirmation"}
            
        if action in ["system_control", "window_control", "mac_action"]:
            # User wants verification for ALL system changes
            return {"level": 7, "reason": "System configuration change requires explicit approval"}

        if action in ["web_search", "browser_navigate"] and is_url(target):
            return {"level": 0, "reason": ""}
        
        # Default: low risk
        return {"level": 0, "reason": ""}

    def _assess_terminal_risk(self, command: str, params: dict) -> dict:
        """
        Assess risk of terminal commands.
        """
        command_lower = command.lower()
        
        # Very dangerous commands
        if any(c in command_lower for c in ["rm -rf", "sudo rm", "mkfs", "dd if="]):
            return {"level": 10, "reason": "Extremely dangerous terminal command"}
        
        # Dangerous commands
        if any(c in command_lower for c in ["sudo", "chmod", "chown", "kill -9"]):
            return {"level": 7, "reason": "Privileged terminal command"}
        
        # Moderate risk
        if any(c in command_lower for c in ["rm", "mv", "git push", "npm publish"]):
            return {"level": 5, "reason": "Terminal command with side effects"}
        
        return {"level": 2, "reason": "Terminal command"}

    async def _audit_log(self, entry: dict) -> None:
        """
        Log action to audit trail.
        """
        from datetime import datetime
        import json
        
        settings = get_settings()
        audit_file = settings.audit_log_path
        audit_file.parent.mkdir(parents=True, exist_ok=True)
        
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            **entry,
        }
        
        with open(audit_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")

    async def request_confirmation(self, plan: list[dict], reason: str) -> bool:
        """
        Request user confirmation for an action.
        Returns True if approved.
        """
        # This will be called by the UI layer
        # For now, return False to require explicit approval
        return False
