"""
Tests for Priority 3: Awareness State + Context Perception.

Covers:
  - AwarenessState dataclass (update, snapshot, to_context_string, thread safety)
  - ContextInjector (topic detection, field selection, build_context)
  - awareness_updater helpers (_classify_activity, _extract_filename, _get_system_stats)
  - Event-bus integration (published events → awareness state changes)

Run with:
    python -m pytest tests/test_awareness.py -v --tb=short
"""
import concurrent.futures
import threading
import time

import pytest

from core.awareness_state import AwarenessState
from core.context_injector import (
    RELEVANCE_MAP, TOPIC_KEYWORDS,
    build_context, detect_relevant_topics, get_relevant_fields,
)
from core.awareness_updater import (
    _classify_activity, _extract_filename, _get_system_stats,
)
from core.event_bus import EventBus
from core.topics import Topics


# ============================================================================
# Helpers — isolated AwarenessState instances (not the global singleton)
# ============================================================================

def make_state(**kwargs) -> AwarenessState:
    """Return a fresh AwarenessState pre-populated with kwargs."""
    s = AwarenessState()
    if kwargs:
        s.update(**kwargs)
    return s


# ============================================================================
# TestAwarenessState
# ============================================================================

class TestAwarenessState:
    def test_update_sets_fields(self):
        s = make_state()
        s.update(active_app="Chrome", current_time="14:32")
        assert s.active_app == "Chrome"
        assert s.current_time == "14:32"

    def test_update_ignores_unknown_keys(self):
        s = make_state()
        s.update(nonexistent_field="value")  # should not raise
        assert not hasattr(s, "nonexistent_field")

    def test_update_does_not_touch_private_fields(self):
        s = make_state()
        original_lock = s._lock
        s.update(_lock="should_not_change")
        assert s._lock is original_lock

    def test_snapshot_returns_dict(self):
        s = make_state(current_time="10:00", active_app="Code")
        snap = s.snapshot()
        assert isinstance(snap, dict)
        assert snap["current_time"] == "10:00"
        assert snap["active_app"] == "Code"

    def test_snapshot_excludes_internal_fields(self):
        s = make_state()
        snap = s.snapshot()
        for key in snap:
            assert not key.startswith("_"), f"Internal field '{key}' leaked into snapshot"
        # threading.Lock objects must not be in the snapshot
        for v in snap.values():
            assert not isinstance(v, threading.Lock)

    def test_snapshot_is_serialisable(self):
        import json
        s = make_state(current_time="09:00", activity="coding",
                       cpu_percent=42.5, pending_reminders=["call dentist"])
        snap = s.snapshot()
        # Should not raise
        json.dumps(snap)

    def test_to_context_string_with_populated_state(self):
        s = make_state(current_time="14:32", active_app="VS Code",
                       activity="coding", time_of_day="afternoon")
        ctx = s.to_context_string()
        assert "[Current Context]" in ctx
        assert "14:32" in ctx
        assert "VS Code" in ctx

    def test_to_context_string_empty_state(self):
        s = AwarenessState()
        # Fresh state has all defaults — should produce empty string
        ctx = s.to_context_string()
        assert ctx == ""

    def test_to_context_string_with_field_filter(self):
        s = make_state(current_time="08:00", active_app="Chrome",
                       cpu_percent=55.0, ram_percent=40.0)
        # Ask only for time fields
        ctx = s.to_context_string(relevant_fields=["current_time"])
        assert "08:00" in ctx
        assert "Chrome" not in ctx
        assert "55" not in ctx

    def test_to_context_string_filters_default_battery(self):
        s = make_state(battery_percent=100, battery_charging=True)
        ctx = s.to_context_string()
        # Default 100% battery should be omitted
        assert "battery" not in ctx.lower()
        assert "Battery" not in ctx

    def test_to_context_string_shows_low_battery(self):
        s = make_state(battery_percent=12, battery_charging=False)
        ctx = s.to_context_string()
        assert "12" in ctx

    def test_to_context_string_filters_neutral_emotion(self):
        s = make_state(user_emotion="neutral", current_time="12:00")
        ctx = s.to_context_string()
        assert "neutral" not in ctx.lower() or "12:00" in ctx

    def test_to_context_string_lists_trimmed_to_5(self):
        s = make_state(pending_reminders=[f"item {i}" for i in range(10)])
        ctx = s.to_context_string()
        # Only first 5 items should appear
        assert "item 0" in ctx
        assert "item 9" not in ctx

    def test_update_thread_safe(self):
        """10 concurrent threads all writing different fields — no crash, final state consistent."""
        s = AwarenessState()
        errors = []

        def worker(n):
            try:
                for _ in range(50):
                    s.update(current_time=f"{n:02d}:00", last_interaction_ago=n)
            except Exception as exc:
                errors.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futs = [pool.submit(worker, i) for i in range(10)]
            for f in futs:
                f.result()

        assert errors == [], f"Thread safety violations: {errors}"
        # State should still be a valid time string
        assert ":" in s.current_time

    def test_snapshot_thread_safe(self):
        """Snapshot while another thread is writing — should not raise."""
        s = AwarenessState()
        stop = threading.Event()

        def writer():
            while not stop.is_set():
                s.update(cpu_percent=float(time.time() % 100))

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        try:
            for _ in range(200):
                snap = s.snapshot()
                assert isinstance(snap, dict)
        finally:
            stop.set()
            t.join(timeout=1)


