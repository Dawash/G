"""Calendar tool registrations — local calendar management.

Registers: add_calendar_event, get_calendar_events
"""

import logging
from tools.schemas import ToolSpec
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _handle_add_event(arguments, **kwargs):
    """Add a calendar event."""
    try:
        from calendar_local import add_event
        result = add_event(
            title=arguments.get("title", ""),
            date=arguments.get("date", ""),
            time=arguments.get("time", ""),
            duration=arguments.get("duration_minutes", 60),
        )
        return result or "Event added to calendar."
    except ImportError:
        return "Calendar module not available."
    except Exception as e:
        return f"Failed to add event: {e}"


def _handle_get_events(arguments, **kwargs):
    """Get upcoming calendar events."""
    try:
        from calendar_local import get_upcoming, format_events
        days = arguments.get("days_ahead", 7)
        events = get_upcoming(days=days)
        if events:
            return format_events(events)
        return "No upcoming events."
    except ImportError:
        return "Calendar module not available."
    except Exception as e:
        return f"Failed to get events: {e}"


def register_calendar_tools(registry: ToolRegistry):
    """Register calendar tools."""
    registry.register(ToolSpec(
        name="add_calendar_event",
        description="Add an event to the local calendar. Use for 'schedule a meeting', 'add event', 'put on my calendar'.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title"},
                "date": {"type": "string", "description": "Date (YYYY-MM-DD or natural like 'tomorrow')"},
                "time": {"type": "string", "description": "Time (HH:MM or natural like '3pm')"},
                "duration_minutes": {"type": "integer", "description": "Duration in minutes", "default": 60},
            },
            "required": ["title", "date"]
        },
        handler=_handle_add_event,
        aliases=["schedule_event", "create_event", "add_event", "calendar_add"],
        primary_arg="title",
    ))

    registry.register(ToolSpec(
        name="get_calendar_events",
        description="Get upcoming calendar events. Use for 'what's on my calendar', 'any events today', 'my schedule'.",
        parameters={
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "Days to look ahead", "default": 7},
            },
        },
        handler=_handle_get_events,
        aliases=["list_events", "calendar", "my_schedule", "upcoming_events"],
    ))

    logger.info("Registered 2 calendar tools")
