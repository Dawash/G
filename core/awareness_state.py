"""
AwarenessState — single source of truth for what JARVIS knows right now.

This is the CORE data structure of the JARVIS architecture. It holds a live
snapshot of the user, their environment, their system, and their conversation.
Updated continuously by perception streams (via the event bus) and injected
into every LLM call for grounded, context-aware responses.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class AwarenessState:
    """Live snapshot of everything JARVIS knows about the current moment."""

    # === User Identity ===
    user_name: str = ""
    user_emotion: str = "neutral"        # calm/stressed/excited/frustrated/neutral
    user_present: bool = True            # whether user seems active (recent input)

    # === Time & Environment ===
    time_of_day: str = ""                # morning/afternoon/evening/night
    day_type: str = ""                   # workday/weekend/holiday
    current_time: str = ""               # "14:32" — updated every 30s
    current_date: str = ""               # "2025-03-17"
    weather_summary: str = ""            # "Partly cloudy, 18°C" or empty
    location: str = ""                   # home/office/travel — inferred

    # === Active Digital Context ===
    active_app: str = ""                 # "Visual Studio Code" / "Google Chrome"
    active_window_title: str = ""        # Full window title string
    active_file: str = ""               # Extracted filename (e.g. "main.py")
    active_url: str = ""                 # Browser URL if detectable
    activity: str = ""                   # coding/browsing/gaming/reading/writing/video-call/idle
    screen_summary: str = ""             # Brief description of what's visible
    clipboard_preview: str = ""          # First 200 chars of clipboard (on change)

    # === System Health ===
    cpu_percent: float = 0.0
    ram_percent: float = 0.0
    battery_percent: int = 100
    battery_charging: bool = True
    network_status: str = ""             # connected/disconnected/limited
    gpu_percent: float = 0.0
    disk_percent: float = 0.0
    system_health: str = ""              # good/degraded/critical

    # === Notifications & Schedule ===
    recent_notifications: List[Dict[str, str]] = field(default_factory=list)
    next_event: Optional[Dict[str, str]] = None  # {name, time, minutes_until}
    pending_reminders: List[str] = field(default_factory=list)

    # === Conversation Context ===
    conversation_topic: str = ""
    conversation_mood: str = "neutral"   # formal/casual/technical/playful
    last_interaction_ago: int = 0        # seconds since last user input
    recent_commands: List[str] = field(default_factory=list)  # last 10
    pending_tasks: List[str] = field(default_factory=list)

    # === Learned Patterns ===
    current_routine: str = ""            # morning_work/evening_wind_down/""
    predicted_next_action: str = ""
    user_preferences: Dict[str, str] = field(default_factory=dict)

    # === Internal (excluded from snapshots) ===
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_updated: float = field(default=0.0, repr=False)

    def update(self, **kwargs: Any) -> None:
        """Thread-safe update of any non-internal fields."""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self, key) and not key.startswith("_"):
                    setattr(self, key, value)
            self._last_updated = datetime.now().timestamp()

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serializable copy of current state.

        Excludes internal fields (prefixed with _) and threading.Lock objects.
        """
        with self._lock:
            result = {}
            for k, v in self.__dict__.items():
                if k.startswith("_") or isinstance(v, threading.Lock):
                    continue
                result[k] = v
            return result

    def to_context_string(self, relevant_fields: Optional[List[str]] = None) -> str:
        """Convert to a compact string for LLM system prompt injection.

        Args:
            relevant_fields: If provided, only include these fields.
                             If None, include all non-empty non-default fields.

        Returns:
            Formatted context block, or empty string if nothing useful to say.
        """
        snap = self.snapshot()

        if relevant_fields is not None:
            snap = {k: v for k, v in snap.items() if k in relevant_fields}

        # Filter out empty / default / uninformative values
        filtered: Dict[str, Any] = {}
        for k, v in snap.items():
            if v is None or v == "" or v == [] or v == {}:
                continue
            if isinstance(v, float) and v == 0.0:
                continue
            if k == "battery_percent" and v == 100:
                continue
            if k == "battery_charging" and v is True:
                continue
            if k == "user_present" and v is True:
                continue
            if k == "conversation_mood" and v == "neutral":
                continue
            if k == "user_emotion" and v == "neutral":
                continue
            if k == "last_interaction_ago" and v == 0:
                continue
            filtered[k] = v

        if not filtered:
            return ""

        _LABELS = {
            "active_app": "Currently using",
            "active_window_title": "Window",
            "active_file": "Editing",
            "activity": "Activity",
            "time_of_day": "Time of day",
            "current_time": "Time",
            "current_date": "Date",
            "weather_summary": "Weather",
            "battery_percent": "Battery",
            "battery_charging": "Charging",
            "network_status": "Network",
            "system_health": "System health",
            "conversation_topic": "Topic",
            "next_event": "Next event",
            "cpu_percent": "CPU",
            "ram_percent": "RAM",
            "disk_percent": "Disk",
            "pending_reminders": "Reminders",
            "clipboard_preview": "Clipboard",
            "user_name": "User",
            "location": "Location",
        }

        lines = ["[Current Context]"]
        for k, v in filtered.items():
            label = _LABELS.get(k, k.replace("_", " ").title())
            if isinstance(v, list):
                v = ", ".join(str(i) for i in v[:5])
            elif isinstance(v, dict):
                v = json.dumps(v)
            elif isinstance(v, float):
                v = f"{v:.0f}%"
            lines.append(f"  {label}: {v}")

        return "\n".join(lines)


# Module-level singleton — import from anywhere with:
#   from core.awareness_state import awareness
awareness = AwarenessState()
