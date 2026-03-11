"""
World state — task-level state tracking for the desktop agent.

Phase 20: Maintains a structured view of what's happening during agent execution.
Tracks open windows, current task progress, completed steps, failures, and
provides typed preconditions/postconditions for actions.

Used by the agent loop to make smarter decisions without repeated LLM calls.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ===================================================================
# Failure taxonomy
# ===================================================================

class FailureType(Enum):
    """Categorized failure types for better recovery decisions."""
    NONE = "none"
    # Element failures
    ELEMENT_NOT_FOUND = "element_not_found"
    ELEMENT_NOT_INTERACTABLE = "element_not_interactable"
    ELEMENT_OBSCURED = "element_obscured"
    # Window failures
    WINDOW_NOT_FOUND = "window_not_found"
    WINDOW_NOT_RESPONDING = "window_not_responding"
    WRONG_WINDOW = "wrong_window"
    # App failures
    APP_NOT_INSTALLED = "app_not_installed"
    APP_CRASHED = "app_crashed"
    APP_LOADING = "app_loading"
    # Navigation failures
    PAGE_NOT_LOADED = "page_not_loaded"
    REDIRECT = "redirect"
    AUTH_REQUIRED = "auth_required"
    # System failures
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    # Agent failures
    STUCK_LOOP = "stuck_loop"
    WRONG_APPROACH = "wrong_approach"
    AMBIGUOUS_STATE = "ambiguous_state"


@dataclass
class FailureRecord:
    """Record of a single failure occurrence."""
    type: FailureType
    step_index: int
    tool_name: str
    error_msg: str
    timestamp: float = field(default_factory=time.time)
    recovery_attempted: str = ""
    resolved: bool = False


# ===================================================================
# Step tracking
# ===================================================================

class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


@dataclass
class StepRecord:
    """Record of a single agent step."""
    index: int
    description: str
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    result: str = ""
    error: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    retries: int = 0
    failure: Optional[FailureRecord] = None

    @property
    def duration(self):
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0.0


# ===================================================================
# Window state
# ===================================================================

@dataclass
class WindowInfo:
    """Snapshot of a window's state."""
    title: str
    process_name: str = ""
    is_active: bool = False
    is_minimized: bool = False
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0


# ===================================================================
# World State
# ===================================================================

