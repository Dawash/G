"""Centralized timeout constants for all Project G components.

Every timeout, delay, interval, and cooldown lives here. No magic numbers
in source files — import from this module instead.

Usage:
    from core.timeouts import Timeouts
    result = provider._call_api(timeout=Timeouts.LLM_CHAT)

To scale all timeouts for slow hardware:
    Timeouts.scale(1.5)  # 50% more time for everything

To override individual timeouts from config.json:
    Timeouts.load_overrides(config)  # reads "timeouts" section if present
"""

import logging

logger = logging.getLogger(__name__)


class Timeouts:
    """All timeout, delay, and interval constants."""

    # === LLM Calls ===
    LLM_CHAT = 60
    LLM_CHAT_FAST = 15
    LLM_STREAM = 120
    LLM_TOOL_ROUND = 30
    LLM_TOOL_LOOP_MAX = 180
    LLM_WARMUP = 60
    LLM_VALIDATION = 10

    # === Brain Processing ===
    BRAIN_THINK_7B = 60
    BRAIN_THINK_32B = 180
    BRAIN_THINK_72B = 300
    BRAIN_THINK_COMPLEX = 1.5    # multiplier
    BRAIN_ACKNOWLEDGMENT = 3.0

    # === Agent / Desktop Automation ===
    AGENT_STEP = 30
    AGENT_TOTAL = 300
    AGENT_REPLAN = 15
    AGENT_VERIFY = 5

    # === Speech ===
    SPEECH_LISTEN = 10
    SPEECH_VAD_CHUNK = 2.0
    SPEECH_SILENCE_AFTER = 0.8
    SPEECH_MIN_DURATION = 0.3
    TTS_SENTENCE = 15
    TTS_COOLDOWN = 0.3
    WAKE_WORD_LISTEN = 2.0
    AUTO_SLEEP = 90

    # === Subprocess / OS ===
    SUBPROCESS_DEFAULT = 30
    SUBPROCESS_QUICK = 10
    SUBPROCESS_LONG = 60
    APP_LAUNCH_WAIT = 3
    APP_CLOSE_WAIT = 5

    # === Network / HTTP ===
    HTTP_REQUEST = 10
    HTTP_DOWNLOAD = 120
    OLLAMA_HEALTH = 5
    OLLAMA_PULL = 600
    OLLAMA_KEEPALIVE = 240
    API_RATE_LIMIT_WAIT = 30

    # === Background Services ===
    AWARENESS_WINDOW_POLL = 3
    AWARENESS_SYSTEM_POLL = 10
    AWARENESS_TIME_POLL = 30
    AWARENESS_CLIPBOARD_POLL = 5
    PROACTIVE_EVAL_INTERVAL = 10
    PROACTIVE_STARTUP_DELAY = 30
    OLLAMA_HEALTH_INTERVAL = 60
    HUD_STATE_PUSH = 3
    SESSION_AUTOSAVE = 60
    MEMORY_AUTOSAVE = 120

    # === Cooldowns ===
    PROACTIVE_MIN_INTERVAL = 300
    PROACTIVE_SPEAK_COOLDOWN = 60
    BARGE_IN_COOLDOWN = 0.5
    ERROR_RETRY_DELAY = 5

    # === Brain model-specific timeouts ===
    BRAIN_THINK_14B = 90
    BRAIN_WARM_7B = 90
    BRAIN_WARM_14B = 120
    BRAIN_WARM_32B = 200
    BRAIN_WARM_72B = 300
    BRAIN_QC_7B = 20
    BRAIN_QC_14B = 45
    BRAIN_QC_32B = 90
    BRAIN_QC_72B = 120
    BRAIN_STREAM_OLLAMA = 45
    BRAIN_STREAM_ANTHROPIC = 60

    # === Ollama keepalive ===
    OLLAMA_KEEPALIVE_PING = 10

    # === Agent timeouts ===
    AGENT_DEFAULT = 180
    AGENT_ESCALATION = 120
    JARVIS_TASK = 45

    _scale_factor = 1.0
    _overrides = {}

    @classmethod
    def scale(cls, factor: float):
        """Scale ALL request timeouts by a factor (not intervals/polls/cooldowns)."""
        cls._scale_factor = factor
        skip = {'INTERVAL', 'POLL', 'COOLDOWN', 'DELAY', 'PUSH', 'AUTOSAVE'}
        for attr in dir(cls):
            if not attr.isupper() or attr.startswith('_'):
                continue
            if any(s in attr for s in skip):
                continue
            val = getattr(cls, attr)
            if isinstance(val, (int, float)):
                setattr(cls, attr, type(val)(val * factor))
        logger.info(f"Timeouts scaled by {factor}x")

    @classmethod
    def load_overrides(cls, config: dict):
        """Load timeout overrides from config.json 'timeouts' section."""
        overrides = config.get("timeouts", {})
        if not overrides:
            return
        if "scale" in overrides:
            cls.scale(float(overrides["scale"]))
        for key, value in overrides.items():
            if key == "scale":
                continue
            if hasattr(cls, key) and key.isupper():
                setattr(cls, key, type(getattr(cls, key))(value))
                cls._overrides[key] = value
                logger.debug(f"Timeout override: {key} = {value}")

    @classmethod
    def get(cls, name: str, default=None):
        """Get a timeout value by name string."""
        return getattr(cls, name, default)

    @classmethod
    def status(cls) -> dict:
        """Return all current timeout values."""
        return {
            attr: getattr(cls, attr)
            for attr in sorted(dir(cls))
            if attr.isupper() and not attr.startswith('_')
        }
