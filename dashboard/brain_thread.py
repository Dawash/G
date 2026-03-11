"""
Brain Thread — runs LLM processing on a background QThread.

Full integration with brain.py, auto-agent spawning, agentic task
execution (plan→execute→verify→diagnose), and real-time UI updates.

Like ChatGPT agentic mode: complex tasks auto-spawn specialist agents,
each broadcasting progress to the dashboard in real-time.
"""

import logging
import queue
import re
import time
import traceback
import uuid

from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

# Keywords that signal a task needs agents
AGENT_TRIGGERS = re.compile(
    r'\b(research|investigate|compare|analyze|find all|summarize multiple|'
    r'create a plan|build a|design|write a report|study|explore|'
    r'check everything|full analysis|deep dive|comprehensive)\b',
    re.IGNORECASE
)

# Keywords for multi-step tasks (only truly compound patterns, not common words)
COMPLEX_TRIGGERS = re.compile(
    r'\b(and then|after that|first .+ then|step by step|followed by)\b',
    re.IGNORECASE
)


class BrainLogInterceptor(logging.Handler):
    """Intercepts brain.py log messages to extract thinking steps."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def emit(self, record):
        msg = record.getMessage()
        # Capture tool calls and results
        if "Brain tool call:" in msg:
            tool_info = msg.split("Brain tool call:", 1)[1].strip()
            self._callback("tool_call", tool_info)
        elif "Brain tool result:" in msg:
            result = msg.split("Brain tool result:", 1)[1].strip()
            # Truncate long results
            if len(result) > 200:
                result = result[:200] + "..."
            self._callback("tool_result", result)
        elif "Extracted tool call:" in msg:
            tool_info = msg.split("Extracted tool call:", 1)[1].strip()
            self._callback("tool_call", tool_info)
        elif "Brain hallucinated tool:" in msg:
            self._callback("warning", msg.split(":", 1)[1].strip() if ":" in msg else msg)
        elif "DesktopAgent" in msg and ("thought:" in msg or "Decision:" in msg):
            self._callback("agent_thought", msg.split(":", 1)[1].strip() if ":" in msg else msg)


class BrainThread(QThread):
    """Background thread: Brain + auto-agent spawning + agentic execution."""

    responseReady = pyqtSignal(str, str)            # role, text
    agentSpawned = pyqtSignal(str, str, str)        # agent_id, role, task
    agentFinished = pyqtSignal(str, str, str, str)  # agent_id, role, task, status
    agentMessage = pyqtSignal(str, str, str)        # agent_id, event_type, message
    thinkingChanged = pyqtSignal(bool)
    thinkingStep = pyqtSignal(str, str)             # step_type, message
    latencyUpdated = pyqtSignal(float)              # response time in seconds
    queueSizeChanged = pyqtSignal(int)              # pending messages in queue

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = queue.Queue()
        self._running = True
        self._brain = None
        self._ready = False
        self._config = None
        self._action_map = None
        self._reminder_mgr = None
        self._provider_name = "ollama"
        self._api_key = ""

    def enqueue(self, text: str):
        """Add a message to be processed."""
        self._queue.put(text)

    def stop(self):
        self._running = False
        self._queue.put(None)
        self.wait(5000)

    def run(self):
        """Initialize brain and process messages."""
        self._init_brain()

        while self._running:
            try:
                msg = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if msg is None:
                break

            # Report queue size
            qsize = self._queue.qsize()
            self.queueSizeChanged.emit(qsize)

            self.thinkingChanged.emit(True)
            start = time.time()
            try:
                self._process(msg)
            except Exception as e:
                logger.error(f"Brain error: {e}\n{traceback.format_exc()}")
                self.responseReady.emit("system", f"Error: {e}")
            finally:
                elapsed = time.time() - start
                logger.info(f"Processing took {elapsed:.1f}s")
                self.latencyUpdated.emit(elapsed)
                self.queueSizeChanged.emit(self._queue.qsize())
                self.thinkingChanged.emit(False)

    # ---- Initialization ----

    def _init_brain(self):
        """Full brain init mirroring assistant.py setup."""
        try:
            from config import load_config, DEFAULT_OLLAMA_MODEL
            from brain import Brain
            from reminders import ReminderManager
            import threading

            self._config = load_config()
            self._provider_name = self._config.get("provider", "ollama")
            self._api_key = self._config.get("api_key", "")
            uname = self._config.get("username", "User")
            ainame = self._config.get("ai_name", "G")
            ollama_model = self._config.get("ollama_model", DEFAULT_OLLAMA_MODEL)

            # Start reminder manager with speech callback
            self._reminder_mgr = ReminderManager(
                speak_fn=lambda msg: self.responseReady.emit("system", f"Reminder: {msg}")
            )
            self._reminder_mgr.start_checker()

            self._action_map = self._build_action_map()

            # module_brain removed — brain.py has its own action log now

            # Install log interceptor to capture brain tool calls for thinking panel
            brain_logger = logging.getLogger("brain")
            self._log_interceptor = BrainLogInterceptor(
                lambda stype, msg: self.thinkingStep.emit(stype, msg)
            )
            brain_logger.addHandler(self._log_interceptor)

            self.thinkingStep.emit("system", "Initializing AI brain...")

            self._brain = Brain(
                provider_name=self._provider_name,
                api_key=self._api_key,
                username=uname,
                ainame=ainame,
                action_registry=self._action_map,
                reminder_mgr=self._reminder_mgr,
                ollama_model=ollama_model,
                ollama_url=self._config.get("ollama_url", "http://localhost:11434"),
            )

            # Warm up in background thread with timeout (don't block message processing)
            self.thinkingStep.emit("system", "Warming up LLM model...")
            warmup_thread = threading.Thread(target=self._brain.warm_up, daemon=True)
            warmup_thread.start()
            warmup_thread.join(timeout=15)

            self._ready = True
            self.thinkingStep.emit("system", f"{ainame} ready — {ollama_model}")
            self.responseReady.emit("system", f"{ainame} is ready.")
            logger.info("Brain thread initialized")

        except Exception as e:
            logger.error(f"Brain init failed: {e}\n{traceback.format_exc()}")
            self.thinkingStep.emit("error", f"Init failed: {e}")
            self.responseReady.emit("system", f"Brain init failed: {e}")

    def _build_action_map(self):
        """Build action registry matching assistant.py."""
        try:
            from actions import (open_application, close_window, minimize_window,
                                 google_search, shutdown_computer, restart_computer,
                                 cancel_shutdown, sleep_computer)
            from weather import get_current_weather, get_forecast
            from news import get_briefing
            from intent import (INTENT_SHUTDOWN, INTENT_RESTART, INTENT_CANCEL_SHUTDOWN,
                                INTENT_SLEEP, INTENT_GOOGLE_SEARCH, INTENT_OPEN_APP,
                                INTENT_CLOSE_APP, INTENT_MINIMIZE_APP, INTENT_WEATHER,
                                INTENT_FORECAST, INTENT_TIME, INTENT_NEWS,
                                INTENT_SET_REMINDER, INTENT_LIST_REMINDERS)
            from datetime import datetime

            rmgr = self._reminder_mgr

            return {
                INTENT_SHUTDOWN: lambda _: shutdown_computer(),
                INTENT_RESTART: lambda _: restart_computer(),
                INTENT_CANCEL_SHUTDOWN: lambda _: cancel_shutdown(),
                INTENT_SLEEP: lambda _: sleep_computer(),
                INTENT_GOOGLE_SEARCH: lambda data: google_search(data),
                INTENT_OPEN_APP: lambda data: open_application(data),
                INTENT_CLOSE_APP: lambda data: close_window(data),
                INTENT_MINIMIZE_APP: lambda data: minimize_window(data),
                INTENT_WEATHER: lambda data: get_current_weather(data or None),
                INTENT_FORECAST: lambda data: get_forecast(data or None),
                INTENT_TIME: lambda _: f"It's {datetime.now().strftime('%A, %I:%M %p')}.",
                INTENT_NEWS: lambda data: get_briefing(data or "general"),
                INTENT_SET_REMINDER: lambda data: self._handle_reminder(data),
                INTENT_LIST_REMINDERS: lambda _: rmgr.list_active(),
            }
        except Exception as e:
            logger.warning(f"Action map partial: {e}")
            return {}

    def _handle_reminder(self, data):
        if not data:
            return "What should I remind you about?"
        if "|" in data:
            parts = data.split("|", 1)
            return self._reminder_mgr.add_reminder(parts[0].strip(), parts[1].strip())
        return self._reminder_mgr.add_reminder(data, "in 1 hour")

    def _try_keyword_fallback(self, user_text):
        """Layer 2 fallback: keyword-based intent detection (matching assistant.py).

        When brain.think() returns None (dead key, rate limited, Ollama down),
        this provides offline functionality via intent detection + action map.
        """
        try:
            from intent import detect_intent
            intent, data = detect_intent(user_text)
            if intent and intent in self._action_map:
                self.thinkingStep.emit("fallback", f"Keyword intent: {intent}")
                handler = self._action_map[intent]
                result = handler(data)
                return result
        except Exception as e:
            logger.warning(f"Keyword fallback failed: {e}")
        return None

    # ---- Processing ----

    def _process(self, user_text: str):
        """Route message: agents for complex tasks, brain for everything else."""
        if not self._brain or not self._ready:
            self.responseReady.emit("system", "Brain is not ready yet. Please wait...")
            return

        # Check if task needs agents (ChatGPT agentic mode)
        if self._needs_agents(user_text):
            self.thinkingStep.emit("routing", "Complex task detected → spawning agents")
            self._run_with_agents(user_text)
            return

        # Check for multi-step commands
        if COMPLEX_TRIGGERS.search(user_text):
            self.thinkingStep.emit("routing", "Multi-step command → task planner")
            self._run_multi_step(user_text)
            return

        # Standard brain processing
        self.thinkingStep.emit("thinking", f"Processing: {user_text[:80]}")
        try:
            response = self._brain.think(user_text)
            if response:
                self.thinkingStep.emit("done", f"Response ready ({len(str(response))} chars)")
                self.responseReady.emit("assistant", str(response))
            else:
                # Layer 2: try keyword-based intent fallback (like assistant.py)
                self.thinkingStep.emit("fallback", "No tool response, trying keyword fallback")
                fallback_result = self._try_keyword_fallback(user_text)
                if fallback_result:
                    self.responseReady.emit("assistant", str(fallback_result))
                else:
                    # Layer 3: quick_chat for conversational response
                    self.thinkingStep.emit("fallback", "No intent match, using quick_chat")
                    fallback = self._brain.quick_chat(
                        f"The user said: {user_text}. Respond helpfully."
                    )
                    if fallback:
                        self.responseReady.emit("assistant", fallback)
        except Exception as e:
            logger.error(f"Brain.think() failed: {e}")
            self.thinkingStep.emit("error", str(e))
            self.responseReady.emit("system", f"Processing error: {e}")

    def _needs_agents(self, text: str) -> bool:
        """Detect if a task should auto-spawn agents."""
        return bool(AGENT_TRIGGERS.search(text))

    # ---- Agentic Mode: Auto-spawn agents ----

    def _run_with_agents(self, user_text: str):
        """Handle complex research tasks via Brain's research mode."""
        self.responseReady.emit("system", "Researching...")

        try:
            # Brain's mode-based routing handles research/agent automatically
            coord_id = uuid.uuid4().hex[:8]
            self.agentSpawned.emit(coord_id, "coordinator", f"Researching: {user_text[:60]}")

            result = self._brain._run_research(user_text)

            self.agentFinished.emit(coord_id, "coordinator", user_text[:60], "done")

            if result:
                self.responseReady.emit("assistant", str(result)[:2000])
            else:
                self.responseReady.emit("assistant",
                    "Research completed but didn't produce a clear result.")

        except Exception as e:
            logger.error(f"Research mode failed: {e}")
            self.responseReady.emit("system", f"Research failed, using direct processing...")
            try:
                response = self._brain.think(user_text)
                if response:
                    self.responseReady.emit("assistant", str(response))
            except Exception as e2:
                self.responseReady.emit("system", f"Error: {e2}")

    # ---- Multi-step execution ----

    def _run_multi_step(self, user_text: str):
        """Handle multi-step commands via brain's mode routing."""
        try:
            response = self._brain.think(user_text)
            if response:
                self.responseReady.emit("assistant", str(response))
        except Exception as e:
            logger.error(f"Multi-step failed: {e}")
            self.responseReady.emit("system", f"Error: {e}")
