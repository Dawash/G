"""
Tool handlers, JSON parsing, and legacy compatibility for the Brain.

Extracted from brain.py. Contains:
  - build_tool_definitions(): delegates to ToolRegistry (single source of truth)
  - Safety blocklists for terminal/file operations
  - _run_terminal, _manage_files, _manage_software: pure handler functions
  - Tool name resolution (delegates to registry)
  - JSON extraction from LLM text output
  - Prompt-based action parsing
  - _toggle_system_setting, media helpers
  - Tool verification data
  - _execute_create_file

NOTE: Tool schemas, aliases, and arg_aliases now live in ToolSpec registrations
(tools/*.py). This file provides backward-compatible exports that delegate to
the ToolRegistry at runtime. See tools/schemas.py and tools/registry.py.
"""

import json
import logging
import os
import re
import subprocess
import time

logger = logging.getLogger(__name__)


# ===================================================================
# Tool definitions — delegated to ToolRegistry (single source of truth)
# ===================================================================

def build_tool_definitions():
    """Build the tool/function definitions for the LLM (OpenAI format).

    Delegates to the ToolRegistry. All tool schemas are defined in ToolSpec
    registrations (tools/*.py), NOT duplicated here.
    """
    from tools.registry import get_default
    reg = get_default()
    if reg:
        return reg.build_llm_schemas()
    # Fallback: should never happen in normal operation
    logger.warning("build_tool_definitions called before registry initialized")
    return []


# ===================================================================
# REMOVED: ~760 lines of hardcoded tool JSON schemas.
# Tool schemas now live in ToolSpec registrations (tools/*.py).
# build_tool_definitions() delegates to ToolRegistry.build_llm_schemas().
# ===================================================================

# --- Terminal safety blocklists ---
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

# --- File management safety blocklist ---
_FILE_BLOCKED_DIRS = [
    "c:\\windows", "c:\\program files", "c:\\program files (x86)",
    "c:\\programdata", "c:\\$recycle.bin", "c:\\system volume information",
]


def _run_terminal(command, admin=False):
    """Execute a PowerShell command with safety checks."""
    cmd_lower = command.lower().strip()

    # Prevent "start <app>" from triggering app picker popups on any system.
    # Convert "start notepad" → "Start-Process notepad.exe" etc.
    # This is universal — works regardless of what apps are installed.
    import re as _re_mod
    _start_match = _re_mod.match(r'^start\s+(\w+)$', cmd_lower)
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
        import time as _time
        deadline = _time.monotonic() + 2.0  # Max 2 seconds
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
            if _time.monotonic() > deadline:
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
# Core tool names, aliases, resolution — all delegate to ToolRegistry
# ===================================================================

def _get_registry():
    """Get the default tool registry (lazy, avoids import-time issues)."""
    from tools.registry import get_default
    return get_default()


def _get_core_tool_names():
    """Get core tool names from registry."""
    reg = _get_registry()
    if reg:
        return set(reg.core_names())
    return set()


# Backward-compatible property: accessed as _CORE_TOOL_NAMES
# Uses a lazy wrapper that resolves from registry on first real use.
class _LazySet:
    """Set-like object that populates from a factory on first access."""
    def __init__(self, factory):
        self._factory = factory
        self._cache = None
    def _ensure(self):
        if self._cache is None:
            self._cache = self._factory()
    def __contains__(self, item):
        self._ensure()
        return item in self._cache
    def __iter__(self):
        self._ensure()
        return iter(self._cache)
    def __len__(self):
        self._ensure()
        return len(self._cache)
    def __repr__(self):
        self._ensure()
        return repr(self._cache)

_CORE_TOOL_NAMES = _LazySet(_get_core_tool_names)
_KNOWN_TOOL_NAMES = _LazySet(lambda: set(_get_registry().all_names()) if _get_registry() else set())


def _build_core_tools():
    """Return a reduced tool set for local models (tools marked core=True)."""
    reg = _get_registry()
    if reg:
        return reg.build_llm_schemas(core_only=True)
    return []


