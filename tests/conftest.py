"""Shared fixtures for G_v0 test suite."""

import os
import sys
import pytest

# Ensure project root is on sys.path so imports work
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def dummy_action_registry():
    """Minimal action registry with no-op handlers for testing."""
    return {
        "open_app": lambda name: f"Opened {name}",
        "close_app": lambda name: f"Closed {name}",
        "google_search": lambda query: f"Searched for {query}",
        "get_weather": lambda **kw: "72F, sunny",
        "get_time": lambda: "3:45 PM",
        "get_news": lambda **kw: "Top news headlines",
        "toggle_setting": lambda setting, state: f"{setting} turned {state}",
        "take_screenshot": lambda: "Screenshot saved",
        "set_reminder": lambda message, time: f"Reminder set: {message}",
    }


@pytest.fixture
def reminder_manager(tmp_path):
    """ReminderManager that writes to a temp file (no side effects)."""
    import reminders as rem_module
    # Temporarily override REMINDERS_FILE to avoid touching real data
    original = rem_module.REMINDERS_FILE
    rem_module.REMINDERS_FILE = str(tmp_path / "test_reminders.json")
    mgr = rem_module.ReminderManager(speak_fn=None, check_interval=9999)
    yield mgr
    rem_module.REMINDERS_FILE = original
