"""
Alarm system — separate from reminders for wake-up alarms with sound.

Features:
  - Morning wake-up alarm with sound/music playback
  - Voice-dismissable ("stop", "snooze", "I'm awake")
  - LLM-generated daily motivation (never repeats)
  - Auto-triggers morning briefing (weather + news) after dismiss
  - Multiple alarm types: morning, custom, one-time
  - Recurring (daily, weekdays, weekends, specific days)
  - Persistent storage (alarms.json)
  - Expandable: add new alarm types, sounds, routines
"""

import json
import logging
import os
import re
import threading
import time
import uuid
import winsound
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

ALARMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alarms.json")
SOUNDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")

# Default alarm sound — Windows system sounds as fallback
DEFAULT_ALARM_SOUND = os.path.join(SOUNDS_DIR, "alarm.wav")
MORNING_SOUND = os.path.join(SOUNDS_DIR, "morning.wav")

DAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
}


@dataclass
class Alarm:
    id: str
    label: str
    hour: int                             # 0-23
    minute: int                           # 0-59
    alarm_type: str = "morning"           # "morning", "custom", "one_time"
    recurrence: str = "daily"             # "daily", "weekdays", "weekends", "once", or "mon,wed,fri"
    sound_file: str | None = None         # Custom sound file path
    active: bool = True
    created_at: float = field(default_factory=time.time)
    last_fired: float = 0.0              # Prevents double-firing
    snooze_minutes: int = 5              # Default snooze duration
    morning_briefing: bool = True        # Trigger weather+news after dismiss (morning only)


