"""
Deterministic fast-path router for high-frequency commands.

Sits between Layer 1 (meta-commands) and Layer 2 (Brain/LLM) in the
assistant loop. Catches obvious single-tool commands via regex and
routes them directly to handlers without an LLM round-trip.

3-stage matching (merged from intent_parser):
  1. Exact match — O(1) hash lookup for common phrases
  2. Pattern match — regex with confidence scoring
  3. Typo correction — retry stages 1+2 with fixed text

Design principles:
  - Only match HIGH-CONFIDENCE patterns where intent is unambiguous
  - Never match questions, multi-step tasks, or anything conversational
  - Short spoken confirmations ("Opening Chrome.", "Reminder set.")
  - Fall through to Brain for anything ambiguous
"""

import logging
import re
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# TTL cache for system queries — avoids re-running PowerShell for identical info
_system_cache = {}  # key → (timestamp, formatted_result)
_SYSTEM_CACHE_TTL = {
    "run_terminal_disk": 30,       # Disk space changes slowly
    "run_terminal_ip": 60,         # IP rarely changes
    "run_terminal_sysinfo": 300,   # System info is static
    "run_terminal_battery": 15,    # Battery changes slowly
    "run_terminal_cpu": 3,         # CPU changes fast, short cache
    "run_terminal_ram": 5,         # RAM changes moderately
    "run_terminal_ports": 10,      # Ports change occasionally
    "run_terminal_processes": 5,   # Processes change moderately
    "run_terminal_ping": 0,        # Never cache ping (real-time)
}


# ===================================================================
# Conversation context — pronoun resolution + multi-step splitting
# ===================================================================

# Tracks the last entity used in a command (e.g., "Chrome" from "open Chrome")
# so "close it" / "minimize that" can resolve the pronoun.
_last_entity = {"name": None, "type": None}  # type: app|query|url

_PRONOUNS = {"it", "this", "that", "the app", "the application", "the program"}

_MULTI_STEP_SPLIT = re.compile(
    r'\s+(?:and(?:\s+also)?|then|also|after that)\s+', re.I)


def _resolve_pronoun(name):
    """Resolve 'it'/'that' to last used entity. Returns resolved name or None."""
    if name.lower() in _PRONOUNS and _last_entity["name"]:
        logger.info(f"Pronoun '{name}' -> '{_last_entity['name']}'")
        return _last_entity["name"]
    return None


def _track_entity(handler_key, arguments):
    """Record the entity from a successful command for pronoun resolution."""
    if handler_key in ("open_app", "close_app", "minimize_app", "focus_window"):
        name = arguments.get("name")
        if name and name.lower() not in _PRONOUNS:
            _last_entity["name"] = name
            _last_entity["type"] = "app"
    elif handler_key in ("play_music", "google_search"):
        query = arguments.get("query")
        if query:
            _last_entity["name"] = query
            _last_entity["type"] = "query"


def split_multi_step(text):
    """Split 'open Chrome and play music' into ['open Chrome', 'play music'].

    Only splits on unambiguous conjunctions. Returns list of 1+ parts.
    Won't split when 'and' is part of a search/play query.
    """
    # Don't split search queries — "and" is part of the query content
    if re.match(r'^(?:search for|google|look up|play|listen to|put on)\s+', text, re.I):
        return [text]

    parts = _MULTI_STEP_SPLIT.split(text)
    # Only return split if each part looks like a command (>2 chars, starts with verb-like word)
    if len(parts) > 1:
        _VERBS = {"open", "close", "minimize", "play", "pause", "search", "check",
                   "show", "list", "set", "get", "what", "how", "tell", "switch",
                   "focus", "snap", "maximize", "launch", "start", "run", "stop",
                   "skip", "next", "remind", "go", "navigate", "take", "ping"}
        # Strip residual conjunction words from the beginning of parts
        _STRIP_LEAD = re.compile(r'^(?:then|also|and)\s+', re.I)
        valid = []
        for p in parts:
            p = _STRIP_LEAD.sub('', p.strip()).strip()
            if len(p) > 2:
                valid.append(p)
        if len(valid) > 1:
            # Each part must start with a verb-like word to be a separate command
            for v in valid:
                first_word = v.split()[0].lower() if v.split() else ""
                if first_word not in _VERBS:
                    return [text]  # Non-command fragment → treat as single
            return valid
    return [text]


# ===================================================================
# Result formatting — convert raw output to spoken-friendly text
# ===================================================================

