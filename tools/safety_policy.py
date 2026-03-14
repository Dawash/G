"""
Safety and permissions policy engine for tool execution.

Defines 4 safety levels, confirmation policies, dry-run support,
and tool classification. Integrates with the ToolExecutor to gate
actions before handlers run.

Safety levels:
  safe      — No confirmation, execute immediately
  moderate  — Optional confirmation (configurable per-tool)
  sensitive — Confirmation required before execution
  critical  — Strong confirmation required, always audited

See docs/safety-model.md for full documentation.
"""

import logging
import os
import re

logger = logging.getLogger(__name__)


# ===================================================================
# Safety levels
# ===================================================================

SAFE = "safe"
MODERATE = "moderate"
SENSITIVE = "sensitive"
CRITICAL = "critical"

_LEVELS_ORDERED = [SAFE, MODERATE, SENSITIVE, CRITICAL]


# ===================================================================
# Tool classification — safety level for every known tool
# ===================================================================

TOOL_SAFETY_LEVELS = {
    # --- Safe: read-only, no side effects, no confirmation ---
    "get_weather": SAFE,
    "get_forecast": SAFE,
    "get_time": SAFE,
    "get_news": SAFE,
    "list_reminders": SAFE,
    "take_screenshot": SAFE,
    "find_on_screen": SAFE,
    "web_read": SAFE,
    "web_search_answer": SAFE,
    "read_clipboard": SAFE,
    "write_clipboard": SAFE,
    "analyze_clipboard_image": SAFE,

    # --- Moderate: mild side effects, optional confirmation ---
    "open_app": MODERATE,
    "close_app": MODERATE,
    "minimize_app": MODERATE,
    "snap_window": MODERATE,
    "google_search": MODERATE,
    "set_reminder": MODERATE,
    "manage_alarm": MODERATE,
    "toggle_setting": MODERATE,
    "play_music": MODERATE,
    "search_in_app": MODERATE,
    "type_text": MODERATE,
    "press_key": MODERATE,
    "click_at": MODERATE,
    "scroll": MODERATE,
    "click_element": MODERATE,
    "manage_tabs": MODERATE,
    "fill_form": MODERATE,
    "create_file": MODERATE,

    # --- Sensitive: confirmation required, reversible ---
    "send_email": SENSITIVE,
    "run_terminal": SENSITIVE,
    "manage_files": SENSITIVE,
    "manage_software": SENSITIVE,
    "run_self_test": SENSITIVE,

    # --- Critical: strong confirmation required, hard to reverse ---
    "system_command": CRITICAL,
    "restart_assistant": CRITICAL,
    "agent_task": CRITICAL,
}


def get_safety_level(tool_name):
    """Return the safety level for a tool. Defaults to MODERATE for unknown tools."""
    return TOOL_SAFETY_LEVELS.get(tool_name, MODERATE)


# ===================================================================
# Confirmation policies — per-level and per-tool overrides
# ===================================================================

# Per-tool confirmation message generators.
# Returns a description string if confirmation is needed, None to skip.
_CONFIRM_DESCRIPTIONS = {
    "send_email": lambda args: (
        f"send an email to {args.get('to', 'someone')} "
        f"about {args.get('subject', 'something')}"
    ),
    "system_command": lambda args: (
        f"run system command: {args.get('command', '?')}"
        if args.get("command") in ("shutdown", "restart")
        else None
    ),
    "manage_software": lambda args: (
        f"{args.get('action', 'manage')} software: {args.get('name', '?')}"
        if args.get("action") in ("install", "uninstall")
        else None
    ),
    "manage_files": lambda args: (
        f"delete {args.get('path', '?')}"
        if args.get("action") == "delete"
        else None
    ),
    "run_terminal": lambda args: (
        f"run command: {args.get('command', '?')[:60]}"
        if _is_risky_command(args.get("command", ""))
        else None
    ),
    "restart_assistant": lambda args: "restart the assistant",
    "agent_task": lambda args: (
        f"run agent mode for: {args.get('goal', '?')[:60]}"
    ),
}

# Backwards-compatible alias used by brain.py import
CONFIRM_TOOLS = _CONFIRM_DESCRIPTIONS


