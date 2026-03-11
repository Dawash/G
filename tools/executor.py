"""
Tool executor — dispatches tool calls through the registry.

Routes registered tools through their ToolSpec handlers with
pre-execution (safety check, confirmation, caching) and
post-execution (undo, learning, verification, audit) hooks.

Non-registered tools fall through to the legacy _execute_tool_inner path.
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from tools.schemas import ToolSpec
from tools.registry import ToolRegistry
from tools.cache import ResponseCache
from tools.undo_manager import UndoManager
from tools.safety_policy import (
    get_safety_level, get_confirmation_status, needs_confirmation,
    supports_dry_run, dry_run, check_terminal_safety,
    validate_tool_choice, SAFE, MODERATE, SENSITIVE, CRITICAL,
)
from tools.audit_log import log_tool_execution

logger = logging.getLogger(__name__)

# Per-tool timeout overrides (seconds). Tools not listed default to 30s.
_HANDLER_TIMEOUTS = {
    "create_file": 120,
    "agent_task": 120,
    "web_read": 60,
    "web_search_answer": 60,
    "send_email": 60,
    "run_self_test": 120,
    "analyze_and_improve": 120,
    "reason_deeply": 120,
    "spawn_agents": 120,
    "chain_tasks": 120,
}

# Tools that must run on the calling thread (e.g. pyautogui desktop automation).
# These are called directly without a timeout wrapper.
_MAIN_THREAD_TOOLS = frozenset({
    "press_key",
    "click_at",
    "type_text",
    "scroll",
    "take_screenshot",
    "find_on_screen",
})

# Map legacy safety values to new levels
_SAFETY_MAP = {
    "safe": SAFE,
    "confirm": SENSITIVE,
    "dangerous": CRITICAL,
    "moderate": MODERATE,
    "sensitive": SENSITIVE,
    "critical": CRITICAL,
}


class ToolExecutor:
    """Executes tool calls through the registry with full lifecycle hooks."""

    def __init__(self, registry: ToolRegistry, cache: ResponseCache,
                 undo_mgr: UndoManager, orchestrator=None):
        self.registry = registry
        self.cache = cache
        self.undo_mgr = undo_mgr
        self._orchestrator = orchestrator  # StatefulOrchestrator (state-first execution)

    def execute(self, tool_name, arguments, action_registry,
                reminder_mgr=None, speak_fn=None,
                user_input="", cognition=None, experience_learner=None,
                log_action_fn=None, fallback_fn=None,
                mode="", dry_run_mode=False):
        """Execute a tool call with full lifecycle.

        Args:
            tool_name: Canonical tool name.
            arguments: Dict of tool arguments.
            action_registry: Legacy action_registry from assistant.py.
            reminder_mgr: Reminder manager instance (optional).
            speak_fn: TTS function for confirmations (optional).
            user_input: Original user text for context.
            cognition: CognitiveEngine for confidence-based switching.
            experience_learner: ExperienceLearner for outcome logging.
            log_action_fn: Callable(module, action, result) for audit log.
            fallback_fn: Legacy handler for non-registered tools.
            mode: Routing mode ("quick", "agent", etc.) for audit.
            dry_run_mode: If True, return preview without executing.

        Returns:
            str: Tool result.
        """
        start_time = time.time()
        spec = self.registry.get(tool_name)

        # Process isolation for risky tools
        if spec and getattr(spec, 'isolate', False):
            try:
                from tools.isolated_executor import IsolatedToolExecutor
                if not hasattr(self, '_isolated'):
                    self._isolated = IsolatedToolExecutor(max_workers=2, default_timeout=30)
                result = self._isolated.execute(tool_name, arguments or {}, timeout=30)
                duration = int((time.time() - start_time) * 1000)
                safety = _SAFETY_MAP.get(spec.safety, spec.safety)
                success = not any(w in str(result).lower()
                                  for w in ["error", "failed", "blocked", "timed out"])
                log_tool_execution(
                    tool_name=tool_name, arguments=arguments,
                    result=result, safety_level=safety,
                    success=success, user_utterance=user_input,
                    mode=mode, duration_ms=duration,
                )
                if log_action_fn:
                    log_action_fn("brain",
                                  f"{tool_name}({json.dumps(arguments)[:100]})",
                                  str(result)[:200])
                return result
            except ImportError:
                pass  # Fall through to in-process execution

        if spec is None:
            # Not in registry — delegate to legacy path
            if fallback_fn:
                result = fallback_fn(tool_name, arguments, action_registry,
                                     reminder_mgr, speak_fn)
                # Audit legacy tool calls too
                duration = int((time.time() - start_time) * 1000)
                safety = get_safety_level(tool_name)
                success = not any(w in str(result).lower()
                                  for w in ["error", "failed", "blocked"])
                log_tool_execution(
                    tool_name=tool_name, arguments=arguments,
                    result=result, safety_level=safety,
                    success=success, user_utterance=user_input,
                    mode=mode, duration_ms=duration,
                )
                return result
            return f"Unknown tool: {tool_name}"

        # Resolve safety level (spec or global classification)
        safety = _SAFETY_MAP.get(spec.safety, spec.safety)
        global_safety = get_safety_level(tool_name)
        # Use the stricter of spec-declared and global classification
        if _SAFETY_MAP.get(global_safety, global_safety) in (SENSITIVE, CRITICAL):
            if _level_rank(global_safety) > _level_rank(safety):
                safety = global_safety

        # --- Pre-execution: terminal safety check ---
        if tool_name == "run_terminal":
            cmd = arguments.get("command", "")
            allowed, reason = check_terminal_safety(cmd)
            if not allowed:
                log_tool_execution(
                    tool_name=tool_name, arguments=arguments,
                    result=reason, safety_level=safety,
                    confirmation_status="not_required",
                    success=False, user_utterance=user_input,
                    mode=mode, error=reason,
                )
                return reason

        # --- Dry-run mode ---
        if dry_run_mode and supports_dry_run(tool_name):
            preview = dry_run(tool_name, arguments)
            log_tool_execution(
                tool_name=tool_name, arguments=arguments,
                result=preview, safety_level=safety,
                dry_run=True, success=True,
                user_utterance=user_input, mode=mode,
            )
            return preview

        # --- Pre-execution: confirmation ---
        confirmation_status = "not_required"
        if safety in (SENSITIVE, CRITICAL):
            # Check spec-level confirm_condition first
            if spec.confirm_condition:
                desc = spec.confirm_condition(arguments)
                if desc:
                    allowed, confirmation_status = get_confirmation_status(
                        tool_name, arguments, speak_fn)
                    if not allowed:
                        log_tool_execution(
                            tool_name=tool_name, arguments=arguments,
                            result="Cancelled by user",
                            safety_level=safety,
                            confirmation_status=confirmation_status,
                            success=False, user_utterance=user_input,
                            mode=mode,
                        )
                        return f"Cancelled — user did not confirm {tool_name}."
            else:
                # Use global confirmation check
                desc = needs_confirmation(tool_name, arguments)
                if desc:
                    allowed, confirmation_status = get_confirmation_status(
                        tool_name, arguments, speak_fn)
                    if not allowed:
                        log_tool_execution(
                            tool_name=tool_name, arguments=arguments,
                            result="Cancelled by user",
                            safety_level=safety,
                            confirmation_status=confirmation_status,
                            success=False, user_utterance=user_input,
                            mode=mode,
                        )
                        return f"Cancelled — user did not confirm {tool_name}."

        # --- Pre-execution: cache check ---
        if spec.cacheable and spec.cache_ttl > 0:
            cached = self.cache.get(tool_name, arguments, spec.cache_ttl)
            if cached is not None:
                try:
                    from core.metrics import metrics
                    metrics.increment("cache_hits")
                except Exception:
                    pass
                return cached
            else:
                try:
                    from core.metrics import metrics
                    metrics.increment("cache_misses")
                except Exception:
                    pass

        # --- Pre-execution: normalize LLM arguments ---
        arguments = self._normalize_arguments(tool_name, arguments)

        # --- Execute handler ---
        error_msg = None
        try:
            _metrics_timer = None
            try:
                from core.metrics import metrics as _m
                _metrics_timer = _m.timer("tool_execution")
                _metrics_timer.__enter__()
            except Exception:
                pass
            try:
                result = self._call_handler(spec, arguments, action_registry,
                                            reminder_mgr, speak_fn, user_input)
            finally:
                if _metrics_timer:
                    try:
                        _metrics_timer.__exit__(None, None, None)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Tool execution error ({tool_name}): {e}")
            error_msg = str(e)
            result = f"Error executing {tool_name}: {e}"

        duration = int((time.time() - start_time) * 1000)
        success = error_msg is None and not any(
            w in str(result).lower()
            for w in ["error", "failed", "blocked", "timed out"])

        # --- Post-execution: cache store ---
        if spec.cacheable and spec.cache_ttl > 0:
            if result and "error" not in str(result).lower():
                self.cache.set(tool_name, arguments, result, ttl=spec.cache_ttl)

        # --- Post-execution: undo registration ---
        if spec.rollback and spec.rollback_description:
            try:
                desc = spec.rollback_description.format(**arguments)
                rollback_fn = (lambda args=arguments, ar=action_registry:
                               spec.rollback(args, ar))
                self.undo_mgr.register(tool_name, arguments, rollback_fn, desc)
            except Exception as e:
                logger.debug(f"Undo registration failed for {tool_name}: {e}")

        # --- Post-execution: action log ---
        if log_action_fn:
            log_action_fn("brain",
                          f"{tool_name}({json.dumps(arguments)[:100]})",
                          str(result)[:200])

        # --- Post-execution: audit log ---
        log_tool_execution(
            tool_name=tool_name, arguments=arguments,
            result=result, safety_level=safety,
            confirmation_status=confirmation_status,
            success=success, user_utterance=user_input,
            mode=mode, duration_ms=duration,
            error=error_msg,
        )

        # --- Post-execution: learning ---
        if experience_learner or cognition:
            self._log_learning(user_input, tool_name, arguments, result,
                               cognition, experience_learner)

        return result

    def _call_handler(self, spec, arguments, action_registry,
                      reminder_mgr, speak_fn, user_input=""):
        """Call the tool handler with appropriate dependencies.

        State-first execution: if orchestrator is available and can handle
        this tool, route through it for structured state tracking. Falls
        back to the spec handler if orchestrator can't handle it or fails.
        """
        # Try state-first execution via orchestrator
        if self._orchestrator and self._orchestrator.can_handle(spec.name, arguments):
            try:
                result = self._orchestrator.execute(spec.name, arguments)
                if result and result.ok:
                    logger.debug(f"Orchestrator handled {spec.name} "
                               f"via {result.strategy_used}")
                    msg = result.message or str(result.state_after)
                    # Mark as orchestrator-verified so brain.py skips re-verification
                    self._last_orchestrator_verified = True
                    return msg
                elif result and result.error:
                    logger.debug(f"Orchestrator failed {spec.name}: {result.error}, "
                               f"falling through to handler")
                # Fall through to handler if orchestrator returned None or failed
            except Exception as e:
                logger.debug(f"Orchestrator error for {spec.name}: {e}")

        # Standard handler call
        kwargs = {"arguments": arguments}
        if spec.requires_registry:
            kwargs["action_registry"] = action_registry
        if spec.requires_reminder_mgr:
            kwargs["reminder_mgr"] = reminder_mgr
        if spec.requires_speak_fn:
            kwargs["speak_fn"] = speak_fn
        if spec.requires_user_input:
            kwargs["user_input"] = user_input
        if spec.requires_quick_chat:
            kwargs["quick_chat_fn"] = getattr(self, '_quick_chat_fn', None)

        # Desktop automation tools need the main thread (pyautogui) — call directly
        if spec.name in _MAIN_THREAD_TOOLS:
            return spec.handler(**kwargs)

        # All other tools get a per-handler timeout to prevent deadlocks
        timeout = _HANDLER_TIMEOUTS.get(spec.name, 30)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(spec.handler, **kwargs)
            try:
                return future.result(timeout=timeout)
            except FuturesTimeout:
                logger.warning(f"Tool {spec.name} timed out after {timeout}s")
                return f"Tool {spec.name} timed out after {timeout}s"

    @staticmethod
    def _normalize_arguments(tool_name, arguments):
        """Fix common LLM argument hallucinations.

        qwen2.5:7b sometimes generates plausible but wrong argument values.
        This normalizes them before execution.
        """
        if not arguments or not isinstance(arguments, dict):
            return arguments or {}

        args = dict(arguments)  # Shallow copy

        # Normalize app names: strip common suffixes the LLM adds
        if tool_name in ("open_app", "close_app", "minimize_app", "focus_window"):
            name = args.get("name", "")
            if name:
                # "Google Chrome" → "Chrome", "Mozilla Firefox" → "Firefox"
                _APP_NORMALIZATIONS = {
                    "google chrome": "Chrome",
                    "mozilla firefox": "Firefox",
                    "microsoft edge": "Edge",
                    "microsoft word": "Word",
                    "microsoft excel": "Excel",
                    "microsoft powerpoint": "PowerPoint",
                    "microsoft outlook": "Outlook",
                    "windows explorer": "Explorer",
                    "file explorer": "Explorer",
                    "visual studio code": "VS Code",
                    "windows terminal": "Terminal",
                }
                normalized = _APP_NORMALIZATIONS.get(name.lower())
                if normalized:
                    args["name"] = normalized

        # Ensure required "action" field isn't empty for multi-action tools
        if tool_name == "manage_files" and not args.get("action"):
            args["action"] = "list"
        if tool_name == "manage_software" and not args.get("action"):
            args["action"] = "search"

        # Strip surrounding quotes from paths (LLM sometimes adds them)
        for key in ("path", "destination", "command"):
            val = args.get(key, "")
            if isinstance(val, str) and len(val) > 2:
                if (val.startswith('"') and val.endswith('"')
                        or val.startswith("'") and val.endswith("'")):
                    args[key] = val[1:-1]

        return args

    def shutdown(self):
        """Clean up resources (e.g. isolated worker processes)."""
        if hasattr(self, '_isolated'):
            self._isolated.shutdown()

    def _log_learning(self, user_input, tool_name, arguments, result,
                      cognition, experience_learner):
        """Log tool outcome for cognitive learning."""
        result_str = str(result) if result else ""
        is_success = not any(w in result_str.lower() for w in [
            "error", "failed", "not found", "blocked", "timed out",
            "permission denied", "could not", "couldn't",
        ])
        if cognition:
            try:
                cognition.log_outcome(user_input, tool_name, arguments,
                                      is_success, result_str)
                return
            except Exception:
                pass
        if experience_learner:
            try:
                experience_learner.log_outcome(user_input, tool_name,
                                               arguments, is_success,
                                               result_str)
            except Exception:
                pass


def _level_rank(level):
    """Return numeric rank for safety level comparison."""
    _RANKS = {SAFE: 0, MODERATE: 1, SENSITIVE: 2, CRITICAL: 3}
    return _RANKS.get(level, 1)