class WorldState:
    """Tracks the state of the world during agent task execution.

    Provides a structured, queryable view of:
      - What windows are open and which is active
      - What steps have been completed, failed, or are pending
      - What failures have occurred and what recovery was attempted
      - What the current URL/page is (for browser tasks)
      - Preconditions and postconditions for actions
    """

    def __init__(self, goal=""):
        self.goal = goal
        self.steps: list[StepRecord] = []
        self.failures: list[FailureRecord] = []
        self.windows: list[WindowInfo] = []
        self.active_window: str = ""
        self.current_url: str = ""
        self.current_page_title: str = ""
        self.clipboard: str = ""
        self.start_time: float = time.time()
        self.completed: bool = False
        self.completion_reason: str = ""
        self._observation_cache: dict = {}
        self._cache_time: float = 0.0

    # --- Step management ---

    def add_step(self, description, tool_name="", tool_args=None):
        """Add a planned step."""
        step = StepRecord(
            index=len(self.steps),
            description=description,
            tool_name=tool_name,
            tool_args=tool_args or {},
        )
        self.steps.append(step)
        return step

    def start_step(self, index):
        """Mark a step as running."""
        if 0 <= index < len(self.steps):
            self.steps[index].status = StepStatus.RUNNING
            self.steps[index].start_time = time.time()

    def complete_step(self, index, result=""):
        """Mark a step as completed."""
        if 0 <= index < len(self.steps):
            self.steps[index].status = StepStatus.COMPLETED
            self.steps[index].result = result
            self.steps[index].end_time = time.time()

    def fail_step(self, index, error="", failure_type=FailureType.NONE):
        """Mark a step as failed."""
        if 0 <= index < len(self.steps):
            step = self.steps[index]
            step.status = StepStatus.FAILED
            step.error = error
            step.end_time = time.time()
            step.retries += 1

            failure = FailureRecord(
                type=failure_type,
                step_index=index,
                tool_name=step.tool_name,
                error_msg=error,
            )
            step.failure = failure
            self.failures.append(failure)

    def skip_step(self, index, reason=""):
        """Mark a step as skipped."""
        if 0 <= index < len(self.steps):
            self.steps[index].status = StepStatus.SKIPPED
            self.steps[index].result = reason

    def rollback_step(self, index):
        """Mark a step as rolled back."""
        if 0 <= index < len(self.steps):
            self.steps[index].status = StepStatus.ROLLED_BACK

    # --- Queries ---

    @property
    def current_step_index(self):
        """Get the index of the first pending or running step."""
        for step in self.steps:
            if step.status in (StepStatus.PENDING, StepStatus.RUNNING):
                return step.index
        return len(self.steps)

    @property
    def completed_steps(self):
        return [s for s in self.steps if s.status == StepStatus.COMPLETED]

    @property
    def failed_steps(self):
        return [s for s in self.steps if s.status == StepStatus.FAILED]

    @property
    def pending_steps(self):
        return [s for s in self.steps if s.status == StepStatus.PENDING]

    @property
    def progress_pct(self):
        """Percentage of steps completed."""
        if not self.steps:
            return 0
        done = len([s for s in self.steps
                    if s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED)])
        return int(100 * done / len(self.steps))

    @property
    def total_failures(self):
        return len(self.failures)

    @property
    def is_stuck(self):
        """Detect if agent is stuck (same step failing 3+ times)."""
        for step in self.steps:
            if step.retries >= 3:
                return True
        # Check for repeated same-type failures
        if len(self.failures) >= 3:
            recent = self.failures[-3:]
            if all(f.type == recent[0].type for f in recent):
                return True
        return False

    @property
    def elapsed_time(self):
        return time.time() - self.start_time

    # --- Window state ---

    def update_windows(self, windows=None):
        """Refresh window state from system.

        Args:
            windows: List of window dicts (from list_windows) or None to auto-detect.
        """
        if windows is not None:
            self.windows = [
                WindowInfo(
                    title=w.get("title", ""),
                    process_name=w.get("process_name", ""),
                    is_minimized=w.get("minimized", False),
                )
                for w in windows
            ]
        else:
            try:
                from automation.window_manager import list_windows
                wins = list_windows()
                self.windows = [
                    WindowInfo(
                        title=w.get("title", ""),
                        process_name=w.get("process_name", ""),
                        is_minimized=w.get("minimized", False),
                    )
                    for w in wins
                ]
            except Exception:
                pass

        # Update active window
        try:
            from automation.ui_control import get_active_window_info
            info = get_active_window_info()
            if info:
                self.active_window = info.get("title", "")
        except Exception:
            pass

    def is_window_open(self, name):
        """Check if a window with the given name is open."""
        name_lower = name.lower()
        return any(name_lower in w.title.lower() for w in self.windows)

    # --- Browser state ---

    def update_browser_state(self):
        """Refresh browser URL and page title."""
        try:
            from automation.browser_driver import browser_get_url, is_browser_active
            if is_browser_active():
                self.current_url = browser_get_url()
        except Exception:
            pass

    # --- Observation cache ---

    def cache_observation(self, key, value, ttl=5.0):
        """Cache an observation result to avoid repeated expensive operations."""
        self._observation_cache[key] = (value, time.time() + ttl)

    def get_cached_observation(self, key):
        """Get a cached observation if still valid."""
        if key in self._observation_cache:
            value, expiry = self._observation_cache[key]
            if time.time() < expiry:
                return value
            del self._observation_cache[key]
        return None

    # --- Failure analysis ---

    def classify_failure(self, error_msg, tool_name=""):
        """Classify an error message into a FailureType."""
        msg = error_msg.lower()

        if "not found" in msg:
            if "window" in msg:
                return FailureType.WINDOW_NOT_FOUND
            if "app" in msg or "application" in msg:
                return FailureType.APP_NOT_INSTALLED
            return FailureType.ELEMENT_NOT_FOUND

        if "not responding" in msg or "hung" in msg:
            return FailureType.WINDOW_NOT_RESPONDING

        if "permission" in msg or "denied" in msg or "access" in msg:
            return FailureType.PERMISSION_DENIED

        if "timeout" in msg or "timed out" in msg:
            return FailureType.TIMEOUT

        if "network" in msg or "connection" in msg or "dns" in msg:
            return FailureType.NETWORK_ERROR

        if "login" in msg or "sign in" in msg or "auth" in msg:
            return FailureType.AUTH_REQUIRED

        if "loading" in msg or "please wait" in msg:
            return FailureType.APP_LOADING

        if "obscured" in msg or "behind" in msg or "covered" in msg:
            return FailureType.ELEMENT_OBSCURED

        return FailureType.NONE

    def suggest_recovery(self, failure_type):
        """Suggest a recovery action for a failure type.

        Returns:
            str: Suggested recovery action description.
        """
        suggestions = {
            FailureType.ELEMENT_NOT_FOUND:
                "Try inspect_window to see available controls, or use take_screenshot",
            FailureType.WINDOW_NOT_FOUND:
                "Use open_app to launch the application first",
            FailureType.APP_NOT_INSTALLED:
                "Use manage_software to install the application",
            FailureType.APP_LOADING:
                "Wait 2-3 seconds and retry the action",
            FailureType.WRONG_WINDOW:
                "Use focus_window to switch to the correct window",
            FailureType.ELEMENT_OBSCURED:
                "Close popup/dialog first, or use press_key('escape')",
            FailureType.AUTH_REQUIRED:
                "Pause for user to log in (takeover mode)",
            FailureType.TIMEOUT:
                "Reduce operation scope or check network connectivity",
            FailureType.PERMISSION_DENIED:
                "Try with admin=True or ask user for permission",
            FailureType.STUCK_LOOP:
                "Abandon current approach and try an alternative method",
        }
        return suggestions.get(failure_type, "Retry with modified parameters")

    # --- Serialization ---

    def summary(self):
        """Get a concise summary of the world state for LLM context."""
        parts = [f"Goal: {self.goal}"]
        parts.append(f"Progress: {self.progress_pct}% ({len(self.completed_steps)}/{len(self.steps)} steps)")

        if self.active_window:
            parts.append(f"Active window: {self.active_window[:50]}")
        if self.current_url:
            parts.append(f"Current URL: {self.current_url[:80]}")

        if self.completed_steps:
            last = self.completed_steps[-1]
            parts.append(f"Last completed: {last.description[:50]}")

        if self.failures:
            last_fail = self.failures[-1]
            parts.append(f"Last failure: {last_fail.type.value} - {last_fail.error_msg[:50]}")

        if self.is_stuck:
            parts.append("WARNING: Agent appears stuck")

        return " | ".join(parts)

    def to_dict(self):
        """Serialize full state for debugging/logging."""
        return {
            "goal": self.goal,
            "elapsed": round(self.elapsed_time, 1),
            "progress_pct": self.progress_pct,
            "steps": [
                {
                    "index": s.index,
                    "desc": s.description[:60],
                    "status": s.status.value,
                    "tool": s.tool_name,
                    "result": s.result[:60] if s.result else "",
                    "error": s.error[:60] if s.error else "",
                    "retries": s.retries,
                    "duration": round(s.duration, 2),
                }
                for s in self.steps
            ],
            "active_window": self.active_window,
            "current_url": self.current_url,
            "total_failures": self.total_failures,
            "is_stuck": self.is_stuck,
        }
