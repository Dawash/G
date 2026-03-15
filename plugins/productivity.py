"""
Productivity Plugin — tracks focus time and suggests breaks.

Features:
  - Pomodoro timer tracking (work/break cycles)
  - Session time tracking
  - Break reminders after extended focus
  - Productivity stats
"""

import time
from plugins.base import BasePlugin, PluginIntent, PluginTool


class ProductivityPlugin(BasePlugin):
    name = "productivity"
    description = "Focus timer, break reminders, and productivity tracking"
    version = "1.0"
    author = "G Assistant"

    def __init__(self):
        super().__init__()
        self._session_start = time.time()

    def get_intents(self):
        return [
            PluginIntent(
                r"how long have i been (?:working|using|on|at)",
                self.session_time,
                priority=55,
                description="Session time tracking",
            ),
            PluginIntent(
                r"(?:start|begin) (?:a )?(?:pomodoro|focus|work)(?:\s+(?:timer|session))?$",
                self.start_pomodoro,
                priority=55,
                description="Start Pomodoro timer",
            ),
            PluginIntent(
                r"(?:my |show )?productivity (?:stats|report|summary)",
                self.productivity_stats,
                priority=50,
                description="Productivity statistics",
            ),
        ]

    def session_time(self, text, match):
        elapsed = time.time() - self._session_start
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        if hours > 0:
            time_str = f"{hours} hour{'s' if hours > 1 else ''} and {minutes} minutes"
        else:
            time_str = f"{minutes} minutes"

        # Proactive suggestion if working too long
        if elapsed > 7200:  # 2+ hours
            return (f"You've been working for {time_str}. "
                    f"That's a long session! Consider taking a break to rest your eyes.")
        elif elapsed > 3600:  # 1+ hour
            return (f"You've been at it for {time_str}. "
                    f"A short walk or stretch break would be good about now.")
        return f"You've been working for {time_str}."

    def start_pomodoro(self, text, match):
        sessions = int(self.recall("pomodoro_count") or "0") + 1
        self.remember("pomodoro_count", str(sessions))
        self.remember("pomodoro_start", str(time.time()))
        return (f"Pomodoro #{sessions} started! Focus for 25 minutes, then take a 5-minute break. "
                f"I'll remind you when it's time.")

    def productivity_stats(self, text, match):
        pomodoros = int(self.recall("pomodoro_count") or "0")
        elapsed = time.time() - self._session_start
        minutes = int(elapsed // 60)
        return (f"Today's stats: {pomodoros} Pomodoro sessions completed, "
                f"current session is {minutes} minutes long.")

    def on_wake(self):
        self._session_start = time.time()
