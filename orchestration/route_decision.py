"""
Unified route decision — single shape for all routing layers.

All routing layers (fast_path, intent_parser, mode_classifier) return
RouteDecision. A single pipeline function picks the best one.

Also provides command normalization to reduce alias table size.
"""

import logging
import re
from dataclasses import dataclass, field

_logger = logging.getLogger(__name__)


# ===================================================================
# Core data types
# ===================================================================

@dataclass
class RouteDecision:
    """Unified routing result from any decision layer."""
    source: str             # "fast_path", "intent_parser", "llm", "none"
    tool_name: str = ""     # Canonical tool name (empty = no tool)
    args: dict = field(default_factory=dict)  # Tool arguments only — no metadata
    confidence: float = 0.0
    specificity: int = 0    # Higher = more specific pattern
    should_execute: bool = False  # True = execute directly, False = send to LLM
    reason: str = ""        # Human-readable debug string (never parsed as logic)
    mode: str = "quick"     # "quick", "agent", "research", "chat"

    # Execution metadata — first-class fields, not buried in args or reason
    handler_key: str = ""   # fast_path handler key (e.g. "open_app", "snap_window")
    intent_name: str = ""   # intent_parser intent (e.g. "open_app", "weather")

    @property
    def is_deterministic(self):
        """Whether this can be executed without LLM."""
        return self.should_execute and self.confidence >= 0.8

    @property
    def is_high_confidence(self):
        return self.confidence >= 0.95

    @property
    def needs_llm(self):
        return not self.should_execute or self.confidence < 0.8


@dataclass
class RouteTrace:
    """Debug trace of routing decisions — all candidates, not just best."""
    best: RouteDecision
    candidates: list = field(default_factory=list)


# ===================================================================
# Command normalization
# ===================================================================

_VERB_NORMALIZATIONS = {
    "go to": "navigate_to",
    "navigate to": "navigate_to",
    "bring up": "focus",
    "switch to": "focus",
    "activate": "focus",
    "fire up": "open",
    "launch": "open",
    "start": "open",
    "run": "open",
    "quit": "close",
    "kill": "close",
    "exit": "close",
    "look up": "search",
    "google": "search",
    "search for": "search",
    "listen to": "play",
    "put on": "play",
    "remind me to": "remind",
    "remind me": "remind",
    "set a reminder to": "remind",
    "set reminder to": "remind",
    "turn on": "enable",
    "turn off": "disable",
    "check my": "get",
    "what's my": "get",
    "what is my": "get",
    "how much": "get",
    "tell me the": "get",
    "what's the": "get",
    "what is the": "get",
    "give me the": "get",
    "get me the": "get",
    "show me": "show",
    "list": "show",
}


def normalize_command(text):
    """Normalize user input for more consistent routing."""
    if not text:
        return ""

    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    text = text.rstrip('?.!')

    for phrase, replacement in sorted(_VERB_NORMALIZATIONS.items(),
                                       key=lambda x: -len(x[0])):
        if text.startswith(phrase + " "):
            text = replacement + " " + text[len(phrase):].strip()
            break
        elif text.startswith(phrase):
            text = replacement + text[len(phrase):]
            break

    return text.strip()


# ===================================================================
# Best-match selection
# ===================================================================

_SOURCE_PRIORITY = {
    "fast_path": 3,
    "intent_parser": 2,
    "llm": 1,
    "none": 0,
}


def choose_best(decisions):
    """Choose the best RouteDecision from candidates.

    Scoring: confidence > specificity > slot count > source priority.
    Margin rule: if top two disagree on tool and confidence gap < 0.05,
    mark as ambiguous (should_execute=False).
    """
    if not decisions:
        return None

    valid = [d for d in decisions if d.confidence > 0]
    if not valid:
        return None

    ranked = sorted(valid, key=lambda d: (
        d.confidence,
        d.specificity,
        len(d.args or {}),
        _SOURCE_PRIORITY.get(d.source, 0),
    ), reverse=True)

    best = ranked[0]

    # Margin rule: near-ties with different tools → ambiguous
    # Only applies to sensitive/critical tools; safe tools execute directly
    if len(ranked) > 1:
        second = ranked[1]
        if (abs(best.confidence - second.confidence) < 0.05
                and best.tool_name != second.tool_name):
            try:
                from tools.safety_policy import get_safety_level
                safety = get_safety_level(best.tool_name)
                if safety in ("sensitive", "critical"):
                    best.should_execute = False
                    best.reason += " [ambiguous: near-tie]"
            except ImportError:
                best.should_execute = False
                best.reason += " [ambiguous: near-tie]"

    return best


# ===================================================================
# Safety-aware execution gate
# ===================================================================

EXECUTE_THRESHOLD = 0.95
SAFE_EXECUTE_THRESHOLD = 0.80