def _format_system_result(handler_key, raw_output):
    """Convert raw PowerShell table output to natural language for speech."""
    if not raw_output or len(raw_output) < 5:
        return raw_output

    try:
        if handler_key == "run_terminal_disk":
            # Parse: "Name Used(GB) Free(GB)\n---- ...\nC  344.9  119.4\nD  279.8  197.0"
            lines = [l.strip() for l in raw_output.strip().split('\n') if l.strip() and not l.startswith('-')]
            if len(lines) < 2:
                return raw_output
            parts = []
            for line in lines[1:]:  # Skip header
                cols = line.split()
                if len(cols) >= 3:
                    parts.append(f"Drive {cols[0]}: {cols[2]} GB free, {cols[1]} GB used")
            return ". ".join(parts) + "." if parts else raw_output

        elif handler_key == "run_terminal_cpu":
            return raw_output.strip()  # Already formatted: "CPU Usage: 4.5%"

        elif handler_key == "run_terminal_battery":
            return raw_output.strip()  # Already formatted: "Battery: 100% (Charging)"

        elif handler_key == "run_terminal_ip":
            lines = [l.strip() for l in raw_output.strip().split('\n') if l.strip() and not l.startswith('-')]
            if len(lines) < 2:
                return raw_output
            parts = []
            for line in lines[1:]:  # Skip header
                ip_match = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', line)
                if ip_match:
                    ip = ip_match.group(1)
                    alias = line[:ip_match.start()].strip()
                    # Filter out self-assigned (APIPA) and loopback addresses
                    if alias and not ip.startswith("169.254.") and ip != "127.0.0.1":
                        parts.append(f"{alias}: {ip}")
            if not parts:
                return "No active network connections found."
            return "Your IP addresses: " + ", ".join(parts) + "."

        elif handler_key == "run_terminal_processes":
            lines = [l.strip() for l in raw_output.strip().split('\n') if l.strip() and not l.startswith('-')]
            if len(lines) < 2:
                return "No matching processes found."
            count = len(lines) - 1  # Subtract header
            # Collect unique names with instance count
            from collections import Counter
            proc_names = []
            for line in lines[1:]:
                cols = line.split()
                if cols:
                    proc_names.append(cols[0])
            name_counts = Counter(proc_names)
            # Format as "chrome (9 instances), python (2 instances), notepad"
            unique = []
            for name, cnt in name_counts.most_common(5):
                unique.append(f"{name} ({cnt})" if cnt > 1 else name)
            if count <= 5:
                return f"Found {count} processes: {', '.join(unique)}."
            return f"Found {count} processes. Top ones: {', '.join(unique)}."

        elif handler_key == "run_terminal_sysinfo":
            return raw_output.strip()  # Already formatted multi-line

        elif handler_key == "run_terminal_ports":
            lines = [l.strip() for l in raw_output.strip().split('\n') if l.strip() and not l.startswith('-')]
            if len(lines) < 2:
                return "No listening ports found."
            ports = set()
            for line in lines[1:]:
                cols = line.split()
                if cols and cols[0].isdigit():
                    ports.add(cols[0])
            if not ports:
                return raw_output
            sorted_ports = sorted(ports, key=int)
            return f"{len(sorted_ports)} unique ports listening: {', '.join(sorted_ports[:10])}."

        elif handler_key == "run_terminal_ram":
            lines = [l.strip() for l in raw_output.strip().split('\n') if l.strip() and not l.startswith('-')]
            if len(lines) < 2:
                return raw_output
            parts = []
            for line in lines[1:min(6, len(lines))]:
                cols = line.split()
                if len(cols) >= 2:
                    parts.append(f"{cols[0]} ({cols[-1]} MB)")
            return "Top processes by memory: " + ", ".join(parts) + "." if parts else raw_output

    except Exception:
        pass  # Fall through to raw output

    return raw_output


# ===================================================================
# Typo correction (merged from intent_parser)
# ===================================================================

_TYPO_MAP = {
    "wether": "weather", "wheather": "weather", "whats": "what's",
    "opne": "open", "lanuch": "launch", "serach": "search",
    "seach": "search", "gogle": "google", "googel": "google",
    "plya": "play", "paly": "play", "remdiner": "reminder",
    "remidner": "reminder", "clsoe": "close", "closee": "close",
    "minimze": "minimize", "swtich": "switch", "swithc": "switch",
}


def _fix_typos(text):
    """Apply common typo corrections."""
    words = text.split()
    return " ".join(_TYPO_MAP.get(w.lower(), w) for w in words)


def _strip_polite_prefix(text):
    """Strip polite prefixes/suffixes: 'can you open X' → 'open X', 'open X for me' → 'open X'.

    Applied iteratively to handle nested forms like 'hey can you open X please'.
    """
    _PREFIX = re.compile(
        r'^(?:can you |could you |would you |please |hey |hey g |g |go ahead and )',
        re.I)
    for _ in range(3):  # Max 3 passes for nested prefixes
        stripped = _PREFIX.sub('', text).strip()
        if stripped == text:
            break
        text = stripped
    # Strip trailing politeness: "for me", "please"
    text = re.sub(r'\s+(?:for me|please)$', '', text, flags=re.I).strip()
    return text


# ===================================================================
# Exact match table (merged from intent_parser — O(1) hash lookup)
# ===================================================================