# Backward-compatible alias dict — not used by resolution anymore but kept
# for any external code that reads _TOOL_ALIASES directly.
_TOOL_ALIASES = {}  # Populated lazily below

def _ensure_legacy_aliases():
    """Build _TOOL_ALIASES from registry if not yet populated."""
    global _TOOL_ALIASES
    if _TOOL_ALIASES:
        return
    reg = _get_registry()
    if not reg:
        return
    for spec in reg.all_specs():
        for alias in spec.aliases:
            _TOOL_ALIASES[alias.lower()] = spec.name


def _resolve_tool_name(raw_name):
    """Resolve a possibly-abbreviated tool name to the real one.

    Delegates to ToolRegistry.resolve_name() for single-source-of-truth resolution.
    """
    if not raw_name or not isinstance(raw_name, str):
        return None
    reg = _get_registry()
    if reg:
        return reg.resolve_name(raw_name)
    # Fallback: should never happen in normal operation
    return None


# Backward-compatible dict — not used by normalization anymore
_ARG_ALIASES = {}  # Kept for external code that reads it directly


def _guess_primary_arg(tool_name):
    """Get the primary argument name for a tool when LLM uses positional args.

    Delegates to ToolRegistry.get_primary_arg().
    """
    reg = _get_registry()
    if reg:
        return reg.get_primary_arg(tool_name)
    return "name"


def _normalize_tool_args(tool_name, args):
    """Fix common argument name mismatches for a given tool.

    Delegates to ToolRegistry.normalize_args().
    """
    if not args:
        return args
    reg = _get_registry()
    if reg:
        return reg.normalize_args(tool_name, args)
    return args


def _tools_as_prompt_text():
    """Convert tool definitions to plain-text for embedding in system prompts.

    Delegates to ToolRegistry.to_prompt_text().
    """
    reg = _get_registry()
    if reg:
        return reg.to_prompt_text()
    return ""


# ===================================================================
# JSON extraction from LLM text output
# ===================================================================

def _extract_single_tool(data):
    """Extract (tool_name, tool_args) from a single JSON dict."""
    name_keys = ["function", "function_name", "functionName", "name",
                 "tool", "tool_name", "toolName", "code", "key", "action"]
    tool_name = None
    name_key_used = None

    for k in name_keys:
        val = data.get(k)
        resolved = _resolve_tool_name(val)
        if resolved:
            tool_name = resolved
            name_key_used = k
            break

    if not tool_name:
        return None

    arg_keys = ["parameters", "params", "args", "arguments", "input"]
    tool_args = {}
    for k in arg_keys:
        val = data.get(k)
        if isinstance(val, dict):
            tool_args = val
            break

    if not tool_args:
        skip = {name_key_used, "type", "description"}
        remaining = {k: v for k, v in data.items()
                     if k not in skip and not isinstance(v, dict)}
        if remaining:
            tool_args = remaining

    tool_args = _normalize_tool_args(tool_name, tool_args)
    return (tool_name, tool_args)


