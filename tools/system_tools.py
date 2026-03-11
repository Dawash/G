"""
System tool registrations — terminal, file management, software management.

Registers: run_terminal, manage_files, manage_software
Handlers imported from brain_defs.py (existing implementations).
"""

import logging

from tools.schemas import ToolSpec
from tools.registry import ToolRegistry
from tools.safety_policy import _is_risky_command

logger = logging.getLogger(__name__)


# ===================================================================
# Handler wrappers (delegate to brain_defs.py implementations)
# ===================================================================

def _handle_run_terminal(arguments):
    from brain_defs import _run_terminal
    return _run_terminal(
        arguments.get("command", ""),
        arguments.get("admin", False),
    )


def _handle_manage_files(arguments):
    from brain_defs import _manage_files
    return _manage_files(
        arguments.get("action", "list"),
        arguments.get("path", ""),
        arguments.get("destination"),
    )


def _handle_manage_software(arguments):
    from brain_defs import _manage_software
    return _manage_software(
        arguments.get("action", "search"),
        arguments.get("name"),
    )


# ===================================================================
# Confirmation conditions (only confirm for risky operations)
# ===================================================================

def _confirm_run_terminal(args):
    cmd = args.get("command", "")
    if args.get("admin", False):
        return f"run admin command: {cmd}"
    if _is_risky_command(cmd):
        return f"run risky command: {cmd}"
    return None  # Safe command, no confirmation


def _confirm_manage_files(args):
    if args.get("action") == "delete":
        return f"delete {args.get('path', 'files')}"
    return None


def _confirm_manage_software(args):
    action = args.get("action", "")
    name = args.get("name", "software")
    if action in ("install", "uninstall"):
        return f"{action} {name}"
    return None


# ===================================================================
# Registration
# ===================================================================

def register_system_tools(registry: ToolRegistry):
    """Register system management tools into the registry."""

    registry.register(ToolSpec(
        name="run_terminal",
        description=(
            "Run a PowerShell or CMD command on this Windows computer. Use for: system info, "
            "disk space, process management, network tools (ping, ipconfig), file listing, "
            "running scripts, git commands, docker, checking ports, services, installed apps. "
            "Do NOT use for: opening apps (use open_app), settings (use toggle_setting), "
            "power commands (use system_command)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string",
                            "description": "The PowerShell command to run (e.g. 'Get-PSDrive C', 'tasklist', 'ping google.com -n 4', 'git status')"},
                "admin": {"type": "boolean",
                          "description": "Set to true if command needs admin privileges. Default false."},
            },
            "required": ["command"]
        },
        handler=_handle_run_terminal,
        safety="sensitive",
        confirm_condition=_confirm_run_terminal,
        aliases=["terminal", "powershell", "cmd", "command", "shell",
                 "run_command", "execute", "exec", "cli"],
        arg_aliases={"cmd": "command", "shell": "command", "run": "command",
                     "powershell": "command", "script": "command"},
        primary_arg="command",
        core=True,
        isolate=True,
    ))

    registry.register(ToolSpec(
        name="manage_files",
        description=(
            "Manage files on this computer. Use for: move, copy, rename, delete files/folders, "
            "zip/unzip, find files by name, get folder size, list directory contents, organize "
            "files by extension. Paths are relative to user home (Desktop, Documents, Downloads)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["move", "copy", "rename", "delete", "zip",
                             "unzip", "find", "size", "list", "organize"],
                    "description": "What to do with the file(s)",
                },
                "path": {"type": "string",
                         "description": "File or folder path (relative to user home, e.g. 'Desktop/report.pdf', 'Downloads/*.zip')"},
                "destination": {"type": "string",
                                "description": "Destination path for move/copy/rename (e.g. 'Documents/reports/')"},
            },
            "required": ["action", "path"]
        },
        handler=_handle_manage_files,
        safety="sensitive",
        confirm_condition=_confirm_manage_files,
        aliases=["files", "file", "move_file", "copy_file", "delete_file",
                 "zip", "unzip", "find_file", "organize"],
        arg_aliases={"file": "path", "source": "path", "src": "path",
                     "target": "destination", "dest": "destination", "to": "destination",
                     "operation": "action", "type": "action"},
        primary_arg="action",
        core=True,
        isolate=True,
    ))

    registry.register(ToolSpec(
        name="manage_software",
        description=(
            "Install, uninstall, update, or search for software using winget (Windows Package "
            "Manager). Use for: 'install VLC', 'uninstall Zoom', 'update all apps', "
            "'is Python installed', 'search for video editor'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["install", "uninstall", "update", "update_all",
                             "list", "search"],
                    "description": "What to do",
                },
                "name": {"type": "string",
                         "description": "Software name (e.g. 'VLC', 'Python', 'Discord'). Not needed for update_all or list."},
            },
            "required": ["action"]
        },
        handler=_handle_manage_software,
        safety="sensitive",
        confirm_condition=_confirm_manage_software,
        aliases=["install", "uninstall", "update_app", "winget",
                 "software", "package"],
        arg_aliases={"app": "name", "package": "name", "program": "name",
                     "software": "name", "operation": "action", "type": "action"},
        primary_arg="action",
        core=True,
        isolate=True,
    ))

    logger.info(f"Registered 3 system tools")
