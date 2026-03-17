"""
Context-aware triggers — based on what the user is actively doing.

These observe the digital environment: idle time, clipboard content,
repeated searches, window titles, and app state to surface relevant help.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from core.proactive_engine import BaseTrigger, Suggestion


class IdleDuringWorkTrigger(BaseTrigger):
    """Offers help when user is idle for 30+ minutes during work hours.

    Won't fire during video calls, gaming, or active coding sessions.
    """

    id = "idle_during_work"
    category = "suggestion"
    cooldown_seconds = 1800   # 30 minutes
    base_urgency = 35

    def should_fire(self, state: dict) -> Optional[Suggestion]:
        idle_secs = state.get("last_interaction_ago", 0)
        day_type = state.get("day_type", "")
        time_of_day = state.get("time_of_day", "")
        activity = state.get("activity", "idle")

        if day_type != "workday" or time_of_day not in ("morning", "afternoon"):
            return None
        if idle_secs < 1800:
            return None
        if activity in ("video-call", "gaming", "coding"):
            return None

        return Suggestion(
            trigger_id=self.id,
            message=(
                "You've been quiet for a while. "
                "Need help with anything, or should I check your task list?"
            ),
            urgency=self.base_urgency,
            category=self.category,
        )


class RepetitiveSearchTrigger(BaseTrigger):
    """Detects when the user asks about the same topic multiple times.

    Uses simple word-frequency analysis over the last 5 commands.
    Fires when a non-stop word appears in 3 or more of the last 5 commands.
    """

    id = "repetitive_search"
    category = "suggestion"
    cooldown_seconds = 600
    base_urgency = 60

    _STOP = frozenset({
        "the", "a", "an", "is", "it", "to", "for", "and", "or", "in", "on",
        "what", "how", "can", "you", "me", "my", "i", "do", "this", "that",
        "of", "with", "about", "please", "hey", "hi", "get", "give", "show",
        "tell", "find", "check", "are", "was", "has", "have", "will", "just",
    })

    def should_fire(self, state: dict) -> Optional[Suggestion]:
        recent = state.get("recent_commands", [])
        if len(recent) < 3:
            return None

        last_5 = [cmd.lower() for cmd in recent[-5:]]
        all_words: list = []
        for cmd in last_5:
            words = [w for w in re.findall(r"\w+", cmd)
                     if len(w) > 2 and w not in self._STOP]
            all_words.extend(words)

        counts = Counter(all_words)
        repeated = [(w, c) for w, c in counts.items() if c >= 3]
        if not repeated:
            return None

        top_word = max(repeated, key=lambda x: x[1])[0]
        return Suggestion(
            trigger_id=self.id,
            message=(
                f"I notice you've been asking about '{top_word}' several times. "
                "Want me to do a thorough research on that topic?"
            ),
            urgency=self.base_urgency,
            category=self.category,
            action="deep_research",
            action_args={"topic": top_word},
        )


class ClipboardHelperTrigger(BaseTrigger):
    """Offers help based on what the user just copied to the clipboard.

    Detects: error messages, URLs, and large text blocks.
    Uses edge-detection — only fires when clipboard content changes.
    """

    id = "clipboard_helper"
    category = "suggestion"
    cooldown_seconds = 120
    base_urgency = 40

    def __init__(self) -> None:
        super().__init__()
        self._last_clip = ""

    def should_fire(self, state: dict) -> Optional[Suggestion]:
        clip = state.get("clipboard_preview", "")
        if not clip or clip == self._last_clip or len(clip) < 10:
            return None

        self._last_clip = clip
        clip_lower = clip.lower()

        # Error / exception pattern
        _ERROR_KW = ("error", "exception", "traceback", "failed", "errno",
                     "undefined", "null pointer", "segfault", "fatal")
        if any(kw in clip_lower for kw in _ERROR_KW):
            return Suggestion(
                trigger_id=self.id,
                message="I see you copied an error message. Want me to help diagnose it?",
                urgency=55,
                category="suggestion",
                action="diagnose_error",
                action_args={"error_text": clip[:500]},
            )

        # URL pattern
        if re.match(r"https?://\S+", clip.strip()):
            return Suggestion(
                trigger_id=self.id,
                message="I see you copied a URL. Want me to summarize that page for you?",
                urgency=40,
                category="suggestion",
                action="summarize_url",
                action_args={"url": clip.strip()},
            )

        # Large text block
        if len(clip) > 200:
            return Suggestion(
                trigger_id=self.id,
                message="I see you copied a large block of text. Want me to summarize or rewrite it?",
                urgency=35,
                category="suggestion",
            )

        return None


class AppCrashTrigger(BaseTrigger):
    """Detects when a window title signals an app crash or freeze.

    Checks for "not responding", "has stopped", "fatal error", etc. in the
    foreground window title.
    """

    id = "app_crash"
    category = "warning"
    cooldown_seconds = 60
    base_urgency = 70

    _ERROR_TITLES = (
        "not responding", "has stopped", "crash", "fatal error",
        "problem", "failed to", "application error",
    )

    def should_fire(self, state: dict) -> Optional[Suggestion]:
        current_app = state.get("active_app", "")
        title = state.get("active_window_title", "").lower()

        if not any(ind in title for ind in self._ERROR_TITLES):
            return None

        app_label = current_app if current_app else "An application"
        return Suggestion(
            trigger_id=self.id,
            message=(
                f"{app_label} may have crashed or frozen. "
                "Want me to try restarting it?"
            ),
            urgency=self.base_urgency,
            category=self.category,
            action="restart_app",
            action_args={"app_name": current_app},
        )