def needs_confirmation(tool_name, arguments):
    """Check if a tool call needs user confirmation.

    Returns:
        str: Description of what needs confirming, or None if no confirmation needed.
    """
    level = get_safety_level(tool_name)

    # Safe tools never need confirmation
    if level == SAFE:
        return None

    # Moderate tools don't need confirmation by default
    if level == MODERATE:
        return None

    # Sensitive and critical: check per-tool rules
    if tool_name in _CONFIRM_DESCRIPTIONS:
        desc_fn = _CONFIRM_DESCRIPTIONS[tool_name]
        return desc_fn(arguments)

    # Sensitive/critical without specific rules: generic confirmation
    if level == CRITICAL:
        return f"execute {tool_name}"
    if level == SENSITIVE:
        return f"run {tool_name}"

    return None


def confirm_with_user(tool_name, arguments, speak_fn):
    """Ask user for voice confirmation before executing a tool.

    Returns True if user confirms, False if denied.
    """
    desc = needs_confirmation(tool_name, arguments)

    # No confirmation needed
    if desc is None:
        return True

    if not speak_fn:
        return True

    # Skip confirmation in text mode
    if os.environ.get("G_INPUT_MODE") == "text":
        logger.info(f"Text mode: auto-confirming {tool_name}")
        return True

    try:
        speak_fn(f"Should I {desc}? Say yes or no.")
    except Exception:
        return True

    try:
        from speech import listen
        response = listen()
        if response:
            response_lower = response.lower().strip()
            if any(w in response_lower for w in
                   ["yes", "yeah", "yep", "sure", "go ahead",
                    "do it", "confirm"]):
                logger.info(f"User confirmed {tool_name}")
                return True
            else:
                logger.info(f"User denied {tool_name}: '{response}'")
                return False
        return False
    except Exception as e:
        logger.warning(f"Confirmation listen failed: {e}")
        return True


def get_confirmation_status(tool_name, arguments, speak_fn):
    """Determine confirmation status and get user confirmation if needed.

    Returns:
        tuple: (allowed: bool, status: str)
        status is one of: "not_required", "confirmed", "denied", "auto_confirmed"
    """
    desc = needs_confirmation(tool_name, arguments)

    if desc is None:
        return True, "not_required"

    if not speak_fn:
        return True, "auto_confirmed"

    if os.environ.get("G_INPUT_MODE") == "text":
        return True, "auto_confirmed"

    try:
        speak_fn(f"Should I {desc}? Say yes or no.")
    except Exception:
        return True, "auto_confirmed"

    try:
        from speech import listen
        response = listen()
        if response:
            response_lower = response.lower().strip()
            if any(w in response_lower for w in
                   ["yes", "yeah", "yep", "sure", "go ahead",
                    "do it", "confirm"]):
                return True, "confirmed"
            else:
                return False, "denied"
        return False, "denied"
    except Exception:
        return True, "auto_confirmed"


# ===================================================================
# Dry-run support
# ===================================================================

# Tools that support dry-run mode (preview without executing)
_DRY_RUN_TOOLS = {
    "run_terminal", "manage_files", "manage_software",
    "system_command", "send_email",
}


def supports_dry_run(tool_name):
    """Check if a tool supports dry-run mode."""
    return tool_name in _DRY_RUN_TOOLS


def dry_run(tool_name, arguments):
    """Generate a dry-run preview of what a tool would do.

    Returns a description string of the planned action, without executing it.
    """
    if tool_name == "run_terminal":
        cmd = arguments.get("command", "")
        admin = arguments.get("admin", False)
        prefix = "[ADMIN] " if admin else ""
        blocked = _check_terminal_blocklist(cmd)
        if blocked:
            return f"[DRY-RUN] BLOCKED: {blocked}"
        return f"[DRY-RUN] {prefix}PowerShell: {cmd}"

    elif tool_name == "manage_files":
        action = arguments.get("action", "?")
        path = arguments.get("path", "?")
        dest = arguments.get("destination", "")
        if action == "delete":
            return f"[DRY-RUN] DELETE file/folder: {path}"
        elif action in ("move", "copy"):
            return f"[DRY-RUN] {action.upper()} {path} -> {dest}"
        elif action == "rename":
            return f"[DRY-RUN] RENAME {path} -> {dest}"
        elif action == "zip":
            return f"[DRY-RUN] ZIP {path}"
        elif action == "unzip":
            return f"[DRY-RUN] UNZIP {path} -> {dest or 'same directory'}"
        return f"[DRY-RUN] {action} on {path}"

    elif tool_name == "manage_software":
        action = arguments.get("action", "?")
        name = arguments.get("name", "?")
        if action == "install":
            return f"[DRY-RUN] INSTALL software: {name} (via winget)"
        elif action == "uninstall":
            return f"[DRY-RUN] UNINSTALL software: {name}"
        elif action == "update":
            return f"[DRY-RUN] UPDATE software: {name}"
        elif action == "update_all":
            return "[DRY-RUN] UPDATE ALL installed software"
        return f"[DRY-RUN] {action} software: {name}"

    elif tool_name == "system_command":
        cmd = arguments.get("command", "?")
        return f"[DRY-RUN] System command: {cmd}"

    elif tool_name == "send_email":
        to = arguments.get("to", "?")
        subj = arguments.get("subject", "?")
        return f"[DRY-RUN] Send email to {to}, subject: {subj}"

    return f"[DRY-RUN] {tool_name}({arguments})"


