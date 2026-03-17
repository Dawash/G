"""
Pattern-based triggers — learns from user behaviour over time.

These observe repeated behaviour across multiple sessions and offer to
automate or surface patterns the user probably hasn't noticed themselves.
"""

from __future__ import annotations

from collections import Counter
from typing import Optional

from core.proactive_engine import BaseTrigger, Suggestion


class MorningRoutineTrigger(BaseTrigger):
    """Detects which apps the user opens every morning and offers to automate them.

    Requires at least 5 mornings of observation before offering automation.
    Only suggests apps opened on ≥60 % of tracked mornings.
    """

    id = "morning_routine"
    category = "automation"
    cooldown_seconds = 43200   # Once per 12 hours
    base_urgency = 50

    def __init__(self) -> None:
        super().__init__()
        self._morning_apps: Counter = Counter()
        self._days_tracked: int = 0
        self._offered_today: bool = False
        self._last_date: str = ""

    def should_fire(self, state: dict) -> Optional[Suggestion]:
        current_date = state.get("current_date", "")
        time_of_day = state.get("time_of_day", "")
        current_app = state.get("active_app", "")

        # Track morning app usage
        if current_date != self._last_date:
            if time_of_day == "morning" and current_app:
                self._morning_apps[current_app] += 1
                self._days_tracked += 1
            self._offered_today = False
            self._last_date = current_date

        if self._offered_today or time_of_day != "morning":
            return None

        if self._days_tracked < 5:
            return None

        threshold = self._days_tracked * 0.6
        routine_apps = [app for app, cnt in self._morning_apps.items()
                        if cnt >= threshold]

        if len(routine_apps) < 2:
            return None

        self._offered_today = True
        app_list = ", ".join(routine_apps[:4])
        return Suggestion(
            trigger_id=self.id,
            message=(
                f"I notice you usually open {app_list} in the morning. "
                "Want me to set that up automatically for you?"
            ),
            urgency=self.base_urgency,
            category=self.category,
            action="open_morning_apps",
            action_args={"apps": routine_apps},
        )
