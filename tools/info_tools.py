"""
Info tool registrations — read-only data retrieval tools.

Registers: get_forecast, get_time, get_news, list_reminders, web_read, web_search_answer, get_calendar
"""

import logging

from tools.schemas import ToolSpec
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


# ===================================================================
# Handler functions
# ===================================================================

def _handle_get_forecast(arguments):
    city = arguments.get("city", "") or None
    from weather import get_forecast
    return get_forecast(city)


def _handle_get_time(arguments, action_registry):
    if "time" in action_registry:
        return action_registry["time"](None)
    from datetime import datetime
    return f"It's {datetime.now().strftime('%A, %I:%M %p')}."


def _handle_get_news(arguments):
    category = arguments.get("category", "general")
    query = arguments.get("query", None)
    country = arguments.get("country", None)
    from news import get_briefing
    return get_briefing(category, query=query, country=country)


def _handle_list_reminders(arguments, action_registry, reminder_mgr=None):
    if reminder_mgr:
        return reminder_mgr.list_active()
    if "list_reminders" in action_registry:
        return action_registry["list_reminders"](None)
    try:
        from reminders import ReminderManager
        return ReminderManager().list_active()
    except Exception:
        return "Reminder system not available."


def _handle_read_clipboard(arguments):
    """Read clipboard content — text, URLs, or file paths."""
    try:
        import pyperclip
        clip = pyperclip.paste()
        if not clip or not clip.strip():
            return "Clipboard is empty."
        import re
        urls = re.findall(r'https?://[^\s<>"\']+', clip)
        if urls:
            url_info = f"Clipboard contains URL: {urls[0]}"
            if len(urls) > 1:
                url_info += f" (and {len(urls)-1} more URLs)"
            url_info += "\nTip: I can read this page for you with web_read."
            return url_info
        return f"Clipboard text ({len(clip)} chars): {clip[:1500]}"
    except Exception as e:
        return f"Could not read clipboard: {e}"


def _handle_analyze_clipboard_image(arguments):
    """Analyze an image from clipboard using vision (llava)."""
    try:
        from PIL import ImageGrab
        img = ImageGrab.grabclipboard()
        if img is None:
            return "No image in clipboard. The clipboard may contain text instead — try read_clipboard."
        from vision import analyze_screen
        question = arguments.get("question", "Describe what you see in this image in detail.")
        return analyze_screen(question, image=img)
    except ImportError:
        return "Pillow (PIL) is required for clipboard image analysis."
    except Exception as e:
        return f"Failed to analyze clipboard image: {e}"


def _handle_web_read(arguments):
    url = arguments.get("url", "")
    if not url:
        return "Error: no URL provided."
    try:
        from web_agent import web_read
        result = web_read(url)
        return result if result else f"Could not read content from {url}."
    except Exception as e:
        return f"Error reading {url}: {e}"


def _handle_web_search_answer(arguments):
    query = arguments.get("query", "")
    if not query:
        return "Error: no search query provided."
    try:
        from web_agent import web_search_extract
        result = web_search_extract(query)
        return result if result else f"No results found for '{query}'."
    except Exception as e:
        return f"Error searching for '{query}': {e}"


def _handle_get_calendar(arguments):
    action = arguments.get("action", "today").lower().strip()
    from calendar_local import (
        get_today_events, get_upcoming, add_event, delete_event, format_events,
    )

    if action == "today":
        events = get_today_events()
        return format_events(events, "Today's events")

    elif action == "upcoming":
        days = 7
        days_str = arguments.get("days", "")
        if days_str:
            try:
                days = int(days_str)
            except (ValueError, TypeError):
                pass
        events = get_upcoming(days)
        return format_events(events, f"Events in the next {days} days")

    elif action == "add":
        title = arguments.get("title", "")
        if not title:
            return "Error: event title is required."
        date = arguments.get("date", None)
        time = arguments.get("time", None)
        duration = arguments.get("duration", None)
        notes = arguments.get("notes", None)
        return add_event(title, date, time, duration, notes)

    elif action == "delete":
        event_id = arguments.get("event_id", None)
        title = arguments.get("title", None)
        if not event_id and not title:
            return "Error: provide event_id or title to delete."
        return delete_event(event_id, title)

    else:
        return f"Unknown calendar action: {action}. Use: today, upcoming, add, delete."


# ===================================================================
# Registration
# ===================================================================