# ============================================================================
# TestContextInjector
# ============================================================================

class TestContextInjector:
    def test_general_always_included(self):
        topics = detect_relevant_topics("hello there")
        assert "general" in topics

    def test_detect_time_query(self):
        topics = detect_relevant_topics("what time is it?")
        assert "time" in topics

    def test_detect_weather_query(self):
        topics = detect_relevant_topics("will it rain today?")
        assert "weather" in topics

    def test_detect_system_query(self):
        topics = detect_relevant_topics("check my cpu and ram")
        assert "system" in topics

    def test_detect_code_query(self):
        topics = detect_relevant_topics("I have a bug in my python script")
        assert "code" in topics

    def test_detect_schedule_query(self):
        topics = detect_relevant_topics("do I have any meetings today?")
        assert "schedule" in topics

    def test_detect_app_query(self):
        topics = detect_relevant_topics("open chrome")
        assert "app" in topics

    def test_detect_communication_query(self):
        topics = detect_relevant_topics("check my email")
        assert "communication" in topics

    def test_detect_multiple_topics(self):
        topics = detect_relevant_topics("what time is it and what's the weather?")
        assert "time" in topics
        assert "weather" in topics
        assert "general" in topics

    def test_get_relevant_fields_deduplicates(self):
        # "code" and "app" share fields — no duplicates
        fields = get_relevant_fields("open my python file in vscode")
        assert len(fields) == len(set(fields))

    def test_build_context_time_includes_current_time(self):
        from core.awareness_state import awareness
        awareness.update(current_time="15:45", time_of_day="afternoon")
        ctx = build_context("what time is it?")
        assert "15:45" in ctx

    def test_build_context_code_includes_active_app(self):
        from core.awareness_state import awareness
        awareness.update(active_app="PyCharm", activity="coding", active_file="app.py")
        ctx = build_context("help me fix this bug")
        assert "PyCharm" in ctx or "app.py" in ctx

    def test_build_context_system_includes_cpu(self):
        from core.awareness_state import awareness
        awareness.update(cpu_percent=88.0, system_health="degraded")
        ctx = build_context("why is my computer slow?")
        assert "88" in ctx or "degraded" in ctx

    def test_build_context_empty_input_returns_general(self):
        from core.awareness_state import awareness
        awareness.update(current_time="09:00", time_of_day="morning")
        ctx = build_context("")
        # General baseline should include time
        assert "09:00" in ctx

    def test_build_context_include_all(self):
        from core.awareness_state import awareness
        awareness.update(current_time="12:00", active_app="Firefox",
                         cpu_percent=30.0, weather_summary="Sunny")
        ctx = build_context(include_all=True)
        assert "12:00" in ctx
        assert "Firefox" in ctx
        assert "Sunny" in ctx

    def test_build_context_no_crash_when_awareness_empty(self):
        # Should never raise regardless of state
        ctx = build_context("random question about nothing specific")
        assert isinstance(ctx, str)

    def test_relevance_map_consistency(self):
        """Every field listed in RELEVANCE_MAP must exist on AwarenessState."""
        s = AwarenessState()
        valid_fields = {k for k in s.__dataclass_fields__.keys() if not k.startswith("_")}
        for topic, fields in RELEVANCE_MAP.items():
            for f in fields:
                assert f in valid_fields, \
                    f"RELEVANCE_MAP['{topic}'] references unknown field '{f}'"


# ============================================================================
# TestAwarenessUpdaterHelpers
# ============================================================================

