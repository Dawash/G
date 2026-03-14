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
from orchestration.fallback_router import build_action_map
from orchestration.fast_path import match_fast_path, execute_handler, try_fast_path
from core.control_flags import (
    trigger_emergency_stop, clear_emergency_stop,
    start_hotkey_listener,
)
from core.state import RuntimeState

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
        with open(trace_path, "a") as f:
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
    """Print full response, then start speaking first sentence immediately.

    While the first sentence plays, the caller can do housekeeping (memory
    logging, etc.). Then speaks the remaining text with barge-in support.

    This cuts perceived latency by 0.3-0.8s for multi-sentence responses
    by overlapping first-sentence TTS with post-brain processing.

    Returns:
        str or None: User's barge-in text if they interrupted, else None.
    """
    # Text mode: no TTS, just print
    if os.environ.get("G_INPUT_MODE", "").lower() == "text":
        return say(ainame, text, speak_interruptible)

    # Print full response to console immediately
    print(f"{ainame}: {text}")

    # Truncate code-heavy / long responses for TTS
    speak_text = truncate_for_speech(text)

    first, remainder = _split_first_sentence(speak_text)

    if not remainder:
        # Single sentence or too short to split — use normal interruptible path
        return speak_interruptible(speak_text)

    # Multi-sentence: speak first sentence immediately (blocking),
    # then speak remainder with barge-in support
    speak(first)

    # Speak remaining text with barge-in monitoring
    interrupted = speak_interruptible(remainder)
    return interrupted


def _llm_response(brain, situation, user_input, uname, fast_key=None):
    """Generate fresh LLM response for meta-situations."""
    return llm_response(brain, situation, user_input, uname, fast_key=fast_key)


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
                if _keepalive_stop.wait(timeout=10):
                    return
            try:
                _requests.post(
                    f"{_base_url}/api/generate",
                    json={
                        "model": ollama_model,
                        "prompt": "hi",
                        "options": {"num_predict": 1},
                    },
                    timeout=10,
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

    is_connected = True
    interaction_count = 0
    _last_session_save = time.time()

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
                    greet = _llm_response(brain, "user just said the wake word, greet them briefly",
                                          f"Hey {ainame}", uname, fast_key="wake")
                    _say(ainame, greet)
                continue

        # Auto-sleep after inactivity (voice mode only)
        _is_text = os.environ.get("G_INPUT_MODE", "").lower() == "text"
        if should_auto_sleep(_ss, is_text_mode=_is_text):
            _ss.set_mode("IDLE")
            _ss.last_mode_was_agent = False
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
                interrupted = _say(ainame, announcement)
                if interrupted:
                    user_input = interrupted
                    break
            else:
                interrupted = None
            if pending:
                _ss.touch()  # Reset inactivity timer after speaking reminders
        except Exception:
            interrupted = None

        if not interrupted:
            _debug_trace("waiting for listen()")
            user_input = listen()
            _debug_trace(f"listen() returned: {user_input[:30] if user_input else 'None'}")
        if user_input is None:
            continue

        _ss.touch()
        user_input = correct_speech(user_input)

        # Auto-save session every 60 seconds
        if time.time() - _last_session_save > 60:
            try:
                _ss.last_user_input = user_input
                _session_persistence.save(brain, _ss)
                _last_session_save = time.time()
            except Exception:
                pass

        interaction_count += 1
        logger.info(f"Loop #{interaction_count}: got input '{user_input[:50]}'")
        memory.log_event(session_id, "user_input", {"text": user_input})

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
            # Session continuity — save before exit
            try:
                _session_persistence.save(brain, _ss)
            except Exception:
                pass
            memory.close()
            farewell = _llm_response(brain, "user is saying goodbye, give a warm farewell",
                                     user_input, uname, fast_key="farewell")
            print(f"{ainame}: {farewell}")
            speak(farewell)
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

        # ----- LAYER 2a: Fast-path routing (deterministic, no LLM) -----
        # try_fast_path handles both single and multi-step commands
        # ("open Chrome and check weather" → two fast-path actions)
        _fp_t0 = time.time()
        _fp_result = try_fast_path(user_input, action_map, reminder_mgr)
        _fp_elapsed = time.time() - _fp_t0

        if _fp_result.handled and _fp_result.response is not None:
            _ss.last_response = str(_fp_result.response)
            logger.info(f"Fast-path: {_fp_result.handler_key} -> {str(_fp_result.response)[:60]}")
            _say(ainame, _fp_result.response)
            memory.log_event(session_id, "fast_path",
                             {"handler": _fp_result.handler_key or "multi_step",
                              "response": str(_fp_result.response)[:200]})
            # Track success for feedback
            try:
                from orchestration.feedback import get_feedback
                get_feedback().record_success(
                    _fp_result.handler_key or "multi_step", "fast_path", _fp_elapsed)
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
                if not check_ollama_health(ollama_url=ollama_url):
                    _brain_available = False
                    ollama_was_down[0] = True
            except Exception:
                pass

        if is_connected and _brain_available and not _api_limited() and not brain.key_is_dead:
            logging.info(f"Brain processing: '{user_input}'")

            _BRAIN_TIMEOUT = 30  # seconds — allows 2-round tool calling (12s LLM + 2s tool + 12s response)
            # Dynamic acknowledgment: longer delay for simple queries (likely fast), shorter for complex
            _ack_delay = 4.0 if len(user_input.split()) <= 5 else 2.5
            _ack_timer = threading.Timer(_ack_delay, lambda: speak_async("Working on it..."))
            _ack_timer.start()
            try:
                _t0 = time.time()
                # Run brain.think() with a hard timeout to prevent hangs
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
                    _fut = _pool.submit(
                        brain.think, user_input,
                        detected_language=get_detected_language()
                    )
                    try:
                        response = _fut.result(timeout=_BRAIN_TIMEOUT)
                    except concurrent.futures.TimeoutError:
                        logger.error(f"Brain.think() timed out after {_BRAIN_TIMEOUT}s")
                        _debug_trace(f"Loop#{interaction_count} TIMEOUT after {_BRAIN_TIMEOUT}s")
                        # Signal Brain to stop between tool rounds
                        brain._cancelled = True
                        response = "Sorry, that took too long. Could you try again?"
                _elapsed = time.time() - _t0
                logger.info(f"Brain returned in {_elapsed:.1f}s: {str(response)[:80] if response else 'None'}")
                _debug_trace(f"Loop#{interaction_count} brain={_elapsed:.1f}s resp={'yes' if response else 'None'}")

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
            except Exception as e:
                logger.error(f"Brain.think() crashed: {e}", exc_info=True)
                _debug_trace(f"Loop#{interaction_count} CRASH: {e}")
                response = None
            finally:
                _ack_timer.cancel()

            if response:
                _ss.last_response = str(response)
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

                _debug_trace(f"Loop#{interaction_count} pre-say")
                # Use streaming TTS: first sentence starts playing immediately
                # while memory logging happens in parallel
                _mem_thread = threading.Thread(
                    target=memory.log_event, daemon=True,
                    args=(session_id, "brain",
                          {"input": user_input, "response": str(response)[:200]}),
                )
                _mem_thread.start()
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
                    user_input = interrupted
                    interaction_count += 1
                    memory.log_event(session_id, "barge_in", {"text": interrupted})
                    continue
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
            memory.log_event(session_id, "crash_recovery", {"error": str(e)})
        except Exception:
            pass
        # Session continuity — save on crash recovery
        try:
            _session_persistence.save(brain, _ss)
        except Exception:
            pass
        continue
