"""
Execution Tiers — strict autonomy levels for task routing.

Tier 0: DETERMINISTIC — local-only tools, no UI, no network side effects
  Time, weather API, reminders, file reads, math, system info queries

Tier 1: STRUCTURED_AUTOMATION — UI automation via accessibility/DOM
  pywinauto, Playwright, WinAppDriver, COM automation
  No raw pixel clicking, no vision-based guessing

Tier 2: ADAPTIVE_AUTOMATION — vision + recovery when structured fails
  Screenshot + LLM analysis, mouse/keyboard fallback
  Only used AFTER Tier 1 fails

Tier 3: HUMAN_REQUIRED — must hand off to user
  Payments, logins, 2FA, CAPTCHAs, admin prompts, credential entry
  Agent PAUSES and asks user to complete this step
"""

import logging
from enum import IntEnum
from typing import Optional

logger = logging.getLogger(__name__)


# ===================================================================
# Tier definitions
# ===================================================================

class ExecutionTier(IntEnum):
    """Autonomy level for a tool or action."""
    DETERMINISTIC = 0         # Pure data, no side effects on OS or UI
    STRUCTURED = 1            # Structured automation (accessibility, DOM, COM)
    ADAPTIVE = 2              # Vision-based, coordinate-based, recovery loops
    HUMAN_REQUIRED = 3        # Must pause and ask user (payments, 2FA, logins)


# ===================================================================
# Tool → Tier mapping (explicit, single source of truth)
# ===================================================================

TOOL_TIER_MAP: dict[str, ExecutionTier] = {
    # --- Tier 0: DETERMINISTIC — read-only data, no UI interaction ---
    "get_time":             ExecutionTier.DETERMINISTIC,
    "get_weather":          ExecutionTier.DETERMINISTIC,
    "get_forecast":         ExecutionTier.DETERMINISTIC,
    "get_news":             ExecutionTier.DETERMINISTIC,
    "set_reminder":         ExecutionTier.DETERMINISTIC,
    "list_reminders":       ExecutionTier.DETERMINISTIC,
    "manage_alarm":         ExecutionTier.DETERMINISTIC,
    "web_read":             ExecutionTier.DETERMINISTIC,
    "web_search_answer":    ExecutionTier.DETERMINISTIC,
    "google_search":        ExecutionTier.DETERMINISTIC,
    "read_clipboard":       ExecutionTier.DETERMINISTIC,
    "memory_control":       ExecutionTier.DETERMINISTIC,
    "get_calendar":         ExecutionTier.DETERMINISTIC,
    "run_self_test":        ExecutionTier.DETERMINISTIC,
    "search_tools":         ExecutionTier.DETERMINISTIC,

    # --- Tier 1: STRUCTURED_AUTOMATION — app/window/browser control ---
    "open_app":             ExecutionTier.STRUCTURED,
    "close_app":            ExecutionTier.STRUCTURED,
    "minimize_app":         ExecutionTier.STRUCTURED,
    "focus_window":         ExecutionTier.STRUCTURED,
    "snap_window":          ExecutionTier.STRUCTURED,
    "list_windows":         ExecutionTier.STRUCTURED,
    "inspect_window":       ExecutionTier.STRUCTURED,
    "toggle_setting":       ExecutionTier.STRUCTURED,
    "system_command":       ExecutionTier.STRUCTURED,
    "play_music":           ExecutionTier.STRUCTURED,
    "search_in_app":        ExecutionTier.STRUCTURED,
    "manage_tabs":          ExecutionTier.STRUCTURED,
    "browser_action":       ExecutionTier.STRUCTURED,
    "click_element":        ExecutionTier.STRUCTURED,
    "click_control":        ExecutionTier.STRUCTURED,
    "set_control_text":     ExecutionTier.STRUCTURED,
    "fill_form":            ExecutionTier.STRUCTURED,
    "create_file":          ExecutionTier.STRUCTURED,
    "run_terminal":         ExecutionTier.STRUCTURED,
    "manage_files":         ExecutionTier.STRUCTURED,
    "manage_software":      ExecutionTier.STRUCTURED,
    "send_email":           ExecutionTier.STRUCTURED,
    "restart_assistant":    ExecutionTier.STRUCTURED,
    "run_workflow":         ExecutionTier.STRUCTURED,

    # --- Tier 2: ADAPTIVE_AUTOMATION — vision, coordinates, agent loops ---
    "click_at":             ExecutionTier.ADAPTIVE,
    "type_text":            ExecutionTier.ADAPTIVE,
    "press_key":            ExecutionTier.ADAPTIVE,
    "scroll":               ExecutionTier.ADAPTIVE,
    "take_screenshot":      ExecutionTier.ADAPTIVE,
    "find_on_screen":       ExecutionTier.ADAPTIVE,
    "analyze_clipboard_image": ExecutionTier.ADAPTIVE,
    "agent_task":           ExecutionTier.ADAPTIVE,
}

