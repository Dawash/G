"""
Context Injector — converts AwarenessState into targeted LLM context.

Instead of dumping ALL awareness fields into every prompt (wasteful tokens),
this analyses the user's query and injects only the fields that are relevant
to answering it well.

Usage:
    from core.context_injector import build_context
    ctx = build_context("what time is it?")  # → "[Current Context]\n  Time: 14:32\n..."
"""

from __future__ import annotations

from typing import List

from core.awareness_state import awareness

# ---------------------------------------------------------------------------
# Topic → AwarenessState field lists
# ---------------------------------------------------------------------------

RELEVANCE_MAP: dict = {
    "time": ["current_time", "current_date", "time_of_day", "day_type"],
    "weather": ["weather_summary", "current_time", "next_event", "location"],
    "system": [
        "cpu_percent", "ram_percent", "battery_percent", "battery_charging",
        "disk_percent", "network_status", "system_health", "gpu_percent",
    ],
    "app": ["active_app", "active_window_title", "active_file", "activity"],
    "code": ["active_app", "active_file", "activity", "active_window_title"],
    "schedule": ["next_event", "pending_reminders", "current_time", "day_type", "current_date"],
    "communication": ["recent_notifications", "next_event", "pending_reminders"],
    "location": ["location", "weather_summary", "time_of_day"],
    # Always-useful baseline — included in every injection
    "general": ["current_time", "time_of_day", "active_app", "activity", "user_name"],
}

# ---------------------------------------------------------------------------
# Keyword → topic mappings
# ---------------------------------------------------------------------------

TOPIC_KEYWORDS: dict = {
    "time": ["time", "clock", "hour", "date", "day", "when", "today", "tonight", "morning",
             "afternoon", "evening", "night", "what day"],
    "weather": ["weather", "rain", "raining", "temperature", "cold", "hot", "warm",
                "forecast", "umbrella", "sunny", "cloudy", "snow"],
    "system": ["cpu", "ram", "memory", "battery", "disk", "storage", "wifi", "network",
               "slow", "performance", "process", "kill", "lag", "freeze", "speed",
               "internet", "connection", "bandwidth"],
    "app": ["open", "close", "switch", "launch", "app", "application", "window",
            "minimize", "focus", "bring up", "start"],
    "code": ["code", "coding", "function", "bug", "error", "compile", "run", "debug",
             "git", "commit", "python", "javascript", "typescript", "file", "script",
             "program", "class", "method", "import"],
    "schedule": ["meeting", "calendar", "schedule", "event", "reminder", "alarm",
                 "appointment", "remind", "when is", "what's next"],
    "communication": ["email", "message", "slack", "call", "notification", "inbox",
                      "text", "chat", "ping"],
    "location": ["location", "where", "home", "office", "work", "travel", "here"],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_relevant_topics(user_input: str) -> List[str]:
    """Return the topic keys whose keywords appear in user_input.

    Always includes "general" for baseline context.
    """
    lower = user_input.lower()
    topics: List[str] = ["general"]
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            if topic not in topics:
                topics.append(topic)
    return topics


def get_relevant_fields(user_input: str) -> List[str]:
    """Return the deduplicated list of AwarenessState fields relevant to user_input."""
    topics = detect_relevant_topics(user_input)
    fields: List[str] = []
    seen = set()
    for topic in topics:
        for f in RELEVANCE_MAP.get(topic, []):
            if f not in seen:
                seen.add(f)
                fields.append(f)
    return fields


def build_context(user_input: str = "", include_all: bool = False) -> str:
    """Build a context string for injection into an LLM system prompt.

    Args:
        user_input:  The user's current request. Used to select relevant fields.
                     Pass empty string for generic/greeting context.
        include_all: If True, include all non-empty fields (for proactive use).

    Returns:
        Formatted context block (may be empty string if nothing useful).
    """
    if include_all:
        return awareness.to_context_string()

    if not user_input:
        return awareness.to_context_string(relevant_fields=RELEVANCE_MAP["general"])

    fields = get_relevant_fields(user_input)
    return awareness.to_context_string(relevant_fields=fields)
