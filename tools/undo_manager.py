"""
Undo manager for reversible tool actions.

Thread-safe undo stack with time-windowed rollback (30s window, 10-entry max).
Tools register rollback functions via ToolSpec.rollback; the UndoManager
stores them and executes on "undo" command.
"""

import logging
import time
import threading

logger = logging.getLogger(__name__)

UNDO_WINDOW = 30  # seconds
MAX_UNDO = 10


class UndoManager:
    """Thread-safe undo stack for reversible tool actions."""

    def __init__(self):
        self._stack = []
        self._lock = threading.Lock()

    @property
    def stack(self):
        """Direct access for legacy code that reads _undo_stack."""
        return self._stack

    def register(self, tool_name, arguments, rollback_fn, description):
        """Push an undo entry onto the stack."""
        with self._lock:
            self._stack.append({
                "time": time.time(),
                "tool": tool_name,
                "args": arguments,
                "rollback_fn": rollback_fn,
                "description": description,
            })
            if len(self._stack) > MAX_UNDO:
                del self._stack[:len(self._stack) - MAX_UNDO]

    def undo(self):
        """Pop and execute the most recent undo within the time window.

        Returns:
            str or None: Description of what was undone, or None if nothing to undo.
        """
        with self._lock:
            if not self._stack:
                return None
            entry = self._stack[-1]
            if time.time() - entry["time"] > UNDO_WINDOW:
                return None
            entry = self._stack.pop()

        # Execute outside lock
        try:
            entry["rollback_fn"]()
            logger.info(f"Undo: reversed '{entry['description']}'")
            return f"Undone: {entry['description']}"
        except Exception as e:
            logger.error(f"Undo failed for '{entry['description']}': {e}")
            return f"Undo failed: {e}"

    def clear(self):
        with self._lock:
            self._stack.clear()
