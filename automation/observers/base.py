"""Base types for structured state observation."""

import time
from dataclasses import dataclass, field


@dataclass
class ObservationResult:
    """Structured state snapshot from an observer.

    Every observer returns this shape. No mutations, no LLM calls.
    Sub-second execution.
    """
    domain: str           # "windows", "browser", "filesystem", "system"
    timestamp: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)
    confidence: float = 1.0   # How reliable is this observation
    source: str = ""          # "win32", "cdp", "uia", "os", "vision"
    stale_after: float = 5.0  # Seconds until this observation expires

    @property
    def is_stale(self):
        return (time.time() - self.timestamp) > self.stale_after

    def get(self, key, default=None):
        return self.data.get(key, default)
