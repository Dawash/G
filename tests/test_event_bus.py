"""
Tests for core.event_bus and core.topics.

Run with:  python -m pytest tests/test_event_bus.py -v
"""
import threading
import time

import pytest

from core.event_bus import EventBus, Event, _matches
from core.topics import Topics


# ---------------------------------------------------------------------------
# _matches helper
# ---------------------------------------------------------------------------

class TestMatches:
    def test_exact_match(self):
        assert _matches("system.shutdown", "system.shutdown")

    def test_exact_no_match(self):
        assert not _matches("system.shutdown", "system.startup.complete")

    def test_global_wildcard(self):
        assert _matches("*", "anything.at.all")

    def test_suffix_wildcard(self):
        assert _matches("cognition.*", "cognition.tool.called")
        assert _matches("cognition.*", "cognition.intent.mode")
        assert not _matches("cognition.*", "perception.audio.speech")

    def test_fnmatch_prefix_wildcard(self):
        assert _matches("*.error", "cognition.tool.error")
        assert _matches("*.error", "system.loop.error")
        assert not _matches("*.error", "system.loop.idle")

    def test_no_wildcard_substring(self):
        # "system" should not match "system.shutdown"
        assert not _matches("system", "system.shutdown")


# ---------------------------------------------------------------------------
# EventBus core behaviour
# ---------------------------------------------------------------------------

class TestEventBus:
    def setup_method(self):
        # Fresh bus per test — avoids cross-test subscriber bleed
        self.bus = EventBus()

    def teardown_method(self):
        self.bus.shutdown()

    def test_subscribe_and_publish(self):
        received = []
        self.bus.subscribe(Topics.SPEECH_RECOGNIZED, lambda e: received.append(e))
        evt = self.bus.publish(Topics.SPEECH_RECOGNIZED, {"text": "hello"})
        assert len(received) == 1
        assert received[0].payload["text"] == "hello"
        assert received[0].topic == Topics.SPEECH_RECOGNIZED
        assert received[0].id == evt.id

    def test_event_id_increments(self):
        ids = []
        self.bus.subscribe("*", lambda e: ids.append(e.id))
        self.bus.publish("a", {})
        self.bus.publish("b", {})
        self.bus.publish("c", {})
        assert ids == [1, 2, 3]

    def test_event_timestamp(self):
        received = []
        self.bus.subscribe("*", lambda e: received.append(e.ts))
        before = time.time()
        self.bus.publish("x", {})
        after = time.time()
        assert len(received) == 1
        assert before <= received[0] <= after

    def test_no_subscribers_no_error(self):
        # Should silently succeed
        self.bus.publish("orphan.topic", {"key": "value"})

    def test_unsubscribe(self):
        received = []
        handler = lambda e: received.append(e)
        self.bus.subscribe("test.topic", handler)
        self.bus.unsubscribe("test.topic", handler)
        self.bus.publish("test.topic", {})
        assert received == []

    def test_multiple_subscribers_same_topic(self):
        counts = [0, 0]
        self.bus.subscribe("t", lambda e: counts.__setitem__(0, counts[0] + 1))
        self.bus.subscribe("t", lambda e: counts.__setitem__(1, counts[1] + 1))
        self.bus.publish("t", {})
        assert counts == [1, 1]

    def test_wildcard_subscription(self):
        received = []
        self.bus.subscribe("perception.*", lambda e: received.append(e.topic))
        self.bus.publish(Topics.SPEECH_RECOGNIZED, {})
        self.bus.publish(Topics.WAKE_WORD_DETECTED, {})
        self.bus.publish(Topics.RESPONSE_READY, {})  # should NOT match
        assert Topics.SPEECH_RECOGNIZED in received
        assert Topics.WAKE_WORD_DETECTED in received
        assert Topics.RESPONSE_READY not in received

    def test_global_wildcard_subscription(self):
        count = [0]
        self.bus.subscribe("*", lambda e: count.__setitem__(0, count[0] + 1))
        for i in range(5):
            self.bus.publish(f"topic.{i}", {})
        assert count[0] == 5

    def test_once_subscriber_fires_once(self):
        received = []
        self.bus.subscribe("evt", lambda e: received.append(e), once=True)
        self.bus.publish("evt", {})
        self.bus.publish("evt", {})
        self.bus.publish("evt", {})
        assert len(received) == 1

    def test_on_decorator(self):
        received = []

        @self.bus.on("deco.topic")
        def handler(e):
            received.append(e.payload)

        self.bus.publish("deco.topic", {"x": 42})
        assert received == [{"x": 42}]

    def test_on_decorator_once(self):
        count = [0]

        @self.bus.on("once.topic", once=True)
        def handler(e):
            count[0] += 1

        self.bus.publish("once.topic", {})
        self.bus.publish("once.topic", {})
        assert count[0] == 1

    def test_handler_exception_does_not_stop_bus(self):
        """A raising handler must not prevent subsequent handlers from firing."""
        second_called = [False]

        def bad_handler(e):
            raise RuntimeError("intentional test error")

        def good_handler(e):
            second_called[0] = True

        self.bus.subscribe("crash.test", bad_handler)
        self.bus.subscribe("crash.test", good_handler)
        self.bus.publish("crash.test", {})
        assert second_called[0]

    def test_source_field(self):
        received = []
        self.bus.subscribe("*", lambda e: received.append(e.source))
        self.bus.publish("t", {}, source="my_module")
        assert received[-1] == "my_module"