def _extract_tool_from_json(text):
    """Extract tool calls from raw JSON or Python function call syntax."""
    if not text:
        return []

    # Try Python function call syntax first
    func_match = re.search(
        r'(\w+)\s*\(\s*((?:\w+\s*=\s*[\'"][^\'"]*[\'"]\s*,?\s*)+)\)',
        text
    )
    if not func_match:
        func_match_lenient = re.search(r'(\w+)\s*\(([^)]*)\)', text)
        if func_match_lenient:
            fn_name = _resolve_tool_name(func_match_lenient.group(1))
            if fn_name:
                inner = func_match_lenient.group(2).strip()
                args = {}
                keys = [m.start() for m in re.finditer(r'\w+\s*=\s*[\'"]', inner)]
                for i, start in enumerate(keys):
                    end = keys[i + 1] if i + 1 < len(keys) else len(inner)
                    chunk = inner[start:end].rstrip(', ')
                    kv_m = re.match(r'(\w+)\s*=\s*[\'"](.*)[\'"]\s*$', chunk)
                    if kv_m:
                        args[kv_m.group(1)] = kv_m.group(2).strip()
                if not args and inner:
                    single_m = re.match(r"""^['"](.*)['"]\s*$""", inner)
                    if single_m:
                        primary_arg = _guess_primary_arg(fn_name)
                        args[primary_arg] = single_m.group(1).strip()
                if args:
                    args = _normalize_tool_args(fn_name, args)
                    return [(fn_name, args)]
    if func_match:
        fn_name = _resolve_tool_name(func_match.group(1))
        if fn_name:
            args_str = func_match.group(2)
            args = {}
            for kv in re.finditer(r"(\w+)\s*=\s*['\"]([^'\"]*)['\"]", args_str):
                args[kv.group(1)] = kv.group(2)
            if args:
                args = _normalize_tool_args(fn_name, args)
                return [(fn_name, args)]

    if '{' not in text:
        return []

    candidates = []
    seen_spans = set()

    for m in re.finditer(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL):
        span = (m.start(), m.end())
        seen_spans.add(span)
        candidates.append(m.group(1))

    for m in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text):
        overlaps = False
        for s_start, s_end in seen_spans:
            if m.start() >= s_start and m.end() <= s_end:
                overlaps = True
                break
        if not overlaps:
            candidates.append(m.group(0))

    for m in re.finditer(r'\[(\s*\{.*?\}\s*(?:,\s*\{.*?\}\s*)*)\]', text, re.DOTALL):
        try:
            arr = json.loads("[" + m.group(1) + "]")
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, dict):
                        candidates.append(json.dumps(item))
        except json.JSONDecodeError:
            pass

    # Brace-counting for deeply nested JSON
    i = 0
    while i < len(text):
        if text[i] == '{':
            depth = 0
            start = i
            in_string = False
            escape_next = False
            for j in range(i, len(text)):
                ch = text[j]
                if escape_next:
                    escape_next = False
                    continue
                if ch == '\\' and in_string:
                    escape_next = True
                    continue
                if ch == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if not in_string:
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            raw = text[start:j+1]
                            if raw not in candidates:
                                candidates.append(raw)
                            i = j
                            break
            else:
                pass
        i += 1

    results = []
    seen_tools = set()

    for raw in candidates:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                fixed = re.sub(r'[\x00-\x1f]', lambda m: f'\\u{ord(m.group()):04x}', raw)
                data = json.loads(fixed)
            except (json.JSONDecodeError, ValueError):
                continue
        if not isinstance(data, dict):
            continue

        for wrapper_key in ("actions", "functions", "tools", "tool_calls"):
            if wrapper_key in data and isinstance(data[wrapper_key], list):
                for item in data[wrapper_key]:
                    if isinstance(item, dict):
                        extracted = _extract_single_tool(item)
                        if extracted:
                            dedup_key = (extracted[0], json.dumps(extracted[1], sort_keys=True))
                            if dedup_key not in seen_tools:
                                seen_tools.add(dedup_key)
                                results.append(extracted)
                if results:
                    return results

        extracted = _extract_single_tool(data)
        if extracted:
            dedup_key = (extracted[0], json.dumps(extracted[1], sort_keys=True))
            if dedup_key not in seen_tools:
                seen_tools.add(dedup_key)
                results.append(extracted)

    return results


def _looks_like_json_garbage(text):
    """Detect if text is primarily JSON that we couldn't parse into valid tools."""
    if not text:
        return False
    stripped = text.strip()
    if stripped.startswith('{') or stripped.startswith('['):
        return True
    brace_count = stripped.count('{') + stripped.count('}')
    if brace_count >= 2 and ':' in stripped:
        json_chars = sum(stripped.count(c) for c in '{}[]:,"')
        if json_chars > len(stripped) * 0.3:
            return True
    return False


# ===================================================================
# Prompt-based action parsing
# ===================================================================