# ===================================================================
# Terminal command safety checks
# ===================================================================

_TERMINAL_BLOCKLIST = [
    "format-volume", "format c:", "format d:",
    "remove-item -recurse -force c:", "remove-item -recurse -force /",
    "del /s /q c:", "rd /s /q c:",
    "rm -rf /", "rm -rf c:",
    "reg delete", "reg add",
    "bcdedit", "diskpart",
    "net user", "net localgroup",
    "set-executionpolicy unrestricted",
    "invoke-webrequest", "invoke-restmethod",
    "start-bitstransfer",
    "new-psdrive",
]

_RISKY_PATTERNS = [
    r"remove-item\b.*-recurse",
    r"\bdel\b.*[/\\]",
    r"\brd\b.*[/\\]",
    r"stop-process\b.*-force",
    r"restart-service\b",
    r"stop-service\b",
    r"set-service\b",
    r"netsh\b.*firewall",
]


def _check_terminal_blocklist(command):
    """Check if a terminal command is blocked.

    Returns the blocked pattern string if blocked, None otherwise.
    """
    cmd_lower = command.lower().strip()
    for blocked in _TERMINAL_BLOCKLIST:
        if blocked in cmd_lower:
            return blocked
    return None


def _is_risky_command(command):
    """Check if a terminal command is risky (needs confirmation)."""
    cmd_lower = command.lower().strip()
    # Blocked commands are always risky
    if _check_terminal_blocklist(cmd_lower):
        return True
    # Check risky patterns
    for pattern in _RISKY_PATTERNS:
        if re.search(pattern, cmd_lower, re.I):
            return True
    return False


def check_terminal_safety(command):
    """Full safety check for a terminal command.

    Returns:
        tuple: (allowed: bool, reason: str)
    """
    blocked = _check_terminal_blocklist(command)
    if blocked:
        return False, f"Blocked for safety: contains '{blocked}'"
    return True, ""


# ===================================================================
# Tool choice validation (catch LLM stickiness)
# ===================================================================

def validate_tool_choice(tool_name, user_input):
    """Catch obvious tool mismatches from LLM stickiness.

    Returns corrected tool_name if mismatch detected, else original.
    """
    if not user_input:
        return tool_name
    lower = user_input.lower()

    _CLEAR_PATTERNS = [
        (r"\b(toggle|turn on|turn off|switch on|switch off|enable|disable)\b"
         r".*(mode|light|wifi|wi-fi|bluetooth|setting|airplane|dark|night)",
         "toggle_setting",
         {"send_email", "create_file", "system_command", "get_weather",
          "get_time", "get_news", "web_read", "web_search_answer", "open_app"}),
        (r"\b(play|listen to)\b"
         r".*(music|song|jazz|rock|classical|pop|hip.hop|spotify|playlist)",
         "play_music",
         {"send_email", "create_file", "system_command", "get_weather",
          "get_time", "get_news", "web_read", "web_search_answer", "open_app"}),
        # open_app: only override non-info tools (never override get_weather/get_news/etc.)
        (r"^(?:open|launch|start)\s+(?:the\s+)?(?:my\s+)?\w+",
         "open_app",
         {"send_email", "create_file", "system_command"}),
    ]
    for pattern, expected, targets in _CLEAR_PATTERNS:
        if re.search(pattern, lower) and tool_name != expected:
            if tool_name in targets:
                logger.warning(
                    f"Tool mismatch: LLM chose {tool_name} but "
                    f"'{lower}' clearly needs {expected} -- overriding")
                return expected
    return tool_name
