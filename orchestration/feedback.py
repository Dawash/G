"""
Command feedback loop — learns from successes and failures.

Tracks which routing paths work for which commands, so the system can:
  - Skip known-bad approaches faster
  - Boost confidence for proven routes
  - Detect degrading tool reliability

Data is session-scoped (in memory, not persisted) to avoid stale learning.
"""

import logging
import time
from collections import defaultdict

logger = logging.getLogger(__name__)


class CommandFeedback:
    """Tracks command outcomes for routing optimization."""

    def __init__(self):
        # Pattern → {successes: int, failures: int, last_success_route: str, avg_time: float}
        self._stats = defaultdict(lambda: {
            "successes": 0,
            "failures": 0,
            "total_time": 0.0,
            "last_success_route": None,
            "last_failure_reason": None,
        })
        self._start_time = time.time()

    def record_success(self, command_pattern, route_source, elapsed):
        """Record a successful command execution."""
        s = self._stats[command_pattern]
        s["successes"] += 1
        s["total_time"] += elapsed
        s["last_success_route"] = route_source

    def record_failure(self, command_pattern, route_source, reason=""):
        """Record a failed command execution."""
        s = self._stats[command_pattern]
        s["failures"] += 1
        s["last_failure_reason"] = reason

    def get_preferred_route(self, command_pattern):
        """Get the route that last worked for a command pattern, if any."""
        s = self._stats.get(command_pattern)
        if s and s["successes"] > 0:
            return s["last_success_route"]
        return None

    def get_success_rate(self, command_pattern):
        """Get success rate for a command pattern (0.0-1.0)."""
        s = self._stats.get(command_pattern)
        if not s:
            return None
        total = s["successes"] + s["failures"]
        if total == 0:
            return None
        return s["successes"] / total

    def get_avg_time(self, command_pattern):
        """Get average execution time for successful commands."""
        s = self._stats.get(command_pattern)
        if not s or s["successes"] == 0:
            return None
        return s["total_time"] / s["successes"]

    def get_session_summary(self):
        """Get a summary of this session's command performance."""
        if not self._stats:
            return "No commands tracked yet."

        total_cmds = sum(s["successes"] + s["failures"] for s in self._stats.values())
        total_success = sum(s["successes"] for s in self._stats.values())
        total_fail = sum(s["failures"] for s in self._stats.values())

        fp_count = sum(1 for s in self._stats.values() if s["last_success_route"] == "fast_path")
        brain_count = sum(1 for s in self._stats.values() if s["last_success_route"] == "brain")

        session_mins = (time.time() - self._start_time) / 60

        lines = [
            f"Session: {session_mins:.0f} min, {total_cmds} commands",
            f"Success: {total_success}/{total_cmds} ({100*total_success/max(total_cmds,1):.0f}%)",
            f"Routes: {fp_count} fast-path, {brain_count} brain",
        ]

        if total_fail > 0:
            failed_patterns = [p for p, s in self._stats.items() if s["failures"] > 0]
            lines.append(f"Failed: {', '.join(failed_patterns[:5])}")

        return " | ".join(lines)

    def clear(self):
        self._stats.clear()
        self._start_time = time.time()


# Session singleton
_feedback = CommandFeedback()


def get_feedback():
    """Get the session-scoped feedback tracker."""
    return _feedback
