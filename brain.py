"""
LLM Brain — the intelligent core that makes G think like an AI OS.

This module gives the LLM access to system tools (open apps, search,
control windows, weather, reminders, etc.) and lets it reason about
complex tasks step-by-step, deciding which actions to take.

Like ChatGPT voice mode or Grok voice mode, but with full OS control.

How it works:
  1. User speaks a request
  2. The LLM receives the request + a list of available tools
  3. The LLM reasons about what to do and returns tool calls (or JSON actions)
  4. We execute the tool calls and feed results back to the LLM
  5. The LLM generates a final spoken response
  6. Loop continues — the LLM maintains full conversation context

Ollama support:
  - Native tool calling for models that support it (llama3.1, qwen2.5, mistral)
  - Prompt-based JSON fallback for models without tool support
  - Auto-detects which mode works and remembers it
"""

import json
import logging
import os
import re
import time
import threading
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from brain_defs import (
    build_tool_definitions,
    _TERMINAL_BLOCKED, _TERMINAL_ADMIN_REQUIRED, _FILE_BLOCKED_DIRS,
    _run_terminal, _manage_files, _manage_software,
    _CORE_TOOL_NAMES, _build_core_tools, _KNOWN_TOOL_NAMES,
    _TOOL_ALIASES, _resolve_tool_name, _extract_single_tool,
    _ARG_ALIASES, _guess_primary_arg, _normalize_tool_args,
    _tools_as_prompt_text,
    _extract_tool_from_json, _looks_like_json_garbage,
    _parse_prompt_actions,
    _toggle_system_setting,
    _press_media_key, VK_MEDIA_PLAY_PAUSE, VK_MEDIA_NEXT_TRACK,
    VK_MEDIA_PREV_TRACK, VK_VOLUME_UP, VK_VOLUME_DOWN, VK_VOLUME_MUTE,
    _wait_for_process, _open_spotify_app,
    _VERIFY_TOOLS, _APP_VERIFY, _verify_tool_completion,
    _execute_create_file,
)

# Extracted modules (Phase 4)
from llm.mode_classifier import classify_mode as _classify_mode_fn
from llm.response_builder import (
    sanitize_response as _sanitize_response_fn,
    is_llm_refusal as _is_llm_refusal_fn,
    suggest_tool_for_retry as _suggest_tool_for_retry_fn,
)
from llm.brain_service import BrainService as _BrainService
from llm.prompt_builder import (
    build_prompt_system as _build_prompt_system,
    build_brain_system_prompt as _build_brain_system_prompt,
    load_test_feedback_hints as _load_test_feedback_hints,
)
from tools.safety_policy import (
    CONFIRM_TOOLS as _CONFIRM_TOOLS,
    confirm_with_user as _confirm_with_user,
    validate_tool_choice as _validate_tool_choice,
)
from platform_impl.windows.media import play_music as _play_music_impl

# Phase 7: Registry-based tool system
from tools.registry import ToolRegistry as _ToolRegistry
from tools.executor import ToolExecutor as _ToolExecutor
from tools.cache import ResponseCache as _ResponseCache
from tools.undo_manager import UndoManager as _UndoManager
from tools.builtin_tools import register_builtin_tools as _register_builtin_tools
from tools.info_tools import register_info_tools as _register_info_tools
from tools.action_tools import register_action_tools as _register_action_tools
from tools.system_tools import register_system_tools as _register_system_tools
from tools.desktop_tools import register_desktop_tools as _register_desktop_tools
from tools.memory_workflow_tools import register_memory_workflow_tools as _register_mw_tools
from tools.browser_tools import register_browser_tools as _register_browser_tools
from tools.interactive_tools import register_interactive_tools as _register_interactive_tools

logger = logging.getLogger(__name__)

# Max tool-call rounds before forcing a text response
MAX_TOOL_ROUNDS = 3

# --- Pre-compiled patterns for hot paths in think() ---
_LANG_CODES = {
    "nepali": "hi", "nepal": "hi", "hindi": "hi", "india": "hi",
    "spanish": "es", "spain": "es", "french": "fr", "france": "fr",
    "german": "de", "germany": "de", "japanese": "ja", "japan": "ja",
    "korean": "ko", "korea": "ko", "chinese": "zh", "china": "zh",
    "portuguese": "pt", "italian": "it", "italy": "it",
    "russian": "ru", "russia": "ru", "arabic": "ar",
    "bengali": "bn", "tamil": "ta", "telugu": "te", "marathi": "mr",
    "urdu": "ur", "thai": "th", "vietnamese": "vi", "dutch": "nl",
    "turkish": "tr", "indonesian": "id", "malay": "ms", "swedish": "sv",
}
_LANG_PATTERN = re.compile(
    r'\b(?:in|into)\s+(nepali?|hindi|spanish|french|german|japanese|korean|'
    r'chinese|portuguese|italian|russian|arabic|bengali|tamil|telugu|'
    r'marathi|urdu|thai|vietnamese|dutch|turkish|indonesian|malay|swedish|'
    r'nepal|india|spain|france|germany|japan|korea|china|italy|russia)\b',
    re.I
)
# Pre-compiled patterns for knowledge question detection (hot path in _try_direct_dispatch)
_RE_KNOWLEDGE_START = re.compile(
    r'^(?:what\'?s?|who\'?s?|where|when|why|how|explain|tell me|define|describe'
    r'|translate|say|calculate|solve|give me|list|name|can you|convert)\b', re.I
)
_RE_ACTION_WORDS = re.compile(
    r'\b(open|close|launch|install|set|create|send|search for|files?|apps?'
    r'|download|reminders?|weather|forecast|news|screenshots?|alarms?|emails?'
    r'|desktop|windows?|click|type|tabs?|processes?|battery|wifi|network)\b', re.I
)
_RE_TIME_DATE = re.compile(r'\b(what|the) (time|date|day)\b', re.I)
_RE_SYSTEM_QUERY = re.compile(r'\b(my |check |how much |what\'?s? my )(ram|cpu|disk|time)\b', re.I)

# ===================================================================
# Shared runtime state (Phase 2 migration — replaces module globals)
# ===================================================================
from core.state import BrainState as _BrainState
_brain_state = _BrainState()  # Shared brain state (will be injected by container later)

# ===================================================================
# Phase 7: Tool registry + executor (registry-based dispatch)
# ===================================================================
_tool_registry = _ToolRegistry()
_response_cache_v2 = _ResponseCache()
_undo_manager = _UndoManager()

# Lazy-init orchestrator for state-first execution
_orchestrator = None
def _get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        try:
            from automation.orchestrator import StatefulOrchestrator
            _orchestrator = StatefulOrchestrator()
            logger.info("State-first orchestrator initialized")
        except Exception as e:
            logger.debug(f"Orchestrator not available: {e}")
    return _orchestrator

_tool_executor = _ToolExecutor(_tool_registry, _response_cache_v2, _undo_manager)
_register_builtin_tools(_tool_registry)
_register_info_tools(_tool_registry)
_register_action_tools(_tool_registry)
_register_system_tools(_tool_registry)
_register_desktop_tools(_tool_registry)
_register_browser_tools(_tool_registry)  # CDP persistent session (overwrites desktop_tools browser_action)
_register_mw_tools(_tool_registry)
_register_interactive_tools(_tool_registry)  # ask_user_choice, ask_user_input, ask_yes_no

# Code Interpreter (safe Python sandbox)
try:
    from tools.code_interpreter import register_code_interpreter as _register_code_interpreter
    _register_code_interpreter(_tool_registry)
except Exception:
    pass

# Set registry as the global default (used by brain_defs.py for resolution)
from tools.registry import set_default as _set_default_registry
_set_default_registry(_tool_registry)

# Set of tool names handled by the new registry (skip legacy dispatch for these)
_REGISTRY_TOOLS = frozenset(_tool_registry.all_names())

# ===================================================================
# Dynamic Tool Factory (merged from meta_brain.py)
# ===================================================================

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CUSTOM_TOOLS_FILE = os.path.join(PROJECT_DIR, "custom_tools.json")


def create_tool(name, description, python_code):
    """Dynamically create a new tool that the brain can use."""
    if not re.match(r'^[a-z][a-z0-9_]{2,30}$', name):
        return f"Invalid tool name '{name}'. Use lowercase with underscores, 3-30 chars."

    dangerous = ["os.remove", "shutil.rmtree", "os.system", "subprocess.call",
                  "eval(", "exec(", "__import__", "open(", "rm -rf"]
    for d in dangerous:
        if d in python_code:
            return f"Blocked: tool code contains dangerous operation '{d}'"

    try:
        ast_tree = compile(python_code, f"<tool:{name}>", "exec")
    except SyntaxError as e:
        return f"Syntax error in tool code: {e}"

    test_namespace = {"__builtins__": __builtins__}
    try:
        exec(ast_tree, test_namespace)
        if name not in test_namespace or not callable(test_namespace[name]):
            return f"Tool code must define a function named '{name}'"
    except Exception as e:
        return f"Tool code execution failed: {e}"

    _brain_state.dynamic_tools[name] = {
        "name": name,
        "description": description,
        "code": python_code,
        "function": test_namespace[name],
        "created": datetime.now().isoformat(),
    }
    _save_custom_tools()
    logger.info(f"Dynamic tool created: {name} — {description}")
    return f"Tool '{name}' created successfully."


def execute_dynamic_tool(name, args):
    """Execute a dynamically created tool."""
    if name not in _brain_state.dynamic_tools:
        return f"Unknown dynamic tool: {name}"
    try:
        fn = _brain_state.dynamic_tools[name]["function"]
        if isinstance(args, dict):
            return str(fn(**args))
        return str(fn(args))
    except Exception as e:
        return f"Dynamic tool '{name}' error: {e}"