# Maps lowercase phrase → (handler_key, args_dict)
_EXACT_MATCHES = {
    # Time
    "time": ("time", {}),
    "the time": ("time", {}),
    "date": ("time", {}),
    "the date": ("time", {}),
    "what time is it": ("time", {}),
    "what's the time": ("time", {}),
    "what is the time": ("time", {}),
    "tell me the time": ("time", {}),
    "tell me the time please": ("time", {}),
    "tell me the current time": ("time", {}),
    "what's the current time": ("time", {}),
    "what is the current time": ("time", {}),
    "check the time": ("time", {}),
    "check time": ("time", {}),
    "get the time": ("time", {}),
    "what's the date": ("time", {}),
    "what is the date": ("time", {}),
    "what day is it": ("time", {}),
    "what's today": ("time", {}),
    # Weather
    "what's the weather": ("weather", {}),
    "what is the weather": ("weather", {}),
    "how's the weather": ("weather", {}),
    "how is the weather": ("weather", {}),
    "weather": ("weather", {}),
    "weather today": ("weather", {}),
    "find out what the weather is like today": ("weather", {}),
    "find out what the weather is like": ("weather", {}),
    "what's the weather like today": ("weather", {}),
    "what is the weather like today": ("weather", {}),
    "what's the weather like": ("weather", {}),
    "show me the weather": ("weather", {}),
    "check the weather": ("weather", {}),
    "get the weather": ("weather", {}),
    "how's the weather today": ("weather", {}),
    "is it raining": ("weather", {}),
    "is it cold": ("weather", {}),
    "is it hot": ("weather", {}),
    "what's the temperature": ("weather", {}),
    "what is the temperature": ("weather", {}),
    "what's the current temperature": ("weather", {}),
    "what is the current temperature": ("weather", {}),
    "current temperature": ("weather", {}),
    "temperature": ("weather", {}),
    # Forecast
    "forecast": ("forecast", {}),
    "weather forecast": ("forecast", {}),
    "what's the forecast": ("forecast", {}),
    "what is the forecast": ("forecast", {}),
    "what's the weather forecast": ("forecast", {}),
    "what is the weather forecast": ("forecast", {}),
    "will it rain": ("forecast", {}),
    "will it rain today": ("forecast", {}),
    "will it rain tomorrow": ("forecast", {}),
    "forecast today": ("forecast", {}),
    "forecast tomorrow": ("forecast", {}),
    "how's the forecast": ("forecast", {}),
    "how is the forecast": ("forecast", {}),
    "how's the forecast today": ("forecast", {}),
    "how is the forecast today": ("forecast", {}),
    # News
    "what's the news": ("news", {}),
    "tell me the news": ("news", {}),
    "news": ("news", {}),
    "latest news": ("news", {}),
    "today's news": ("news", {}),
    "news headlines": ("news", {}),
    "tell me the latest news": ("news", {}),
    # Reminders
    "my reminders": ("list_reminders", {}),
    "list reminders": ("list_reminders", {}),
    "show reminders": ("list_reminders", {}),
    "what reminders do i have": ("list_reminders", {}),
    "delete all reminders": ("clear_reminders", {}),
    "clear all reminders": ("clear_reminders", {}),
    "remove all reminders": ("clear_reminders", {}),
    "cancel all reminders": ("clear_reminders", {}),
    # Alarms
    "my alarms": ("manage_alarm", {"action": "list"}),
    "list alarms": ("manage_alarm", {"action": "list"}),
    "show alarms": ("manage_alarm", {"action": "list"}),
    "list my alarms": ("manage_alarm", {"action": "list"}),
    "what alarms do i have": ("manage_alarm", {"action": "list"}),
    # Music controls
    "pause": ("pause_music", {"action": "pause"}),
    "pause music": ("pause_music", {"action": "pause"}),
    "resume music": ("pause_music", {"action": "play"}),
    "stop music": ("pause_music", {"action": "pause"}),
    "next song": ("next_track", {"action": "next"}),
    "next track": ("next_track", {"action": "next"}),
    "skip song": ("next_track", {"action": "next"}),
    "previous song": ("next_track", {"action": "previous"}),
    # System queries
    "disk space": ("run_terminal_disk", {}),
    "how much disk space": ("run_terminal_disk", {}),
    "how much disk space do i have": ("run_terminal_disk", {}),
    "how much storage do i have": ("run_terminal_disk", {}),
    "how much ram do i have": ("run_terminal_ram", {}),
    "how much memory do i have": ("run_terminal_ram", {}),
    "cpu usage": ("run_terminal_cpu", {}),
    "battery": ("run_terminal_battery", {}),
    "battery level": ("run_terminal_battery", {}),
    "what's my ip": ("run_terminal_ip", {}),
    "what is my ip": ("run_terminal_ip", {}),
    "what is my ip address": ("run_terminal_ip", {}),
    "my ip address": ("run_terminal_ip", {}),
    "list processes": ("run_terminal_processes", {}),
    "show processes": ("run_terminal_processes", {}),
    "system info": ("run_terminal_sysinfo", {}),
    "system information": ("run_terminal_sysinfo", {}),
    "computer info": ("run_terminal_sysinfo", {}),
    "computer specs": ("run_terminal_sysinfo", {}),
    "show system info": ("run_terminal_sysinfo", {}),
    "show system information": ("run_terminal_sysinfo", {}),
    "show computer info": ("run_terminal_sysinfo", {}),
    "what ports are open": ("run_terminal_ports", {}),
    "what ports are listening": ("run_terminal_ports", {}),
    "listening ports": ("run_terminal_ports", {}),
    "show listening ports": ("run_terminal_ports", {}),
    "check ports": ("run_terminal_ports", {}),
    # System
    "screenshot": ("screenshot", {}),
    "take a screenshot": ("screenshot", {}),
    "take screenshot": ("screenshot", {}),
    # Window management
    "list windows": ("list_windows", {}),
    "show windows": ("list_windows", {}),
    "what windows are open": ("list_windows", {}),
    "what's open": ("list_windows", {}),
    # Calendar
    "what's on my calendar": ("calendar_today", {}),
    "what is on my calendar": ("calendar_today", {}),
    "my calendar": ("calendar_today", {}),
    "my schedule": ("calendar_today", {}),
    "my agenda": ("calendar_today", {}),
    "today's events": ("calendar_today", {}),
    "any meetings today": ("calendar_today", {}),
    "any events today": ("calendar_today", {}),
    "do i have any meetings today": ("calendar_today", {}),
    "do i have any events today": ("calendar_today", {}),
    "what's my schedule": ("calendar_today", {}),
    "what is my schedule": ("calendar_today", {}),
    "what's my schedule today": ("calendar_today", {}),
    "upcoming events": ("calendar_upcoming", {}),
    "what's coming up": ("calendar_upcoming", {}),
    "what is coming up": ("calendar_upcoming", {}),
    "events this week": ("calendar_upcoming", {}),
    "my upcoming events": ("calendar_upcoming", {}),
    "my weekly schedule": ("calendar_upcoming", {}),
}


# ===================================================================
# Handler → tool name mapping
# ===================================================================

