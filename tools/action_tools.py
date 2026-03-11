"""
Action tool registrations — tools that perform system actions.

Registers: close_app, minimize_app, toggle_setting, system_command,
           run_self_test, restart_assistant
"""

import logging

from tools.schemas import ToolSpec
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ===================================================================
# Handler functions
# ===================================================================

def _handle_close_app(arguments, action_registry):
    if "close_app" not in action_registry:
        return "Error: close_app not available in action registry."
    return action_registry["close_app"](arguments.get("name", ""))


def _handle_minimize_app(arguments, action_registry):
    if "minimize_app" not in action_registry:
        return "Error: minimize_app not available in action registry."
    return action_registry["minimize_app"](arguments.get("name", ""))


def _handle_toggle_setting(arguments):
    from brain_defs import _toggle_system_setting
    setting = arguments.get("setting", "").lower()
    state = arguments.get("state", "off").lower()
    return _toggle_system_setting(setting, state)


def _handle_system_command(arguments, action_registry, user_input=""):
    cmd = arguments.get("command", "")
    valid_power_cmds = {"shutdown", "restart", "sleep", "cancel_shutdown"}
    if cmd not in valid_power_cmds:
        return f"'{cmd}' is not a power command. Use agent_task for settings changes."
    # Safety: prevent "turn off bluetooth" from triggering shutdown
    if cmd in ("shutdown", "restart") and user_input:
        user_text = user_input.lower()
        feature_words = [
            "bluetooth", "wifi", "wi-fi", "hotspot", "location",
            "airplane", "vpn", "night light", "dark mode",
            "brightness", "volume", "notification",
        ]
        if any(w in user_text for w in feature_words):
            logger.warning(f"Blocked misrouted {cmd} — user said: {user_text}")
            return f"I won't {cmd} — you asked about a feature, not the computer. Use agent_task instead."
    if cmd in action_registry:
        return action_registry[cmd](None)
    return f"Unknown system command: {cmd}"


def _handle_run_self_test(arguments):
    from self_test import run_self_test
    return run_self_test()


def _handle_restart_assistant(arguments):
    return "__RESTART__"


# ===================================================================
# Rollback functions
# ===================================================================

def _rollback_close_app(arguments, action_registry):
    name = arguments.get("name", "")
    open_fn = action_registry.get("open_app")
    if open_fn:
        return open_fn(name)
    return f"No open handler for {name}"


# Issue #2 warning: undo after close_app only re-launches the app — it cannot
# restore unsaved documents, browser tabs, or in-progress work. The rollback
# description below is updated to make this clear to the user.


def _rollback_toggle_setting(arguments, action_registry):
    from brain_defs import _toggle_system_setting
    setting = arguments.get("setting", "")
    state = arguments.get("state", "off")
    opposite = "on" if state == "off" else "off"
    return _toggle_system_setting(setting, opposite)


def _handle_manage_alarm(arguments):
    """Handle alarm management: add, list, remove, toggle."""
    from alarms import get_alarm_manager
    am = get_alarm_manager()
    if not am:
        return "Alarm system not initialized yet. Try again after startup."
    action = arguments.get("action", "add").lower()
    if action in ("add", "set"):
        time_str = arguments.get("time", "")
        if not time_str:
            return "Please specify a time, e.g. '7am' or '6:30 AM'."
        label = arguments.get("label", "Alarm")
        alarm_type = arguments.get("type", "morning")
        recurrence = arguments.get("recurrence", None)
        return am.add_alarm(time_str, alarm_type=alarm_type,
                            label=label, recurrence=recurrence)
    elif action == "list":
        return am.list_alarms()
    elif action in ("remove", "delete", "cancel"):
        alarm_id = arguments.get("alarm_id", "")
        if not alarm_id:
            return "Specify which alarm to remove (use 'list alarms' to see IDs)."
        return am.remove_alarm(alarm_id)
    elif action in ("toggle", "enable", "disable"):
        alarm_id = arguments.get("alarm_id", "")
        active = action != "disable"
        return am.toggle_alarm(alarm_id, active=active)
    return f"Unknown action: {action}. Use add, list, remove, or toggle."


# ===================================================================
# Registration
# ===================================================================

