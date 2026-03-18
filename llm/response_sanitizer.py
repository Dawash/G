"""
Error recovery UX — convert raw tool errors to friendly spoken messages.

Extracted from: brain.py  _friendly_error(), _is_error_result(),
                _ERROR_PATTERNS, _FRIENDLY_MESSAGES

Responsibility:
  - Classify raw error strings by pattern (timeout, network, permission, ...)
  - Map each category to a natural, spoken-friendly message
  - Suggest similar apps when "not found"
  - Detect whether a result is actually an error vs. a success string
"""

import re
import logging

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# Pattern-based error classification
# Each entry: (pattern_substring, category_key)
# -----------------------------------------------------------------------

ERROR_PATTERNS = [
    # App not found — suggest similar apps
    ("not found", "app_not_found"),
    ("not installed", "app_not_found"),
    ("no such", "app_not_found"),
    # Timeout errors
    ("timed out", "timeout"),
    ("timeout", "timeout"),
    ("took too long", "timeout"),
    # Network / connection errors
    ("connection error", "network"),
    ("network error", "network"),
    ("connectionerror", "network"),
    ("cannot connect", "network"),
    ("couldn't connect", "network"),
    ("unable to connect", "network"),
    ("no internet", "network"),
    # Permission errors
    ("permission denied", "permission"),
    ("access denied", "permission"),
    ("blocked for safety", "safety"),
    ("blocked:", "safety"),
    # Music/media playback
    ("couldn't play", "playback"),
    ("couldn't auto-play", "playback"),
    ("playback failed", "playback"),
    ("no results", "no_results"),
    # Agent/automation failures
    ("agent task failed", "agent_fail"),
    ("agent_task timed out", "timeout"),
    ("task took too long", "timeout"),
    # Generic tool errors
    ("error executing", "generic_tool"),
    ("unknown tool", "unknown_tool"),
]

FRIENDLY_MESSAGES = {
    "app_not_found": "I couldn't find that app on your system. Would you like me to search for something similar?",
    "timeout": "That's taking too long. Want me to try a different approach?",
    "network": "I'm having trouble connecting to the internet. Let me try again in a moment.",
    "permission": "I don't have permission to do that. You may need to run it manually.",
    "safety": "That action is blocked for safety. I can help you do it a different way if you'd like.",
    "playback": "I had trouble with playback. Would you like me to try a different app or method?",
    "no_results": "I couldn't find any results for that. Try rephrasing or being more specific.",
    "agent_fail": "I wasn't able to complete that task automatically. Want me to try a simpler approach?",
    "generic_tool": "Something went wrong with that action. Let me try a different way.",
    "unknown_tool": "I don't have a tool for that. Let me try to handle it differently.",
}


def friendly_error(error_text, user_input="", tool_name=""):
    """Convert raw error messages to natural, helpful spoken messages.

    Args:
        error_text: The raw error string from tool execution.
        user_input: The original user request (for context).
        tool_name: The tool that failed (for context).

    Returns:
        A friendly, speakable error message with suggestions when possible.
        Returns the original text unchanged if it is not an error.
    """
    if not error_text:
        return error_text

    error_lower = str(error_text).lower()

    # Check if this is actually an error (not all results with these words are errors)
    _not_errors = ["opened", "completed", "success", "done", "playing", "started"]
    if any(w in error_lower for w in _not_errors) and not any(
        w in error_lower for w in ["error", "failed", "couldn't", "timed out"]
    ):
        return error_text  # Not actually an error — return as-is

    # Match against known error patterns
    matched_category = None
    for pattern, category in ERROR_PATTERNS:
        if pattern in error_lower:
            matched_category = category
            break

    if not matched_category:
        # No pattern matched — check if it even looks like an error
        if not any(w in error_lower for w in [
            "error", "failed", "couldn't", "timed out", "timeout",
            "blocked", "denied", "not found", "unable",
        ]):
            return error_text  # Not an error — return unchanged
        # Generic fallback for unrecognized errors
        matched_category = "generic_tool"

    friendly = FRIENDLY_MESSAGES.get(matched_category, error_text)

    # App-not-found: try to suggest similar apps (skip for open_app which
    # already has its own suggestion logic in execute_tool)
    if matched_category == "app_not_found" and tool_name != "open_app":
        _app_match = re.search(r"(?:find|open|launch|start)\s+(.+?)(?:\.|$)", user_input, re.I)
        if _app_match:
            app_name = _app_match.group(1).strip()
            try:
                from app_finder import find_similar_apps
                alts = find_similar_apps(app_name, limit=3)
                if alts:
                    friendly = f"I couldn't find {app_name}. Did you mean: {', '.join(alts)}?"
            except Exception:
                pass

    # Playback errors: suggest alternative app
    if matched_category == "playback":
        if "spotify" in error_lower or "spotify" in user_input.lower():
            friendly = "I couldn't start playing on Spotify. Would you like me to try YouTube instead?"
        elif "youtube" in error_lower or "youtube" in user_input.lower():
            friendly = "I had trouble playing on YouTube. Would you like me to try Spotify instead?"

    # Log the conversion for debugging
    logger.debug(f"Friendly error: '{str(error_text)[:80]}' -> category={matched_category}")
    return friendly


def is_error_result(result):
    """Check if a tool result string represents an error that needs wrapping.

    Returns False for results that already contain user-friendly messages
    (e.g., "I couldn't find a location called 'xyz'. Try...")
    """
    if not result:
        return False
    lower = str(result).lower()
    # Already user-friendly — contains advice like "try", "did you mean", "make sure"
    if any(w in lower for w in ["try ", "did you mean", "make sure", "would you like"]):
        return False
    return any(w in lower for w in [
        "error", "failed", "not found", "couldn't", "timed out",
        "timeout", "blocked", "denied", "unable", "could not",
    ]) and not any(w in lower for w in [
        "opened", "completed", "success", "done", "playing", "started",
    ])
