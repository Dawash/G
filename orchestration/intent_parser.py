"""
Intent parser — 3-stage deterministic intent extraction with slot filling.

Phase 19: Replaces simple regex matching with a structured parser that:
  1. Exact match (known commands, greetings, meta-commands)
  2. Normalize + slot extract (entity extraction with confidence)
  3. Fuzzy fallback (typo tolerance, partial matches)

Goal: handle 60%+ of requests deterministically without LLM classification.
Works alongside fast_path.py (which handles execution) — this module focuses
on intent classification and entity extraction.
"""

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ===================================================================
# Intent types and slots
# ===================================================================

@dataclass
class ParsedIntent:
    """Result of intent parsing."""
    intent: str             # Intent name (e.g. "open_app", "weather", "greeting")
    confidence: float       # 0.0 - 1.0
    slots: dict = field(default_factory=dict)  # Extracted entities
    raw_text: str = ""      # Original input
    method: str = "none"    # "exact", "pattern", "fuzzy", "none"

    @property
    def is_actionable(self):
        """Whether this intent maps to a tool call."""
        return self.intent not in ("greeting", "farewell", "thanks",
                                    "chat", "unknown", "meta_command")

    @property
    def tool_name(self):
        """Map intent to tool name (if applicable)."""
        return _INTENT_TO_TOOL.get(self.intent, "")


# Intent → tool mapping
_INTENT_TO_TOOL = {
    "open_app": "open_app",
    "close_app": "close_app",
    "minimize_app": "minimize_app",
    "focus_window": "focus_window",
    "snap_window": "snap_window",
    "weather": "get_weather",
    "forecast": "get_forecast",
    "time": "get_time",
    "news": "get_news",
    "search": "google_search",
    "reminder_set": "set_reminder",
    "reminder_list": "list_reminders",
    "play_music": "play_music",
    "pause_music": "play_music",
    "next_track": "play_music",
    "toggle_setting": "toggle_setting",
    "system_command": "system_command",
    "terminal": "run_terminal",
    "install_software": "manage_software",
    "file_operation": "manage_files",
    "screenshot": "take_screenshot",
    "browser_navigate": "browser_action",
    "browser_search": "google_search",
    "memory_control": "memory_control",
    "workflow": "run_workflow",
}


# ===================================================================
# Stage 1: Exact match patterns
# ===================================================================

# Commands that need zero parsing — exact or near-exact
_EXACT_INTENTS = {
    # Time
    "what time is it": ("time", {}),
    "what's the time": ("time", {}),
    "what is the time": ("time", {}),
    "tell me the time": ("time", {}),
    "tell me the current time": ("time", {}),
    "what's the current time": ("time", {}),
    "what is the current time": ("time", {}),
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
    "what's the weather forecast for today": ("forecast", {}),
    "what's the weather forecast for tomorrow": ("forecast", {}),
    "what is the weather forecast for tomorrow": ("forecast", {}),
    "weather forecast for today": ("forecast", {}),
    "weather forecast for tomorrow": ("forecast", {}),
    "will it rain": ("forecast", {}),
    "will it rain today": ("forecast", {}),
    "will it rain tomorrow": ("forecast", {}),
    "find out if it will rain": ("forecast", {}),
    "find out if it's going to rain": ("forecast", {}),
    "forecast today": ("forecast", {}),
    "forecast tomorrow": ("forecast", {}),
    # News
    "what's the news": ("news", {}),
    "tell me the news": ("news", {}),
    "news": ("news", {}),
    "latest news": ("news", {}),
    "today's news": ("news", {}),
    "today's news headlines": ("news", {}),
    "news headlines": ("news", {}),
    "tell me about today's news": ("news", {}),
    "tell me about today's news headlines": ("news", {}),
    "tell me about the news": ("news", {}),
    "tell me the latest news": ("news", {}),
    "what are the news headlines": ("news", {}),
    "what are today's headlines": ("news", {}),
    # Reminders
    "my reminders": ("reminder_list", {}),
    "list reminders": ("reminder_list", {}),
    "show reminders": ("reminder_list", {}),
    "what reminders do i have": ("reminder_list", {}),
    # Music controls
    "pause": ("pause_music", {"action": "pause"}),
    "pause music": ("pause_music", {"action": "pause"}),
    "resume music": ("pause_music", {"action": "play"}),
    "stop music": ("pause_music", {"action": "pause"}),
    "next song": ("next_track", {"action": "next"}),
    "next track": ("next_track", {"action": "next"}),
    "skip": ("next_track", {"action": "next"}),
    "skip song": ("next_track", {"action": "next"}),
    "previous song": ("next_track", {"action": "previous"}),
    # System
    "screenshot": ("screenshot", {}),
    "take a screenshot": ("screenshot", {}),
    "take screenshot": ("screenshot", {}),
    # Window management
    "list windows": ("list_windows", {}),
    "show windows": ("list_windows", {}),
    "what windows are open": ("list_windows", {}),
    "what's open": ("list_windows", {}),
    "minimize all": ("minimize_all", {}),
    "show desktop": ("minimize_all", {}),
    # Greetings
    "hello": ("greeting", {}),
    "hi": ("greeting", {}),
    "hey": ("greeting", {}),
    "good morning": ("greeting", {}),
    "good afternoon": ("greeting", {}),
    "good evening": ("greeting", {}),
    "how are you": ("greeting", {}),
    # Thanks
    "thanks": ("thanks", {}),
    "thank you": ("thanks", {}),
    "thank you so much": ("thanks", {}),
    # Farewell
    "goodbye": ("farewell", {}),
    "bye": ("farewell", {}),
    "good night": ("farewell", {}),
    "see you": ("farewell", {}),
    "see you later": ("farewell", {}),
}


