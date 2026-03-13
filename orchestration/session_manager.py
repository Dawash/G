"""
Session lifecycle and startup management.

Extracted from: assistant.py (startup_greeting, _time_greeting, _check_battery,
                              auto-sleep logic, provider initialization)

Responsibility:
  - Startup greeting with parallel weather/rain/battery/reminders
  - Time-appropriate greeting
  - Battery check
  - Auto-sleep timeout constants and logic
  - Provider switch execution
"""

import os
import sys
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Auto-sleep timeouts
AUTO_SLEEP_SECONDS = 90        # Go to IDLE after 90s of silence
AUTO_SLEEP_AFTER_AGENT = 180   # Longer timeout after agent tasks


def time_greeting():
    """Time-appropriate greeting prefix."""
    hour = datetime.now().hour
    if hour < 12:
        return "Good morning"
    elif hour < 17:
        return "Good afternoon"
    elif hour < 21:
        return "Good evening"
    return "Hey"


def check_battery():
    """Check battery and warn if low. Returns message or None."""
    try:
        import psutil
        bat = psutil.sensors_battery()
        if bat and not bat.power_plugged and bat.percent < 20:
            return f"Battery is at {bat.percent}%."
    except Exception:
        pass
    return None


def _summarize_news(headlines):
    """Summarize news into a conversational briefing using LLM.

    Fetches full article details (title + description) and asks the LLM
    to produce an actual summary — not just rephrased headlines.
    Falls back to joined headlines if LLM is unavailable.
    """
    if not headlines:
        return None

    # Fetch detailed articles (title + description snippets)
    try:
        from news import get_news_detailed
        articles = get_news_detailed("general", count=5)
    except Exception:
        articles = None

    # Build rich content for LLM
    if articles:
        content_lines = []
        for a in articles:
            line = f"- {a['title']}"
            if a.get("description"):
                line += f": {a['description']}"
            content_lines.append(line)
        news_content = "\n".join(content_lines)
    else:
        news_content = "\n".join(f"- {h}" for h in headlines)

    # LLM summarization — use provider.chat() which takes a plain string
    try:
        from ai_providers import create_provider
        from config import load_config, DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL
        cfg = load_config()
        if cfg:
            provider = create_provider(
                cfg["provider"], cfg["api_key"],
                "You are a friendly news anchor giving a quick morning briefing. "
                "Be conversational and brief.",
                ollama_model=cfg.get("ollama_model", DEFAULT_OLLAMA_MODEL),
                ollama_url=cfg.get("ollama_url", DEFAULT_OLLAMA_URL))
            prompt = (
                "Summarize the following news into exactly 2-3 SHORT sentences (max 50 words total). "
                "Focus on what happened. Be conversational and concise — this is for a quick voice briefing.\n\n"
                f"{news_content}"
            )
            summary = provider.chat(prompt)
            if summary and len(summary) > 20:
                return f"Quick news update: {summary.strip()}"
    except Exception:
        pass

    # Fallback: plain headlines
    joined = ". ".join(headlines[:3])
    return f"In the news today: {joined}."


def startup_greeting(config, reminder_mgr, speak_fn, speak_async_fn):
    """Greet the user with time, weather, news, reminders, battery. Parallelized.

    Args:
        config: Loaded config dict.
        reminder_mgr: ReminderManager instance.
        speak_fn: Blocking speak function.
        speak_async_fn: Non-blocking speak function (for text mode).
    """
    from weather import get_current_weather, check_rain_alert

    uname = config["username"]
    ainame = config["ai_name"]
    current_time = datetime.now().strftime("%A, %I:%M %p")

    parts = [f"{time_greeting()} {uname}! {ainame} here. It's {current_time}."]

    # Fetch weather, rain, battery, and news in parallel (saves 3-4 seconds)
    weather_result = [None]
    rain_result = [None]
    battery_result = [None]
    news_result = [None]

    def _fetch_weather():
        try:
            weather_result[0] = get_current_weather()
        except Exception:
            pass

    def _fetch_rain():
        try:
            rain_result[0] = check_rain_alert()
        except Exception:
            pass

    def _fetch_battery():
        battery_result[0] = check_battery()

    def _fetch_news():
        try:
            from news import get_headlines
            headlines = get_headlines("general", count=5)
            if headlines:
                news_result[0] = headlines[:5]
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(_fetch_weather),
            pool.submit(_fetch_rain),
            pool.submit(_fetch_battery),
            pool.submit(_fetch_news),
        ]
        for future in futures:
            try:
                future.result(timeout=4)
            except Exception:
                pass

    if weather_result[0]:
        parts.append(weather_result[0])
    if rain_result[0]:
        parts.append(rain_result[0])
    if battery_result[0]:
        parts.append(battery_result[0])

    # News — summarized for natural speech
    if news_result[0]:
        summary = _summarize_news(news_result[0])
        if summary:
            parts.append(summary)

    active = [r for r in reminder_mgr.reminders if r.active]
    if active:
        parts.append(f"You have {len(active)} active reminder{'s' if len(active) > 1 else ''}.")

    # Check for reminders that fired while the assistant was offline
    try:
        missed = reminder_mgr.get_missed_reminders(max_age_hours=24)
        if missed:
            parts.append(f"You missed {len(missed)} reminder{'s' if len(missed) > 1 else ''} while I was offline:")
            for r in missed[:3]:
                parts.append(f"  \"{r.message}\"")
    except Exception:
        pass

    parts.append("What can I do for you?")

    greeting = " ".join(parts)
    print(f"\n{ainame}: {greeting}")
    sys.stdout.flush()

    if os.environ.get("G_INPUT_MODE", "").lower() == "text":
        speak_async_fn(greeting)
    else:
        speak_fn(greeting)


def should_auto_sleep(session_state, is_text_mode=False):
    """Check if the assistant should go to IDLE due to inactivity.

    Args:
        session_state: SessionState from core.state.
        is_text_mode: True if G_INPUT_MODE=text.

    Returns:
        True if should transition to IDLE.
    """
    if is_text_mode:
        return False
    timeout = AUTO_SLEEP_AFTER_AGENT if session_state.last_mode_was_agent else AUTO_SLEEP_SECONDS
    return session_state.idle_seconds() > timeout


def do_provider_switch(new_provider, config, brain_cls, action_map,
                       reminder_mgr, uname, ainame, system_prompt,
                       user_preferences=None):
    """Switch provider. Returns (message, new_brain) or None.

    Args:
        new_provider: Target provider name.
        config: Current config dict.
        brain_cls: Brain class (not instance).
        action_map: Action registry dict.
        reminder_mgr: ReminderManager instance.
        uname: Username.
        ainame: AI name.
        system_prompt: System prompt string.
    """
    from config import switch_provider, load_config, DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL

    if switch_provider(new_provider):
        cfg = load_config()
        ollama_model = cfg.get("ollama_model", DEFAULT_OLLAMA_MODEL)
        new_brain = brain_cls(
            provider_name=cfg["provider"], api_key=cfg["api_key"],
            username=uname, ainame=ainame,
            action_registry=action_map, reminder_mgr=reminder_mgr,
            ollama_model=ollama_model,
            user_preferences=user_preferences,
            ollama_url=cfg.get("ollama_url", DEFAULT_OLLAMA_URL),
        )
        resp = new_brain.quick_chat(
            f"You just switched AI providers to {new_provider}. "
            f"Greet the user '{uname}' briefly and confirm you're ready."
        )
        return (resp or f"Switched to {new_provider}!", new_brain)
    return None
