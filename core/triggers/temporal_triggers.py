"""
Time-based triggers — morning briefing, meeting alerts, end-of-day, late night.

These triggers fire based on the current time and day type, not system state.
All use a _last_date guard to fire at most once per day where appropriate.
"""

from __future__ import annotations

from typing import Optional

from core.proactive_engine import BaseTrigger, Suggestion


class MorningBriefingTrigger(BaseTrigger):
    """Offers a morning briefing when the user first becomes active in the morning.

    Fires between 07:00–10:00 on any day, once per day, when the user has
    recently interacted (last_interaction_ago ≤ 30 s) — i.e., just woke the
    assistant up.
    """

    id = "morning_briefing"
    category = "suggestion"
    cooldown_seconds = 43200   # 12 hours
    base_urgency = 55

    def __init__(self) -> None:
        super().__init__()
        self._offered_today = False
        self._last_date = ""

    def should_fire(self, state: dict) -> Optional[Suggestion]:
        current_date = state.get("current_date", "")
        current_time = state.get("current_time", "")
        time_of_day = state.get("time_of_day", "")
        last_interaction = state.get("last_interaction_ago", 9999)

        # Reset flag on new day
        if current_date != self._last_date:
            self._offered_today = False
            self._last_date = current_date

        if self._offered_today:
            return None

        if time_of_day != "morning":
            return None

        try:
            hour = int(current_time.split(":")[0]) if current_time else -1
        except (ValueError, IndexError):
            hour = -1

        if hour < 7 or hour > 10:
            return None

        # Only fire immediately after a fresh interaction (user just woke us up)
        if last_interaction > 30:
            return None

        self._offered_today = True
        return Suggestion(
            trigger_id=self.id,
            message=(
                "Good morning! Want me to give you a quick briefing? "
                "I can cover your schedule, weather, and any important notifications."
            ),
            urgency=self.base_urgency,
            category=self.category,
            action="morning_briefing",
        )


class MeetingAlertTrigger(BaseTrigger):
    """Warns about an upcoming calendar event in the next 15 minutes.

    Urgency scales: 5 min → 90, 2 min or less → 95.
    """

    id = "meeting_alert"
    category = "reminder"
    cooldown_seconds = 180   # 3 minutes
    base_urgency = 80

    def should_fire(self, state: dict) -> Optional[Suggestion]:
        next_event = state.get("next_event")
        if not next_event or not isinstance(next_event, dict):
            return None

        minutes = next_event.get("minutes_until", -1)
        name = next_event.get("name", "event")

        if minutes < 0 or minutes > 15:
            return None

        if minutes <= 2:
            urgency = 95
            msg = f"Your {name} starts NOW!"
        elif minutes <= 5:
            urgency = 90
            msg = f"{name} starts in {minutes} minutes."
        else:
            urgency = 75
            msg = f"Heads up — {name} in {minutes} minutes."

        location = next_event.get("location", "")
        if location:
            msg += f" It's at {location}."

        return Suggestion(
            trigger_id=self.id,
            message=msg,
            urgency=urgency,
            category=self.category,
        )


class EndOfDaySummaryTrigger(BaseTrigger):
    """Offers an end-of-day summary between 17:00 and 19:00 on workdays."""

    id = "end_of_day_summary"
    category = "suggestion"
    cooldown_seconds = 43200   # 12 hours
    base_urgency = 45

    def __init__(self) -> None:
        super().__init__()
        self._offered_today = False
        self._last_date = ""

    def should_fire(self, state: dict) -> Optional[Suggestion]:
        current_date = state.get("current_date", "")
        current_time = state.get("current_time", "")
        day_type = state.get("day_type", "")

        if current_date != self._last_date:
            self._offered_today = False
            self._last_date = current_date

        if self._offered_today or day_type != "workday":
            return None

        try:
            hour = int(current_time.split(":")[0]) if current_time else -1
        except (ValueError, IndexError):
            hour = -1

        if hour < 17 or hour > 19:
            return None

        self._offered_today = True
        return Suggestion(
            trigger_id=self.id,
            message=(
                "It's getting late. "
                "Want a summary of what you accomplished today, "
                "or a look at tomorrow's schedule?"
            ),
            urgency=self.base_urgency,
            category=self.category,
            action="daily_summary",
        )


class LateNightTrigger(BaseTrigger):
    """Gently reminds the user when they're working past midnight.

    Cooldown is 1 hour so it doesn't repeat every 10 seconds.
    """

    id = "late_night"
    category = "suggestion"
    cooldown_seconds = 3600
    base_urgency = 45

    def should_fire(self, state: dict) -> Optional[Suggestion]:
        time_of_day = state.get("time_of_day", "")
        current_time = state.get("current_time", "")
        user_present = state.get("user_present", False)

        if time_of_day != "night" or not user_present:
            return None

        try:
            hour = int(current_time.split(":")[0]) if current_time else -1
        except (ValueError, IndexError):
            hour = -1

        # Only fire between 23:00 and 05:59
        if 6 <= hour < 23:
            return None

        return Suggestion(
            trigger_id=self.id,
            message=f"It's {current_time}. You might want to consider wrapping up for the night.",
            urgency=self.base_urgency,
            category=self.category,
        )