def _parse_prompt_actions(text):
    """Parse JSON action blocks from LLM text output.
    Returns (actions_list, spoken_response)."""
    json_match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if not json_match:
        json_match = re.search(r'(\{"actions"\s*:\s*\[.*?\]\s*\})', text, re.DOTALL)

    if not json_match:
        extracted = _extract_tool_from_json(text)
        if extracted:
            actions = [{"tool": name, "args": args} for name, args in extracted]
            cleaned = re.sub(r'```(?:json)?\s*\{.*?\}\s*```', '', text, flags=re.DOTALL)
            cleaned = re.sub(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', '', cleaned)
            spoken = cleaned.strip()
            return actions, spoken
        return [], text.strip()

    try:
        data = json.loads(json_match.group(1))
        actions = data.get("actions", [])
    except (json.JSONDecodeError, AttributeError):
        extracted = _extract_tool_from_json(text)
        if extracted:
            actions = [{"tool": name, "args": args} for name, args in extracted]
            cleaned = re.sub(r'```(?:json)?\s*\{.*?\}\s*```', '', text, flags=re.DOTALL)
            cleaned = re.sub(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', '', cleaned)
            return actions, cleaned.strip()
        return [], text.strip()

    spoken = text[json_match.end():].strip()
    if not spoken:
        spoken = text[:json_match.start()].strip()
    spoken = re.sub(r'```\w*\s*', '', spoken).strip()

    return actions, spoken


# ===================================================================
# Toggle system settings (Bluetooth, WiFi, etc.)
# ===================================================================

def _toggle_system_setting(setting, state):
    """Toggle a Windows system setting (Bluetooth, WiFi, etc.) via PowerShell."""
    setting = setting.lower().strip()
    turn_on = state in ("on", "enable", "true", "1")

    if "bluetooth" in setting:
        if turn_on:
            ps_cmd = (
                'Add-Type -AssemblyName System.Runtime.WindowsRuntime; '
                '$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | '
                'Where-Object { $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and '
                '$_.GetParameters()[0].ParameterType.Name -eq "IAsyncOperation`1" })[0]; '
                '$radio = [Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime]::GetRadiosAsync(); '
                '$asTaskGeneric = $asTask.MakeGenericMethod([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]]); '
                '$radios = $asTaskGeneric.Invoke($null, @($radio)); '
                '$radios.Wait(); '
                'foreach ($r in $radios.Result) { '
                'if ($r.Kind -eq "Bluetooth") { '
                '$setTask = $r.SetStateAsync("On"); '
                '$asTask2 = $asTask.MakeGenericMethod([Windows.Devices.Radios.RadioAccessStatus]); '
                '$asTask2.Invoke($null, @($setTask)).Wait() } }'
            )
        else:
            ps_cmd = (
                'Add-Type -AssemblyName System.Runtime.WindowsRuntime; '
                '$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | '
                'Where-Object { $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and '
                '$_.GetParameters()[0].ParameterType.Name -eq "IAsyncOperation`1" })[0]; '
                '$radio = [Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime]::GetRadiosAsync(); '
                '$asTaskGeneric = $asTask.MakeGenericMethod([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]]); '
                '$radios = $asTaskGeneric.Invoke($null, @($radio)); '
                '$radios.Wait(); '
                'foreach ($r in $radios.Result) { '
                'if ($r.Kind -eq "Bluetooth") { '
                '$setTask = $r.SetStateAsync("Off"); '
                '$asTask2 = $asTask.MakeGenericMethod([Windows.Devices.Radios.RadioAccessStatus]); '
                '$asTask2.Invoke($null, @($setTask)).Wait() } }'
            )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                return f"Bluetooth has been turned {'on' if turn_on else 'off'}."
            else:
                subprocess.Popen(["explorer", "ms-settings:bluetooth"])
                return f"Opened Bluetooth settings. Please toggle it manually."
        except Exception as e:
            logger.error(f"Bluetooth toggle failed: {e}")
            subprocess.Popen(["explorer", "ms-settings:bluetooth"])
            return f"Couldn't toggle Bluetooth automatically. Opened settings for you."

    elif "wifi" in setting or "wi-fi" in setting:
        action = "enable" if turn_on else "disable"
        try:
            result = subprocess.run(
                ["netsh", "interface", "set", "interface", "Wi-Fi", action],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                return f"WiFi has been turned {'on' if turn_on else 'off'}."
            logger.warning(f"WiFi netsh failed (needs admin): {result.stderr.strip()}")
            subprocess.Popen(["explorer", "ms-settings:network-wifi"])
            return f"Opened WiFi settings. Please toggle it manually (needs admin rights)."
        except Exception as e:
            logger.error(f"WiFi toggle failed: {e}")
            subprocess.Popen(["explorer", "ms-settings:network-wifi"])
            return f"Couldn't toggle WiFi automatically. Opened settings for you."

    elif "airplane" in setting or "flight" in setting:
        subprocess.Popen(["explorer", "ms-settings:network-airplanemode"])
        return "Opened Airplane Mode settings. Please toggle it manually."

    elif "night light" in setting or "nightlight" in setting:
        subprocess.Popen(["explorer", "ms-settings:nightlight"])
        state_word = "on" if turn_on else "off"
        return f"Opened Night Light settings to turn it {state_word}. Please toggle it."

    elif "dark mode" in setting or "darkmode" in setting:
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                0, winreg.KEY_SET_VALUE,
            )
            val = 0 if not turn_on else 1
            winreg.SetValueEx(key, "AppsUseLightTheme", 0, winreg.REG_DWORD, val)
            winreg.SetValueEx(key, "SystemUsesLightTheme", 0, winreg.REG_DWORD, val)
            winreg.CloseKey(key)
            return f"Dark mode has been turned {'on' if not turn_on else 'off'}."
        except Exception as e:
            return f"Dark mode toggle failed: {e}"

    else:
        subprocess.Popen(["explorer", "ms-settings:"])
        return f"I don't have a direct toggle for '{setting}'. Opened Windows Settings."


# ===================================================================
# Media key helpers
# ===================================================================

def _press_media_key(vk_code):
    """Send a media key press via Windows API."""
    import ctypes
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY, 0)
    ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)

VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_VOLUME_UP = 0xAF
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_MUTE = 0xAD


def _wait_for_process(name, timeout=5):
    """Wait until a process is running, up to timeout seconds."""
    for _ in range(timeout * 4):
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace"
        )
        if name.lower() in result.stdout.lower():
            return True
        time.sleep(0.25)
    return False


def _open_spotify_app():
    """Try multiple methods to open Spotify. Returns True if Spotify is running."""
    if _wait_for_process("Spotify.exe", timeout=1):
        logger.info("Spotify already running")
        return True

    try:
        os.startfile("spotify:")
        if _wait_for_process("Spotify.exe", timeout=6):
            logger.info("Spotify opened via URI protocol")
            return True
        logger.warning("spotify: URI executed but Spotify.exe not found in processes")
    except Exception as e:
        logger.warning(f"spotify: URI failed: {e}")

    try:
        from app_finder import launch_app
        result = launch_app("Spotify")
        logger.info(f"launch_app('Spotify') returned: {result}")
        if _wait_for_process("Spotify.exe", timeout=6):
            logger.info("Spotify opened via app_finder")
            return True
    except Exception as e:
        logger.warning(f"app_finder launch failed: {e}")

    common_paths = [
        os.path.expandvars(r"%APPDATA%\Spotify\Spotify.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\Spotify.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Spotify\Spotify.exe"),
    ]
    for path in common_paths:
        if os.path.isfile(path):
            try:
                subprocess.Popen([path])
                if _wait_for_process("Spotify.exe", timeout=6):
                    logger.info(f"Spotify opened from {path}")
                    return True
            except Exception as e:
                logger.warning(f"Failed to launch from {path}: {e}")

    logger.error("All methods to open Spotify failed")
    return False


# ===================================================================
# Tool verification data
# ===================================================================

_VERIFY_TOOLS = {"play_music", "search_in_app", "open_app", "google_search"}

_APP_VERIFY = {
    "spotify": ("Spotify.exe", "Spotify"),
    "chrome": ("chrome.exe", "Chrome"),
    "firefox": ("firefox.exe", "Firefox"),
    "edge": ("msedge.exe", "Edge"),
    "steam": ("steam.exe", "Steam"),
    "discord": ("Discord.exe", "Discord"),
    "notepad": ("notepad.exe", "Notepad"),
    "code": ("Code.exe", "Visual Studio Code"),
    "vs code": ("Code.exe", "Visual Studio Code"),
    "youtube": (None, "YouTube"),
    "google": (None, "Google"),
}