class AlarmManager:
    """Manages alarms with sound playback and voice dismissal."""

    def __init__(self, speak_fn=None, brain_ref=None):
        self.alarms: list[Alarm] = []
        self._lock = threading.Lock()
        self.speak_fn = speak_fn
        self.brain_ref = brain_ref        # For LLM motivation generation
        self._checker_thread = None
        self._running = False
        self._alarm_playing = False       # True while alarm sound is ringing
        self._stop_alarm = threading.Event()
        self._current_alarm: Alarm | None = None
        self._load()

    # ------------------------------------------------------------------
    # Time parsing (for alarm creation)
    # ------------------------------------------------------------------

    def _parse_alarm_time(self, time_str):
        """Parse time string into (hour, minute).

        Handles: "7am", "7:30 AM", "17:00", "6:30", "noon", "midnight"
        Returns (hour, minute) or (None, None).
        """
        text = time_str.strip().lower()

        if text == "noon":
            return 12, 0
        if text == "midnight":
            return 0, 0

        # "7am", "7pm", "7 am"
        m = re.match(r'^(\d{1,2})\s*(am|pm)$', text)
        if m:
            h = int(m.group(1))
            if m.group(2) == "pm" and h != 12:
                h += 12
            if m.group(2) == "am" and h == 12:
                h = 0
            return h, 0

        # "7:30am", "7:30 pm", "7:30AM"
        m = re.match(r'^(\d{1,2}):(\d{2})\s*(am|pm)?$', text)
        if m:
            h = int(m.group(1))
            mi = int(m.group(2))
            if m.group(3) == "pm" and h != 12:
                h += 12
            if m.group(3) == "am" and h == 12:
                h = 0
            return h, mi

        # "17:00"
        m = re.match(r'^(\d{1,2}):(\d{2})$', text)
        if m:
            return int(m.group(1)), int(m.group(2))

        return None, None

    def _parse_recurrence(self, text):
        """Parse recurrence from user text.

        Returns: "daily", "weekdays", "weekends", "once", or "mon,wed,fri" etc.
        """
        text = text.lower()
        if "every day" in text or "daily" in text:
            return "daily"
        if "weekday" in text:
            return "weekdays"
        if "weekend" in text:
            return "weekends"
        if "once" in text or "one time" in text or "tomorrow" in text:
            return "once"

        # Specific days: "monday and wednesday" or "mon, wed, fri"
        found_days = []
        for day_name, day_num in DAY_NAMES.items():
            if day_name in text:
                short = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][day_num]
                if short not in found_days:
                    found_days.append(short)
        if found_days:
            return ",".join(found_days)

        return "daily"  # Default

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add_alarm(self, time_str, alarm_type="morning", label="Alarm",
                  recurrence=None, sound_file=None):
        """Create an alarm. Returns confirmation string."""
        hour, minute = self._parse_alarm_time(time_str)
        if hour is None:
            return f"Couldn't understand the time '{time_str}'. Try '7am' or '6:30 AM'."

        if recurrence is None:
            recurrence = "daily" if alarm_type == "morning" else "once"

        alarm = Alarm(
            id=uuid.uuid4().hex[:8],
            label=label,
            hour=hour,
            minute=minute,
            alarm_type=alarm_type,
            recurrence=recurrence,
            sound_file=sound_file,
            morning_briefing=(alarm_type == "morning"),
        )
        with self._lock:
            self.alarms.append(alarm)
            self._save()

        time_display = f"{hour:02d}:{minute:02d}"
        # Convert to 12h for display
        ampm = "AM" if hour < 12 else "PM"
        h12 = hour % 12 or 12
        time_display = f"{h12}:{minute:02d} {ampm}"

        return f"Alarm set: {label} at {time_display} ({recurrence})."

    def remove_alarm(self, alarm_id):
        """Delete an alarm by ID."""
        with self._lock:
            before = len(self.alarms)
            self.alarms = [a for a in self.alarms if a.id != alarm_id]
            self._save()
            if len(self.alarms) < before:
                return "Alarm removed."
        return "Alarm not found."

    def list_alarms(self):
        """List all active alarms. Returns formatted string."""
        active = [a for a in self.alarms if a.active]
        if not active:
            return "No alarms set."
        lines = []
        for a in active:
            ampm = "AM" if a.hour < 12 else "PM"
            h12 = a.hour % 12 or 12
            lines.append(f"  {a.label}: {h12}:{a.minute:02d} {ampm} "
                         f"({a.recurrence}) [{a.id}]")
        return f"Active alarms ({len(active)}):\n" + "\n".join(lines)

    def toggle_alarm(self, alarm_id, active=None):
        """Enable/disable an alarm."""
        with self._lock:
            for a in self.alarms:
                if a.id == alarm_id:
                    a.active = active if active is not None else not a.active
                    self._save()
                    state = "enabled" if a.active else "disabled"
                    return f"Alarm '{a.label}' {state}."
        return "Alarm not found."

    # ------------------------------------------------------------------
    # Checker loop
    # ------------------------------------------------------------------

    def start_checker(self):
        """Start background alarm checker thread."""
        if self._running:
            return
        self._running = True
        self._checker_thread = threading.Thread(target=self._check_loop, daemon=True)
        self._checker_thread.start()
        logger.info("Alarm checker started")

    def stop_checker(self):
        """Stop the alarm checker."""
        self._running = False
        self._stop_alarm.set()

    def _check_loop(self):
        """Background loop — checks every 15s if an alarm should fire."""
        while self._running:
            try:
                now = datetime.now()
                with self._lock:
                    for alarm in self.alarms:
                        if not alarm.active:
                            continue
                        if not self._should_fire(alarm, now):
                            continue
                        # Don't double-fire (check within 60s window)
                        if time.time() - alarm.last_fired < 120:
                            continue
                        # Check day match
                        if not self._day_matches(alarm, now):
                            continue
                        alarm.last_fired = time.time()
                        self._save()
                        # Fire in separate thread to not block checker
                        threading.Thread(
                            target=self._fire_alarm, args=(alarm,), daemon=True
                        ).start()
            except Exception as e:
                logger.error(f"Alarm check error: {e}")
            time.sleep(15)

    def _should_fire(self, alarm, now):
        """Check if alarm time matches current time (within 1 minute window)."""
        return alarm.hour == now.hour and alarm.minute == now.minute

    def _day_matches(self, alarm, now):
        """Check if today matches the alarm's recurrence schedule."""
        weekday = now.weekday()  # 0=Mon, 6=Sun
        rec = alarm.recurrence

        if rec == "daily":
            return True
        if rec == "weekdays":
            return weekday < 5
        if rec == "weekends":
            return weekday >= 5
        if rec == "once":
            return True  # Will be deactivated after firing
        # Specific days: "mon,wed,fri" or single "sat"
        day_shorts = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        allowed = [d.strip() for d in rec.split(",")]
        if any(d in day_shorts for d in allowed):
            return day_shorts[weekday] in allowed
        return True

    # ------------------------------------------------------------------
    # Alarm firing — sound + voice dismiss + morning briefing
    # ------------------------------------------------------------------

    def _fire_alarm(self, alarm):
        """Fire an alarm: play sound, wait for voice dismiss, then briefing."""
        try:
            logger.info(f"Firing alarm: {alarm.label} ({alarm.alarm_type})")
            print(f"\n{'='*50}")
            print(f"  ALARM: {alarm.label}")
            print(f"{'='*50}")

            self._alarm_playing = True
            self._stop_alarm.clear()
            self._current_alarm = alarm

            # Suppress mic listening while alarm plays (prevents false triggers)
            try:
                from speech import set_audio_playing
                set_audio_playing(True)
            except ImportError:
                pass

            # Play alarm sound in loop until dismissed
            sound_thread = threading.Thread(
                target=self._play_alarm_sound, args=(alarm,), daemon=True
            )
            sound_thread.start()

            # Wait for voice dismissal (max 5 minutes, then auto-stop)
            self._stop_alarm.wait(timeout=300)

        except Exception as e:
            logger.error(f"Alarm fire error: {e}", exc_info=True)
        finally:
            self._alarm_playing = False
            self._current_alarm = None

            # Re-enable mic listening
            try:
                from speech import set_audio_playing
                set_audio_playing(False)
            except ImportError:
                pass

        # Deactivate one-time alarms
        if alarm.recurrence == "once":
            with self._lock:
                alarm.active = False
                self._save()

        # Morning briefing after dismiss
        if alarm.alarm_type == "morning" and alarm.morning_briefing:
            self._morning_briefing(alarm)

    def _play_alarm_sound(self, alarm):
        """Play alarm sound repeatedly until stopped."""
        try:
            # Determine sound file
            sound = alarm.sound_file
            if not sound or not os.path.isfile(sound):
                if alarm.alarm_type == "morning" and os.path.isfile(MORNING_SOUND):
                    sound = MORNING_SOUND
                elif os.path.isfile(DEFAULT_ALARM_SOUND):
                    sound = DEFAULT_ALARM_SOUND
                else:
                    sound = None

            while not self._stop_alarm.is_set():
                try:
                    if sound and os.path.isfile(sound):
                        # Play .wav file
                        winsound.PlaySound(sound, winsound.SND_FILENAME | winsound.SND_ASYNC)
                    else:
                        # Fallback: Windows system beep pattern
                        for freq in [800, 1000, 800, 1000, 1200]:
                            if self._stop_alarm.is_set():
                                break
                            winsound.Beep(freq, 300)
                            time.sleep(0.1)
                    # Wait between repeats
                    self._stop_alarm.wait(timeout=3)
                except Exception as e:
                    logger.debug(f"Alarm sound error: {e}")
                    time.sleep(2)
        except Exception as e:
            logger.error(f"Alarm sound thread error: {e}")
        finally:
            # Stop any playing sound
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass

    def dismiss_alarm(self, snooze_minutes=0):
        """Dismiss the currently ringing alarm. Called from voice input.

        Args:
            snooze_minutes: If > 0, snooze instead of dismiss.
        Returns:
            Response string.
        """
        if not self._alarm_playing:
            return None  # No alarm ringing

        alarm = self._current_alarm
        self._alarm_playing = False
        self._current_alarm = None
        self._stop_alarm.set()

        # Stop alarm sound immediately so TTS response is audible
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

        # Re-enable mic listening (alarm audio stopped)
        try:
            from speech import set_audio_playing
            set_audio_playing(False)
        except ImportError:
            pass

        if snooze_minutes > 0 and alarm:
            # Reschedule for snooze
            snooze_time = datetime.now() + timedelta(minutes=snooze_minutes)
            with self._lock:
                alarm.hour = snooze_time.hour
                alarm.minute = snooze_time.minute
                alarm.last_fired = 0  # Allow re-firing
                self._save()
            return f"Snoozed for {snooze_minutes} minutes."

        return "Alarm dismissed."

    @property
    def is_ringing(self):
        """Check if an alarm is currently ringing."""
        return self._alarm_playing

    # ------------------------------------------------------------------
    # Morning briefing — motivation + weather + news
    # ------------------------------------------------------------------

    def _morning_briefing(self, alarm):
        """Post-alarm morning briefing: motivation, weather, news."""
        logger.info("Starting morning briefing")
        parts = []

        # 1. LLM-generated motivation (unique every day)
        motivation = self._generate_motivation()
        if motivation:
            parts.append(motivation)

        # 2. Weather forecast for the day
        weather = self._get_morning_weather()
        if weather:
            parts.append(weather)

        # 3. News summary (top 3 headlines)
        news = self._get_morning_news()
        if news:
            parts.append(news)

        if not parts:
            parts.append("Good morning! Have a great day.")

        briefing = " ".join(parts)
        print(f"\n[Morning Briefing] {briefing}")

        if self.speak_fn:
            try:
                self.speak_fn(briefing)
            except Exception as e:
                logger.error(f"Morning briefing TTS error: {e}")

    def _generate_motivation(self):
        """Generate a unique motivational wake-up message using LLM."""
        if self.brain_ref and hasattr(self.brain_ref, 'quick_chat'):
            try:
                prompt = (
                    "Generate a short, unique, energizing morning motivation (1-2 sentences). "
                    "Be creative, warm, and inspiring. Never repeat the same message. "
                    "Don't start with 'Good morning' — just the motivation part. "
                    "Examples of tone: 'Today is a blank page — fill it with something amazing.' "
                    "or 'Every sunrise is an invitation to brighten someone's day, starting with yours.'"
                )
                result = self.brain_ref.quick_chat(prompt)
                if result and len(result) > 10:
                    return result.strip()
            except Exception as e:
                logger.debug(f"Motivation generation failed: {e}")

        # Fallback: rotating static motivations
        motivations = [
            "Every day is a fresh start. Make it count!",
            "You've got this. Today is going to be a great day.",
            "Rise and shine! The world is waiting for your energy.",
            "Small steps every day lead to big results.",
            "Today's possibilities are endless. Let's make things happen.",
            "You're stronger than you think. Let's crush it today.",
            "A new day, a new opportunity. What will you create?",
            "The best time to start is now. Let's go!",
        ]
        day_index = datetime.now().timetuple().tm_yday % len(motivations)
        return motivations[day_index]

    def _get_morning_weather(self):
        """Get weather forecast for the day."""
        try:
            from weather import get_current_weather, check_rain_alert
            weather = get_current_weather()
            rain = check_rain_alert()
            result = f"Today's weather: {weather}" if weather else ""
            if rain:
                result += f" {rain}"
            return result if result else None
        except Exception as e:
            logger.debug(f"Morning weather failed: {e}")
            return None

    def _get_morning_news(self):
        """Get LLM-summarized news for morning briefing.

        Fetches full article content (title + description), then asks the LLM
        to produce a real summary of what happened — not just rephrased titles.
        """
        try:
            from news import get_news_detailed, get_headlines

            # Get articles with descriptions for real summarization
            articles = get_news_detailed("general", count=5)

            if articles and self.brain_ref and hasattr(self.brain_ref, 'quick_chat'):
                content_lines = []
                for a in articles:
                    line = f"- {a['title']}"
                    if a.get("description"):
                        line += f": {a['description']}"
                    content_lines.append(line)
                news_content = "\n".join(content_lines)

                prompt = (
                    "Summarize the following news into exactly 2-3 SHORT sentences (max 50 words total). "
                    "Focus on what happened. Be conversational and concise — this is for a quick morning voice briefing.\n\n"
                    f"{news_content}"
                )
                try:
                    summary = self.brain_ref.quick_chat(prompt)
                    if summary and len(summary) > 20:
                        return f"Here's what's happening: {summary.strip()}"
                except Exception:
                    pass

            # Fallback: plain headlines
            headlines = get_headlines("general", count=3)
            if headlines:
                return "In the news: " + ". ".join(headlines[:3]) + "."
        except Exception as e:
            logger.debug(f"Morning news failed: {e}")
        return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self):
        """Save alarms to disk."""
        try:
            data = [asdict(a) for a in self.alarms]
            with open(ALARMS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            logger.error(f"Failed to save alarms: {e}")

    def _load(self):
        """Load alarms from disk."""
        if not os.path.isfile(ALARMS_FILE):
            return
        try:
            with open(ALARMS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data:
                self.alarms.append(Alarm(**d))
            logger.info(f"Loaded {len(self.alarms)} alarms")
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.error(f"Failed to load alarms: {e}")


# ===================================================================
# Module-level singleton
# ===================================================================

_default_manager: AlarmManager | None = None


def get_alarm_manager() -> AlarmManager | None:
    return _default_manager


def set_alarm_manager(mgr: AlarmManager):
    global _default_manager
    _default_manager = mgr