# ===================================================================
# Stage 2: Pattern-based slot extraction (scored matching)
# ===================================================================

# Each entry: (compiled_regex, intent_name, slot_extractor_fn, specificity)
# specificity: higher = more specific pattern, used as tiebreaker.
#   10 = highly specific (URL pattern, exact system command, terminal tool name)
#    7 = specific (two+ extracted slots, constrained entity)
#    5 = moderate (single entity extraction)
#    3 = broad (generic verb + catch-all entity)
_SLOT_PATTERNS = [
    # --- Navigate URL (very specific — matches URLs only) ---
    (re.compile(r"^(?:go to|navigate to|open)\s+(?P<url>(?:https?://|www\.)\S+)$", re.I),
     "browser_navigate", lambda m: {"url": m.group("url").strip()}, 10),

    # --- System commands (very specific — closed set of verbs) ---
    (re.compile(r"^(?:shutdown|restart|sleep|lock)(?: the)?(?: computer| pc| system)?$", re.I),
     "system_command", lambda m: {"action": m.group(0).split()[0].lower()}, 10),

    # --- Terminal: specific tool names ---
    (re.compile(r"^(?:ping|tracert|nslookup|whoami|hostname)\s*(?P<target>.*)$", re.I),
     "terminal", lambda m: {"command": m.group(0).strip()}, 10),

    # --- Snap window (specific — requires app + position) ---
    (re.compile(r"^(?:snap|dock|put|move)\s+(?P<app>.+?)\s+(?:to the\s+|to\s+)?(?P<pos>left|right|center|top|bottom)$", re.I),
     "snap_window", lambda m: {"name": _clean_entity(m.group("app")),
                                "position": m.group("pos").lower()}, 8),

    (re.compile(r"^maximize\s+(?P<app>.+?)$", re.I),
     "snap_window", lambda m: {"name": _clean_entity(m.group("app")),
                                "position": "maximize"}, 7),

    # --- Weather with city (specific — "weather in/at/for X") ---
    (re.compile(r"^(?:what(?:'s| is) the )?weather (?:in|at|for)\s+(?P<city>.+?)[\?\.]*$", re.I),
     "weather", lambda m: {"city": m.group("city").strip()}, 8),

    # --- News with category (specific — category + "news") ---
    (re.compile(r"^(?:give me|tell me|show me|what(?:'s| is))\s+(?:the\s+)?(?P<cat>tech|sports?|business|science|health|entertainment)\s+news$", re.I),
     "news", lambda m: {"category": m.group("cat").strip()}, 8),

    # --- Set reminder (specific — "remind me" + message + optional time) ---
    (re.compile(r"^(?:remind me(?: to)?|set (?:a )?reminder(?: to)?)\s+(?P<msg>.+?)(?:\s+(?:at|in|on|every|by)\s+(?P<time>.+))?$", re.I),
     "reminder_set", lambda m: {"message": m.group("msg").strip(),
                                  "time": (m.group("time") or "in 1 hour").strip()}, 7),

    # --- Terminal: system info queries (specific — constrained entity set) ---
    (re.compile(r"^(?:how much|check|what(?:'s| is)(?: my)?)\s+(?P<query>disk space|storage|ram|memory|cpu|battery|ip).*$", re.I),
     "terminal", lambda m: {"query": m.group("query").strip()}, 7),

    # --- Memory ---
    (re.compile(r"^remember (?:that )?(?P<fact>.+?)$", re.I),
     "memory_control", lambda m: {"action": "remember", "data": m.group("fact").strip()}, 7),

    (re.compile(r"^(?:forget|delete memory)\s+(?P<fact>.+?)$", re.I),
     "memory_control", lambda m: {"action": "forget", "data": m.group("fact").strip()}, 7),

    # --- File operations (moderate — specific verb + path) ---
    # "find" excludes "find out" (phrasal verb = discover, not locate files)
    (re.compile(r"^(?:move|copy|rename|delete|zip|unzip|find(?!\s+out)|organize)\s+(?P<path>.+?)(?:\s+to\s+(?P<dest>.+))?$", re.I),
     "file_operation", lambda m: {"action": m.group(0).split()[0].lower(),
                                    "path": m.group("path").strip(),
                                    "destination": (m.group("dest") or "").strip()}, 6),

    # --- Install software (moderate — specific verb + name) ---
    (re.compile(r"^(?:install|uninstall|update)\s+(?P<name>.+?)$", re.I),
     "install_software", lambda m: {"action": m.group(0).split()[0].lower(),
                                     "name": m.group("name").strip()}, 6),

    # --- Minimize (moderate — single verb + app) ---
    (re.compile(r"^minimize\s+(?P<app>.+?)$", re.I),
     "minimize_app", lambda m: {"name": _clean_entity(m.group("app"))}, 5),

    # --- Open app (moderate — common verbs + app name) ---
    (re.compile(r"^(?:open|launch|start|run|fire up)\s+(?P<app>.+?)(?:\s+for me)?$", re.I),
     "open_app", lambda m: {"name": _clean_entity(m.group("app"))}, 5),

    # --- Close app ---
    (re.compile(r"^(?:close|quit|kill|exit|stop)\s+(?P<app>.+?)(?:\s+for me)?$", re.I),
     "close_app", lambda m: {"name": _clean_entity(m.group("app"))}, 5),

    # --- Play music (moderate — "play X" is ambiguous with open) ---
    (re.compile(r"^(?:play|listen to|put on)\s+(?P<query>.+?)(?:\s+on\s+(?P<app>spotify|youtube))?$", re.I),
     "play_music", lambda m: {"query": _clean_music_query(m.group("query")),
                               "app": m.group("app") or "spotify",
                               "action": "play"}, 5),

    # --- Search ---
    (re.compile(r"^(?:search for|google|look up|search)\s+(?P<query>.+?)$", re.I),
     "search", lambda m: {"query": m.group("query").strip()}, 5),

    # --- Toggle setting ---
    (re.compile(r"^(?:turn on|turn off|toggle|enable|disable)\s+(?P<setting>.+?)$", re.I),
     "toggle_setting", lambda m: {"setting": m.group("setting").strip()}, 5),

    # --- Focus/switch window (broad — "go to X" matches many things) ---
    (re.compile(r"^(?:switch to|go to|focus|bring up|activate|show)\s+(?P<app>.+?)$", re.I),
     "focus_window", lambda m: {"name": _clean_entity(m.group("app"))}, 3),
]


