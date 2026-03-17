"""
Tool resolution, JSON parsing, media helpers, and legacy compatibility for the Brain.

Contains:
  - build_tool_definitions(): delegates to ToolRegistry (single source of truth)
  - Tool name resolution (delegates to registry)
  - JSON extraction from LLM text output
  - Prompt-based action parsing
  - Media key helpers (_press_media_key, Spotify helpers)
  - Tool verification data
  - Backward-compatible re-exports of implementations moved to tools/

NOTE: Tool handler implementations have been moved to their respective tools/ files:
  - _run_terminal, _manage_files, _manage_software -> tools/system_tools.py
  - _toggle_system_setting -> tools/action_tools.py
  - _execute_create_file -> tools/desktop_tools.py
"""

import json
import logging
import os
import re
import subprocess
import time

logger = logging.getLogger(__name__)


# ===================================================================
# Backward-compatible re-exports — implementations moved to tools/
# ===================================================================

# System tools (moved to tools/system_tools.py)
from tools.system_tools import (
    _TERMINAL_BLOCKED, _TERMINAL_ADMIN_REQUIRED, _FILE_BLOCKED_DIRS,
    _run_terminal, _manage_files, _manage_software,
)

# Action tools (moved to tools/action_tools.py)
from tools.action_tools import _toggle_system_setting

# Desktop tools (moved to tools/desktop_tools.py)
from tools.desktop_tools import _execute_create_file


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