def register_info_tools(registry: ToolRegistry):
    """Register read-only info tools into the registry."""

    registry.register(ToolSpec(
        name="get_forecast",
        description=(
            "Get weather forecast for coming days. Use for 'will it rain tomorrow', "
            "'weekend weather', 'forecast'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name (optional, defaults to current location)"
                }
            },
            "required": []
        },
        handler=_handle_get_forecast,
        cacheable=True,
        cache_ttl=300,
        aliases=["check_forecast", "weather_forecast", "forecast"],
        arg_aliases={"location": "city", "place": "city", "area": "city", "where": "city"},
        primary_arg="city",
        core=False,  # Cloud-only: get_weather already covers forecasts
    ))

    registry.register(ToolSpec(
        name="get_time",
        description=(
            "Get current time and date. ALWAYS use this tool for time/date queries "
            "— NEVER answer from memory or guess."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_handle_get_time,
        requires_registry=True,
        cacheable=True,
        cache_ttl=30,
        aliases=["check_time", "current_time", "get_date", "check_date",
                 "time", "find_time", "datetime"],
        core=True,
    ))

    registry.register(ToolSpec(
        name="get_news",
        description=(
            "Get latest news headlines. Use for 'what's in the news', 'tech news', "
            "'headlines'. Optional: category, query, country."
        ),
        parameters={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "News category: general, tech, sports, entertainment, science, business, health",
                    "enum": ["general", "tech", "sports", "entertainment",
                             "science", "business", "health"],
                },
                "query": {
                    "type": "string",
                    "description": "Search for specific news topic",
                },
                "country": {
                    "type": "string",
                    "description": "Country code (e.g. 'us', 'gb', 'in')",
                },
            },
            "required": []
        },
        handler=_handle_get_news,
        cacheable=True,
        cache_ttl=600,
        aliases=["check_news", "latest_news", "news", "headlines", "get_headlines"],
        core=True,
    ))

    registry.register(ToolSpec(
        name="list_reminders",
        description="Show all active reminders.",
        parameters={"type": "object", "properties": {}},
        handler=_handle_list_reminders,
        requires_registry=True,
        requires_reminder_mgr=True,
        aliases=["reminders", "show_reminders"],
    ))

    registry.register(ToolSpec(
        name="web_read",
        description=(
            "Fetch and read a web page's text content. Use this to get actual "
            "information from the web, not just open a browser."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to read"}
            },
            "required": ["url"]
        },
        handler=_handle_web_read,
        cacheable=True,
        cache_ttl=300,
        aliases=["read_web", "fetch", "read_url"],
        primary_arg="url",
    ))

    registry.register(ToolSpec(
        name="web_search_answer",
        description=(
            "Search the web and extract a direct answer. Use ONLY for factual questions "
            "like 'who is X', 'what is Y', 'how to Z'. DO NOT use for weather (use get_weather), "
            "time (use get_time), news (use get_news), or forecasts (use get_forecast)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"}
            },
            "required": ["query"]
        },
        handler=_handle_web_search_answer,
        cacheable=True,
        cache_ttl=120,
        aliases=["search_answer", "answer"],
        arg_aliases={"q": "query", "search": "query", "question": "query", "text": "query"},
        primary_arg="query",
    ))

    registry.register(ToolSpec(
        name="get_calendar",
        description=(
            "Manage local calendar events. Use for 'what's on my calendar', "
            "'any meetings today', 'add event', 'my schedule', 'upcoming events'. "
            "Actions: today (default), upcoming, add, delete."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Calendar action: today, upcoming, add, delete",
                    "enum": ["today", "upcoming", "add", "delete"],
                },
                "title": {
                    "type": "string",
                    "description": "Event title (required for add)",
                },
                "date": {
                    "type": "string",
                    "description": "Event date: 'today', 'tomorrow', 'monday', 'march 15', '2026-03-10'",
                },
                "time": {
                    "type": "string",
                    "description": "Event time: '2pm', '14:00', 'morning', 'afternoon'",
                },
                "duration": {
                    "type": "string",
                    "description": "Event duration: '1h', '30m', '2 hours'",
                },
                "days": {
                    "type": "string",
                    "description": "Number of days to look ahead (for upcoming action, default 7)",
                },
                "event_id": {
                    "type": "string",
                    "description": "Event ID to delete",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional event notes",
                },
            },
            "required": []
        },
        handler=_handle_get_calendar,
        cacheable=True,
        cache_ttl=60,
        aliases=["calendar", "schedule", "my_calendar", "check_calendar",
                 "events", "appointments", "meetings", "agenda"],
        arg_aliases={"name": "title", "event": "title", "description": "title",
                     "when": "date", "at": "time"},
        primary_arg="action",
        core=False,  # Cloud-only: complex multi-action tool confuses 7B model
    ))

    registry.register(ToolSpec(
        name="read_clipboard",
        description=(
            "Read the current clipboard content. Use when user says 'check my clipboard', "
            "'what did I copy', 'this link', 'read this'. Detects URLs, text, and file paths. "
            "If clipboard has a URL, consider using web_read on it next."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_handle_read_clipboard,
        aliases=["clipboard", "get_clipboard", "check_clipboard", "whats_in_clipboard",
                 "my_clipboard", "paste", "read_clip"],
        core=False,  # Cloud-only: clipboard auto-injected via ambient context
    ))

    registry.register(ToolSpec(
        name="analyze_clipboard_image",
        description=(
            "Analyze an image from the clipboard using vision. Use when user says "
            "'look at this screenshot', 'what's in this image', 'analyze this image', "
            "'I copied a screenshot'. Requires an image in clipboard."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What to look for in the image, e.g. 'what app is shown', 'read the text'"
                }
            },
            "required": []
        },
        handler=_handle_analyze_clipboard_image,
        aliases=["clipboard_image", "analyze_screenshot", "read_image", "describe_image",
                 "clipboard_screenshot"],
        primary_arg="question",
        core=False,  # Cloud-only: specialized, rarely used
    ))

    logger.info(f"Registered 9 info tools")
