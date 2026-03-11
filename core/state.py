"""
Centralized runtime state management.

Replaces: Module-level globals across brain.py, assistant.py, speech.py, ai_providers.py

All mutable runtime state lives here in Lock-protected dataclass containers.
No module-level mutation — state only changes through methods.

Migration target for ~42 globals + 8 locks scattered across 5 files.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Assistant / Session State
# Replaces: assistant.py _assistant_state, _last_activity_time, _emergency_stop,
#           _last_response, _last_action, _last_mode_was_agent, _dead_key_warned
# ---------------------------------------------------------------------------

@dataclass
class SessionState:
    """Tracks assistant session lifecycle and meta-command state."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # State machine
    mode: str = "ACTIVE"                          # "IDLE" | "ACTIVE"
    last_activity: float = field(default_factory=time.time)
    auto_sleep_seconds: int = 90
    auto_sleep_after_agent: int = 180
    last_mode_was_agent: bool = False

    # Meta-command support
    last_response: Optional[str] = None
    last_action: Optional[tuple] = None           # (tool_name, args)
    last_user_input: Optional[str] = None

    # Flags
    emergency_stop: bool = False
    dead_key_warned: bool = False
    offline_mode: bool = False

    def touch(self) -> None:
        """Update last activity timestamp."""
        with self._lock:
            self.last_activity = time.time()

    def idle_seconds(self) -> float:
        with self._lock:
            return time.time() - self.last_activity

    def trigger_emergency_stop(self) -> None:
        with self._lock:
            self.emergency_stop = True

    def clear_emergency_stop(self) -> None:
        with self._lock:
            self.emergency_stop = False

    def is_emergency_stopped(self) -> bool:
        with self._lock:
            return self.emergency_stop

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self.mode = mode
            if mode == "ACTIVE":
                self.last_activity = time.time()

    def get_mode(self) -> str:
        with self._lock:
            return self.mode


# ---------------------------------------------------------------------------
# Brain / Tool Execution State
# Replaces: brain.py _undo_stack, _recent_actions, _last_created_file,
#           _response_cache, _escalation_depth, _last_escalation,
#           _experience_learner, _dynamic_tools, _action_log
# ---------------------------------------------------------------------------

@dataclass
class BrainState:
    """Tracks LLM brain execution state: undo, cache, escalation, actions."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # Undo
    undo_stack: list = field(default_factory=list)
    max_undo: int = 10

    # Recent actions for "do that again"
    recent_actions: list = field(default_factory=list)
    max_recent: int = 5

    # File tracking
    last_created_file: Optional[str] = None

    # Response cache: {cache_key: (result, timestamp)}
    response_cache: dict = field(default_factory=dict)

    # Escalation control
    escalation_depth: int = 0
    max_escalation_depth: int = 2
    last_escalation: dict = field(default_factory=dict)  # {(tool, query): timestamp}
    escalation_cooldown: float = 60.0

    # Dynamic tools
    dynamic_tools: dict = field(default_factory=dict)

    # Action audit log
    action_log: list = field(default_factory=list)
    max_log: int = 200

    # Experience learner reference (optional)
    experience_learner: Any = None

    def push_undo(self, entry: dict) -> None:
        with self._lock:
            self.undo_stack.append(entry)
            if len(self.undo_stack) > self.max_undo:
                self.undo_stack = self.undo_stack[-self.max_undo:]

    def pop_undo(self) -> Optional[dict]:
        with self._lock:
            return self.undo_stack.pop() if self.undo_stack else None

    def push_action(self, tool_name: str, arguments: dict, result: str) -> None:
        with self._lock:
            self.recent_actions.append((tool_name, arguments, result))
            if len(self.recent_actions) > self.max_recent:
                self.recent_actions = self.recent_actions[-self.max_recent:]

    def get_last_action(self) -> Optional[tuple]:
        with self._lock:
            return self.recent_actions[-1] if self.recent_actions else None

    def cache_get(self, key: str, ttl: float) -> Optional[str]:
        with self._lock:
            if key in self.response_cache:
                result, ts = self.response_cache[key]
                if time.time() - ts < ttl:
                    return result
                del self.response_cache[key]
            return None

    def cache_set(self, key: str, result: str) -> None:
        with self._lock:
            self.response_cache[key] = (result, time.time())

    def cache_clear(self) -> None:
        with self._lock:
            self.response_cache.clear()

    def can_escalate(self, tool_name: str, query: str) -> bool:
        with self._lock:
            if self.escalation_depth >= self.max_escalation_depth:
                return False
            key = (tool_name, query)
            if key in self.last_escalation:
                if time.time() - self.last_escalation[key] < self.escalation_cooldown:
                    return False
            return True

    def record_escalation(self, tool_name: str, query: str) -> None:
        with self._lock:
            self.escalation_depth += 1
            self.last_escalation[(tool_name, query)] = time.time()

    def reset_escalation(self) -> None:
        with self._lock:
            self.escalation_depth = 0

    def log_action(self, module: str, action: str, result: str,
                   success: bool = True) -> None:
        with self._lock:
            self.action_log.append({
                "timestamp": time.time(),
                "module": module,
                "action": action,
                "result": result[:200] if result else "",
                "success": success,
            })
            if len(self.action_log) > self.max_log:
                self.action_log = self.action_log[-self.max_log:]


# ---------------------------------------------------------------------------
# Audio / Speech State
# Replaces: speech.py _mic_state, _is_speaking, _detected_language,
#           _next_speak_language, _last_spoken_text, _speak_end_time,
#           _calibrated, _stt_engine, _input_mode
# ---------------------------------------------------------------------------

@dataclass
class AudioState:
    """Tracks microphone, STT, and TTS state."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # Mic state machine
    mic_state: str = "IDLE"                       # IDLE | LISTENING | PROCESSING | SPEAKING

    # Speaking state
    is_speaking: bool = False
    last_spoken_text: Optional[str] = None
    speak_end_time: float = 0.0

    # Language
    detected_language: str = "en"
    next_speak_language: Optional[str] = None

    # Calibration
    calibrated: bool = False

    # Engine selection
    stt_engine: str = "whisper"                   # "whisper" | "google"
    input_mode: str = "voice"                     # "voice" | "text" | "hybrid"

    # Wake words
    wake_words: set = field(default_factory=set)

    def set_mic_state(self, state: str) -> None:
        with self._lock:
            self.mic_state = state

    def get_mic_state(self) -> str:
        with self._lock:
            return self.mic_state

    def set_speaking(self, speaking: bool, text: Optional[str] = None) -> None:
        with self._lock:
            self.is_speaking = speaking
            if text is not None:
                self.last_spoken_text = text
            if not speaking:
                self.speak_end_time = time.time()

    def set_language(self, lang: str) -> None:
        with self._lock:
            self.detected_language = lang

    def get_language(self) -> str:
        with self._lock:
            return self.detected_language

    def consume_next_language(self) -> Optional[str]:
        """Get and clear the one-shot language override."""
        with self._lock:
            lang = self.next_speak_language
            self.next_speak_language = None
            return lang


