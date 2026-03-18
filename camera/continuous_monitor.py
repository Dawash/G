"""
Continuous Monitor — periodic camera capture + vision analysis in background.

Captures frames at a configurable interval, analyzes each with a vision LLM,
and accumulates observations. The caller can stop monitoring at any time and
get an LLM-generated summary of what was observed.

Constraints:
  - Max 100 observations (bounded memory)
  - Min 10 second interval between captures
  - Runs as a daemon thread (dies with main process)
  - Never saves frames to disk

Usage:
    from camera.continuous_monitor import ContinuousMonitor
    monitor = ContinuousMonitor()
    monitor.start("Tell me if anyone enters the room", camera_id=0, interval=15)
    ...
    summary = monitor.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, List, Optional, Union

logger = logging.getLogger(__name__)

_MAX_OBSERVATIONS = 100
_MIN_INTERVAL_SECS = 10


class ContinuousMonitor:
    """Background camera monitor with periodic vision analysis."""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._question: str = ""
        self._camera_id: Union[int, str] = 0
        self._interval: float = 30.0
        self._observations: List[str] = []
        self._started_at: Optional[float] = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        question: str,
        camera_id: Union[int, str] = 0,
        interval: float = 30.0,
    ) -> str:
        """Start continuous monitoring in a background thread.

        Args:
            question: What to look for in each frame.
            camera_id: Which camera to monitor.
            interval: Seconds between captures (min 10).

        Returns:
            Status message.
        """
        if self.is_running:
            return "Monitor is already running. Stop it first with stop()."

        self._question = question
        self._camera_id = camera_id
        self._interval = max(_MIN_INTERVAL_SECS, interval)
        self._observations = []
        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="camera-monitor",
            daemon=True,
        )
        self._thread.start()
        self._started_at = time.time()

        # Publish start event
        self._publish_event("camera.monitor.started", {
            "question": question,
            "camera_id": camera_id,
            "interval": self._interval,
        })

        return (
            f"Started monitoring camera {camera_id} every {self._interval:.0f}s. "
            f"Looking for: {question}"
        )

    def stop(self) -> str:
        """Stop monitoring and return a summary of observations.

        Returns:
            LLM-generated summary, or a list of observations if LLM unavailable.
        """
        if not self.is_running:
            return "Monitor is not running."

        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10.0)
            self._thread = None

        elapsed = time.time() - self._started_at if self._started_at else 0

        self._publish_event("camera.monitor.stopped", {
            "observations": len(self._observations),
            "duration_seconds": round(elapsed),
        })

        with self._lock:
            obs = list(self._observations)

        if not obs:
            return f"Monitoring stopped after {elapsed:.0f}s. No observations captured."

        summary = self.get_summary()
        return summary

    def get_summary(self) -> str:
        """Summarize all observations using the LLM.

        Falls back to a plain list if the LLM is unavailable.
        """
        with self._lock:
            obs = list(self._observations)

        if not obs:
            return "No observations yet."

        # Try LLM summary
        try:
            from camera.vision_analyzer import vision
            obs_text = "\n".join(f"[{i+1}] {o}" for i, o in enumerate(obs))
            prompt = (
                f"You are summarizing camera monitoring observations.\n"
                f"The user asked to watch for: {self._question}\n\n"
                f"Observations ({len(obs)} total):\n{obs_text}\n\n"
                f"Provide a concise summary of what was observed. "
                f"Highlight any notable events or changes."
            )
            # Use a quick chat for the summary (text-only, no vision needed)
            try:
                from brain import Brain
                brain = Brain.__dict__.get("_singleton")
                if brain and hasattr(brain, "quick_chat"):
                    result = brain.quick_chat(prompt)
                    if result:
                        return result
            except Exception:
                pass
        except Exception:
            pass

        # Fallback: plain list
        lines = [f"Monitoring summary ({len(obs)} observations):"]
        for i, o in enumerate(obs[-10:], 1):  # last 10
            lines.append(f"  {i}. {o}")
        if len(obs) > 10:
            lines.insert(1, f"  ... ({len(obs) - 10} earlier observations omitted)")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        """Background capture + analysis loop."""
        from camera.camera_manager import camera_mgr
        from camera.vision_analyzer import vision

        logger.info("Continuous monitor started (camera=%s, interval=%.0fs)",
                     self._camera_id, self._interval)

        while not self._stop_event.is_set():
            try:
                frame = camera_mgr.capture_frame(self._camera_id)
                if frame is not None:
                    analysis = vision.analyze_frame(frame, self._question)
                    if analysis and "Error" not in analysis:
                        timestamp = time.strftime("%H:%M:%S")
                        observation = f"[{timestamp}] {analysis}"

                        with self._lock:
                            if len(self._observations) >= _MAX_OBSERVATIONS:
                                self._observations.pop(0)  # drop oldest
                            self._observations.append(observation)

                        self._publish_event("camera.monitor.observation", {
                            "text": analysis,
                            "count": len(self._observations),
                        })
                        logger.debug("Monitor observation #%d: %s",
                                     len(self._observations), analysis[:80])
                else:
                    logger.debug("Monitor: capture_frame returned None")
            except Exception as exc:
                logger.debug("Monitor loop error: %s", exc)

            # Wait for the interval, but check stop event frequently
            self._stop_event.wait(self._interval)

        logger.info("Continuous monitor stopped (%d observations)",
                     len(self._observations))

    def _publish_event(self, topic: str, payload: dict) -> None:
        """Publish to the event bus (if available)."""
        try:
            from core.event_bus import bus
            bus.publish(topic, payload, source="camera_monitor")
        except Exception:
            pass