def _verify_tool_completion(tool_name, arguments, result, user_input=""):
    """Check if a tool action actually completed, not just started."""
    if tool_name not in _VERIFY_TOOLS:
        return True, [], []

    if tool_name == "play_music":
        action = arguments.get("action", "play")
        if action in ("pause", "next", "previous", "volume_up", "volume_down", "mute", "unmute"):
            return True, [], []
        result_lower = str(result).lower()
        # Only consider it fully complete if it says "Playing" (not "searched for" or "couldn't")
        if "playing" in result_lower and "couldn't" not in result_lower:
            return True, ["music playing"], []
        # Partial: searched but couldn't play
        if "couldn't auto-play" in result_lower or "click a result" in result_lower:
            return False, ["searched"], ["click play"]

    app = ""
    query = ""
    if tool_name == "play_music":
        app = arguments.get("app", "spotify").lower()
        query = arguments.get("query", "")
    elif tool_name == "search_in_app":
        app = arguments.get("app", "").lower()
        query = arguments.get("query", "")
    elif tool_name == "open_app":
        app = arguments.get("name", "").lower()
    elif tool_name == "google_search":
        query = arguments.get("query", "")
        app = "chrome"

    if not app and not query:
        return True, [], []

    what_done = []
    what_missing = []

    time.sleep(0.5)

    app_info = _APP_VERIFY.get(app)
    if app_info and app_info[0]:
        exe = app_info[0]
        try:
            proc = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/V", "/FO", "CSV"],
                          capture_output=True, text=True, timeout=10,
                          encoding="utf-8", errors="replace")
            if exe.lower() in proc.stdout.lower():
                what_done.append(f"{app} is running")

                if query:
                    import csv as _csv
                    import io as _io
                    q_lower = query.lower()
                    reader = _csv.reader(_io.StringIO(proc.stdout.strip()))
                    next(reader, None)
                    for row in reader:
                        if len(row) >= 9:
                            title = row[-1].strip().lower()
                            if q_lower in title:
                                what_done.append(f"'{query}' found in window title")
                                return True, what_done, []
                    what_missing.append(f"'{query}' not in {app} window title")
                else:
                    return True, what_done, []
            else:
                what_missing.append(f"{app} not running")
        except Exception as e:
            logger.debug(f"Verify process check failed: {e}")

    if query:
        try:
            import pygetwindow as gw
            titles = gw.getAllTitles()
            q_lower = query.lower()
            for title in titles:
                if title and q_lower in title.lower():
                    what_done.append(f"'{query}' found in window: {title[:50]}")
                    return True, what_done, []
        except Exception:
            pass

        if app_info and app_info[1]:
            try:
                import pygetwindow as gw
                kw = app_info[1].lower()
                for title in gw.getAllTitles():
                    if title and kw in title.lower():
                        what_done.append(f"{app} window open: {title[:50]}")
                        break
            except Exception:
                pass

        if not what_done:
            what_missing.append(f"no window with '{query}' found")

    if what_missing and not what_done:
        return False, what_done, what_missing
    if what_missing:
        return False, what_done, what_missing
    return True, what_done, []


# ===================================================================
# File creation helper
# ===================================================================

def _get_ollama_url():
    """Get the Ollama URL from config, with fallback to default."""
    try:
        from config import load_config, DEFAULT_OLLAMA_URL
        cfg = load_config()
        return cfg.get("ollama_url", DEFAULT_OLLAMA_URL).rstrip("/")
    except Exception:
        return "http://localhost:11434"


def _get_ollama_model():
    """Get the configured Ollama model name, with fallback to default."""
    try:
        from config import load_config, DEFAULT_OLLAMA_MODEL
        cfg = load_config()
        return cfg.get("ollama_model", DEFAULT_OLLAMA_MODEL)
    except Exception:
        return "qwen2.5:7b"