def should_execute_directly(decision):
    """Determine if a RouteDecision should execute without LLM.

    Uses tool safety from registry when available, falls back to
    hardcoded classification.
    """
    if not decision or not decision.tool_name:
        return False

    # Fast-path decisions are pre-validated with deterministic handlers —
    # they don't pass user input to sensitive tools, they run fixed commands.
    if decision.source == "fast_path" and decision.confidence >= SAFE_EXECUTE_THRESHOLD:
        return True

    safety = _get_tool_safety(decision.tool_name)

    if safety == "safe":
        return decision.confidence >= SAFE_EXECUTE_THRESHOLD
    elif safety == "moderate":
        return decision.confidence >= EXECUTE_THRESHOLD
    elif safety in ("sensitive", "critical"):
        return False  # Always go through Brain for confirmation

    # Unknown safety — use strict threshold
    return decision.confidence >= EXECUTE_THRESHOLD


def _get_tool_safety(tool_name):
    """Get safety level from registry, with fallback classification."""
    try:
        from tools.registry import get_default
        reg = get_default()
        if reg:
            spec = reg.get(tool_name)
            if spec and spec.safety:
                return spec.safety
    except Exception:
        pass

    # Fallback classification for when registry isn't initialized
    _SAFE = frozenset({
        "get_weather", "get_forecast", "get_time", "get_news",
        "list_reminders", "list_windows", "inspect_window",
        "take_screenshot", "find_on_screen", "google_search",
    })
    _MODERATE = frozenset({
        "open_app", "close_app", "focus_window", "snap_window",
        "play_music", "minimize_app", "browser_action",
        "set_reminder", "toggle_setting",
    })
    _SENSITIVE = frozenset({
        "system_command", "manage_files",
        "manage_software", "run_terminal", "send_email",
    })

    if tool_name in _SAFE:
        return "safe"
    elif tool_name in _MODERATE:
        return "moderate"
    elif tool_name in _SENSITIVE:
        return "sensitive"
    return "moderate"


# ===================================================================
# Unified routing pipeline
# ===================================================================

def route(user_input, debug=False):
    """Unified routing pipeline: fast_path → intent_parser → choose best.

    Args:
        user_input: Raw user text (after speech correction).
        debug: If True, return RouteTrace with all candidates.

    Returns:
        RouteDecision (or RouteTrace if debug=True).
    """
    _none = RouteDecision(source="none", mode="chat")

    if not user_input or len(user_input.strip()) < 2:
        return RouteTrace(best=_none) if debug else _none

    decisions = []

    # Layer 1: Fast path (high confidence, pre-validated patterns)
    try:
        from orchestration.fast_path import match_fast_path
        fp = match_fast_path(user_input)
        if fp:
            decisions.append(fp)
    except Exception as e:
        _logger.debug(f"fast_path matching failed: {e}")

    # Layer 2: Intent parser (broader coverage, scored patterns)
    try:
        from orchestration.intent_parser import parse_intent, to_route_decision
        parsed = parse_intent(user_input)
        rd = to_route_decision(parsed)
        if rd:
            decisions.append(rd)
    except Exception as e:
        _logger.debug(f"intent_parser failed: {e}")

    if not decisions:
        return RouteTrace(best=_none, candidates=[]) if debug else _none

    best = choose_best(decisions)
    if not best:
        return RouteTrace(best=_none, candidates=decisions) if debug else _none

    # Apply safety-aware execution gate unless margin rule flagged ambiguity
    if "[ambiguous" not in best.reason:
        best.should_execute = should_execute_directly(best)

    if debug:
        return RouteTrace(best=best, candidates=decisions)
    return best


# ===================================================================
# Execution dispatch
# ===================================================================

def execute_route(decision, action_registry=None, reminder_mgr=None):
    """Execute a deterministic RouteDecision.

    Routes to the appropriate execution backend. All execution uses
    structured dict arguments — no string-packed entities.

    Args:
        decision: RouteDecision with should_execute=True.
        action_registry: Dict of intent -> handler function.
        reminder_mgr: ReminderManager instance.

    Returns:
        Response string, or None if execution failed (caller should
        fall through to Brain/LLM).
    """
    if not decision or not decision.should_execute:
        return None

    handler_key = decision.handler_key

    if decision.source == "fast_path":
        if not handler_key:
            return None
        return _exec_handler(handler_key, decision.args,
                             action_registry, reminder_mgr)

    elif decision.source == "intent_parser":
        # Try fast_path handlers first (better response formatting)
        if handler_key:
            result = _exec_handler(handler_key, decision.args,
                                   action_registry, reminder_mgr)
            if result is not None:
                return result

        # Fall back to tool registry
        if decision.tool_name:
            return _exec_via_registry(decision)

    return None


def _exec_handler(handler_key, arguments, action_registry, reminder_mgr):
    """Execute via fast_path handler with structured arguments."""
    try:
        from orchestration.fast_path import execute_handler
        return execute_handler(handler_key, arguments,
                               action_registry, reminder_mgr)
    except Exception as e:
        _logger.error(f"Handler execution failed ({handler_key}): {e}")
        return None


def _exec_via_registry(decision):
    """Execute via tool registry (for intents not handled by fast_path)."""
    try:
        from tools.registry import get_default
        reg = get_default()
        if not reg:
            return None

        spec = reg.get(decision.tool_name)
        if not spec or not spec.handler:
            return None

        return spec.handler(arguments=decision.args)
    except Exception as e:
        _logger.error(f"Registry execution failed ({decision.tool_name}): {e}")
        return None
