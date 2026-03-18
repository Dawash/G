"""
Main assistant conversation loop.

Extracted from: assistant.py::run()

Responsibility:
  - Initialize services (config, brain, speech, memory, reminders)
  - IDLE/ACTIVE state machine with wake word detection
  - Auto-sleep after inactivity
  - Listen -> route -> execute -> speak cycle
  - Pending reminder announcement checks
  - Ollama health monitor
  - Crash recovery (catch exceptions, keep running)

Does NOT contain:
  - Meta-command definitions (-> command_router.py)
  - Session lifecycle helpers (-> session_manager.py)
  - Speech output formatting (-> response_dispatcher.py)
  - Fallback intent mapping (-> fallback_router.py)
  - Emergency stop service (-> core/control_flags.py)
"""

import concurrent.futures
import logging
import os
import re
import sys
import threading
import time
import uuid

from config import load_config, get_system_prompt, DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL
from ai_providers import create_provider
from speech import (
    speak, speak_async, listen, get_detected_language, set_stt_engine,
    set_language, speak_interruptible, stop_speaking,
    init_wake_words, listen_for_wake_word,
)
from app_finder import get_app_index
from reminders import ReminderManager
from memory import MemoryStore, UserPreferences, HabitTracker
from brain import Brain
from intent import detect_intent, INTENT_CHAT

from orchestration.command_router import (
    detect_meta_command, is_exit_command, is_connection_command,
    check_provider_switch, is_self_test_request, correct_speech,
)
from orchestration.session_manager import (
    startup_greeting, should_auto_sleep, do_provider_switch,
)
from orchestration.response_dispatcher import say, llm_response, truncate_for_speech
from llm.response_builder import sanitize_for_speech
from orchestration.fallback_router import build_action_map
from orchestration.fast_path import match_fast_path, execute_handler, try_fast_path
from core.control_flags import (
    trigger_emergency_stop, clear_emergency_stop,
    start_hotkey_listener,
)
from core.state import RuntimeState
from core.event_bus import bus
from core.timeouts import Timeouts
from core.topics import Topics

logger = logging.getLogger(__name__)


# ===================================================================
# Helpers
# ===================================================================

