"""
Lightweight in-process publish/subscribe event bus.

Design goals
------------
* Zero new dependencies — pure Python (threading + queue).
* Thread-safe: publish() is safe to call from any thread.
* Wildcard subscriptions: subscribe("perception.*") matches all topics
  whose string starts with "perception.".
* Async subscribers: handlers decorated with run_async=True are dispatched
  to a background worker thread pool — they never block the publisher.
* Typed events: each event is an Event dataclass with topic, payload,
  timestamp, and an optional source label.
* Drop-in upgrade path: if you later want ZeroMQ for cross-process IPC,
  replace _dispatch() with a ZMQ socket send and add a receive loop.

Usage
-----
    from core.event_bus import bus
    from core.topics import Topics

    # Subscribe (sync — runs in publisher's thread)
    @bus.on(Topics.SPEECH_RECOGNIZED)
    def on_speech(event):
        print(event.payload["text"])

    # Subscribe (async — runs in background thread, non-blocking)
    @bus.on(Topics.TOOL_CALLED, run_async=True)
    def on_tool(event):
        log_to_dashboard(event)

    # Wildcard subscription
    bus.subscribe("cognition.*", my_logger)

    # Publish
    bus.publish(Topics.SPEECH_RECOGNIZED, {"text": "open youtube", "lang": "en"})

    # One-shot wait
    event = bus.wait_for(Topics.RESPONSE_READY, timeout=10.0)
"""

from __future__ import annotations

import fnmatch
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """An event published to the bus.

    Attributes:
        topic:   The topic string (e.g. "perception.audio.speech").
        payload: Arbitrary dict with event data.
        ts:      Unix timestamp (float) when the event was created.
        source:  Optional label identifying the publishing component.
        id:      Monotonically increasing integer per-bus instance.
    """
    topic: str
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    source: str = ""
    id: int = 0


# ---------------------------------------------------------------------------
# Subscriber record
# ---------------------------------------------------------------------------

