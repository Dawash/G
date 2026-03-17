"""
System tool registrations — terminal, file management, software management.

Registers: run_terminal, manage_files, manage_software
Contains the actual implementations (moved from brain_defs.py).
"""

import logging
import os
import re
import subprocess
import time

from tools.schemas import ToolSpec
from tools.registry import ToolRegistry
from tools.safety_policy import _is_risky_command

logger = logging.getLogger(__name__)


# ===================================================================
# Safety blocklists (moved from brain_defs.py)
# ===================================================================

_TERMINAL_BLOCKED = [
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
    # Download-and-execute patterns (aliases + tools)
    "iwr ", "irm ", "iwr(", "irm(",  # PowerShell aliases for Invoke-WebRequest/RestMethod
    "| iex", "|iex", "invoke-expression",  # Download-and-execute pipeline
    "mshta ", "wscript ", "cscript ",  # Script hosts for remote execution
    "certutil -decode", "certutil -urlcache",  # Download/decode via certutil
    "regsvr32 /s", "regsvr32 /u",  # DLL registration for remote code
    "rundll32 ",  # DLL execution
    "bitsadmin /transfer",  # Background download
]

_TERMINAL_ADMIN_REQUIRED = [
    "restart-service", "stop-service", "start-service",
    "set-service", "new-service",
    "hosts", "drivers",
    "netsh advfirewall",
    "dism", "sfc /scannow",
    "chkdsk",
]

_FILE_BLOCKED_DIRS = [
    "c:\\windows", "c:\\program files", "c:\\program files (x86)",
    "c:\\programdata", "c:\\$recycle.bin", "c:\\system volume information",
]


# ===================================================================
# Implementation functions (moved from brain_defs.py)
# ===================================================================

def _run_terminal(command, admin=False):
    """Execute a PowerShell command with safety checks."""
    cmd_lower = command.lower().strip()

    # Prevent "start <app>" from triggering app picker popups on any system.
    # Convert "start notepad" -> "Start-Process notepad.exe" etc.
    _start_match = re.match(r'^start\s+(\w+)$', cmd_lower)
    if _start_match:
        app = _start_match.group(1)
        # Add .exe if no extension to prevent file association popup
        if '.' not in app:
            command = f"Start-Process '{app}.exe' -ErrorAction SilentlyContinue"

    for blocked in _TERMINAL_BLOCKED:
        if blocked in cmd_lower:
            return f"Blocked for safety: '{command}' contains '{blocked}'"

    for pattern in _TERMINAL_ADMIN_REQUIRED:
        if pattern in cmd_lower and not admin:
            admin = True

    # Adaptive timeout: long-running commands get more time
    _LONG_RUNNING = {"ping", "tracert", "nslookup", "npm", "pip", "cargo",
                     "dotnet", "gradle", "maven", "docker", "git clone",
                     "git pull", "git push", "chkdsk", "sfc", "dism"}
    timeout = 30
    for pattern in _LONG_RUNNING:
        if pattern in cmd_lower:
            timeout = 120
            break

    try:
        if admin:
            # Escape command for safe embedding in PowerShell ArgumentList
            safe_cmd = command.replace("'", "''").replace('"', '\\"')
            ps_cmd = f"Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -Command {safe_cmd}' -Wait"
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        else:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )

        output = result.stdout.strip() or result.stderr.strip()
        if not output:
            return "Command completed with no output."
        if len(output) > 2000:
            output = output[:2000] + "\n... (truncated)"
        return output
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout} seconds."
    except Exception as e:
        return f"Error running command: {e}"


