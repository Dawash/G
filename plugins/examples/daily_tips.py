"""
Daily Tips Plugin — example plugin showing the plugin architecture.

Demonstrates:
  - Intent-based matching (regex patterns)
  - LLM tool registration
  - Persistent memory (remembering water intake)
  - Proactive suggestions
"""

from plugins.base import BasePlugin, PluginIntent, PluginTool


class DailyTipsPlugin(BasePlugin):
    name = "daily_tips"
    description = "Health tips, water tracking, and daily motivation"
    version = "1.0"
    author = "G Assistant"

    def get_intents(self):
        return [
            PluginIntent(
                r"how (?:many|much) (?:glasses? of )?water (?:did i|have i|should i)",
                self.water_info,
                priority=60,
                description="Water intake tracking and advice",
            ),
            PluginIntent(
                r"(?:i )?(?:just )?drank (?:a |one |1 )?(?:glass|cup|bottle) of water",
                self.log_water,
                priority=60,
                description="Log water intake",
            ),
            PluginIntent(
                r"(?:give me|tell me) (?:a |some )?(?:health |daily |morning )?(?:tip|advice|motivation)",
                self.daily_tip,
                priority=40,
                description="Health tips and motivation",
            ),
        ]

    def get_tools(self):
        return [
            PluginTool(
                name="track_water",
                description="Log or check the user's daily water intake. "
                            "Action can be 'log' (add 1 glass) or 'check' (show count).",
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["log", "check"],
                            "description": "Whether to log a glass or check count",
                        },
                    },
                    "required": ["action"],
                },
                handler=self._handle_track_water,
            ),
        ]

    def water_info(self, text, match):
        """Handle water intake questions."""
        count = int(self.recall("water_today") or "0")
        goal = 8
        remaining = max(0, goal - count)
        if "should" in text.lower():
            return (f"You should drink about 8 glasses of water a day. "
                    f"You've had {count} so far today. {remaining} more to go!")
        return (f"You've had {count} glasses of water today. "
                f"{'Great job!' if count >= goal else f'{remaining} more to reach your daily goal of {goal}.'}")

    def log_water(self, text, match):
        """Log a glass of water."""
        count = int(self.recall("water_today") or "0") + 1
        self.remember("water_today", str(count))
        goal = 8
        if count >= goal:
            return f"Glass #{count} logged! You've reached your daily goal of {goal} glasses!"
        return f"Glass #{count} logged! {goal - count} more to reach your daily goal."

    def daily_tip(self, text, match):
        """Give a health tip, using LLM for variety."""
        tip = self.quick_chat(
            "Give ONE brief, practical health or productivity tip (1-2 sentences). "
            "Be specific and actionable, not generic. No markdown."
        )
        return tip or "Stay hydrated and take regular breaks from screens!"

    def _handle_track_water(self, arguments):
        """LLM tool handler for water tracking."""
        action = arguments.get("action", "check")
        if action == "log":
            count = int(self.recall("water_today") or "0") + 1
            self.remember("water_today", str(count))
            return f"Logged glass #{count}. {'Great job!' if count >= 8 else f'{8 - count} more to go.'}"
        else:
            count = int(self.recall("water_today") or "0")
            return f"You've had {count} glasses today. Goal: 8."

    def on_wake(self):
        """Reset daily counter at start of new day."""
        import datetime
        today = datetime.date.today().isoformat()
        last_date = self.recall("last_date")
        if last_date != today:
            self.remember("water_today", "0")
            self.remember("last_date", today)
            self.logger.info("Daily water counter reset")