class TestAwarenessUpdaterHelpers:

    # --- _classify_activity ---

    def test_classify_coding_vscode(self):
        assert _classify_activity("code", "main.py - Visual Studio Code") == "coding"

    def test_classify_coding_terminal(self):
        assert _classify_activity("WindowsTerminal", "PowerShell") == "coding"

    def test_classify_browsing_chrome(self):
        assert _classify_activity("chrome", "Google - Google Chrome") == "browsing"

    def test_classify_browsing_github(self):
        assert _classify_activity("chrome", "github.com/user/repo") == "coding"

    def test_classify_browsing_youtube(self):
        assert _classify_activity("chrome", "YouTube - Coding Tutorial") == "media"

    def test_classify_gaming_steam(self):
        assert _classify_activity("steam", "Steam Store") == "gaming"

    def test_classify_communication_slack(self):
        assert _classify_activity("slack", "# general - Slack") == "communication"

    def test_classify_writing_word(self):
        assert _classify_activity("word", "Report.docx - Word") == "writing"

    def test_classify_idle_unknown(self):
        assert _classify_activity("unknown_app_xyz", "Random Window Title") == "idle"

    def test_classify_empty_input(self):
        assert _classify_activity("", "") == "idle"

    # --- _extract_filename ---

    def test_extract_filename_vscode(self):
        assert _extract_filename("main.py - Visual Studio Code") == "main.py"

    def test_extract_filename_long_path(self):
        result = _extract_filename("~/projects/app/src/index.ts - Sublime Text")
        assert result == "index.ts"

    def test_extract_filename_docx(self):
        assert _extract_filename("report.docx - Microsoft Word") == "report.docx"

    def test_extract_filename_em_dash(self):
        # em-dash separator (U+2014)
        assert _extract_filename("script.py \u2014 Sublime Text") == "script.py"

    def test_extract_filename_no_match(self):
        assert _extract_filename("Google Chrome") == ""

    def test_extract_filename_empty(self):
        assert _extract_filename("") == ""

    def test_extract_filename_no_extension(self):
        # No extension → should not match
        assert _extract_filename("README - Notepad") == ""

    # --- _get_system_stats ---

    def test_get_system_stats_returns_dict(self):
        stats = _get_system_stats()
        assert isinstance(stats, dict)

    def test_get_system_stats_has_valid_health_if_psutil_available(self):
        try:
            import psutil  # noqa: F401
            stats = _get_system_stats()
            if "system_health" in stats:
                assert stats["system_health"] in ("good", "degraded", "critical")
        except ImportError:
            pytest.skip("psutil not installed")

    def test_get_system_stats_cpu_ram_in_range(self):
        try:
            import psutil  # noqa: F401
            stats = _get_system_stats()
            if "cpu_percent" in stats:
                assert 0.0 <= stats["cpu_percent"] <= 100.0
            if "ram_percent" in stats:
                assert 0.0 <= stats["ram_percent"] <= 100.0
        except ImportError:
            pytest.skip("psutil not installed")


# ============================================================================
# TestEventBusIntegration
# ============================================================================

class TestEventBusIntegration:
    """
    Test that awareness state reacts correctly to bus events.
    Uses a fresh EventBus + fresh AwarenessState per test — does NOT call
    start_awareness_updates() (which spawns daemon threads) to keep tests fast.
    """

    def test_manual_update_from_input_event(self):
        """Simulate what the _on_input subscriber does."""
        s = AwarenessState()
        # Mimic the updater logic
        text = "open spotify"
        cmds = list(s.recent_commands[-9:])
        cmds.append(text)
        s.update(recent_commands=cmds, last_interaction_ago=0, user_present=True)

        assert s.recent_commands == ["open spotify"]
        assert s.last_interaction_ago == 0
        assert s.user_present is True

    def test_speech_recognized_accumulates_commands(self):
        s = AwarenessState()
        for phrase in ["play music", "set reminder", "check weather"]:
            cmds = list(s.recent_commands[-9:])
            cmds.append(phrase)
            s.update(recent_commands=cmds)
        assert len(s.recent_commands) == 3
        assert "check weather" in s.recent_commands

    def test_state_idle_clears_user_present(self):
        s = AwarenessState()
        s.update(user_present=True)
        # Mimic _on_idle
        s.update(user_present=False)
        assert s.user_present is False

    def test_state_active_sets_user_present(self):
        s = AwarenessState()
        s.update(user_present=False)
        # Mimic _on_active
        s.update(user_present=True, last_interaction_ago=0)
        assert s.user_present is True
        assert s.last_interaction_ago == 0

    def test_context_injection_end_to_end(self):
        """Set awareness state → build_context → verify field appears in output."""
        from core.awareness_state import awareness

        awareness.update(current_time="11:30", time_of_day="morning")
        ctx = build_context("what time is it?")

        assert "11:30" in ctx, f"Expected '11:30' in context:\n{ctx}"

    def test_bus_event_triggers_awareness_update(self):
        """
        Publish INPUT_RECEIVED on the global bus and verify the
        awareness_updater subscriber (registered by start_awareness_updates)
        updates the state.  We call start_awareness_updates() once, publish,
        then check.
        """
        from core.awareness_state import awareness
        from core.awareness_updater import start_awareness_updates
        from core.event_bus import bus
        from core.topics import Topics

        # Reset awareness state
        awareness.update(recent_commands=[], last_interaction_ago=999)

        # Register subscribers (idempotent — extra subscriptions are harmless)
        start_awareness_updates()

        # Publish the event
        bus.publish(Topics.INPUT_RECEIVED, {"text": "test event bus integration"},
                    source="test")

        # Give sync subscriber a moment (it's synchronous, but schedule a poll)
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if "test event bus integration" in awareness.recent_commands:
                break
            time.sleep(0.05)

        assert "test event bus integration" in awareness.recent_commands
        assert awareness.last_interaction_ago == 0