# Complexity guards — reduce confidence instead of hard-blocking.
# Polite phrasing passes at lower confidence; multi-step gets penalized heavily.
_COMPLEXITY_GUARDS = [
    (re.compile(r'\b(?:and then|then|after that|and also)\b', re.I), 0.20),
    (re.compile(r'\b(?:if|when|unless|while|because|since)\b', re.I), 0.20),
    # "how" only in conversational forms — not "how much", "how's the weather"
    (re.compile(r'\b(?:how (?:do|can|should|would|could|to)|why|explain|compare|difference|should i)\b', re.I), 0.20),
    (re.compile(r'\b(?:what do you think|can you help|tell me about)\b', re.I), 0.20),
]


# ===================================================================
# Stage 3: Fuzzy matching
# ===================================================================

# Common typos and variations
_FUZZY_MAPPINGS = {
    "wether": "weather",
    "wheather": "weather",
    "whats": "what's",
    "opne": "open",
    "lanuch": "launch",
    "serach": "search",
    "seach": "search",
    "gogle": "google",
    "googel": "google",
    "plya": "play",
    "paly": "play",
    "remdiner": "reminder",
    "remidner": "reminder",
    "clsoe": "close",
    "closee": "close",
    "minimze": "minimize",
    "swtich": "switch",
    "swithc": "switch",
}


