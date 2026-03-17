"""
Executor Agent — Desktop task execution.

Wraps the existing DesktopAgent + StrategySelector + tool executor
to run individual plan steps. Each step goes through:

  1. Try direct strategy (CLI/API/Tool) — fastest path
  2. Try desktop agent observe-think-act for UI steps
  3. Report result + success/failure to blackboard

The Executor is the workhorse — it handles one step at a time,
called repeatedly by the Orchestrator.
"""

import logging
import re
import time

from .base import BaseAgent
from .blackboard import PlanNode

logger = logging.getLogger(__name__)

# Max attempts per single step before giving up
MAX_STEP_ATTEMPTS = 3
# Max time for a single step (seconds)
STEP_TIMEOUT = 120


class ExecutorAgent(BaseAgent):
    """Executes individual plan steps using the best available strategy."""

    name = "executor"
    role = "Desktop automation executor that runs individual task steps"

    def __init__(self, llm_fn, blackboard, brain=None, action_registry=None, **kwargs):
        super().__init__(llm_fn, blackboard, **kwargs)
        self.brain = brain
        self.action_registry = action_registry or {}

    def run(self, step: PlanNode = None, **kwargs) -> dict:
        """Execute a single plan step.

        Args:
            step: PlanNode to execute. If None, gets next from blackboard.

        Returns:
            {"status": "ok"|"failed"|"takeover", "result": str, "duration": float}
        """
        if step is None:
            step = self.bb.get_current_step()
        if step is None:
            return {"status": "error", "result": "No step to execute"}

        self._log(f"Executing step {step.id}: {step.description[:80]}")
        self.bb.mark_step(step.id, "in_progress")
        t0 = time.perf_counter()

        # --- Takeover check ---
        if step.takeover:
            self._log(f"Step {step.id} requires user takeover")
            self.bb.mark_step(step.id, "done", result="User takeover requested")
            return {"status": "takeover", "result": step.description}

        # --- Attempt execution (up to MAX_STEP_ATTEMPTS) ---
        last_error = ""
        for attempt in range(1, MAX_STEP_ATTEMPTS + 1):
            try:
                result = self._execute_step(step)
                elapsed = time.perf_counter() - t0

                if result["success"]:
                    self.bb.mark_step(step.id, "done", result=result["output"])
                    self.bb.log_action(
                        result.get("tool", step.tool_hint),
                        result.get("args", {}),
                        result["output"],
                        True,
                        elapsed,
                    )
                    return {"status": "ok", "result": result["output"], "duration": elapsed}

                last_error = result.get("error", "Unknown failure")
                self._log(f"Step {step.id} attempt {attempt} failed: {last_error[:80]}")
                self.bb.log_action(
                    result.get("tool", step.tool_hint),
                    result.get("args", {}),
                    last_error,
                    False,
                    elapsed,
                )

            except Exception as e:
                last_error = str(e)
                logger.warning(f"Step {step.id} exception on attempt {attempt}: {e}")

            # Check timeout
            if time.perf_counter() - t0 > STEP_TIMEOUT:
                break

        # All attempts failed
        elapsed = time.perf_counter() - t0
        self.bb.mark_step(step.id, "failed", result=last_error)
        self.bb.append("errors", {"step": step.id, "error": last_error})
        return {"status": "failed", "result": last_error, "duration": elapsed}

    def _execute_step(self, step: PlanNode) -> dict:
        """Try to execute a step through multiple strategies.

        Returns: {"success": bool, "output": str, "tool": str, "args": dict}
        """
        desc = step.description
        tool_hint = step.tool_hint

        # --- Strategy 1: Direct tool dispatch (fastest) ---
        result = self._try_direct_dispatch(desc, tool_hint)
        if result is not None:
            return result

        # --- Strategy 2: Strategy selector (CLI/API/Website/Tool/UIA/CDP) ---
        result = self._try_strategy_selector(desc)
        if result is not None:
            return result

        # --- Strategy 3: Full desktop agent (observe-think-act) ---
        result = self._try_desktop_agent(desc)
        if result is not None:
            return result

        return {"success": False, "error": f"No strategy could execute: {desc}", "tool": "", "args": {}}

    # Required args per tool — if missing, skip direct dispatch and let strategy
    # selector handle it with full NL context instead of malformed args.
    _REQUIRED_ARGS = {
        "open_app":     ("name",),
        "close_app":    ("name",),
        "google_search":("query",),
        "browser_action":("url",),
        "type_text":    ("text",),
        "run_terminal": ("command",),
        "set_reminder": ("message",),
    }

    def _try_direct_dispatch(self, desc: str, tool_hint: str) -> dict | None:
        """Try direct tool execution if we know the tool."""
        if not tool_hint:
            return None

        # Build args from description
        args = self._extract_args(desc, tool_hint)

        # Validate required args — if any are missing/empty, don't call
        # execute_tool with bad args (it would silently fail or do wrong thing)
        required = self._REQUIRED_ARGS.get(tool_hint, ())
        for req_key in required:
            val = args.get(req_key, "")
            if not val or (isinstance(val, str) and len(val.strip()) < 2):
                logger.debug(f"Direct dispatch skipped for {tool_hint}: missing required arg '{req_key}'")
                return None  # Let strategy selector handle with full context

        try:
            from brain import execute_tool
            execute_tool._last_user_input = desc
            if self.brain:
                execute_tool._brain_quick_chat = self.brain.quick_chat
            result = execute_tool(tool_hint, args, self.action_registry)
            if result:
                result_str = str(result)
                # Check for error indicators
                if any(w in result_str.lower()[:80] for w in ["error:", "failed:", "not found", "timed out"]):
                    return {"success": False, "error": result_str, "tool": tool_hint, "args": args}
                return {"success": True, "output": result_str, "tool": tool_hint, "args": args}
        except Exception as e:
            logger.debug(f"Direct dispatch failed for {tool_hint}: {e}")

        return None

    def _try_strategy_selector(self, desc: str) -> dict | None:
        """Try execution_strategies.StrategySelector."""
        try:
            from execution_strategies import get_selector, gather_context
            selector = get_selector()
            ctx = gather_context()
            result, strategy = selector.execute_step(
                desc, context=ctx,
                action_registry=self.action_registry,
                skip_vision=True,
            )
            if result and strategy:
                return {"success": True, "output": str(result), "tool": strategy, "args": {}}
        except Exception as e:
            logger.debug(f"Strategy selector failed: {e}")
        return None

    def _try_desktop_agent(self, desc: str) -> dict | None:
        """Use full desktop agent for complex UI tasks."""
        try:
            from desktop_agent import DesktopAgent
            agent = DesktopAgent(
                llm_fn=self.llm,
                action_registry=self.action_registry,
                speak_fn=self.config.get("speak_fn"),
            )
            result = agent.execute(desc)
            if result:
                return {"success": True, "output": str(result), "tool": "desktop_agent", "args": {}}
        except Exception as e:
            logger.debug(f"Desktop agent failed: {e}")
        return None

    def _extract_args(self, desc: str, tool: str) -> dict:
        """Extract tool arguments from natural language description."""
        lower = desc.lower()
        args = {}

        if tool in ("open_app", "close_app", "minimize_app", "focus_window"):
            # Extract app name
            m = re.search(r'(?:open|close|launch|minimize|focus)\s+(?:the\s+)?(.+?)(?:\s+(?:app|application|window))?$', lower)
            if m:
                args["name"] = m.group(1).strip()

        elif tool == "google_search":
            m = re.search(r'(?:search|google)\s+(?:for\s+)?(.+?)(?:\s+on\s+google)?$', lower)
            if m:
                args["query"] = m.group(1).strip()

        elif tool == "browser_action":
            m = re.search(r'(?:go to|navigate to|open|visit)\s+(.+)', lower)
            if m:
                args["action"] = "navigate"
                target = m.group(1).strip()
                if not target.startswith("http"):
                    target = "https://" + target
                args["url"] = target

        elif tool == "type_text":
            m = re.search(r'(?:type|enter|input)\s+["\']?(.+?)["\']?\s*(?:in|into)?', lower)
            if m:
                args["text"] = m.group(1).strip()

        elif tool == "click_at":
            m = re.search(r'click\s+(?:on\s+)?(?:the\s+)?(.+)', lower)
            if m:
                args["description"] = m.group(1).strip()

        elif tool == "run_terminal":
            m = re.search(r'(?:run|execute)\s+(.+)', lower)
            if m:
                args["command"] = m.group(1).strip()

        elif tool == "create_file":
            args["description"] = desc

        elif tool == "set_reminder":
            m = re.search(r'remind(?:er)?\s+(?:me\s+)?(?:to\s+)?(.+?)(?:\s+(?:at|in)\s+(.+))?$', lower)
            if m:
                args["message"] = m.group(1).strip()
                args["time"] = (m.group(2) or "in 1 hour").strip()

        # Fallback: pass description as primary arg
        if not args:
            args["description"] = desc

        return args
