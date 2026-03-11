"""
Smart reminder system with natural language time parsing.

Features:
  - Natural language: "remind me to call John at 5pm"
  - Relative times: "in 30 minutes", "in 2 hours"
  - Recurring: "every Monday at 9am"
  - Persistent storage (survives restarts)
  - Background checker thread
  - Voice + console notification
"""

import json
import logging
import os
import re
import time
import threading
import uuid
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

REMINDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.json")

DAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
}


@dataclass
class Reminder:
    id: str
    message: str
    trigger_time: float  # Unix timestamp
    recurrence: str | None = None  # "daily", "weekly", "weekdays", or None
    created_at: float = field(default_factory=time.time)
    active: bool = True
    snoozed_until: float | None = None
    action_type: str = "notify"  # "notify" (default) or "execute"
    action_command: str | None = None  # tool name to execute (e.g. "get_weather")
    action_args: dict = field(default_factory=dict)  # tool arguments


class ReminderManager:
    """Manages reminders with persistence and background checking."""

    def __init__(self, speak_fn=None, check_interval=30, action_registry=None):
        self.reminders: list[Reminder] = []
        self._lock = threading.RLock()  # Protects self.reminders access across threads
        self.speak_fn = speak_fn
        self.check_interval = check_interval
        self._checker_thread = None
        self._running = False
        self._pending_announcements = []  # Thread-safe queue for main thread
        self._action_registry = action_registry
        self._first_check = True  # Batch overdue reminders on first check cycle
        self._load()

    # --- Natural language time parsing ---

    def parse_time(self, time_str):
        """
        Parse natural language time into a Unix timestamp.

        Handles:
          - "5pm", "5:30 PM", "17:00"
          - "tomorrow at 9am"
          - "in 30 minutes", "in 2 hours"
          - "next Monday at 10am"
          - "every day at 8am" (returns time + recurrence)

        Returns (timestamp, recurrence) or (None, None) on failure.
        """
        text = time_str.lower().strip()
        original_text = text  # Keep original for day-name detection later
        now = datetime.now()
        recurrence = None

        # Check for recurrence keywords
        if text.startswith("every day") or text.startswith("daily"):
            recurrence = "daily"
            text = re.sub(r"^(every day|daily)\s*(at)?\s*", "", text)
        elif text.startswith("every weekday"):
            recurrence = "weekdays"
            text = re.sub(r"^every weekday\s*(at)?\s*", "", text)
        elif text.startswith("every week") or any(text.startswith(f"every {d}") for d in DAY_NAMES):
            recurrence = "weekly"
            text = re.sub(r"^every\s*(week\s*(on)?\s*)?", "", text)

        # Relative time: "in X minutes/hours"
        rel_match = re.match(r"in\s+(\d+)\s+(minute|min|hour|hr|second|sec)s?", text)
        if rel_match:
            amount = int(rel_match.group(1))
            unit = rel_match.group(2)
            if unit in ("minute", "min"):
                target = now + timedelta(minutes=amount)
            elif unit in ("hour", "hr"):
                target = now + timedelta(hours=amount)
            elif unit in ("second", "sec"):
                target = now + timedelta(seconds=amount)
            else:
                return None, None
            return target.timestamp(), recurrence

        # Day offset
        day_offset = 0
        if "tomorrow" in text:
            day_offset = 1
            text = text.replace("tomorrow", "").strip()
        elif "today" in text:
            text = text.replace("today", "").strip()

        # Named day: "next monday", "monday"
        for day_name, day_num in DAY_NAMES.items():
            if day_name in text:
                days_ahead = (day_num - now.weekday()) % 7
                if days_ahead == 0 and "next" in text:
                    days_ahead = 7
                elif days_ahead == 0:
                    # Today is that day — only push to next week if time already passed.
                    # We'll check after parsing the time; for now keep days_ahead=0
                    pass
                day_offset = days_ahead
                text = text.replace("next", "").replace(day_name, "").strip()
                break

        # Strip "at" keyword
        text = re.sub(r"^at\s+", "", text).strip()

        # Parse time of day
        target_time = self._parse_clock_time(text)
        if target_time is None:
            # If no specific time, default to top of next hour
            target_time = now.replace(minute=0, second=0) + timedelta(hours=1)
        else:
            target_time = now.replace(
                hour=target_time.hour,
                minute=target_time.minute,
                second=0,
                microsecond=0,
            )

        target_time += timedelta(days=day_offset)

        # If the time is in the past today, push forward
        if target_time.timestamp() <= now.timestamp() and day_offset == 0:
            # If a specific day name was used (e.g. "Monday at 5pm" on Monday 6pm),
            # push to next week, not just tomorrow
            _used_day_name = any(d in original_text.lower() for d in DAY_NAMES)
            target_time += timedelta(days=7 if _used_day_name else 1)

        return target_time.timestamp(), recurrence

    def _parse_clock_time(self, text):
        """Parse clock time like '5pm', '5:30 PM', '17:00', 'noon', 'midnight'."""
        text = text.strip()

        if not text:
            return None

        if text in ("noon", "12 noon"):
            return datetime.now().replace(hour=12, minute=0)
        if text in ("midnight", "12 midnight"):
            return datetime.now().replace(hour=0, minute=0)

        # 12-hour: "5pm", "5:30pm", "5:30 PM"
        match = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)", text)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            period = match.group(3).replace(".", "")

            if period == "pm" and hour != 12:
                hour += 12
            elif period == "am" and hour == 12:
                hour = 0

            return datetime.now().replace(hour=hour, minute=minute)

        # 24-hour: "17:00", "9:30"
        match = re.match(r"(\d{1,2}):(\d{2})", text)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return datetime.now().replace(hour=hour, minute=minute)

        # Plain number: "5" (ambiguous — pick the NEXT upcoming occurrence)
        match = re.match(r"^(\d{1,2})$", text)
        if match:
            hour = int(match.group(1))
            now = datetime.now()

            # Hours > 12 are unambiguous 24-hour format (e.g. "17" = 5pm)
            if hour > 12:
                return now.replace(hour=hour % 24, minute=0)

            # Hour 12 is ambiguous: noon vs midnight.
            # Pick whichever is next. If morning → noon today; if afternoon → midnight tomorrow.
            if hour == 12:
                noon_today = now.replace(hour=12, minute=0, second=0, microsecond=0)
                if now < noon_today:
                    return now.replace(hour=12, minute=0)
                else:
                    return now.replace(hour=0, minute=0)  # midnight — will be pushed to tomorrow by caller

            # Hours 1-11: two candidates — hour (AM) and hour+12 (PM).
            # Pick whichever is the NEXT upcoming one.
            candidate_am = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            candidate_pm = now.replace(hour=hour + 12, minute=0, second=0, microsecond=0)

            # Collect future candidates
            if candidate_am > now and candidate_pm > now:
                # Both are in the future — pick the earlier (closer) one
                chosen = min(candidate_am, candidate_pm)
            elif candidate_pm > now:
                chosen = candidate_pm
            elif candidate_am > now:
                chosen = candidate_am
            else:
                # Both have passed today — pick AM (will be pushed to tomorrow by caller)
                chosen = candidate_am

            return now.replace(hour=chosen.hour, minute=0)

        return None

    # --- CRUD operations ---

    def add_reminder(self, message, time_str):
        """Create a reminder from natural language. Returns confirmation string."""
        trigger_time, recurrence = self.parse_time(time_str)
        if trigger_time is None:
            return f"I couldn't understand the time '{time_str}'. Try something like '5pm' or 'in 30 minutes'."

        reminder = Reminder(
            id=uuid.uuid4().hex[:8],
            message=message,
            trigger_time=trigger_time,
            recurrence=recurrence,
        )
        with self._lock:
            self.reminders.append(reminder)
            self._save()

        trigger_dt = datetime.fromtimestamp(trigger_time)
        time_display = trigger_dt.strftime("%I:%M %p on %A")
        recur_text = f" ({recurrence})" if recurrence else ""

        return f"Got it! I'll remind you to {message} at {time_display}{recur_text}."

    def check_due(self):
        """Return list of reminders that are due now."""
        now = time.time()
        stale_cutoff = now - 48 * 3600  # 48 hours ago
        due = []
        stale_deactivated = 0

        with self._lock:
            for r in self.reminders:
                if not r.active:
                    continue
                if r.snoozed_until and now < r.snoozed_until:
                    continue
                if now >= r.trigger_time:
                    # Silently deactivate non-recurring reminders >48h overdue
                    if not r.recurrence and r.trigger_time < stale_cutoff:
                        r.active = False
                        stale_deactivated += 1
                        continue
                    due.append(r)
            if stale_deactivated:
                logger.info(f"Silently deactivated {stale_deactivated} stale reminder(s) (>48h overdue)")
                self._save()

        return due

    def fire_reminder(self, reminder):
        """Handle a fired reminder — reschedule or deactivate."""
        with self._lock:
            if reminder.recurrence:
                # Reschedule recurring reminders
                if reminder.recurrence == "daily":
                    reminder.trigger_time += 86400
                elif reminder.recurrence == "weekly":
                    reminder.trigger_time += 7 * 86400
                elif reminder.recurrence == "weekdays":
                    next_dt = datetime.fromtimestamp(reminder.trigger_time)
                    while True:
                        next_dt += timedelta(days=1)
                        if next_dt.weekday() < 5:
                            break
                    reminder.trigger_time = next_dt.timestamp()
                reminder.snoozed_until = None
            else:
                reminder.active = False

            self._save()

    def snooze_reminder(self, reminder_id, minutes=10):
        """Snooze a reminder for N minutes."""
        with self._lock:
            for r in self.reminders:
                if r.id == reminder_id:
                    r.snoozed_until = time.time() + minutes * 60
                    r.trigger_time = r.snoozed_until
                    self._save()
                    return f"Snoozed for {minutes} minutes."
        return "Couldn't find that reminder."

    def remove_reminder(self, reminder_id):
        """Cancel a reminder."""
        with self._lock:
            self.reminders = [r for r in self.reminders if r.id != reminder_id]
            self._save()
        return "Reminder cancelled."

    def clear_all(self):
        """Delete all active reminders."""
        with self._lock:
            count = sum(1 for r in self.reminders if r.active)
            self.reminders = [r for r in self.reminders if not r.active]
            self._save()
        if count == 0:
            return "No active reminders to delete."
        return f"Deleted {count} reminder{'s' if count != 1 else ''}."

    def list_active(self):
        """List all active reminders as a voice-friendly string."""
        with self._lock:
            active = [r for r in self.reminders if r.active]
        if not active:
            return "You have no active reminders."

        lines = [f"You have {len(active)} active reminder{'s' if len(active) > 1 else ''}:"]
        for r in sorted(active, key=lambda x: x.trigger_time):
            dt = datetime.fromtimestamp(r.trigger_time)
            lines.append(f"  {r.message} — {dt.strftime('%I:%M %p, %A')}")

        return " ".join(lines)

    # --- Background checker ---

    def get_missed_reminders(self, max_age_hours=24):
        """Return reminders that were due while the assistant was offline.

        Only returns non-recurring reminders that fired within max_age_hours.
        Recurring reminders are auto-rescheduled, so they're not 'missed'.
        """
        now = time.time()
        cutoff = now - (max_age_hours * 3600)
        missed = []
        with self._lock:
            for r in self.reminders:
                if not r.active:
                    continue
                if r.recurrence:
                    continue  # Recurring ones auto-reschedule
                if cutoff < r.trigger_time < now:
                    missed.append(r)
        return missed

    def start_checker(self):
        """Start the background thread that checks for due reminders."""
        if self._running:
            return
        self._running = True
        self._checker_thread = threading.Thread(target=self._check_loop, daemon=True)
        self._checker_thread.start()
        logger.info("Reminder checker started")

    def stop_checker(self):
        """Stop the background checker."""
        self._running = False

    def _check_loop(self):
        """Background loop that fires due reminders."""
        while self._running:
            try:
                due = self.check_due()

                # First check cycle: batch >2 overdue reminders into one announcement
                if self._first_check and len(due) > 2:
                    labels = [r.message for r in due]
                    batch_msg = f"You have {len(due)} overdue reminders: {', '.join(labels)}."
                    logger.info(f"Startup batch: {len(due)} overdue reminders")
                    print(f"\n[REMINDER] Batch: {batch_msg}")
                    for r in due:
                        # Execute action-based reminders silently
                        if r.action_type == "execute" and r.action_command:
                            try:
                                from brain import execute_tool
                                execute_tool(
                                    r.action_command,
                                    r.action_args or {},
                                    action_registry=self._action_registry or {},
                                    reminder_mgr=self,
                                )
                            except Exception as e:
                                logger.error(f"Reminder action failed: {e}")
                        self.fire_reminder(r)
                    with self._lock:
                        self._pending_announcements.append(batch_msg)
                    self._first_check = False
                else:
                    self._first_check = False
                    for r in due:
                        msg = f"Reminder: {r.message}"
                        logger.info(f"Firing reminder: {r.message}")
                        print(f"\n[REMINDER] {r.message}")

                        # Execute action-based reminders (e.g. "every morning tell me the weather")
                        if r.action_type == "execute" and r.action_command:
                            try:
                                from brain import execute_tool
                                result = execute_tool(
                                    r.action_command,
                                    r.action_args or {},
                                    action_registry=self._action_registry or {},
                                    reminder_mgr=self,
                                )
                                if result:
                                    msg = f"{r.message}: {result}"
                            except Exception as e:
                                logger.error(f"Reminder action failed: {e}")

                        # Queue the reminder for the main thread to speak.
                        # pyttsx3 is NOT thread-safe — calling speak_fn from
                        # a background thread crashes the engine.
                        with self._lock:
                            self._pending_announcements.append(msg)
                        self.fire_reminder(r)
            except Exception as e:
                logger.error(f"Reminder check error: {e}")

            time.sleep(self.check_interval)

    def get_pending_announcements(self):
        """
        Return and clear any pending reminder announcements.
        Called from the main thread to safely speak reminders.
        """
        with self._lock:
            if not self._pending_announcements:
                return []
            announcements = list(self._pending_announcements)
            self._pending_announcements.clear()
            return announcements

    # --- Persistence ---

    def _save(self):
        """Save reminders to disk."""
        try:
            data = []
            for r in self.reminders:
                d = asdict(r)
                data.append(d)
            with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            logger.error(f"Failed to save reminders: {e}")

    def _load(self):
        """Load reminders from disk."""
        if not os.path.isfile(REMINDERS_FILE):
            return
        try:
            with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                for d in data:
                    self.reminders.append(Reminder(**d))
            logger.info(f"Loaded {len(self.reminders)} reminders")
            self.cleanup_inactive()
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.error(f"Failed to load reminders: {e}")

    def cleanup_inactive(self):
        """Remove all inactive, non-recurring reminders and save."""
        with self._lock:
            before = len(self.reminders)
            self.reminders = [
                r for r in self.reminders
                if r.active or r.recurrence
            ]
            removed = before - len(self.reminders)
            if removed:
                logger.info(f"Cleaned up {removed} inactive reminder(s)")
                self._save()


def format_due_time(due_time):
    """Format due time in a user-friendly way."""
    now = datetime.now()
    delta = due_time - now
    total_seconds = int(delta.total_seconds())
    if total_seconds < 3600:
        return f"in {total_seconds // 60} minutes"
    elif total_seconds < 86400:
        return f"in {total_seconds // 3600} hours"
    else:
        days = total_seconds // 86400
        return f"in {days} day{'s' if days > 1 else ''}"