_HANDLER_TO_TOOL = {
    "open_app": "open_app",
    "close_app": "close_app",
    "minimize_app": "minimize_app",
    "time": "get_time",
    "weather": "get_weather",
    "forecast": "get_forecast",
    "set_reminder": "set_reminder",
    "list_reminders": "list_reminders",
    "clear_reminders": "list_reminders",
    "play_music": "play_music",
    "pause_music": "play_music",
    "next_track": "play_music",
    "google_search": "google_search",
    "focus_window": "focus_window",
    "snap_window": "snap_window",
    "snap_maximize": "snap_window",
    "list_windows": "list_windows",
    "news": "get_news",
    "screenshot": "take_screenshot",
    "browser_navigate": "browser_action",
    "browser_new_tab": "browser_action",
    "browser_close_tab": "browser_action",
    "browser_back": "browser_action",
    "calendar_today": "get_calendar",
    "calendar_upcoming": "get_calendar",
    # System queries → run_terminal
    "run_terminal_disk": "run_terminal",
    "run_terminal_ram": "run_terminal",
    "run_terminal_cpu": "run_terminal",
    "run_terminal_battery": "run_terminal",
    "run_terminal_ip": "run_terminal",
    "run_terminal_processes": "run_terminal",
    "run_terminal_ping": "run_terminal",
    "run_terminal_sysinfo": "run_terminal",
    "run_terminal_ports": "run_terminal",
}


# ===================================================================
# Argument extractors — return structured dicts, never strings
# ===================================================================

def _clean_app(text):
    """Clean app name from match group."""
    for filler in ("please", "the", "app", "application", "for me"):
        text = re.sub(rf'\b{filler}\b', '', text, flags=re.I).strip()
    return text


def _app_args(m):
    return {"name": _clean_app(m.group("app").strip())}


def _query_args(m):
    return {"query": m.group("query").strip()}


def _url_args(m):
    return {"url": m.group("query").strip()}


def _reminder_args(m):
    msg = m.group("msg").strip()
    t = m.group("time")
    return {"message": msg, "time": (t.strip() if t else "in 1 hour")}


def _reminder_time_first_args(m):
    """Extract args from 'remind me for/at 5pm to eat dinner' (time-first ordering)."""
    msg = m.group("msg").strip()
    t = m.group("time").strip()
    return {"message": msg, "time": t}


def _music_args(m):
    raw = m.group("query").strip()
    raw = re.sub(r'^(some|a|the|my|me)\s+', '', raw, flags=re.I).strip()
    return {"query": raw or "popular hits", "action": "play"}


def _snap_args(m):
    return {"name": _clean_app(m.group("app").strip()),
            "position": m.group("pos").strip()}


def _maximize_args(m):
    return {"name": _clean_app(m.group("app").strip()),
            "position": "maximize"}


# ===================================================================
# Pattern definitions — ordered by specificity
# ===================================================================

