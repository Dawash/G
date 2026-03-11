"""
Local calendar — JSON-backed event storage for G.

No external APIs or OAuth needed. Events stored in calendar.json.
Supports: today's events, upcoming events, adding new events,
deleting events, and importing from .ics files.

Format:
  {"events": [
      {"id": 1, "title": "Meeting", "date": "2026-03-10",
       "time": "14:00", "duration": "1h", "notes": ""}
  ]}
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_CALENDAR_FILE = os.path.join(os.path.dirname(__file__), "calendar.json")


def _load_data():
    """Load calendar data from JSON file."""
    if not os.path.exists(_CALENDAR_FILE):
        return {"events": []}
    try:
        with open(_CALENDAR_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "events" not in data:
            data["events"] = []
        return data
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Failed to load calendar: {e}")
        return {"events": []}


def _save_data(data):
    """Save calendar data to JSON file."""
    try:
        with open(_CALENDAR_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except IOError as e:
        logger.error(f"Failed to save calendar: {e}")


def _next_id(data):
    """Get the next available event ID."""
    if not data["events"]:
        return 1
    return max(e.get("id", 0) for e in data["events"]) + 1


def _parse_date(date_str):
    """Parse a date string flexibly.

    Supports: "2026-03-10", "today", "tomorrow", "monday", "march 15",
              "next week", "in 3 days", etc.
    """
    if not date_str:
        return datetime.now().strftime("%Y-%m-%d")

    date_str = date_str.strip().lower()

    if date_str == "today":
        return datetime.now().strftime("%Y-%m-%d")
    elif date_str == "tomorrow":
        return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # "in N days"
    m = re.match(r"in\s+(\d+)\s+days?", date_str)
    if m:
        days = int(m.group(1))
        return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

    # "next week" — next Monday
    if date_str == "next week":
        today = datetime.now()
        days_ahead = 7 - today.weekday()  # next Monday
        return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    # Day of week: "monday", "tuesday", etc.
    _DAYS = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    if date_str in _DAYS:
        today = datetime.now()
        target = _DAYS[date_str]
        days_ahead = (target - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7  # next occurrence
        return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    # "month day" — e.g. "march 15", "april 3"
    _MONTHS = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    m = re.match(r"(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?", date_str)
    if m and m.group(1) in _MONTHS:
        month = _MONTHS[m.group(1)]
        day = int(m.group(2))
        year = datetime.now().year
        try:
            dt = datetime(year, month, day)
            if dt.date() < datetime.now().date():
                dt = datetime(year + 1, month, day)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # ISO format "YYYY-MM-DD"
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass

    # Fallback: return as-is (the LLM might pass a valid date string)
    return date_str


def _parse_time(time_str):
    """Normalize a time string to HH:MM format.

    Supports: "14:00", "2pm", "2:30 pm", "morning", "afternoon", "evening".
    """
    if not time_str:
        return ""

    time_str = time_str.strip().lower()

    # Named periods
    _PERIODS = {
        "morning": "09:00", "afternoon": "14:00", "evening": "18:00",
        "night": "20:00", "noon": "12:00", "midnight": "00:00",
    }
    if time_str in _PERIODS:
        return _PERIODS[time_str]

    # "2pm", "2:30pm", "2:30 pm", "14:00"
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", time_str)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}"

    return time_str


def load_events():
    """Load all events from calendar.json."""
    data = _load_data()
    return data["events"]


def get_today_events():
    """Get events for today, sorted by time."""
    today = datetime.now().strftime("%Y-%m-%d")
    events = load_events()
    today_events = [e for e in events if e.get("date") == today]
    today_events.sort(key=lambda e: e.get("time", ""))
    return today_events


def get_upcoming(days=7):
    """Get events for the next N days, sorted by date and time."""
    today = datetime.now().date()
    end = today + timedelta(days=days)
    events = load_events()

    upcoming = []
    for e in events:
        try:
            event_date = datetime.strptime(e.get("date", ""), "%Y-%m-%d").date()
            if today <= event_date <= end:
                upcoming.append(e)
        except ValueError:
            continue

    upcoming.sort(key=lambda e: (e.get("date", ""), e.get("time", "")))
    return upcoming


def add_event(title, date=None, time=None, duration=None, notes=None):
    """Add a new event to the calendar.

    Args:
        title: Event title/description.
        date: Date string (flexible parsing: "today", "tomorrow", "march 15", etc.)
        time: Time string (flexible: "2pm", "14:00", "morning", etc.)
        duration: Duration string (e.g. "1h", "30m", "2 hours").
        notes: Optional notes.

    Returns:
        Confirmation string.
    """
    data = _load_data()
    event_id = _next_id(data)

    parsed_date = _parse_date(date)
    parsed_time = _parse_time(time) if time else ""

    event = {
        "id": event_id,
        "title": title,
        "date": parsed_date,
        "time": parsed_time,
        "duration": duration or "",
        "notes": notes or "",
    }
    data["events"].append(event)
    _save_data(data)

    # Build confirmation
    parts = [f"Added '{title}'"]
    try:
        dt = datetime.strptime(parsed_date, "%Y-%m-%d")
        parts.append(f"on {dt.strftime('%A, %B %d')}")
    except ValueError:
        parts.append(f"on {parsed_date}")
    if parsed_time:
        parts.append(f"at {parsed_time}")
    if duration:
        parts.append(f"({duration})")

    logger.info(f"Calendar: added event #{event_id}: {title} on {parsed_date}")
    return " ".join(parts) + "."


def delete_event(event_id=None, title=None):
    """Delete an event by ID or title.

    Args:
        event_id: Numeric event ID.
        title: Event title (fuzzy match).

    Returns:
        Confirmation string.
    """
    data = _load_data()
    if not data["events"]:
        return "No events to delete."

    removed = None
    if event_id is not None:
        try:
            eid = int(event_id)
            for i, e in enumerate(data["events"]):
                if e.get("id") == eid:
                    removed = data["events"].pop(i)
                    break
        except (ValueError, TypeError):
            pass

    if removed is None and title:
        title_lower = title.strip().lower()
        for i, e in enumerate(data["events"]):
            if title_lower in e.get("title", "").lower():
                removed = data["events"].pop(i)
                break

    if removed:
        _save_data(data)
        logger.info(f"Calendar: deleted event #{removed.get('id')}: {removed.get('title')}")
        return f"Deleted '{removed.get('title')}' from calendar."
    return "Event not found."


def format_events(events, label="Events"):
    """Format a list of events for spoken/text output."""
    if not events:
        return f"No {label.lower()} found."

    lines = [f"{label} ({len(events)}):"]
    for e in events:
        parts = []
        # Date
        try:
            dt = datetime.strptime(e.get("date", ""), "%Y-%m-%d")
            date_str = dt.strftime("%A, %B %d")
        except ValueError:
            date_str = e.get("date", "unknown date")

        time_str = e.get("time", "")
        title = e.get("title", "Untitled")
        duration = e.get("duration", "")

        line = f"  - {title}"
        if time_str:
            line += f" at {time_str}"
        if duration:
            line += f" ({duration})"
        # Only show date if not all same date
        if len(set(ev.get("date") for ev in events)) > 1:
            line += f" — {date_str}"

        lines.append(line)

    return "\n".join(lines)


def import_ics(filepath):
    """Import events from an .ics file (basic VEVENT parsing).

    Args:
        filepath: Path to .ics file.

    Returns:
        Number of events imported.
    """
    if not os.path.exists(filepath):
        return f"File not found: {filepath}"

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except IOError as e:
        return f"Error reading file: {e}"

    # Simple VEVENT parser
    events_imported = 0
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", content, re.DOTALL):
        title = ""
        date = ""
        time = ""
        duration = ""

        m = re.search(r"SUMMARY:(.+)", block)
        if m:
            title = m.group(1).strip()

        m = re.search(r"DTSTART[^:]*:(\d{8})T?(\d{4,6})?", block)
        if m:
            d = m.group(1)
            date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            if m.group(2):
                t = m.group(2)
                time = f"{t[:2]}:{t[2:4]}"

        m = re.search(r"DURATION:PT?(\d+H)?(\d+M)?", block)
        if m:
            h = m.group(1) or ""
            mins = m.group(2) or ""
            duration = (h.lower() + mins.lower()).strip() or ""

        if title and date:
            add_event(title, date, time, duration)
            events_imported += 1

    return f"Imported {events_imported} events from {os.path.basename(filepath)}."