def _find_locking_process(file_path):
    """Try to identify which process has a file locked.

    Returns a human-readable string like 'WINWORD.EXE (PID 1234)', or None.
    Uses a time budget to avoid blocking on systems with many processes.
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        deadline = time.monotonic() + 2.0  # Max 2 seconds
        file_path = os.path.normpath(os.path.abspath(file_path))
        file_lower = file_path.lower()
        # Only check user processes likely to hold document locks
        _LIKELY_LOCKERS = {
            "winword.exe", "excel.exe", "powerpnt.exe", "outlook.exe",
            "notepad.exe", "notepad++.exe", "code.exe", "devenv.exe",
            "explorer.exe", "chrome.exe", "msedge.exe", "firefox.exe",
            "acrobat.exe", "acrord32.exe", "vlc.exe", "python.exe",
            "node.exe", "java.exe", "sqlservr.exe",
        }
        for proc in psutil.process_iter(["name", "pid"]):
            if time.monotonic() > deadline:
                break
            pname = (proc.info.get("name") or "").lower()
            if pname not in _LIKELY_LOCKERS:
                continue
            try:
                for f in proc.open_files():
                    if f.path.lower() == file_lower:
                        return f"{proc.info['name']} (PID {proc.info['pid']})"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return None


def _manage_files(action, path, destination=None):
    """Manage files with safety checks."""
    import shutil
    import glob as glob_mod
    import zipfile

    home = os.path.expanduser("~")

    def resolve(p):
        if not p:
            return None
        p = p.strip()
        if os.path.isabs(p):
            return p
        return os.path.join(home, p)

    full_path = resolve(path)
    full_dest = resolve(destination) if destination else None

    for blocked in _FILE_BLOCKED_DIRS:
        if full_path and full_path.lower().startswith(blocked):
            return f"Blocked for safety: cannot modify files in {blocked}"
        if full_dest and full_dest.lower().startswith(blocked):
            return f"Blocked for safety: cannot modify files in {blocked}"

    try:
        if action == "list":
            if not os.path.isdir(full_path):
                return f"'{path}' is not a directory."
            entries = os.listdir(full_path)
            if not entries:
                return f"'{path}' is empty."
            lines = []
            for e in sorted(entries)[:50]:
                fp = os.path.join(full_path, e)
                kind = "[dir]" if os.path.isdir(fp) else "[file]"
                lines.append(f"  {kind} {e}")
            result = f"Contents of {path} ({len(entries)} items):\n" + "\n".join(lines)
            if len(entries) > 50:
                result += f"\n  ... and {len(entries) - 50} more"
            return result

        elif action == "find":
            pattern = full_path
            if not any(c in pattern for c in "*?["):
                pattern = os.path.join(home, "**", f"*{path}*")
            matches = glob_mod.glob(pattern, recursive=True)[:20]
            if not matches:
                return f"No files matching '{path}' found."
            lines = [os.path.relpath(m, home) for m in matches]
            return f"Found {len(matches)} match(es):\n  " + "\n  ".join(lines)

        elif action == "size":
            if os.path.isfile(full_path):
                size = os.path.getsize(full_path)
            elif os.path.isdir(full_path):
                size = sum(
                    os.path.getsize(os.path.join(dp, f))
                    for dp, _, files in os.walk(full_path) for f in files
                )
            else:
                return f"'{path}' not found."
            for unit in ["B", "KB", "MB", "GB"]:
                if size < 1024:
                    return f"Size of {path}: {size:.1f} {unit}"
                size /= 1024
            return f"Size of {path}: {size:.1f} TB"

        elif action == "move":
            if not full_dest:
                return "Destination required for move."
            if os.path.isdir(full_dest):
                full_dest = os.path.join(full_dest, os.path.basename(full_path))
            shutil.move(full_path, full_dest)
            return f"Moved {path} to {destination}."

        elif action == "copy":
            if not full_dest:
                return "Destination required for copy."
            if os.path.isdir(full_path):
                shutil.copytree(full_path, full_dest)
            else:
                if os.path.isdir(full_dest):
                    full_dest = os.path.join(full_dest, os.path.basename(full_path))
                shutil.copy2(full_path, full_dest)
            return f"Copied {path} to {destination}."

        elif action == "rename":
            if not full_dest:
                return "New name required for rename."
            new_path = os.path.join(os.path.dirname(full_path), full_dest if not os.path.isabs(destination) else os.path.basename(full_dest))
            os.rename(full_path, new_path)
            return f"Renamed {path} to {os.path.basename(new_path)}."

        elif action == "delete":
            if os.path.isdir(full_path):
                shutil.rmtree(full_path)
            elif os.path.isfile(full_path):
                os.remove(full_path)
            else:
                return f"'{path}' not found."
            # Verify deletion with event-driven wait
            try:
                from automation.event_waiter import wait_for_file_gone
                result = wait_for_file_gone(full_path, max_wait=5, interval=0.2)
                if not result["gone"]:
                    return f"Delete issued for {path}, but it may still exist (locked?)."
            except ImportError:
                pass
            return f"Deleted {path}."

        elif action == "zip":
            if not full_dest:
                full_dest = full_path + ".zip"
            with zipfile.ZipFile(full_dest, 'w', zipfile.ZIP_DEFLATED) as zf:
                if os.path.isdir(full_path):
                    for dp, _, files in os.walk(full_path):
                        for f in files:
                            fp = os.path.join(dp, f)
                            zf.write(fp, os.path.relpath(fp, os.path.dirname(full_path)))
                else:
                    zf.write(full_path, os.path.basename(full_path))
            return f"Created zip: {destination or path + '.zip'}."

        elif action == "unzip":
            if not full_dest:
                full_dest = os.path.splitext(full_path)[0]
            with zipfile.ZipFile(full_path, 'r') as zf:
                zf.extractall(full_dest)
            return f"Extracted {path} to {destination or os.path.splitext(path)[0]}."

        elif action == "organize":
            if not os.path.isdir(full_path):
                return f"'{path}' is not a directory."
            moved = 0
            import shutil as _shutil
            for f in os.listdir(full_path):
                fp = os.path.join(full_path, f)
                if not os.path.isfile(fp):
                    continue
                ext = os.path.splitext(f)[1].lower().lstrip(".")
                if not ext:
                    continue
                ext_dir = os.path.join(full_path, ext.upper())
                os.makedirs(ext_dir, exist_ok=True)
                _shutil.move(fp, os.path.join(ext_dir, f))
                moved += 1
            return f"Organized {moved} files in {path} by extension."

        elif action == "create":
            # LLM sometimes sends manage_files(action="create") for file creation
            # Redirect to create_file logic
            content = destination or ""  # LLM might put content in destination
            if full_path:
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(content)
                # Verify creation with event-driven wait
                try:
                    from automation.event_waiter import wait_for_file
                    wait_for_file(full_path, max_wait=3, interval=0.2)
                except ImportError:
                    pass
                return f"Created {path}"
            return "No file path specified."

        else:
            return f"Unknown file action: {action}"

    except FileNotFoundError:
        return f"File not found: {path}"
    except PermissionError:
        # Try to identify which process has the file locked
        locking_info = _find_locking_process(full_path)
        if locking_info:
            return f"Permission denied: {path} — locked by {locking_info}. Close it first."
        return f"Permission denied: {path}. The file may be open in another program."
    except Exception as e:
        return f"File operation failed: {e}"


def _manage_software(action, name=None):
    """Manage software via winget."""
    try:
        ver_result = subprocess.run(["winget", "--version"], capture_output=True,
                                     text=True, timeout=5,
                                     encoding="utf-8", errors="replace")
        logger.debug(f"winget version: {ver_result.stdout.strip()}")
    except FileNotFoundError:
        return ("winget is not installed. To install it:\n"
                "1. Open Microsoft Store\n"
                "2. Search for 'App Installer'\n"
                "3. Install or update it\n"
                "Or download from: https://aka.ms/getwinget")
    except subprocess.TimeoutExpired:
        return "winget is installed but not responding. Try restarting your terminal."

    timeout = 120 if action in ("install", "update", "update_all") else 30

    try:
        if action == "search":
            if not name:
                return "Please specify what software to search for."
            result = subprocess.run(
                ["winget", "search", "--query", name, "--source", "winget"],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        elif action == "install":
            if not name:
                return "Please specify what software to install."
            result = subprocess.run(
                ["winget", "install", "-e", "--query", name,
                 "--source", "winget",
                 "--accept-package-agreements", "--accept-source-agreements"],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        elif action == "uninstall":
            if not name:
                return "Please specify what software to uninstall."
            result = subprocess.run(
                ["winget", "uninstall", "--query", name],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        elif action == "update":
            if not name:
                return "Please specify what software to update."
            result = subprocess.run(
                ["winget", "upgrade", "--query", name],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        elif action == "update_all":
            result = subprocess.run(
                ["winget", "upgrade", "--all"],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        elif action == "list":
            cmd = ["winget", "list"]
            if name:
                cmd += ["--query", name]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        else:
            return f"Unknown software action: {action}"

        output = result.stdout.strip() or result.stderr.strip()
        if result.returncode != 0 and not output:
            return f"Software {action} failed (exit code {result.returncode})."
        if not output:
            return f"Software {action} completed successfully."
        if len(output) > 2000:
            output = output[:2000] + "\n... (truncated)"
        return output

    except subprocess.TimeoutExpired:
        return f"Software {action} timed out after {timeout} seconds."
    except Exception as e:
        return f"Software {action} failed: {e}"


# ===================================================================
# Handler wrappers (call local implementations)
# ===================================================================

def _handle_run_terminal(arguments):
    return _run_terminal(
        arguments.get("command", ""),
        arguments.get("admin", False),
    )


def _handle_manage_files(arguments):
    return _manage_files(
        arguments.get("action", "list"),
        arguments.get("path", ""),
        arguments.get("destination"),
    )


def _handle_manage_software(arguments):
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
