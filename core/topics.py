"""
Event topic constants for the G_v0 event bus.

Import as:
    from core.topics import Topics

All topics follow the pattern:  domain.subdomain.event
Wildcards (for subscriptions):  "perception.*"  matches all perception events.
"""


class Topics:
    # ── Perception ───────────────────────────────────────────────────────────
    # User said the wake word
    WAKE_WORD_DETECTED = "perception.audio.wake_word"
    # Whisper produced a transcription
    SPEECH_RECOGNIZED = "perception.audio.speech"
    # Mic state changed (IDLE / LISTENING / PROCESSING / SPEAKING)
    MIC_STATE_CHANGED = "perception.audio.mic_state"

    # ── Cognition ────────────────────────────────────────────────────────────
    # Raw user text entered the processing pipeline
    INPUT_RECEIVED = "cognition.input.received"
    # brain classified mode (quick / agent / research)
    MODE_CLASSIFIED = "cognition.intent.mode"
    # A tool is about to be invoked
    TOOL_CALLED = "cognition.tool.called"
    # A tool returned a result
    TOOL_RESULT = "cognition.tool.result"
    # Tool raised an error
    TOOL_ERROR = "cognition.tool.error"
    # brain.think() produced a final response
    RESPONSE_READY = "cognition.response.ready"
    # Fast-path (deterministic) handler matched
    FAST_PATH_MATCHED = "cognition.fast_path.matched"

    # ── Communication (TTS) ──────────────────────────────────────────────────
    # TTS engine started speaking
    TTS_STARTED = "communication.tts.started"
    # A single sentence was sent to TTS (streaming pipeline)
    TTS_SENTENCE = "communication.tts.sentence"
    # TTS finished all sentences
    TTS_COMPLETED = "communication.tts.completed"
    # User interrupted TTS (barge-in)
    TTS_INTERRUPTED = "communication.tts.interrupted"

    # ── Memory & Reminders ───────────────────────────────────────────────────
    # memory.log_event() was called
    MEMORY_EVENT_LOGGED = "memory.event.logged"
    # A reminder fired and is pending announcement
    REMINDER_FIRED = "memory.reminder.fired"

    # ── System Lifecycle ─────────────────────────────────────────────────────
    # Assistant entered IDLE state (waiting for wake word)
    STATE_IDLE = "system.loop.idle"
    # Assistant entered ACTIVE state (normal conversation)
    STATE_ACTIVE = "system.loop.active"
    # All startup phases complete
    STARTUP_COMPLETE = "system.startup.complete"
    # System is shutting down (fired before exit)
    SHUTDOWN = "system.shutdown"
    # An unhandled error occurred in the main loop
    LOOP_ERROR = "system.loop.error"
    # Ollama health changed (available / unavailable)
    OLLAMA_HEALTH = "system.ollama.health"

    # ── Awareness ────────────────────────────────────────────────────────────
    # Periodic full awareness snapshot (for HUD, dashboard, external consumers)
    CONTEXT_UPDATE = "context.awareness.update"

    # ── Proactive Intelligence ────────────────────────────────────────────────
    # A proactive suggestion was ranked and delivered (score + delivery method)
    PROACTIVE_SUGGESTION = "proactive.suggestion"
    # High-urgency suggestion that should be spoken immediately (score >= 90)
    PROACTIVE_SPEAK = "proactive.speak_now"

    # ── HUD Overlay ───────────────────────────────────────────────────────────
    # Command sent from the HUD browser UI (text command typed by user)
    HUD_COMMAND = "hud.user_command"