# Each entry: (compiled_regex, handler_key, args_extractor_fn_or_None)
_FAST_PATTERNS = [
    # --- Open app (very high frequency) ---
    # Reject multi-step: "open X and do Y" falls through to Brain
    (re.compile(
        r"^(?:open|launch|start|run)\s+(?P<app>(?:(?!\b(?:and|then|also|after)\b).)+?)(?:\s+for me)?$",
        re.I),
     "open_app", _app_args),

    # --- Browser tab (must come BEFORE close_app to avoid "close tab" → close_app) ---
    (re.compile(r"^close (?:this |current )?tab$", re.I),
     "browser_close_tab", None),

    # --- Close / minimize app ---
    (re.compile(
        r"^(?:close|quit|kill|exit)\s+(?P<app>.+?)(?:\s+for me)?$",
        re.I),
     "close_app", _app_args),

    (re.compile(
        r"^minimize\s+(?P<app>.+?)(?:\s+for me)?$",
        re.I),
     "minimize_app", _app_args),

    # --- Time ---
    (re.compile(
        r"^(?:what(?:'s| is) the (?:current )?time|what time is it|"
        r"tell me the (?:current )?time|"
        r"what(?:'s| is) the (?:current )?date|what(?:'s| is) today)[\?\.]?$",
        re.I),
     "time", None),

    # --- Weather ---
    (re.compile(
        r"^(?:what(?:'s| is) the (?:current )?(?:weather|temperature)|"
        r"how(?:'s| is) the weather(?: today)?|"
        r"what(?:'s| is) the weather like(?: today)?|"
        r"is it (?:raining|cold|hot|warm|sunny|cloudy)|"
        r"(?:current )?temperature|"
        r"find out what the weather is like(?: today)?|"
        r"weather(?: today| now| outside)?)[\?\.]?$",
        re.I),
     "weather", None),

    # --- Forecast ---
    (re.compile(
        r"^(?:(?:what(?:'s| is) the )?(?:weather )?forecast"
        r"(?: for (?:today|tomorrow|this week))?|"
        r"will it rain(?: today| tomorrow)?|"
        r"forecast(?: for)? (?:today|tomorrow|this week))[\?\.]?$",
        re.I),
     "forecast", None),

    # --- Alarms ---
    # "set alarm for 7am", "wake me up at 6:30", "set morning alarm for 7am"
    (re.compile(
        r"^(?:set|create|add)\s+(?:an?\s+)?(?:morning\s+)?alarm\s+"
        r"(?:for|at)\s+(?P<time>.+?)(?:\s+(?P<recurrence>daily|every day|weekdays|weekends))?$",
        re.I),
     "manage_alarm", lambda m: {"action": "add", "time": m.group("time").strip(),
                                 "type": "morning", "label": "Morning alarm",
                                 "recurrence": m.group("recurrence") or "daily"}),
    (re.compile(
        r"^(?:wake me up|wake me)\s+(?:at|for)\s+(?P<time>.+)$",
        re.I),
     "manage_alarm", lambda m: {"action": "add", "time": m.group("time").strip(),
                                 "type": "morning", "label": "Morning wake up",
                                 "recurrence": "daily"}),
    # "cancel/remove/delete alarm" — needs LLM to pick which one
    (re.compile(
        r"^(?:cancel|remove|delete|turn off)\s+(?:my\s+)?(?:all\s+)?alarms?$",
        re.I),
     "manage_alarm", lambda m: {"action": "list"}),  # Show list so user can pick

    # --- Timers (mapped to reminders) ---
    (re.compile(
        r"^set (?:a )?timer (?:for |of )?(?P<time>\d+\s*(?:minutes?|mins?|hours?|hrs?|seconds?|secs?))$",
        re.I),
     "set_reminder", lambda m: {"message": "timer", "time": "in " + m.group("time").strip()}),

    # --- Reminders ---
    # Time-first: "set a reminder for 5pm to eat dinner" / "remind me at 3pm to call John"
    (re.compile(
        r"^(?:remind me|set (?:a )?reminder)\s+"
        r"(?:for|at|in|on|by)\s+(?P<time>.+?)\s+to\s+(?P<msg>.+)$",
        re.I),
     "set_reminder", _reminder_time_first_args),

    # Message-first: "remind me to eat dinner at 5pm" / "set a reminder to call John in 30 minutes"
    (re.compile(
        r"^(?:remind me(?: to)?|set (?:a )?reminder(?: to)?)\s+"
        r"(?P<msg>.+?)(?:\s+(?:at|in|on|every|by|for)\s+(?P<time>.+))?$",
        re.I),
     "set_reminder", _reminder_args),

    (re.compile(
        r"^(?:(?:list|show|what are)(?: my)? reminders|my reminders|"
        r"what reminders do i have)[\?\.]?$",
        re.I),
     "list_reminders", None),

    # --- Music ---
    (re.compile(
        r"^(?:play|listen to|put on)\s+(?P<query>.+?)$",
        re.I),
     "play_music", _music_args),

    (re.compile(
        r"^(?:pause|stop|resume) (?:the )?music$",
        re.I),
     "pause_music", lambda m: {"action": "pause"}),

    (re.compile(
        r"^(?:next|skip)(?: song| track)?$",
        re.I),
     "next_track", lambda m: {"action": "next"}),

    # --- Google search ---
    (re.compile(
        r"^(?:search for|google|look up|search)\s+(?P<query>.+?)$",
        re.I),
     "google_search", _query_args),

    # --- Browser navigation (before focus_window — URL is more specific) ---
    (re.compile(
        r"^(?:go to|navigate to|open)\s+(?P<query>(?:https?://|www\.)\S+)$",
        re.I),
     "browser_navigate", _url_args),

    # --- System queries (deterministic PowerShell routing) ---
    # Must come BEFORE focus_window to avoid "show processes" → focus_window
    (re.compile(
        r"^(?:how much|check|show|what'?s?|what is)(?: my| the)? (?:disk|drive|storage)(?: space| usage| left)?(?:\s+\w+)*[\?\.]?$",
        re.I),
     "run_terminal_disk", None),

    (re.compile(
        r"^(?:how much|check|show|what'?s?|what is)(?: my| the)? (?:ram|memory)(?: usage| used| left| available)?(?:\s+\w+)*[\?\.]?$",
        re.I),
     "run_terminal_ram", None),

    (re.compile(
        r"^(?:how much|check|show|what'?s?|what is)(?: my| the)? (?:cpu|processor)(?: usage| load)?(?:\s+\w+)*[\?\.]?$",
        re.I),
     "run_terminal_cpu", None),

    (re.compile(
        r"^(?:how much|check|show|what'?s?|what is)(?: my| the)? battery(?: level| left| remaining| life| percentage)?(?:\s+\w+)*[\?\.]?$",
        re.I),
     "run_terminal_battery", None),

    (re.compile(
        r"^(?:what'?s?|what is|show|check)(?: my| the)? (?:ip|ip address|ipaddress)[\?\.]?$",
        re.I),
     "run_terminal_ip", None),

    (re.compile(
        r"^(?:list|show|what are|what)(?: all)?(?: the)?(?: running)? (?:processes|tasks)(?: (?:are )?running)?(?:\s+(?:with|named|called|containing|like|that have|using)\s+(?P<query>.+))?[\?\.]?$",
        re.I),
     "run_terminal_processes", lambda m: {"query": (m.group("query") or "").strip()}),

    (re.compile(
        r"^what (?:processes|tasks|programs|apps) are (?:currently )?running[\?\.]?$",
        re.I),
     "run_terminal_processes", lambda m: {"query": ""}),

    (re.compile(
        r"^(?:list|show|what are)(?: all)?(?: the)?(?: running)? (?:programs|apps|applications)[\?\.]?$",
        re.I),
     "run_terminal_processes", lambda m: {"query": ""}),

    (re.compile(
        r"^ping\s+(?P<query>\S+)$",
        re.I),
     "run_terminal_ping", lambda m: {"query": m.group("query").strip()}),

    (re.compile(
        r"^(?:(?:system|computer) (?:info|information|specs|details)|"
        r"(?:show|get|check)(?: my| the)? (?:system|computer) (?:info|information|specs|details))[\?\.]?$",
        re.I),
     "run_terminal_sysinfo", None),

    (re.compile(
        r"^(?:what |show |list |check )?(?:(?:open|listening) )?ports(?:\s+(?:are )?(?:open|listening))?[\?\.]?$",
        re.I),
     "run_terminal_ports", None),

    # --- Focus / switch window ---
    (re.compile(
        r"^(?:switch to|go to|focus|show|bring up|activate)\s+(?P<app>.+?)$",
        re.I),
     "focus_window", _app_args),

    # --- Snap window ---
    (re.compile(
        r"^(?:snap|dock|put|move)\s+(?P<app>.+?)\s+(?:to the |to )?(?P<pos>left|right|center)$",
        re.I),
     "snap_window", _snap_args),

    (re.compile(
        r"^maximize\s+(?P<app>.+?)$",
        re.I),
     "snap_maximize", _maximize_args),

    # --- List windows ---
    (re.compile(
        r"^(?:(?:list|show|what are)(?: the| my)? (?:open )?windows|"
        r"what(?:'s| is) open|what windows are open)[\?\.]?$",
        re.I),
     "list_windows", None),

    # --- Browser tab management ---
    (re.compile(r"^(?:new|open (?:a )?new) tab$", re.I),
     "browser_new_tab", None),

    (re.compile(r"^go back$", re.I),
     "browser_back", None),

    # --- Calendar ---
    (re.compile(
        r"^(?:what(?:'s| is) on my (?:calendar|schedule)|"
        r"my (?:calendar|schedule|agenda)(?: for today)?|"
        r"(?:today(?:'s|s)? |any )?(?:events|meetings|appointments)(?: today)?|"
        r"do i have any (?:events|meetings|appointments)(?: today)?|"
        r"what(?:'s| is) my schedule(?: for today)?)[\?\.]?$",
        re.I),
     "calendar_today", None),

    (re.compile(
        r"^(?:(?:what(?:'s| is) )?(?:coming up|upcoming)(?: (?:this|next) week)?|"
        r"my (?:upcoming|weekly) (?:calendar|schedule|events|agenda)|"
        r"what(?:'s| is) on my (?:calendar|schedule) this week|"
        r"events (?:this|next) week)[\?\.]?$",
        re.I),
     "calendar_upcoming", None),
]

