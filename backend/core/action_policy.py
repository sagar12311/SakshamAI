"""Deterministic approval rules for every action Saksham can execute.

The planner and any web content are untrusted inputs.  This module is the
single place that decides whether an action is read-only, needs the user's
normal confirmation, or needs a time-bound admin activation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ApprovalLevel(str, Enum):
    NONE = "none"
    CONFIRM = "confirm"
    ADMIN = "admin"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ActionAssessment:
    level: ApprovalLevel
    risk: int
    reason: str

    @property
    def approved_by_default(self) -> bool:
        return self.level is ApprovalLevel.NONE


class ActionPolicy:
    """Classify structured plans without trusting model-supplied risk labels."""

    _protected_paths = (
        "/",
        "/system",
        "/library",
        "/usr",
        "/bin",
        "/sbin",
        "/private",
        "/var",
        "/etc",
    )
    _admin_actions = {
        # External side effects and privileged OS/account changes.
        "send", "send_email", "send_message", "call", "call_contact",
        "buy", "purchase", "payment", "screen_control", "install_software",
        "uninstall_software", "software_install", "software_uninstall",
        "security_control", "credential_change", "password_change",
        "change_password", "set_password", "update_password", "change_credentials",
        "update_credentials", "account_change", "account_security", "browser_interact",
        "browser_click", "browser_type", "browser_submit", "browser_download",
        # Account-changing Amazon controls use the same live voice + PIN
        # ceremony. Buy Now preview remains routine because it never reaches
        # Place Order, but cart/payment changes do affect the account state.
        "amazon_add_to_cart", "amazon_remove_from_cart", "amazon_set_cart_quantity",
        "amazon_select_payment", "amazon_select_address", "amazon_place_order",
    }
    _routine_actions = {
        "open_app", "minimize_app", "maximize_app", "browser_navigate",
        "web_search", "speak_response", "wait_for_user", "music_control",
        "play_music", "play_youtube_music",
        # Deterministic, hostname-pinned Amazon.in reads/preparation only.
        # Generic browser interaction remains admin-only above.
        "amazon_search", "browser_scroll", "amazon_open_product", "amazon_open_cart",
        "amazon_checkout_preview",
    }

    def assess(self, step: dict[str, Any]) -> ActionAssessment:
        action = str(step.get("action") or "").strip().lower()
        target = str(step.get("target") or "").strip()
        parameters = step.get("parameters") or {}

        if action in self._admin_actions:
            return ActionAssessment(ApprovalLevel.ADMIN, 8, "This action can affect your private data or the outside world")

        if action == "terminal_command":
            return ActionAssessment(ApprovalLevel.ADMIN, 8, "Terminal commands require a live admin activation")

        if action == "file_operation":
            operation = str(parameters.get("operation") or "read").lower()
            if self._is_protected_path(target) and operation != "read":
                return ActionAssessment(ApprovalLevel.BLOCKED, 10, "System locations cannot be modified")
            if operation in {"delete", "move", "rename"}:
                return ActionAssessment(ApprovalLevel.ADMIN, 9, "Deleting or moving files requires a live admin activation")
            if operation in {"write", "create"}:
                return ActionAssessment(ApprovalLevel.CONFIRM, 5, "Writing files requires your confirmation")
            return ActionAssessment(ApprovalLevel.NONE, 1, "Read-only file access")

        if action == "system_control":
            command = str(target or parameters.get("command") or "").lower()
            if command in {"capture_screen", "screen_control", "accessibility_control", "change_security"}:
                return ActionAssessment(ApprovalLevel.ADMIN, 8, "Screen or security control requires a live admin activation")
            if command in {"set_volume", "set_dark_mode"}:
                return ActionAssessment(ApprovalLevel.CONFIRM, 4, "This changes a system setting")
            return ActionAssessment(ApprovalLevel.NONE, 1, "Read-only or navigation system action")

        # Keep navigation/read-only browsing routine, but hold interactions
        # that submit forms, download, purchase, or otherwise affect a site.
        if action in {"browser_click", "browser_type", "browser_submit", "browser_download", "browser_interact"}:
            return ActionAssessment(ApprovalLevel.ADMIN, 8, "Browser interaction with an external side effect requires admin activation")

        if action == "close_app":
            return ActionAssessment(ApprovalLevel.CONFIRM, 5, "Closing an app can discard unsaved work")

        if action == "add_app_alias":
            return ActionAssessment(ApprovalLevel.CONFIRM, 4, "Saving an app alias changes Saksham's local configuration")

        if action in self._routine_actions:
            return ActionAssessment(ApprovalLevel.NONE, 1, "Routine, reversible action")

        return ActionAssessment(ApprovalLevel.CONFIRM, 5, "Unknown action type requires your confirmation")

    def assess_plan(self, plan: list[dict[str, Any]]) -> ActionAssessment:
        assessments = [self.assess(step) for step in plan]
        if not assessments:
            return ActionAssessment(ApprovalLevel.BLOCKED, 10, "An empty plan cannot be approved")
        return max(assessments, key=lambda item: (self._priority(item.level), item.risk))

    def _is_protected_path(self, target: str) -> bool:
        normalized = target.replace("~", "").rstrip("/").lower() or "/"
        return any(normalized == path or normalized.startswith(path + "/") for path in self._protected_paths)

    @staticmethod
    def _priority(level: ApprovalLevel) -> int:
        return {
            ApprovalLevel.NONE: 0,
            ApprovalLevel.CONFIRM: 1,
            ApprovalLevel.ADMIN: 2,
            ApprovalLevel.BLOCKED: 3,
        }[level]


_action_policy = ActionPolicy()


def get_action_policy() -> ActionPolicy:
    return _action_policy