# ---------------------------------------------------------------------------
# Async subscribers
# ---------------------------------------------------------------------------

class TestAsyncSubscribers:
    def setup_method(self):
        self.bus = EventBus()

    def teardown_method(self):
        self.bus.shutdown()

    def test_async_subscriber_called(self):
        done = threading.Event()
        received = []

        def handler(e):
            received.append(e.payload)
            done.set()

        self.bus.subscribe("async.test", handler, run_async=True)
        self.bus.publish("async.test", {"msg": "hello"})
        assert done.wait(timeout=2.0), "Async handler never fired"
        assert received[0]["msg"] == "hello"

    def test_async_does_not_block_publisher(self):
        """Async subscriber with a 100ms sleep must not slow the publisher."""
        def slow_handler(e):
            time.sleep(0.1)

        self.bus.subscribe("slow.test", slow_handler, run_async=True)
        t0 = time.time()
        self.bus.publish("slow.test", {})
        elapsed = time.time() - t0
        # Publisher should return almost immediately, not wait for 100ms
        assert elapsed < 0.05, f"Publisher blocked for {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# wait_for
# ---------------------------------------------------------------------------

class TestWaitFor:
    def setup_method(self):
        self.bus = EventBus()

    def teardown_method(self):
        self.bus.shutdown()

    def test_wait_for_receives_event(self):
        def publish_after_delay():
            time.sleep(0.05)
            self.bus.publish("delayed.event", {"n": 99})

        threading.Thread(target=publish_after_delay, daemon=True).start()
        evt = self.bus.wait_for("delayed.event", timeout=2.0)
        assert evt is not None
        assert evt.payload["n"] == 99

    def test_wait_for_timeout_returns_none(self):
        evt = self.bus.wait_for("never.fires", timeout=0.1)
        assert evt is None

    def test_wait_for_wildcard(self):
        def publish_after_delay():
            time.sleep(0.05)
            self.bus.publish("wild.sub.event", {})

        threading.Thread(target=publish_after_delay, daemon=True).start()
        evt = self.bus.wait_for("wild.*", timeout=2.0)
        assert evt is not None
        assert evt.topic == "wild.sub.event"


# ---------------------------------------------------------------------------
# Topics constants sanity check
# ---------------------------------------------------------------------------

class TestTopics:
    def test_all_topics_are_strings(self):
        for name in dir(Topics):
            if name.startswith("_"):
                continue
            val = getattr(Topics, name)
            assert isinstance(val, str), f"Topics.{name} is not a string"

    def test_topics_follow_dot_convention(self):
        for name in dir(Topics):
            if name.startswith("_"):
                continue
            val = getattr(Topics, name)
            assert "." in val, f"Topics.{name} = '{val}' has no dot separator"

    def test_no_duplicate_values(self):
        vals = [getattr(Topics, n) for n in dir(Topics) if not n.startswith("_")]
        assert len(vals) == len(set(vals)), "Duplicate topic values found"