# Domains/keywords that force Tier 3 regardless of tool
_HUMAN_REQUIRED_DOMAINS = frozenset({
    # Financial
    "payment", "checkout", "billing", "purchase", "buy", "order",
    "credit card", "debit card", "bank", "paypal", "venmo", "stripe",
    "wallet", "transaction", "transfer money", "wire transfer",
    # Authentication
    "login", "sign in", "signin", "log in", "authenticate",
    "password", "credential", "two-factor", "2fa", "otp",
    "captcha", "recaptcha", "verify you are human",
    # Sensitive data
    "ssn", "social security", "tax", "medical record",
    "passport", "driver license", "insurance",
    # System privileges
    "admin", "uac", "elevation", "run as administrator",
    # Crypto/investment
    "crypto", "bitcoin", "ethereum", "trading", "invest",
})


# ===================================================================
# Tier policies — what each tier allows/requires
# ===================================================================

TIER_POLICIES = {
    ExecutionTier.DETERMINISTIC: {
        "auto_execute": True,
        "requires_confirmation": False,
        "log_action": False,
        "screenshot_before_after": False,
        "description": "Auto-execute, no confirmation needed",
    },
    ExecutionTier.STRUCTURED: {
        "auto_execute": True,
        "requires_confirmation": False,
        "log_action": True,
        "screenshot_before_after": False,
        "description": "Auto-execute, log action for audit trail",
    },
    ExecutionTier.ADAPTIVE: {
        "auto_execute": True,
        "requires_confirmation": False,
        "log_action": True,
        "screenshot_before_after": True,
        "description": "Auto-execute, log + screenshot before/after",
    },
    ExecutionTier.HUMAN_REQUIRED: {
        "auto_execute": False,
        "requires_confirmation": True,
        "log_action": True,
        "screenshot_before_after": True,
        "description": "Pause and require human confirmation",
    },
}


# ===================================================================
# Classification
# ===================================================================

def classify_tier(tool_name: str, args: Optional[dict] = None,
                  context: Optional[dict] = None) -> ExecutionTier:
    """Classify a tool call into its execution tier.

    Checks:
      1. Context-based overrides (payment/login domains → Tier 3)
      2. Argument-based escalation (risky terminal commands → higher tier)
      3. Explicit TOOL_TIER_MAP lookup
      4. Default to STRUCTURED for unknown tools

    Args:
        tool_name: Canonical tool name.
        args: Tool arguments dict (optional).
        context: Ambient context dict with keys like 'active_window',
                 'url', 'user_goal' (optional).

    Returns:
        ExecutionTier for this specific invocation.
    """
    args = args or {}
    context = context or {}

    # --- Pass 1: Check for human-required domains in context ---
    goal = str(context.get("user_goal", "")).lower()
    url = str(context.get("url", "")).lower()
    active_window = str(context.get("active_window", "")).lower()

    context_text = f"{goal} {url} {active_window}"
    for domain_kw in _HUMAN_REQUIRED_DOMAINS:
        if domain_kw in context_text:
            logger.info(f"Tier override → HUMAN_REQUIRED: "
                        f"'{domain_kw}' found in context for {tool_name}")
            return ExecutionTier.HUMAN_REQUIRED

    # --- Pass 2: Argument-based escalation ---
    tier = _check_arg_escalation(tool_name, args)
    if tier is not None:
        return tier

    # --- Pass 3: Explicit map lookup ---
    if tool_name in TOOL_TIER_MAP:
        return TOOL_TIER_MAP[tool_name]

    # --- Pass 4: Default for unknown tools ---
    logger.debug(f"Unknown tool '{tool_name}' → default STRUCTURED")
    return ExecutionTier.STRUCTURED