def _save_custom_tools():
    """Save custom tools to disk."""
    save_data = {}
    for name, info in _brain_state.dynamic_tools.items():
        save_data[name] = {
            "name": info["name"], "description": info["description"],
            "code": info["code"], "created": info["created"],
        }
    try:
        with open(CUSTOM_TOOLS_FILE, "w") as f:
            json.dump(save_data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save custom tools: {e}")


def _load_custom_tools():
    """Load custom tools from disk and re-register them."""
    if not os.path.exists(CUSTOM_TOOLS_FILE):
        return
    try:
        with open(CUSTOM_TOOLS_FILE, "r") as f:
            data = json.load(f)
        for name, info in data.items():
            try:
                ns = {"__builtins__": __builtins__}
                exec(compile(info["code"], f"<tool:{name}>", "exec"), ns)
                if name in ns and callable(ns[name]):
                    _brain_state.dynamic_tools[name] = {
                        "name": name, "description": info["description"],
                        "code": info["code"], "function": ns[name],
                        "created": info["created"],
                    }
            except Exception as e:
                logger.warning(f"Failed to reload custom tool '{name}': {e}")
    except Exception as e:
        logger.warning(f"Failed to load custom tools: {e}")

_load_custom_tools()

# ===================================================================
# Action log — delegated to _brain_state.action_log
# ===================================================================


def log_action(module, action, result, success=True):
    """Log an action to the shared action log."""
    _brain_state.log_action(module, action, str(result)[:300], success)


def get_action_history(limit=20):
    """Get recent action history."""
    with _brain_state._lock:
        return list(_brain_state.action_log[-limit:])

# Models known to support native tool calling in Ollama
TOOL_CAPABLE_MODELS = {
    "llama3.1", "llama3.2", "llama3.3",
    "qwen2.5", "qwen2.5-coder",
    "mistral", "mistral-nemo",
    "command-r", "command-r-plus",
    "firefunction-v2",
    "nemotron",
}


def _get_dynamic_tool_names():
    """Get names of dynamically created tools."""
    return set(_brain_state.dynamic_tools.keys())


# ===================================================================
# Tool executor — maps LLM tool calls to real actions
# ===================================================================

def _play_music(action, query=None, app="spotify"):
    """Delegate to platform_impl.windows.media.play_music()."""
    last_input = getattr(execute_tool, '_last_user_input', '')
    quick_chat_fn = getattr(execute_tool, '_brain_quick_chat', None)
    return _play_music_impl(action, query, app,
                            last_user_input=last_input,
                            quick_chat_fn=quick_chat_fn)


def _run_agent_with_timeout(goal, timeout=20, blocking=True):
    """Run desktop agent with a timeout to prevent blocking Ollama.

    If blocking=False, fires agent in background thread and returns immediately.
    This prevents nested agent calls from blocking the main Brain loop.
    """
    if not blocking:
        # Fire-and-forget: run agent in background, don't block caller
        import threading
        def _bg_agent():
            try:
                from desktop_agent import DesktopAgent
                ar = getattr(execute_tool, '_action_registry', None) or {}
                agent = DesktopAgent(action_registry=ar)
                agent.execute(goal)
            except Exception as e:
                logger.error(f"Background agent failed: {e}")
        t = threading.Thread(target=_bg_agent, daemon=True)
        t.start()
        logger.info(f"Agent escalation launched in background: {goal[:60]}")
        return None

    try:
        from desktop_agent import DesktopAgent
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        ar = getattr(execute_tool, '_action_registry', None) or {}
        agent = DesktopAgent(action_registry=ar)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(agent.execute, goal)
            return future.result(timeout=timeout)
    except FuturesTimeout:
        agent.cancel()  # Signal agent to stop — frees Ollama
        logger.warning(f"Agent escalation timed out after {timeout}s: {goal[:60]}")
        return None
    except Exception as e:
        logger.error(f"Agent escalation failed: {e}")
        return None


# Safety policy: _CONFIRM_TOOLS, _confirm_with_user, _validate_tool_choice
# → see tools/safety_policy.py


def _register_undo_for_tool(tool_name, arguments, action_registry):
    """Register undo handlers for reversible tool actions."""
    with _state_lock:
        if tool_name == "open_app":
            name = arguments.get("name", "")
            _undo_stack.append({
                "time": time.time(),
                "tool": tool_name,
                "args": arguments,
                "rollback_fn": lambda n=name: action_registry.get("close_app", lambda x: "No close handler")(n),
                "description": f"opened {name}",
            })
        elif tool_name == "close_app":
            name = arguments.get("name", "")
            _undo_stack.append({
                "time": time.time(),
                "tool": tool_name,
                "args": arguments,
                "rollback_fn": lambda n=name: action_registry.get("open_app", lambda x: "No open handler")(n),
                "description": f"closed {name}",
            })
        elif tool_name == "toggle_setting":
            setting = arguments.get("setting", "")
            state = arguments.get("state", "off")
            opposite = "on" if state == "off" else "off"
            _undo_stack.append({
                "time": time.time(),
                "tool": tool_name,
                "args": arguments,
                "rollback_fn": lambda: _toggle_system_setting(setting, opposite),
                "description": f"toggled {setting} {state}",
            })
        # Keep stack bounded
        if len(_undo_stack) > 10:
            del _undo_stack[:len(_undo_stack) - 10]


# ===================================================================
# Module-level state — delegated to _brain_state (core.state.BrainState)
# ===================================================================
# Legacy aliases for code that still reads these directly:
_undo_stack = _brain_state.undo_stack
_recent_actions = _brain_state.recent_actions
_state_lock = _brain_state._lock
_response_cache = _brain_state.response_cache

# Track last created file for pronoun resolution ("open it")
# Migrated to _brain_state.last_created_file (core.state.BrainState)

# Cognitive learning reference (set by Brain.__init__)
# Migrated to _brain_state.experience_learner (core.state.BrainState)

_CACHE_TTL = {
    "get_weather": 300,    # 5 minutes
    "get_forecast": 300,   # 5 minutes
    "get_time": 30,        # 30 seconds
    "get_news": 600,       # 10 minutes
}


def _log_learning(user_input, tool_name, arguments, result):
    """Log tool outcome to the cognitive engine (learning + comprehension)."""
    # Use CognitiveEngine if available (handles both learning + referent tracking)
    cog = getattr(_log_learning, '_cognition', None)
    if cog:
        try:
            result_str = str(result) if result else ""
            is_success = not any(w in result_str.lower() for w in [
                "error", "failed", "not found", "blocked", "timed out",
                "permission denied", "could not", "couldn't",
            ])
            cog.log_outcome(user_input, tool_name, arguments, is_success, result_str)
            return
        except Exception:
            pass
    # Fallback: use raw ExperienceLearner
    if _brain_state.experience_learner:
        try:
            result_str = str(result) if result else ""
            is_success = not any(w in result_str.lower() for w in [
                "error", "failed", "not found", "blocked", "timed out",
                "permission denied", "could not", "couldn't",
            ])
            _brain_state.experience_learner.log_outcome(user_input, tool_name, arguments, is_success, result_str)
        except Exception:
            pass


def execute_tool(tool_name, arguments, action_registry, reminder_mgr=None, speak_fn=None):
    """
    Execute a tool call from the LLM and return the result string.
    Maps tool names to the action_registry handlers.

    Phase 7: Registry-based tools (open_app, google_search, get_weather,
    set_reminder, send_email) go through the new ToolExecutor.
    All other tools fall through to the legacy _execute_tool_inner path.

    For complex tools (play_music, search_in_app, open_app, google_search),
    after execution verifies the task actually completed. If only partially
    done (e.g. Spotify opened but jazz not playing), auto-escalates to
    agent_task (agentic mode) to finish the job.
    """
    # Validate tool choice against user intent (catches LLM stickiness)
    user_input = getattr(execute_tool, '_last_user_input', '')
    tool_name = _validate_tool_choice(tool_name, user_input)

    logger.info(f"execute_tool called: {tool_name}({arguments})")
    execute_tool._action_registry = action_registry  # For agent escalation

    # Cognitive Phase 4: confidence-based tool switching
    cog = getattr(_log_learning, '_cognition', None)
    if cog and tool_name not in _CONFIRM_TOOLS:
        try:
            confidence = cog.get_confidence(user_input, tool_name)
            if confidence < 0.3:
                alt = cog.find_alternative(tool_name, user_input)
                if alt and alt.get("tool"):
                    logger.info(f"Cognitive: low confidence ({confidence:.0%}) for {tool_name}, "
                                f"switching to {alt['tool']} ({alt['reason']})")
                    tool_name = alt["tool"]
        except Exception:
            pass

    # --- ANSWER CHECK: tool blacklist (EasyTool pattern) ---
    # If this tool was already blacklisted for the current request, try alternative
    brain_ref = getattr(execute_tool, '_brain_ref', None)
    if brain_ref and tool_name in getattr(brain_ref, '_tool_blacklist', set()):
        logger.info(f"Tool {tool_name} is blacklisted — trying alternative")
        cog = getattr(_log_learning, '_cognition', None)
        if cog:
            alt = cog.find_alternative(tool_name, user_input)
            if alt and alt.get("tool") and alt["tool"] not in brain_ref._tool_blacklist:
                logger.info(f"Blacklist redirect: {tool_name} → {alt['tool']}")
                tool_name = alt["tool"]

    # --- Phase 7: Registry-based dispatch for migrated tools ---
    # Inject orchestrator for state-first execution (lazy init)
    if _tool_executor._orchestrator is None:
        _tool_executor._orchestrator = _get_orchestrator()

    if tool_name in _REGISTRY_TOOLS:
        result = _tool_executor.execute(
            tool_name, arguments, action_registry,
            reminder_mgr=reminder_mgr,
            speak_fn=speak_fn,
            user_input=user_input,
            cognition=cog,
            experience_learner=_brain_state.experience_learner,
            log_action_fn=log_action,
            fallback_fn=_execute_tool_inner,
        )

        # Record action for "do that again" replay (thread-safe)
        with _state_lock:
            _recent_actions.append((tool_name, arguments, str(result)[:200]))
            if len(_recent_actions) > 5:
                del _recent_actions[0]

        # Track last created file for pronoun resolution ("open it")
        if tool_name == "create_file" and result and "Created file:" in str(result):
            import re as _re
            path_match = _re.search(r'Created file: (.+)', str(result))
            if path_match:
                _brain_state.last_created_file = path_match.group(1).strip()

        # Failure recovery: suggest similar apps if "not found"
        if tool_name == "open_app" and result and "not found" in str(result).lower():
            name = arguments.get("name", "")
            try:
                from app_finder import find_similar_apps
                alts = find_similar_apps(name, limit=3)
                if alts:
                    return f"Couldn't find {name}. Did you mean: {', '.join(alts)}?"
            except Exception:
                pass

        # Verify tool completion — auto-escalate to agent if partial
        # Skip if already verified by orchestrator, or brain was cancelled
        _already_verified = getattr(_tool_executor, '_last_orchestrator_verified', False)
        _tool_executor._last_orchestrator_verified = False  # Reset for next call
        spec = _tool_registry.get(tool_name)
        if spec and spec.verifier and result and "error" not in str(result).lower() and not _already_verified:
            # Check if Brain was cancelled before expensive verification
            if hasattr(execute_tool, '_brain_ref') and getattr(execute_tool._brain_ref, '_cancelled', False):
                return result
            is_complete, v_done, v_missing = spec.verifier(arguments, result, user_input)
            if not is_complete and v_missing:
                logger.info(f"Tool {tool_name} PARTIAL — done: {v_done}, missing: {v_missing}")
                agent_result = _auto_escalate_to_agent(
                    tool_name, arguments, v_done, v_missing,
                    user_input=user_input,
                    action_registry=action_registry,
                    speak_fn=speak_fn,
                )
                if agent_result:
                    if _brain_state.experience_learner:
                        try:
                            _brain_state.experience_learner.log_recovery(
                                user_input, tool_name, "agent_task", {"goal": v_missing})
                        except Exception:
                            pass
                    return agent_result

        return result

    # --- Legacy path for non-registry tools ---

    # User confirmation for sensitive tools (send_email, system shutdown/restart)
    if tool_name in _CONFIRM_TOOLS:
        if not _confirm_with_user(tool_name, arguments, speak_fn):
            return f"Cancelled — user did not confirm {tool_name}."

    # Response cache: return cached result for repeated queries (weather, time, news)
    cache_key = f"{tool_name}:{json.dumps(arguments, sort_keys=True)}"
    if tool_name in _CACHE_TTL:
        with _state_lock:
            cached = _response_cache.get(cache_key)
        if cached and (time.time() - cached[1]) < _CACHE_TTL[tool_name]:
            logger.info(f"Cache hit: {tool_name}")
            return cached[0]

    result = _execute_tool_inner(tool_name, arguments, action_registry, reminder_mgr, speak_fn)

    # Store in cache if cacheable and successful
    if tool_name in _CACHE_TTL and result and "error" not in str(result).lower():
        with _state_lock:
            _response_cache[cache_key] = (result, time.time())

    # Record action for "do that again" replay + action log (thread-safe)
    with _state_lock:
        _recent_actions.append((tool_name, arguments, str(result)[:200]))
        if len(_recent_actions) > 5:
            del _recent_actions[0]
    log_action("brain", f"{tool_name}({json.dumps(arguments)[:100]})", str(result)[:200])

    # Register undo for reversible actions
    _register_undo_for_tool(tool_name, arguments, action_registry)

    # Log outcome for cognitive learning
    _log_learning(user_input, tool_name, arguments, result)

    # Failure recovery: suggest similar apps if "not found" (Phase 9)
    if tool_name == "open_app" and result and "not found" in str(result).lower():
        name = arguments.get("name", "")
        try:
            from app_finder import find_similar_apps
            alts = find_similar_apps(name, limit=3)
            if alts:
                return f"Couldn't find {name}. Did you mean: {', '.join(alts)}?"
        except Exception:
            pass

    # Verify tool completion — auto-escalate to agent if partial
    # Check if Brain was cancelled before expensive verification
    brain_ref = getattr(execute_tool, '_brain_ref', None)
    if brain_ref and getattr(brain_ref, '_cancelled', False):
        return result
    if tool_name in _VERIFY_TOOLS and result and "error" not in str(result).lower():
        user_input = getattr(execute_tool, '_last_user_input', '')
        is_complete, v_done, v_missing = _verify_tool_completion(
            tool_name, arguments, result, user_input
        )
        if not is_complete and v_missing:
            logger.info(f"Tool {tool_name} PARTIAL — done: {v_done}, missing: {v_missing}")
            agent_result = _auto_escalate_to_agent(
                tool_name, arguments, v_done, v_missing,
                user_input=user_input,
                action_registry=action_registry,
                speak_fn=speak_fn,
            )
            if agent_result:
                if _brain_state.experience_learner:
                    try:
                        _brain_state.experience_learner.log_recovery(
                            user_input, tool_name, "agent_task", {"goal": v_missing})
                    except Exception:
                        pass
                return agent_result

    return result


def _execute_tool_inner(tool_name, arguments, action_registry, reminder_mgr=None, speak_fn=None):
    """Inner tool execution — all the actual tool dispatch logic."""
    try:
        if tool_name == "open_app" and "open_app" in action_registry:
            name = arguments.get("name", "").strip()
            if not name:
                return "Error: no app name provided."
            # Pronoun resolution: "open it" → open last created file
            if name.lower() in ("it", "this", "that", "the file", "the result") and _brain_state.last_created_file:
                import os, subprocess
                if os.path.exists(_brain_state.last_created_file):
                    subprocess.Popen(["start", "", _brain_state.last_created_file], shell=True)
                    return f"Opening {os.path.basename(_brain_state.last_created_file)}"
            # App category resolution: "browser" → user's preferred browser
            try:
                from memory import UserPreferences, MemoryStore
                _prefs = UserPreferences(MemoryStore())
                resolved = _prefs.resolve_app_category(name)
                if resolved.lower() != name.lower():
                    logger.info(f"App category '{name}' → '{resolved}'")
                    name = resolved
            except Exception:
                pass
            return action_registry["open_app"](name)

        elif tool_name == "close_app" and "close_app" in action_registry:
            return action_registry["close_app"](arguments.get("name", ""))

        elif tool_name == "minimize_app" and "minimize_app" in action_registry:
            return action_registry["minimize_app"](arguments.get("name", ""))

        elif tool_name == "google_search" and "google_search" in action_registry:
            return action_registry["google_search"](arguments.get("query", ""))

        elif tool_name == "get_weather":
            city = arguments.get("city", "") or None
            # Always call weather.py directly — action_registry lambda ignores city
            from weather import get_current_weather
            return get_current_weather(city)

        elif tool_name == "get_forecast":
            city = arguments.get("city", "") or None
            from weather import get_forecast
            return get_forecast(city)

        elif tool_name == "get_time":
            if "time" in action_registry:
                return action_registry["time"](None)
            from datetime import datetime
            return f"It's {datetime.now().strftime('%A, %I:%M %p')}."

        elif tool_name == "get_news":
            category = arguments.get("category", "general")
            query = arguments.get("query", None)
            country = arguments.get("country", None)
            from news import get_briefing
            return get_briefing(category, query=query, country=country)

        elif tool_name == "set_reminder" and "set_reminder" in action_registry:
            msg = arguments.get("message", "")
            t = arguments.get("time", "in 1 hour")
            return action_registry["set_reminder"](f"{msg}|{t}")

        elif tool_name == "list_reminders" and "list_reminders" in action_registry:
            return action_registry["list_reminders"](None)

        elif tool_name == "system_command":
            cmd = arguments.get("command", "")
            # Safety: only execute actual power commands, not feature toggles
            valid_power_cmds = {"shutdown", "restart", "sleep", "cancel_shutdown"}
            if cmd not in valid_power_cmds:
                return f"'{cmd}' is not a power command. Use agent_task for settings changes."
            # Safety: check if user actually asked to power off the computer
            # Prevent "turn off bluetooth" from triggering shutdown
            if cmd in ("shutdown", "restart") and hasattr(execute_tool, '_last_user_input'):
                user_text = execute_tool._last_user_input.lower()
                # If user mentioned a feature/device (not the computer), redirect
                feature_words = ["bluetooth", "wifi", "wi-fi", "hotspot", "location",
                                 "airplane", "vpn", "night light", "dark mode",
                                 "brightness", "volume", "notification"]
                if any(w in user_text for w in feature_words):
                    # Misrouted! User wants to toggle a feature, not power off
                    logger.warning(f"Blocked misrouted {cmd} — user said: {user_text}")
                    return f"I won't {cmd} — you asked about a feature, not the computer. Use agent_task instead."
            if cmd in action_registry:
                return action_registry[cmd](None)
            return f"Unknown system command: {cmd}"

        # --- System settings toggle (Bluetooth, WiFi, etc.) ---
        elif tool_name == "toggle_setting":
            setting = arguments.get("setting", "").lower()
            state = arguments.get("state", "off").lower()
            return _toggle_system_setting(setting, state)

        # --- Music playback control ---
        elif tool_name == "play_music":
            # Post-process: ensure query is populated from user input if LLM forgot it
            if arguments.get("action") in ("play", "play_query", None) and not arguments.get("query"):
                user_input = getattr(execute_tool, '_last_user_input', '')
                if user_input:
                    # Extract music terms from user input
                    import re as _re
                    # Remove command words, keep genre/song/artist
                    cleaned = _re.sub(r'^(play|listen to|put on|start)\s+', '', user_input, flags=_re.I)
                    cleaned = _re.sub(r'\s+(on|in|using|with|from)\s+(spotify|youtube|browser).*$', '', cleaned, flags=_re.I)
                    cleaned = _re.sub(r'^(some|a|the|my|me)\s+', '', cleaned, flags=_re.I)
                    cleaned = cleaned.strip()
                    if cleaned and len(cleaned) > 1:
                        arguments["query"] = cleaned
                        logger.info(f"play_music: injected missing query '{cleaned}' from user input")
            action = arguments.get("action", "play")
            query = arguments.get("query", "")
            app = arguments.get("app", "spotify")
            return _play_music(action, query, app)

        # --- New agentic tools ---
        elif tool_name == "send_email":
            from email_sender import send_email
            return send_email(
                arguments.get("to", ""),
                arguments.get("subject", ""),
                arguments.get("body", ""),
            )

        elif tool_name == "web_read":
            from web_agent import web_read
            return web_read(arguments.get("url", ""))

        elif tool_name == "web_search_answer":
            from web_agent import web_search_extract
            return web_search_extract(arguments.get("query", ""))

        elif tool_name == "manage_alarm":
            from alarms import get_alarm_manager
            am = get_alarm_manager()
            if not am:
                return "Alarm system not available."
            action = arguments.get("action", "add").lower()
            if action == "add" or action == "set":
                time_str = arguments.get("time", "")
                if not time_str:
                    return "Please specify a time for the alarm, e.g. '7am'."
                label = arguments.get("label", "Alarm")
                alarm_type = arguments.get("type", "morning")
                recurrence = arguments.get("recurrence", None)
                return am.add_alarm(time_str, alarm_type=alarm_type,
                                    label=label, recurrence=recurrence)
            elif action == "list":
                return am.list_alarms()
            elif action in ("remove", "delete", "cancel"):
                alarm_id = arguments.get("alarm_id", "")
                if not alarm_id:
                    return "Please specify which alarm to remove."
                return am.remove_alarm(alarm_id)
            elif action in ("toggle", "enable", "disable"):
                alarm_id = arguments.get("alarm_id", "")
                active = action != "disable"
                return am.toggle_alarm(alarm_id, active=active)
            return f"Unknown alarm action: {action}"

        elif tool_name == "run_self_test":
            from self_test import run_self_test
            return run_self_test()

        elif tool_name == "restart_assistant":
            return "__RESTART__"  # Signal to assistant.py to restart

        # run_dev_team and run_guardian handlers removed — developer-only

        elif tool_name == "create_file":
            result = _execute_create_file(
                arguments.get("path", ""),
                arguments.get("content", ""),
                quick_chat_fn=getattr(execute_tool, '_brain_quick_chat', None),
                user_request=getattr(execute_tool, '_last_user_input', '') or '',
            )
            # Track last created file for pronoun resolution ("open it")
            if result and "Created file:" in str(result):
                import re as _re
                path_match = _re.search(r'Created file: (.+)', str(result))
                if path_match:
                    _brain_state.last_created_file = path_match.group(1).strip()
            return result

        # --- Desktop automation tools ---
        elif tool_name == "search_in_app":
            from computer import search_in_app
            return search_in_app(
                arguments.get("app", ""),
                arguments.get("query", ""),
            )

        elif tool_name == "type_text":
            from computer import type_text
            return type_text(arguments.get("text", ""))

        elif tool_name == "press_key":
            from computer import press_key
            return press_key(arguments.get("keys", ""))

        elif tool_name == "click_at":
            from computer import click_at
            return click_at(
                arguments.get("x", 0),
                arguments.get("y", 0),
                arguments.get("button", "left"),
            )

        elif tool_name == "scroll":
            direction = arguments.get("direction", "down").lower()
            try:
                import pyautogui
                clicks = -3 if direction == "down" else 3
                pyautogui.scroll(clicks)
                return f"Scrolled {direction}."
            except Exception as e:
                return f"Scroll failed: {e}"

        # --- Clipboard tools ---
        elif tool_name == "read_clipboard":
            try:
                import pyperclip
                clip = pyperclip.paste()
                if not clip or not clip.strip():
                    return "Clipboard is empty."
                import re as _re
                urls = _re.findall(r'https?://[^\s<>"\']+', clip)
                if urls:
                    return f"Clipboard contains URL: {urls[0]}" + (
                        f" (and {len(urls)-1} more)" if len(urls) > 1 else "")
                return f"Clipboard text ({len(clip)} chars): {clip[:1500]}"
            except Exception as e:
                return f"Could not read clipboard: {e}"

        elif tool_name == "analyze_clipboard_image":
            try:
                from PIL import ImageGrab
                img = ImageGrab.grabclipboard()
                if img is None:
                    return "No image in clipboard. The clipboard may contain text instead."
                from vision import analyze_screen
                question = arguments.get("question", "Describe what you see in this image.")
                return analyze_screen(question, image=img)
            except ImportError:
                return "Pillow (PIL) is required for clipboard image analysis."
            except Exception as e:
                return f"Failed to analyze clipboard image: {e}"

        # --- Screen vision tools ---
        elif tool_name == "take_screenshot":
            from vision import analyze_screen
            return analyze_screen(arguments.get("question", "What is on the screen?"))

        elif tool_name == "find_on_screen":
            element_name = arguments.get("element", "")

            # Try 1: UI Automation accessibility tree (fast, precise)
            try:
                from computer import get_ui_elements
                elements = get_ui_elements(max_elements=40)
                name_lower = element_name.lower()
                for el in elements:
                    if name_lower in el["name"].lower() or el["name"].lower() in name_lower:
                        return f"Found '{el['name']}' ({el['type']}) at ({el['x']}, {el['y']})"
                # Fuzzy match
                from difflib import get_close_matches
                el_names = [e["name"] for e in elements if e["name"]]
                matches = get_close_matches(element_name, el_names, n=1, cutoff=0.5)
                if matches:
                    for el in elements:
                        if el["name"] == matches[0]:
                            return f"Found '{el['name']}' ({el['type']}) at ({el['x']}, {el['y']})"
            except Exception as e:
                logger.debug(f"UI Automation find failed: {e}")

            # Try 2: Vision (llava) — slower but handles visual elements
            from vision import find_element
            result = find_element(element_name)
            if result.get("found"):
                return f"Found at ({result['x']}, {result['y']}): {result.get('description', '')}"
            else:
                return f"Not found: {result.get('description', 'element not visible')}"

        # --- Precision interaction tools ---
        elif tool_name == "click_element":
            from computer import click_element_by_name
            return click_element_by_name(arguments.get("name", ""))

        elif tool_name == "manage_tabs":
            from computer import manage_tabs
            return manage_tabs(
                arguments.get("action", "list"),
                arguments.get("index"),
            )

        elif tool_name == "fill_form":
            from computer import fill_form_fields
            return fill_form_fields(arguments.get("fields", {}))

        # --- Terminal, Files, Software tools ---
        elif tool_name == "run_terminal":
            cmd = arguments.get("command", "")
            admin = arguments.get("admin", False)
            return _run_terminal(cmd, admin)

        elif tool_name == "manage_files":
            return _manage_files(
                arguments.get("action", "list"),
                arguments.get("path", ""),
                arguments.get("destination"),
            )

        elif tool_name == "manage_software":
            return _manage_software(
                arguments.get("action", "search"),
                arguments.get("name"),
            )

        elif tool_name == "agent_task":
            goal = arguments.get("goal", "")
            goal_lower = goal.lower()

            # Smart pre-routing: intercept tasks that have simpler direct solutions
            # Spotify music — search_in_app handles open + search + auto-play
            if any(w in goal_lower for w in ["spotify", "play music", "play a song", "play song"]):
                music_query = "popular hits"
                # Extract what to play from the goal
                for pattern in ["play (.+?) on spotify", "play (.+?) in spotify",
                                "play (.+?) spotify", "spotify.*play (.+)",
                                "play (.+)"]:
                    m = re.search(pattern, goal_lower)
                    if m:
                        extracted = m.group(1).strip()
                        # Clean up articles and filler words
                        extracted = re.sub(r'^(a |an |some |the |any )', '', extracted).strip()
                        if extracted and extracted not in ("music", "song", "songs"):
                            music_query = extracted
                        break
                from computer import search_in_app as _search_spotify
                return _search_spotify("Spotify", music_query)

            # Bluetooth/WiFi toggle — open settings page directly, then use agent
            # for the toggle part only
            if any(w in goal_lower for w in ["bluetooth", "wifi", "wi-fi"]):
                setting = "bluetooth" if "bluetooth" in goal_lower else "wifi"
                # Open settings page directly first
                if "open_app" in action_registry:
                    action_registry["open_app"](setting)
                    import time as _time
                    _time.sleep(2)  # Wait for settings to load

            from desktop_agent import DesktopAgent
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
            agent = DesktopAgent(
                action_registry=action_registry,
                reminder_mgr=reminder_mgr,
                speak_fn=speak_fn,
            )
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(agent.execute, goal)
                    result = future.result(timeout=30)
                return result or "Task completed."
            except FuturesTimeout:
                agent.cancel()  # Signal agent to stop — frees Ollama
                logger.warning(f"agent_task timed out after 30s: {goal[:60]}")
                return "Task took too long. Some steps may have completed."

        # spawn_agents, chain_tasks, reason_deeply, delegate_task handlers removed
        # — replaced by mode-based routing (agent mode + research mode)

        # --- Dynamic custom tools ---
        elif tool_name in _get_dynamic_tool_names():
            return execute_dynamic_tool(tool_name, arguments)

        else:
            return f"Unknown tool: {tool_name}"

    except Exception as e:
        logger.error(f"Tool execution error ({tool_name}): {e}")
        return f"Error executing {tool_name}: {e}"


# ===================================================================
# Post-tool verification + agentic escalation
# ===================================================================

# Escalation state — delegated to _brain_state
_MAX_ESCALATION_DEPTH = 2


def _auto_escalate_to_agent(tool_name, arguments, what_done, what_missing, user_input="",
                            action_registry=None, speak_fn=None):
    """Build and execute a agent_task to finish a partially completed action.

    Called when _verify_tool_completion detects PARTIAL completion.
    E.g. Spotify is open but 'jazz' isn't playing → agent_task finishes it.

    Returns: result string from desktop_agent, or None.
    """
    query_key = arguments.get("query", arguments.get("name", ""))
    if not _brain_state.can_escalate(tool_name, query_key):
        logger.warning(f"Escalation blocked for {tool_name} (depth or cooldown)")
        _brain_state.reset_escalation()
        return None
    _brain_state.record_escalation(tool_name, query_key)
    app = ""
    query = ""
    if tool_name == "play_music":
        app = arguments.get("app", "spotify")
        query = arguments.get("query", "")
    elif tool_name == "search_in_app":
        app = arguments.get("app", "")
        query = arguments.get("query", "")
    elif tool_name == "open_app":
        app = arguments.get("name", "")
    elif tool_name == "google_search":
        query = arguments.get("query", "")
        app = "browser"

    done_str = "; ".join(what_done) if what_done else "nothing confirmed"
    missing_str = "; ".join(what_missing) if what_missing else "unknown"

    # Build specific agentic goal
    if tool_name in ("play_music", "search_in_app") and query and app:
        goal = (
            f"{app} is already open on screen. "
            f"Search for '{query}' in {app} and play it. "
            f"Look for the search bar, type '{query}', press Enter, "
            f"then click the first result to play it."
        )
    elif tool_name == "google_search" and query:
        goal = (
            f"A browser is already open. Navigate to Google and search for '{query}'. "
            f"Click the address bar, type 'google.com', press Enter, "
            f"then search for '{query}'."
        )
    elif tool_name == "open_app" and app:
        goal = f"Open {app}. Try the Start Menu or taskbar."
    else:
        goal = user_input or f"Complete: {tool_name} with {arguments}"

    logger.info(f"Auto-escalating to agent_task: {goal[:80]} "
                f"(done: {done_str}, missing: {missing_str})")

    # Store action_registry for _run_agent_with_timeout
    if action_registry:
        execute_tool._action_registry = action_registry
    # Run agent to finish the partially completed task
    result = _run_agent_with_timeout(goal, timeout=40, blocking=True)
    _brain_state.reset_escalation()
    if result:
        return result
    return f"Finishing up: {goal[:80]}"


# Prompt builders: _build_prompt_system, _build_brain_system_prompt, _load_test_feedback_hints
# → see llm/prompt_builder.py


class Brain:
    """
    The AI brain that reasons about tasks and controls the OS.

    Supports two tool-calling modes:
    1. Native tool calling (OpenAI format) — for OpenAI, Anthropic, and
       Ollama models that support it (llama3.1, qwen2.5, mistral, etc.)
    2. Prompt-based tool calling — for Ollama models without native support.
       Tools are described in the system prompt; LLM outputs JSON actions.

    Auto-detects which mode works for the current model.
    """

    def __init__(self, provider_name, api_key, username, ainame,
                 action_registry, reminder_mgr=None, ollama_model=None,
                 user_preferences=None, ollama_url=None):
        self.provider_name = provider_name
        self.api_key = api_key
        self.username = username
        self.ainame = ainame
        self.action_registry = action_registry
        self.reminder_mgr = reminder_mgr
        self.user_preferences = user_preferences
        self.speak_fn = None  # Set by assistant.py for desktop agent progress
        self.ollama_url = (ollama_url or "http://localhost:11434").rstrip("/")
        # Tool schemas from registry (single source of truth)
        self.tools_full = _tool_registry.build_llm_schemas()
        # Ollama local models get overwhelmed with 27 tools — send core set only
        if provider_name == "ollama":
            self.tools = _tool_registry.build_llm_schemas(core_only=True)
        else:
            self.tools = self.tools_full
        self._svc = _BrainService(username, ainame, max_context=10)
        self._ctx = self._svc.ctx
        try:
            from config import DEFAULT_OLLAMA_MODEL
        except ImportError:
            DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
        self.ollama_model = ollama_model or DEFAULT_OLLAMA_MODEL

        # Determine tool-calling mode
        self._use_native_tools = True  # Default: try native tools
        if provider_name == "ollama":
            model_base = self.ollama_model.split(":")[0].lower()
            self._use_native_tools = model_base in TOOL_CAPABLE_MODELS
            if not self._use_native_tools:
                logger.info(f"Model '{self.ollama_model}' — using prompt-based tool calling")

        # Set system prompt based on mode
        if self._use_native_tools:
            self.system_prompt = _build_brain_system_prompt(
                username, ainame,
                user_preferences=self._get_pref_dict())
        else:
            self.system_prompt = _build_prompt_system(username, ainame)

        # Track if native tools failed (auto-fallback, recoverable)
        self._native_tools_failed = False
        self._prompt_mode_calls = 0        # calls since switching to prompt mode
        self._PROMPT_MODE_RETRY = 2        # retry native after N prompt-mode calls (fast recovery)

        # Track permanently dead API keys (insufficient_quota / invalid)
        self._key_dead = False
        self._key_dead_warned = False

        # Structured call trace — written after each think() for external consumers
        self.last_call_trace = None
        self._trace_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "brain_trace.json"
        )

        # Undo registry (Phase 5): track reversible actions
        self._undo_stack = []  # [{time, tool, args, rollback_fn, description}]
        self._UNDO_WINDOW = 30  # seconds — only undo within this window

        # Recent actions buffer (Phase 6): for "do that again"
        self._recent_actions = []  # Last 5 (tool_name, args, result)

        # Cancellation flag — set by assistant_loop on timeout
        self._cancelled = False

        # Cognitive engine: lazy-loaded on first use (saves 1-2s startup)
        self._cognition = None
        self._learner = None
        self._cognition_loaded = False

        # --- UPGRADE: Tool blacklist (EasyTool answer_check pattern) ---
        # Tools that failed for the current request — avoid re-picking
        self._tool_blacklist = set()  # Reset per think() call

        # --- UPGRADE: Skill library (Voyager pattern) ---
        self._skill_lib = None  # Lazy-loaded
        self._skill_lib_loaded = False

    def _ensure_cognition(self):
        """Lazy-load CognitiveEngine on first use."""
        if self._cognition_loaded:
            return
        self._cognition_loaded = True
        try:
            from cognitive import CognitiveEngine
            self._cognition = CognitiveEngine()
            self._learner = self._cognition.learner
            _brain_state.experience_learner = self._learner
            _log_learning._cognition = self._cognition
        except Exception as e:
            logger.warning(f"Cognitive engine init failed: {e}")

    def _ensure_skill_lib(self):
        """Lazy-load SkillLibrary on first use."""
        if self._skill_lib_loaded:
            return
        self._skill_lib_loaded = True
        try:
            from skills import SkillLibrary
            self._skill_lib = SkillLibrary()
        except Exception as e:
            logger.debug(f"Skill library init failed: {e}")

    def _check_skill_library(self, user_input):
        """Check if a stored skill matches the user's request (Voyager pattern).

        Returns the skill's step descriptions if found, None otherwise.
        Also checks required credentials before returning a match.
        """
        self._ensure_skill_lib()
        if not self._skill_lib:
            return None

        try:
            matches = self._skill_lib.find_skill(user_input, min_similarity=0.7, limit=1)
            if matches:
                match = matches[0]
                logger.info(f"Skill library match: {match['name']} "
                            f"(similarity={match['similarity']:.2f}, "
                            f"used {match['success_count']}x)")

                # Check if required credentials are available
                ok, missing = self._skill_lib.check_credentials(match["name"])
                if not ok:
                    logger.info(f"Skill {match['name']} skipped — "
                                f"missing credentials: {missing}")
                    return None  # Fall through to LLM

                return match
        except Exception as e:
            logger.debug(f"Skill library lookup failed: {e}")
        return None

    def _try_direct_dispatch(self, user_input):
        """Direct mode: handle simple requests without LLM (0ms latency).

        Uses the StrategySelector to try all execution strategies in order:
        CLI → API → TOOL → UIA → CDP, before falling back to LLM.
        Also handles compound requests (split-screen, parallel tasks).
        Returns result string, or None to fall through to LLM.
        """
        if not user_input or not user_input.strip():
            return None

        try:
            from execution_strategies import (
                get_selector, detect_split_screen, execute_split_screen,
                detect_parallel_tasks, match_direct_tool,
                match_cli_command, execute_cli,
            )
            import execution_strategies as _es
        except ImportError:
            return None

        # Set quick_chat function so API handlers can expand vague queries
        _es._quick_chat_fn = self.quick_chat

        # --- Compound requests (must check before single-strategy) ---

        # Split-screen: "open firefox and spotify side by side"
        split = detect_split_screen(user_input)
        if split:
            app1, app2 = split
            logger.info(f"Direct dispatch: split-screen {app1} + {app2}")
            return execute_split_screen(app1, app2, self.action_registry)

        # Parallel: "open chrome, notepad, and spotify"
        parallel = detect_parallel_tasks(user_input)
        if len(parallel) >= 2:
            logger.info(f"Direct dispatch: parallel {parallel}")
            tasks = []
            for task_text in parallel:
                tool_match = match_direct_tool(task_text)
                if tool_match:
                    tasks.append(tool_match)
            if len(tasks) >= 2:
                from execution_strategies import execute_parallel_tools
                results = execute_parallel_tools(tasks, self.action_registry)
                if all(t.get("tool") == "open_app" for t in tasks):
                    try:
                        import time as _t
                        _t.sleep(1)
                        from automation.window_manager import arrange_windows
                        names = [t["args"]["name"] for t in tasks]
                        arrange_windows(names, "side-by-side" if len(names) == 2 else "grid")
                    except Exception:
                        pass
                summaries = [f"{r[0]}({r[1].get('name', r[1].get('query', ''))}) -> OK"
                             for r in results if "error" not in r[2].lower()]
                return f"Done - {', '.join(summaries)}" if summaries else "Some tasks failed."

        # Clipboard URL auto-read: "read this link" / "summarize this page"
        import re as _re
        if _re.search(r'\b(read|summarize|check|open|what\'?s)\s+(this|that|the)\s+(link|url|page|site|website)\b', user_input, _re.I):
            try:
                import pyperclip
                clip = pyperclip.paste()
                if clip:
                    urls = _re.findall(r'https?://[^\s<>"\']+', clip)
                    if urls:
                        url = urls[0]
                        logger.info(f"Direct dispatch: auto-read clipboard URL {url}")
                        from web_agent import web_read
                        content = web_read(url)
                        if content:
                            if "summarize" in user_input.lower():
                                return f"Here's a summary of {url}:\n{content[:800]}"
                            return f"Content from {url}:\n{content[:1500]}"
            except Exception:
                pass

        # --- Reminder fast-path: bypass strategy selector for reliability ---
        # list_reminders and set_reminder are pure local ops — no LLM or strategy
        # overhead needed. Pattern-match here before handing off to selector.
        _ui_lower = user_input.lower().strip()
        if _re.search(r'\b(?:list|show|check|what(?:\'s|\s+are)?\s+(?:my\s+)?)reminders?\b', _ui_lower):
            logger.info("Direct dispatch: list_reminders (fast-path)")
            result = execute_tool("list_reminders", {}, self.action_registry,
                                  reminder_mgr=getattr(self, 'reminder_mgr', None))
            if result:
                return str(result)

        _reminder_set_match = _re.search(
            r'(?:remind\s+me\s+(?:to\s+)?(.+?)\s+(?:at|in|on)\s+(.+)'
            r'|set\s+(?:a\s+)?reminder\s+(?:for\s+)?(.+?)\s+(?:to|for)\s+(.+)'
            r'|set\s+(?:a\s+)?reminder\s+(?:to\s+)(.+?)(?:\s+(?:at|in|on)\s+(.+))?$)',
            _ui_lower,
        )
        if _reminder_set_match:
            g = _reminder_set_match.groups()
            # Groups differ by which branch matched; pick first non-None pair
            if g[0] is not None:   # remind me to X at/in Y
                msg, t = g[0].strip(), g[1].strip()
            elif g[2] is not None: # set a reminder for X to/for Y
                msg, t = g[3].strip(), g[2].strip()
            else:                  # set a reminder to X [at/in Y]
                msg = g[4].strip()
                t = (g[5] or "in 1 hour").strip()
            if msg and t:
                logger.info(f"Direct dispatch: set_reminder fast-path — '{msg}' at '{t}'")
                result = execute_tool(
                    "set_reminder", {"message": msg, "time": t},
                    self.action_registry,
                    reminder_mgr=getattr(self, 'reminder_mgr', None),
                )
                if result:
                    return str(result)

        # --- Screenshot fast-path: bypass strategy selector ---
        if _re.search(r'\b(take|capture|grab|save)\s+(a\s+)?screenshot\b', _ui_lower):
            logger.info("Direct dispatch: take_screenshot (fast-path)")
            result = execute_tool("take_screenshot", {}, self.action_registry)
            if result:
                return str(result)

        # --- Google search fast-path: "search for X on google" / "google X" ---
        _search_match = _re.search(
            r'^(?:search|google)\s+(?:for\s+)?(.+?)(?:\s+on\s+google)?$', _ui_lower)
        if _search_match and self.action_registry and "google_search" in self.action_registry:
            query = _search_match.group(1).strip()
            # Don't match compound intents like "search and play"
            if query and not _re.search(r'\band\s+(?:play|open|show|do|then)\b', query, _re.I):
                # Don't match if target is youtube/spotify (those need agent mode)
                if not _re.search(r'\bon\s+(?:youtube|spotify)\b', query, _re.I):
                    logger.info(f"Direct dispatch: google_search fast-path ({query})")
                    result = self.action_registry["google_search"](query)
                    if result:
                        return str(result)

        # --- Time/date fast-path: "what day is it", "what's the date" ---
        if _re.search(r'\b(?:what(?:\'?s|\s+is)?\s+(?:the\s+)?(?:day|date)\b|what\s+day\s+is\s+it)', _ui_lower):
            logger.info("Direct dispatch: get_time (day/date fast-path)")
            result = execute_tool("get_time", {}, self.action_registry)
            if result:
                return str(result)

        # --- StrategySelector: tries CLI → API → WEBSITE → TOOL → UIA → CDP ---
        selector = get_selector()
        execute_tool._last_user_input = user_input
        execute_tool._brain_quick_chat = self.quick_chat
        _tool_executor._quick_chat_fn = self.quick_chat

        # Gather system context for smart routing
        try:
            from execution_strategies import gather_context
            ctx = gather_context()
            # Inject recent actions from brain state
            with _state_lock:
                ctx["recent_actions"] = list(_recent_actions)
        except Exception:
            ctx = None

        result, strategy = selector.execute_step(
            user_input, context=ctx,
            action_registry=self.action_registry, skip_vision=True
        )

        if result and strategy:
            logger.info(f"Direct dispatch via {strategy}: {user_input[:60]}")

            # Naturalize CLI output into spoken language
            if strategy == "cli":
                return self._naturalize_cli_output(user_input, result)

            # Record for "do that again"
            with _state_lock:
                _recent_actions.append((strategy, {}, str(result)[:200]))
                if len(_recent_actions) > 5:
                    del _recent_actions[0]

            # Auto-escalate partial interactive results to agent mode
            _PARTIAL_INDICATORS = [
                "not confirmed", "couldn't auto-play", "click a result",
                "try clicking", "but couldn't", "but playback",
            ]
            result_lower = str(result).lower()
            if any(ind in result_lower for ind in _PARTIAL_INDICATORS):
                logger.info(f"Direct dispatch partial ({strategy}) — escalating to agent")
                try:
                    agent_result = self._run_agent_mode(user_input)
                    if agent_result and "error" not in str(agent_result).lower():
                        return agent_result
                except Exception as e:
                    logger.warning(f"Agent escalation failed: {e}")

            return str(result)

        # Pure knowledge/chat questions: skip tool calling, use quick_chat()
        # Exclude only system-action words, not knowledge about those topics.
        # "what is RAM" = knowledge (quick_chat), "how much RAM" = system (tool)
        _no_tool_needed = (
            _RE_KNOWLEDGE_START.search(user_input)
            and not _RE_ACTION_WORDS.search(user_input)
            and not _RE_TIME_DATE.search(user_input)
            # Allow knowledge about tech topics (RAM, CPU, etc.) — only block system queries
            and not _RE_SYSTEM_QUERY.search(user_input)
        )
        if _no_tool_needed and len(user_input.split()) <= 20:
            logger.info(f"Direct dispatch: quick_chat (pure knowledge question)")
            try:
                answer = self.quick_chat(user_input)
                if answer and len(answer) > 5:
                    return answer
            except Exception:
                pass

        return None

    def _naturalize_cli_output(self, question, raw_output):
        """Turn raw PowerShell output into natural spoken language.

        Uses quick_chat to convert technical output into a conversational
        answer that's easy to understand when spoken aloud.
        """
        if not raw_output or "Error" in raw_output:
            return raw_output

        # Short/already natural outputs don't need LLM
        if len(raw_output) < 60 and not any(c in raw_output for c in ['|', '\t', '{']):
            return raw_output

        # Already-natural sentence output (CLI patterns often return ready-to-speak text)
        # Detect: starts with word, ends with period, contains human words, no table chars
        _clean = raw_output.strip()
        if (_clean and _clean[0].isupper() and _clean.endswith('.')
                and not any(c in _clean for c in ['|', '\t', '{', '}', '\\\\'])
                and any(w in _clean.lower() for w in ['is ', 'are ', 'has ', 'have ', 'your ', 'you '])):
            return _clean

        try:
            natural = self.quick_chat(
                f"The user asked: \"{question}\"\n"
                f"System returned this raw data:\n{raw_output}\n\n"
                "Turn this into a short, natural spoken answer (1-3 sentences). "
                "IMPORTANT: Include the actual numbers and names from the data. "
                "Use human-friendly units (GB not bytes, minutes not seconds). "
                "Be direct — just answer with the specific data shown above."
            )
            if natural and len(natural) > 10:
                return natural
        except Exception:
            pass
        return raw_output

    def _answer_check(self, user_input, tool_name, arguments, result):
        """Validate tool execution result (EasyTool answer_check pattern).

        If the result doesn't match what the user asked for, blacklist
        the tool and return False to trigger retry with a different tool.

        Returns:
            True if the result looks correct, False if it should retry
        """
        if not result:
            return False

        result_str = str(result).lower()

        # Clear failure indicators
        if any(w in result_str for w in [
            "error", "failed", "not found", "blocked", "timed out",
            "permission denied", "could not", "couldn't", "invalid",
        ]):
            # Blacklist this tool for the current request
            self._tool_blacklist.add(tool_name)
            logger.info(f"answer_check: blacklisting {tool_name} "
                        f"(failed, blacklist={self._tool_blacklist})")
            return False

        # Check for LLM refusal in response
        if _is_llm_refusal_fn(result_str):
            return False

        return True

    def _save_as_skill(self, user_input, tool_calls_history):
        """Save a successful tool sequence as a skill (Voyager pattern).

        Called after a successful multi-tool think() execution.
        """
        if not tool_calls_history or len(tool_calls_history) < 2:
            return

        self._ensure_skill_lib()
        if not self._skill_lib:
            return

        try:
            name = self._skill_lib.generate_skill_name(user_input, llm_fn=self.quick_chat)

            tool_sequence = []
            for tc in tool_calls_history:
                tool_sequence.append({
                    "tool": tc.get("tool", ""),
                    "args": tc.get("args", {}),
                    "description": tc.get("description", ""),
                    "result": str(tc.get("result", ""))[:100],
                })

            self._skill_lib.save_skill(
                name=name,
                description=f"Learned from: {user_input[:100]}",
                goal=user_input,
                tool_sequence=tool_sequence,
            )
            logger.info(f"Saved skill from brain: {name} ({len(tool_sequence)} steps)")
        except Exception as e:
            logger.debug(f"Failed to save brain skill: {e}")

    def _get_pref_dict(self):
        """Get preferences dict for system prompt, or None."""
        if self.user_preferences is None:
            return None
        try:
            return self.user_preferences.get_all_preferences()
        except Exception:
            return None

    @property
    def messages(self):
        return self._ctx.messages

    @messages.setter
    def messages(self, value):
        self._ctx.messages = value

    @property
    def max_context(self):
        return self._ctx.max_context

    @max_context.setter
    def max_context(self, value):
        self._ctx.max_context = value

    def reset_context(self):
        """Clear conversation history but keep topic and last action summary."""
        last_action_summary = ""
        if self._recent_actions:
            last_tool, last_args, last_result = self._recent_actions[-1]
            last_action_summary = f"[Last action: {last_tool}({json.dumps(last_args)[:100]}) -> {last_result[:100]}]"
        self._ctx.reset(last_action_summary=last_action_summary)
        self._native_tools_failed = False
        self._prompt_mode_calls = 0

    # ------------------------------------------------------------------
    # Undo registry (Phase 5)
    # ------------------------------------------------------------------

    def _register_undo(self, tool_name, args, rollback_fn, description):
        """Register an undoable action."""
        self._undo_stack.append({
            "time": time.time(),
            "tool": tool_name,
            "args": args,
            "rollback_fn": rollback_fn,
            "description": description,
        })
        if len(self._undo_stack) > 10:
            del self._undo_stack[:len(self._undo_stack) - 10]

    def undo_last_action(self):
        """Undo the most recent reversible action within the time window."""
        now = time.time()
        rollback_fn = None
        desc = None
        with _state_lock:
            while _undo_stack:
                entry = _undo_stack[-1]
                if now - entry["time"] > self._UNDO_WINDOW:
                    _undo_stack.pop()
                    continue
                _undo_stack.pop()
                rollback_fn = entry["rollback_fn"]
                desc = entry["description"]
                break
        if rollback_fn:
            try:
                result = rollback_fn()
                logger.info(f"Undo: {desc} → {result}")
                return f"Undone: {desc}"
            except Exception as e:
                logger.error(f"Undo failed: {e}")
                return f"Couldn't undo: {e}"
        return None

    # ------------------------------------------------------------------
    # Recent actions buffer (Phase 6): "do that again"
    # ------------------------------------------------------------------

    def _record_action(self, tool_name, args, result):
        """Record a completed action for replay."""
        self._recent_actions.append((tool_name, args, result))
        if len(self._recent_actions) > 5:
            del self._recent_actions[0]

    # ------------------------------------------------------------------
    # Topic tracking (Phase 7)
    # ------------------------------------------------------------------

    def _extract_topic(self, text):
        """Extract topic from user text using keyword matching."""
        return self._ctx.extract_topic(text)

    def _update_topic(self, user_input):
        """Update topic tracking and adjust context window size."""
        self._ctx.update_topic(user_input)
        # Cognitive Phase 6: periodic self-analysis (truly non-blocking, hourly)
        if self._cognition:
            try:
                import threading
                def _bg_analysis():
                    try:
                        insights = self._cognition.run_self_analysis()
                        if insights:
                            logger.info(f"Cognitive self-analysis: {len(insights)} insights")
                            for i in insights:
                                logger.info(f"  [{i['type']}] {i['detail']}: {i['suggestion']}")
                    except Exception:
                        pass
                t = threading.Thread(target=_bg_analysis, daemon=True)
                t.start()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Context awareness (Phase 6): ambient context injection
    # ------------------------------------------------------------------

    def _get_ambient_context(self, user_input):
        """Build ambient context string for system prompt injection."""
        return self._ctx.get_ambient_context(user_input)

    @property
    def key_is_dead(self):
        """True if the API key is permanently unusable."""
        return self._key_dead

    def think(self, user_input, detected_language=None):
        """
        Process user input through the LLM brain.

        The LLM decides whether to:
        - Respond with text (simple conversation)
        - Call tools (system actions)
        - Chain multiple tools for complex tasks

        Returns the final spoken response string, or None on failure.
        None means the caller should use keyword fallback.
        """
        from ai_providers import is_rate_limited, _record_rate_limit, _clear_rate_limit

        # Reset cancellation flag at start of each think() call
        self._cancelled = False
        # Allow execute_tool to check cancellation
        execute_tool._brain_ref = self

        # Reset tool blacklist for new request (EasyTool pattern)
        self._tool_blacklist = set()

        # Skip brain entirely for empty input
        if not user_input or not user_input.strip():
            return None

        # Skip brain entirely if key is known-dead
        if self._key_dead:
            return None

        # Check emergency stop
        try:
            from core.control_flags import is_emergency_stopped
            if is_emergency_stopped():
                return "All automation has been stopped."
        except ImportError:
            pass

        if is_rate_limited():
            return None  # Let caller fall back to keyword mode

        # --- DIRECT MODE (no LLM needed) ---
        # Pattern-match system queries (RAM, disk, CPU) for instant execution
        # This MUST come before skill library to avoid replaying broken stored skills
        direct_result = self._try_direct_dispatch(user_input)
        if direct_result:
            return direct_result

        # --- SKILL LIBRARY CHECK (Voyager pattern) ---
        # Before full LLM processing, check if we have a stored skill
        skill_match = self._check_skill_library(user_input)
        if skill_match and skill_match.get("similarity", 0) >= 0.70:
            _skill_sim = skill_match.get("similarity", 0)
            _low_confidence = _skill_sim < 0.80
            if _low_confidence:
                logger.warning(f"Low-confidence skill match: {skill_match['name']} "
                               f"(similarity={_skill_sim:.2f}) — proceeding with caution")
            logger.info(f"Executing stored skill: {skill_match['name']}")
            try:
                # Replay the skill's tool sequence
                results = []
                for tc in skill_match["tool_sequence"]:
                    tool = tc.get("tool", "")
                    args = tc.get("args", {}) or {}
                    if not tool:
                        continue

                    # Pre-condition check: skip open_app if the app is already running
                    if tool == "open_app":
                        app_name = (
                            args.get("name") or args.get("app") or ""
                        ).strip()
                        if app_name:
                            try:
                                import subprocess
                                proc = subprocess.run(
                                    ["tasklist", "/FI",
                                     f"IMAGENAME eq {app_name}.exe",
                                     "/NH"],
                                    capture_output=True, text=True, timeout=3
                                )
                                if app_name.lower() in proc.stdout.lower():
                                    logger.debug(
                                        f"Skill replay: skipping open_app({app_name!r})"
                                        f" — already running"
                                    )
                                    results.append(f"{app_name} already running")
                                    continue
                            except Exception as _te:
                                logger.debug(f"tasklist check failed: {_te}")
                                # proceed normally — better to open than to skip wrongly

                    r = execute_tool(tool, args, self.action_registry,
                                     self.reminder_mgr, self.speak_fn)
                    results.append(str(r)[:100])
                if results:
                    self._skill_lib.record_use(skill_match["name"], success=True)
                    _prefix = "(low-confidence skill) " if _low_confidence else ""
                    return f"{_prefix}Done — {results[-1]}" if results else "Skill executed."
            except Exception as e:
                logger.warning(f"Skill execution failed: {e}")
                if self._skill_lib:
                    self._skill_lib.record_use(skill_match["name"], success=False)
                # Fall through to normal processing

        # EARLY MODE CLASSIFICATION — skip expensive context setup for agent/research
        decision = self._classify_mode(user_input)
        _early_mode = decision.mode

        if _early_mode == "agent":
            logger.info("Mode: AGENT — routing to autonomous agent (skipping context setup)")
            if self.speak_fn:
                try:
                    self.speak_fn("Working on it...")
                except Exception:
                    pass
            return self._run_agent_mode(user_input)

        if _early_mode == "research":
            logger.info("Mode: RESEARCH — multi-source web research (skipping context setup)")
            if self.speak_fn:
                try:
                    self.speak_fn("Let me research that for you...")
                except Exception:
                    pass
            return self._run_research(user_input)

        # QUICK MODE — full context setup needed
        # Lazy-load cognitive engine on first think() call
        self._ensure_cognition()

        # Auto-reset context if idle >120s (prevents cross-session degradation)
        if self._ctx.check_idle_reset(idle_threshold=120):
            self.reset_context()

        # Topic tracking: adjust context window based on conversation topic
        self._update_topic(user_input)

        # Cognitive Phase 2: resolve pronouns ("open it" → "open Chrome")
        if self._cognition:
            try:
                resolved = self._cognition.resolve_input(user_input)
                if resolved != user_input:
                    logger.info(f"Cognitive resolved: '{user_input}' → '{resolved}'")
                    user_input = resolved
            except Exception:
                pass

        # Detect one-shot language override: "say X in Hindi", "greet in Nepali"
        # User is speaking ENGLISH but wants OUTPUT in another language
        _lang_match = _LANG_PATTERN.search(user_input)
        if _lang_match:
            target_lang = _lang_match.group(1).lower()
            lang_code = _LANG_CODES.get(target_lang)
            if lang_code:
                try:
                    from speech import set_next_speak_language
                    set_next_speak_language(lang_code)
                    logger.info(f"One-shot language override: next response in {target_lang} ({lang_code})")
                except ImportError:
                    pass

        # Update system prompt with current detected language + ambient context
        lang = detected_language or "en"
        if self._use_native_tools and not self._native_tools_failed:
            self.system_prompt = _build_brain_system_prompt(
                self.username, self.ainame, detected_language=lang,
                user_preferences=self._get_pref_dict())
        else:
            self.system_prompt = _build_prompt_system(
                self.username, self.ainame, detected_language=lang)

        # Inject ambient context (active window, clipboard, time)
        ambient = self._get_ambient_context(user_input)
        if ambient:
            self.system_prompt += f"\n\nCONTEXT: {ambient}"

        # Inject cognitive context (learning, comprehension, autonomy)
        if self._cognition:
            try:
                cog_ctx = self._cognition.get_context(user_input)
                if cog_ctx:
                    self.system_prompt += f"\n\nCOGNITIVE:\n{cog_ctx}"
            except Exception:
                pass

        # Handle "do that again" / "same thing" — replay last SUCCESSFUL action
        if re.search(r'\b(do that again|same thing|repeat that action|again)\b', user_input, re.I):
            replay_tool, replay_args = None, None
            with _state_lock:
                if _recent_actions:
                    # Find last successful action (skip errors)
                    for tool, args, prev_result in reversed(_recent_actions):
                        if not any(w in prev_result.lower() for w in ["error", "not found", "failed", "blocked", "cancelled"]):
                            replay_tool, replay_args = tool, args
                            break
                    if not replay_tool:
                        replay_tool, replay_args, _ = _recent_actions[-1]
            if replay_tool:
                logger.info(f"Replay: {replay_tool}({replay_args})")
                execute_tool(replay_tool, replay_args, self.action_registry, self.reminder_mgr)
                return f"Done — repeated {replay_tool}."

        # "What have you learned" — report cognitive state
        if self._cognition and re.search(r'\b(what have you learned|what did you learn|how smart are you|your experience|learning (summary|stats|report)|cognitive (report|status))\b', user_input, re.I):
            return self._cognition.get_report()

        # Cognitive decomposition disabled — mode_classifier's smart decomposition
        # handles compound requests via regex (0ms) instead of LLM (6.5s).
        # See: llm/mode_classifier.py SMART DECOMPOSITION section.

        self.messages.append({"role": "user", "content": user_input})
        self._trim_context()

        # Store user input and quick_chat reference for execute_tool
        execute_tool._last_user_input = user_input
        execute_tool._brain_quick_chat = self.quick_chat
        _tool_executor._quick_chat_fn = self.quick_chat

        # Initialize trace for this call
        import time as _time
        _trace_start = _time.time()
        self.last_call_trace = {
            "user_input": user_input,
            "tool_calls": [],
            "tool_results": [],
            "response": None,
            "errors": [],
            "mode": "native" if (self._use_native_tools and not self._native_tools_failed) else "prompt",
            "elapsed": 0.0,
        }

        try:
            # Mode already classified and agent/research already handled above.
            # Only QUICK mode reaches here.
            mode = "quick"
            self.last_call_trace["mode"] = mode

            # QUICK MODE: normal LLM tool calling
            # Periodically retry native mode after transient failure
            if self._native_tools_failed and self._use_native_tools:
                self._prompt_mode_calls += 1
                if self._prompt_mode_calls >= self._PROMPT_MODE_RETRY:
                    logger.info("Retrying native tool mode after %d prompt-mode calls",
                                self._prompt_mode_calls)
                    self._native_tools_failed = False
                    self._prompt_mode_calls = 0

            if self._use_native_tools and not self._native_tools_failed:
                logger.info(f"Context: {len(self.messages)} msgs, max={self.max_context}, "
                            f"tools={'native' if not self._native_tools_failed else 'prompt'}")
                result = self._think_native()
            else:
                result = self._think_prompt_based()

            # Smart escalation: escalate to agent mode when quick mode can't
            # fully complete a task. Two cases:
            # 1. UI-related errors needing screen interaction
            # 2. Partial completion (action started but not confirmed/finished)
            result_lower = str(result).lower() if result else ""

            # Case 1: Partial completion — action started but needs follow-up
            _PARTIAL_INDICATORS = [
                "not confirmed", "couldn't auto-play", "click a result",
                "try clicking", "but couldn't", "but playback",
                "couldn't auto-play", "searched for", "but couldn't",
            ]
            _is_partial = any(ind in result_lower for ind in _PARTIAL_INDICATORS)

            # Case 2: UI-related failure
            _is_ui_error = (
                any(w in result_lower for w in ["error", "failed", "couldn't"])
                and not any(w in result_lower for w in [
                    "cancelled", "blocked for safety", "not found",
                    "not installed", "no such", "timed out", "timeout",
                    "no output", "permission denied",
                ])
                and any(w in result_lower for w in [
                    "click", "button", "element", "screen", "window",
                    "navigate", "page", "browser", "ui",
                ])
            )

            if result and mode == "quick" and (_is_partial or _is_ui_error):
                reason = "partial completion" if _is_partial else "UI failure"
                logger.info(f"Quick mode {reason} — escalating to agent mode")
                try:
                    agent_result = self._run_agent_mode(user_input)
                    if agent_result and "error" not in str(agent_result).lower():
                        result = agent_result
                except Exception as e:
                    logger.warning(f"Agent escalation failed: {e}")

            if result:
                _clear_rate_limit()
            self.last_call_trace["response"] = str(result)[:500] if result else None
            self.last_call_trace["elapsed"] = round(_time.time() - _trace_start, 2)
            self._write_trace()

            # Save successful multi-tool sequences as skills (Voyager pattern)
            if result and self.last_call_trace.get("tool_calls"):
                tool_calls_for_skill = self.last_call_trace["tool_calls"]
                # Only save if multiple tools were used and no errors in result
                if (len(tool_calls_for_skill) >= 2
                        and not any(w in str(result).lower()
                                    for w in ["error", "failed", "not found"])):
                    try:
                        self._save_as_skill(user_input, tool_calls_for_skill)
                    except Exception as e:
                        logger.debug(f"Skill save failed: {e}")

            # Post-think cleanup: collapse tool messages from this request
            # into a clean user→assistant pair. Prevents context pollution
            # from multi-round tool chains (play_music→click_at→etc.)
            self._collapse_completed_turn(result)
            return result

        except requests.HTTPError as e:
            self._pop_user_message()
            if e.response is not None:
                status = e.response.status_code
                body = ""
                try:
                    body = e.response.text
                except Exception:
                    pass

                if status == 429 and "insufficient_quota" in body:
                    logger.error("Brain: API key has no credits (insufficient_quota)")
                    self._key_dead = True  # Stop trying this key
                    return None  # Fall through to keyword mode
                elif status == 429:
                    _record_rate_limit()
                elif status == 401:
                    logger.error("Brain: Invalid API key (401)")
                    self._key_dead = True
                    return None

            logger.warning(f"Brain API error: {e}")
            return None

        except requests.ConnectionError:
            self._pop_user_message()
            if self.provider_name == "ollama":
                logger.error("Brain: Cannot connect to Ollama. Is it running?")
                return "I can't reach Ollama. Make sure it's running with 'ollama serve'."
            logger.warning("Brain connection error")
            return None

        except requests.Timeout:
            # Keep user message in context so next attempt has history
            # Only pop if we want stateless retry (we don't — context matters)
            if self.provider_name == "ollama":
                logger.warning("Brain: Ollama response timed out (streaming should prevent this)")
                return ("That took too long. The model might still be loading. "
                        "Try again in a moment.")
            self._pop_user_message()
            logger.warning("Brain API timed out")
            return None

        except Exception as e:
            self._pop_user_message()
            logger.error(f"Brain error: {e}", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Mode-based routing: classify → route to quick/agent/research
    # ------------------------------------------------------------------

    # Mode classification: patterns and logic in llm/mode_classifier.py

    def _classify_mode(self, user_input):
        """Delegate to llm.mode_classifier.classify_mode()."""
        return _classify_mode_fn(user_input, quick_chat_fn=self.quick_chat)

    def _run_agent_mode(self, user_input):
        """Run autonomous agent. Tries CLI/API strategies first, then desktop agent.

        For complex multi-step tasks, uses the SwarmOrchestrator (multi-agent team).
        For simpler agent tasks, uses the existing desktop agent path.
        """
        # Skip strategy shortcut for UI-interactive tasks — these need full agent
        _ui_interactive = re.search(
            r'\b(spotify|youtube)\b.*(play|search|find|watch|listen)|'
            r'(play|search|find|watch|listen).*(spotify|youtube)\b|'
            r'\b(order|book|buy|purchase)\b.*(online|pizza|food|ticket)',
            user_input, re.I)

        # Detect complex multi-step tasks that benefit from multi-agent swarm
        _complex_task = re.search(
            r'\b(plan|book|order|create|research|build|organize|schedule|prepare)\b.+'
            r'\b(and|then|also|plus|after|with)\b.+'
            r'\b(send|post|upload|create|save|book|set|open|email|share)\b',
            user_input, re.I)

        # Track which strategies were tried (pass to agent to avoid retrying)
        _tried_strategies = set()

        # Try fast strategies before expensive agent mode (non-UI tasks only)
        if not _ui_interactive:
            try:
                from execution_strategies import get_selector
                selector = get_selector()
                result, strategy = selector.execute_step(
                    user_input, action_registry=self.action_registry, skip_vision=True)
                if result and strategy:
                    logger.info(f"Agent mode shortcut: {strategy} handled '{user_input[:40]}'")
                    return result
                # Record which strategies were attempted but didn't fully succeed
                if hasattr(selector, '_last_tried_strategies'):
                    _tried_strategies = set(selector._last_tried_strategies)
            except Exception as e:
                logger.debug(f"Strategy pre-check failed: {e}")
        else:
            logger.info(f"Agent mode: skipping strategy shortcut for UI task '{user_input[:40]}'")

        # --- Multi-Agent Swarm for complex tasks ---
        if _complex_task or _ui_interactive:
            try:
                from agents.orchestrator import SwarmOrchestrator
                swarm = SwarmOrchestrator(self, speak_fn=self.speak_fn)
                result = swarm.execute(user_input)
                if result and "error" not in str(result).lower()[:30]:
                    logger.info(f"Swarm completed: {user_input[:40]}")
                    return result
                # Swarm failed — fall through to legacy agent
                logger.info(f"Swarm partial/failed, falling back to legacy agent")
            except Exception as e:
                logger.warning(f"Swarm orchestrator failed: {e}, falling back to legacy agent")

        from orchestration.agent_runner import run_agent_mode
        return run_agent_mode(
            user_input, self.action_registry, self.reminder_mgr,
            self.speak_fn, messages=self.messages,
            skip_strategies=_tried_strategies,
        )

    def _run_research(self, user_input):
        """Research mode: multi-step web research with citations + LLM synthesis.

        Uses deep_research() for multi-query search, link following, and source
        scoring. Same 2 LLM calls as before (query gen + synthesis) but gathers
        3-4x more web data with citation tracking.
        """
        try:
            from web_agent import deep_research
        except ImportError:
            return "Research mode unavailable — web_agent module not found."

        # Run deep research with LLM-powered query generation
        research = deep_research(
            query=user_input,
            llm_fn=self.quick_chat,
            max_sources=6,
            max_follow_links=3,
        )

        report = research.get("report", "")
        sources = research.get("sources", [])

        if not report:
            return "I couldn't find relevant information online. Try rephrasing your question."

        # Build source attribution string
        source_refs = ""
        if sources:
            refs = []
            for src in sources[:5]:
                idx = src.get("index", 0)
                title = src.get("title", "")[:40]
                url = src.get("url", "")
                if title:
                    refs.append(f"[{idx}] {title}")
            if refs:
                source_refs = "\n\nSources: " + ", ".join(refs)

        # Synthesize research into a natural spoken answer
        synthesis = self.quick_chat(
            f"Based on this web research, give a comprehensive spoken answer to: '{user_input}'\n\n"
            f"Research results:\n{report[:3000]}\n\n"
            f"Rules: Speak naturally as if explaining to someone. Reference sources by number "
            f"(e.g. 'According to source 1...'). Be thorough but concise. No markdown formatting."
        )

        if synthesis:
            self.messages.append({"role": "assistant", "content": synthesis})
            return synthesis

        # Fallback: return raw report summary
        return report[:500]

    def _write_trace(self):
        """Write last_call_trace to brain_trace.json for external consumers."""
        if not self.last_call_trace:
            return
        try:
            import json as _json
            with open(self._trace_file, "w", encoding="utf-8") as f:
                _json.dump(self.last_call_trace, f, indent=2, default=str)
        except Exception:
            pass  # Non-critical — don't break Brain on trace write failure

    def _record_trace_tool(self, tool_name, tool_args, result=None):
        """Record a tool call in the current trace."""
        if not self.last_call_trace:
            return
        self.last_call_trace["tool_calls"].append({
            "tool": tool_name, "args": tool_args,
        })
        if result is not None:
            self.last_call_trace["tool_results"].append(
                str(result)[:300]
            )

    @staticmethod
    def _sanitize_response(text):
        """Remove LLM artifacts (special tokens, leftover JSON) from spoken text."""
        return _sanitize_response_fn(text)

    def _pop_user_message(self):
        """Remove the last user message (on error, before it gets into context)."""
        self._ctx.pop_last_user_message()

    # ------------------------------------------------------------------
    # Mode 1: Native tool calling (OpenAI / Anthropic / Ollama with tools)
    # ------------------------------------------------------------------

    def _think_native(self):
        """Process with native function/tool calling. Delegates to llm.tool_caller."""
        from llm.tool_caller import think_native
        return think_native(self)

    # ------------------------------------------------------------------
    # Mode 2: Prompt-based tool calling (LLM outputs JSON actions)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_llm_refusal(text):
        """Detect if LLM output is a refusal to use tools."""
        return _is_llm_refusal_fn(text)

    @staticmethod
    def _suggest_tool_for_retry(user_msg):
        """Suggest the right tool based on keywords in the user's request."""
        return _suggest_tool_for_retry_fn(user_msg)

    def _think_prompt_based(self):
        """Process with prompt-based tool calling. Delegates to llm.tool_caller."""
        from llm.tool_caller import think_prompt_based
        return think_prompt_based(self)

    # ------------------------------------------------------------------
    # LLM API calls
    # ------------------------------------------------------------------

    def _call_llm_native(self):
        """Make API call WITH tool definitions. Returns choice dict or None."""
        if self.provider_name in ("openai", "openrouter", "ollama"):
            return self._call_openai_style(with_tools=True)
        elif self.provider_name == "anthropic":
            return self._call_anthropic_style()
        return None

    def _call_llm_simple(self):
        """Make API call WITHOUT tool definitions (prompt-based mode).
        Returns {"content": "..."} dict or None."""
        if self.provider_name in ("openai", "openrouter", "ollama"):
            result = self._call_openai_style(with_tools=False)
            if result:
                return result.get("message", {})
        elif self.provider_name == "anthropic":
            result = self._call_anthropic_style(with_tools=False)
            if result:
                return result.get("message", {})
        return None

    def _call_openai_style(self, with_tools=True):
        """Call OpenAI, OpenRouter, or Ollama."""
        urls = {
            "openai": "https://api.openai.com/v1/chat/completions",
            "openrouter": "https://openrouter.ai/api/v1/chat/completions",
            "ollama": f"{self.ollama_url}/api/chat",
        }
        models = {
            "openai": "gpt-4o-mini",
            "openrouter": "gpt-4o-mini",
            "ollama": self.ollama_model,
        }

        url = urls[self.provider_name]
        model = models[self.provider_name]

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                *self._get_clean_messages(),
            ],
            "temperature": 0.7,
        }

        # Ollama speed optimizations
        if self.provider_name == "ollama":
            payload["stream"] = True  # Stream tokens for no timeout issues
            # Scale context window based on model size
            _model_lower = (self.ollama_model or "").lower()
            if any(s in _model_lower for s in ("72b", "70b")):
                _num_ctx = 32768   # 32K for 70B+ models
                _num_predict = 1024
            elif any(s in _model_lower for s in ("32b", "27b")):
                _num_ctx = 24576   # 24K for 27-32B models
                _num_predict = 768
            elif any(s in _model_lower for s in ("14b", "13b")):
                _num_ctx = 20480   # 20K for 13-14B models
                _num_predict = 640
            else:
                _num_ctx = 16384   # 16K for 7B and smaller
                _num_predict = 512
            payload["options"] = {
                "num_predict": _num_predict,
                "num_ctx": _num_ctx,
                "temperature": 0.3,  # Lower = more deterministic tool selection, fewer hallucinations
            }

        # Only add tools for native mode
        if with_tools:
            payload["tools"] = self.tools
            # Don't send tool_choice for Ollama — not always supported
            if self.provider_name != "ollama":
                payload["tool_choice"] = "auto"

        headers = {"Content-Type": "application/json"}
        if self.provider_name != "ollama":
            headers["Authorization"] = f"Bearer {self.api_key}"

        # Ollama timeout: scale by model size (larger models need more time)
        # First call includes model loading (~120s), warm calls scale by model
        if self.provider_name == "ollama":
            _model_lower = (self.ollama_model or "").lower()
            if any(s in _model_lower for s in ("72b", "70b")):
                _warm_timeout = 300  # 70B+ models: 5 min
            elif any(s in _model_lower for s in ("32b", "27b")):
                _warm_timeout = 200  # 32B models: 3.3 min
            elif any(s in _model_lower for s in ("14b", "13b")):
                _warm_timeout = 120  # 14B models: 2 min
            else:
                _warm_timeout = 90   # 7B and smaller: 1.5 min
            timeout = (5, 240) if not hasattr(self, '_ollama_warmed') else (5, _warm_timeout)
            self._ollama_warmed = True
        else:
            timeout = 15

        try:
            if self.provider_name == "ollama":
                # Streaming: read chunks with per-chunk timeout (no total timeout limit)
                # Connect timeout = 5s, first-chunk timeout scales by model size
                response = requests.post(url, headers=headers, json=payload,
                                        timeout=timeout, stream=True)
                response.raise_for_status()

                # Accumulate streamed response
                content_parts = []
                tool_calls = []
                last_data = None
                for line in response.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    last_data = chunk
                    msg = chunk.get("message", {})
                    if msg.get("content"):
                        content_parts.append(msg["content"])
                    if msg.get("tool_calls"):
                        tool_calls.extend(msg["tool_calls"])

                response.close()

                # Build final response in Ollama's expected format
                if last_data:
                    final_msg = last_data.get("message", {})
                    final_msg["content"] = "".join(content_parts)
                    if tool_calls:
                        final_msg["tool_calls"] = tool_calls
                    last_data["message"] = final_msg
                    return last_data
                return None
            else:
                response = requests.post(url, headers=headers, json=payload, timeout=timeout)
                response.raise_for_status()
                data = response.json()
                return data["choices"][0]
        except requests.HTTPError:
            raise  # Let the caller handle HTTP errors
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            logger.error(f"Malformed API response: {e}")
            return None

    def _call_anthropic_style(self, with_tools=True):
        """
        Call Anthropic with tool use.
        Converts Anthropic's format to match OpenAI's for uniform handling.
        """
        # Convert messages
        anthropic_messages = []
        i = 0
        msgs = self._get_clean_messages()
        while i < len(msgs):
            msg = msgs[i]
            role = msg.get("role")

            if role == "user":
                anthropic_messages.append({"role": "user", "content": msg["content"]})
            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    content_blocks = []
                    if msg.get("content"):
                        content_blocks.append({"type": "text", "text": msg["content"]})
                    for tc in tool_calls:
                        try:
                            raw_args = tc["function"]["arguments"]
                            # Ollama native returns dict; OpenAI returns JSON string
                            if isinstance(raw_args, dict):
                                input_data = raw_args
                            elif isinstance(raw_args, str):
                                input_data = json.loads(raw_args)
                            else:
                                input_data = {}
                        except (json.JSONDecodeError, KeyError):
                            input_data = {}
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["function"]["name"],
                            "input": input_data,
                        })
                    anthropic_messages.append({"role": "assistant", "content": content_blocks})
                else:
                    anthropic_messages.append({"role": "assistant", "content": msg.get("content", "")})
            elif role == "tool":
                tool_results = []
                while i < len(msgs) and msgs[i].get("role") == "tool":
                    tool_msg = msgs[i]
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_msg.get("tool_call_id", ""),
                        "content": tool_msg.get("content", ""),
                    })
                    i += 1
                anthropic_messages.append({"role": "user", "content": tool_results})
                continue

            i += 1

        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "temperature": 0.7,
            "system": self.system_prompt,
            "messages": anthropic_messages,
        }

        if with_tools:
            anthropic_tools = []
            for tool in self.tools:
                fn = tool["function"]
                anthropic_tools.append({
                    "name": fn["name"],
                    "description": fn["description"],
                    "input_schema": fn["parameters"],
                })
            payload["tools"] = anthropic_tools

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        # Convert to OpenAI format
        content_blocks = data.get("content", [])
        text_parts = []
        tool_calls = []

        for block in content_blocks:
            block_type = block.get("type", "")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", "unknown"),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })

        message = {
            "content": " ".join(text_parts) if text_parts else None,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        return {"message": message}

    # ------------------------------------------------------------------
    # Context management
    # ------------------------------------------------------------------

    def _collapse_completed_turn(self, final_response):
        """Collapse completed tool call/result messages into a clean summary."""
        self._ctx.collapse_completed_turn(final_response)

    def _get_clean_messages(self):
        """Get messages suitable for API calls, condensing old tool context."""
        return self._ctx.get_clean_messages(skip_tools=self._native_tools_failed)

    def _trim_context(self):
        """Trim context by dropping oldest messages when limit exceeded."""
        self._ctx.trim()

    # ------------------------------------------------------------------
    # Warm-up (pre-load model in Ollama for faster first response)
    # ------------------------------------------------------------------

    def warm_up(self):
        """
        Send a minimal request to pre-load the model into memory.
        Call this at startup for faster first response.
        """
        if self.provider_name != "ollama":
            return

        try:
            logger.info(f"Warming up Ollama model '{self.ollama_model}'...")
            # keep_alive=-1 keeps the model in VRAM permanently (no cold-start penalty)
            requests.post(
                f"{self.ollama_url}/api/generate",
                json={
                    "model": self.ollama_model,
                    "prompt": "",
                    "keep_alive": -1,
                },
                timeout=180,
            )
            self._ollama_warmed = True
            logger.info(f"Ollama {self.ollama_model} warm-up complete")
        except Exception as e:
            logger.warning(f"Ollama warm-up failed: {e}")

        # Vision model (llava) — warm up in separate thread since it's rarely needed
        def _warm_llava():
            try:
                requests.post(
                    f"{self.ollama_url}/api/generate",
                    json={"model": "llava", "prompt": "", "keep_alive": -1},
                    timeout=30,
                )
                logger.info("Ollama llava warm-up complete")
            except Exception:
                pass  # llava is optional

        import threading
        threading.Thread(target=_warm_llava, daemon=True).start()

    def quick_chat(self, prompt):
        """Quick single-turn LLM call for generating natural responses. No tools, no history."""
        try:
            from core.metrics import metrics
            _qc_timer = metrics.timer("llm_quick_chat")
            _qc_timer.__enter__()
        except Exception:
            _qc_timer = None
        try:
            _identity = (
                f"You are {self.ainame}, a personal AI created by Dawa Sangay Sherpa. "
                f"If asked about your creator/maker, ALWAYS say 'Dawa Sangay Sherpa created me'. "
                f"Never say Alibaba, Qwen, OpenAI, Anthropic, Meta, or any company."
            )
            _messages = [
                {"role": "system", "content": _identity},
                {"role": "user", "content": prompt},
            ]

            if self.provider_name == "ollama":
                # Use native Ollama endpoint (works on all versions)
                # Timeout scales by model size (32b needs ~40s even for short responses)
                _model_lower = (self.ollama_model or "").lower()
                if any(s in _model_lower for s in ("72b", "70b")):
                    _qc_timeout = 120
                elif any(s in _model_lower for s in ("32b", "27b")):
                    _qc_timeout = 90
                elif any(s in _model_lower for s in ("14b", "13b")):
                    _qc_timeout = 45
                else:
                    _qc_timeout = 20
                resp = requests.post(
                    f"{self.ollama_url}/api/chat",
                    json={
                        "model": self.ollama_model,
                        "messages": _messages,
                        "stream": False,
                        "options": {"num_predict": 100, "temperature": 0.8},
                    },
                    timeout=_qc_timeout,
                )
                resp.raise_for_status()
                content = resp.json()["message"]["content"]
                return self._sanitize_response(content)

            elif self.provider_name in ("openai", "openrouter"):
                urls = {
                    "openai": "https://api.openai.com/v1/chat/completions",
                    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
                }
                models = {
                    "openai": "gpt-4o-mini",
                    "openrouter": "gpt-4o-mini",
                }
                headers = {"Content-Type": "application/json"}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"

                resp = requests.post(
                    urls[self.provider_name],
                    headers=headers,
                    json={
                        "model": models[self.provider_name],
                        "messages": _messages,
                        "max_tokens": 100,
                        "temperature": 0.8,
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return self._sanitize_response(content)
            elif self.provider_name == "anthropic":
                resp = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "claude-sonnet-4-20250514",
                        "max_tokens": 100,
                        "system": _identity,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                content = resp.json()["content"][0]["text"]
                return self._sanitize_response(content)
        except Exception as e:
            logger.debug(f"quick_chat failed: {e}")
            return None
        finally:
            if _qc_timer:
                try:
                    _qc_timer.__exit__(None, None, None)
                except Exception:
                    pass

    def stream_response(self, prompt, speak_fn=None):
        """
        Stream LLM response and speak each sentence as it completes.
        Much faster perceived latency — first words spoken in ~1-2s.
        Returns the full response text.
        """
        if self.provider_name != "ollama":
            # Non-streaming fallback for non-Ollama providers
            result = self.quick_chat(prompt)
            if result and speak_fn:
                speak_fn(result)
            return result

        try:
            import threading
            response = requests.post(
                f"{self.ollama_url}/api/chat",
                json={
                    "model": self.ollama_model,
                    "messages": [
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": True,
                    "options": {"num_predict": 200, "temperature": 0.5},
                },
                stream=True,
                timeout=45,
            )

            buffer = ""
            full_response = ""
            import re
            sentence_end = re.compile(r'[.!?]\s')

            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                token = chunk.get("message", {}).get("content", "")
                if not token:
                    if chunk.get("done"):
                        break
                    continue

                buffer += token
                full_response += token

                # Check for complete sentence
                match = sentence_end.search(buffer)
                if match:
                    sentence = buffer[:match.end()].strip()
                    buffer = buffer[match.end():]
                    if speak_fn and sentence:
                        threading.Thread(target=speak_fn, args=(sentence,), daemon=True).start()

            # Speak remaining buffer
            remainder = buffer.strip()
            if remainder and speak_fn:
                speak_fn(remainder)

            return self._sanitize_response(full_response) if full_response else None

        except Exception as e:
            logger.debug(f"stream_response failed: {e}")
            # Fallback to non-streaming
            result = self.quick_chat(prompt)
            if result and speak_fn:
                speak_fn(result)
            return result

    # ------------------------------------------------------------------
    # Session persistence — export/import for continuity across restarts
    # ------------------------------------------------------------------

    def export_session(self):
        """Export conversation state for persistence.

        Returns a dict with messages, topic, and tool_blacklist that can be
        serialized to JSON and restored later via import_session().
        """
        return {
            "messages": self.messages[-20:] if self.messages else [],
            "topic": getattr(self._ctx, '_current_topic', None),
            "tool_blacklist": list(getattr(self, '_tool_blacklist', set())),
        }

    def import_session(self, state):
        """Restore conversation state from a saved session.

        Args:
            state: Dict previously returned by export_session().
        """
        if not state:
            return
        messages = state.get("messages", [])
        if messages:
            self.messages = messages
        topic = state.get("topic")
        if topic:
            self._ctx._current_topic = topic
        blacklist = state.get("tool_blacklist", [])
        if blacklist:
            self._tool_blacklist = set(blacklist)