# ===================================================================
# Entity cleaning helpers
# ===================================================================

def _clean_entity(text):
    """Clean entity from common filler words."""
    for filler in ("please", "the", "app", "application", "program",
                   "for me", "now", "right now", "quickly"):
        text = re.sub(rf'\b{re.escape(filler)}\b', '', text, flags=re.I)
    return text.strip()


def _clean_music_query(text):
    """Clean music query from common prefixes."""
    text = re.sub(r'^(some|a|the|my|me)\s+', '', text, flags=re.I)
    return text.strip() or "popular hits"


def _fix_typos(text):
    """Apply common typo corrections."""
    words = text.split()
    fixed = []
    for word in words:
        fixed.append(_FUZZY_MAPPINGS.get(word.lower(), word))
    return " ".join(fixed)


# ===================================================================
# Main parser
# ===================================================================

def _collect_pattern_matches(text, confidence_base, method):
    """Collect ALL pattern matches with scores, instead of returning first match.

    Args:
        text: Text to match against.
        confidence_base: Base confidence for matches (0.95 for direct, 0.8 for fuzzy).
        method: "pattern" or "fuzzy".

    Returns:
        List of ParsedIntent candidates sorted by (confidence, specificity, slot_count).
    """
    candidates = []
    for pattern, intent_name, extractor, specificity in _SLOT_PATTERNS:
        m = pattern.match(text)
        if not m:
            continue
        slots = extractor(m)
        # Reject pronoun references for app-targeting intents
        if intent_name in ("open_app", "close_app", "focus_window", "minimize_app"):
            app_name = slots.get("name", "").lower()
            if app_name in ("it", "this", "that", "the file", "them"):
                continue
        # Score: specificity bonus added to base confidence (max 1.0)
        confidence = min(confidence_base + specificity * 0.005, 1.0)
        slot_count = sum(1 for v in slots.values() if v)
        candidates.append((
            confidence, specificity, slot_count,
            ParsedIntent(
                intent=intent_name,
                confidence=confidence,
                slots=slots,
                raw_text=text,
                method=method,
            )
        ))
    # Sort by (confidence desc, specificity desc, slot_count desc)
    candidates.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
    return [c[3] for c in candidates]


