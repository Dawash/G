"""
Performance metrics collection.

Lightweight, thread-safe metrics instrumentation for the voice assistant.
Tracks timings, counters, and numeric values with zero external dependencies.

Usage:
    from core.metrics import metrics

    # Time a block
    with metrics.timer("stt"):
        text = transcribe(audio)

    # Record a value
    metrics.record("confidence", 0.95)

    # Increment a counter
    metrics.increment("cache_hits")

    # Query results
    count, avg, mn, mx, last = metrics.get_timing("stt")
    summary = metrics.get_summary()

    # Debug snapshot
    metrics.snapshot()  # writes debug/metrics_snapshot.json

Built-in timer labels (documented, not enforced):
    startup, stt, tts, llm_tool_call, llm_quick_chat,
    tool_execution, mode_classification, provider_latency, fast_path

Built-in counter labels:
    cache_hits, cache_misses, fast_path_handled, fast_path_missed,
    agent_retries, agent_failures, provider_errors
"""

import json
import os
import threading
import time
from contextlib import contextmanager


class Metrics:
    """Thread-safe singleton for collecting performance metrics.

    Three metric types:
      - Timers: context-manager based, track count/avg/min/max/last in ms.
      - Counters: monotonically increasing integers.
      - Values: arbitrary numeric recordings with count/avg/min/max/last.
    """

    _instance = None
    _init_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._lock = threading.Lock()
                    inst._timers = {}    # label -> [duration_ms, ...]
                    inst._counters = {}  # label -> int
                    inst._values = {}    # label -> [value, ...]
                    inst._start_time = time.perf_counter()
                    cls._instance = inst
        return cls._instance

    # ------------------------------------------------------------------
    # Timer
    # ------------------------------------------------------------------

    @contextmanager
    def timer(self, label: str):
        """Context manager that records the duration of a block in milliseconds.

        Usage:
            with metrics.timer("stt"):
                result = transcribe(audio)
        """
        t0 = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            with self._lock:
                if label not in self._timers:
                    self._timers[label] = []
                self._timers[label].append(elapsed_ms)

    def get_timing(self, label: str):
        """Return (count, avg_ms, min_ms, max_ms, last_ms) for a timer label.

        Returns (0, 0.0, 0.0, 0.0, 0.0) if no data recorded.
        """
        with self._lock:
            samples = self._timers.get(label)
            if not samples:
                return (0, 0.0, 0.0, 0.0, 0.0)
            count = len(samples)
            avg = sum(samples) / count
            mn = min(samples)
            mx = max(samples)
            last = samples[-1]
            return (count, avg, mn, mx, last)

    # ------------------------------------------------------------------
    # Counter
    # ------------------------------------------------------------------

    def increment(self, label: str, amount: int = 1):
        """Increment a counter by the given amount (default 1)."""
        with self._lock:
            self._counters[label] = self._counters.get(label, 0) + amount

    def get_counter(self, label: str) -> int:
        """Return the current value of a counter (0 if not set)."""
        with self._lock:
            return self._counters.get(label, 0)

    # ------------------------------------------------------------------
    # Numeric value recording
    # ------------------------------------------------------------------

    def record(self, label: str, value: float):
        """Record a numeric metric value."""
        with self._lock:
            if label not in self._values:
                self._values[label] = []
            self._values[label].append(float(value))

    def get_value(self, label: str):
        """Return (count, avg, min, max, last) for a recorded value label.

        Returns (0, 0.0, 0.0, 0.0, 0.0) if no data recorded.
        """
        with self._lock:
            samples = self._values.get(label)
            if not samples:
                return (0, 0.0, 0.0, 0.0, 0.0)
            count = len(samples)
            avg = sum(samples) / count
            mn = min(samples)
            mx = max(samples)
            last = samples[-1]
            return (count, avg, mn, mx, last)

    # ------------------------------------------------------------------
    # Summary / export
    # ------------------------------------------------------------------

    def get_summary(self) -> dict:
        """Return a dict of all collected metrics.

        Structure:
            {
                "uptime_s": float,
                "timers": {
                    "label": {"count": int, "avg_ms": float, "min_ms": float,
                              "max_ms": float, "last_ms": float}
                },
                "counters": {"label": int, ...},
                "values": {
                    "label": {"count": int, "avg": float, "min": float,
                              "max": float, "last": float}
                }
            }
        """
        with self._lock:
            summary = {
                "uptime_s": round(time.perf_counter() - self._start_time, 2),
                "timers": {},
                "counters": dict(self._counters),
                "values": {},
            }

            for label, samples in self._timers.items():
                count = len(samples)
                summary["timers"][label] = {
                    "count": count,
                    "avg_ms": round(sum(samples) / count, 2),
                    "min_ms": round(min(samples), 2),
                    "max_ms": round(max(samples), 2),
                    "last_ms": round(samples[-1], 2),
                }

            for label, samples in self._values.items():
                count = len(samples)
                summary["values"][label] = {
                    "count": count,
                    "avg": round(sum(samples) / count, 4),
                    "min": round(min(samples), 4),
                    "max": round(max(samples), 4),
                    "last": round(samples[-1], 4),
                }

            return summary

    def snapshot(self, path: str = None):
        """Write current metrics to a JSON file for debugging.

        Default path: debug/metrics_snapshot.json (relative to project root).
        Creates the directory if it does not exist.
        """
        if path is None:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            debug_dir = os.path.join(project_root, "debug")
            os.makedirs(debug_dir, exist_ok=True)
            path = os.path.join(debug_dir, "metrics_snapshot.json")

        summary = self.get_summary()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

    def reset(self):
        """Clear all collected metrics. Mainly useful for testing."""
        with self._lock:
            self._timers.clear()
            self._counters.clear()
            self._values.clear()
            self._start_time = time.perf_counter()


# Module-level singleton instance
metrics = Metrics()