def register_action_tools(registry: ToolRegistry):
    """Register system action tools into the registry."""

    registry.register(ToolSpec(
        name="close_app",
        description="Close/quit an application. Use for 'close Chrome', 'quit Spotify'.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Application name to close"}
            },
            "required": ["name"]
        },
        handler=_handle_close_app,
        requires_registry=True,
        rollback=_rollback_close_app,
        rollback_description="closed {name} (undo will reopen but unsaved work is lost)",
        aliases=["close", "kill", "quit", "close_application", "close_window"],
        arg_aliases={"app_name": "name", "app": "name", "application": "name"},
        primary_arg="name",
        core=True,
    ))

    registry.register(ToolSpec(
        name="minimize_app",
        description="Minimize a window. Use for 'minimize Chrome', 'minimize this window'.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Application name to minimize"}
            },
            "required": ["name"]
        },
        handler=_handle_minimize_app,
        requires_registry=True,
        aliases=["minimize", "min", "minimize_application", "minimize_window"],
        arg_aliases={"app_name": "name", "app": "name", "application": "name"},
        primary_arg="name",
        core=True,
    ))

    registry.register(ToolSpec(
        name="toggle_setting",
        description=(
            "Toggle a Windows system setting ON or OFF. Use for: dark mode, Bluetooth, "
            "WiFi, night light, airplane mode. ALWAYS use this for settings — NEVER use "
            "create_file, play_music, press_key, or agent_task for toggles."
        ),
        parameters={
            "type": "object",
            "properties": {
                "setting": {"type": "string", "description": "The setting to toggle (e.g. 'bluetooth', 'wifi', 'dark mode', 'night light', 'airplane mode')"},
                "state": {"type": "string", "description": "Target state", "enum": ["on", "off"]}
            },
            "required": ["setting", "state"]
        },
        handler=_handle_toggle_setting,
        safety="moderate",
        rollback=_rollback_toggle_setting,
        rollback_description="toggled {setting} {state}",
        aliases=["toggle", "bluetooth", "wifi", "wifi_toggle", "bluetooth_toggle",
                 "dark_mode", "night_light", "airplane", "airplane_mode", "setting"],
        arg_aliases={"feature": "setting", "name": "setting", "device": "setting",
                     "action": "state", "mode": "state", "value": "state"},
        primary_arg="setting",
        core=True,
    ))

    registry.register(ToolSpec(
        name="system_command",
        description=(
            "Execute a COMPUTER POWER command ONLY: shutdown, restart, sleep, or "
            "cancel_shutdown. WARNING: Do NOT use this for turning off Bluetooth, WiFi, "
            "or any app/feature — those use agent_task instead. ONLY use for powering "
            "off/restarting the ENTIRE computer."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The system command to execute",
                    "enum": ["shutdown", "restart", "sleep", "cancel_shutdown"]
                }
            },
            "required": ["command"]
        },
        handler=_handle_system_command,
        requires_registry=True,
        requires_user_input=True,
        safety="critical",
        aliases=["system", "sys", "shutdown", "restart"],
        arg_aliases={"cmd": "command", "action": "command"},
        primary_arg="command",
        core=True,
    ))

    registry.register(ToolSpec(
        name="run_self_test",
        description=(
            "Run diagnostics on all systems. Tests every module, API connection, "
            "memory, weather, news, etc. Use when user asks to check/test/debug the system."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_handle_run_self_test,
        safety="sensitive",
        aliases=["self_test", "test", "diagnostics"],
    ))

    registry.register(ToolSpec(
        name="restart_assistant",
        description="Restart the AI assistant. Use when user asks to restart, reboot, or reload.",
        parameters={"type": "object", "properties": {}},
        handler=_handle_restart_assistant,
        safety="critical",
    ))

    registry.register(ToolSpec(
        name="manage_alarm",
        description=(
            "Set, list, or remove alarms. Use for 'set alarm for 7am', "
            "'wake me up at 6:30', 'list my alarms', 'remove alarm', "
            "'set morning alarm'. Morning alarms play sound and give "
            "weather + news briefing after dismissal."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action: add, list, remove, toggle",
                    "enum": ["add", "list", "remove", "toggle"],
                },
                "time": {
                    "type": "string",
                    "description": "Alarm time: '7am', '6:30 AM', '17:00'",
                },
                "label": {
                    "type": "string",
                    "description": "Alarm label, e.g. 'Morning wake up', 'Gym time'",
                },
                "type": {
                    "type": "string",
                    "description": "Alarm type: morning (with briefing) or custom",
                    "enum": ["morning", "custom", "one_time"],
                },
                "recurrence": {
                    "type": "string",
                    "description": "Recurrence: daily, weekdays, weekends, once, or 'mon,wed,fri'",
                },
                "alarm_id": {
                    "type": "string",
                    "description": "Alarm ID (for remove/toggle)",
                },
            },
            "required": []
        },
        handler=_handle_manage_alarm,
        aliases=["alarm", "set_alarm", "wake_up", "wake_me", "morning_alarm",
                 "add_alarm", "list_alarms", "remove_alarm"],
        arg_aliases={"when": "time", "at": "time", "name": "label", "message": "label"},
        primary_arg="time",
        core=False,  # Cloud-only: overlaps with set_reminder, confuses 7B model
    ))

    logger.info(f"Registered 7 action tools")
