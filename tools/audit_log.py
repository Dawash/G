"""
Structured audit log for tool execution.

Writes one JSON line per tool execution to audit_log.jsonl.
Thread-safe, append-only, with automatic rotation at 10MB.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_LOG_DIR = os.path.dirname(os.path.dirname(__file__))
_LOG_FILE = os.path.join(_LOG_DIR, "audit_log.jsonl")
_MAX_SIZE = 10 * 1024 * 1024  # 10 MB
_lock = threading.Lock()


def _rotate_if_needed():
    """Rotate the log file if it exceeds max size."""
    try:
        if os.path.exists(_LOG_FILE) and os.path.getsize(_LOG_FILE) > _MAX_SIZE:
            rotated = _LOG_FILE + ".1"
            if os.path.exists(rotated):
                os.remove(rotated)
            os.rename(_LOG_FILE, rotated)
    except Exception as e:
        logger.debug(f"Audit log rotation failed: {e}")


def log_tool_execution(
    tool_name,
    arguments,
    result,
    safety_level="safe",
    confirmation_status="not_required",
    dry_run=False,
    success=True,
    user_utterance="",
    mode="",
    verification_result=None,
    duration_ms=0,
    error=None,
):
    """Append a structured audit entry for a tool execution.

    Args:
        tool_name: Canonical tool name.
        arguments: Dict of tool arguments (sensitive values are redacted).
        result: Tool result string (truncated to 500 chars).
        safety_level: "safe" | "moderate" | "sensitive" | "critical".
        confirmation_status: "not_required" | "confirmed" | "denied" | "auto_confirmed".
        dry_run: Whether this was a dry-run execution.
        success: Whether the tool succeeded.
        user_utterance: Original user text that triggered this tool.
        mode: Routing mode ("quick", "agent", "research", "fast_path").
        verification_result: Optional dict from verifier.
        duration_ms: Execution time in milliseconds.
        error: Error message if failed.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "arguments": _redact_sensitive(arguments),
        "safety_level": safety_level,
        "confirmation": confirmation_status,
        "dry_run": dry_run,
        "success": success,
        "result": str(result)[:500] if result else "",
        "user_utterance": user_utterance[:200] if user_utterance else "",
        "mode": mode,
        "duration_ms": duration_ms,
    }
    if verification_result is not None:
        entry["verification"] = verification_result
    if error:
        entry["error"] = str(error)[:300]

    with _lock:
        try:
            _rotate_if_needed()
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"Audit log write failed: {e}")


def read_recent(n=50):
    """Read the last n audit entries. Returns list of dicts."""
    try:
        if not os.path.exists(_LOG_FILE):
            return []
        with open(_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        entries = []
        for line in lines[-n:]:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return entries
    except Exception:
        return []


# Redaction

_SENSITIVE_KEYS = {"password", "token", "key", "secret", "credential", "body"}


def _redact_sensitive(arguments):
    """Redact sensitive argument values for the audit log."""
    if not isinstance(arguments, dict):
        return arguments
    redacted = {}
    for k, v in arguments.items():
        if any(s in k.lower() for s in _SENSITIVE_KEYS):
            redacted[k] = "***REDACTED***"
        elif isinstance(v, str) and len(v) > 500:
            redacted[k] = v[:500] + "...(truncated)"
        else:
            redacted[k] = v
    return redacted