# Guard patterns with penalties — reduce confidence instead of hard-blocking.
# Polite phrasing like "can you open Chrome" passes with lower confidence
# while complex multi-step requests get penalized heavily.
_COMPLEXITY_GUARDS = [
    (re.compile(r'\b(?:and then|then|after that|and also)\b', re.I), 0.20),
    (re.compile(r'\b(?:if|when|unless|while|because|since)\b', re.I), 0.20),
    (re.compile(r'\b(?:how (?:do|can|should|would|could|to)|why|explain|compare|difference|should i)\b', re.I), 0.20),
    (re.compile(r'\b(?:what do you think|can you help|tell me about)\b', re.I), 0.20),
]


# ===================================================================
# Match-only function (returns RouteDecision)
# ===================================================================

def _match_patterns(text, guard_penalty):
    """Try regex patterns against text. Returns (handler_key, args, confidence) or None."""
    for pattern, handler_key, extractor in _FAST_PATTERNS:
        m = pattern.match(text)
        if not m:
            continue

        arguments = extractor(m) if extractor else {}

        # Resolve pronoun entities ("close it" → "close Chrome")
        if handler_key in ("open_app", "close_app", "minimize_app", "focus_window"):
            name = arguments.get("name", "").lower()
            if name in _PRONOUNS:
                resolved = _resolve_pronoun(name)
                if resolved:
                    arguments["name"] = resolved
                else:
                    continue  # No context to resolve — fall through to Brain

        confidence = max(0.98 - guard_penalty, 0.0)
        return handler_key, arguments, confidence

    return None


def match_fast_path(user_input):
    """3-stage match: exact → pattern → typo+retry. Returns RouteDecision or None.

    Stage 1: Exact match — O(1) hash lookup (conf=0.99)
    Stage 2: Pattern match — regex with guard penalties (conf=0.98 - penalty)
    Stage 3: Typo correction — retry stages 1+2 with fixed text (conf=0.90)
    """
    from orchestration.route_decision import RouteDecision

    text = user_input.strip()
    if not text or len(text) < 2:
        return None

    lower = text.lower().rstrip("?!.")

    def _build(handler_key, arguments, confidence):
        tool_name = _HANDLER_TO_TOOL.get(handler_key, handler_key)
        return RouteDecision(
            source="fast_path",
            tool_name=tool_name,
            args=arguments,
            confidence=confidence,
            specificity=8,
            should_execute=(confidence >= 0.80),
            reason=f"fast_path:{handler_key}",
            mode="quick",
            handler_key=handler_key,
        )

    # --- Stage 1: Exact match (O(1), highest confidence) ---
    if lower in _EXACT_MATCHES:
        handler_key, arguments = _EXACT_MATCHES[lower]
        return _build(handler_key, dict(arguments), 0.99)

    # Compute guard penalty for pattern stages
    guard_penalty = 0.0
    for guard, penalty in _COMPLEXITY_GUARDS:
        if guard.search(lower):
            guard_penalty += penalty

    # Heavy guard penalty → skip pattern matching entirely
    if guard_penalty >= 0.30:
        return None

    # --- Stage 2: Pattern match (regex) ---
    result = _match_patterns(text, guard_penalty)
    if result:
        return _build(*result)

    # --- Stage 3: Polite prefix strip + retry ---
    stripped = _strip_polite_prefix(text)
    if stripped != text and len(stripped) > 2:
        stripped_lower = stripped.lower().rstrip("?!.")

        if stripped_lower in _EXACT_MATCHES:
            handler_key, arguments = _EXACT_MATCHES[stripped_lower]
            return _build(handler_key, dict(arguments), 0.95)

        result = _match_patterns(stripped, guard_penalty)
        if result:
            return _build(*result)

    # --- Stage 4: Typo correction + retry ---
    fixed = _fix_typos(text)
    if fixed != text:
        fixed_lower = fixed.lower().rstrip("?!.")

        # Retry exact match
        if fixed_lower in _EXACT_MATCHES:
            handler_key, arguments = _EXACT_MATCHES[fixed_lower]
            return _build(handler_key, dict(arguments), 0.90)

        # Retry patterns (lower confidence for typo-corrected)
        result = _match_patterns(fixed, guard_penalty + 0.08)
        if result:
            return _build(*result)

    return None


# ===================================================================
# Structured execution handlers
# ===================================================================