def parse_intent(user_input):
    """Parse user input into a structured intent with slots.

    3-stage pipeline:
      1. Exact match (highest confidence, 1.0)
      2. Scored pattern matching — collects ALL matches, picks best by
         (confidence, specificity, slot_count) instead of first-match
      3. Fuzzy correction + retry (lower confidence)

    Args:
        user_input: Raw user text (after speech correction).

    Returns:
        ParsedIntent with intent, confidence, and extracted slots.
    """
    if not user_input or len(user_input.strip()) < 2:
        return ParsedIntent(intent="unknown", confidence=0.0, raw_text=user_input or "")

    text = user_input.strip()
    lower = text.lower().rstrip("?!.")

    # --- Stage 1: Exact match ---
    if lower in _EXACT_INTENTS:
        intent, slots = _EXACT_INTENTS[lower]
        return ParsedIntent(
            intent=intent,
            confidence=1.0,
            slots=slots,
            raw_text=text,
            method="exact",
        )

    # --- Complexity guard (penalty, not hard block) ---
    guard_penalty = 0.0
    for guard, penalty in _COMPLEXITY_GUARDS:
        if guard.search(lower):
            guard_penalty += penalty

    # Heavy penalty → treat as complex (but still allow exact matches above)
    if guard_penalty >= 0.30:
        return ParsedIntent(
            intent="complex",
            confidence=0.3,
            raw_text=text,
            method="none",
        )

    # --- Stage 2: Scored pattern matching (collect all, pick best) ---
    adj_base = max(0.90 - guard_penalty, 0.50)
    candidates = _collect_pattern_matches(text, confidence_base=adj_base, method="pattern")
    if candidates:
        return candidates[0]

    # --- Stage 3: Typo correction + retry ---
    fixed = _fix_typos(text)
    if fixed != text:
        fixed_lower = fixed.lower().rstrip("?!.")

        # Retry exact match
        if fixed_lower in _EXACT_INTENTS:
            intent, slots = _EXACT_INTENTS[fixed_lower]
            return ParsedIntent(
                intent=intent,
                confidence=0.85,
                slots=slots,
                raw_text=text,
                method="fuzzy",
            )

        # Retry patterns (scored)
        fuzzy_candidates = _collect_pattern_matches(fixed, confidence_base=0.75, method="fuzzy")
        if fuzzy_candidates:
            return fuzzy_candidates[0]

    # --- No match ---
    return ParsedIntent(
        intent="unknown",
        confidence=0.0,
        raw_text=text,
        method="none",
    )


# Intent → fast_path handler_key mapping (for execution routing)
_INTENT_TO_HANDLER = {
    "open_app": "open_app",
    "close_app": "close_app",
    "minimize_app": "minimize_app",
    "focus_window": "focus_window",
    "snap_window": "snap_window",
    "weather": "weather",
    "forecast": "forecast",
    "time": "time",
    "news": "news",
    "search": "google_search",
    "reminder_set": "set_reminder",
    "reminder_list": "list_reminders",
    "play_music": "play_music",
    "pause_music": "pause_music",
    "next_track": "next_track",
    "screenshot": "screenshot",
    "list_windows": "list_windows",
    "browser_navigate": "browser_navigate",
}


def to_route_decision(parsed):
    """Convert a ParsedIntent to a RouteDecision.

    Maps intent parser output to the unified routing shape so
    it can be compared with fast_path and other routing layers.
    Sets handler_key and intent_name as first-class fields.

    Args:
        parsed: ParsedIntent from parse_intent().

    Returns:
        RouteDecision, or None if not actionable.
    """
    if not parsed or not parsed.is_actionable or parsed.confidence < 0.5:
        return None

    from orchestration.route_decision import RouteDecision

    # Map method → specificity baseline
    method_specificity = {"exact": 10, "pattern": 6, "fuzzy": 4, "none": 0}
    specificity = method_specificity.get(parsed.method, 0)

    return RouteDecision(
        source="intent_parser",
        tool_name=parsed.tool_name,
        args=parsed.slots,
        confidence=parsed.confidence,
        specificity=specificity,
        should_execute=False,  # Will be set by should_execute_directly()
        reason=f"intent_parser:{parsed.intent}({parsed.method})",
        mode="quick",
        intent_name=parsed.intent,
        handler_key=_INTENT_TO_HANDLER.get(parsed.intent, ""),
    )


def get_coverage_stats():
    """Return stats about deterministic coverage.

    Returns:
        dict with counts of exact patterns, slot patterns, etc.
    """
    return {
        "exact_patterns": len(_EXACT_INTENTS),
        "slot_patterns": len(_SLOT_PATTERNS),
        "fuzzy_corrections": len(_FUZZY_MAPPINGS),
        "complexity_guards": len(_COMPLEXITY_GUARDS),
        "intent_to_tool_mappings": len(_INTENT_TO_TOOL),
    }
