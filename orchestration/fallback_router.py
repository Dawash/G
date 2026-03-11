"""
Keyword-based fallback intent routing (when Brain is unavailable).

Extracted from: assistant.py::_build_action_map(), INSTANT_INTENTS

Responsibility:
  - Build action map: intent -> handler function
  - Define which intents can be handled instantly (no LLM)
  - Used when: Ollama is down, API key is dead, rate limited
"""

from datetime import datetime

from intent import (
    INTENT_SHUTDOWN, INTENT_RESTART, INTENT_CANCEL_SHUTDOWN, INTENT_SLEEP,
    INTENT_GOOGLE_SEARCH, INTENT_OPEN_APP, INTENT_CLOSE_APP, INTENT_MINIMIZE_APP,
    INTENT_WEATHER, INTENT_FORECAST, INTENT_TIME, INTENT_NEWS,
    INTENT_SET_REMINDER, INTENT_LIST_REMINDERS, INTENT_SNOOZE,
)

# Commands that can be handled INSTANTLY without the Brain (keyword match)
INSTANT_INTENTS = {
    INTENT_OPEN_APP, INTENT_CLOSE_APP, INTENT_MINIMIZE_APP,
    INTENT_WEATHER, INTENT_FORECAST, INTENT_TIME, INTENT_NEWS,
    INTENT_GOOGLE_SEARCH,
    INTENT_SHUTDOWN, INTENT_RESTART, INTENT_CANCEL_SHUTDOWN, INTENT_SLEEP,
    INTENT_SET_REMINDER, INTENT_LIST_REMINDERS, INTENT_SNOOZE,
}


def build_action_map(reminder_mgr, provider, memory, config):
    """Build the unified action registry for keyword-based fallback.

    Args:
        reminder_mgr: ReminderManager instance.
        provider: ChatProvider instance for fallback chat.
        memory: MemoryStore instance.
        config: Config dict.

    Returns:
        dict: Intent name -> handler function mapping.
    """
    from actions import (
        google_search, open_application, minimize_window, close_window,
        shutdown_computer, restart_computer, cancel_shutdown, sleep_computer,
    )
    from weather import get_current_weather, get_forecast
    from news import get_briefing

    def _handle_set_reminder(data):
        if not data:
            return "What should I remind you about?"
        if "|" in data:
            parts = data.split("|", 1)
            return reminder_mgr.add_reminder(parts[0].strip(), parts[1].strip())
        return reminder_mgr.add_reminder(data, "in 1 hour")

    def _handle_news(data):
        category = data if data else "general"
        return get_briefing(category)

    def _handle_snooze(data):
        minutes = int(data) if data and str(data).isdigit() else 10
        due = reminder_mgr.check_due()
        if due:
            return reminder_mgr.snooze_reminder(due[0].id, minutes)
        active = [r for r in reminder_mgr.reminders if r.active]
        if active:
            return reminder_mgr.snooze_reminder(active[-1].id, minutes)
        return "No active reminder to snooze."

    def _handle_self_test(_):
        from self_test import run_self_test
        return run_self_test()

    return {
        INTENT_SHUTDOWN: lambda _: shutdown_computer(),
        INTENT_RESTART: lambda _: restart_computer(),
        INTENT_CANCEL_SHUTDOWN: lambda _: cancel_shutdown(),
        INTENT_SLEEP: lambda _: sleep_computer(),
        INTENT_GOOGLE_SEARCH: lambda data: google_search(data),
        INTENT_OPEN_APP: lambda data: open_application(data),
        INTENT_CLOSE_APP: lambda data: close_window(data),
        INTENT_MINIMIZE_APP: lambda data: minimize_window(data),
        INTENT_WEATHER: lambda _: get_current_weather(),
        INTENT_FORECAST: lambda _: get_forecast(),
        INTENT_TIME: lambda _: f"It's {datetime.now().strftime('%A, %I:%M %p')}.",
        INTENT_NEWS: _handle_news,
        INTENT_SET_REMINDER: _handle_set_reminder,
        INTENT_LIST_REMINDERS: lambda _: reminder_mgr.list_active(),
        INTENT_SNOOZE: _handle_snooze,
        "self_test": _handle_self_test,
    }