# ---------------------------------------------------------------------------
# Provider State
# Replaces: ai_providers.py _rate_limits, _ollama_last_check, _ollama_available
# ---------------------------------------------------------------------------

@dataclass
class ProviderState:
    """Tracks AI provider health and rate limiting."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # Rate limits: {provider_name: {"until": float, "consecutive": int}}
    rate_limits: dict = field(default_factory=dict)

    # Ollama health
    ollama_available: Optional[bool] = None
    ollama_last_check: float = 0.0
    ollama_check_interval: float = 60.0

    def is_rate_limited(self, provider: str) -> bool:
        with self._lock:
            if provider not in self.rate_limits:
                return False
            return time.time() < self.rate_limits[provider].get("until", 0)

    def record_rate_limit(self, provider: str, backoff_seconds: float) -> None:
        with self._lock:
            if provider not in self.rate_limits:
                self.rate_limits[provider] = {"until": 0, "consecutive": 0}
            entry = self.rate_limits[provider]
            entry["consecutive"] += 1
            entry["until"] = time.time() + backoff_seconds

    def clear_rate_limit(self, provider: str) -> None:
        with self._lock:
            if provider in self.rate_limits:
                self.rate_limits[provider] = {"until": 0, "consecutive": 0}

    def should_check_ollama(self) -> bool:
        with self._lock:
            return time.time() - self.ollama_last_check > self.ollama_check_interval

    def set_ollama_status(self, available: bool) -> None:
        with self._lock:
            self.ollama_available = available
            self.ollama_last_check = time.time()


# ---------------------------------------------------------------------------
# Agent State
# Replaces: desktop_agent.py execution tracking (currently class-internal)
# ---------------------------------------------------------------------------

@dataclass
class AgentState:
    """Tracks desktop agent execution state."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    running: bool = False
    current_goal: Optional[str] = None
    current_step: int = 0
    max_steps: int = 15
    max_retries: int = 3
    max_diagnosis_rounds: int = 2
    last_plan: list = field(default_factory=list)
    last_result: Optional[str] = None
    step_traces: list = field(default_factory=list)

    def start(self, goal: str) -> None:
        with self._lock:
            self.running = True
            self.current_goal = goal
            self.current_step = 0
            self.step_traces = []

    def stop(self, result: Optional[str] = None) -> None:
        with self._lock:
            self.running = False
            self.last_result = result

    def is_running(self) -> bool:
        with self._lock:
            return self.running

    def trace_step(self, step_data: dict) -> None:
        with self._lock:
            self.step_traces.append(step_data)
            self.current_step += 1


# ---------------------------------------------------------------------------
# Unified Runtime State
# ---------------------------------------------------------------------------

@dataclass
class RuntimeState:
    """Top-level container for all runtime state.

    Constructed once at startup, passed to services via the DI container.
    Each sub-state has its own Lock — no single bottleneck.
    """

    session: SessionState = field(default_factory=SessionState)
    brain: BrainState = field(default_factory=BrainState)
    audio: AudioState = field(default_factory=AudioState)
    provider: ProviderState = field(default_factory=ProviderState)
    agent: AgentState = field(default_factory=AgentState)
