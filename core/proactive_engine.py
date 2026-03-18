"""
Proactive Intelligence Engine — monitors everything, suggests before asked.

The engine runs a background evaluation loop every 10 seconds that:
1. Evaluates all registered triggers against the current AwarenessState snapshot
2. Ranks suggestions by urgency, relevance, and user acceptance history
3. Delivers suggestions through the right channel (voice, HUD, log)

Triggers are pluggable — subclass BaseTrigger, implement should_fire(), register.

Usage:
    from core.proactive_engine import proactive_engine
    proactive_engine.start()   # call once at startup; runs autonomously

    # Suggestions are published to the event bus AND optionally queued:
    pending = proactive_engine.get_pending_suggestion()  # call between interactions
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.timeouts import Timeouts

logger = logging.getLogger(__name__)


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class Suggestion:
    """A single proactive suggestion to present to the user."""

    trigger_id: str
    """Unique ID of the trigger that fired (e.g. ``"battery_low"``)."""

    message: str
    """The suggestion text to speak or display."""

    urgency: int
    """Base urgency score 0-100 (before ranking adjustments)."""

    category: str
    """One of: warning / reminder / suggestion / info / automation."""

    action: Optional[str] = None
    """Optional action key to execute if user accepts (e.g. ``"enable_power_saver"``)."""

    action_args: Dict[str, Any] = field(default_factory=dict)
    """Arguments for the action."""

    expires_in: int = 300
    """Seconds before this suggestion becomes stale."""

    created_at: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        """True if the suggestion is older than expires_in seconds."""
        return (time.time() - self.created_at) > self.expires_in


# ============================================================================
# Base trigger
# ============================================================================

class BaseTrigger:
    """Base class for all proactive triggers.

    Subclass and implement :meth:`should_fire` to create a new trigger.
    Register with ``proactive_engine.register_trigger(MyTrigger())``.

    Attributes:
        id:               Unique trigger identifier string.
        category:         warning / reminder / suggestion / info / automation.
        cooldown_seconds: Minimum seconds between consecutive firings.
        base_urgency:     Default urgency score for suggestions from this trigger.
    """

    id: str = "base"
    category: str = "info"
    cooldown_seconds: int = Timeouts.PROACTIVE_MIN_INTERVAL
    base_urgency: int = 50

    def __init__(self) -> None:
        self._last_fired: float = 0.0
        self._fire_count: int = 0
        self._accept_count: int = 0
        self._reject_count: int = 0

    def should_fire(self, state: Dict[str, Any]) -> Optional[Suggestion]:
        """Evaluate whether this trigger should fire given the current awareness state.

        Args:
            state: :meth:`AwarenessState.snapshot` dict.

        Returns:
            A :class:`Suggestion` if the trigger should fire, ``None`` otherwise.
        """
        raise NotImplementedError

    def can_fire(self) -> bool:
        """Return True if the cooldown period has elapsed since last firing."""
        return (time.time() - self._last_fired) >= self.cooldown_seconds

    def mark_fired(self) -> None:
        """Record that this trigger just fired."""
        self._last_fired = time.time()
        self._fire_count += 1

    def mark_accepted(self) -> None:
        """Record that the user acted on the suggestion."""
        self._accept_count += 1

    def mark_rejected(self) -> None:
        """Record that the user dismissed or ignored the suggestion."""
        self._reject_count += 1

    @property
    def acceptance_rate(self) -> float:
        """Historical acceptance rate 0.0–1.0. Returns 0.5 when no data."""
        total = self._accept_count + self._reject_count
        if total == 0:
            return 0.5
        return self._accept_count / total


# ============================================================================
# Ranker
# ============================================================================

class SuggestionRanker:
    """Adjusts a suggestion's urgency score based on context and history.

    Final score factors:
    - Base urgency from the trigger (0–100)
    - Acceptance history bonus/penalty (±15)
    - User-busy penalty (video-call: –15, coding non-warning: –10)
    - Night-time penalty for non-urgent suggestions (–10)
    """

    MIN_SCORE: int = 40
    SPEAK_THRESHOLD: int = 70
    URGENT_THRESHOLD: int = 90
    MAX_PER_INTERVAL: int = 1

    def rank(self, suggestion: Suggestion, trigger: BaseTrigger,
             state: Dict[str, Any]) -> int:
        """Return a final score 0–100 for the suggestion."""
        score = suggestion.urgency

        # Acceptance history
        rate = trigger.acceptance_rate
        total_interactions = trigger._accept_count + trigger._reject_count
        if rate > 0.7:
            score += 15
        elif rate < 0.3 and total_interactions >= 3:
            score -= 15

        # Activity-based penalty
        activity = state.get("activity", "idle")
        if activity in ("video-call", "gaming"):
            score -= 15
        elif activity == "coding" and suggestion.category != "warning":
            score -= 10

        # Night-time penalty for non-critical suggestions
        tod = state.get("time_of_day", "morning")
        if tod == "night" and suggestion.category not in ("warning", "reminder"):
            score -= 10

        return max(0, min(100, score))


# ============================================================================
# Delivery strategy
# ============================================================================

class DeliveryStrategy:
    """Converts a score into a delivery channel."""

    @staticmethod
    def determine(score: int) -> str:
        """Return delivery method string.

        Returns:
            ``"speak_now"``      — score ≥ 90: speak immediately
            ``"speak_at_pause"`` — score 70–89: queue for next interaction gap
            ``"hud_only"``       — score 40–69: show on HUD / console only
            ``"log_only"``       — score < 40: just log for daily summary
        """
        if score >= 90:
            return "speak_now"
        if score >= 70:
            return "speak_at_pause"
        if score >= 40:
            return "hud_only"
        return "log_only"


# ============================================================================
# Engine
# ============================================================================

class ProactiveEngine:
    """Background engine that continuously evaluates triggers and surfaces suggestions.

    Start once at startup::

        from core.proactive_engine import proactive_engine
        proactive_engine.start()

    The engine runs autonomously in a daemon thread.  At most one suggestion
    is delivered per evaluation cycle to prevent suggestion storms.
    """

    _EVAL_INTERVAL: int = Timeouts.PROACTIVE_EVAL_INTERVAL
    _STARTUP_DELAY: int = Timeouts.PROACTIVE_STARTUP_DELAY
    _MAX_PENDING: int = 3      # max queued speak_at_pause suggestions
    _MAX_HISTORY: int = 50     # max entries in suggestion_history

    def __init__(self) -> None:
        self._triggers: List[BaseTrigger] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._pending_suggestions: List[Dict[str, Any]] = []
        self._suggestion_history: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._ranker = SuggestionRanker()
        self._delivery = DeliveryStrategy()

    # ── Registration ────────────────────────────────────────────────────────

    def register_trigger(self, trigger: BaseTrigger) -> None:
        """Register a trigger for evaluation.  Ignores duplicates by id."""
        with self._lock:
            if not any(t.id == trigger.id for t in self._triggers):
                self._triggers.append(trigger)
                logger.debug("Proactive: registered trigger '%s'", trigger.id)

    def unregister_trigger(self, trigger_id: str) -> None:
        """Remove a trigger by ID."""
        with self._lock:
            self._triggers = [t for t in self._triggers if t.id != trigger_id]

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background evaluation loop (idempotent)."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._evaluation_loop,
            daemon=True,
            name="proactive-engine",
        )
        self._thread.start()
        logger.info("Proactive engine started")

    def stop(self) -> None:
        """Signal the evaluation loop to stop."""
        self._running = False

    # ── Delivery interface (called from the main loop) ────────────────────────

    def get_pending_suggestion(self) -> Optional[str]:
        """Pop the next queued ``speak_at_pause`` suggestion.

        Called by the main loop between user interactions.

        Returns:
            Suggestion message string, or ``None`` if the queue is empty.
        """
        with self._lock:
            if self._pending_suggestions:
                item = self._pending_suggestions.pop(0)
                return item.get("message", "")
        return None

    def mark_suggestion_accepted(self, trigger_id: str) -> None:
        """Record that the user acted on a suggestion from this trigger."""
        with self._lock:
            for t in self._triggers:
                if t.id == trigger_id:
                    t.mark_accepted()
                    break

    def mark_suggestion_rejected(self, trigger_id: str) -> None:
        """Record that the user dismissed or ignored a suggestion from this trigger."""
        with self._lock:
            for t in self._triggers:
                if t.id == trigger_id:
                    t.mark_rejected()
                    break

    # ── Persistence ──────────────────────────────────────────────────────────

    def save_state(self, filepath: str = "data/proactive_state.json") -> None:
        """Persist trigger statistics to disk so acceptance rates survive restarts."""
        import json
        import os
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        state: Dict[str, Any] = {}
        with self._lock:
            for t in self._triggers:
                state[t.id] = {
                    "fire_count": t._fire_count,
                    "accept_count": t._accept_count,
                    "reject_count": t._reject_count,
                }
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            logger.debug("Saved proactive state for %d triggers", len(state))
        except Exception as exc:
            logger.debug("Failed to save proactive state: %s", exc)

    def load_state(self, filepath: str = "data/proactive_state.json") -> None:
        """Restore trigger statistics from disk."""
        import json
        try:
            with open(filepath, encoding="utf-8") as f:
                state = json.load(f)
            with self._lock:
                for t in self._triggers:
                    if t.id in state:
                        s = state[t.id]
                        t._fire_count = s.get("fire_count", 0)
                        t._accept_count = s.get("accept_count", 0)
                        t._reject_count = s.get("reject_count", 0)
            logger.debug("Loaded proactive state for %d triggers", len(state))
        except FileNotFoundError:
            pass  # First run
        except Exception as exc:
            logger.debug("Failed to load proactive state: %s", exc)

    # ── Internal evaluation ──────────────────────────────────────────────────

    def _evaluation_loop(self) -> None:
        """Background daemon loop: evaluates all triggers every N seconds."""
        time.sleep(self._STARTUP_DELAY)
        while self._running:
            try:
                self._evaluate_all()
            except Exception as exc:
                logger.debug("Proactive evaluation error: %s", exc)
            time.sleep(self._EVAL_INTERVAL)

    def _evaluate_all(self) -> None:
        """Evaluate all triggers against the current awareness snapshot."""
        from core.awareness_state import awareness
        state = awareness.snapshot()

        with self._lock:
            triggers_copy = list(self._triggers)

        fired: List[Dict[str, Any]] = []

        for trigger in triggers_copy:
            try:
                if not trigger.can_fire():
                    continue
                suggestion = trigger.should_fire(state)
                if suggestion is None:
                    continue
                score = self._ranker.rank(suggestion, trigger, state)
                delivery = self._delivery.determine(score)
                trigger.mark_fired()
                fired.append({
                    "trigger_id": trigger.id,
                    "message": suggestion.message,
                    "score": score,
                    "delivery": delivery,
                    "category": suggestion.category,
                    "action": suggestion.action,
                    "action_args": suggestion.action_args,
                    "timestamp": time.time(),
                })
            except Exception as exc:
                logger.debug("Trigger '%s' error: %s", trigger.id, exc)

        if not fired:
            return

        # Only deliver the single highest-scoring suggestion per cycle
        fired.sort(key=lambda x: x["score"], reverse=True)
        top = fired[: self._ranker.MAX_PER_INTERVAL]
        for item in top:
            self._deliver(item)

    def _deliver(self, item: Dict[str, Any]) -> None:
        """Route a suggestion to the appropriate channel."""
        from core.event_bus import bus

        # Record in history
        with self._lock:
            self._suggestion_history.append(item)
            if len(self._suggestion_history) > self._MAX_HISTORY:
                self._suggestion_history = self._suggestion_history[-self._MAX_HISTORY:]

        # Always publish to bus (HUD, dashboard, logging subscribers)
        try:
            bus.publish("proactive.suggestion", item, source="proactive_engine")
        except Exception as e:
            logger.debug("proactive_engine: bus publish proactive.suggestion failed: %s", e)

        delivery = item["delivery"]

        if delivery == "speak_now":
            try:
                bus.publish("proactive.speak_now",
                            {"message": item["message"], "trigger_id": item["trigger_id"]},
                            source="proactive_engine")
            except Exception as e:
                logger.debug("proactive_engine: bus publish proactive.speak_now failed: %s", e)
            logger.info("Proactive [SPEAK NOW]: %s", item["message"])

        elif delivery == "speak_at_pause":
            with self._lock:
                if len(self._pending_suggestions) < self._MAX_PENDING:
                    self._pending_suggestions.append(item)
            logger.info("Proactive [queued]: %s", item["message"])

        elif delivery == "hud_only":
            logger.info("Proactive [HUD]: %s", item["message"])

        else:
            logger.debug("Proactive [logged]: %s", item["message"])


# ============================================================================
# Module-level singleton
# ============================================================================

#: Global singleton — import from anywhere:
#:   from core.proactive_engine import proactive_engine
proactive_engine = ProactiveEngine()