@dataclass
class _Subscriber:
    pattern: str                        # exact topic or wildcard (e.g. "cognition.*")
    handler: Callable[[Event], None]
    run_async: bool = False             # True → dispatched to worker pool
    once: bool = False                  # True → auto-unsubscribe after first call


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class EventBus:
    """Thread-safe in-process pub/sub event bus.

    A single instance is shared across the application via the module-level
    ``bus`` singleton.  Import it with::

        from core.event_bus import bus
    """

    _MAX_ASYNC_WORKERS = 4
    _ASYNC_QUEUE_SIZE = 256

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: List[_Subscriber] = []
        self._event_counter = 0

        # Background worker pool for async subscribers
        self._async_queue: queue.Queue[Tuple[_Subscriber, Event]] = queue.Queue(
            maxsize=self._ASYNC_QUEUE_SIZE
        )
        self._workers: List[threading.Thread] = []
        self._running = True
        for i in range(self._MAX_ASYNC_WORKERS):
            t = threading.Thread(
                target=self._async_worker,
                name=f"event-bus-worker-{i}",
                daemon=True,
            )
            t.start()
            self._workers.append(t)

    # ------------------------------------------------------------------
    # Subscribe / Unsubscribe
    # ------------------------------------------------------------------

    def subscribe(
        self,
        pattern: str,
        handler: Callable[[Event], None],
        *,
        run_async: bool = False,
        once: bool = False,
    ) -> None:
        """Register a handler for a topic or wildcard pattern.

        Args:
            pattern:    Exact topic (e.g. "system.shutdown") or wildcard
                        (e.g. "cognition.*", "*").
            handler:    Callable that receives a single Event argument.
            run_async:  If True, dispatched to a background thread so the
                        publisher is never blocked.
            once:       If True, the handler is automatically removed after
                        the first matching event.
        """
        sub = _Subscriber(pattern=pattern, handler=handler,
                          run_async=run_async, once=once)
        with self._lock:
            self._subscribers.append(sub)

    def unsubscribe(self, pattern: str, handler: Callable[[Event], None]) -> None:
        """Remove a previously registered handler.

        Silently does nothing if the handler was not registered.
        """
        with self._lock:
            self._subscribers = [
                s for s in self._subscribers
                if not (s.pattern == pattern and s.handler is handler)
            ]

    def on(
        self,
        pattern: str,
        *,
        run_async: bool = False,
        once: bool = False,
    ) -> Callable:
        """Decorator shorthand for subscribe().

        Usage::

            @bus.on(Topics.SPEECH_RECOGNIZED)
            def handle_speech(event): ...
        """
        def decorator(fn: Callable[[Event], None]) -> Callable[[Event], None]:
            self.subscribe(pattern, fn, run_async=run_async, once=once)
            return fn
        return decorator

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def publish(
        self,
        topic: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        source: str = "",
    ) -> Event:
        """Publish an event to all matching subscribers.

        Sync subscribers are called in the publisher's thread (in subscription
        order).  Async subscribers are queued to the worker pool.

        Args:
            topic:   Topic string.
            payload: Optional dict with event data.
            source:  Optional label for debugging.

        Returns:
            The Event that was published.
        """
        with self._lock:
            self._event_counter += 1
            eid = self._event_counter
            # Snapshot subscribers to avoid holding the lock during dispatch
            subs = list(self._subscribers)

        event = Event(
            topic=topic,
            payload=payload or {},
            ts=time.time(),
            source=source,
            id=eid,
        )

        once_remove: List[_Subscriber] = []

        for sub in subs:
            if not _matches(sub.pattern, topic):
                continue
            if sub.once:
                once_remove.append(sub)
            if sub.run_async:
                try:
                    self._async_queue.put_nowait((sub, event))
                except queue.Full:
                    logger.warning(
                        "event-bus async queue full — dropped %s handler for %s",
                        sub.handler.__name__, topic,
                    )
            else:
                try:
                    sub.handler(event)
                except Exception:
                    logger.exception(
                        "event-bus sync handler %s raised on topic %s",
                        sub.handler.__name__, topic,
                    )

        if once_remove:
            with self._lock:
                for sub in once_remove:
                    try:
                        self._subscribers.remove(sub)
                    except ValueError:
                        pass

        logger.debug("bus.publish %s (id=%d src=%s)", topic, eid, source or "-")
        return event

    # ------------------------------------------------------------------
    # Wait for a single event
    # ------------------------------------------------------------------

    def wait_for(
        self,
        pattern: str,
        timeout: Optional[float] = None,
    ) -> Optional[Event]:
        """Block until an event matching pattern arrives, then return it.

        Args:
            pattern: Exact topic or wildcard.
            timeout: Seconds to wait; None = wait forever.

        Returns:
            The matching Event, or None on timeout.
        """
        result: List[Optional[Event]] = [None]
        arrived = threading.Event()

        def _handler(ev: Event) -> None:
            result[0] = ev
            arrived.set()

        self.subscribe(pattern, _handler, once=True)
        arrived.wait(timeout=timeout)

        if result[0] is None:
            # Timeout — clean up the subscriber
            self.unsubscribe(pattern, _handler)
        return result[0]

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Signal worker threads to exit.  Call on application shutdown."""
        self._running = False
        # Unblock workers
        for _ in self._workers:
            try:
                self._async_queue.put_nowait((None, None))  # type: ignore[arg-type]
            except queue.Full:
                pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _async_worker(self) -> None:
        while self._running:
            try:
                item = self._async_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item == (None, None):
                break
            sub, event = item
            try:
                sub.handler(event)
            except Exception:
                logger.exception(
                    "event-bus async handler %s raised on topic %s",
                    sub.handler.__name__, event.topic,
                )
            finally:
                self._async_queue.task_done()

    def __repr__(self) -> str:
        with self._lock:
            n = len(self._subscribers)
        return f"EventBus(subscribers={n}, events_published={self._event_counter})"


# ---------------------------------------------------------------------------
# Wildcard matching
# ---------------------------------------------------------------------------

def _matches(pattern: str, topic: str) -> bool:
    """Return True if topic matches the subscription pattern.

    Supports:
      - Exact match:      "system.shutdown" matches "system.shutdown"
      - Suffix wildcard:  "cognition.*"     matches "cognition.tool.called"
      - Global wildcard:  "*"               matches everything
      - fnmatch patterns: "*.error"         matches "cognition.tool.error"
    """
    if pattern == "*":
        return True
    if "*" not in pattern:
        return pattern == topic
    return fnmatch.fnmatch(topic, pattern)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

bus = EventBus()
