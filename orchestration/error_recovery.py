"""
Error recovery chain for tool failures.

When a tool fails, this module provides structured retry → fallback → degrade:
  1. Retry: Same tool, clean/adjusted args
  2. Fallback: Alternative tool for same intent
  3. Degrade: Honest failure message (never silent swallow)

Also tracks failure patterns to avoid repeating known-bad approaches.
"""

import logging
import re
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

# Recent failures: tool_name → [(timestamp, error_msg), ...]
_failure_log = defaultdict(list)
_MAX_FAILURES = 10  # Per tool, rolling window


def record_failure(tool_name, error_msg, args=None):
    """Record a tool failure for pattern detection."""
    _failure_log[tool_name].append({
        "time": time.time(),
        "error": str(error_msg)[:200],
        "args": str(args)[:100] if args else "",
    })
    # Keep only recent
    if len(_failure_log[tool_name]) > _MAX_FAILURES:
        _failure_log[tool_name] = _failure_log[tool_name][-_MAX_FAILURES:]


def is_tool_failing(tool_name, window_seconds=120):
    """Check if a tool has been failing recently (circuit breaker)."""
    cutoff = time.time() - window_seconds
    recent = [f for f in _failure_log[tool_name] if f["time"] > cutoff]
    return len(recent) >= 3  # 3+ failures in 2 minutes


def get_failure_hint(tool_name):
    """Get a hint about why a tool keeps failing."""
    if not _failure_log[tool_name]:
        return None
    last = _failure_log[tool_name][-1]
    return last["error"]


# Fallback chains: tool → alternative tool(s) for same intent
_FALLBACK_CHAINS = {
    "open_app": ["focus_window"],         # If app can't open, try focusing existing window
    "focus_window": ["open_app"],          # If can't focus, try opening
    "get_weather": ["web_search_answer"],  # If weather API fails, search web
    "get_forecast": ["web_search_answer"],
    "play_music": [],                      # No fallback — music is app-specific
    "browser_action": [],                  # No fallback
    "google_search": ["web_search_answer"],
    "web_search_answer": ["google_search"],
}


def get_fallback(tool_name):
    """Get fallback tool for a failed tool. Returns tool_name or None."""
    chain = _FALLBACK_CHAINS.get(tool_name, [])
    for fallback in chain:
        if not is_tool_failing(fallback):
            return fallback
    return None


def recover_from_failure(handler_key, error_msg, arguments, action_registry=None):
    """Attempt recovery from a fast-path handler failure.

    Returns (success: bool, result: str or None, strategy: str).
    """
    tool_name = handler_key
    record_failure(tool_name, error_msg, arguments)

    # Strategy 1: Retry with cleaned args
    if handler_key in ("open_app", "close_app", "focus_window"):
        name = arguments.get("name", "")
        # Try common name corrections
        cleaned = re.sub(r'\s+(app|application|program)$', '', name, flags=re.I).strip()
        if cleaned != name and action_registry:
            fn = action_registry.get(handler_key)
            if fn:
                try:
                    result = fn(cleaned)
                    if result and "not found" not in str(result).lower():
                        return True, result, "retry_cleaned_name"
                except Exception:
                    pass

    # Strategy 2: Fallback tool
    fallback = get_fallback(handler_key)
    if fallback and action_registry:
        fn = action_registry.get(fallback)
        if fn:
            try:
                name = arguments.get("name", "")
                result = fn(name)
                if result and "not found" not in str(result).lower():
                    return True, result, f"fallback_{fallback}"
            except Exception:
                pass

    # Strategy 3: Graceful degradation — honest failure message
    if handler_key in ("open_app",):
        name = arguments.get("name", "unknown")
        return False, f"I couldn't open {name}. It might not be installed.", "degrade"
    elif handler_key in ("close_app",):
        name = arguments.get("name", "unknown")
        return False, f"I couldn't close {name}. It might not be running.", "degrade"
    elif handler_key in ("get_weather", "get_forecast"):
        return False, "I couldn't get the weather right now. The service might be down.", "degrade"
    elif handler_key.startswith("run_terminal_"):
        return False, f"That command didn't work: {error_msg}", "degrade"

    return False, None, "no_recovery"


def clear_failures(tool_name=None):
    """Clear failure history (e.g., after successful use)."""
    if tool_name:
        _failure_log.pop(tool_name, None)
    else:
        _failure_log.clear()