def execute_handler(handler_key, arguments, action_registry, reminder_mgr):
    """Execute a fast-path action with structured arguments.

    Args:
        handler_key: The handler to invoke (e.g. "open_app", "weather").
        arguments: Dict of structured arguments (e.g. {"name": "Chrome"}).
        action_registry: Dict of intent -> handler function.
        reminder_mgr: ReminderManager instance.

    Returns:
        Response string, or None on failure (fall through to Brain).
    """
    # Track entity for pronoun resolution in future commands
    _track_entity(handler_key, arguments)

    try:
        if handler_key == "open_app":
            name = arguments.get("name")
            if not name:
                return None
            fn = action_registry.get("open_app") if action_registry else None
            if not fn:
                return None
            result = fn(name)
            if result and "not found" in str(result).lower():
                return None  # Let Brain handle app-not-found (suggests alternatives)
            if result and "error" in str(result).lower():
                return result
            # Verify the app actually opened
            try:
                from tools.outcome import verify_app_opened
                opened, evidence = verify_app_opened(name, timeout=3)
                if opened:
                    return f"Opening {name}."
                else:
                    logger.warning(f"open_app verify failed for '{name}': {evidence}")
                    return f"Launched {name}, but couldn't confirm it opened."
            except ImportError:
                return f"Opening {name}."

        elif handler_key == "close_app":
            name = arguments.get("name")
            fn = action_registry.get("close_app") if action_registry else None
            if not fn or not name:
                return None
            fn(name)
            # Verify the app actually closed
            try:
                from tools.outcome import verify_app_closed
                closed, evidence = verify_app_closed(name, timeout=2)
                if closed:
                    return f"Closed {name}."
                else:
                    logger.warning(f"close_app verify failed for '{name}': {evidence}")
                    return f"Tried to close {name}, but it may still be running."
            except ImportError:
                return f"Closing {name}."

        elif handler_key == "minimize_app":
            name = arguments.get("name")
            fn = action_registry.get("minimize_app") if action_registry else None
            if not fn or not name:
                return None
            fn(name)
            return f"Minimizing {name}."

        elif handler_key == "time":
            now = datetime.now()
            return f"It's {now.strftime('%A, %I:%M %p')}."

        elif handler_key == "weather":
            from weather import get_current_weather
            return get_current_weather()

        elif handler_key == "forecast":
            from weather import get_forecast
            return get_forecast()

        elif handler_key == "set_reminder":
            if not reminder_mgr:
                return None
            message = arguments.get("message")
            time_str = arguments.get("time", "in 1 hour")
            if not message:
                return None
            return reminder_mgr.add_reminder(message, time_str)

        elif handler_key == "list_reminders":
            if not reminder_mgr:
                return None
            return reminder_mgr.list_active()

        elif handler_key == "clear_reminders":
            if not reminder_mgr:
                return None
            return reminder_mgr.clear_all()

        elif handler_key == "play_music":
            action = arguments.get("action", "play")
            query = arguments.get("query")
            app = arguments.get("app", "spotify")
            try:
                from platform_impl.windows.media import play_music
                result = play_music(action, query, app)
                return result if result else (f"Trying to play {query}." if query else "Done.")
            except ImportError:
                return f"Music playback is not available."
            except Exception as e:
                return f"Couldn't play music: {e}"

        elif handler_key == "pause_music":
            try:
                from platform_impl.windows.media import play_music
                return play_music("pause", "", "spotify")
            except ImportError:
                return None

        elif handler_key == "next_track":
            try:
                from platform_impl.windows.media import play_music
                return play_music("next", "", "spotify")
            except ImportError:
                return None

        elif handler_key == "manage_alarm":
            from alarms import get_alarm_manager
            am = get_alarm_manager()
            if not am:
                return None
            action = arguments.get("action", "list")
            if action in ("add", "set"):
                time_str = arguments.get("time", "")
                if not time_str:
                    return None
                label = arguments.get("label", "Alarm")
                alarm_type = arguments.get("type", "morning")
                recurrence = arguments.get("recurrence")
                return am.add_alarm(time_str, alarm_type=alarm_type,
                                    label=label, recurrence=recurrence)
            elif action == "list":
                return am.list_alarms()
            return None

        elif handler_key == "news":
            from news import get_briefing
            return get_briefing()

        elif handler_key == "screenshot":
            try:
                from vision import take_screenshot
                path = take_screenshot()
                return f"Screenshot saved to {path}." if path else "Screenshot taken."
            except Exception:
                return None

        elif handler_key == "google_search":
            query = arguments.get("query")
            fn = action_registry.get("google_search") if action_registry else None
            if not fn or not query:
                return None
            fn(query)
            return f"Searching for {query}."

        elif handler_key == "focus_window":
            name = arguments.get("name")
            if not name:
                return None
            try:
                from automation.ui_control import focus_window
                result = focus_window(name)
                if "not found" in result.lower():
                    return None  # Fall through to Brain (might need open_app)
                return f"Switching to {name}."
            except Exception:
                return None

        elif handler_key in ("snap_window", "snap_maximize"):
            name = arguments.get("name")
            position = arguments.get("position", "left")
            if not name:
                return None
            try:
                from automation.window_manager import snap_window
                return snap_window(name, position)
            except Exception:
                return None

        elif handler_key == "list_windows":
            try:
                from automation.window_manager import list_windows
                windows = list_windows()
                if not windows:
                    return "No windows are open."
                names = [w["title"][:50] for w in windows[:8]]
                return f"Open windows: {', '.join(names)}."
            except Exception:
                return None

        elif handler_key == "browser_navigate":
            url = arguments.get("url")
            if not url:
                return None
            try:
                from automation.browser_driver import browser_navigate
                return browser_navigate(url)
            except Exception:
                return None

        elif handler_key == "browser_new_tab":
            try:
                from automation.browser_driver import browser_new_tab
                return browser_new_tab()
            except Exception:
                return None

        elif handler_key == "browser_close_tab":
            try:
                from automation.browser_driver import browser_close_tab
                return browser_close_tab()
            except Exception:
                return None

        elif handler_key == "browser_back":
            try:
                from automation.browser_driver import browser_back
                return browser_back()
            except Exception:
                return None

        # --- System queries (deterministic PowerShell commands) ---
        elif handler_key.startswith("run_terminal_"):
            try:
                from brain_defs import _run_terminal
            except ImportError:
                return None

            # TTL cache for system queries (avoid re-running PowerShell for identical info)
            _now = time.time()
            _cache_key = handler_key + ":" + str(arguments.get("query", ""))
            _cached = _system_cache.get(_cache_key)
            _ttl = _SYSTEM_CACHE_TTL.get(handler_key, 0)
            if _cached and _ttl > 0 and (_now - _cached[0]) < _ttl:
                logger.info(f"System cache hit: {handler_key}")
                return _cached[1]

            _SYSTEM_COMMANDS = {
                "run_terminal_disk": "Get-PSDrive -PSProvider FileSystem | Select-Object Name,@{N='Used(GB)';E={[math]::Round($_.Used/1GB,1)}},@{N='Free(GB)';E={[math]::Round($_.Free/1GB,1)}} | Format-Table -AutoSize",
                "run_terminal_ram": "Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 10 Name,@{N='MB';E={[math]::Round($_.WorkingSet64/1MB)}} | Format-Table -AutoSize",
                "run_terminal_cpu": "Get-Counter '\\Processor(_Total)\\% Processor Time' -SampleInterval 1 -MaxSamples 1 | ForEach-Object { $_.CounterSamples | ForEach-Object { 'CPU Usage: ' + [math]::Round($_.CookedValue,1).ToString() + '%' } }",
                "run_terminal_battery": "(Get-WmiObject Win32_Battery | Select-Object EstimatedChargeRemaining,BatteryStatus) | ForEach-Object { 'Battery: ' + $_.EstimatedChargeRemaining.ToString() + '%' + $(if($_.BatteryStatus -eq 2){' (Charging)'} else {' (On battery)'}) }",
                "run_terminal_ip": "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notlike 'Loopback*' -and $_.IPAddress -ne '127.0.0.1' } | Select-Object InterfaceAlias,IPAddress | Format-Table -AutoSize",
                "run_terminal_sysinfo": "Get-ComputerInfo | Select-Object CsName,OsName,OsVersion,CsProcessors,CsTotalPhysicalMemory | ForEach-Object { 'Computer: ' + $_.CsName + \"`nOS: \" + $_.OsName + \"`nVersion: \" + $_.OsVersion + \"`nRAM: \" + [math]::Round($_.CsTotalPhysicalMemory/1GB,1).ToString() + ' GB' }",
                "run_terminal_ports": "Get-NetTCPConnection -State Listen | Select-Object LocalPort,OwningProcess | Sort-Object LocalPort | Select-Object -First 20 | Format-Table -AutoSize",
            }

            if handler_key == "run_terminal_processes":
                query = arguments.get("query", "")
                # Clean up natural language suffixes: "python in the name" → "python"
                if query:
                    query = re.sub(r'\s+(?:in the name|in their name|in the title|in name)$', '', query, flags=re.I).strip()
                if query:
                    cmd = f"Get-Process | Where-Object {{ $_.ProcessName -like '*{query}*' }} | Select-Object Name,Id,@{{N='MB';E={{[math]::Round($_.WorkingSet64/1MB)}}}} | Format-Table -AutoSize"
                else:
                    cmd = "Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 15 Name,Id,@{N='MB';E={[math]::Round($_.WorkingSet64/1MB)}} | Format-Table -AutoSize"
            elif handler_key == "run_terminal_ping":
                target = arguments.get("query", "google.com")
                cmd = f"ping {target} -n 4"
            else:
                cmd = _SYSTEM_COMMANDS.get(handler_key)

            if not cmd:
                return None
            result = _run_terminal(cmd)
            if not result:
                return "Command completed."
            formatted = _format_system_result(handler_key, result)
            # Store in cache for TTL
            if _ttl > 0:
                _system_cache[_cache_key] = (time.time(), formatted)
            return formatted

        elif handler_key == "calendar_today":
            try:
                from calendar_local import get_today_events, format_events
                events = get_today_events()
                return format_events(events, "Today's events")
            except Exception:
                return None

        elif handler_key == "calendar_upcoming":
            try:
                from calendar_local import get_upcoming, format_events
                events = get_upcoming(7)
                return format_events(events, "Events in the next 7 days")
            except Exception:
                return None

        return None

    except Exception as e:
        logger.error(f"Handler execution error ({handler_key}): {e}")
        return None


