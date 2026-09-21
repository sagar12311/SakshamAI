"""
Saksham AI - Executor Agent
Performs Mac OS, application, and system actions.
The hands of Saksham - executes what the brain plans.
"""

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse
from datetime import date
from typing import Any, Optional
from uuid import uuid4

from loguru import logger

from .base_agent import BaseAgent
from core.cognition_bus import CognitionMessage, MessageType
from core.recovery_policy import get_recovery_policy
from core.action_policy import ApprovalLevel, get_action_policy
from core.admin_auth import consume_admin_capability


_CONFIRMATION_CONTEXT_VERSION = 1
_CONFIRMATION_CONTEXT_PURPOSE = "confirmed_plan"
_CONFIRMATION_CONTEXT_TTL_SECONDS = 300
_MAX_WEB_SNIPPET_CHARS = 1_200
_MAX_WEB_EVIDENCE_CHARS = 6_000
# This process-local key is intentionally not configuration.  A confirmation
# capability is useful only inside the running cognition pipeline and becomes
# invalid after a restart.
_CONFIRMATION_CONTEXT_KEY = secrets.token_bytes(32)


def _plan_digest(plan: list[dict]) -> Optional[str]:
    """Return a deterministic digest only for serializable structured plans."""
    try:
        encoded = json.dumps(
            plan,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _confirmation_signature(payload: dict) -> str:
    """Sign the immutable, non-secret fields of a confirmation capability."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hmac.new(_CONFIRMATION_CONTEXT_KEY, encoded, hashlib.sha256).hexdigest()


def issue_trusted_confirmation_context(
    plan: list[dict],
    *,
    task_id: Optional[str],
    conversation_id: Optional[str],
) -> dict:
    """Create a short-lived, plan-bound capability for a normal affirmation.

    This is deliberately an internal Python API used by ``PlannerAgent`` after
    Guardian and the user have approved a confirm-level plan.  It is not an
    API payload a client can mint: a random or edited context will fail HMAC
    verification in ``ExecutorAgent``.
    """
    digest = _plan_digest(plan)
    if not digest:
        raise ValueError("A confirmation capability requires a serializable plan")

    issued_at = int(time.time())
    signed_fields = {
        "version": _CONFIRMATION_CONTEXT_VERSION,
        "purpose": _CONFIRMATION_CONTEXT_PURPOSE,
        "task_id": str(task_id or ""),
        "conversation_id": str(conversation_id or "default"),
        "plan_digest": digest,
        "issued_at": issued_at,
        "expires_at": issued_at + _CONFIRMATION_CONTEXT_TTL_SECONDS,
        "nonce": secrets.token_urlsafe(24),
    }
    return {
        **signed_fields,
        "signature": _confirmation_signature(signed_fields),
    }


class ExecutorAgent(BaseAgent):
    """
    The Executor Agent is responsible for:
    - Executing planned actions on the Mac system
    - Controlling applications
    - Running terminal commands
    - Managing file operations
    - Interacting with browsers
    - Reporting progress and results
    """
    
    def __init__(self):
        super().__init__("executor")
        self._current_execution: Optional[str] = None
        self._mac_controller = None  # Will be injected
        self._music_controller = None
        self._youtube_controller = None
        self._recovery_policy = get_recovery_policy()
        self._action_policy = get_action_policy()
        self._consumed_confirmation_nonces: dict[str, int] = {}
        self._system_prompt = """You are Saksham's Executor Agent - the hands of a Jarvis-class AI system.

Your role is to:
1. Execute planned actions on the Mac system
2. Control applications precisely
3. Handle errors gracefully
4. Report progress in real-time
5. Adapt to unexpected situations

You can:
- Open/close Mac applications
- Run terminal commands
- Navigate and interact with browsers
- Perform file operations
- Control development tools

Always execute carefully and report results accurately."""

    async def initialize(self) -> None:
        """Initialize executor with Mac controllers"""
        await super().initialize()
        
        # Import Mac controllers lazily
        try:
            from mac.app_controller import AppController
            from mac.terminal_executor import TerminalExecutor
            from mac.file_controller import FileController
            from mac.browser_controller import BrowserController
            from mac.jxa import JXAController
            from mac.music_controller import MusicController
            from mac.youtube_controller import YouTubeController
            
            self._app_controller = AppController()
            self._terminal = TerminalExecutor()
            self._files = FileController()
            self._browser = BrowserController()
            # Commerce is deliberately wired to the same authenticated Chrome
            # profile, but it can only use the adapter's pinned-tab methods.
            from core.amazon_commerce import get_amazon_commerce_service
            get_amazon_commerce_service().set_browser(self._browser)
            self._jxa = JXAController()
            self._music_controller = MusicController()
            self._youtube_controller = YouTubeController(browser=self._browser)
            
            self.log("Mac controllers initialized (including JXA)")
        except ImportError as e:
            self.log(f"Mac controllers not available: {e}", level="warning")

    async def think(self, message: CognitionMessage) -> dict:
        """
        Analyze the execution request.
        """
        payload = message.payload
        
        if message.type == MessageType.TASK_START:
            plan = payload.get("plan", [])
            authorization = self._authorize_task_start(
                plan,
                confirmation_context=payload.get("confirmation_context"),
                source=message.source,
                task_id=payload.get("task_id"),
                conversation_id=payload.get("conversation_id", "default"),
                approval_id=payload.get("approval_id"),
                admin_capability=payload.get("admin_capability"),
                consume_confirmation=False,
            )
            return {
                "action": "execute_plan" if authorization["allowed"] else "refuse_plan",
                "plan": plan,
                "intent": payload.get("intent"),
                "conversation_id": payload.get("conversation_id", "default"),
                "nlu": payload.get("nlu", {}),
                "memory_context": payload.get("memory_context", {}),
                "task_id": payload.get("task_id"),
                "confirmation_context": payload.get("confirmation_context"),
                "admin_capability": payload.get("admin_capability"),
                "approval_id": payload.get("approval_id"),
                "_task_start_source": message.source,
                "_authorization": authorization,
                "execution_id": str(uuid4()),
            }
        
        if message.type == MessageType.MAC_COMMAND:
            return {
                "action": "execute_single",
                "command": payload.get("action"),
                "target": payload.get("target"),
                "parameters": payload.get("parameters", {}),
            }
        
        return {"action": "unknown", "payload": payload}

    async def act(self, thought: dict) -> Any:
        """
        Execute the planned actions.
        """
        action = thought.get("action")
        
        if action == "execute_plan":
            return await self._execute_plan(thought)
        elif action == "refuse_plan":
            return await self._refuse_plan(thought)
        elif action == "execute_single":
            return await self._execute_single_action(thought)
        else:
            return {"error": f"Unknown action: {action}"}

    async def _execute_plan(self, thought: dict) -> dict:
        """
        Execute a multi-step plan.
        """
        plan = thought.get("plan", [])
        execution_id = thought.get("execution_id")
        task_id = thought.get("task_id")
        results = []

        # ``think`` performs this check first for a clear refusal response,
        # but repeat it at the execution boundary.  This prevents callers of
        # this method (or a mutated thought between phases) from bypassing the
        # TaskStart policy decision.
        authorization = self._authorize_task_start(
            plan,
            confirmation_context=thought.get("confirmation_context"),
            source=thought.get("_task_start_source"),
            task_id=task_id,
            conversation_id=thought.get("conversation_id", "default"),
            approval_id=thought.get("approval_id"),
            admin_capability=thought.get("admin_capability"),
            consume_confirmation=True,
        )
        if not authorization["allowed"]:
            return await self._refuse_plan({
                **thought,
                "_authorization": authorization,
            })
        
        self._current_execution = execution_id
        if task_id:
            from core.state_store import get_state_store
            execution = await get_state_store().create_execution(
                str(task_id),
                status="running",
                current_step_id="1" if plan else None,
                metadata={"intent": thought.get("intent"), "total_steps": len(plan)},
                execution_id=execution_id,
            )
            if not execution:
                raise RuntimeError("Task record was unavailable; refusing to execute without an audit trail")
        
        for i, step in enumerate(plan):
            # Keep the approval tied to the exact plan that was confirmed.
            # A mutation after authorization invalidates the remainder before
            # any new step can reach a controller.
            if _plan_digest(plan) != authorization["plan_digest"]:
                results.append({
                    "step": i + 1,
                    "success": False,
                    "error": "Plan changed after authorization; refusing remaining steps",
                    "security_refusal": True,
                })
                break

            step_assessment = self._action_policy.assess(step)
            if step_assessment.level is ApprovalLevel.BLOCKED or (
                step_assessment.level is ApprovalLevel.ADMIN
                and not authorization.get("admin_verified")
            ):
                results.append({
                    "step": i + 1,
                    "success": False,
                    "error": "Step no longer passes the execution safety policy",
                    "security_refusal": True,
                })
                break
            if step_assessment.level is ApprovalLevel.CONFIRM and not authorization["confirmed"]:
                results.append({
                    "step": i + 1,
                    "success": False,
                    "error": "Step requires a verified user confirmation",
                    "security_refusal": True,
                })
                break

            if task_id:
                from core.state_store import get_state_store
                current_execution = await get_state_store().get_execution(execution_id)
                if current_execution and current_execution.get("cancellation_requested"):
                    results.append({
                        "step": i + 1,
                        "success": False,
                        "cancelled": True,
                        "error": "Cancellation requested before this step ran",
                    })
                    break
            # Broadcast progress
            await self.broadcast(
                MessageType.TASK_PROGRESS,
                {
                    "execution_id": execution_id,
                    "task_id": task_id,
                    "conversation_id": thought.get("conversation_id", "default"),
                    "step": i + 1,
                    "total_steps": len(plan),
                    "description": step.get("description", step.get("action")),
                }
            )
            
            # Execute the step
            try:
                result, step_success = await self._execute_step_with_recovery(
                    step,
                    conversation_id=thought.get("conversation_id", "default"),
                )
                results.append({
                    "step": i + 1,
                    "success": step_success,
                    "result": result,
                })

                # Commerce events are intentionally fixed labels with no
                # address, payment, PIN, audio, or raw page data.
                event_names = result.get("events") if isinstance(result, dict) else []
                if not isinstance(event_names, list):
                    event_names = [result.get("event")] if isinstance(result, dict) else []
                event_names = [name for name in event_names if name in {
                    "AMAZON_REHEARSAL_COMPLETED", "AMAZON_ORDER_DISPATCHED",
                    "AMAZON_ORDER_CONFIRMED", "AMAZON_ORDER_STATUS_UNKNOWN",
                }]
                if task_id and event_names:
                    from core.state_store import get_state_store
                    store = get_state_store()
                    if hasattr(store, "record_task_event"):
                        for event_name in event_names:
                            await store.record_task_event(
                                str(task_id), event_name, event_name,
                                execution_id=execution_id, level="info",
                            )

                if not step_success:
                    error = str(result.get("error", "Action did not complete"))
                    self.log(f"Step {i + 1} failed: {error}", level="error")
                    await self.broadcast(
                        MessageType.ERROR_DETECTED,
                        {
                            "execution_id": execution_id,
                            "step": i + 1,
                            "error": error,
                        },
                    )
            except Exception as e:
                self.log(f"Step {i+1} failed: {e}", level="error")
                
                # Notify observer of error
                await self.broadcast(
                    MessageType.ERROR_DETECTED,
                    {
                        "execution_id": execution_id,
                        "step": i + 1,
                        "error": str(e),
                    }
                )
                
                results.append({
                    "step": i + 1,
                    "success": False,
                    "error": str(e),
                })
                
                # Decide whether to continue or abort
                # For now, we continue
        
        # Broadcast completion
        cancelled = any(r.get("cancelled") for r in results)
        success = bool(results) and all(r.get("success") for r in results)
        await self.broadcast(
            MessageType.TASK_COMPLETE if success else MessageType.TASK_FAILED,
            {
                "execution_id": execution_id,
                "task_id": task_id,
                "intent": thought.get("intent"), # Pass intent back to Planner
                "conversation_id": thought.get("conversation_id", "default"),
                "nlu": thought.get("nlu", {}),
                "memory_context": thought.get("memory_context", {}),
                "results": results,
                "success": success,
                "cancelled": cancelled,
            }
        )
        
        self._current_execution = None
        
        return {
            "execution_id": execution_id,
            "success": success,
            "cancelled": cancelled,
            "results": results,
        }

    async def _refuse_plan(self, thought: dict) -> dict:
        """Fail closed without handing an unsafe plan to a controller."""
        authorization = thought.get("_authorization") or {}
        reason = str(authorization.get("reason") or "Plan failed execution authorization")
        execution_id = thought.get("execution_id") or str(uuid4())
        task_id = thought.get("task_id")
        self.log(f"Refused plan at execution boundary: {reason}", level="warning")

        result = {
            "step": 0,
            "success": False,
            "error": reason,
            "security_refusal": True,
        }
        await self.broadcast(
            MessageType.TASK_FAILED,
            {
                "execution_id": execution_id,
                "task_id": task_id,
                "intent": thought.get("intent"),
                "conversation_id": thought.get("conversation_id", "default"),
                "nlu": thought.get("nlu", {}),
                "memory_context": thought.get("memory_context", {}),
                "results": [result],
                "success": False,
                "cancelled": False,
                "security_refusal": True,
            },
        )
        return {
            "execution_id": execution_id,
            "success": False,
            "refused": True,
            "error": reason,
            "results": [result],
        }

    def _authorize_task_start(
        self,
        plan: Any,
        *,
        confirmation_context: Any,
        source: Optional[str],
        task_id: Optional[str],
        conversation_id: Optional[str],
        approval_id: Optional[str] = None,
        admin_capability: Any = None,
        consume_confirmation: bool,
    ) -> dict:
        """Re-evaluate every TaskStart plan before it can touch the Mac.

        Routine plans can execute normally.  Confirm-level plans need a
        short-lived capability minted by Planner after a real approval;
        admin and blocked steps fail closed until the separate live-admin flow
        exists.
        """
        if not isinstance(plan, list) or not plan:
            return {
                "allowed": False,
                "confirmed": False,
                "reason": "Execution requires a non-empty structured plan",
                "plan_digest": None,
            }

        digest = _plan_digest(plan)
        if not digest:
            return {
                "allowed": False,
                "confirmed": False,
                "reason": "Execution plan is not safely serializable",
                "plan_digest": None,
            }

        needs_confirmation = False
        needs_admin = False
        for index, step in enumerate(plan, start=1):
            if not isinstance(step, dict):
                return {
                    "allowed": False,
                    "confirmed": False,
                    "reason": f"Plan step {index} is not structured",
                    "plan_digest": digest,
                }
            assessment = self._action_policy.assess(step)
            if assessment.level is ApprovalLevel.BLOCKED:
                return {
                    "allowed": False,
                    "confirmed": False,
                    "reason": f"Plan step {index} is blocked: {assessment.reason}",
                    "plan_digest": digest,
                }
            if assessment.level is ApprovalLevel.ADMIN:
                needs_admin = True
                continue
            if assessment.level is ApprovalLevel.CONFIRM:
                needs_confirmation = True

        # A purchase is a single, immutable final-dispatch plan. A caller
        # cannot place it after running earlier mixed steps under the same
        # admin ceremony.
        if any(str(step.get("action") or "").lower() == "amazon_place_order" for step in plan):
            if len(plan) != 1:
                return {
                    "allowed": False,
                    "confirmed": False,
                    "reason": "Amazon order dispatch must be its own one-step admin plan",
                    "plan_digest": digest,
                }

        if needs_admin:
            capability_ok, capability_reason = consume_admin_capability(
                admin_capability,
                plan=plan,
                task_id=task_id,
                approval_id=approval_id,
                conversation_id=conversation_id,
                consume=consume_confirmation,
            )
            if not capability_ok:
                return {"allowed": False, "confirmed": False, "admin_verified": False,
                        "reason": capability_reason, "plan_digest": digest}
            return {"allowed": True, "confirmed": True, "admin_verified": True,
                    "reason": "Verified live admin activation", "plan_digest": digest}

        if not needs_confirmation:
            return {
                "allowed": True,
                "confirmed": False,
                "admin_verified": False,
                "reason": "Routine plan",
                "plan_digest": digest,
            }

        context_valid, context_reason = self._validate_confirmation_context(
            confirmation_context,
            plan_digest=digest,
            source=source,
            task_id=task_id,
            conversation_id=conversation_id,
            consume=consume_confirmation,
        )
        if not context_valid:
            return {
                "allowed": False,
                "confirmed": False,
                "reason": context_reason,
                "plan_digest": digest,
            }
        return {
            "allowed": True,
            "confirmed": True,
            "reason": "Verified user confirmation",
            "plan_digest": digest,
        }

    def _validate_confirmation_context(
        self,
        context: Any,
        *,
        plan_digest: str,
        source: Optional[str],
        task_id: Optional[str],
        conversation_id: Optional[str],
        consume: bool,
    ) -> tuple[bool, str]:
        """Verify a planner-minted, plan-bound confirmation capability."""
        if source != "planner":
            return False, "Confirm-level plans must originate from Planner"
        if not isinstance(context, dict):
            return False, "Plan requires a verified user confirmation"

        required_fields = (
            "version",
            "purpose",
            "task_id",
            "conversation_id",
            "plan_digest",
            "issued_at",
            "expires_at",
            "nonce",
            "signature",
        )
        if any(field not in context for field in required_fields):
            return False, "Confirmation context is incomplete"

        try:
            issued_at = int(context["issued_at"])
            expires_at = int(context["expires_at"])
            nonce = str(context["nonce"])
            signed_fields = {
                "version": int(context["version"]),
                "purpose": str(context["purpose"]),
                "task_id": str(context["task_id"]),
                "conversation_id": str(context["conversation_id"]),
                "plan_digest": str(context["plan_digest"]),
                "issued_at": issued_at,
                "expires_at": expires_at,
                "nonce": nonce,
            }
            signature = str(context["signature"])
        except (TypeError, ValueError):
            return False, "Confirmation context is malformed"

        if (
            signed_fields["version"] != _CONFIRMATION_CONTEXT_VERSION
            or signed_fields["purpose"] != _CONFIRMATION_CONTEXT_PURPOSE
        ):
            return False, "Confirmation context has an unsupported purpose"
        if signed_fields["task_id"] != str(task_id or ""):
            return False, "Confirmation context belongs to a different task"
        if signed_fields["conversation_id"] != str(conversation_id or "default"):
            return False, "Confirmation context belongs to a different conversation"
        if signed_fields["plan_digest"] != plan_digest:
            return False, "Plan changed after user confirmation"

        now = int(time.time())
        if issued_at > now + 30 or expires_at <= now:
            return False, "Confirmation context has expired"
        if expires_at - issued_at > _CONFIRMATION_CONTEXT_TTL_SECONDS:
            return False, "Confirmation context has an invalid lifetime"
        expected_signature = _confirmation_signature(signed_fields)
        if not hmac.compare_digest(signature, expected_signature):
            return False, "Confirmation context is not trusted"

        # Keep one-use nonces only through their expiry so normal execution
        # cannot be replayed by resending a captured TaskStart message.
        expired_nonces = [
            used_nonce
            for used_nonce, expiry in self._consumed_confirmation_nonces.items()
            if expiry <= now
        ]
        for used_nonce in expired_nonces:
            self._consumed_confirmation_nonces.pop(used_nonce, None)
        if nonce in self._consumed_confirmation_nonces:
            return False, "Confirmation context was already used"
        if consume:
            self._consumed_confirmation_nonces[nonce] = expires_at
        return True, "Verified user confirmation"

    async def _execute_step_with_recovery(
        self, step: dict, *, conversation_id: str = "default"
    ) -> tuple[Any, bool]:
        """Execute one step and try allowlisted equivalent fallbacks when needed."""
        try:
            primary_result = await self._execute_step(step, conversation_id=conversation_id)
        except Exception as error:
            primary_result = {
                "success": False,
                "action": step.get("action"),
                "error": str(error),
            }

        primary_success = self._result_succeeded(primary_result)
        attempts = [{
            "action": step.get("action"),
            "success": primary_success,
            "result": primary_result,
        }]
        if primary_success:
            return primary_result, True

        # Never retry an ambiguous final commerce submission. The deterministic
        # adapter marks it unknown and permanently disarms live ordering.
        if str(step.get("action") or "").lower() == "amazon_place_order":
            return primary_result, False

        alternatives = self._recovery_policy.alternatives(step, primary_result)
        for fallback in alternatives[: self._recovery_policy.max_alternatives_per_step]:
            self.log(
                f"Primary action {step.get('action')} did not complete; "
                f"trying {fallback.get('action')}"
            )
            try:
                fallback_result = await self._execute_step(fallback, conversation_id=conversation_id)
            except Exception as error:
                fallback_result = {
                    "success": False,
                    "action": fallback.get("action"),
                    "error": str(error),
                }
            fallback_success = self._result_succeeded(fallback_result)
            attempts.append({
                "action": fallback.get("action"),
                "success": fallback_success,
                "result": fallback_result,
            })
            if fallback_success:
                if isinstance(fallback_result, dict):
                    recovered_result = {
                        **fallback_result,
                        "fallback_used": True,
                        "fallback_from": step.get("action"),
                        "attempts": attempts,
                    }
                else:
                    recovered_result = {
                        "success": True,
                        "result": fallback_result,
                        "fallback_used": True,
                        "fallback_from": step.get("action"),
                        "attempts": attempts,
                    }
                return recovered_result, True

        if isinstance(primary_result, dict):
            failed_result = {**primary_result, "attempts": attempts}
            if len(attempts) > 1:
                fallback_error = attempts[-1]["result"]
                if isinstance(fallback_error, dict) and fallback_error.get("error"):
                    failed_result["error"] = (
                        f"{primary_result.get('error', 'Primary action failed')} "
                        f"YouTube fallback also failed: {fallback_error['error']}"
                    )
            return failed_result, False
        return {"success": False, "result": primary_result, "attempts": attempts}, False

    @staticmethod
    def _result_succeeded(result: Any) -> bool:
        """Require completion evidence, not merely a successfully opened destination."""
        if not isinstance(result, dict):
            return True
        if result.get("success") is False or "error" in result:
            return False
        if result.get("playback_state") == "opened_for_user":
            return False
        return True

    async def _execute_step(self, step: dict, *, conversation_id: str = "default") -> Any:
        """
        Execute a single step.
        """
        action = step.get("action", "")
        target = step.get("target", "")
        params = step.get("parameters", {})
        
        self.log(f"Executing: {action} on {target}")
        
        if action == "open_app":
            return await self._open_app(target, params)
        elif action == "close_app":
            return await self._close_app(target)
        elif action == "minimize_app":
            return await self._minimize_app(target)
        elif action == "maximize_app":
            return await self._maximize_app(target)
        elif action == "terminal_command":
            return await self._run_terminal(target, params)
        elif action == "browser_navigate":
            return await self._browser_navigate(target, params)
        elif action == "browser_interact":
            return await self._browser_interact(target, params)
        elif action == "amazon_search":
            return await self._amazon_search(target, params, conversation_id)
        elif action == "browser_scroll":
            return await self._amazon_scroll(params, conversation_id)
        elif action == "amazon_open_product":
            return await self._amazon_open_product(target, params, conversation_id)
        elif action == "amazon_open_cart":
            return await self._amazon_open_cart(conversation_id)
        elif action == "amazon_add_to_cart":
            return await self._amazon_add_to_cart(target, params, conversation_id)
        elif action == "amazon_checkout_preview":
            return await self._amazon_checkout_preview(target, params, conversation_id)
        elif action == "amazon_select_payment":
            return await self._amazon_select_payment(params, conversation_id)
        elif action == "amazon_place_order":
            return await self._amazon_place_order(params, conversation_id)
        elif action == "file_operation":
            return await self._file_operation(target, params)
        elif action == "web_search":
            return await self._web_search(target, params, step.get("description", ""))
        elif action == "speak_response":
            return {"spoken": target}
        elif action == "wait_for_user":
            return {"waiting": True}
        elif action == "system_control":
            return await self._system_control(target, params)
        elif action == "add_app_alias":
            return await self._add_app_alias(params)
        elif action == "play_music":
            return await self._play_music(target, params)
        elif action == "play_youtube_music":
            return await self._play_youtube_music(target, params)
        elif action == "music_control":
            return await self._music_control(target)
        else:
            raise ValueError(f"Unknown action type: {action}")

    async def _execute_single_action(self, thought: dict) -> dict:
        """
        Execute a single Mac command.
        """
        step = {
            "action": thought.get("command"),
            "target": thought.get("target"),
            "parameters": thought.get("parameters", {}),
        }
        assessment = self._action_policy.assess(step)
        if assessment.level is not ApprovalLevel.NONE:
            return {
                "success": False,
                "requires_approval": assessment.level.value,
                "error": assessment.reason,
            }
        
        try:
            result = await self._execute_step(step)
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Mac Control Methods
    
    async def _open_app(self, app_name: str, params: dict) -> dict:
        """Open a Mac application"""
        if self._app_controller:
            return await self._app_controller.open_app(app_name, **params)
        
        # Fallback: use AppleScript directly
        from mac.applescript_bridge import run_applescript
        script = f'tell application "{app_name}" to activate'
        await run_applescript(script)
        return {"opened": app_name}

    async def _close_app(self, app_name: str) -> dict:
        """Close/quit a Mac application"""
        if self._app_controller:
            return await self._app_controller.close_app(app_name)
        
        from mac.applescript_bridge import run_applescript
        
        # First check if app is running
        check_script = f'''
        tell application "System Events"
            set runningApps to name of every process whose name contains "{app_name}"
            if (count of runningApps) > 0 then
                return "running"
            else
                return "not running"
            end if
        end tell
        '''
        status = await run_applescript(check_script)
        
        if "not running" in status.lower():
            return {"closed": app_name, "note": "App was not running"}
        
        # Try to quit gracefully with save dialog handling
        quit_script = f'''
        tell application "{app_name}"
            try
                quit saving no
            on error
                quit
            end try
        end tell
        '''
        try:
            await run_applescript(quit_script)
            return {"closed": app_name}
        except Exception as e:
            # Force quit if graceful quit fails
            force_script = f'do shell script "killall -9 \\"{app_name}\\"" '
            try:
                await run_applescript(force_script)
                return {"closed": app_name, "method": "force"}
            except:
                return {"error": f"Could not close {app_name}: {str(e)}"}

    async def _minimize_app(self, app_name: str) -> dict:
        """Minimize an application's windows to the dock"""
        from mac.applescript_bridge import run_applescript
        
        script = f'''
        tell application "System Events"
            tell process "{app_name}"
                set frontmost to true
                keystroke "m" using command down
            end tell
        end tell
        '''
        try:
            await run_applescript(script)
            return {"minimized": app_name}
        except Exception as e:
            # Fallback: try telling the app directly
            alt_script = f'''
            tell application "{app_name}"
                try
                    set miniaturized of every window to true
                end try
            end tell
            '''
            try:
                await run_applescript(alt_script)
                return {"minimized": app_name, "method": "direct"}
            except:
                return {"error": f"Could not minimize {app_name}: {str(e)}"}

    async def _maximize_app(self, app_name: str) -> dict:
        """Maximize/zoom an application's window"""
        from mac.applescript_bridge import run_applescript
        
        # First activate the app, then toggle zoom
        script = f'''
        tell application "{app_name}"
            activate
        end tell
        delay 0.3
        tell application "System Events"
            tell process "{app_name}"
                try
                    click button 2 of window 1
                on error
                    -- Try zoom menu item
                    keystroke "f" using {{control down, command down}}
                end try
            end tell
        end tell
        '''
        try:
            await run_applescript(script)
            return {"maximized": app_name}
        except Exception as e:
            return {"error": f"Could not maximize {app_name}: {str(e)}"}

    async def _run_terminal(self, command: str, params: dict) -> dict:
        """Run a terminal command"""
        if self._terminal:
            return await self._terminal.execute(command, **params)
        
        # Fallback: use subprocess
        import asyncio
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return {
            "command": command,
            "stdout": stdout.decode() if stdout else "",
            "stderr": stderr.decode() if stderr else "",
            "return_code": process.returncode,
        }

    async def _browser_navigate(self, url: str, params: dict) -> dict:
        """Navigate browser to a URL"""
        browser = params.get("browser", "Chrome")
        
        if self._browser:
            return await self._browser.navigate(url, browser=browser)
        
        from mac.applescript_bridge import run_applescript
        script = f'''
        tell application "{browser}"
            activate
            open location "{url}"
        end tell
        '''
        await run_applescript(script)
        return {"navigated": url, "browser": browser}

    async def _browser_interact(self, target: str, params: dict) -> dict:
        """Interact with browser element"""
        action = params.get("action", "click")
        
        if self._browser:
            return await self._browser.interact(target, action=action, **params)
        
        return {"error": "Browser controller not available"}

    async def _amazon_search(self, query: str, params: dict, conversation_id: str) -> dict:
        from core.amazon_commerce import AmazonCommerceError, get_amazon_commerce_service
        try:
            return await get_amazon_commerce_service().search(
                query, params.get("max_price_inr"), conversation_id,
            )
        except AmazonCommerceError as error:
            return {"success": False, "action": "amazon_search", "error": str(error)}

    async def _amazon_scroll(self, params: dict, conversation_id: str) -> dict:
        from core.amazon_commerce import AmazonCommerceError, get_amazon_commerce_service
        try:
            return await get_amazon_commerce_service().scroll(
                str(params.get("direction") or "down"), conversation_id,
            )
        except AmazonCommerceError as error:
            return {"success": False, "action": "browser_scroll", "error": str(error)}

    async def _amazon_open_product(self, target: str, params: dict, conversation_id: str) -> dict:
        from core.amazon_commerce import AmazonCommerceError, get_amazon_commerce_service
        try:
            return await get_amazon_commerce_service().open_product(
                str(params.get("asin") or target), conversation_id,
            )
        except AmazonCommerceError as error:
            return {"success": False, "action": "amazon_open_product", "error": str(error)}

    async def _amazon_open_cart(self, conversation_id: str) -> dict:
        from core.amazon_commerce import AmazonCommerceError, get_amazon_commerce_service
        try:
            return await get_amazon_commerce_service().open_cart(conversation_id)
        except AmazonCommerceError as error:
            return {"success": False, "action": "amazon_open_cart", "error": str(error)}

    async def _amazon_add_to_cart(self, target: str, params: dict, conversation_id: str) -> dict:
        from core.amazon_commerce import AmazonCommerceError, get_amazon_commerce_service
        try:
            return await get_amazon_commerce_service().add_to_cart(
                str(params.get("asin") or target), conversation_id,
            )
        except AmazonCommerceError as error:
            return {"success": False, "action": "amazon_add_to_cart", "error": str(error)}

    async def _amazon_checkout_preview(self, target: str, params: dict, conversation_id: str) -> dict:
        from core.amazon_commerce import AmazonCommerceError, get_amazon_commerce_service
        try:
            return await get_amazon_commerce_service().checkout_preview(
                str(params.get("asin") or target), params.get("quantity", 1), conversation_id,
            )
        except (AmazonCommerceError, TypeError, ValueError) as error:
            return {"success": False, "action": "amazon_checkout_preview", "error": str(error)}

    async def _amazon_select_payment(self, params: dict, conversation_id: str) -> dict:
        from core.amazon_commerce import AmazonCommerceError, get_amazon_commerce_service
        try:
            return await get_amazon_commerce_service().select_payment(
                str(params.get("method") or ""), conversation_id,
            )
        except AmazonCommerceError as error:
            return {"success": False, "action": "amazon_select_payment", "error": str(error)}

    async def _amazon_place_order(self, params: dict, conversation_id: str) -> dict:
        from core.amazon_commerce import AmazonCommerceError, get_amazon_commerce_service
        try:
            return await get_amazon_commerce_service().place_order(
                params.get("order_snapshot"), conversation_id,
            )
        except AmazonCommerceError as error:
            return {"success": False, "action": "amazon_place_order", "error": str(error)}

    async def _file_operation(self, path: str, params: dict) -> dict:
        """Perform file operation"""
        operation = params.get("operation", "read")
        
        if self._files:
            if operation == "read":
                return await self._files.read(path)
            elif operation == "write":
                return await self._files.write(path, params.get("content", ""))
            elif operation == "create":
                return await self._files.create(path)
            elif operation == "delete":
                return await self._files.delete(path)
        
        return {"error": "File controller not available"}

    async def _system_control(self, command: str, params: dict) -> dict:
        """Control Mac System Settings via JXA"""
        if not self._jxa:
             return {"error": "JXA controller not available"}
             
        if command == "set_volume":
            level = int(params.get("level", 50))
            return await self._jxa.set_volume(level)
            
        elif command == "set_dark_mode":
            enabled = params.get("enabled", True)
            return await self._jxa.set_dark_mode(enabled)
            
        elif command == "open_url":
            url = params.get("url")
            return await self._jxa.open_url(url)
            
        return {"error": f"Unknown system command: {command}"}

    async def _add_app_alias(self, params: dict) -> dict:
        """Register a new app alias"""
        alias = params.get("alias")
        app_name = params.get("app_name")
        
        if not alias or not app_name:
            return {"error": "Missing alias or app_name"}
            
        if self._app_controller:
            success = self._app_controller.add_alias(alias, app_name)
            if success:
                return {"success": True, "message": f"Learned: '{alias}' -> '{app_name}'"}
            else:
                return {"error": "Failed to save alias"}
        
        return {"error": "App controller not available"}

    async def _play_music(self, target: str, params: dict) -> dict:
        """Start a verified Apple Music track for the requested genre."""
        genre = str(params.get("genre") or target or "")
        if not self._music_controller:
            return {
                "success": False,
                "action": "play_music",
                "error": "Apple Music controls are not available on this Mac.",
            }
        return await self._music_controller.play_genre(genre)

    async def _play_youtube_music(self, target: str, params: dict) -> dict:
        """Use verified YouTube browser playback as the music recovery provider."""
        query = str(params.get("query") or target or "")
        if not self._youtube_controller:
            return {
                "success": False,
                "action": "play_music",
                "provider": "youtube",
                "error": "YouTube controls are not available on this Mac.",
            }
        return await self._youtube_controller.play_music(query)

    async def _music_control(self, command: str) -> dict:
        """Apply a playback control to Apple Music."""
        if not self._music_controller:
            return {
                "success": False,
                "action": "music_control",
                "error": "Apple Music controls are not available on this Mac.",
            }

        commands = {
            "pause": self._music_controller.pause,
            "resume": self._music_controller.resume,
            "play": self._music_controller.resume,
            "next": self._music_controller.next_track,
            "next_track": self._music_controller.next_track,
        }
        handler = commands.get(str(command).lower())
        if handler is None:
            return {
                "success": False,
                "action": "music_control",
                "error": f"Unsupported Apple Music command: {command}",
            }
        return await handler()

    def _normalize_web_search_query(self, query: str, params: dict, description: str = "") -> str:
        """Recover the actual search query from imperfect planner output."""
        raw_query = (query or "").strip()
        param_query = str(params.get("query", "") or "").strip()
        description_text = (description or "").strip()

        generic_targets = {"", "query", "url", "search", "web_search", "web"}

        if param_query:
            return param_query

        if raw_query.lower() in generic_targets and description_text:
            cleaned = re.sub(r"^(search|look up|find)\s+(for\s+)?", "", description_text, flags=re.IGNORECASE).strip()
            if cleaned:
                return cleaned

        if raw_query.startswith(("http://", "https://")):
            parsed = urllib.parse.urlparse(raw_query)
            query_params = urllib.parse.parse_qs(parsed.query)

            for key in ("q", "query", "search", "term"):
                values = query_params.get(key)
                if values and values[0].strip():
                    return values[0].strip()

            if description_text:
                cleaned = re.sub(r"^(search|look up|find)\s+(for\s+)?", "", description_text, flags=re.IGNORECASE).strip()
                if cleaned:
                    return cleaned

            path_parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
            if path_parts:
                tail = " ".join(path_parts[-2:])
                tail = tail.replace("-", " ").replace("_", " ").strip()
                if tail:
                    if description_text:
                        return f"{description_text} {tail}".strip()
                    return tail

        if raw_query:
            return raw_query

        return description_text or "web search"

    @staticmethod
    def _add_search_freshness(query: str) -> str:
        """Bias time-sensitive recommendation searches toward current sources."""
        lowered = query.lower()
        time_sensitive = (
            "best", "latest", "current", "recommend", "which llm", "what llm",
            "llm", "gpu", "graphics card", "software version", "price",
        )
        if any(term in lowered for term in time_sensitive) and not re.search(r"\b20\d{2}\b", query):
            return f"{query} {date.today().year} current"
        return query

    @staticmethod
    def _bounded_web_text(value: Any, limit: int) -> str:
        """Keep untrusted search text bounded before it reaches an LLM prompt."""
        cleaned = " ".join(str(value or "").split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 1)].rstrip() + "…"

    @staticmethod
    def _safe_web_source_url(value: str) -> str:
        """Keep attribution while excluding query strings and fragments.

        Search-result URLs can contain a user's query or an access token.  The
        assistant only needs an origin/path to attribute a result.
        """
        parsed = urllib.parse.urlparse(str(value or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return urllib.parse.urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            "",
            "",
        ))

    async def _web_search(self, query: str, params: dict, description: str = "") -> dict:
        """Perform a web search and return summarized results"""
        try:
            import httpx
            from bs4 import BeautifulSoup

            resolved_query = self._add_search_freshness(
                self._normalize_web_search_query(query, params, description)
            )

            def do_search():
                # Specialized intercept for weather data, since DuckDuckGo text snippets don't contain live weather widgets
                lower_q = resolved_query.lower()
                if "weather " in lower_q:
                    city = lower_q.replace("what is the", "").replace("what's the", "").replace("weather", "").replace("in", "").replace("for", "").replace("today", "").replace("now", "").replace("right now", "").strip()
                    if city:
                        try:
                            # Prefer structured weather data so the summarizer does not misread compact symbols.
                            res = httpx.get(
                                f"https://wttr.in/{urllib.parse.quote(city)}?format=j1",
                                timeout=5.0,
                            )
                            if res.status_code == 200:
                                weather_data = res.json()
                                current = (weather_data.get("current_condition") or [{}])[0]
                                if current:
                                    condition_items = current.get("weatherDesc") or [{}]
                                    condition = condition_items[0].get("value", "Unknown")
                                    location = " ".join(part.capitalize() for part in city.split()) or city
                                    summary_lines = [
                                        f"Location: {location}",
                                        f"Condition: {condition}",
                                        f"Temperature: {current.get('temp_C', 'Unknown')} C",
                                        f"Feels like: {current.get('FeelsLikeC', 'Unknown')} C",
                                        f"Wind: {current.get('windspeedKmph', 'Unknown')} km/h",
                                    ]
                                    humidity = current.get("humidity")
                                    if humidity:
                                        summary_lines.append(f"Humidity: {humidity}%")

                                    return {
                                        "scraped_data": self._bounded_web_text(
                                            "Live Weather Data:\n" + "\n".join(summary_lines),
                                            _MAX_WEB_EVIDENCE_CHARS,
                                        ),
                                        "live_weather": {
                                            "location": location,
                                            "condition": condition,
                                            "temperature_c": current.get("temp_C"),
                                            "feels_like_c": current.get("FeelsLikeC"),
                                            "wind_kmph": current.get("windspeedKmph"),
                                            "humidity_percent": humidity,
                                            "source": "wttr.in",
                                        },
                                    }
                        except Exception:
                            pass  # Fallback to standard web search if wttr.in fails
                            
                url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(resolved_query)}"
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                res = httpx.get(url, headers=headers, timeout=10.0)
                soup = BeautifulSoup(res.text, "html.parser")
                results = []
                result_nodes = soup.select(".result")[:5]

                for node in result_nodes:
                    title_node = node.select_one(".result__title")
                    snippet_node = node.select_one(".result__snippet")
                    link_node = node.select_one("a.result__a")
                    parts = []
                    if title_node:
                        parts.append(self._bounded_web_text(
                            title_node.get_text(" ", strip=True),
                            _MAX_WEB_SNIPPET_CHARS,
                        ))
                    if snippet_node:
                        parts.append(self._bounded_web_text(
                            snippet_node.get_text(" ", strip=True),
                            _MAX_WEB_SNIPPET_CHARS,
                        ))
                    if link_node and link_node.get("href"):
                        href = str(link_node.get("href"))
                        parsed_href = urllib.parse.urlparse(href)
                        redirect_target = urllib.parse.parse_qs(parsed_href.query).get("uddg", [""])[0]
                        source_url = urllib.parse.unquote(redirect_target) if redirect_target else href
                        source_url = self._safe_web_source_url(source_url)
                        source_domain = urllib.parse.urlparse(source_url).netloc
                        if source_domain:
                            parts.append(f"Source: {source_domain} {source_url}")
                    if parts:
                        results.append(" - ".join(parts))

                # Fallback for older DuckDuckGo HTML layouts
                if not results:
                    for node in soup.find_all("a", class_="result__snippet")[:3]:
                        results.append(self._bounded_web_text(
                            node.get_text(strip=True),
                            _MAX_WEB_SNIPPET_CHARS,
                        ))
                return self._bounded_web_text("\n\n".join(results), _MAX_WEB_EVIDENCE_CHARS)
                
            # Run the synchronous search in a thread pool to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            search_results = await loop.run_in_executor(None, do_search)
            
            if not search_results:
                return {"error": "No web search results found."}
                
            if isinstance(search_results, dict):
                return {
                    "action": "web_search",
                    "query": resolved_query,
                    "trust": "untrusted_web",
                    "sources_are_untrusted": True,
                    **search_results,
                }

            return {
                "action": "web_search",
                "query": resolved_query,
                "trust": "untrusted_web",
                "sources_are_untrusted": True,
                "scraped_data": self._bounded_web_text(search_results, _MAX_WEB_EVIDENCE_CHARS),
            }
        except Exception as e:
            self.log(f"Web search failed: {e}", level="error")
            return {"error": f"Failed to search web: {str(e)}"}