def _debug_trace(msg):
    """Write diagnostic to a separate file (bypasses logging system)."""
    try:
        trace_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug_trace.txt")
        try:
            if os.path.isfile(trace_path) and os.path.getsize(trace_path) > 1_000_000:
                os.replace(trace_path, trace_path + ".old")
        except OSError:
            pass
        with open(trace_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S', time.localtime())}: {msg}\n")
            f.flush()
    except Exception:
        pass


def _say(ainame, text):
    """Print and speak a response via response_dispatcher."""
    return say(ainame, text, speak_interruptible)


def _split_first_sentence(text):
    """Split text into (first_sentence, remainder) for parallel TTS.

    Returns (first_sentence, remainder) where remainder may be empty.
    Only splits if there are at least 2 sentences and the first is long enough
    to be worth speaking separately (>10 chars).
    """
    # Split on sentence-ending punctuation followed by whitespace
    parts = re.split(r'(?<=[.!?])\s+', text, maxsplit=1)
    if len(parts) == 2 and len(parts[0].strip()) > 10:
        return parts[0].strip(), parts[1].strip()
    return text, ""


def _say_streaming(ainame, text):
    """Print full response, then speak all sentences individually via TTS.

    Splits the complete response into sentences and feeds each to TTS one at
    a time using speak_stream(). Per-sentence delivery enables barge-in
    detection between sentences and avoids TTS processing the whole block.

    Returns:
        str or None: User's barge-in text if interrupted, else None.
    """
    if os.environ.get("G_INPUT_MODE", "").lower() == "text":
        return say(ainame, text, speak_interruptible)

    print(f"{ainame}: {text}")

    speak_text = truncate_for_speech(text)
    if not speak_text.strip():
        return None

    try:
        from speech import speak_stream

        # Split already-complete text on sentence boundaries directly
        # No need for iter_sentences() — the text isn't a token stream
        sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z"\'])', speak_text)
        # Handle single sentence or sentences ending in non-standard ways
        if len(sentences) == 1:
            sentences = re.split(r'(?<=[.!?])\s+', speak_text)

        def _sentence_gen():
            for s in sentences:
                s = s.strip()
                if s:
                    bus.publish(Topics.TTS_SENTENCE, {"sentence": s}, source="assistant_loop")
                    yield s

        interrupted = speak_stream(_sentence_gen())
        bus.publish(
            Topics.TTS_INTERRUPTED if interrupted else Topics.TTS_COMPLETED,
            {"interrupted": bool(interrupted)},
            source="assistant_loop",
        )
        return interrupted
    except Exception:
        # Fallback
        first, remainder = _split_first_sentence(speak_text)
        if not remainder:
            return speak_interruptible(speak_text)
        speak(first)
        return speak_interruptible(remainder)


def _llm_response(brain, situation, user_input, uname, fast_key=None):
    """Generate fresh LLM response for meta-situations."""
    return llm_response(brain, situation, user_input, uname, fast_key=fast_key)


def _say_quick_stream(ainame, brain, prompt):
    """Speak a brain response that streams token-by-token to TTS.

    Used for meta-command LLM responses (goodbye, undo ack, etc.) where
    we want sub-200ms first-word latency by streaming the LLM output
    directly through sentence_buffer → speak_stream.

    Falls back to normal quick_chat + speak if streaming unavailable.

    Returns:
        str or None: Barge-in text if interrupted, else None.
    """
    if os.environ.get("G_INPUT_MODE", "").lower() == "text":
        result = brain.quick_chat(prompt)
        if result:
            print(f"{ainame}: {result}")
        return None

    try:
        from speech import speak_stream
        full_text_parts = []

        def _sentence_gen():
            for sentence in brain.stream_quick_chat(prompt):
                full_text_parts.append(sentence)
                yield sentence

        interrupted = speak_stream(_sentence_gen())
        full_text = " ".join(full_text_parts)
        if full_text:
            print(f"{ainame}: {full_text}")
        return interrupted
    except Exception:
        # Fallback to non-streaming
        result = brain.quick_chat(prompt)
        if result:
            print(f"{ainame}: {result}")
            return speak_interruptible(result)
        return None


def _api_limited():
    """Check if all API providers are rate-limited."""
    try:
        from ai_providers import is_rate_limited
        return is_rate_limited()
    except ImportError:
        return False


def _restart_process():
    """Restart the assistant process."""
    logger.info("Restarting assistant process...")
    python = sys.executable
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run.py")
    os.execv(python, [python, script])


def _start_ollama_health_monitor(provider_name, ollama_was_down, ollama_url=None):
    """Start background thread that checks Ollama availability periodically."""
    if provider_name != "ollama":
        return

    def _monitor():
        while True:
            time.sleep(60)
            try:
                from ai_providers import check_ollama_health
                available = check_ollama_health(force=True, ollama_url=ollama_url)
                if ollama_was_down[0] and available:
                    ollama_was_down[0] = False
                    logger.info("Ollama reconnected — brain available again")
                elif not available and not ollama_was_down[0]:
                    ollama_was_down[0] = True
                    logger.warning("Ollama went offline — falling back to keyword intent")
            except Exception:
                pass

    threading.Thread(target=_monitor, daemon=True).start()


# ===================================================================
# Ollama keepalive — prevents model cold starts (2-3s penalty)
# ===================================================================

_keepalive_stop = threading.Event()


def _start_ollama_keepalive(ollama_model, ollama_url=None):
    """Start background thread that pings Ollama every 240s to keep the model in VRAM.

    Ollama unloads models after 5 minutes of inactivity by default. This sends a
    minimal 1-token generation request every 4 minutes to prevent that, avoiding
    the 2-3 second cold-start penalty on the next real request.

    Args:
        ollama_model: Model name (e.g. "qwen2.5:7b").
        ollama_url: Ollama API base URL (default: http://localhost:11434).
    """
    import requests as _requests
    _base_url = (ollama_url or "http://localhost:11434").rstrip("/")

    def _keepalive():
        while not _keepalive_stop.is_set():
            # Wait 240 seconds, but check stop event every 10s for fast shutdown
            for _ in range(24):
                if _keepalive_stop.wait(timeout=Timeouts.OLLAMA_KEEPALIVE_PING):
                    return
            try:
                _requests.post(
                    f"{_base_url}/api/generate",
                    json={
                        "model": ollama_model,
                        "prompt": "hi",
                        "options": {"num_predict": 1},
                    },
                    timeout=Timeouts.OLLAMA_KEEPALIVE_PING,
                )
                logger.debug(f"Ollama keepalive: pinged {ollama_model}")
            except Exception as e:
                logger.debug(f"Ollama keepalive: ping failed (ok if Ollama is down): {e}")

    _keepalive_stop.clear()
    threading.Thread(target=_keepalive, daemon=True, name="ollama-keepalive").start()
    logger.info(f"Ollama keepalive started for {ollama_model} (every 240s)")


def _stop_ollama_keepalive():
    """Signal the keepalive thread to stop."""
    _keepalive_stop.set()


# ===================================================================
# Main loop
# ===================================================================

def run(runtime_state=None):
    """Main assistant loop — speed-first smart routing with wake word + meta-commands.

    Args:
        runtime_state: Optional RuntimeState instance. Created if not provided.
    """
    if runtime_state is None:
        runtime_state = RuntimeState()

    _ss = runtime_state.session

    # --- Configuration ---
    config = load_config()
    uname = config["username"]
    ainame = config["ai_name"]
    provider_name = config["provider"]
    api_key = config["api_key"]
    ollama_model = config.get("ollama_model", DEFAULT_OLLAMA_MODEL)
    ollama_url = config.get("ollama_url", DEFAULT_OLLAMA_URL)
    cloud_model = config.get("cloud_model")

    # Load timeout overrides from config
    Timeouts.load_overrides(config)

    system_prompt = get_system_prompt(uname, ainame)
    provider = create_provider(provider_name, api_key, system_prompt,
                               ollama_model=ollama_model, ollama_url=ollama_url,
                               model=cloud_model)

    # --- Language settings ---
    stt_engine = config.get("stt_engine", "whisper")
    set_stt_engine(stt_engine)
    lang_pref = config.get("language", "auto")
    if lang_pref != "auto":
        set_language(lang_pref)

    init_wake_words(ainame)

    # --- Services ---
    session_id = uuid.uuid4().hex[:12]
    memory = MemoryStore()
    preferences = UserPreferences(memory)
    habits = HabitTracker(memory)

    # Prune old events/usage on startup to prevent unbounded DB growth
    try:
        db_mb = memory.get_db_size_mb()
        if db_mb > 50:  # Only cleanup if DB is getting large
            stats = memory.cleanup(max_events_age_days=30, max_usage_age_days=90)
            logger.info(f"Memory DB was {db_mb:.1f}MB, cleaned {stats}")
    except Exception as e:
        logger.debug(f"Memory cleanup skipped: {e}")
    reminder_mgr = ReminderManager(speak_fn=speak)
    reminder_mgr.start_checker()

    # --- Alarm system ---
    try:
        from alarms import AlarmManager, set_alarm_manager
        alarm_mgr = AlarmManager(speak_fn=speak)
        alarm_mgr.start_checker()
        set_alarm_manager(alarm_mgr)
    except Exception as _ae:
        logger.debug(f"Alarm system init: {_ae}")
        alarm_mgr = None

    # Wire memory and workflow references for tools
    from tools.memory_workflow_tools import set_memory_refs, set_workflow_registry
    from features.workflows.registry import WorkflowRegistry
    set_memory_refs(memory, preferences)
    _workflow_registry = WorkflowRegistry()
    set_workflow_registry(_workflow_registry)

    # Multi-model router — initialize from config (opt-in, non-blocking)
    try:
        from llm.model_router import model_router as _model_router
        _model_router.setup_from_config(config)
        logger.info("Multi-model router initialized")
    except Exception as _mr_err:
        logger.debug(f"Model router init skipped: {_mr_err}")

    # Advanced 3-layer memory — initialize singleton (non-blocking)
    _adv_memory = None
    try:
        from memory.memory_api import memory as _adv_memory
        logger.info(f"Advanced memory initialized: {_adv_memory.get_stats()['episodic']}")
    except Exception as _mem_err:
        logger.debug(f"Advanced memory init skipped: {_mem_err}")

    # Rust audio pipeline — detect and report availability (non-blocking, optional)
    _rust_audio = None
    try:
        from audio.rust_audio import is_rust_audio_available, get_audio_backend, BINARY_PATH
        if is_rust_audio_available():
            _rust_audio = get_audio_backend()
            logger.info("Rust audio pipeline available — 30ms VAD frames active")
            print("  [Audio] Rust audio pipeline detected (30ms VAD frames)")
        else:
            logger.debug(
                "Rust audio binary not found at %s — using Python VAD. "
                "Build with: python crates/build.py", BINARY_PATH
            )
    except Exception as _ra_err:
        logger.debug(f"Rust audio init skipped: {_ra_err}")

    action_map = build_action_map(reminder_mgr, provider, memory, config)

    brain = Brain(
        provider_name=provider_name,
        api_key=api_key,
        username=uname,
        ainame=ainame,
        action_registry=action_map,
        reminder_mgr=reminder_mgr,
        ollama_model=ollama_model,
        user_preferences=preferences,
        ollama_url=ollama_url,
        cloud_model=cloud_model,
    )

    # Create interaction handler (used for exit/meta/connection detection)
    from orchestration.interaction_handler import InteractionHandler
    _handler = InteractionHandler(
        brain=brain,
        config=config,
        services={
            "memory": memory,
            "reminder_mgr": reminder_mgr,
            "action_map": action_map,
            "provider": provider,
        },
    )

    # Session continuity — restore previous session
    from core.session_persistence import SessionPersistence
    _session_persistence = SessionPersistence()
    _restored = _session_persistence.restore(brain, _ss)
    if _restored:
        logger.info("Session restored from previous run")

    # --- Startup health check (quick, critical systems only) ---
    try:
        from self_test import run_quick_check
        _health = run_quick_check()
        if "Issues" in _health:
            logger.warning(f"Startup health check: {_health}")
            print(f"  [Health] {_health}")
        else:
            logger.info(f"Startup health: {_health}")
    except Exception as e:
        logger.debug(f"Startup health check skipped: {e}")

    # Wire brain ref into alarm manager for LLM-generated motivations
    if alarm_mgr:
        alarm_mgr.brain_ref = brain

    # === PHASED STARTUP ===
    # Phase 1 (essential): config, provider, memory, brain — already done above
    # Phase 2 (background): warmup, app index, hotkeys, greeting — parallel
    # Phase 3 (on-demand): cognitive engine, vision, desktop agent — lazy in brain.py

    try:
        from core.metrics import metrics
        _startup_timer = metrics.timer("startup")
        _startup_timer.__enter__()
    except Exception:
        _startup_timer = None

    # Phase 2: all background tasks launch in parallel — no blocking waits
    threading.Thread(target=get_app_index, daemon=True).start()
    threading.Thread(target=start_hotkey_listener, daemon=True).start()
    threading.Thread(target=brain.warm_up, daemon=True).start()

    # Phase 2a: Awareness system — starts perception threads + event-bus subscriptions
    # Must start BEFORE brain.warm_up completes so context is populated for first LLM call
    try:
        from core.awareness_state import awareness as _awareness
        from core.awareness_updater import start_awareness_updates
        _awareness.update(user_name=uname)
        start_awareness_updates()
        logger.info("Awareness system started (time/window/system/clipboard monitors)")
    except Exception as _aw_err:
        logger.debug(f"Awareness system init skipped: {_aw_err}")

    # Phase 2b-proactive: Proactive Intelligence Engine — 13 built-in triggers
    _proactive = None
    try:
        from core.triggers.registry import register_all_triggers
        from core.proactive_engine import proactive_engine as _proactive

        _trigger_count = register_all_triggers()
        _proactive.load_state()          # restore acceptance history from last run
        _proactive.start()

        # Wire urgent (speak_now) suggestions to immediate TTS
        @bus.on("proactive.speak_now", run_async=True)
        def _handle_urgent_proactive(event):
            msg = event.payload.get("message", "")
            if msg:
                try:
                    print(f"{ainame}: [Proactive] {msg}")
                    speak(msg)
                except Exception:
                    pass

        logger.info(f"Proactive engine started with {_trigger_count} triggers")
    except Exception as _pe_err:
        logger.debug(f"Proactive engine init skipped: {_pe_err}")

    # Observability — centralized metrics collection
    try:
        from core.observability import start_observability
        start_observability()
        logger.info("Observability started")
    except Exception as _obs_err:
        logger.debug(f"Observability init skipped: {_obs_err}")

    # Phase 2b: Load plugins (Mycroft-style skill system)
    try:
        from plugins.loader import PluginLoader
        import brain as _brain_mod
        _plugin_loader = PluginLoader()
        _plugin_loader.set_brain(brain)
        _plugin_loader.set_memory(memory)
        _plugin_loader.set_speak_fn(speak)
        loaded, errors = _plugin_loader.discover_and_load()
        _brain_mod._plugin_loader = _plugin_loader
        # Add plugin tools to brain's LLM tool list
        try:
            plugin_tools = _plugin_loader.get_tool_definitions()
            if plugin_tools:
                brain.tools.extend(plugin_tools)
                logger.info(f"Added {len(plugin_tools)} plugin tools to LLM")
        except Exception:
            pass
        if loaded > 0:
            logger.info(f"Plugins: {loaded} loaded, {errors} errors")
        _handler.set_plugin_loader(_plugin_loader)
    except Exception as e:
        logger.debug(f"Plugin system init: {e}")
        _plugin_loader = None

    # Phase 2c: Initialize JARVIS skill engine
    try:
        from skills.engine import JarvisEngine
        import brain as _brain_mod2
        _jarvis = JarvisEngine(quick_chat_fn=brain.quick_chat)
        _jarvis.register_builtin_skills(
            action_registry=action_map,
            reminder_mgr=reminder_mgr,
        )
        _brain_mod2._jarvis_engine = _jarvis
        logger.info(f"JARVIS engine: {_jarvis.registry.count} skills ready")
    except Exception as e:
        logger.debug(f"JARVIS engine init: {e}")

    # Phase 2d: HUD Overlay — JARVIS visual dashboard (always-on, port 8767)
    _hud_server = None
    _hud_command_queue: list[str] = []
    _hud_cmd_lock = threading.Lock()
    if config.get("hud_enabled", True):
        try:
            from hud.server import start_hud_server as _start_hud
            hud_port = config.get("hud_port", 8767)
            if _start_hud(port=hud_port):
                logger.info(f"HUD overlay started on http://localhost:{hud_port}")
                print(f"  [HUD] Open http://localhost:{hud_port} for the JARVIS dashboard")

                # Route HUD text commands back into the main listen loop
                @bus.on(Topics.HUD_COMMAND)
                def _on_hud_command(event):
                    text = event.payload.get("text", "").strip()
                    if text:
                        with _hud_cmd_lock:
                            _hud_command_queue.append(text)

                from hud.server import get_hud_server
                _hud_server = get_hud_server()
        except Exception as _hud_err:
            logger.debug(f"HUD server init skipped: {_hud_err}")

    # Phase 2e: Camera perception (EDITH mode) — disabled by default
    _camera = None
    if config.get("camera_enabled", False):
        try:
            from perception.camera import CameraPerception
            _camera = CameraPerception(
                camera_index=config.get("camera_index", 0),
                fps=config.get("camera_fps", 5.0),
            )
            if _camera.is_available:
                if _camera.start():
                    logger.info("Camera perception active (EDITH mode)")
                    print("  [Camera] EDITH mode active")
                else:
                    _camera = None
            else:
                _camera = None
        except Exception as _cam_err:
            logger.debug(f"Camera init: {_cam_err}")

    # Phase 2f: Camera vision system (webcam + IP cameras for LLM vision tools)
    try:
        from camera.camera_manager import camera_mgr as _camera_mgr
        _camera_mgr.load_ip_cameras_from_config(config)
        _discovered_cameras = _camera_mgr.discover_cameras()
        if _discovered_cameras:
            logger.info(f"Camera system: {len(_discovered_cameras)} camera(s)")
            print(f"  [Camera] {len(_discovered_cameras)} camera(s) detected")
    except Exception as _cam_sys_err:
        logger.debug(f"Camera system init: {_cam_sys_err}")

    # Gesture-based controls (if camera active)
    if _camera:
        @bus.on("perception.camera.gesture")
        def _on_gesture(event):
            gesture = event.payload.get("gesture", "") if hasattr(event, 'payload') else ""
            if gesture == "open_palm":
                try:
                    stop_speaking()
                except Exception:
                    pass

    ollama_was_down = [False]
    _start_ollama_health_monitor(provider_name, ollama_was_down, ollama_url=ollama_url)

    # Keepalive: prevent Ollama model cold starts (2-3s penalty after 5m idle)
    if provider_name == "ollama":
        _start_ollama_keepalive(ollama_model, ollama_url=ollama_url)

    # Start WebSocket gateway + Web UI for remote control (phone/browser)
    # Controlled by config: "web_remote": true/false (default: false)
    _gateway = None
    _web_ui = None
    if config.get("web_remote", False):
        try:
            from gateway.ws_server import GatewayServer
            _gateway = GatewayServer(brain, config)
            if _gateway.start():
                logger.info("WebSocket gateway started on port %s", config.get("gateway_port", 8765))
            # Also start the web UI HTTP server (port = gateway_port + 1)
            from gateway.http_server import WebUIServer
            http_port = config.get("gateway_port", 8765) + 1
            _web_ui = WebUIServer(http_port=http_port)
            if _web_ui.start():
                try:
                    import socket as _sock
                    _s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
                    _s.connect(("8.8.8.8", 80))
                    _local_ip = _s.getsockname()[0]
                    _s.close()
                except Exception:
                    _local_ip = "localhost"
                logger.info(f"Web UI available at http://{_local_ip}:{http_port}")
                _gw_token = config.get("gateway_token", "")
                print(f"\n  [Web Remote] Open http://{_local_ip}:{http_port} on your phone")
                if _gw_token:
                    print(f"  [Web Remote] Access token: {_gw_token}")
                print()
        except Exception as e:
            logger.debug(f"Gateway not available: {e}")
    else:
        logger.debug("Web remote access disabled (set web_remote: true in config.json to enable)")

    # Startup greeting runs while warmup proceeds in background
    # (previously we blocked up to 15s on warmup BEFORE greeting)
    startup_greeting(config, reminder_mgr, speak_fn=speak, speak_async_fn=speak_async)
    _ss.touch()

    # Start PopupGuardian session-wide (catches popups in quick mode too, not just agent mode)
    _popup_guardian = None
    try:
        from agents.popup_guardian import PopupGuardian
        _popup_guardian = PopupGuardian(goal="session-wide popup dismissal")
        _popup_guardian.start()
        logger.info("PopupGuardian started (session-wide)")
    except Exception as e:
        logger.debug(f"PopupGuardian not available: {e}")

    if _startup_timer:
        try:
            _startup_timer.__exit__(None, None, None)
        except Exception:
            pass

    bus.publish(Topics.STARTUP_COMPLETE, {
        "provider": provider_name,
        "model": ollama_model,
        "ainame": ainame,
    }, source="assistant_loop")

    is_connected = True
    interaction_count = 0
    _last_session_save = time.time()
    _barge_in_text = None  # Carries barge-in input across loop iterations
    _recent_commands = []          # Last 10 user inputs for repetition detection
    _proactive_cooldowns = {}      # {topic: last_suggested_time} — rate limits
    _last_tool_used = None         # Tool name from last brain call
    _last_tool_success = True      # Whether last tool succeeded

    # === MAIN LOOP ===
    while True:
      try:
        # ----- STATE MACHINE: IDLE / ACTIVE -----
        if _ss.get_mode() == "IDLE":
            if os.environ.get("G_INPUT_MODE", "").lower() == "text":
                _ss.set_mode("ACTIVE")
            else:
                woke = listen_for_wake_word(timeout_s=None)
                if woke:
                    _ss.set_mode("ACTIVE")
                    bus.publish(Topics.WAKE_WORD_DETECTED, {"ainame": ainame}, source="assistant_loop")
                    bus.publish(Topics.STATE_ACTIVE, {"reason": "wake_word"}, source="assistant_loop")
                    _say_quick_stream(
                        ainame, brain,
                        f"User just woke you up by saying the wake word. Give a brief friendly greeting as {ainame}."
                    )
                else:
                    bus.publish(Topics.STATE_IDLE, {"reason": "no_wake_word"}, source="assistant_loop")
                continue

        # Auto-sleep after inactivity (voice mode only)
        _is_text = os.environ.get("G_INPUT_MODE", "").lower() == "text"
        if should_auto_sleep(_ss, is_text_mode=_is_text):
            _ss.set_mode("IDLE")
            _ss.last_mode_was_agent = False
            bus.publish(Topics.STATE_IDLE, {"reason": "inactivity"}, source="assistant_loop")
            _say(ainame, f"Going to sleep. Say Hey {ainame} to wake me.")
            continue

        # ----- LISTEN (check reminders first, max 2 per cycle) -----
        try:
            pending = reminder_mgr.get_pending_announcements()
            # Limit to 2 announcements per cycle; re-queue the rest
            if len(pending) > 2:
                requeue = pending[2:]
                pending = pending[:2]
                with reminder_mgr._lock:
                    reminder_mgr._pending_announcements[0:0] = requeue
            for announcement in pending:
                # Print to console only — don't speak (TTS blocks the main loop)
                print(f"[REMINDER] {announcement}")
                logger.info(f"Reminder fired: {announcement}")
            interrupted = None
            if pending:
                _ss.touch()  # Reset inactivity timer after reminders
        except Exception:
            interrupted = None

        # Pick up barge-in text from previous iteration (speech interruption)
        if _barge_in_text:
            user_input = _barge_in_text
            _barge_in_text = None
            interrupted = user_input  # skip listen()

        # Pick up commands typed in the HUD browser dashboard
        if not interrupted:
            with _hud_cmd_lock:
                if _hud_command_queue:
                    user_input = _hud_command_queue.pop(0)
                    interrupted = user_input

        if not interrupted:
            _debug_trace("waiting for listen()")
            user_input = listen()
            _debug_trace(f"listen() returned: {user_input[:30] if user_input else 'None'}")
        if user_input is None:
            continue

        _ss.touch()
        user_input = correct_speech(user_input)
        bus.publish(Topics.INPUT_RECEIVED, {"text": user_input}, source="assistant_loop")

        # Auto-save session periodically
        if time.time() - _last_session_save > Timeouts.SESSION_AUTOSAVE:
            try:
                _ss.last_user_input = user_input
                _session_persistence.save(brain, _ss)
                _last_session_save = time.time()
            except Exception:
                pass

        interaction_count += 1
        logger.info(f"Loop #{interaction_count}: got input '{user_input[:50]}'")
        memory.log_event(session_id, "user_input", {"text": user_input})
        _recent_commands.append(user_input)
        if len(_recent_commands) > 10:
            _recent_commands.pop(0)

        # Smart proactive suggestion (replaces every-20 counter)
        # Fires on context triggers: post-task / repetition / keyword / failure
        try:
            _suggestion = habits.smart_proactive_check(
                user_input, _last_tool_used, _last_tool_success,
                _recent_commands, _proactive_cooldowns
            )
            if _suggestion:
                logger.info(f"Smart suggestion: {_suggestion}")
                # Speak suggestion naturally, don't shout — append to next response
                _ss.pending_suggestion = _suggestion
        except Exception:
            pass

        # Every 50 interactions, log session feedback summary
        if interaction_count > 0 and interaction_count % 50 == 0:
            try:
                from orchestration.feedback import get_feedback
                summary = get_feedback().get_session_summary()
                if summary:
                    logger.info(f"Session feedback: {summary}")
            except Exception:
                pass

        # ----- ALARM DISMISS: check if an alarm is ringing -----
        # When alarm is ringing, ANY voice input is treated as dismissal attempt.
        # Natural language: "stop", "I'm up", "okay okay", "turn it off",
        # "snooze for 10 minutes", "give me 5 more minutes", etc.
        if alarm_mgr and alarm_mgr.is_ringing:
            _lower = user_input.lower()
            import re as _re_alarm

            # Check for snooze intent first (more specific)
            _is_snooze = any(w in _lower for w in [
                "snooze", "later", "more minutes", "more time", "not yet",
                "few more", "5 more", "10 more", "let me sleep", "sleepy",
                "too early", "can't get up", "don't want to",
            ])
            if _is_snooze:
                snooze_min = 5  # default
                _sm = _re_alarm.search(r'(\d+)\s*(?:min|minute)', _lower)
                if _sm:
                    snooze_min = int(_sm.group(1))
                result = alarm_mgr.dismiss_alarm(snooze_minutes=snooze_min)
                if result:
                    _say(ainame, result)
                continue

            # Everything else = dismiss (natural language catch-all)
            # Any speech while alarm is ringing means "I'm awake"
            result = alarm_mgr.dismiss_alarm(snooze_minutes=0)
            if result:
                # Generate a fresh greeting after alarm dismiss
                _say(ainame, result)
            continue

        # ----- LAYER 1: Instant commands (0ms, no API) -----
        if is_exit_command(user_input):
            preferences.learn_from_usage()
            reminder_mgr.stop_checker()
            if alarm_mgr:
                alarm_mgr.stop_checker()
            _stop_ollama_keepalive()
            if _gateway:
                _gateway.stop()
            if _web_ui:
                _web_ui.stop()
            if _camera:
                _camera.stop()
            # Camera vision system cleanup
            try:
                from camera.camera_manager import camera_mgr as _cam_mgr_cleanup
                _cam_mgr_cleanup.close_all()
            except Exception:
                pass
            # Session continuity — save before exit
            try:
                _session_persistence.save(brain, _ss)
            except Exception:
                pass
            memory.close()
            if _proactive:
                try:
                    _proactive.save_state()
                    _proactive.stop()
                except Exception:
                    pass
            try:
                from core.observability import metrics as _obs
                _obs.stop()
            except Exception:
                pass
            bus.publish(Topics.SHUTDOWN, {"reason": "user_exit", "input": user_input},
                        source="assistant_loop")
            bus.shutdown()
            _say_quick_stream(
                ainame, brain,
                f"The user just said '{user_input}'. Give a warm, brief farewell as {ainame}."
            )
            break

        conn_cmd = is_connection_command(user_input)
        if conn_cmd == "disconnect":
            is_connected = False
            resp = _llm_response(brain, "user wants to go offline, acknowledge and mention local commands still work",
                                 user_input, uname, fast_key="disconnect")
            _say(ainame, resp)
            continue
        elif conn_cmd == "connect":
            is_connected = True
            resp = _llm_response(brain, "user is reconnecting to online mode, welcome them back",
                                 user_input, uname, fast_key="connect")
            _say(ainame, resp)
            continue

        switch_match = check_provider_switch(user_input)
        if switch_match:
            result = do_provider_switch(switch_match, config, Brain, action_map,
                                        reminder_mgr, uname, ainame, system_prompt,
                                        user_preferences=preferences)
            if result:
                config = load_config()
                provider_name = config["provider"]
                api_key = config["api_key"]
                ollama_model = config.get("ollama_model", DEFAULT_OLLAMA_MODEL)
                ollama_url = config.get("ollama_url", DEFAULT_OLLAMA_URL)
                cloud_model = config.get("cloud_model")
                provider = create_provider(provider_name, api_key, system_prompt,
                                           ollama_model=ollama_model, ollama_url=ollama_url,
                                           model=cloud_model)
                brain = result[1]
                _ss.dead_key_warned = False
                _say(ainame, result[0])
            else:
                resp = _llm_response(brain, f"provider {switch_match} is not configured yet",
                                     f"switch to {switch_match}", uname)
                _say(ainame, resp)
            continue

        # ----- META-COMMANDS: skip, shorter, repeat, undo, correction -----
        meta = detect_meta_command(user_input)
        if meta:
            if meta == "skip":
                stop_speaking()
                continue
            elif meta == "shorter" and _ss.last_response:
                short = _llm_response(brain, "summarize this in 1 sentence: " + _ss.last_response[:300],
                                      user_input, uname)
                _say(ainame, short)
                # Track user's preference for shorter responses
                try:
                    preferences.track_response_preference(short=True)
                except Exception:
                    pass
                continue
            elif meta == "more_detail" and _ss.last_response:
                detail = brain.quick_chat(
                    f"The user wants more detail on this response: {_ss.last_response[:500]}. "
                    f"Expand with additional information, 2-3 sentences."
                )
                if detail:
                    _ss.last_response = detail
                    _say(ainame, detail)
                # Track user's preference for longer responses
                try:
                    preferences.track_response_preference(short=False)
                except Exception:
                    pass
                continue
            elif meta == "repeat" and _ss.last_response:
                _say(ainame, _ss.last_response)
                continue
            elif meta == "undo":
                undo_result = brain.undo_last_action()
                if undo_result:
                    _say(ainame, undo_result)
                else:
                    _say(ainame, "Nothing to undo.")
                continue
            elif meta == "emergency_stop":
                trigger_emergency_stop()
                _say(ainame, "All automation stopped.")
                clear_emergency_stop()
                continue
            elif isinstance(meta, tuple) and meta[0] == "correction":
                user_input = meta[1]
                logger.info(f"Correction detected, re-processing: {user_input}")

        # ----- LAYER 2: Self-test -----
        if is_self_test_request(user_input):
            _say(ainame, _llm_response(brain, "user wants to run system diagnostics, acknowledge briefly",
                                       user_input, uname, fast_key="self_test"))
            from self_test import run_self_test
            result = run_self_test()
            _say(ainame, result)
            memory.log_event(session_id, "self_test", {"result": result[:200]})
            continue

        # ----- LAYER 1.5: Plugin intents (before fast-path, 0ms regex) -----
        if _plugin_loader:
            try:
                _plugin_result = _plugin_loader.try_handle(user_input)
                if _plugin_result:
                    _ss.last_response = str(_plugin_result)
                    _say(ainame, _plugin_result)
                    continue
            except Exception:
                pass

        # ----- LAYER 2a: Fast-path routing (deterministic, no LLM) -----
        # try_fast_path handles both single and multi-step commands
        # ("open Chrome and check weather" → two fast-path actions)
        _fp_t0 = time.time()
        _fp_result = try_fast_path(user_input, action_map, reminder_mgr)
        _fp_elapsed = time.time() - _fp_t0

        if _fp_result.handled and _fp_result.response is not None:
            _ss.last_response = str(_fp_result.response)
            logger.info(f"Fast-path: {_fp_result.handler_key} -> {str(_fp_result.response)[:60]}")
            bus.publish(Topics.FAST_PATH_MATCHED, {
                "handler": _fp_result.handler_key or "multi_step",
                "response": str(_fp_result.response)[:200],
                "elapsed_ms": round(_fp_elapsed * 1000, 1),
            }, source="assistant_loop")
            bus.publish(Topics.RESPONSE_READY, {
                "text": str(_fp_result.response),
                "mode": "fast_path",
            }, source="assistant_loop")
            _say(ainame, _fp_result.response)
            memory.log_event(session_id, "fast_path",
                             {"handler": _fp_result.handler_key or "multi_step",
                              "response": str(_fp_result.response)[:200]})
            # Track success for feedback
            try:
                from orchestration.feedback import get_feedback
                get_feedback().record_success(
                    _fp_result.handler_key or "multi_step", "fast_path", _fp_elapsed)
            except Exception as e:
                logger.debug(f"Non-critical: {type(e).__name__}: {e}")
            try:
                from core.observability import metrics as _obs
                _obs.record_success("fast_path")
                _obs.record_interaction()
            except Exception:
                pass
            continue

        # Also try single-command match for error recovery path
        _decision = match_fast_path(user_input)
        if _decision and _decision.is_deterministic and not _fp_result.handled:
            # Fast-path matched but execute_handler returned None — try recovery
            try:
                from orchestration.error_recovery import recover_from_failure
                _ok, _recovery_result, _strategy = recover_from_failure(
                    _decision.handler_key, "returned None", _decision.args, action_map)
                if _ok and _recovery_result:
                    logger.info(f"Recovery ({_strategy}): {str(_recovery_result)[:60]}")
                    _ss.last_response = str(_recovery_result)
                    _say(ainame, _recovery_result)
                    continue
                elif _recovery_result:
                    # Graceful degradation message
                    logger.info(f"Degraded ({_strategy}): {_recovery_result}")
                    _ss.last_response = _recovery_result
                    _say(ainame, _recovery_result)
                    continue
            except Exception:
                pass
            logger.info(f"Fast-path matched ({_decision.reason}) but returned None, trying Brain")

        # ----- LAYER 2b: Brain (LLM understands intent + acts) -----
        _brain_available = True
        if provider_name == "ollama":
            try:
                from ai_providers import check_ollama_health
                # Force a fresh health check to catch mid-session Ollama crashes
                # quickly (5s timeout) instead of hanging for 60-180s in brain.think()
                if not check_ollama_health(force=True, ollama_url=ollama_url):
                    _brain_available = False
                    ollama_was_down[0] = True
                    # Give the user immediate feedback instead of silent fallthrough
                    speak_async("Ollama seems to be offline. Using basic commands only.")
                    logger.warning("Ollama health check failed (force=True) — using intent fallback")
                    # Try intent-based keyword detection for offline-capable commands
                    _offline_intents = detect_intent(user_input, provider_name=provider_name,
                                                     api_key=api_key, use_ai=False)
                    _offline_handled = False
                    for _oi_intent, _oi_data in _offline_intents:
                        if _oi_intent == INTENT_CHAT:
                            continue  # Can't handle chat without LLM
                        handler = action_map.get(_oi_intent)
                        if handler:
                            _oi_result = handler(_oi_data) if _oi_data else handler()
                            if _oi_result:
                                _say(ainame, str(_oi_result))
                                _offline_handled = True
                                break
                    if _offline_handled:
                        continue
                    _say(ainame, "I can't process that without the AI model. "
                         "Basic commands like time, weather, and app control still work.")
                    continue
            except Exception:
                pass

        if is_connected and _brain_available and not _api_limited() and not brain.key_is_dead:
            logging.info(f"Brain processing: '{user_input}'")

            # Scale timeout by model size + task complexity
            _model_name = getattr(brain, 'ollama_model', '') or ''
            _model_lower = _model_name.lower()
            if any(s in _model_lower for s in ("72b", "70b")):
                _base_timeout = Timeouts.BRAIN_THINK_72B
            elif any(s in _model_lower for s in ("32b", "34b", "27b")):
                _base_timeout = Timeouts.BRAIN_THINK_32B
            elif any(s in _model_lower for s in ("14b", "13b")):
                _base_timeout = Timeouts.BRAIN_THINK_14B
            else:
                _base_timeout = Timeouts.BRAIN_THINK_7B
            # Complex tasks get 50% more time (create, book, agent-level commands)
            _ui_lower = user_input.lower()
            _is_complex = any(w in _ui_lower for w in [
                "create", "build", "make", "generate", "write",
                "book", "order", "search and", "and then",
                "agent", "automate",
            ])
            _BRAIN_TIMEOUT = int(_base_timeout * Timeouts.BRAIN_THINK_COMPLEX) if _is_complex else _base_timeout
            # Dynamic acknowledgment: longer delay for simple queries (likely fast), shorter for complex
            _ack_delay = Timeouts.BRAIN_ACKNOWLEDGMENT + 1.0 if len(user_input.split()) <= 5 else Timeouts.BRAIN_ACKNOWLEDGMENT - 0.5
            import random as _rnd
            _ack_phrases = [
                "Working on it...", "Let me handle that...", "On it...",
                "Give me a moment...", "Just a second...", "Processing that...",
                "Let me check...", "One moment...", "Looking into it...",
                "Hang on...", "Let me figure that out...", "Getting that for you...",
            ]
            _ack_timer = threading.Timer(_ack_delay, lambda: speak_async(_rnd.choice(_ack_phrases)))
            _ack_timer.start()
            try:
                _t0 = time.time()

                # ── Streaming path: stream_think() → speak_stream() ──
                # Runs the full think() tool-calling loop, then streams the
                # final response sentence-by-sentence to TTS so the user
                # hears the first sentence before the full text is done.
                _streamed = False
                response = None
                try:
                    if hasattr(brain, 'stream_think') and is_connected and _brain_available:
                        import queue as _q
                        _sentence_q = _q.Queue()
                        _stream_done = threading.Event()
                        _stream_err = [None]

                        def _stream_producer():
                            try:
                                for sentence in brain.stream_think(
                                    user_input,
                                    detected_language=get_detected_language(),
                                ):
                                    _sentence_q.put(sentence)
                            except Exception as _e:
                                _stream_err[0] = _e
                            finally:
                                _stream_done.set()

                        _st = threading.Thread(target=_stream_producer, daemon=True)
                        _st.start()

                        _parts = []
                        _deadline = time.time() + _BRAIN_TIMEOUT
                        while not _stream_done.is_set() or not _sentence_q.empty():
                            try:
                                _wait = min(2.0, max(0.1, _deadline - time.time()))
                                _sent = _sentence_q.get(timeout=_wait)
                                if _sent:
                                    if not _parts:
                                        _ack_timer.cancel()
                                    _parts.append(_sent)
                            except _q.Empty:
                                if time.time() >= _deadline:
                                    brain._cancelled = True
                                    break

                        if _parts:
                            response = " ".join(_parts)
                            _streamed = True
                except Exception as _se:
                    logger.debug(f"stream_think path failed: {_se}")

                # ── Fallback: blocking brain.think() ──
                if not _streamed:
                    _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    _fut = _pool.submit(
                        brain.think, user_input,
                        detected_language=get_detected_language()
                    )
                    try:
                        response = _fut.result(timeout=_BRAIN_TIMEOUT)
                    except concurrent.futures.TimeoutError:
                        logger.error(f"Brain.think() timed out after {_BRAIN_TIMEOUT}s")
                        _debug_trace(f"Loop#{interaction_count} TIMEOUT after {_BRAIN_TIMEOUT}s")
                        brain._cancelled = True
                        from brain import _friendly_error
                        response = _friendly_error(
                            f"Brain timed out after {_BRAIN_TIMEOUT}s",
                            user_input=user_input,
                        )
                    finally:
                        _pool.shutdown(wait=False)

                _elapsed = time.time() - _t0
                logger.info(f"Brain returned in {_elapsed:.1f}s: {str(response)[:80] if response else 'None'}")
                _debug_trace(f"Loop#{interaction_count} brain={_elapsed:.1f}s resp={'yes' if response else 'None'} streamed={_streamed}")

                _ss.touch()
                _ss.last_mode_was_agent = _elapsed > 10

                # Track Brain success/failure for feedback
                try:
                    from orchestration.feedback import get_feedback
                    if response:
                        get_feedback().record_success("brain_think", "brain", _elapsed)
                    else:
                        get_feedback().record_failure("brain_think", "brain", "returned None")
                except Exception:
                    pass
                # Track brain metrics for observability
                try:
                    from core.observability import metrics as _obs
                    if response:
                        _obs.record_success("brain.think", duration_ms=_elapsed * 1000)
                    else:
                        _obs.record_failure("brain.think", error="returned None", duration_ms=_elapsed * 1000)
                    _obs.record_interaction()
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Brain.think() crashed: {e}", exc_info=True)
                _debug_trace(f"Loop#{interaction_count} CRASH: {e}")
                response = None
            finally:
                _ack_timer.cancel()

            if response and str(response).strip():
                # Check __RESTART__ on raw response before sanitizing
                if "__RESTART__" in str(response):
                    _say(ainame, _llm_response(brain, "assistant is about to restart, say a quick goodbye",
                                               user_input, uname, fast_key="restart"))
                    memory.close()
                    reminder_mgr.stop_checker()
                    _stop_ollama_keepalive()
                    if _gateway:
                        _gateway.stop()
                    _restart_process()
                    break

                # Strip non-Latin script leaks (CJK/Cyrillic from qwen2.5)
                # before the response is spoken or stored for "repeat"
                response = sanitize_for_speech(str(response))
                if not response.strip():
                    # Entire response was non-Latin garbage — skip
                    continue

                _ss.last_response = response
                bus.publish(Topics.RESPONSE_READY, {
                    "text": response,
                    "mode": "brain",
                    "elapsed_ms": round(_elapsed * 1000, 1),
                }, source="assistant_loop")
                _debug_trace(f"Loop#{interaction_count} pre-say")
                # Use streaming TTS: first sentence starts playing immediately
                # while memory logging happens in parallel
                _mem_thread = threading.Thread(
                    target=memory.log_event, daemon=True,
                    args=(session_id, "brain",
                          {"input": user_input, "response": str(response)[:200]}),
                )
                _mem_thread.start()
                # Advanced memory: log episode + update working memory context
                if _adv_memory is not None:
                    try:
                        _trace = getattr(brain, 'last_call_trace', {})
                        _tools_used = [t.get('name', '') for t in _trace.get('tool_calls', [])]
                        _adv_elapsed = round(_elapsed * 1000)
                        threading.Thread(
                            target=_adv_memory.learn_from_turn, daemon=True,
                            kwargs={
                                "user_input": user_input,
                                "response": str(response)[:500],
                                "tools": _tools_used or None,
                                "success": True,
                                "duration_ms": _adv_elapsed,
                            },
                        ).start()
                    except Exception:
                        pass
                # Log usage for habit tracking (enables proactive suggestions)
                try:
                    _trace = getattr(brain, 'last_call_trace', {})
                    _tool_calls = _trace.get('tool_calls', [])
                    if _tool_calls:
                        for _tc in _tool_calls[:3]:  # Track up to 3 tools per request
                            _tool_name = _tc.get('name', '')
                            _tool_args = _tc.get('args', {}) if isinstance(_tc.get('args'), dict) else {}
                            _entity = _tool_args.get('app_name', _tool_args.get('query', _tool_args.get('url', '')))
                            if _tool_name:
                                memory.log_usage(_tool_name, str(_entity)[:50] if _entity else '')
                        # Update last tool for smart proactive suggestions
                        _last_tool_used = _tool_calls[-1].get('name', None)
                        _last_tool_success = not any(
                            w in str(response).lower()
                            for w in ["failed", "error", "not found", "couldn't"]
                        )
                    else:
                        _last_tool_used = None
                        _last_tool_success = True
                except Exception:
                    pass

                # Append pending smart suggestion to response if present
                try:
                    _pending = getattr(_ss, 'pending_suggestion', None)
                    if _pending:
                        _ss.pending_suggestion = None
                        response = str(response) + f"  Also — {_pending}"
                except Exception:
                    pass
                interrupted = _say_streaming(ainame, response)
                _debug_trace(f"Loop#{interaction_count} post-say")
                logger.info("_say_streaming() completed")
                _mem_thread.join(timeout=2)  # ensure memory write completes
                _debug_trace(f"Loop#{interaction_count} post-memory")
                logger.info("Post-brain: said response, looping back to listen()")
                if os.environ.get("G_INPUT_MODE", "").lower() != "text":
                    sys.stdout.flush()
                _debug_trace(f"Loop#{interaction_count} post-flush, going to listen")
                if interrupted:
                    _barge_in_text = interrupted
                    memory.log_event(session_id, "barge_in", {"text": interrupted})
                    continue

                # Deliver any queued proactive suggestion at the natural pause
                if _proactive:
                    try:
                        _proactive_msg = _proactive.get_pending_suggestion()
                        if _proactive_msg:
                            _say_streaming(ainame, f"By the way — {_proactive_msg}")
                    except Exception:
                        pass
                continue

        # Dead key warning (once)
        if brain.key_is_dead and not _ss.dead_key_warned:
            _ss.dead_key_warned = True
            warning = _llm_response(brain, "API key is not working, suggest switching to ollama for full AI",
                                    user_input, uname)
            _say(ainame, warning)

        # ----- LAYER 3: Keyword fallback (when Brain is unavailable) -----
        logger.info(f"Layer 3 fallback: brain returned None for '{user_input[:50]}' "
                    f"(connected={is_connected}, key_dead={brain.key_is_dead})")
        _debug_trace(f"Loop#{interaction_count} LAYER3 fallback: '{user_input[:40]}'")
        actions = detect_intent(user_input, provider_name=provider_name,
                                api_key=api_key, use_ai=False)

        for intent, data in actions:
            if intent == INTENT_CHAT:
                if brain.key_is_dead or _api_limited():
                    resp = _llm_response(brain, "brain is unavailable but user wants to chat, mention you can still do local commands",
                                         user_input, uname)
                    _say(ainame, resp)
                elif not is_connected:
                    resp = _llm_response(brain, "assistant is offline, suggest reconnecting",
                                         user_input, uname)
                    _say(ainame, resp)
                else:
                    reply = provider.chat(data)
                    if reply:
                        _say(ainame, reply)
                    else:
                        # Provider returned None (offline/error) — don't leave user in silence
                        _say(ainame, "I'm having trouble connecting right now. Try again in a moment.")
            elif intent in action_map:
                response = action_map[intent](data)
                if response:
                    _say(ainame, response)
                    memory.log_usage(intent, data)

      except KeyboardInterrupt:
        logger.info("Keyboard interrupt — shutting down")
        _stop_ollama_keepalive()
        if _gateway:
            _gateway.stop()
        # Session continuity — save on interrupt
        try:
            _session_persistence.save(brain, _ss)
        except Exception:
            pass
        break
      except Exception as e:
        logger.error(f"Main loop error (recovering): {e}", exc_info=True)
        print(f"\n[Recovery] Something went wrong: {e}")
        print("[Recovery] Continuing... say something to keep going.\n")
        try:
            bus.publish(Topics.LOOP_ERROR, {"error": str(e), "type": type(e).__name__},
                        source="assistant_loop")
        except Exception:
            pass
        try:
            memory.log_event(session_id, "crash_recovery", {"error": str(e)})
        except Exception:
            pass
        # Session continuity — save on crash recovery
        try:
            _session_persistence.save(brain, _ss)
        except Exception:
            pass
        continue