def _generate_file_content(prompt, max_tokens=2048, timeout=120):
    """Generate file content using Ollama with higher token limit than quick_chat."""
    try:
        import requests as _req
        ollama_url = _get_ollama_url()
        # Use Ollama native API (faster than OpenAI-compat for long outputs)
        resp = _req.post(
            f"{ollama_url}/api/generate",
            json={
                "model": _get_ollama_model(),
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": max_tokens, "temperature": 0.7},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception:
        return None


def _execute_create_file(path, content, quick_chat_fn=None, user_request=""):
    """Create a file on the user's computer.
    Defaults to ~/Desktop. Blocks path traversal and absolute paths.
    """
    if not path:
        return "Error: file path is required."

    # Expand environment variables (%USERPROFILE%, %APPDATA%, etc.)
    # LLMs often generate paths like %USERPROFILE%\Desktop\file.txt
    path = os.path.expandvars(path)

    # If content is empty, use LLM to generate real content
    if not content or len(content.strip()) < 10:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ("py", "html", "css", "js", "json", "md", "sh", "bat", "java", "cpp", "c", "ts"):
            if quick_chat_fn:
                try:
                    gen_prompt = (
                        f"Generate the complete content for a file called '{path}'. "
                        f"User's request: '{user_request}'. "
                        f"Output ONLY the file content, no explanations or markdown fences."
                    )
                    # Use direct Ollama/API call with higher token limit for code generation
                    generated = _generate_file_content(gen_prompt)
                    if not generated or len(generated.strip()) < 10:
                        generated = quick_chat_fn(gen_prompt)
                    if generated and len(generated.strip()) > 10:
                        generated = re.sub(r'^```\w*\n?', '', generated.strip())
                        generated = re.sub(r'\n?```$', '', generated.strip())
                        content = generated
                except Exception:
                    pass
        if not content or len(content.strip()) < 10:
            boilerplate = {
                "html": "<!DOCTYPE html>\n<html>\n<head><meta charset=\"UTF-8\"><title>Page</title></head>\n<body>\n  <h1>Hello World</h1>\n</body>\n</html>",
                "css": "body { margin: 0; font-family: sans-serif; }\n",
                "js": "console.log('Ready');\n",
                "py": "def main():\n    pass\n\nif __name__ == '__main__':\n    main()\n",
                "txt": "",
            }
            content = boilerplate.get(ext, "# New file\n")

    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    documents = os.path.join(os.path.expanduser("~"), "Documents")

    # Handle absolute paths that point to Desktop/Documents (e.g. from expandvars)
    norm_path = os.path.normpath(path)
    if os.path.isabs(norm_path):
        if norm_path.lower().startswith(os.path.normpath(desktop).lower()):
            path = os.path.relpath(norm_path, desktop)
        elif norm_path.lower().startswith(os.path.normpath(documents).lower()):
            path = os.path.relpath(norm_path, documents)
            desktop = documents
        else:
            return "Error: files can only be created under Desktop or Documents."
    elif ".." in path:
        return "Error: '..' is not allowed in file paths."

    path = path.replace("\\", "/")

    path_lower = path.lower()
    if path_lower.startswith("desktop/") or path_lower.startswith("desktop\\"):
        path = path[8:]
    elif path_lower.startswith("documents/") or path_lower.startswith("documents\\"):
        path = path[10:]
        desktop = documents

    full_path = os.path.normpath(os.path.join(desktop, path))

    if not (full_path.startswith(os.path.normpath(desktop)) or
            full_path.startswith(os.path.normpath(documents))):
        return "Error: files can only be created under Desktop or Documents."

    try:
        parent = os.path.dirname(full_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        try:
            ext_lower = os.path.splitext(full_path)[1].lower()
            if ext_lower in (".html", ".htm"):
                import webbrowser as _wb
                _wb.open(full_path)
            else:
                subprocess.Popen(["explorer", "/select,", full_path])
        except Exception:
            pass

        return f"Created file: {full_path}"
    except Exception as e:
        return f"Error creating file: {e}"