# ===================================================================
# Legacy wrapper (thin shim over route + execute_route)
# ===================================================================

class FastPathResult:
    """Result of fast-path routing."""
    __slots__ = ("handled", "response", "handler_key", "entity")

    def __init__(self, handled, response=None, handler_key=None, entity=None):
        self.handled = handled
        self.response = response
        self.handler_key = handler_key
        self.entity = entity


def try_fast_path(user_input, action_registry, reminder_mgr=None):
    """Match + execute directly (no route_decision overhead).

    Handles multi-step commands: "open Chrome and play music" → two actions.
    Returns FastPathResult for backward compatibility.
    """
    # Try multi-step split first
    steps = split_multi_step(user_input)
    if len(steps) > 1:
        responses = []
        for step in steps:
            decision = match_fast_path(step)
            if not decision or not decision.is_deterministic:
                # One step can't be routed → fall through to Brain for entire input
                return FastPathResult(False)
            r = execute_handler(decision.handler_key, decision.args,
                                action_registry, reminder_mgr)
            if r is None:
                return FastPathResult(False)
            responses.append(r)
        combined = " ".join(responses)
        return FastPathResult(True, combined, "multi_step", "")

    # Single command
    decision = match_fast_path(user_input)
    if not decision or not decision.is_deterministic:
        return FastPathResult(False)

    result = execute_handler(decision.handler_key, decision.args,
                             action_registry, reminder_mgr)
    if result is not None:
        entity = (decision.args.get("name") or decision.args.get("query")
                  or decision.args.get("url") or "")
        return FastPathResult(True, result, decision.handler_key, entity)

    return FastPathResult(False)
