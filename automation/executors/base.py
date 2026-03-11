"""Base types for typed domain execution."""

import time
from dataclasses import dataclass, field


@dataclass
class ActionSpec:
    """Specification for a state transition.

    Every domain action is described by this shape before execution.
    The executor reads preconditions, executes, and verifies postconditions.
    """
    name: str
    domain: str                          # "windows", "browser", "filesystem"
    args: dict = field(default_factory=dict)
    preconditions: list = field(default_factory=list)   # e.g. ["browser_running"]
    verification: list = field(default_factory=list)     # e.g. ["url_is:https://..."]
    fallback_chain: list = field(default_factory=list)   # e.g. ["uia", "keyboard"]
    safe: bool = True


@dataclass
class ActionResult:
    """Result of executing a state transition.

    Captures before/after state, which strategy succeeded, and verification.
    """
    ok: bool
    strategy_used: str = ""       # "api", "cdp", "uia", "keyboard", "vision"
    state_before: dict = field(default_factory=dict)
    state_after: dict = field(default_factory=dict)
    verified: bool = False
    error: str | None = None
    duration_ms: int = 0
    message: str = ""             # Human-readable result

    @property
    def state_changed(self):
        """Whether the state actually changed."""
        return self.state_before != self.state_after


def _timed_exec(fn):
    """Measure execution time, return (result, duration_ms)."""
    t0 = time.perf_counter()
    result = fn()
    ms = int((time.perf_counter() - t0) * 1000)
    return result, ms