def _check_arg_escalation(tool_name: str, args: dict) -> Optional[ExecutionTier]:
    """Check if specific arguments escalate a tool to a higher tier.

    Returns an ExecutionTier if escalation applies, None otherwise.
    """
    # run_terminal: destructive commands escalate to ADAPTIVE
    if tool_name == "run_terminal":
        cmd = str(args.get("command", "")).lower()
        admin = args.get("admin", False)
        # Admin commands → ADAPTIVE (needs extra caution)
        if admin:
            return ExecutionTier.ADAPTIVE
        # Destructive patterns → ADAPTIVE
        destructive_patterns = [
            "remove-item", "del ", "rd ", "rm ",
            "format", "diskpart", "bcdedit",
            "stop-process", "stop-service",
        ]
        for pattern in destructive_patterns:
            if pattern in cmd:
                return ExecutionTier.ADAPTIVE

    # manage_files: delete operations escalate
    if tool_name == "manage_files":
        action = str(args.get("action", "")).lower()
        if action == "delete":
            return ExecutionTier.ADAPTIVE

    # manage_software: install/uninstall escalate
    if tool_name == "manage_software":
        action = str(args.get("action", "")).lower()
        if action in ("install", "uninstall"):
            return ExecutionTier.ADAPTIVE

    # system_command: shutdown/restart are structured but notable
    if tool_name == "system_command":
        cmd = str(args.get("command", "")).lower()
        if cmd in ("shutdown", "restart"):
            return ExecutionTier.STRUCTURED

    # send_email: always at least STRUCTURED (already mapped),
    # but sending to unknown recipients could be escalated by caller
    return None


# ===================================================================
# Policy enforcement
# ===================================================================

def check_tier_policy(tier: ExecutionTier, tool_name: str,
                      args: Optional[dict] = None) -> tuple[bool, str]:
    """Check whether an action is allowed under its tier's policy.

    Returns:
        (allowed, reason): allowed is True if the action can proceed,
        reason explains the decision.
    """
    policy = TIER_POLICIES.get(tier)
    if policy is None:
        return False, f"Unknown tier: {tier}"

    if policy["auto_execute"]:
        return True, policy["description"]

    # HUMAN_REQUIRED: not auto-executable
    if tier == ExecutionTier.HUMAN_REQUIRED:
        return False, (
            f"Tool '{tool_name}' classified as HUMAN_REQUIRED — "
            f"agent must pause and ask user to complete this step"
        )

    return True, policy["description"]


def get_tier_name(tier: ExecutionTier) -> str:
    """Human-readable tier name."""
    names = {
        ExecutionTier.DETERMINISTIC: "Tier 0: Deterministic",
        ExecutionTier.STRUCTURED: "Tier 1: Structured Automation",
        ExecutionTier.ADAPTIVE: "Tier 2: Adaptive Automation",
        ExecutionTier.HUMAN_REQUIRED: "Tier 3: Human Required",
    }
    return names.get(tier, f"Tier {tier.value}: Unknown")
